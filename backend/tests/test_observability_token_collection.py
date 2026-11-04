from sqlalchemy import create_engine, select

from services.persistence.repositories.observability_repository import PostgresMetricsStore
from services.persistence.sqlalchemy import platform_metric_events


def test_metrics_snapshot_keeps_latency_and_failures_without_token_totals():
    engine = create_engine("sqlite:///:memory:")
    platform_metric_events.create(engine)
    store = PostgresMetricsStore(dsn="sqlite:///:memory:", engine=engine)

    store.record(metric_name="llm", status="SUCCESS", duration_ms=125)
    store.record(
        metric_name="llm",
        status="FAILED",
        duration_ms=250,
        error_code="ConnectError",
    )

    metric = store.snapshot()["metrics"]["llm"]

    assert metric["avg_ms"] == 187.5
    assert metric["error_count"] == 1
    assert metric["error_rate"] == 0.5
    assert metric["error_codes"] == {"ConnectError": 1}
    assert not {"input_tokens", "output_tokens", "total_tokens", "avg_input_tokens", "avg_output_tokens"} & metric.keys()
    assert "input_tokens" in platform_metric_events.c
    assert "output_tokens" in platform_metric_events.c
    with engine.connect() as connection:
        token_rows = connection.execute(
            select(platform_metric_events.c.input_tokens, platform_metric_events.c.output_tokens)
        ).all()
    assert token_rows == [(None, None), (None, None)]
