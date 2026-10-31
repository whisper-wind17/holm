"""Typed failures exposed by the local Request Ledger boundary."""

from __future__ import annotations


class LedgerError(RuntimeError):
    """Base class for product-level Ledger failures."""


class LedgerConfigurationError(LedgerError):
    """The configured Ledger path or role is unusable."""


class LedgerPermissionError(LedgerError):
    """A Ledger directory or file has unsafe local permissions."""


class LedgerSchemaError(LedgerError):
    """The on-disk Ledger schema is unknown or inconsistent."""


class LedgerStorageError(LedgerError):
    """SQLite or the local filesystem could not complete an operation."""


class LedgerSequenceError(LedgerError):
    """An event would violate the per-request lifecycle."""


class SpoolWriterClosedError(LedgerStorageError):
    """The dedicated Spool Writer is not accepting more events."""


class SpoolWriterUnavailableError(LedgerStorageError):
    """The dedicated Spool Writer entered storage protection."""


__all__ = [
    "LedgerConfigurationError",
    "LedgerError",
    "LedgerPermissionError",
    "LedgerSchemaError",
    "LedgerSequenceError",
    "LedgerStorageError",
    "SpoolWriterClosedError",
    "SpoolWriterUnavailableError",
]
