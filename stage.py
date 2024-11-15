import asyncio
import os
import re
import shutil
import sys
from json.decoder import JSONDecodeError
from urllib.parse import ParseResult, urljoin, urlparse

import boto3
import pystac
import stac_asset


async def stage_url_or_path(config: stac_asset.Config, location: str) -> pystac.Catalog:
    """Stage in a STAC item from a URL or file path."""

    print(f"Stage in STAC data at {location}", file=sys.stderr)

    try:
        item = pystac.read_file(location)
    except FileNotFoundError:
        print(f"The file at {location} was not found.", file=sys.stderr)
        raise
    except JSONDecodeError:
        print(f"The file at {location} is not a valid JSON file.", file=sys.stderr)
        raise
    except Exception as e:
        print(f"Failed to read item at {location}: {e}", file=sys.stderr)
        raise

    os.makedirs(item.id, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(item.id)

    item = await stac_asset.download_item(item=item, directory=".", config=config)
    print(f"Item and assets downloaded: {os.listdir('.')}", file=sys.stderr)
    os.chdir(cwd)

    cat = pystac.Catalog(
        id="catalog",
        description=f"catalog with staged {item.id}",
        title=f"catalog with staged {item.id}",
    )
    cat.add_item(item)
    cat.normalize_hrefs("./")
    cat.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    print(f"Catalog {cat.id} normalised and saved.", file=sys.stderr)

    return cat


def download_s3(bucket: str, key: str, file_name: str, s3_client: boto3.client) -> str:
    """Download an item from S3."""
    print(f"Downloading item {file_name} in bucket {bucket}, key {key}", file=sys.stderr)

    obj = s3_client.get_object(Bucket=bucket, Key=key)

    # Create a temporary file and write the object content to it
    with open(file_name, "wb") as file:
        file.write(obj["Body"].read())
        file_path = os.path.abspath(file.name)
    return file_path


def parse_s3_url_components(parsed_url: ParseResult) -> tuple:
    """Parse S3 URL components and return access point, item key and file name."""
    access_point = f"{parsed_url.netloc}"
    item_key = parsed_url.path.lstrip("/")
    file_name = item_key.split("/")[-1]
    return access_point, item_key, file_name


def get_current_namespace():
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except IOError:
        # Return default namespace
        return "default"


async def stage_s3(config: stac_asset.Config, href: str, s3_client: boto3.client) -> pystac.Catalog:
    """Stage in a STAC item from S3."""
    print(f"Stage in S3 URL {href}", file=sys.stderr)
    # Parse S3 URL
    parsed_url = urlparse(href)
    access_point, item_key, file_name = parse_s3_url_components(parsed_url)
    file_path = download_s3(access_point, item_key, file_name, s3_client)

    item = pystac.read_file(file_path)

    os.makedirs(item.id, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(item.id)

    destination = os.path.join(".", file_name)
    new_file_path = shutil.move(file_path, destination)
    item = pystac.read_file(new_file_path)
    print(f"Item {item.id} moved to {new_file_path} and re-ingested by pystac", file=sys.stderr)

    for asset_key, asset in item.assets.items():
        asset_href = asset.href
        parsed_asset_url = urlparse(asset_href)
        if parsed_asset_url.scheme == "s3":
            print(f"Downloading asset {asset_href} via S3", file=sys.stderr)
            asset_access_point, asset_key, asset_name = parse_s3_url_components(parsed_asset_url)
            asset_path = download_s3(asset_access_point, asset_key, asset_name, s3_client)
            asset.href = asset_path
        elif parsed_asset_url.scheme in ["http", "https"]:
            print(f"Downloading asset {asset_href} via URL", file=sys.stderr)
            asset_path = await stac_asset.download_asset(asset, directory=".", config=config)
            asset.href = asset_path
        elif parsed_asset_url.scheme == "":
            print(f"Downloading asset {asset_href} via relative S3", file=sys.stderr)
            asset_key = urljoin(item_key, asset_href)
            asset_name = asset_key.split("/")[-1]
            asset_path = download_s3(access_point, asset_key, asset_name, s3_client)
        else:
            print(f"Asset {asset_href} not supported", file=sys.stderr)
            continue

    print(f"Item and assets downloaded: {os.listdir('.')}", file=sys.stderr)
    os.chdir(cwd)

    cat = pystac.Catalog(
        id="catalog",
        description=f"catalog with staged {item.id}",
        title=f"catalog with staged {item.id}",
    )
    cat.add_item(item)
    cat.normalize_hrefs("./")
    cat.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    print(f"Catalog {cat.id} normalised and saved.", file=sys.stderr)

    return cat


def is_valid_url(url: str):
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme) and bool(parsed.netloc)
    except Exception:
        return False


def is_valid_file_path(path: str) -> bool:
    """Check if a file path contains illegal characters."""
    illegal_chars_pattern = r'[<>:"|?*]'

    if re.search(illegal_chars_pattern, path):
        return False

    return True


def generate_aws_credentials(role_arn: str, token: str, aws_creds_path: str):
    # Generate and save AWS credentials
    sts_client = boto3.client("sts")

    username = get_current_namespace()

    role = sts_client.assume_role_with_web_identity(
        RoleArn=role_arn,
        RoleSessionName=f"{username}-session",
        WebIdentityToken=token,
    )
    creds = role["Credentials"]

    # Save credentials to volume for further mounting
    with open(aws_creds_path, "w") as f:
        f.write("[default]\n")
        f.write(f"aws_access_key_id = {creds['AccessKeyId']}\n")
        f.write(f"aws_secret_access_key = {creds['SecretAccessKey']}\n")
        f.write(f"aws_session_token = {creds['SessionToken']}\n")


def get_aws_client(region_name: str) -> boto3.client:
    # Configure AWS client
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=region_name,
        )
    else:
        try:
            # First uses credentials file, then service account
            s3_client = boto3.client("s3")
        except Exception as e:
            print(f"Failed to configure AWS client: {e}", file=sys.stderr)
            raise
    return s3_client


if __name__ == "__main__":

    config = stac_asset.Config(warn=True)
    location = None
    token = sys.argv[1]
    if len(sys.argv) > 2:
        location = sys.argv[2]
    role_arn = os.getenv("AWS_ROLE_ARN")
    endpoint_url = os.getenv("AWS_S3_ENDPOINT")
    region_name = os.getenv("AWS_REGION")
    aws_creds_path = os.getenv("AWS_SHARED_CREDENTIALS_FILE")

    # If credentials not found, retrieve them
    if not os.path.exists(aws_creds_path):
        generate_aws_credentials(role_arn, token, aws_creds_path)

    if location:
        if location.startswith("s3://"):
            s3_client = get_aws_client(region_name)
            cat = asyncio.run(stage_s3(config, location, s3_client))
        elif is_valid_url(location):
            cat = asyncio.run(stage_url_or_path(config, location))
        elif is_valid_file_path(location):
            cat = asyncio.run(stage_url_or_path(config, location))
        else:
            raise ValueError(f"Location {location} is not a valid S3 key, URL or file path")
