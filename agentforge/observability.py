"""Small dependency-free metrics and optional OpenTelemetry bridge."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator


class MetricsRegistry:
    """Counters and duration observations rendered in Prometheus text format."""

    def __init__(self):
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: defaultdict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
        self._lock = threading.RLock()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            self._observations[key].append(float(value))

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_labels(labels)} {value:g}")
            for (name, labels), values in sorted(self._observations.items()):
                if not values:
                    continue
                lines.append(f"{name}_count{_labels(labels)} {len(values)}")
                lines.append(f"{name}_sum{_labels(labels)} {sum(values):g}")
        return "\n".join(lines) + ("\n" if lines else "")


@contextmanager
def trace_span(name: str, **attributes) -> Iterator[object]:
    """Use OpenTelemetry when installed, otherwise provide a no-op span."""
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("agentforge")
        with tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                span.set_attribute(key, str(value))
            yield span
    except ImportError:
        started = time.perf_counter()
        span = _NoopSpan(name, attributes, started)
        try:
            yield span
        finally:
            span.duration_seconds = time.perf_counter() - started


class _NoopSpan:
    def __init__(self, name, attributes, started):
        self.name = name
        self.attributes = attributes
        self.started = started
        self.duration_seconds = 0.0

    def set_attribute(self, key, value):
        self.attributes[key] = value


def _labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    escaped = [f'{key}="{value.replace(chr(92), chr(92) + chr(92)).replace(chr(34), chr(92) + chr(34))}"' for key, value in labels]
    return "{" + ",".join(escaped) + "}"
