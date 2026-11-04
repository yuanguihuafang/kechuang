import asyncio
from pathlib import Path

from services.document_parsing.file_ingestion import ParsedDocument
from services.document_parsing.text_parser import HybridDocumentParser


class RecordingParser:
    def __init__(self, name, extensions):
        self.name = name
        self.supported_extensions = set(extensions)
        self.calls = []

    async def parse(self, path, **kwargs):
        self.calls.append((Path(path).suffix.lower(), kwargs))
        return ParsedDocument(
            source_name=Path(path).name,
            text=self.name,
            raw_text=self.name,
            metadata={"parser": self.name},
        )


def test_hybrid_parser_routes_docx_to_python_docx(monkeypatch, tmp_path):
    remote = RecordingParser("mineru_http", {"pdf", "docx", "pptx"})
    local = RecordingParser("mineru_local", {"pdf", "docx", "pptx"})
    visual = RecordingParser("vision", {"png", "pdf", "pptx"})
    image_mineru = RecordingParser("mineru_image", {"png"})
    monkeypatch.setattr(
        "services.document_parsing.text_parser.extract_docx_page_texts",
        lambda _path: {1: "docx text"},
    )
    parser = HybridDocumentParser(
        remote,
        ark_parser=visual,
        preferred_parser="ark",
        local_mineru_parser=local,
        image_mineru_parser=image_mineru,
    )

    result = asyncio.run(parser.parse(tmp_path / "sample.docx"))

    assert result.metadata["parser"] == "python_docx"
    assert result.text == "docx text"
    assert not remote.calls


def test_hybrid_parser_keeps_pdf_mineru_and_pptx_local(tmp_path):
    remote = RecordingParser("mineru_http", {"pdf", "pptx"})
    local = RecordingParser("mineru_local", {"pdf", "pptx"})
    visual = RecordingParser("vision", {"pdf", "pptx", "png"})
    parser = HybridDocumentParser(
        remote,
        ark_parser=visual,
        preferred_parser="ark",
        local_mineru_parser=local,
    )

    pdf = asyncio.run(parser.parse(tmp_path / "sample.pdf"))
    pptx = asyncio.run(parser.parse(tmp_path / "sample.pptx"))

    assert pdf.metadata["parser"] == "mineru_http"
    assert pptx.metadata["parser"] == "mineru_local"
    assert len(remote.calls) == 1
    assert len(local.calls) == 1
    assert not visual.calls


def test_hybrid_parser_keeps_images_on_visual_model(tmp_path):
    remote = RecordingParser("mineru_http", {"png"})
    visual = RecordingParser("vision", {"png"})
    image_mineru = RecordingParser("mineru_image", {"png"})
    parser = HybridDocumentParser(
        remote,
        ark_parser=visual,
        local_mineru_parser=None,
        image_mineru_parser=image_mineru,
    )

    result = asyncio.run(parser.parse(tmp_path / "sample.png"))

    assert result.metadata["parser"] == "vision"
    assert len(visual.calls) == 1
    assert not image_mineru.calls
