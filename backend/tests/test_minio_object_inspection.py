import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from services.storage.minio.minio_storage import MinioUploadService


def test_inspection_reads_only_object_head_and_preserves_internal_key():
    calls = []
    modified = datetime(2026, 10, 16, tzinfo=timezone.utc)

    def head(**kwargs):
        calls.append(kwargs)
        return {"ETag": '"multipart-2"', "ContentLength": 123, "LastModified": modified}

    storage = object.__new__(MinioUploadService)
    storage.bucket = "test"
    storage.client = SimpleNamespace(head_object=head)
    key = "derived/doc/attempts/image a.png"
    assert asyncio.run(storage.inspect_object(key)) == {
        "storage_key": key, "etag": '"multipart-2"', "size": 123,
        "last_modified": modified.isoformat(),
    }
    assert calls == [{"Bucket": "test", "Key": key}]


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "AccessDenied", "NoSuchBucket", "500"])
def test_only_missing_object_errors_return_none(code):
    error = ClientError({"Error": {"Code": code}}, "HeadObject")

    def head(**kwargs):
        raise error

    storage = object.__new__(MinioUploadService)
    storage.bucket = "test"
    storage.client = SimpleNamespace(head_object=head)
    if code in {"404", "NoSuchKey"}:
        assert asyncio.run(storage.inspect_object("uploads/file.pdf")) is None
    else:
        with pytest.raises(ClientError) as raised:
            asyncio.run(storage.inspect_object("uploads/file.pdf"))
        assert raised.value is error


def test_inspection_propagates_connection_errors():
    error = EndpointConnectionError(endpoint_url="http://unavailable")

    def head(**kwargs):
        raise error

    storage = object.__new__(MinioUploadService)
    storage.bucket = "test"
    storage.client = SimpleNamespace(head_object=head)
    with pytest.raises(EndpointConnectionError) as raised:
        asyncio.run(storage.inspect_object("derived/doc/result.json"))
    assert raised.value is error
