import asyncio
import os
import re
import shutil
import sys
from json.decoder import JSONDecodeError
from typing import Optional
from urllib.parse import ParseResult, urljoin, urlparse

import boto3
import botocore
import pystac
import requests
import stac_asset

DATAHUB_DOMAIN = os.getenv("DATAHUB_DOMAIN", "staging.eodatahub.org.uk")

DATAHUB_WORKSPACE_DOMAIN = os.getenv(
    "DATAHUB_WORKSPACE_DOMAIN", "staging.eodatahub-workspaces.org.uk"
)

API_TOKEN = os.getenv("API_TOKEN", "your_token_value")


async def stage_url(
    location: str,
    token: Optional[str] = None,
    datahub_domain: Optional[str] = None,
    datahub_workspace_domain: Optional[str] = None,
) -> pystac.Catalog:
    """Stage in a STAC item from a URL."""
    try:
        parsed_url = urlparse(location)
        domain = parsed_url.netloc
        split_domain = domain.split(".", 1)[1]
        # Check if location is within the EODataHub Resource Catalogue
        # If token is provided, pass it to the API
        if datahub_domain and domain == datahub_domain:
            if token:
                response = requests.get(location, headers={"Authorization": f"Bearer {token}"})
            else:
                response = requests.get(location)
            item = pystac.Item.from_dict(response.json())
        elif datahub_workspace_domain and split_domain == datahub_workspace_domain:
            if token:
                response = requests.get(location, headers={"Authorization": f"Bearer {token}"})
            else:
                response = requests.get(location)
            item = pystac.Item.from_dict(response.json())
        else:
            response = requests.get(location)
            item = pystac.Item.from_dict(response.json())
    except FileNotFoundError:
        print(f"The file at {location} was not found.", file=sys.stderr)
        raise
    except JSONDecodeError:
        print(f"The file at {location} is not a valid JSON file.", file=sys.stderr)
        raise
    except Exception as e:
        print(f"Failed to read item at {location}: {e}", file=sys.stderr)
        raise

    return item


async def stage_path(
    location: str,
) -> pystac.Catalog:
    """Stage in a STAC item from a file path."""
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

    return item


async def stage_url_or_path(
    config: stac_asset.Config,
    location: str,
    token: Optional[str] = None,
    datahub_domain: Optional[str] = None,
    datahub_workspace_domain: Optional[str] = None,
) -> pystac.Catalog:
    """Stage in a STAC item from a URL or file path."""

    print(f"Stage in STAC data at {location}", file=sys.stderr)

    if is_valid_url(location):
        item = await stage_url(location, token, datahub_domain, datahub_workspace_domain)
    elif is_valid_file_path(location):
        item = await stage_path(location)
    else:
        raise ValueError(f"Location {location} is not a valid S3 key, URL or file path")

    os.makedirs(item.id, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(item.id)

    # Remove other links from item to avoid auth issues
    item.links = [link for link in item.links if link.rel == "self"]

    # Check if assets contain links to DataHub data
    for _, asset in item.assets.items():
        asset_href = asset.href
        print(asset_href)
        parsed_url = urlparse(asset_href)
        domain = parsed_url.netloc
        try:
            split_domain = domain.split(".", 1)[1]
            # Check if location is within the EODataHub Resource Catalogue
            # If token is provided, pass it to the API
            if datahub_workspace_domain and split_domain == datahub_workspace_domain:
                if token:
                    print(f"Trying to download {asset_href} with api token")
                    ## Download data from href and save to file
                    response = requests.get(
                        asset_href, headers={"Authorization": f"Bearer {token}"}
                    )
                    print(response.status_code)
                    if response.status_code == 200:
                        filename = os.path.basename(asset_href)
                        print(f"Downloading {asset_href} to {filename}", file=sys.stderr)
                        with open(f"{filename}", "wb") as f:
                            f.write(response.content)
                    else:
                        print(f"Failed to download {asset_href}: {response.status_code}")
        except IndexError:
            print(f"Error parsing domain from {asset_href}")
            continue

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

    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as e:
        print(
            f"Failed to download item {file_name} in bucket {bucket}, key {key}: {e}",
            file=sys.stderr,
        )
        raise

    # Create a temporary file and write the object content to it
    with open(file_name, "wb") as file:
        file.write(obj["Body"].read())
        file_path = os.path.abspath(file.name)
    return file_path


def parse_s3_url_components(parsed_url: ParseResult) -> tuple:
    """Parse S3 URL components and return access point/bucket, item key and file name."""
    if parsed_url.netloc.endswith(":accesspoint"):
        parts = parsed_url.path.lstrip("/").split("/", 1)
        bucket_name = f"{parsed_url.netloc}/{parts[0]}"
        item_key = parts[1]
        file_name = item_key.split("/")[-1]
    else:
        bucket_name = f"{parsed_url.netloc}"
        item_key = parsed_url.path.lstrip("/")
        file_name = item_key.split("/")[-1]
    return bucket_name, item_key, file_name


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
    bucket_name, item_key, file_name = parse_s3_url_components(parsed_url)
    file_path = download_s3(bucket_name, item_key, file_name, s3_client)

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
            asset_bucket_name, asset_key, asset_name = parse_s3_url_components(parsed_asset_url)
            asset_path = download_s3(asset_bucket_name, asset_key, asset_name, s3_client)
            asset.href = asset_path
        elif parsed_asset_url.scheme in ["http", "https"]:
            print(f"Downloading asset {asset_href} via URL", file=sys.stderr)
            asset_path = await stac_asset.download_asset(asset, directory=".", config=config)
            asset.href = asset_path
        elif parsed_asset_url.scheme == "":
            print(f"Downloading asset {asset_href} via relative S3", file=sys.stderr)
            asset_key = urljoin(item_key, asset_href)
            asset_name = asset_key.split("/")[-1]
            asset_path = download_s3(bucket_name, asset_key, asset_name, s3_client)
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


def is_valid_url(url: str) -> bool:
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


def get_aws_client() -> boto3.client:
    # Configure AWS client
    if (
        os.getenv("AWS_ACCESS_KEY_ID")
        and os.getenv("AWS_SECRET_ACCESS_KEY")
        and os.getenv("AWS_SESSION_TOKEN")
    ):
        try:
            s3_client = boto3.client(
                service_name="s3",
                use_ssl=True,
                aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
            )
        except Exception as e:
            print(f"Failed to configure AWS client: {e}", file=sys.stderr)
            raise
    else:
        raise ValueError("AWS credentials not provided")
    return s3_client


def main(config: stac_asset.Config, location: str):
    if location.startswith("s3://"):
        s3_client = get_aws_client()
        cat = asyncio.run(stage_s3(config, location, s3_client))
    else:
        cat = asyncio.run(
            stage_url_or_path(config, location, API_TOKEN, DATAHUB_DOMAIN, DATAHUB_WORKSPACE_DOMAIN)
        )
    return cat


if __name__ == "__main__":

    config = stac_asset.Config(warn=True)
    location = sys.argv[1]

    cat = main(config, location)

    print(f"Catalog {cat.id} staged successfully.", file=sys.stderr)
