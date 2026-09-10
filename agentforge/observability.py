"""Small dependency-free metrics and optional OpenTelemetry bridge."""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_fallback_trace_id: ContextVar[str | None] = ContextVar("agentforge_trace_id", default=None)
_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_FORBIDDEN_LABELS = frozenset({"run_id", "attempt_id", "tool_call_id"})


class MetricsRegistry:
    """Counters and duration observations rendered in Prometheus text format."""

    def __init__(self):
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: defaultdict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
        self._lock = threading.RLock()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, _metric_labels(labels))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, _metric_labels(labels))
        with self._lock:
            self._observations[key].append(float(value))

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name in sorted({name for name, _ in self._counters}):
                lines.append(f"# TYPE {name} counter")
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_labels(labels)} {value:g}")
            for name in sorted({name for name, _ in self._observations}):
                lines.append(f"# TYPE {name} histogram")
            for (name, labels), values in sorted(self._observations.items()):
                if not values:
                    continue
                for bound in _DEFAULT_BUCKETS:
                    bucket_count = sum(1 for value in values if value <= bound)
                    bucket_labels = tuple(sorted((*labels, ("le", _format_number(bound)))))
                    lines.append(f"{name}_bucket{_labels(bucket_labels)} {bucket_count}")
                infinity_labels = tuple(sorted((*labels, ("le", "+Inf"))))
                lines.append(f"{name}_bucket{_labels(infinity_labels)} {len(values)}")
                lines.append(f"{name}_count{_labels(labels)} {len(values)}")
                lines.append(f"{name}_sum{_labels(labels)} {sum(values):g}")
        return "\n".join(lines) + ("\n" if lines else "")


@contextmanager
def trace_span(name: str, **attributes) -> Iterator[object]:
    """Use OpenTelemetry when installed, otherwise provide a no-op span."""
    try:
        from opentelemetry import trace
    except ImportError:
        with _fallback_span(name, attributes) as span:
            yield span
        return

    tracer = trace.get_tracer("agentforge")
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, str(value))
        context = span.get_span_context()
        valid = bool(context and context.is_valid)
        token = None
        if not valid and _fallback_trace_id.get() is None:
            token = _fallback_trace_id.set(uuid.uuid4().hex)
        try:
            yield span
        finally:
            if token is not None:
                _fallback_trace_id.reset(token)


def current_trace_id() -> str | None:
    """Return the active OTel trace ID, or a correlation ID for no-op tracing."""
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return f"{context.trace_id:032x}"
    except ImportError:
        pass
    return _fallback_trace_id.get()


def configure_tracing(endpoint: str | None = None) -> bool:
    """Install an optional OTLP exporter; return false when unavailable."""
    target = endpoint or __import__("os").environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not target:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False
    try:
        provider = TracerProvider(resource=Resource.create({"service.name": "agentforge"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=target)))
        trace.set_tracer_provider(provider)
    except Exception:
        # OTLP is optional; an invalid endpoint must not disable local runs.
        return False
    return True


class _NoopSpan:
    def __init__(self, name, attributes, started, trace_id):
        self.name = name
        self.attributes = attributes
        self.started = started
        self.trace_id = trace_id
        self.duration_seconds = 0.0

    def set_attribute(self, key, value):
        self.attributes[key] = value


@contextmanager
def _fallback_span(name: str, attributes: dict[str, object]) -> Iterator[_NoopSpan]:
    started = time.perf_counter()
    token = None
    if _fallback_trace_id.get() is None:
        token = _fallback_trace_id.set(uuid.uuid4().hex)
    span = _NoopSpan(name, dict(attributes), started, _fallback_trace_id.get() or "")
    try:
        yield span
    finally:
        span.duration_seconds = time.perf_counter() - started
        if token is not None:
            _fallback_trace_id.reset(token)


def _metric_labels(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (str(key), str(value))
            for key, value in labels.items()
            if str(key) not in _FORBIDDEN_LABELS
        )
    )


def _labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    escaped = [f'{key}="{value.replace(chr(92), chr(92) + chr(92)).replace(chr(34), chr(92) + chr(34))}"' for key, value in labels]
    return "{" + ",".join(escaped) + "}"


def _format_number(value: float) -> str:
    return f"{value:g}"
