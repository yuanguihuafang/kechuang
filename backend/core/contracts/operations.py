"""跨存储操作的持久化状态契约。"""

from enum import StrEnum


class OperationState(StrEnum):
    REQUESTED = "REQUESTED"
    STORAGE_READY = "STORAGE_READY"
    PARSED = "PARSED"
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
