from __future__ import annotations

from agentforge.observability import MetricsRegistry, current_trace_id, trace_span


def test_metrics_render_histogram_without_high_cardinality_labels():
    metrics = MetricsRegistry()
    metrics.inc("agentforge_runs_total", status="succeeded")
    metrics.observe("agentforge_run_duration_seconds", 0.2)
    metrics.inc("agentforge_runs_total", run_id="should-not-be-rendered")
    rendered = metrics.render()
    assert "agentforge_runs_total{status=\"succeeded\"} 1" in rendered
    assert "agentforge_run_duration_seconds_bucket{le=\"0.25\"} 1" in rendered
    assert "agentforge_run_duration_seconds_bucket{le=\"+Inf\"} 1" in rendered
    assert "agentforge_run_duration_seconds_count 1" in rendered
    assert "run_id" not in rendered


def test_trace_span_does_not_repeat_body_import_error():
    calls = 0
    try:
        with trace_span("test.import-error"):
            calls += 1
            raise ImportError("raised by the traced operation")
    except ImportError:
        pass
    assert calls == 1


def test_trace_span_has_trace_id_for_events_without_exporter():
    assert current_trace_id() is None
    with trace_span("test.span", component="test"):
        trace_id = current_trace_id()
        assert trace_id is not None
        assert len(trace_id) == 32
    assert current_trace_id() is None
