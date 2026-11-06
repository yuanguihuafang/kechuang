"""Milvus 向量存储实现。

接入 Milvus 原生全文检索：
- BM25 Function：Milvus 从 ``chunk_text`` 自动生成 sparse vector
- Hybrid Search：Milvus 同时执行 dense / BM25 召回
- RRFRanker：Milvus 原生完成两路结果融合
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
import uuid
import warnings
from contextlib import contextmanager
from functools import wraps
from threading import Event, Lock, Thread, current_thread, local
from typing import TYPE_CHECKING, Any

from services.observability.logging import log_event
from services.observability.trace_context import get_trace_context
from services.vector.milvus.sync_priority import milvus_search, mysql_sync_write

COLLECTION_NAME = "solution"
PRIVATE_COLLECTION_NAME = "inner"
PUBLIC_COLLECTIONS = {
    "enterprise": "enterprise",
    "expert": "expert",
    "demand": "demand",
    "achievement": "achievement",
    "solution": "solution",
}
PUBLIC_COLLECTION_ALIASES = {
    knowledge_type: f"kc_public_{knowledge_type}"
    for knowledge_type in PUBLIC_COLLECTIONS
}
MILVUS_ALIAS = "kechuang_milvus"
DIM = 1024  # bge-m3 dense 维度
MILVUS_CHUNK_TEXT_MAX_BYTES = 8192
MILVUS_MAX_QUERY_LIMIT = 16383
MILVUS_QUERY_BATCH_SIZE = 1000
MILVUS_SAFE_UPSERT_BYTES = 8 * 1024 * 1024
MILVUS_LOAD_TIMEOUT_SECONDS = 30

_VARCHAR_LIMITS = {
    "id": 64,
    "user_id": 64,
    "document_id": 64,
    "mysql_id": 128,
    "source_id": 128,
    "source_locator": 512,
    "chunk_text": MILVUS_CHUNK_TEXT_MAX_BYTES,
    "chunk_role": 32,
    "field_names": 2048,
    "status": 32,
    "update_time": 64,
}

if TYPE_CHECKING:
    from agents.retrieval_agent.schemas import RetrievalHit

logger = logging.getLogger(__name__)
recovery_logger = logging.getLogger("kechuang.retrieval.milvus")

_COLLECTION_RECOVERY_DELAYS_SECONDS = (0.0, 1.0, 3.0, 5.0)
_COLLECTION_BACKGROUND_RECOVERY_DELAYS_SECONDS = (5.0, 15.0, 30.0, 60.0)
_PYMILVUS_ORM_DEPRECATION = r".*ORM-style PyMilvus API.*Use `MilvusClient` instead\."


def _configure_pymilvus_warning_filter() -> None:
    """隐藏当前 ORM 适配层的弃用提示，保留其他 PyMilvus 告警和异常。"""

    warnings.filterwarnings(
        "ignore",
        message=_PYMILVUS_ORM_DEPRECATION,
        category=Warning,
    )


def _is_collection_recovering(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "collection on recovering" in message
        or "may be in recovery" in message
        or "do not found any channel in target" in message
    )


def _is_transient_milvus_error(exc: Exception) -> bool:
    """识别可通过重新连接/短暂重试恢复的客户端或服务端错误。"""

    message = str(exc).lower()
    return _is_collection_recovering(exc) or _is_milvus_connection_error(exc) or any(
        marker in message
        for marker in (
            "deadline exceeded",
            "timed out",
            "temporarily unavailable",
            "service unavailable",
        )
    )


def _is_milvus_connection_error(exc: Exception) -> bool:
    """仅传输/连接故障需要失效共享 alias；加载等待超时不属于连接故障。"""

    if isinstance(exc, ConnectionError):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "connection reset",
            "transport is closing",
            "channel is closing",
            "connection refused",
            "broken pipe",
            "should create connection first",
        )
    )


def _utf8_size(value: object) -> int:
    return len(str(value or "").encode("utf-8"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _fenced_write(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._write_guard():
            return method(self, *args, **kwargs)

    return wrapped


def _milvus_write_lock_key(alias_name: str) -> int:
    value = hashlib.sha256(f"kechuang:milvus:{alias_name}".encode("utf-8")).digest()[:8]
    return int.from_bytes(value, byteorder="big", signed=True)


def _lazy_import_pymilvus():
    _configure_pymilvus_warning_filter()
    try:
        from pymilvus import (
            AnnSearchRequest,
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            Function,
            FunctionType,
            RRFRanker,
            connections,
            utility,
        )

        return (
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            Function,
            FunctionType,
            AnnSearchRequest,
            RRFRanker,
            connections,
            utility,
        )
    except ImportError:
        raise ImportError("pymilvus 未安装，请运行: pip install pymilvus")


class MilvusConnectionManager:
    """Own one lazy PyMilvus connection shared by a worker's Collections."""

    def __init__(
        self,
        *,
        uri: str,
        database: str,
        username: str | None,
        password: str | None,
    ) -> None:
        self.uri = uri
        self.database = database
        self.username = username
        self.password = password
        self.alias = f"{MILVUS_ALIAS}_{uuid.uuid4().hex[:8]}"
        self._lock = Lock()
        self._connected = False
        self._generation = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def ensure_connected(self) -> int:
        with self._lock:
            if self._connected:
                return self._generation
            *_, connections, _ = _lazy_import_pymilvus()
            connect_kwargs = {
                "alias": self.alias,
                "uri": self.uri,
                "db_name": self.database,
            }
            if self.username:
                connect_kwargs["user"] = self.username
            if self.password:
                connect_kwargs["password"] = self.password
            connections.connect(**connect_kwargs)
            self._connected = True
            self._generation += 1
            logger.info("Milvus 连接成功: %s, database=%s", self.uri, self.database)
            return self._generation

    def invalidate(self, *, expected_generation: int | None = None) -> None:
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                return
            if not self._connected:
                return
            try:
                *_, connections, _ = _lazy_import_pymilvus()
                connections.disconnect(alias=self.alias)
            finally:
                self._connected = False
                self._generation += 1

    def close(self) -> None:
        self.invalidate()


