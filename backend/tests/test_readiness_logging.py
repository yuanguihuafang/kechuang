import logging
import re
import sys
from datetime import datetime, timezone

from services.observability.logging import ColoredConsoleFormatter, setup_logger


def test_console_formatter_includes_exception_traceback():
    try:
        raise RuntimeError("embedding service unavailable")
    except RuntimeError:
        record = logging.LogRecord(
            "kechuang.retrieval.agent",
            logging.WARNING,
            __file__,
            1,
            "readiness_probe_failed dependency=%s error_type=%s error=%s",
            ("embedding", "RuntimeError", "embedding service unavailable"),
            None,
        )
        record.exc_info = sys.exc_info()

    output = ColoredConsoleFormatter().format(record)

    assert "dependency=embedding" in output
    assert "RuntimeError: embedding service unavailable" in output


def test_console_formatter_shows_span_dependency_and_duration():
    record = logging.LogRecord(
        "kechuang.perception", logging.INFO, __file__, 1, "span_end", (), None
    )
    record.structured = {
        "event": "span_end",
        "span_name": "readiness_probe",
        "dependency": "minio",
        "duration_ms": 12,
    }

    output = ColoredConsoleFormatter().format(record)

    assert "span=readiness_probe" in output
    assert "dependency=minio" in output
    assert "duration_ms=12" in output


def test_console_formatter_uses_rich_style_timestamp_level_and_source():
    record = logging.LogRecord(
        "mcp.server",
        logging.INFO,
        __file__.replace("test_readiness_logging.py", "streamable_http.py"),
        831,
        "Terminating session: %s",
        (None,),
        None,
    )
    record.created = datetime(2026, 9, 24, 2, 16, 18, tzinfo=timezone.utc).timestamp()

    output = ColoredConsoleFormatter().format(record)
    plain_output = re.sub(r"\x1b\[[0-9;]*m", "", output)

    assert (
        "[09/24/26 10:16:18] INFO     Terminating session: None streamable_http.py:831"
        == plain_output
    )


def test_setup_logger_installs_shared_root_console_handler():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    logger = logging.getLogger("kechuang.test_shared_console")
    original_logger_handlers = logger.handlers[:]
    original_propagate = logger.propagate
    try:
        root.handlers.clear()
        root.setLevel(logging.WARNING)

        configured = setup_logger("test_shared_console", console=True, json_file=False)

        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, ColoredConsoleFormatter)
        assert configured.propagate is True
        assert configured.handlers == []
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
        logger.handlers[:] = original_logger_handlers
        logger.propagate = original_propagate


def test_console_colors_distinguish_progress_from_ordinary_logs():
    formatter = ColoredConsoleFormatter(use_color=True)
    record = logging.LogRecord("kechuang.retrieval", logging.INFO, __file__, 1, "阶段", (), None)
    assert "\x1b[" not in formatter.format(record)
    record.structured = {"event": "retrieval_finished", "latencyMs": 123}
    assert formatter.format(record).startswith("\x1b[32m")
    assert "duration_ms=123" in formatter.format(record)
    record.levelno = logging.ERROR
    record.levelname = "ERROR"
    assert formatter.format(record).startswith("\x1b[31m")
    assert "\x1b[" not in ColoredConsoleFormatter(use_color=False).format(record)


def test_console_respects_no_color_and_noninteractive_output(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "")
    record = logging.LogRecord("kechuang", logging.ERROR, __file__, 1, "失败", (), None)
    assert "\x1b[" not in ColoredConsoleFormatter().format(record)
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    assert "\x1b[" not in ColoredConsoleFormatter().format(record)
