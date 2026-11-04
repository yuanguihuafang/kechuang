"""统一 MinIO/S3 客户端配置。"""

from __future__ import annotations

from functools import lru_cache

from runtime.config import get_settings


def minio_config() -> dict[str, str]:
    settings = get_settings()
    return {
        "endpoint": settings.minio_endpoint,
        "public_endpoint": settings.minio_public_endpoint.strip(),
        "access_key": settings.minio_access_key,
        "secret_key": settings.minio_secret_key,
        "bucket": settings.minio_bucket,
    }


def _build_client(endpoint: str, config: dict[str, str]):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


@lru_cache(maxsize=1)
def get_minio_client():
    config = minio_config()
    return _build_client(f"http://{config['endpoint']}", config)


@lru_cache(maxsize=1)
def get_minio_public_client():
    config = minio_config()
    if not config["public_endpoint"]:
        raise RuntimeError("未配置 MINIO_PUBLIC_ENDPOINT，无法生成公网预签名 URL。")
    return _build_client(config["public_endpoint"].rstrip("/"), config)


def get_minio_bucket() -> str:
    return minio_config()["bucket"]
