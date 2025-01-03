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
import pystac_client
import requests
import stac_asset

RESOURCE_CATALOGUE_PREFIX = os.getenv(
    "RESOURCE_CATALOGUE_PREFIX", "https://test.eodatahub.org.uk/api/catalogue/stac/"
)


async def stage_url_or_path(
    config: stac_asset.Config,
    location: str,
    token: Optional[str] = None,
    resource_catalogue_prefix: Optional[str] = None,
) -> pystac.Catalog:
    """Stage in a STAC item from a URL or file path."""

    print(f"Stage in STAC data at {location}", file=sys.stderr)

    try:
        # Check if location is within the EODataHub Resource Catalogue
        # If token is provided, pass it to the API
        if resource_catalogue_prefix and token and location.startswith(resource_catalogue_prefix):
            item = pystac_client.Client.open(location, headers={"Authorization": f"Bearer {token}"})
        else:
            # Otherwise attempt to access public data
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


def generate_calling_workspace_token(token: str, calling_workspace: Optional[str]):
    KEYCLOAK_BASE_URL = os.environ.get("KEYCLOAK_BASE_URL")
    REALM = os.environ.get("KEYCLOAK_REALM")
    CLIENT_ID = os.environ.get("CLIENT_ID")
    CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

    KEYCLOAK_URL = f"https://{KEYCLOAK_BASE_URL}/realms/{REALM}/protocol/openid-connect/token"
    scope = f"workspace:{calling_workspace}"

    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": token,
        "scope": scope,
    }

    response = requests.post(
        KEYCLOAK_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    return response.json().get("access_token")


def generate_executing_workspace_token(token: str, executing_workspace: Optional[str]):
    KEYCLOAK_BASE_URL = os.environ.get("KEYCLOAK_BASE_URL")
    REALM = os.environ.get("KEYCLOAK_REALM")
    CLIENT_ID = os.environ.get("CLIENT_ID")
    CLIENT_SECRET = os.environ.get("CLIENT_SECRET")

    KEYCLOAK_URL = f"https://{KEYCLOAK_BASE_URL}/realms/{REALM}/protocol/openid-connect/token"
    scope = f"user_services:{executing_workspace}"

    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": token,
        "scope": scope,
    }

    response = requests.post(
        KEYCLOAK_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    return response.json().get("access_token")


def generate_aws_credentials(
    role_arn: str,
    creds_filepath: str,
    token: str,
    calling_workspace: Optional[str] = None,
    executing_workspace: Optional[str] = None,
    save: bool = False,
    additional_path: Optional[str] = None,
) -> dict:

    if not calling_workspace and not executing_workspace:
        raise ValueError("Either calling_workspace or executing_workspace must be provided")

    # Generate and save AWS credentials
    sts_client = boto3.client("sts")

    if calling_workspace:
        workspace_token = generate_calling_workspace_token(token, calling_workspace)
        session_name = f"calling_{calling_workspace}_session"
    elif executing_workspace:
        workspace_token = generate_executing_workspace_token(token, executing_workspace)
        session_name = f"executing_{executing_workspace}_session"

    role = sts_client.assume_role_with_web_identity(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        WebIdentityToken=workspace_token,
    )
    creds = role["Credentials"]

    if save:
        # Save credentials to volume for further mounting
        with open(creds_filepath, "w") as f:
            f.write("[default]\n")
            f.write(f"aws_access_key_id = {creds['AccessKeyId']}\n")
            f.write(f"aws_secret_access_key = {creds['SecretAccessKey']}\n")
            f.write(f"aws_session_token = {creds['SessionToken']}\n")
        print(f"AWS credentials generated and saved to {creds_filepath}", file=sys.stderr)

        if additional_path:
            with open(additional_path, "w") as f:
                f.write("[default]\n")
                f.write(f"aws_access_key_id = {creds['AccessKeyId']}\n")
                f.write(f"aws_secret_access_key = {creds['SecretAccessKey']}\n")
                f.write(f"aws_session_token = {creds['SessionToken']}\n")
            print(f"Credentials additionally saved to {additional_path}", file=sys.stderr)

    return creds


def get_aws_client(creds: dict) -> boto3.client:
    # Configure AWS client
    try:
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
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
    workspace_role_arn = os.getenv("AWS_ROLE_ARN_WORKSPACE")
    user_service_role_arn = os.getenv("AWS_ROLE_ARN_USER_SERVICE")
    region_name = os.getenv("AWS_REGION")
    aws_creds_path = os.getenv("AWS_SHARED_CREDENTIALS_FILE")
    aws_creds_directory = os.path.dirname(aws_creds_path)
    endpoint_url = os.getenv("AWS_S3_ENDPOINT")
    calling_workspace = os.getenv("CALLING_WORKSPACE")
    executing_workspace = os.getenv("EXECUTING_WORKSPACE")

    # Determine if this is a user service
    user_service = calling_workspace != executing_workspace

    creds = {}

    # Generate workspace access creds
    creds = generate_aws_credentials(
        workspace_role_arn,
        f"{aws_creds_directory}/workspace/credentials",
        token,
        calling_workspace=calling_workspace,
        save=True,
        additional_path=f"{aws_creds_directory}/service/credentials" if not user_service else None,
    )

    if user_service:
        # Generate user service credentials
        generate_aws_credentials(
            user_service_role_arn,
            f"{aws_creds_directory}/service/credentials",
            token,
            executing_workspace=executing_workspace,
            save=True,
        )

    if location:
        if location.startswith("s3://"):
            s3_client = get_aws_client(creds)
            cat = asyncio.run(stage_s3(config, location, s3_client))
        elif is_valid_url(location):
            cat = asyncio.run(
                stage_url_or_path(
                    config,
                    location,
                    token=token,
                    resource_catalogue_prefix=RESOURCE_CATALOGUE_PREFIX,
                )
            )
        elif is_valid_file_path(location):
            cat = asyncio.run(stage_url_or_path(config, location))
        else:
            raise ValueError(f"Location {location} is not a valid S3 key, URL or file path")
