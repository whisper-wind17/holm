"""Infrastructure-independent common contracts for Private AI Workspace."""

from holm_common.config import ConfigLoadError, PlatformConfig, load_config
from holm_common.download_staging import (
    DOWNLOAD_HEADROOM_BYTES,
    DOWNLOAD_HEADROOM_PERCENT,
    GIBIBYTE,
    DiskSpaceSnapshot,
    DownloadCapacity,
    ProgressWriteThrottle,
    StagingFilesystemMismatch,
    StagingLayout,
    StagingPathRejected,
    TokenizerStagingLayout,
    assert_same_filesystem,
    check_download_capacity,
)
from holm_common.ids import UUID7Generator, new_uuid7, uuid7_unix_ms
from holm_common.secrets import SecretFileError, read_secret_file
from holm_common.startup import startup_failure_code
from holm_common.time import (
    UTCDateTimeError,
    as_utc,
    format_rfc3339_utc,
    parse_rfc3339_utc,
    require_utc,
    utc_now,
)
from holm_common.usernames import (
    ACCOUNT_NAME_MAX_LENGTH,
    ACCOUNT_NAME_MIN_LENGTH,
    ACCOUNT_NAME_PATTERN,
    DISPLAY_NAME_MAX_LENGTH,
    USERNAME_MAX_LENGTH,
    UsernameError,
    normalize_account_name,
    normalize_display_name,
    normalize_username,
)

__all__ = [
    "ACCOUNT_NAME_MAX_LENGTH",
    "ACCOUNT_NAME_MIN_LENGTH",
    "ACCOUNT_NAME_PATTERN",
    "DISPLAY_NAME_MAX_LENGTH",
    "DOWNLOAD_HEADROOM_BYTES",
    "DOWNLOAD_HEADROOM_PERCENT",
    "GIBIBYTE",
    "USERNAME_MAX_LENGTH",
    "ConfigLoadError",
    "DiskSpaceSnapshot",
    "DownloadCapacity",
    "PlatformConfig",
    "ProgressWriteThrottle",
    "SecretFileError",
    "startup_failure_code",
    "StagingFilesystemMismatch",
    "StagingLayout",
    "StagingPathRejected",
    "TokenizerStagingLayout",
    "UTCDateTimeError",
    "UUID7Generator",
    "UsernameError",
    "as_utc",
    "assert_same_filesystem",
    "check_download_capacity",
    "format_rfc3339_utc",
    "load_config",
    "new_uuid7",
    "normalize_account_name",
    "normalize_display_name",
    "normalize_username",
    "parse_rfc3339_utc",
    "read_secret_file",
    "require_utc",
    "utc_now",
    "uuid7_unix_ms",
]
