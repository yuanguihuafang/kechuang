from core.governance.metrics import SupervisorMetrics
from services.observability.metrics import _percentile


def test_p95_uses_upper_sample_for_two_requests():
    supervisor = SupervisorMetrics()
    supervisor.observe(status="SUCCESS", duration_ms=836.674)
    supervisor.observe(status="SUCCESS", duration_ms=6049.0)
    assert supervisor.snapshot()["p95_ms"] == 6049.0
    assert _percentile([836.674, 6049.0], 0.95) == 6049.0