class MilvusService:
    """Milvus 原生 BM25 与向量检索实现。"""

    def __init__(
        self,
        collection_name: str = COLLECTION_NAME,
        requires_user_id: bool = False,
        *,
        alias_name: str | None = None,
        connection_manager: MilvusConnectionManager,
    ) -> None:
        from runtime.config import get_settings

        settings = get_settings()
        self.uri = settings.milvus_uri
        self.database = settings.milvus_database
        self.username = settings.milvus_username
        self.password = settings.milvus_password
        self.database_url = settings.database_url
        self.collection_name = alias_name or collection_name
        self.legacy_collection_name = collection_name
        self.alias_name = alias_name
        self._physical_collection_name: str | None = None
        self.requires_user_id = requires_user_id
        self.connection_manager = connection_manager
        self.alias = self.connection_manager.alias
        self._connected = False
        self._connection_generation: int | None = None
        self._collection: Any = None
        self._collection_loaded = False
        self._load_recovery_stop = Event()
        self._load_recovery_lock = Lock()
        self._load_recovery_thread: Thread | None = None
        self._load_recovery_closed = False
        self._write_lock_state = local()

    @contextmanager
    def _write_guard(self):
        alias_name = getattr(self, "alias_name", None)
        if not alias_name:
            yield
            return
        state = getattr(self, "_write_lock_state", None)
        if state is None:
            state = self._write_lock_state = local()
        depth = getattr(state, "depth", 0)
        if depth:
            state.depth = depth + 1
            try:
                yield
            finally:
                state.depth -= 1
            return
        if not getattr(self, "database_url", None):
            raise RuntimeError("PostgreSQL 数据库地址未配置，不能绕过 Milvus 写入栅栏")
        from services.persistence.sqlalchemy import postgres_advisory_lock

        with postgres_advisory_lock(
            self.database_url,
            _milvus_write_lock_key(alias_name),
            shared=True,
        ):
            state.depth = 1
            try:
                yield
            finally:
                state.depth = 0

    @contextmanager
    def _maintenance_guard(self):
        if not self.alias_name or not getattr(self, "database_url", None):
            if self.alias_name:
                raise RuntimeError("PostgreSQL 数据库地址未配置，不能维护 Milvus 稳定别名")
            yield
            return
        from services.persistence.sqlalchemy import postgres_advisory_lock

        with postgres_advisory_lock(
            self.database_url,
            _milvus_write_lock_key(self.alias_name),
            shared=False,
        ):
            yield

    @property
    def connection_alias(self) -> str:
        return getattr(self, "alias", MILVUS_ALIAS)

    @property
    def priority_key(self) -> str:
        return "|".join(
            (
                str(getattr(self, "uri", "")),
                str(getattr(self, "database", "")),
                self.connection_alias,
                str(getattr(self, "collection_name", COLLECTION_NAME)),
            )
        )

    def _ensure_connection(self) -> None:
        generation = self.connection_manager.ensure_connected()
        if self._connection_generation != generation:
            self._collection = None
            self._collection_loaded = False
            self._connection_generation = generation
        self._connected = True

    def close(self) -> None:
        self.stop_collection_load_recovery()
        try:
            self.connection_manager.invalidate()
        finally:
            self._connected = False
            self._connection_generation = None
            self._collection = None
            self._collection_loaded = False

    def stop_collection_load_recovery(self) -> None:
        """等待本 Collection 的后台加载退出后，调用方再关闭共享连接。"""

        with self._load_recovery_lock:
            self._load_recovery_closed = True
            stop_event = self._load_recovery_stop
            thread = self._load_recovery_thread
            stop_event.set()
        if thread is not None and thread is not current_thread():
            thread.join()

    def _start_collection_load_recovery(self, *, traceparent: str | None) -> None:
        delays = _COLLECTION_BACKGROUND_RECOVERY_DELAYS_SECONDS
        if not delays:
            return
        with self._load_recovery_lock:
            if self._load_recovery_closed:
                return
            if self._load_recovery_thread and self._load_recovery_thread.is_alive():
                return
            stop_event = Event()
            thread = Thread(
                target=self._recover_collection_load,
                args=(stop_event, traceparent, delays),
                name=f"milvus-load-{self.collection_name}",
                daemon=True,
            )
            self._load_recovery_stop = stop_event
            self._load_recovery_thread = thread
            log_event(
                recovery_logger,
                "milvus_collection_load_recovery_scheduled",
                logging.WARNING,
                traceparent=traceparent,
                collection=self.collection_name,
                attempts=len(delays),
                retry_delays_seconds=list(delays),
            )
            thread.start()

    def _recover_collection_load(
        self,
        stop_event: Event,
        traceparent: str | None,
        delays: tuple[float, ...],
    ) -> None:
        for attempt, delay in enumerate(delays, start=1):
            if stop_event.wait(delay):
                return
            collection = None
            try:
                self._ensure_connection()
                *_, utility = _lazy_import_pymilvus()
                physical_name = self._resolve_collection_name()
                if physical_name is None:
                    raise RuntimeError(
                        f"Milvus Collection {self.collection_name} 不存在，无法恢复加载"
                    )
                collection = self._existing_collection(load=False)
                if collection is None:
                    raise RuntimeError(
                        f"Milvus Collection {self.collection_name} 不存在，无法恢复加载"
                    )
                collection.load(timeout=MILVUS_LOAD_TIMEOUT_SECONDS)
                state = utility.load_state(
                    physical_name,
                    using=self.connection_alias,
                    timeout=MILVUS_LOAD_TIMEOUT_SECONDS,
                )
                state_name = getattr(state, "name", str(state))
                if state_name != "Loaded":
                    raise RuntimeError(
                        f"Milvus Collection {self.collection_name} load_state={state_name}"
                    )
            except Exception as exc:
                if collection is not None and self._collection is collection:
                    self._collection = None
                self._collection_loaded = False
                if _is_milvus_connection_error(exc):
                    try:
                        self._invalidate_connection()
                    except Exception:
                        logger.exception("后台恢复时清理失效的 Milvus 连接失败")
                recovery_logger.exception(
                    "Milvus Collection 后台加载恢复失败",
                    extra={
                        "structured": {
                            "event": "milvus_collection_load_recovery_failed",
                            "traceparent": traceparent,
                            "database": self.database,
                            "collection": self.collection_name,
                            "attempt": attempt,
                            "attempts": len(delays),
                            "load_state": f"{type(exc).__name__}: {exc}",
                            "error": str(exc),
                        }
                    },
                )
                diagnostics = self._collection_load_diagnostics(collection)
                log_event(
                    recovery_logger,
                    "milvus_collection_load_recovery_diagnostics",
                    logging.ERROR,
                    traceparent=traceparent,
                    collection=self.collection_name,
                    attempt=attempt,
                    **diagnostics,
                )
                if attempt == len(delays):
                    recovery_logger.exception(
                        "Milvus Collection 后台加载恢复耗尽",
                        extra={
                            "structured": {
                                "event": "milvus_collection_load_recovery_exhausted",
                                "traceparent": traceparent,
                                "database": self.database,
                                "collection": self.collection_name,
                                "attempts": len(delays),
                                "load_state": f"{type(exc).__name__}: {exc}",
                                "error": str(exc),
                            }
                        },
                    )
                continue
            if stop_event.is_set():
                return
            self._collection = collection
            self._collection_loaded = True
            log_event(
                recovery_logger,
                "milvus_collection_load_recovered",
                logging.INFO,
                traceparent=traceparent,
                collection=self.collection_name,
                attempt=attempt,
                attempts=len(delays),
            )
            return

    def _collection_load_diagnostics(self, collection: Any) -> dict[str, object]:
        """只读采集失败现场；单项诊断失败不覆盖原始 load 异常。"""

        snapshot: dict[str, object] = {
            "database": self.database,
            "schema_fields": [],
            "milvus_load_state": "unavailable",
            "query_segment_state": "unavailable",
        }
        unavailable: dict[str, str] = {}
        try:
            if collection is None:
                raise RuntimeError("failed collection handle is unavailable")
            snapshot["schema_fields"] = [
                {
                    "name": str(getattr(field, "name", "")),
                    "dtype": str(getattr(field, "dtype", "")),
                    "is_primary": bool(getattr(field, "is_primary", False)),
                    "params": dict(getattr(field, "params", {}) or {}),
                }
                for field in getattr(getattr(collection, "schema", None), "fields", [])
            ]
        except Exception as exc:  # noqa: BLE001 - 诊断失败不能覆盖原始加载异常
            unavailable["schema_fields"] = f"{type(exc).__name__}: {exc}"
        try:
            self._ensure_connection()
            *_, utility = _lazy_import_pymilvus()
            state = utility.load_state(
                self._physical_collection_name or self.collection_name,
                using=self.connection_alias,
                timeout=3,
            )
            snapshot["milvus_load_state"] = getattr(state, "name", str(state))
        except Exception as exc:  # noqa: BLE001 - 诊断失败不能覆盖原始加载异常
            unavailable["milvus_load_state"] = f"{type(exc).__name__}: {exc}"
        try:
            *_, utility = _lazy_import_pymilvus()
            segments = utility.get_query_segment_info(
                self._physical_collection_name or self.collection_name,
                using=self.connection_alias,
                timeout=3,
            )
            fields = (
                "segmentID",
                "collectionID",
                "partitionID",
                "nodeID",
                "nodeIds",
                "state",
                "num_rows",
                "mem_size",
                "index_name",
                "indexID",
                "level",
                "is_sorted",
                "storage_version",
            )
            snapshot["query_segment_state"] = [
                self._query_segment_snapshot(segment, fields) for segment in segments
            ]
        except Exception as exc:  # noqa: BLE001 - 诊断失败不能覆盖原始加载异常
            unavailable["query_segment_state"] = f"{type(exc).__name__}: {exc}"
        if unavailable:
            snapshot["diagnostics_unavailable"] = unavailable
        return snapshot

    @staticmethod
    def _query_segment_snapshot(segment: Any, fields: tuple[str, ...]) -> dict[str, object]:
        descriptor = getattr(segment, "DESCRIPTOR", None)
        result: dict[str, object] = {}
        for name in fields:
            if not hasattr(segment, name):
                continue
            value = getattr(segment, name)
            field_descriptor = (
                descriptor.fields_by_name.get(name) if descriptor is not None else None
            )
            if field_descriptor and field_descriptor.enum_type:
                enum_value = field_descriptor.enum_type.values_by_number.get(value)
                if enum_value is not None:
                    value = enum_value.name
            if name == "nodeIds":
                value = list(value)
            result[name] = value
        return result

    def _invalidate_connection(self) -> None:
        """由 manager 清理故障代次，避免迟到的错误断开已经重连的共享连接。"""

        try:
            self.connection_manager.invalidate(expected_generation=self._connection_generation)
        finally:
            self._connected = False
            self._connection_generation = None
            self._collection = None
            self._collection_loaded = False

    async def check_ready(self) -> None:
        """只读验证必需集合的 schema 与 Loaded 状态，不创建或加载集合。"""

        await asyncio.to_thread(self._check_ready_sync)

    def _check_ready_sync(self) -> None:
        try:
            self._ensure_connection()
            *_, utility = _lazy_import_pymilvus()
            collections = None
            if not getattr(self, "alias_name", None):
                collections = utility.list_collections(using=self.connection_alias)
            physical_name = self._resolve_collection_name(collections=collections)
            if physical_name is None:
                raise RuntimeError(f"Milvus 必需 Collection {self.collection_name} 不存在")
            collection = self._existing_collection(load=False)
            if collection is None:
                raise RuntimeError(f"Milvus 必需 Collection {self.collection_name} 不存在")
            self._assert_source_fields(collection)
            state = utility.load_state(
                physical_name,
                using=self.connection_alias,
                timeout=MILVUS_LOAD_TIMEOUT_SECONDS,
            )
            self._collection_loaded = state.name == "Loaded"
            if not self._collection_loaded:
                raise RuntimeError(
                    f"Milvus Collection {self.collection_name} 未就绪：load_state={state.name}"
                )
        except Exception as exc:
            self._collection = None
            self._collection_loaded = False
            if _is_milvus_connection_error(exc):
                try:
                    self._invalidate_connection()
                except Exception:  # 保留原始 readiness 异常，并记录清理失败。
                    logger.exception("清理失效的 Milvus 连接失败")
            raise

    def _with_collection_recovery_retry(self, operation):
        """短暂恢复期间重试同一读写操作，避免把可恢复状态当成永久故障。"""

        reloaded_after_not_loaded = False
        for attempt, delay in enumerate(_COLLECTION_RECOVERY_DELAYS_SECONDS, start=1):
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except Exception as exc:  # 仅对明确的恢复中/暂时性错误重试。
                is_transient = _is_transient_milvus_error(exc)
                is_not_loaded = "not loaded" in str(exc).lower()
                if (
                    is_not_loaded
                    and self._collection_loaded
                    and self._collection is not None
                    and not reloaded_after_not_loaded
                ):
                    self._collection_loaded = False
                    self._load_collection(self._collection)
                    reloaded_after_not_loaded = True
                    continue
                if not is_transient or attempt == len(_COLLECTION_RECOVERY_DELAYS_SECONDS):
                    if is_transient:
                        self._collection = None
                        self._collection_loaded = False
                    elif is_not_loaded:
                        self._collection_loaded = False
                    if _is_milvus_connection_error(exc):
                        try:
                            self._invalidate_connection()
                        except Exception:  # 保留原始操作异常，并记录清理失败。
                            logger.exception("清理失效的 Milvus 连接失败")
                    if is_transient:
                        log_event(
                            logger,
                            "milvus_operation_failed",
                            logging.ERROR,
                            traceparent=get_trace_context().traceparent,
                            collection=getattr(self, "collection_name", "unknown"),
                            attempt=attempt,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                    raise
                retry_delay = _COLLECTION_RECOVERY_DELAYS_SECONDS[attempt]
                log_event(
                    logger,
                    "milvus_operation_retry",
                    logging.WARNING,
                    traceparent=get_trace_context().traceparent,
                    collection=getattr(self, "collection_name", "unknown"),
                    retry_after_seconds=retry_delay,
                    attempt=attempt,
                    max_attempts=len(_COLLECTION_RECOVERY_DELAYS_SECONDS),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    def _build_schema(self):
        (
            _,
            CollectionSchema,
            DataType,
            FieldSchema,
            Function,
            FunctionType,
            *_,
        ) = _lazy_import_pymilvus()
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
        ]
        if getattr(self, "requires_user_id", False):
            fields.append(FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=64))
        fields.extend(
            [
                FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="mysql_id", dtype=DataType.VARCHAR, max_length=128),
                FieldSchema(name="source_id", dtype=DataType.VARCHAR, max_length=128),
                FieldSchema(name="chunk_index", dtype=DataType.INT32),
                FieldSchema(name="source_locator", dtype=DataType.VARCHAR, max_length=512),
                FieldSchema(
                    name="chunk_text",
                    dtype=DataType.VARCHAR,
                    max_length=8192,
                    enable_analyzer=True,
                    enable_match=True,
                    analyzer_params={
                        "tokenizer": "jieba",
                        "filter": ["cnalphanumonly"],
                    },
                ),
                FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=DIM),
                FieldSchema(name="chunk_role", dtype=DataType.VARCHAR, max_length=32),
                FieldSchema(name="field_names", dtype=DataType.VARCHAR, max_length=2048),
                FieldSchema(name="status", dtype=DataType.VARCHAR, max_length=32),
                FieldSchema(name="update_time", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
            ]
        )
        bm25_function = Function(
            name="chunk_text_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["chunk_text"],
            output_field_names=["sparse_vector"],
        )
        return CollectionSchema(
            fields=fields,
            functions=[bm25_function],
            description="科创知识块索引（原生 BM25 + dense hybrid）",
        )

    def ensure_collection(self):
        """显式保证集合存在并加载；同一连接代次复用已加载句柄。"""

        if self.alias_name:
            with self._maintenance_guard():
                return self._ensure_collection()
        return self._ensure_collection()

    def _ensure_collection(self):

        if self.alias_name:
            self._ensure_alias_locked()
        col = self._existing_collection()
        if col is not None:
            return col

        Collection, *_ = _lazy_import_pymilvus()
        physical_name = self._new_physical_collection_name()
        col = Collection(
            name=physical_name,
            schema=self._build_schema(),
            using=self.connection_alias,
        )
        col.create_index(
            field_name="dense_vector",
            index_params={
                "index_type": "IVF_FLAT",
                "metric_type": "COSINE",
                "params": {"nlist": 128},
            },
        )
        col.create_index(
            field_name="sparse_vector",
            index_params={
                "index_type": "AUTOINDEX",
                "metric_type": "BM25",
                "params": {},
            },
        )
        self._load_collection(col)
        if self.alias_name:
            *_, utility = _lazy_import_pymilvus()
            utility.create_alias(
                collection_name=physical_name,
                alias=self.alias_name,
                using=self.connection_alias,
            )
            self._physical_collection_name = physical_name
            col = Collection(name=self.alias_name, using=self.connection_alias)
            self._assert_source_fields(col)
            self._load_collection(col)
        else:
            self._physical_collection_name = physical_name
        self._collection = col
        logger.info("Milvus Collection '%s' 创建完成", self.collection_name)
        return col

    def _resolve_collection_name(self, *, collections: list[str] | None = None) -> str | None:
        """Return the physical target of this logical collection or alias."""
        self._ensure_connection()
        *_, connections, utility = _lazy_import_pymilvus()
        alias_name = getattr(self, "alias_name", None)
        if not alias_name:
            exists = (
                self.collection_name in collections
                if collections is not None
                else utility.has_collection(self.collection_name, using=self.connection_alias)
            )
            return self.collection_name if exists else None
        if collections is not None and alias_name in collections:
            raise RuntimeError(
                f"Milvus 别名 {self.alias_name} 与物理 Collection 同名，拒绝自动迁移"
            )
        if not utility.has_collection(alias_name, using=self.connection_alias):
            return None
        context = connections._generate_call_context(self.connection_alias)
        alias_info = connections._fetch_handler(self.connection_alias).describe_alias(
            alias_name,
            context=context,
        )
        target = alias_info.get("collection_name")
        if target:
            self._physical_collection_name = target
        return target

    def ensure_alias(self) -> str | None:
        """Attach a stable alias to an existing legacy collection without changing its data."""
        if not self.alias_name:
            return self.collection_name
        with self._maintenance_guard():
            return self._ensure_alias_locked()

    def _ensure_alias_locked(self) -> str | None:
        if not self.alias_name:
            return self.collection_name
        if self.alias_name == self.legacy_collection_name:
            raise RuntimeError("Milvus 稳定别名必须与旧物理 Collection 名不同")
        self._ensure_connection()
        *_, utility = _lazy_import_pymilvus()
        collections = utility.list_collections(using=self.connection_alias)
        target = self._resolve_collection_name(collections=collections)
        if target:
            return target
        if self.legacy_collection_name not in collections:
            return None
        utility.create_alias(
            collection_name=self.legacy_collection_name,
            alias=self.alias_name,
            using=self.connection_alias,
        )
        self._physical_collection_name = self.legacy_collection_name
        self._collection = None
        self._collection_loaded = False
        return self.legacy_collection_name

    def _new_physical_collection_name(self) -> str:
        if not self.alias_name:
            return self.collection_name
        return f"{self.legacy_collection_name}__g{uuid.uuid4().hex[:12]}"

    def new_shadow_generation(self, collection_name: str | None = None) -> MilvusService:
        """Create an isolated physical target using this service's Milvus connection."""
        if not self.alias_name:
            raise RuntimeError("影子恢复必须从稳定 Alias 服务创建")
        expected_prefix = f"{self.legacy_collection_name}__recovery_"
        if collection_name is not None and not collection_name.startswith(expected_prefix):
            raise ValueError("影子 Collection 名称不属于该知识库的恢复 generation")
        return MilvusService(
            collection_name=collection_name or f"{expected_prefix}{uuid.uuid4().hex[:12]}",
            connection_manager=self.connection_manager,
        )

    def activate_shadow_generation(
        self,
        shadow: MilvusService,
        *,
        backup_collection_name: str,
        displaced_collection_name: str,
    ) -> dict[str, str]:
        """Promote a validated shadow to the canonical name, retaining old collections."""
        if not self.alias_name:
            raise RuntimeError("只有稳定 Alias 服务可以切换影子 Collection")
        if shadow.alias_name or shadow.connection_manager is not self.connection_manager:
            raise ValueError("影子 Collection 必须是同一 Milvus 连接上的独立物理集合")
        if shadow._resolve_collection_name() != shadow.collection_name:
            raise RuntimeError(f"影子 Collection {shadow.collection_name} 不存在")
        previous = self._resolve_collection_name()
        if previous is None:
            raise RuntimeError(f"活动 Milvus Alias {self.alias_name} 不存在")
        canonical = self.legacy_collection_name
        if (
            backup_collection_name == canonical
            or backup_collection_name == shadow.collection_name
            or displaced_collection_name
            in {canonical, backup_collection_name, shadow.collection_name}
            or not backup_collection_name.startswith(f"{canonical}__backup_")
            or not displaced_collection_name.startswith(f"{canonical}__backup_stale_")
        ):
            raise ValueError("备份 Collection 名称无效")
        self._ensure_connection()
        *_, utility = _lazy_import_pymilvus()
        if utility.has_collection(backup_collection_name, using=self.connection_alias):
            raise RuntimeError(f"旧 Collection 备份目标已存在：{backup_collection_name}")
        canonical_exists = utility.has_collection(canonical, using=self.connection_alias)
        if canonical_exists and canonical not in {previous, shadow.collection_name}:
            if utility.has_collection(displaced_collection_name, using=self.connection_alias):
                raise RuntimeError(f"原名冲突库备份目标已存在：{displaced_collection_name}")
            utility.rename_collection(
                old_collection_name=canonical,
                new_collection_name=displaced_collection_name,
                using=self.connection_alias,
            )
        if previous != backup_collection_name:
            utility.rename_collection(
                old_collection_name=previous,
                new_collection_name=backup_collection_name,
                using=self.connection_alias,
            )
        utility.rename_collection(
            old_collection_name=shadow.collection_name,
            new_collection_name=canonical,
            using=self.connection_alias,
        )
        utility.alter_alias(
            collection_name=canonical,
            alias=self.alias_name,
            using=self.connection_alias,
        )
        active = self.refresh_alias_target()
        if active != canonical:
            raise RuntimeError(f"Milvus Alias 切换后目标异常：预期 {canonical}，实际 {active}")
        return {"previous": backup_collection_name, "active": active}

    def refresh_alias_target(self) -> str:
        """Discard a cached SDK handle after the stable alias has been switched."""
        target = self._resolve_collection_name()
        if target is None:
            raise RuntimeError(f"Milvus 别名 {self.alias_name} 不存在")
        self._collection = None
        self._collection_loaded = False
        self._physical_collection_name = target
        return target

    def has_physical_collection(self, collection_name: str) -> bool:
        self._ensure_connection()
        *_, utility = _lazy_import_pymilvus()
        return bool(utility.has_collection(collection_name, using=self.connection_alias))

    def rebuild_collection(self) -> None:
        """释放、删除并按当前契约重建 Collection；调用方随后负责全量回灌。"""

        if self.alias_name:
            raise RuntimeError("公开 Collection 禁止直接删除重建，请使用影子 Collection 恢复")

        def rebuild() -> None:
            *_, utility = _lazy_import_pymilvus()
            self._ensure_connection()
            if utility.has_collection(self.collection_name, using=self.connection_alias):
                try:
                    utility.release_collection(self.collection_name, using=self.connection_alias)
                except Exception as exc:  # noqa: BLE001 - 未加载时释放可能返回错误
                    logger.debug("释放 Collection '%s' 时忽略异常: %s", self.collection_name, exc)
                utility.drop_collection(self.collection_name, using=self.connection_alias)
            self._collection = None
            self._collection_loaded = False
            self.ensure_collection()

        self._with_collection_recovery_retry(rebuild)

    def _assert_source_fields(self, collection: Any) -> None:
        required = {
            "id",
            "document_id",
            "mysql_id",
            "source_id",
            "chunk_role",
            "chunk_index",
            "source_locator",
            "chunk_text",
            "dense_vector",
            "sparse_vector",
            "field_names",
            "status",
            "update_time",
        }
        if getattr(self, "requires_user_id", False):
            required.add("user_id")
        fields_by_name = {field.name: field for field in collection.schema.fields}
        fields = set(fields_by_name)
        missing = required - fields
        if missing:
            raise RuntimeError(
                f"Milvus Collection {self.collection_name} schema 不符合当前 MVP "
                f"{', '.join(sorted(missing))}；当前 MVP 需要清空并重建该 Collection 后再索引。"
            )
        mismatches: list[str] = []
        for name, expected_max in _VARCHAR_LIMITS.items():
            field = fields_by_name.get(name)
            if field is None:
                continue
            actual_max = getattr(field, "max_length", None)
            if actual_max is None:
                actual_max = (getattr(field, "params", {}) or {}).get("max_length")
            if actual_max is not None and int(actual_max) < expected_max:
                mismatches.append(f"{name}.max_length={actual_max}<{expected_max}")
        dense_field = fields_by_name.get("dense_vector")
        dense_dim = getattr(dense_field, "dim", None) if dense_field else None
        if dense_dim is None and dense_field is not None:
            dense_dim = (getattr(dense_field, "params", {}) or {}).get("dim")
        if dense_dim is not None and int(dense_dim) != DIM:
            mismatches.append(f"dense_vector.dim={dense_dim}!={DIM}")
        functions = getattr(collection.schema, "functions", None)
        if functions is not None:
            function_names = {str(getattr(function, "name", "")) for function in functions}
            if "chunk_text_bm25" not in function_names:
                mismatches.append("missing BM25 function chunk_text_bm25")
        if mismatches:
            raise RuntimeError(
                f"Milvus Collection {self.collection_name} schema 不兼容："
                f"{'; '.join(mismatches)}；请清空并重建该 Collection。"
            )

    @property
    def collection(self):
        self._ensure_connection()
        if self._collection is None:
            return self.ensure_collection()
        self._load_collection(self._collection)
        return self._collection

    def _load_collection(self, collection: Any) -> None:
        """仅在当前代次尚未确认加载时执行有限等待，失败不标记为已加载。"""

        if not self._collection_loaded:
            try:
                self._with_collection_recovery_retry(
                    lambda: collection.load(timeout=MILVUS_LOAD_TIMEOUT_SECONDS)
                )
            except Exception as exc:
                if self._collection is collection:
                    self._collection = None
                self._collection_loaded = False
                traceparent = get_trace_context().traceparent
                recovery_logger.exception(
                    "Milvus Collection 首次加载失败",
                    extra={
                        "structured": {
                            "event": "milvus_collection_load_failed",
                            "traceparent": traceparent,
                            "collection": self.collection_name,
                            "database": self.database,
                            "load_state": f"{type(exc).__name__}: {exc}",
                            "error": str(exc),
                        }
                    },
                )
                diagnostics = self._collection_load_diagnostics(collection)
                log_event(
                    recovery_logger,
                    "milvus_collection_load_diagnostics",
                    logging.ERROR,
                    traceparent=traceparent,
                    collection=self.collection_name,
                    **diagnostics,
                )
                self._start_collection_load_recovery(traceparent=traceparent)
                raise
            self._collection_loaded = True

    # ---- 批量写入 ----

    def _chunk_to_row(self, chunk: dict[str, Any]) -> dict[str, Any]:
        if getattr(self, "requires_user_id", False) and not str(chunk.get("user_id") or "").strip():
            raise ValueError("私有知识块缺少 user_id")
        row = {
            "id": str(chunk.get("id") or ""),
            "document_id": str(chunk.get("document_id") or ""),
            "mysql_id": str(chunk.get("mysql_id") or ""),
            "source_id": str(chunk.get("source_id") or ""),
            "chunk_index": chunk.get("chunk_index"),
            "source_locator": str(chunk.get("source_locator") or ""),
            "chunk_text": str(chunk.get("chunk_text") or ""),
            "dense_vector": chunk.get("dense_vector"),
            "chunk_role": str(chunk.get("chunk_role") or "admin_doc"),
            "field_names": _truncate_utf8(
                "|".join(str(item) for item in (chunk.get("field_names") or [])),
                _VARCHAR_LIMITS["field_names"],
            ),
            "status": str(chunk.get("status") or "active"),
            "update_time": str(chunk.get("update_time") or ""),
        }
        if getattr(self, "requires_user_id", False):
            row["user_id"] = str(chunk.get("user_id") or "")
        self._validate_row(row)
        return row

    @staticmethod
    def _estimate_row_bytes(row: dict[str, Any]) -> int:
        string_bytes = sum(_utf8_size(row.get(name)) for name in _VARCHAR_LIMITS if name in row)
        vector_bytes = len(row.get("dense_vector") or []) * 4
        return string_bytes + vector_bytes + 512

    @_fenced_write
    def _upsert_rows(self, rows: list[dict[str, Any]], *, flush: bool) -> None:
        self._write_rows(rows, flush=flush, operation="upsert")

    @_fenced_write
    def _insert_rows(self, rows: list[dict[str, Any]], *, flush: bool) -> None:
        self._write_rows(rows, flush=flush, operation="insert")

    def _write_rows(
        self, rows: list[dict[str, Any]], *, flush: bool, operation: str
    ) -> None:
        if not rows:
            return
        for row in rows:
            self._validate_row(row)
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_bytes = 0
        for row in rows:
            row_bytes = self._estimate_row_bytes(row)
            if row_bytes > MILVUS_SAFE_UPSERT_BYTES:
                raise ValueError(
                    f"Milvus 单行写入估算超过安全批次：collection={getattr(self, 'collection_name', 'unknown')} "
                    f"id={row.get('id')} bytes={row_bytes}"
                )
            if current and current_bytes + row_bytes > MILVUS_SAFE_UPSERT_BYTES:
                batches.append(current)
                current, current_bytes = [], 0
            current.append(row)
            current_bytes += row_bytes
        if current:
            batches.append(current)

        for batch_index, batch in enumerate(batches, start=1):
            batch_bytes = sum(self._estimate_row_bytes(row) for row in batch)
            is_mysql_sync_batch = operation == "upsert" and all(
                row.get("chunk_role") == "mysql" for row in batch
            )

            def write_batch(batch=batch) -> None:
                getattr(self.collection, operation)(batch)

            try:
                if is_mysql_sync_batch:
                    with mysql_sync_write(self.priority_key):
                        self._with_collection_recovery_retry(write_batch)
                else:
                    self._with_collection_recovery_retry(write_batch)
            except Exception:
                logger.exception(
                    "Milvus 批量写入失败",
                    extra={
                        "structured": {
                            "event": f"milvus_{operation}_failed",
                            "collection": getattr(self, "collection_name", "unknown"),
                            "batch_index": batch_index,
                            "batch_count": len(batches),
                            "row_count": len(batch),
                            "estimated_bytes": batch_bytes,
                        }
                    },
                )
                raise
            logger.debug(
                "Milvus 批量写入完成 collection=%s batch=%d/%d rows=%d estimated_bytes=%d",
                getattr(self, "collection_name", "unknown"),
                batch_index,
                len(batches),
                len(batch),
                batch_bytes,
            )
        if flush:
            self._with_collection_recovery_retry(self.collection.flush)

    def _query_batches(
        self,
        collection: Any,
        expr: str,
        output_fields: list[str],
    ):
        iterator_factory = getattr(collection, "query_iterator", None)
        if callable(iterator_factory):
            iterator = iterator_factory(
                expr=expr,
                output_fields=output_fields,
                batch_size=MILVUS_QUERY_BATCH_SIZE,
            )
            try:
                while True:
                    rows = iterator.next() or []
                    if not rows:
                        break
                    yield rows
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            return
        rows = (
            collection.query(
                expr=expr,
                output_fields=output_fields,
                limit=MILVUS_MAX_QUERY_LIMIT,
            )
            or []
        )
        yield rows

    def _query_ids(self, collection: Any, expr: str) -> list[str]:
        ids: list[str] = []
        for rows in self._query_batches(collection, expr, ["id"]):
            ids.extend(str(row.get("id") or "") for row in rows if row.get("id"))
        return ids

    def source_ids_by_mysql_id(self, mysql_id: str, chunk_role: str) -> list[str]:
        """Read association keys already indexed for one draft role."""
        value = str(mysql_id or "").strip()
        role = str(chunk_role or "").strip()
        if not value or not role:
            raise ValueError("查询草稿 source_id 必须提供 mysql_id 和 chunk_role")
        collection = self._existing_collection()
        if collection is None:
            return []
        expr = (
            f'mysql_id == "{_escape_expr_value(value)}" '
            f'&& chunk_role == "{_escape_expr_value(role)}"'
        )
        source_ids = {
            str(row.get("source_id") or "").strip()
            for rows in self._query_batches(collection, expr, ["source_id"])
            for row in rows
            if str(row.get("source_id") or "").strip()
        }
        return sorted(source_ids)

    def source_ids_by_mysql_ids(self, mysql_ids: list[str]) -> dict[str, list[str]]:
        values = sorted({str(value).strip() for value in mysql_ids if str(value).strip()})
        if not values:
            return {}
        collection = self._existing_collection()
        if collection is None:
            return {}
        literals = ", ".join(f'"{_escape_expr_value(value)}"' for value in values)
        expression = (
            'chunk_role in ["agent_draft", "admin_draft", "mysql"] '
            f'&& mysql_id in [{literals}] && status == "active"'
        )
        drafts: dict[str, set[str]] = {}
        mysql: dict[str, set[str]] = {}
        for rows in self._query_batches(collection, expression, ["mysql_id", "source_id", "chunk_role"]):
            for row in rows:
                mysql_id = str(row.get("mysql_id") or "").strip()
                source_id = str(row.get("source_id") or "").strip()
                if mysql_id in values and source_id:
                    grouped = drafts if row.get("chunk_role") in {"agent_draft", "admin_draft"} else mysql
                    grouped.setdefault(mysql_id, set()).add(source_id)
        # 草稿是业务关联来源；旧 mysql 块可能仍保留同步前的来源。
        return {key: sorted(drafts.get(key) or mysql[key]) for key in drafts.keys() | mysql.keys()}

    def published_draft_mysql_ids(self, mysql_ids: list[str]) -> set[str]:
        values = sorted({str(value).strip() for value in mysql_ids if str(value).strip()})
        if not values:
            return set()
        collection = self._existing_collection()
        if collection is None:
            return set()
        literals = ", ".join(f'"{_escape_expr_value(value)}"' for value in values)
        expression = (
            'chunk_role in ["agent_draft", "admin_draft"] '
            f'&& mysql_id in [{literals}] && status == "active"'
        )
        return {
            str(row.get("mysql_id") or "").strip()
            for rows in self._query_batches(collection, expression, ["mysql_id"])
            for row in rows
            if str(row.get("mysql_id") or "").strip()
        }

    @_fenced_write
    def delete_by_ids(self, ids: set[str] | list[str]) -> None:
        collection = self._existing_collection()
        if collection is None:
            return
        values = [str(value) for value in ids if str(value).strip()]
        if not values:
            return
        for start in range(0, len(values), 500):
            batch = values[start : start + 500]
            literals = ", ".join(f'"{_escape_expr_value(value)}"' for value in batch)
            self._with_collection_recovery_retry(
                lambda literals=literals: collection.delete(f"id in [{literals}]")
            )
        self._with_collection_recovery_retry(collection.flush)

    def _validate_row(self, row: dict[str, Any]) -> None:
        for field_name, max_bytes in _VARCHAR_LIMITS.items():
            if field_name not in row:
                continue
            value = row.get(field_name)
            if _utf8_size(value) > max_bytes:
                raise ValueError(
                    f"Milvus 字段过长：collection={getattr(self, 'collection_name', 'unknown')} field={field_name} "
                    f"bytes={_utf8_size(value)} max_bytes={max_bytes} id={row.get('id')}"
                )
        self._validate_dense_vector(row.get("dense_vector"))

    @_fenced_write
    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> int:
        if not chunks:
            return 0
        rows = [self._chunk_to_row(chunk) for chunk in chunks]
        defer_flush = all((chunk.get("chunk_role") or "") == "mysql" for chunk in chunks)
        self._upsert_rows(rows, flush=not defer_flush)
        logger.info(
            "Milvus 写入 %d chunks%s",
            len(chunks),
            "（等待同步结束 flush）" if defer_flush else "",
            extra={
                "structured": {
                    "event": "milvus_upsert_finished",
                    "collection": getattr(self, "collection_name", "unknown"),
                    "row_count": len(chunks),
                    "defer_flush": defer_flush,
                }
            },
        )
        return len(chunks)

    @_fenced_write
    def insert_chunks(self, chunks: list[dict[str, Any]], *, flush: bool = False) -> int:
        """Insert unseen keys, accepting identical source rows and rejecting conflicts.

        Return accepted input rows so the caller retains all expected primary keys.
        """
        if not chunks:
            return 0
        rows = [self._chunk_to_row(chunk) for chunk in chunks]
        # 时间戳和 embedding 是派生值；重复来源以全部持久化业务字段比较。
        content_fields = [key for key in rows[0] if key not in {"update_time", "dense_vector"}]

        def same_content(left, right):
            return all(left.get(key) == right.get(key) for key in content_fields)

        unique_rows: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for row in rows:
            key = str(row["id"])
            if key in unique_rows:
                if not same_content(unique_rows[key], row):
                    raise ValueError(f"影子恢复主键内容冲突：{key}")
                duplicates += 1
            else:
                unique_rows[key] = row
        collection = self._existing_collection()
        if collection is None:
            raise RuntimeError(f"Milvus Collection {self.collection_name} 不存在，无法插入影子数据")
        existing: set[str] = set()
        ids = list(unique_rows)
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            literals = ", ".join(f'"{_escape_expr_value(value)}"' for value in batch)
            stored_rows = self._with_collection_recovery_retry(
                lambda literals=literals: collection.query(
                    expr=f"id in [{literals}]",
                    output_fields=content_fields,
                    consistency_level="Strong",
                )
            )
            for stored in stored_rows:
                key = str(stored["id"])
                if not same_content(unique_rows[key], stored):
                    raise ValueError(f"影子恢复主键内容冲突：{key}")
                existing.add(key)
        duplicates += sum(1 for key in existing if key in unique_rows)
        insert_rows = [row for key, row in unique_rows.items() if key not in existing]
        if duplicates:
            logger.warning(
                "Milvus 影子恢复检测到重复主键并跳过",
                extra={
                    "structured": {
                        "event": "milvus_recovery_duplicate_primary_keys",
                        "collection": self.collection_name,
                        "duplicate_count": duplicates,
                    }
                },
            )
        self._insert_rows(insert_rows, flush=flush)
        return len(rows)

    @_fenced_write
    def replace_chunks(
        self,
        chunks: list[dict[str, Any]],
        *,
        delete_expr: str,
        user_id: str | None = None,
    ) -> int:
        """先写入新块，再清理旧块，避免旧索引先删导致发布后完全不可检索。"""

        collection = self._existing_collection()
        if collection is None:
            return self.upsert_chunks(chunks)
        scoped_expr = delete_expr
        if getattr(self, "requires_user_id", False):
            if not user_id:
                raise ValueError("私有知识必须提供 user_id")
            if " || " in scoped_expr:
                scoped_expr = f"({scoped_expr})"
            scoped_expr += f' && user_id == "{_escape_expr_value(user_id)}"'
        old_ids = set(self._query_ids(collection, scoped_expr))
        new_ids = {str(chunk.get("id") or "") for chunk in chunks}
        count = self.upsert_chunks(chunks) if chunks else 0
        stale_ids = old_ids - new_ids
        if stale_ids:
            self.delete_by_ids(stale_ids)
        return count

    @_fenced_write
    def flush(self) -> None:
        """提交延迟 flush 的批量写入。"""
        self._with_collection_recovery_retry(lambda: self.collection.flush())

    def count_entities(self) -> int:
        collection = self._existing_collection()
        if collection is None:
            raise RuntimeError(f"Milvus Collection {self.collection_name} 不存在")
        return int(collection.num_entities)

    def existing_ids(self, record_ids: set[str]) -> set[str]:
        collection = self._existing_collection()
        if collection is None:
            raise RuntimeError(f"Milvus Collection {self.collection_name} 不存在")
        found: set[str] = set()
        values = sorted(record_ids)
        for start in range(0, len(values), 500):
            batch = values[start : start + 500]
            literals = ", ".join(f'"{_escape_expr_value(value)}"' for value in batch)
            found.update(self._query_ids(collection, f"id in [{literals}]"))
        return found

    def flush_for_mysql_sync(self) -> None:
        """以低优先级提交 MySQL 同步写入，避免同步收尾阻塞在线检索。"""
        with mysql_sync_write(self.priority_key):
            self.flush()

    @_fenced_write
    def delete_by_document_id(self, document_id: str, user_id: str | None = None) -> None:
        collection = self._existing_collection()
        if collection is None:
            return
        expr = f'document_id == "{_escape_expr_value(document_id)}"'
        if getattr(self, "requires_user_id", False):
            if not user_id:
                raise ValueError("删除私有知识必须提供 user_id")
            expr += f' && user_id == "{_escape_expr_value(user_id)}"'
        self._with_collection_recovery_retry(lambda: (collection.delete(expr), collection.flush()))

    @staticmethod
    def _current_version_document_id(version_id: str, active_version_id: str | None) -> str:
        # 当前 schema 只有文档索引；调用边界必须先从生命周期核实活动版本。
        document_id, separator, number = version_id.rpartition(":v")
        if (not active_version_id or version_id != active_version_id or not document_id
                or not separator or not number.isdigit() or int(number) < 1):
            raise ValueError("只有已核实的活动版本才能操作当前文档向量")
        return document_id

    def delete_by_version_id(
        self, version_id: str, user_id: str | None = None, *, active_version_id: str | None = None
    ) -> None:
        document_id = self._current_version_document_id(version_id, active_version_id)
        self.delete_by_document_id(document_id, user_id)

    @_fenced_write
    def delete_by_mysql_id(self, mysql_id: str, user_id: str | None = None) -> None:
        """只删除一个业务记录的向量，避免同文档其他草稿被连带删除。"""
        collection = self._existing_collection()
        if collection is None:
            return
        expr = f'mysql_id == "{_escape_expr_value(mysql_id)}"'
        if getattr(self, "requires_user_id", False):
            if not user_id:
                raise ValueError("删除私有知识必须提供 user_id")
            expr += f' && user_id == "{_escape_expr_value(user_id)}"'
        self._with_collection_recovery_retry(lambda: (collection.delete(expr), collection.flush()))

    @_fenced_write
    def delete_by_chunk_role(self, chunk_role: str, user_id: str | None = None) -> None:
        """按块角色清理整类同步向量，例如 MySQL 全量重同步。"""
        collection = self._existing_collection()
        if collection is None:
            return
        expr = f'chunk_role == "{_escape_expr_value(chunk_role)}"'
        if getattr(self, "requires_user_id", False):
            if not user_id:
                raise ValueError("删除私有知识必须提供 user_id")
            expr += f' && user_id == "{_escape_expr_value(user_id)}"'
        self._with_collection_recovery_retry(lambda: (collection.delete(expr), collection.flush()))

    @_fenced_write
    def delete_by_source_id(self, source_id: str, user_id: str | None = None) -> None:
        """删除一个草稿及其关联原文的全部向量。"""
        collection = self._existing_collection()
        if collection is None:
            return
        expr = f'source_id == "{_escape_expr_value(source_id)}"'
        if getattr(self, "requires_user_id", False):
            if not user_id:
                raise ValueError("删除私有知识必须提供 user_id")
            expr += f' && user_id == "{_escape_expr_value(user_id)}"'
        self._with_collection_recovery_retry(lambda: (collection.delete(expr), collection.flush()))

    def count_by_document_id(self, document_id: str) -> int:
        """统计某个同步来源在 Collection 中的向量块数量。"""
        collection = self._existing_collection()
        if collection is None:
            return 0
        expr = f'document_id == "{_escape_expr_value(document_id)}"'
        return self._with_collection_recovery_retry(
            lambda: sum(len(rows) for rows in self._query_batches(collection, expr, ["id"])),
        )

    def count_by_mysql_records(self, document_id: str) -> int:
        """按 mysql_id 去重统计同步资料数，而不是统计分块后的向量数。"""
        collection = self._existing_collection()
        if collection is None:
            return 0
        expr = f'document_id == "{_escape_expr_value(document_id)}"'

        def read_record_ids() -> set[str]:
            record_ids: set[str] = set()
            iterator_factory = getattr(collection, "query_iterator", None)
            if iterator_factory:
                iterator = iterator_factory(
                    expr=expr, output_fields=["mysql_id"], batch_size=MILVUS_QUERY_BATCH_SIZE
                )
                try:
                    while True:
                        rows = iterator.next() or []
                        if not rows:
                            break
                        record_ids.update(str(row.get("mysql_id") or "") for row in rows)
                finally:
                    close = getattr(iterator, "close", None)
                    if close:
                        close()
            else:
                rows = collection.query(
                    expr=expr, output_fields=["mysql_id"], limit=MILVUS_MAX_QUERY_LIMIT
                )
                record_ids.update(str(row.get("mysql_id") or "") for row in (rows or []))
            return record_ids

        record_ids = self._with_collection_recovery_retry(read_record_ids)
        return len({record_id for record_id in record_ids if record_id})

    def count_by_logical_records(self) -> int:
        """按已写入的主记录去重，附件解析分块不单独计数。"""
        collection = self._existing_collection()
        if collection is None:
            return 0

        def read_record_ids() -> set[str]:
            record_ids: set[str] = set()
            iterator_factory = getattr(collection, "query_iterator", None)
            if iterator_factory:
                iterator = iterator_factory(
                    expr="",
                    output_fields=["mysql_id", "chunk_role", "status"],
                    batch_size=MILVUS_QUERY_BATCH_SIZE,
                )
                try:
                    while True:
                        rows = iterator.next() or []
                        if not rows:
                            break
                        record_ids.update(
                            str(row.get("mysql_id") or "")
                            for row in rows
                            if row.get("status") == "active" and row.get("mysql_id")
                        )
                finally:
                    close = getattr(iterator, "close", None)
                    if close:
                        close()
            else:
                rows = collection.query(
                    expr="",
                    output_fields=["mysql_id", "chunk_role", "status"],
                    limit=MILVUS_MAX_QUERY_LIMIT,
                )
                record_ids.update(
                    str(row.get("mysql_id") or "")
                    for row in (rows or [])
                    if row.get("status") == "active" and row.get("mysql_id")
                )
            return record_ids

        record_ids = self._with_collection_recovery_retry(read_record_ids)
        return len({record_id for record_id in record_ids if record_id})

    def set_publication_status_by_version_id(
        self, version_id: str, publication_status: str, user_id: str | None = None,
        *, active_version_id: str | None = None,
    ) -> int:
        """更新版本可检索状态，保留 Milvus 向量供后续重新上架。"""

        document_id = self._current_version_document_id(version_id, active_version_id)
        return self.set_publication_status_by_document_id(document_id, publication_status, user_id)

    def set_publication_status_by_document_id(
        self,
        document_id: str,
        publication_status: str,
        user_id: str | None = None,
        chunk_role: str | None = None,
    ) -> int:
        """更新文档当前索引的可检索状态。"""

        expr = f'document_id == "{_escape_expr_value(document_id)}"'
        if chunk_role:
            expr += f' && chunk_role == "{_escape_expr_value(chunk_role)}"'
        return self._set_publication_status(expr, publication_status, user_id)

    def set_publication_status_by_mysql_id(
        self, mysql_id: str, publication_status: str, user_id: str | None = None
    ) -> int:
        """只更新一个业务草稿的向量，不影响同一原文关联的其他草稿。"""

        return self._set_publication_status(
            f'mysql_id == "{_escape_expr_value(mysql_id)}"', publication_status, user_id
        )

    @_fenced_write
    def _set_publication_status(
        self, expr: str, publication_status: str, user_id: str | None = None
    ) -> int:

        publication_status = {
            "PUBLISHED": "active",
            "UNPUBLISHED": "withdrawn",
        }.get(publication_status, publication_status)
        if publication_status not in {"active", "withdrawn", "deleted"}:
            raise ValueError("status 只能是 active、withdrawn 或 deleted")
        collection = self._existing_collection()
        if collection is None:
            return 0
        output_fields = [
            "id",
            "document_id",
            "mysql_id",
            "source_id",
            "chunk_role",
            "chunk_index",
            # sparse_vector 是 BM25 函数输出字段，Milvus 不允许 query 返回它；
            # dense_vector 保留用于状态 upsert，sparse_vector 会由函数重新生成。
            "source_locator",
            "chunk_text",
            "dense_vector",
            "field_names",
            "status",
            "update_time",
        ]
        if getattr(self, "requires_user_id", False):
            if not user_id:
                raise ValueError("私有知识必须提供 user_id")
            expr += f' && user_id == "{_escape_expr_value(user_id)}"'
            output_fields.insert(1, "user_id")

        def update_rows() -> int:
            updated = 0
            for rows in self._query_batches(collection, expr, output_fields):
                for row in rows:
                    row["status"] = publication_status
                self._upsert_rows(rows, flush=False)
                updated += len(rows)
            if updated:
                self._with_collection_recovery_retry(collection.flush)
            return updated

        return self._with_collection_recovery_retry(update_rows)

    def _existing_collection(self, *, load: bool = True):
        """仅获取既有集合，避免删除操作意外创建空集合。"""
        self._ensure_connection()
        if self._collection is not None:
            if load:
                self._load_collection(self._collection)
            return self._collection
        Collection, *_, utility = _lazy_import_pymilvus()
        if self._resolve_collection_name() is None:
            return None
        collection = Collection(self.collection_name, using=self.connection_alias)
        self._assert_source_fields(collection)
        if load:
            self._load_collection(collection)
        self._collection = collection
        return collection

    # ---- 搜索 ----

    def search_dense(
        self,
        query_vector: list[float],
        top_k: int = 20,
        expr: str | None = None,
    ) -> list[RetrievalHit]:
        """ANN 密向量搜索，返回 RetrievalHit 列表。"""
        top_k = _validate_top_k(top_k)
        self._validate_dense_vector(query_vector)
        params = {"metric_type": "COSINE", "params": {"nprobe": 10}}
        expr = _published_expr(expr)
        with milvus_search(self.priority_key):
            res = self._with_collection_recovery_retry(
                lambda: self.collection.search(
                    data=[query_vector],
                    anns_field="dense_vector",
                    param=params,
                    limit=top_k,
                    expr=expr,
                    output_fields=self._output_fields(),
                )
            )
        return build_retrieval_hits(res, source="dense")

    def search_hybrid(
        self,
        query_text: str,
        query_vector: list[float],
        top_k: int = 20,
        expr: str | None = None,
    ) -> list[RetrievalHit]:
        """Milvus 原生 BM25 + dense hybrid search，并由 RRFRanker 融合。"""
        top_k = _validate_top_k(top_k)
        self._validate_dense_vector(query_vector)
        (
            _,
            _,
            _,
            _,
            _,
            _,
            AnnSearchRequest,
            RRFRanker,
            *_,
        ) = _lazy_import_pymilvus()
        expr = _published_expr(expr)
        output_fields = self._output_fields()
        candidate_limit = min(max(top_k * 3, top_k), MILVUS_MAX_QUERY_LIMIT)
        dense_request = AnnSearchRequest(
            data=[query_vector],
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            limit=candidate_limit,
            expr=expr,
        )
        bm25_request = AnnSearchRequest(
            data=[query_text],
            anns_field="sparse_vector",
            param={"metric_type": "BM25", "params": {}},
            limit=candidate_limit,
            expr=expr,
        )
        with milvus_search(self.priority_key):
            res = self._with_collection_recovery_retry(
                lambda: self.collection.hybrid_search(
                    reqs=[dense_request, bm25_request],
                    rerank=RRFRanker(k=60),
                    limit=top_k,
                    output_fields=output_fields,
                )
            )
        return build_retrieval_hits(res, source="hybrid")

    def search_text_prefix(
        self,
        query_text: str,
        top_k: int = 20,
        expr: str | None = None,
    ) -> list[RetrievalHit]:
        """按长文本前缀兜底，避免已复制的 chunk 因混合排序完全漏召回。"""
        top_k = _validate_top_k(top_k)
        prefix = str(query_text or "").strip()[:200]
        if len(prefix) < 20:
            return []
        # 复杂问答查询（尤其是草稿 JSON）不能作为 Milvus LIKE 字面量。
        # LIKE 只保留给无引号、无转义字符的短文本兜底，正常查询走向量/BM25。
        if any(char in prefix for char in ("\\", '"', "'", "\r", "\n")):
            return []
        if getattr(self, "requires_user_id", False):
            raise ValueError("私有知识必须通过检索表达式提供 user_id")
        safe_prefix = _escape_expr_value(prefix)
        filters = [f'chunk_text like "{safe_prefix}%"']
        if expr:
            filters.insert(0, f"({expr})")
        with milvus_search(self.priority_key):
            collection = self._existing_collection()
            if collection is None:
                return []
            rows = self._with_collection_recovery_retry(
                lambda: collection.query(
                    expr=_published_expr(" && ".join(filters)),
                    output_fields=self._output_fields(),
                    limit=top_k,
                )
            )
        return [
            build_retrieval_hits(
                [[type("Hit", (), {"fields": row, "id": row.get("id"), "distance": 1.0})()]],
                "text_prefix",
            )[0]
            for row in rows or []
            if row.get("id")
        ]

    @staticmethod
    def _validate_dense_vectors(chunks: list[dict[str, Any]]) -> None:
        for chunk in chunks:
            MilvusService._validate_dense_vector(chunk["dense_vector"])

    @staticmethod
    def _validate_dense_vector(vector: list[float] | None) -> None:
        if vector is None or len(vector) != DIM:
            actual = 0 if vector is None else len(vector)
            raise ValueError(f"dense vector 维度错误：期望 {DIM}，实际 {actual}")
        if any(not math.isfinite(float(value)) for value in vector):
            raise ValueError("dense vector 包含 NaN 或 Infinity")

    def _output_fields(self) -> list[str]:
        fields = [
            "id",
            "document_id",
            "mysql_id",
            "source_id",
            "chunk_role",
            "chunk_index",
            "source_locator",
            "chunk_text",
            "field_names",
            "status",
            "update_time",
        ]
        if getattr(self, "requires_user_id", False):
            fields.insert(1, "user_id")
        return fields

    def query_document_chunks(
        self,
        document_id: str | None = None,
        mysql_id: str | None = None,
        source_id: str | None = None,
        chunk_role: str | None = None,
        expr: str | None = None,
        user_id: str | None = None,
    ) -> list[RetrievalHit]:
        """按业务标识回取已发布块，用于命中后的草稿/原文上下文补全。"""

        collection = self._existing_collection()
        if collection is None:
            return []
        if not document_id and not mysql_id and not source_id:
            return []
        filters = []
        if mysql_id:
            filters.append(f'mysql_id == "{_escape_expr_value(mysql_id)}"')
        elif document_id:
            filters.append(f'document_id == "{_escape_expr_value(document_id)}"')
        if source_id:
            filters.append(f'source_id == "{_escape_expr_value(source_id)}"')
        if chunk_role:
            filters.append(f'chunk_role == "{_escape_expr_value(chunk_role)}"')
        if expr:
            filters.insert(0, f"({expr})")
        if getattr(self, "requires_user_id", False):
            if not user_id:
                raise ValueError("私有知识必须提供 user_id")
            filters.append(f'user_id == "{_escape_expr_value(user_id)}"')
        rows = self._with_collection_recovery_retry(
            lambda: [
                row
                for batch in self._query_batches(
                    collection, _published_expr(" && ".join(filters)), self._output_fields()
                )
                for row in batch
            ]
        )
        from agents.retrieval_agent.schemas import RetrievalHit

        return [
            RetrievalHit(
                record_id=str(row.get("id") or ""),
                score=0.0,
                source="document_context",
                metadata={
                    "user_id": row.get("user_id"),
                    "document_id": row.get("document_id"),
                    "mysql_id": row.get("mysql_id") or None,
                    "source_id": row.get("source_id") or None,
                    "chunk_role": row.get("chunk_role") or "admin_doc",
                    "status": row.get("status") or "active",
                    "update_time": row.get("update_time") or None,
                    "chunk_index": row.get("chunk_index"),
                    "source_locator": row.get("source_locator"),
                    "chunk_text": row.get("chunk_text", ""),
                    "field_names": str(row.get("field_names") or "").split("|")
                    if row.get("field_names")
                    else [],
                },
            )
            for row in rows or []
            if row.get("id")
        ]


