"""结构化父块的 MinIO 持久化。

Milvus 只保存可检索的子块；Excel 行记录、PPTX 幻灯片组等完整父块和
父子关系保存在 MinIO JSON 中。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from botocore.exceptions import ClientError

from services.storage.minio.client import get_minio_bucket, get_minio_client


class ParentRecordService:
    """保存和读取完整父块，不参与向量检索。"""

    def __init__(self) -> None:
        self.bucket = get_minio_bucket()
        self.client = get_minio_client()

    async def check_ready(self) -> None:
        """只读检查父块与图片清单所在 Bucket。"""

        await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)

    async def save(self, document_id: str, parent_id: str, payload: dict[str, Any]) -> str:
        key = self.object_key(document_id, parent_id)
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
        )
        return key

    async def load(self, document_id: str, parent_id: str) -> dict[str, Any] | None:
        key = self.object_key(document_id, parent_id)
        try:
            response = await asyncio.to_thread(
                self.client.get_object, Bucket=self.bucket, Key=key
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404"}:
                return None
            raise
        payload = json.loads(response["Body"].read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None

    async def load_many(
        self,
        keys: list[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """并行读取唯一父块，同一父块在一次检索中只访问一次 MinIO。"""

        unique_keys = list(dict.fromkeys(keys))
        payloads = await asyncio.gather(*(
            self.load(document_id, parent_id)
            for document_id, parent_id in unique_keys
        ))
        return {
            key: payload
            for key, payload in zip(unique_keys, payloads, strict=True)
            if payload is not None
        }

    @staticmethod
    def object_key(document_id: str, parent_id: str) -> str:
        return f"derived/{_safe_component(document_id)}/records/{_safe_component(parent_id)}.json"


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._")
    if cleaned and cleaned not in {".", ".."}:
        return cleaned[:160]
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
