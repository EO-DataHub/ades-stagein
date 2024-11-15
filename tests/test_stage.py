from json.decoder import JSONDecodeError
from unittest import mock
from unittest.mock import patch
from urllib.parse import urlparse

import pystac
import pytest

from stage import (
    download_s3,
    is_valid_file_path,
    is_valid_url,
    parse_s3_url_components,
    stage_s3,
    stage_url_or_path,
)


@patch("pystac.Catalog")
@patch("os.listdir")
@patch("stac_asset.download_item")
@patch("os.chdir")
@patch("os.makedirs")
@patch("pystac.read_file")
@pytest.mark.asyncio
async def test_stage_url_or_path(
    mock_read_file,
    mock_makedirs,
    mock_chdir,
    mock_stac_asset_download_item,
    mock_listdir,
    mock_pystac_catalog,
):

    # Configure mocks
    mock_item = mock.Mock()
    mock_item.id = "item_12346"
    mock_read_file.return_value = mock_item
    mock_makedirs.return_value = None
    mock_chdir.return_value = None
    mock_stac_asset_download_item.return_value = mock_item
    mock_listdir.return_value = ["item_12346"]
    mock_catalog = mock.Mock()
    mock_catalog.add_item.return_value = None
    mock_catalog.normalize_hrefs.return_value = None
    mock_catalog.safe.return_value = None
    mock_pystac_catalog.return_value = mock_catalog
    mock_config = "test-config"

    # Configure test variables
    location = "https://example.com/item.json"

    result = await stage_url_or_path(mock_config, location)

    # Confirm calls were as expected
    mock_read_file.assert_called_once_with(location)
    mock_stac_asset_download_item.assert_called_once_with(
        item=mock_item, directory=".", config=mock_config
    )
    mock_pystac_catalog.assert_called_once_with(
        id="catalog",
        description="catalog with staged item_12346",
        title="catalog with staged item_12346",
    )
    mock_catalog.add_item.assert_called_once_with(mock_item)
    mock_catalog.normalize_hrefs.assert_called_once_with("./")
    mock_catalog.save.assert_called_once_with(catalog_type=pystac.CatalogType.SELF_CONTAINED)

    # Assert result is as expected
    assert result == mock_catalog


@patch("pystac.read_file")
@pytest.mark.asyncio
async def test_stage_url_or_path_not_found(mock_read_file, capsys):

    # Configure mocks
    mock_read_file.side_effect = FileNotFoundError
    mock_config = "test-config"

    # Configure test variables
    location = "https://example.com/item.json"

    with pytest.raises(FileNotFoundError):
        result = await stage_url_or_path(mock_config, location)

        # Assert result is as expected
        assert not result

    # Assert error is handle as expected
    assert "The file at https://example.com/item.json was not found" in capsys.readouterr().err


@patch("pystac.read_file")
@pytest.mark.asyncio
async def test_stage_url_or_path_json_decode_error(mock_read_file, capsys):

    # Configure mocks
    mock_read_file.side_effect = JSONDecodeError("Expecting value", "", 0)
    mock_config = "test-config"

    # Configure test variables
    location = "https://example.com/item.json"

    with pytest.raises(JSONDecodeError):
        result = await stage_url_or_path(mock_config, location)

        # Assert result is as expected
        assert not result

    # Assert error is handle as expected
    assert (
        "The file at https://example.com/item.json is not a valid JSON file"
        in capsys.readouterr().err
    )


@patch("pystac.read_file")
@pytest.mark.asyncio
async def test_stage_url_or_path_error(mock_read_file, capsys):

    # Configure mocks
    mock_read_file.side_effect = RuntimeError
    mock_config = "test-config"

    # Configure test variables
    location = "https://example.com/item.json"

    with pytest.raises(RuntimeError):
        result = await stage_url_or_path(mock_config, location)

        # Assert result is as expected
        assert not result

    # Assert error is handle as expected
    assert "Failed to read item at https://example.com/item.json: " in capsys.readouterr().err


@patch("os.path.abspath")
@patch("builtins.open")
@patch("boto3.client")
def test_download_s3(mock_boto3_client, mock_builtins_open, mock_abspath):

    # Configure mocks
    mock_obj = mock.Mock()
    mock_obj.read.return_value = "example file content"
    mock_boto3_client_instance = mock.Mock()
    mock_boto3_client_instance.get_object.return_value = {"Body": mock_obj}
    mock_boto3_client.return_value = mock_boto3_client_instance

    mock_file = mock.Mock()
    mock_file.write.return_value = None
    mock_file.name = "example-name"
    mock_builtins_open.return_value.__enter__.return_value = mock_file
    mock_builtins_open.__exit__.return_value = None

    mock_abspath.return_value = "/absolute/path/to/example-name"

    # Configure test variables
    bucket = "test-bucket"
    key = "test-key"
    file_name = "test-file"

    result = download_s3(bucket, key, file_name, mock_boto3_client_instance)

    # Confirm calls were as expected
    mock_boto3_client_instance.get_object.assert_called_with(Bucket=bucket, Key=key)
    mock_file.write.assert_called_with("example file content")
    assert mock_abspath.call_count == 1

    # Assert result is as expected
    assert result == "/absolute/path/to/example-name"


