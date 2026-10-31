"""Closed vocabulary for how one Request Record obtained its Token counts.

Metering follows a three-level priority (Runtime final Usage, Runtime
per-request Metrics, in-memory Tokenizer Bundle fallback). ``MeteringSource``
names which level actually produced the persisted counts and
``MetricsCapability`` records how complete that measurement was, so operators
can compare Runtime dialects without reading any request body.
"""

from __future__ import annotations

from enum import StrEnum


class MeteringSource(StrEnum):
    """Which of the three metering levels produced the persisted Token counts."""

    RUNTIME_USAGE = "runtime_usage"
    RUNTIME_METRICS = "runtime_metrics"
    TOKENIZER_FALLBACK = "tokenizer_fallback"
    UNAVAILABLE = "unavailable"


class MetricsCapability(StrEnum):
    """How complete the measurement behind ``MeteringSource`` was.

    ``FULL`` means the Runtime reported both counts under its own tokenization.
    ``PARTIAL`` means a measurement existed but could not be complete: Gateway
    side Tokenizer counting never observes the Runtime's chat template or
    special Tokens, and a fallback that timed out or overflowed its bounded
    buffer is incomplete for the same reason.  ``UNSUPPORTED`` means no usable
    measurement capability existed at all - no Bundle, an External Artifact
    whose source the platform must not read, a Manifest Hash mismatch, or a
    Bundle this Gateway cannot load without executing code.
    """

    FULL = "full"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


__all__ = ["MeteringSource", "MetricsCapability"]