def _escape_expr_value(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def escape_milvus_expr_value(value: str) -> str:
    """转义 Milvus 表达式中的字符串字面量，供索引编排层复用。"""

    return _escape_expr_value(value)


def _validate_top_k(top_k: int) -> int:
    value = int(top_k)
    if value < 1 or value > MILVUS_MAX_QUERY_LIMIT:
        raise ValueError(f"Milvus top_k 必须在 1～{MILVUS_MAX_QUERY_LIMIT} 之间，实际 {value}")
    return value


def _published_expr(expr: str | None) -> str:
    """把发布状态过滤放在 Milvus 适配层，兼容上层无文档过滤的调用。"""
    publication = 'status == "active"'
    return f"({expr}) && {publication}" if expr else publication


def build_retrieval_hits(results: list[list[Any]], source: str) -> list[RetrievalHit]:
    """适配 PyMilvus 检索命中结果。"""
    from agents.retrieval_agent.schemas import RetrievalHit

    if not results:
        return []
    hits: list[RetrievalHit] = []
    for hit in results[0]:
        fields = hit.fields or {}
        record_id = str(fields.get("id") or hit.id)
        hits.append(
            RetrievalHit(
                record_id=record_id,
                score=float(hit.distance),
                source=source,
                metadata={
                    "user_id": fields.get("user_id"),
                    "collection_name": getattr(hit, "collection_name", None),
                    "document_id": fields.get("document_id"),
                    "mysql_id": fields.get("mysql_id") or None,
                    "source_id": fields.get("source_id") or None,
                    "chunk_role": fields.get("chunk_role") or "admin_doc",
                    "status": fields.get("status") or "active",
                    "update_time": fields.get("update_time") or None,
                    "chunk_index": fields.get("chunk_index"),
                    "source_locator": fields.get("source_locator"),
                    "chunk_text": fields.get("chunk_text", ""),
                    "field_names": (
                        str(fields.get("field_names") or "").split("|")
                        if fields.get("field_names")
                        else []
                    ),
                },
            )
        )
    return hits