def test_parse_s3_url_components():

    # Configure mock
    parsed_url = mock.Mock()
    url = "s3://bucket-name/key/to/file.ex"
    parsed_url = urlparse(url)
    # parsed_url.path = "/some/path/to/file.json"
    # parsed_url.netloc = "www.example.com"

    result = parse_s3_url_components(parsed_url)

    # Assert result is as expected
    assert result[0] == "bucket-name"
    assert result[1] == "key/to/file.ex"
    assert result[2] == "file.ex"


@patch("pystac.Catalog")
@patch("os.listdir")
@patch("shutil.move")
@patch("os.chdir")
@patch("os.getcwd")
@patch("os.makedirs")
@patch("pystac.read_file")
@patch("stage.download_s3")
@pytest.mark.asyncio
async def test_stage_s3_s3_assets(
    mock_download_s3,
    mock_pyastac_read_file,
    mock_makedirs,
    mock_getcwd,
    mock_chdir,
    mock_shutil_move,
    mock_listdir,
    mock_pystac_catalog,
):

    # Configure test variables

    # Configure mocks
    mock_config = "test-config"
    href = "s3://bucket-name/path/to/item.json"
    mock_download_s3.return_value = "/absolute/path/to/item.json"
    mock_makedirs.return_value = None
    mock_getcwd.return_value = "/current/directory"
    mock_chdir.return_value = None
    mock_listdir.return_value = ["item.json"]
    mock_shutil_move.return_value = "/current/directory/item.json"
    mock_item = mock.Mock()
    mock_asset_1 = mock.Mock()
    mock_asset_2 = mock.Mock()
    mock_asset_1.href = "s3://bucket-name/path/to/asset_1.json"
    mock_asset_2.href = "s3://bucket-name/path/to/asset_2.json"
    mock_item.assets.items.return_value = [("asset_1", mock_asset_1), ("asset_2", mock_asset_2)]
    mock_item.id = "item_12346"
    mock_pyastac_read_file.return_value = mock_item
    mock_boto3_client_instance = mock.Mock()

    mock_catalog = mock.Mock()
    mock_catalog.add_item.return_value = None
    mock_catalog.normalize_hrefs.return_value = None
    mock_catalog.save.return_value = None
    mock_pystac_catalog.return_value = mock_catalog

    result = await stage_s3(mock_config, href, mock_boto3_client_instance)

    # Assert result is as expected
    assert result == mock_catalog

    # Assert calls were as expected
    mock_makedirs.assert_called_once_with("item_12346", exist_ok=True)
    mock_getcwd.assert_called_once()

    calls = mock_chdir.call_args_list
    assert calls[0][0][0] == "item_12346"
    assert calls[1][0][0] == "/current/directory"

    calls = mock_pyastac_read_file.call_args_list
    assert calls[0][0][0] == "/absolute/path/to/item.json"
    assert calls[1][0][0] == "/current/directory/item.json"

    calls = mock_download_s3.call_args_list
    assert calls[0][0] == (
        "bucket-name",
        "path/to/item.json",
        "item.json",
        mock_boto3_client_instance,
    )
    assert calls[1][0] == (
        "bucket-name",
        "path/to/asset_1.json",
        "asset_1.json",
        mock_boto3_client_instance,
    )
    assert calls[2][0] == (
        "bucket-name",
        "path/to/asset_2.json",
        "asset_2.json",
        mock_boto3_client_instance,
    )


@patch("stac_asset.download_asset")
@patch("pystac.Catalog")
@patch("os.listdir")
@patch("shutil.move")
@patch("os.chdir")
@patch("os.getcwd")
@patch("os.makedirs")
@patch("pystac.read_file")
@patch("stage.download_s3")
@pytest.mark.asyncio
async def test_stage_s3_url_assets(
    mock_download_s3,
    mock_pyastac_read_file,
    mock_makedirs,
    mock_getcwd,
    mock_chdir,
    mock_shutil_move,
    mock_listdir,
    mock_pystac_catalog,
    mock_stac_asset_download_asset,
):

    # Configure test variables

    # Configure mocks
    mock_config = "test-config"
    href = "s3://bucket-name/path/to/item.json"
    mock_download_s3.return_value = "/absolute/path/to/item.json"
    mock_stac_asset_download_asset.return_value = None
    mock_makedirs.return_value = None
    mock_getcwd.return_value = "/current/directory"
    mock_chdir.return_value = None
    mock_listdir.return_value = ["item.json"]
    mock_shutil_move.return_value = "/current/directory/item.json"
    mock_item = mock.Mock()
    mock_asset_1 = mock.Mock()
    mock_asset_2 = mock.Mock()
    mock_asset_1.href = "https://example.com/path/to/asset_1.json"
    mock_asset_2.href = "https://example.com/path/to/asset_2.json"
    mock_item.assets.items.return_value = [("asset_1", mock_asset_1), ("asset_2", mock_asset_2)]
    mock_item.id = "item_12346"
    mock_pyastac_read_file.return_value = mock_item

    mock_catalog = mock.Mock()
    mock_catalog.add_item.return_value = None
    mock_catalog.normalize_hrefs.return_value = None
    mock_catalog.save.return_value = None
    mock_pystac_catalog.return_value = mock_catalog
    mock_boto3_client_instance = mock.Mock()

    result = await stage_s3(mock_config, href, mock_boto3_client_instance)

    # Assert result is as expected
    assert result == mock_catalog

    # Assert calls were as expected
    mock_makedirs.assert_called_once_with("item_12346", exist_ok=True)
    mock_getcwd.assert_called_once()

    calls = mock_chdir.call_args_list
    assert calls[0][0][0] == "item_12346"
    assert calls[1][0][0] == "/current/directory"

    calls = mock_pyastac_read_file.call_args_list
    assert calls[0][0][0] == "/absolute/path/to/item.json"
    assert calls[1][0][0] == "/current/directory/item.json"

    mock_download_s3.assert_called_once_with(
        "bucket-name", "path/to/item.json", "item.json", mock_boto3_client_instance
    )

    calls = mock_stac_asset_download_asset.call_args_list
    assert calls[0][0][0] == mock_asset_1
    assert calls[0][1] == {"directory": ".", "config": mock_config}
    assert calls[1][0][0] == mock_asset_2
    assert calls[1][1] == {"directory": ".", "config": mock_config}


