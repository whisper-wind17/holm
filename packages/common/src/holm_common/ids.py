"""UUIDv7 identity generation with process-local monotonic ordering."""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Callable
from typing import Final

_TIMESTAMP_BITS: Final[int] = 48
_ENTROPY_BITS: Final[int] = 74
_MAX_TIMESTAMP: Final[int] = (1 << _TIMESTAMP_BITS) - 1
_MAX_ENTROPY: Final[int] = (1 << _ENTROPY_BITS) - 1


def _unix_milliseconds() -> int:
    return time.time_ns() // 1_000_000


class UUID7Generator:
    """Generate ordered UUIDv7 values while tolerating a wall-clock rollback."""

    def __init__(
        self,
        *,
        clock_milliseconds: Callable[[], int] = _unix_milliseconds,
        random_bits: Callable[[int], int] = secrets.randbits,
    ) -> None:
        self._clock_milliseconds = clock_milliseconds
        self._random_bits = random_bits
        self._lock = threading.Lock()
        self._last_timestamp = -1
        self._last_entropy = -1

    def new(self) -> uuid.UUID:
        observed_timestamp = self._clock_milliseconds()
        if (
            isinstance(observed_timestamp, bool)
            or not isinstance(observed_timestamp, int)
            or not 0 <= observed_timestamp <= _MAX_TIMESTAMP
        ):
            raise ValueError("UUIDv7 clock must return a 48-bit Unix millisecond integer")

        with self._lock:
            if observed_timestamp > self._last_timestamp:
                timestamp = observed_timestamp
                entropy = self._random_bits(_ENTROPY_BITS)
                if (
                    isinstance(entropy, bool)
                    or not isinstance(entropy, int)
                    or not 0 <= entropy <= _MAX_ENTROPY
                ):
                    raise ValueError("UUIDv7 entropy source returned an invalid value")
            else:
                timestamp = self._last_timestamp
                entropy = self._last_entropy + 1
                if entropy > _MAX_ENTROPY:
                    timestamp += 1
                    entropy = 0
                    if timestamp > _MAX_TIMESTAMP:
                        raise OverflowError("UUIDv7 timestamp space exhausted")

            self._last_timestamp = timestamp
            self._last_entropy = entropy

        random_a = entropy >> 62
        random_b = entropy & ((1 << 62) - 1)
        value = (timestamp << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
        return uuid.UUID(int=value)


_DEFAULT_GENERATOR: Final[UUID7Generator] = UUID7Generator()


def new_uuid7() -> uuid.UUID:
    """Return a process-locally ordered UUIDv7."""
    return _DEFAULT_GENERATOR.new()


def uuid7_unix_ms(value: uuid.UUID) -> int:
    """Extract the Unix millisecond timestamp from a validated UUIDv7."""
    if value.version != 7 or value.variant != uuid.RFC_4122:
        raise ValueError("value must be an RFC variant UUIDv7")
    return value.int >> 80
