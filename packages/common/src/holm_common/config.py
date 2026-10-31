"""Closed non-sensitive configuration schema and deterministic layer merging."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]
StrictPercent = Annotated[int, Field(strict=True, gt=0, lt=100)]
MIN_RETENTION_DAYS: Final = 30
RetentionDays = Annotated[int, Field(strict=True, ge=MIN_RETENTION_DAYS)]

ENVIRONMENT_PREFIX: Final[str] = "HOLM__"
SENSITIVE_ENVIRONMENT_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "api_key_pepper",
        "database_password",
        "master_key",
        "pepper",
        "secret",
        "tls_private_key",
    }
)


class ConfigLoadError(ValueError):
    """A sanitized configuration error that never embeds source values."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()) -> None:
        self.code = code
        self.fields = fields
        super().__init__(f"configuration rejected: {code}")


class _ClosedConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class StorageConfig(_ClosedConfigModel):
    model_root: Path = Path("/var/lib/holm/models")
    tokenizer_root: Path = Path("/var/lib/holm/tokenizers")
    external_model_roots: tuple[Path, ...] = (Path("/srv/models"),)
    disk_warning_percent: StrictPercent = 10
    disk_critical_percent: StrictPercent = 5
    disk_critical_free_gib: StrictPositiveInt = 5

    @field_validator("model_root", "tokenizer_root")
    @classmethod
    def validate_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("path must be absolute")
        return value

    @field_validator("external_model_roots")
    @classmethod
    def validate_external_roots(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(not path.is_absolute() for path in value):
            raise ValueError("all paths must be absolute")
        # The path check compares a candidate against the allowed roots *after*
        # ``realpath``.  A root that is itself reached through a symbolic link
        # would therefore never match its own resolved form, and swapping that
        # link silently re-points the whole Allowlist.  A root must name the
        # real path, so the configured spelling and its ``realpath`` must be
        # the same string.  This does not require the directory to exist:
        # ``realpath`` of a link-free, absent absolute path is itself.
        if any(os.path.realpath(path) != str(path) for path in value):
            raise ValueError("all paths must already be their own realpath")
        return value

    @model_validator(mode="after")
    def validate_disk_threshold_order(self) -> StorageConfig:
        if self.disk_critical_percent >= self.disk_warning_percent:
            raise ValueError("critical threshold must be below warning threshold")
        return self


class GatewayConfig(_ClosedConfigModel):
    max_request_body_mib: StrictPositiveInt = 16
    authorization_snapshot_max_age_seconds: StrictPositiveInt = 60
    connect_timeout_seconds: StrictPositiveInt = 3
    first_token_timeout_seconds: StrictPositiveInt = 600
    total_timeout_seconds: StrictPositiveInt = 3600
    max_inflight_requests: StrictPositiveInt = 512
    max_inflight_per_instance: Literal["auto"] | StrictPositiveInt = "auto"
    runtime_pool_timeout_ms: StrictPositiveInt = 100
    runtime_pool_max_connections_per_instance: Literal["auto"] | StrictPositiveInt = "auto"
    runtime_pool_max_keepalive_per_instance: StrictPositiveInt = 32
    tokenizer_cache_max_mib: StrictPositiveInt = 512
    tokenizer_worker_threads: StrictPositiveInt = 2
    tokenizer_fallback_timeout_seconds: StrictPositiveInt = 5
    ledger_group_commit_max_wait_ms: StrictPositiveInt = 2
    ledger_group_commit_max_requests: StrictPositiveInt = 64


class RuntimeConfig(_ClosedConfigModel):
    start_timeout_seconds: StrictPositiveInt = 1800
    stop_grace_seconds: StrictPositiveInt = 60
    restart_limit: StrictPositiveInt = 3
    restart_window_seconds: StrictPositiveInt = 600
    max_gpus_per_instance: Literal["auto"] | StrictPositiveInt = "auto"


class TelemetryConfig(_ClosedConfigModel):
    scrape_interval_seconds: StrictPositiveInt = 5
    scrape_timeout_seconds: StrictPositiveInt = 2
    raw_retention_seconds: StrictPositiveInt = 3600
    aggregate_retention_days: RetentionDays = MIN_RETENTION_DAYS


class RetentionConfig(_ClosedConfigModel):
    request_records_days: RetentionDays = MIN_RETENTION_DAYS
    download_tasks_days: StrictPositiveInt = 30


class PlatformConfig(_ClosedConfigModel):
    public_base_url: AnyHttpUrl
    timezone_default: str = "Asia/Shanghai"
    storage: StorageConfig = StorageConfig()
    gateway: GatewayConfig = GatewayConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    telemetry: TelemetryConfig = TelemetryConfig()
    retention: RetentionConfig = RetentionConfig()

    @field_validator("public_base_url")
    @classmethod
    def validate_https(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("public base URL must use HTTPS")
        return value

    @field_validator("timezone_default")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an installed IANA zone") from error
        return value


def _environment_paths(
    model_type: type[BaseModel], prefix: tuple[str, ...] = ()
) -> frozenset[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for name, field in model_type.model_fields.items():
        annotation = field.annotation
        current = (*prefix, name)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            paths.update(_environment_paths(annotation, current))
        else:
            paths.add(current)
    return frozenset(paths)


ENVIRONMENT_PATHS: Final[frozenset[tuple[str, ...]]] = _environment_paths(PlatformConfig)


def _parse_environment_value(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor = data
    for segment in path[:-1]:
        existing = cursor.get(segment)
        nested = dict(existing) if isinstance(existing, Mapping) else {}
        cursor[segment] = nested
        cursor = nested
    cursor[path[-1]] = value


def _environment_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    seen_paths: set[tuple[str, ...]] = set()
    for key in sorted(environ):
        if not key.startswith(ENVIRONMENT_PREFIX):
            continue
        path = tuple(segment.lower() for segment in key[len(ENVIRONMENT_PREFIX) :].split("__"))
        if not path or any(not segment for segment in path):
            raise ConfigLoadError("unknown_environment_field")
        if path[0] in SENSITIVE_ENVIRONMENT_ROOTS:
            raise ConfigLoadError("sensitive_environment_field")
        if path not in ENVIRONMENT_PATHS:
            raise ConfigLoadError("unknown_environment_field", (".".join(path),))
        if path in seen_paths:
            raise ConfigLoadError("duplicate_environment_field", (".".join(path),))
        seen_paths.add(path)
        _set_nested(overrides, path, _parse_environment_value(environ[key]))
    return overrides


def _merge_nested(destination: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = destination.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            nested = dict(existing)
            _merge_nested(nested, value)
            destination[key] = nested
        else:
            destination[key] = copy.deepcopy(value)


def load_config(
    config: Mapping[str, Any], *, environ: Mapping[str, str] | None = None
) -> PlatformConfig:
    """Load closed configuration with environment values taking highest precedence."""
    merged = copy.deepcopy(dict(config))
    _merge_nested(merged, _environment_overrides(os.environ if environ is None else environ))
    try:
        return PlatformConfig.model_validate(merged)
    except ValidationError as error:
        fields = tuple(
            ".".join(str(segment) for segment in item["loc"])
            for item in error.errors(include_input=False, include_url=False)
        )
        raise ConfigLoadError("validation_failed", fields) from None