@patch("stac_asset.download_asset")
@patch("pystac.Catalog")
@patch("os.listdir")
@patch("shutil.move")
@patch("os.chdir")
@patch("os.getcwd")
@patch("os.makedirs")
@patch("pystac.read_file")
@patch("stage.download_s3")
@pytest.mark.asyncio
async def test_stage_s3_relative_assets(
    mock_download_s3,
    mock_pyastac_read_file,
    mock_makedirs,
    mock_getcwd,
    mock_chdir,
    mock_shutil_move,
    mock_listdir,
    mock_pystac_catalog,
    mock_stac_asset_download_asset,
):

    # Configure test variables

    # Configure mocks
    mock_config = "test-config"
    href = "s3://bucket-name/path/to/item.json"
    mock_download_s3.return_value = "/absolute/path/to/item.json"
    mock_stac_asset_download_asset.return_value = None
    mock_makedirs.return_value = None
    mock_getcwd.return_value = "/current/directory"
    mock_chdir.return_value = None
    mock_listdir.return_value = ["item.json"]
    mock_shutil_move.return_value = "/current/directory/item.json"
    mock_item = mock.Mock()
    mock_asset_1 = mock.Mock()
    mock_asset_2 = mock.Mock()
    mock_asset_1.href = "./asset_1.json"
    mock_asset_2.href = "./asset_2.json"
    mock_item.assets.items.return_value = [("asset_1", mock_asset_1), ("asset_2", mock_asset_2)]
    mock_item.id = "item_12346"
    mock_pyastac_read_file.return_value = mock_item
    mock_boto3_client_instance = mock.Mock()

    mock_catalog = mock.Mock()
    mock_catalog.add_item.return_value = None
    mock_catalog.normalize_hrefs.return_value = None
    mock_catalog.save.return_value = None
    mock_pystac_catalog.return_value = mock_catalog

    result = await stage_s3(mock_config, href, mock_boto3_client_instance)

    # Assert result is as expected
    assert result == mock_catalog

    # Assert calls were as expected
    mock_makedirs.assert_called_once_with("item_12346", exist_ok=True)
    mock_getcwd.assert_called_once()

    calls = mock_chdir.call_args_list
    assert calls[0][0][0] == "item_12346"
    assert calls[1][0][0] == "/current/directory"

    calls = mock_pyastac_read_file.call_args_list
    assert calls[0][0][0] == "/absolute/path/to/item.json"
    assert calls[1][0][0] == "/current/directory/item.json"

    calls = mock_download_s3.call_args_list
    assert calls[0][0] == (
        "bucket-name",
        "path/to/item.json",
        "item.json",
        mock_boto3_client_instance,
    )
    assert calls[1][0] == (
        "bucket-name",
        "path/to/asset_1.json",
        "asset_1.json",
        mock_boto3_client_instance,
    )
    assert calls[2][0] == (
        "bucket-name",
        "path/to/asset_2.json",
        "asset_2.json",
        mock_boto3_client_instance,
    )


def test_is_valid_url_valid():

    # Configure test variables
    valid_url = "https://example.com/item.json"

    result = is_valid_url(valid_url)

    # Assert result is as expected
    assert result


def test_is_valid_url_invalid():

    # Configure test variables
    invalid_url = "http:///example.com"

    result = is_valid_url(invalid_url)

    # Assert result is as expected
    assert not result


def test_is_valid_file_path_valid():

    # Configure test variables
    valid_path = "/path/to/file.json"

    result = is_valid_file_path(valid_path)

    # Assert result is as expected
    assert result


def test_is_valid_file_path_invalid():

    # Configure test variables
    invalid_path = "path*/to/file.json"

    result = is_valid_file_path(invalid_path)

    # Assert result is as expected
    assert not result
