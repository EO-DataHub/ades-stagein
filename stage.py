import asyncio
import logging
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

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


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
            response.raise_for_status()
            item_json = response.json()
            item = pystac.Item.from_dict(item_json)
        elif datahub_workspace_domain and split_domain == datahub_workspace_domain:
            if token:
                response = requests.get(location, headers={"Authorization": f"Bearer {token}"})
            else:
                response = requests.get(location)
            response.raise_for_status()
            item_json = response.json()
            item = pystac.Item.from_dict(item_json)
        else:
            response = requests.get(location)
            response.raise_for_status()
            item_json = response.json()
            item = pystac.Item.from_dict(item_json)
    except JSONDecodeError:
        logger.error("The file at %s is not a valid JSON file.", location)
        raise
    except Exception as e:
        logger.error("Failed to read item at %s: %s", location, e)
        raise

    return item


async def stage_path(
    location: str,
) -> pystac.Catalog:
    """Stage in a STAC item from a file path."""
    try:
        item = pystac.read_file(location)
    except FileNotFoundError:
        logger.error("The file at %s was not found.", location)
        raise
    except JSONDecodeError:
        logger.error("The file at %s is not a valid JSON file.", location)
        raise
    except Exception as e:
        logger.error("Failed to read item at %s: %s", location, e)
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

    logger.info("Stage in STAC data at %s", location)

    if is_valid_url(location):
        item = await stage_url(location, token, datahub_domain, datahub_workspace_domain)
    elif is_valid_file_path(location):
        item = await stage_path(location)
    else:
        raise ValueError(f"Location {location} is not a valid S3 key, URL or file path")

    os.makedirs(item.id, exist_ok=True)
    cwd = os.getcwd()
    os.chdir(item.id)

    # Remove other links from item to avoid parent issues
    item.links = [link for link in item.links if link.rel == "self"]

    # Check if assets contain links to DataHub data
    for _, asset in item.assets.items():
        asset_href = asset.href
        parsed_url = urlparse(asset_href)
        domain = parsed_url.netloc
        try:
            split_domain = domain.split(".", 1)[1]
        except IndexError:
            split_domain = None
        # Check if location is within the EODataHub Resource Catalogue
        # If token is provided, pass it to the API
        if (
            datahub_workspace_domain and split_domain and split_domain == datahub_workspace_domain
        ) or (datahub_domain and domain == datahub_domain):
            if token:
                logger.info("Trying to download %s with api token", asset_href)
                ## Download data from href and save to file
                response = requests.get(asset_href, headers={"Authorization": f"Bearer {token}"})
                if response.status_code == 200:
                    filename = os.path.basename(asset_href)
                    logger.info("Saving %s to %s", asset_href, filename)
                    with open(f"{filename}", "wb") as f:
                        f.write(response.content)
                else:
                    logger.error("Failed to download %s: %s", asset_href, response.status_code)
                    response.raise_for_status()

    item = await stac_asset.download_item(item=item, directory=".", config=config)
    logger.info("Item and assets downloaded: %s", os.listdir("."))
    os.chdir(cwd)

    cat = pystac.Catalog(
        id="catalog",
        description=f"catalog with staged {item.id}",
        title=f"catalog with staged {item.id}",
    )
    cat.add_item(item)
    cat.normalize_hrefs("./")
    cat.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    logger.info("Catalog %s normalised and saved.", cat.id)

    return cat


def download_s3(bucket: str, key: str, file_name: str, s3_client: boto3.client) -> str:
    """Download an item from S3."""
    logger.info("Downloading item %s in bucket %s, key %s", file_name, bucket, key)

    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as e:
        logger.info(
            "Failed to download item %s in bucket %s, key %s: %s", file_name, bucket, key, e
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
    logger.info("Stage in S3 URL %s", href)
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
    logger.info("Item %s moved to %s and re-ingested by pystac", item.id, new_file_path)

    for asset_key, asset in item.assets.items():
        asset_href = asset.href
        parsed_asset_url = urlparse(asset_href)
        if parsed_asset_url.scheme == "s3":
            logger.info("Downloading asset %s via S3", asset_href)
            asset_bucket_name, asset_key, asset_name = parse_s3_url_components(parsed_asset_url)
            asset_path = download_s3(asset_bucket_name, asset_key, asset_name, s3_client)
            asset.href = asset_path
        elif parsed_asset_url.scheme in ["http", "https"]:
            logger.info("Downloading asset %s via URL", asset_href)
            asset_path = await stac_asset.download_asset(asset, directory=".", config=config)
            asset.href = asset_path
        elif parsed_asset_url.scheme == "":
            logger.info("Downloading asset %s via relative S3", asset_href)
            asset_key = urljoin(item_key, asset_href)
            asset_name = asset_key.split("/")[-1]
            asset_path = download_s3(bucket_name, asset_key, asset_name, s3_client)
        else:
            logger.warning("Asset %s not supported", asset_href)
            continue

    logger.info("Item and assets downloaded: %s", os.listdir("."))
    os.chdir(cwd)

    cat = pystac.Catalog(
        id="catalog",
        description=f"catalog with staged {item.id}",
        title=f"catalog with staged {item.id}",
    )
    cat.add_item(item)
    cat.normalize_hrefs("./")
    cat.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    logger.info("Catalog %s normalised and saved.", cat.id)

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
            logger.info("Failed to configure AWS client: %s", e)
            raise
    else:
        s3_client = boto3.client("s3")
    return s3_client


def main(config: stac_asset.Config, location: str):
    if location.startswith("s3://"):
        s3_client = get_aws_client()
        cat = asyncio.run(stage_s3(config, location, s3_client))
    else:
        cat = asyncio.run(
            stage_url_or_path(
                config,
                location,
                os.getenv("WORKSPACE_ACCESS_TOKEN"),
                os.getenv("DATAHUB_DOMAIN"),
                os.getenv("DATAHUB_WORKSPACE_DOMAIN"),
            )
        )
    return cat


if __name__ == "__main__":

    config = stac_asset.Config(warn=True)
    location = sys.argv[1]

    cat = main(config, location)

    logger.info("Catalog %s staged successfully.", cat.id)
