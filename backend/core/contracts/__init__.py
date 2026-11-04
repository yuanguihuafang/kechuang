"""Perception、Retrieval 与平台层共享的最小数据契约。"""

from .document import DocumentBlock, ParsedDocument
from .retrieval import ChunkPreviewBlock, ChunkPreviewRequest, Evidence, IndexChunk, RelatedDocument, RelatedImage, RetrievedRecord, RetrievalResult

__all__ = [
    "DocumentBlock",
    "ChunkPreviewBlock",
    "ChunkPreviewRequest",
    "Evidence",
    "IndexChunk",
    "ParsedDocument",
    "RelatedDocument",
    "RelatedImage",
    "RetrievedRecord",
    "RetrievalResult",
]
