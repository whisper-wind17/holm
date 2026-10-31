"""Framework-independent persisted vocabulary for model acquisition."""

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from holm_domain.errors import ErrorCategory, ErrorCode, classify_error

EXTERNAL_REVISION: Final[str] = "external"


class DownloadCancellationReason(StrEnum):
    DUPLICATE_RESOLVED_REVISION = ErrorCode.DUPLICATE_RESOLVED_REVISION.value


class DownloadFailureReason(StrEnum):
    """Failures persisted on Download Tasks, sourced from stable protocol codes."""

    INSUFFICIENT_STORAGE = ErrorCode.INSUFFICIENT_STORAGE.value
    DOWNLOAD_TASK_STATE_MISMATCH = ErrorCode.DOWNLOAD_TASK_STATE_MISMATCH.value
    DOWNLOAD_TASK_IDENTITY_MISMATCH = ErrorCode.DOWNLOAD_TASK_IDENTITY_MISMATCH.value
    DOWNLOAD_PROCESS_FAILED = ErrorCode.DOWNLOAD_PROCESS_FAILED.value
    STAGING_FILESYSTEM_MISMATCH = ErrorCode.STAGING_FILESYSTEM_MISMATCH.value
    STAGING_CLEANUP_FAILED = ErrorCode.STAGING_CLEANUP_FAILED.value
    ARTIFACT_FILE_LIST_INVALID = ErrorCode.ARTIFACT_FILE_LIST_INVALID.value
    ARTIFACT_MANIFEST_HASH_FAILED = ErrorCode.ARTIFACT_MANIFEST_HASH_FAILED.value
    MODEL_LAYOUT_UNSUPPORTED = ErrorCode.MODEL_LAYOUT_UNSUPPORTED.value
    ARTIFACT_REQUIRED_FILES_INVALID = ErrorCode.ARTIFACT_REQUIRED_FILES_INVALID.value
    ARTIFACT_SAFETENSORS_INVALID = ErrorCode.ARTIFACT_SAFETENSORS_INVALID.value
    ARTIFACT_RUNTIME_INCOMPATIBLE = ErrorCode.ARTIFACT_RUNTIME_INCOMPATIBLE.value
    REMOTE_CODE_NOT_ALLOWED = ErrorCode.REMOTE_CODE_NOT_ALLOWED.value
    EXTERNAL_PATH_UNREADABLE = ErrorCode.EXTERNAL_PATH_UNREADABLE.value
    ARTIFACT_UNSAFE_FILE = ErrorCode.ARTIFACT_UNSAFE_FILE.value
    ARTIFACT_PUBLISH_FAILED = ErrorCode.ARTIFACT_PUBLISH_FAILED.value


DOWNLOAD_FAILURE_CATEGORIES: Final = MappingProxyType(
    {
        DownloadFailureReason.INSUFFICIENT_STORAGE: ErrorCategory.CAPACITY,
        DownloadFailureReason.DOWNLOAD_TASK_STATE_MISMATCH: ErrorCategory.CONFLICT,
        DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH: ErrorCategory.CONFLICT,
        DownloadFailureReason.DOWNLOAD_PROCESS_FAILED: ErrorCategory.DEPENDENCY,
        DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH: ErrorCategory.DEPENDENCY,
        DownloadFailureReason.STAGING_CLEANUP_FAILED: ErrorCategory.DEPENDENCY,
        DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED: ErrorCategory.VALIDATION,
        DownloadFailureReason.MODEL_LAYOUT_UNSUPPORTED: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_RUNTIME_INCOMPATIBLE: ErrorCategory.VALIDATION,
        DownloadFailureReason.REMOTE_CODE_NOT_ALLOWED: ErrorCategory.VALIDATION,
        DownloadFailureReason.EXTERNAL_PATH_UNREADABLE: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_UNSAFE_FILE: ErrorCategory.VALIDATION,
        DownloadFailureReason.ARTIFACT_PUBLISH_FAILED: ErrorCategory.DEPENDENCY,
    }
)


def assert_download_failure_registry_complete(
    reasons: type[DownloadFailureReason] = DownloadFailureReason,
) -> None:
    """Fail loudly when a persisted failure is added without classification."""

    if set(DOWNLOAD_FAILURE_CATEGORIES) != set(reasons):
        raise AssertionError("download failure reason registry drift")
    for reason, category in DOWNLOAD_FAILURE_CATEGORIES.items():
        protocol_code = ErrorCode(reason.value)
        if protocol_code.name != reason.name or classify_error(protocol_code) is not category:
            raise AssertionError("download failure reason registry drift")


assert_download_failure_registry_complete()


class DownloadFailureStage(StrEnum):
    """Pipeline stage at which a Download Task failed.

    The words are the state names of the stages that actually persist an
    ``error_code``.  ``Queued`` is absent on purpose: nothing writes a code while
    a task waits, so a ``Queued`` member would be a stage no row can ever carry.
    """

    RESOLVING = "Resolving"
    DOWNLOADING = "Downloading"
    VERIFYING = "Verifying"
    PUBLISHING = "Publishing"


DOWNLOAD_ERROR_STAGES: Final[
    MappingProxyType[DownloadCancellationReason | DownloadFailureReason, DownloadFailureStage]
] = MappingProxyType(
    {
        # ``core.download_tasks.error_code`` holds two vocabularies, not one:
        # ``merge_subscriptions`` persists the cancellation reason on the losing
        # side of a merge, which is an ordinary outcome rather than a
        # failure.  A stage table that covered only ``DownloadFailureReason``
        # would turn that normal row into an unreadable one.
        DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION: DownloadFailureStage.RESOLVING,
        DownloadFailureReason.INSUFFICIENT_STORAGE: DownloadFailureStage.DOWNLOADING,
        DownloadFailureReason.DOWNLOAD_TASK_STATE_MISMATCH: DownloadFailureStage.DOWNLOADING,
        DownloadFailureReason.DOWNLOAD_TASK_IDENTITY_MISMATCH: DownloadFailureStage.DOWNLOADING,
        DownloadFailureReason.DOWNLOAD_PROCESS_FAILED: DownloadFailureStage.DOWNLOADING,
        DownloadFailureReason.STAGING_FILESYSTEM_MISMATCH: DownloadFailureStage.DOWNLOADING,
        DownloadFailureReason.STAGING_CLEANUP_FAILED: DownloadFailureStage.DOWNLOADING,
        DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.MODEL_LAYOUT_UNSUPPORTED: DownloadFailureStage.RESOLVING,
        DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.ARTIFACT_RUNTIME_INCOMPATIBLE: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.REMOTE_CODE_NOT_ALLOWED: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.EXTERNAL_PATH_UNREADABLE: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.ARTIFACT_UNSAFE_FILE: DownloadFailureStage.VERIFYING,
        DownloadFailureReason.ARTIFACT_PUBLISH_FAILED: DownloadFailureStage.PUBLISHING,
    }
)


def assert_download_error_stage_registry_complete(
    reasons: type[DownloadFailureReason] = DownloadFailureReason,
    cancellations: type[DownloadCancellationReason] = DownloadCancellationReason,
) -> None:
    """Fail loudly when a persisted ``error_code`` gains no stage.

    Set equality, not containment: an added reason and a removed one are both
    drift, and either would otherwise show up first as a read endpoint that
    cannot render a perfectly ordinary row.
    """

    if {member.value for member in reasons} & {member.value for member in cancellations}:
        # The two vocabularies share one database column, so a shared value
        # would make ``error_code`` ambiguous and would collapse two rows of the
        # value-keyed lookup below into one.
        raise AssertionError("download error stage registry drift")
    if len(DOWNLOAD_ERROR_STAGES) != len(reasons) + len(cancellations):
        raise AssertionError("download error stage registry drift")
    if {member.value for member in DOWNLOAD_ERROR_STAGES} != {
        member.value for member in reasons
    } | {member.value for member in cancellations}:
        raise AssertionError("download error stage registry drift")


assert_download_error_stage_registry_complete()

# ``Enum.__hash__`` hashes the member *name*, so the table above cannot be
# indexed by the raw string read back from the database. This is the same table
# keyed by the persisted value, built once and guarded by the assertion above.
_STAGE_BY_ERROR_CODE: Final[MappingProxyType[str, DownloadFailureStage]] = MappingProxyType(
    {reason.value: stage for reason, stage in DOWNLOAD_ERROR_STAGES.items()}
)


class UnknownDownloadErrorCode(LookupError):
    """A persisted ``error_code`` outside both vocabularies; never echoes it."""

    def __init__(self) -> None:
        super().__init__("persisted download error code is not classified")


def download_error_stage(error_code: str | None) -> DownloadFailureStage | None:
    """Classify one persisted ``error_code``; ``None`` stays ``None``.

    An unrecognized code raises instead of being passed through: the UI needs
    an unknown value to reject the response, and passing it through would let an
    unclassified code reach the UI as if it were a stage.
    """

    if error_code is None:
        return None
    stage = _STAGE_BY_ERROR_CODE.get(error_code)
    if stage is None:
        # Raised outside the lookup so the rejected code is not carried along
        # by an exception chain into a log record or an error body.
        raise UnknownDownloadErrorCode()
    return stage


class DownloadTaskStatus(StrEnum):
    """Download Task lifecycle states."""

    QUEUED = "Queued"
    RESOLVING = "Resolving"
    DOWNLOADING = "Downloading"
    PAUSED = "Paused"
    VERIFYING = "Verifying"
    PUBLISHING = "Publishing"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class ModelArtifactStatus(StrEnum):
    """Model Artifact lifecycle states."""

    READY = "Ready"
    CORRUPTED = "Corrupted"
    MISSING = "Missing"


class ArtifactStorageType(StrEnum):
    """Whether Artifact files are managed by the platform or externally owned."""

    MANAGED = "Managed"
    EXTERNAL = "External"


class DownloadSubscriberType(StrEnum):
    """Kinds of consumers that can follow a Download Task."""

    INSTANCE = "Instance"
    STANDALONE = "Standalone"


class ModelSourceType(StrEnum):
    """Credential-bearing official model sources."""

    HUGGING_FACE = "Hugging Face"
    MODELSCOPE = "ModelScope"


class ModelOriginType(StrEnum):
    """Complete Model source vocabulary."""

    HUGGING_FACE = "Hugging Face"
    MODELSCOPE = "ModelScope"
    LOCAL_PATH = "Local Path"


class MetadataStatus(StrEnum):
    """Whether a catalog Model's metadata was confirmed by its source."""

    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"


class ModelSourceConfigurationState(StrEnum):
    """Credential configuration states that must remain distinguishable."""

    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"
    KEY_UNAVAILABLE = "key_unavailable"


class ModelSourceValidationStatus(StrEnum):
    """Observable source outcomes persisted after validation."""

    ACCESSIBLE = "accessible"
    INVALID_CREDENTIAL = "invalid_credential"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"


class ModelSourceValidationAction(StrEnum):
    """Stable operator directions derived from a validation conclusion."""

    UPDATE_CREDENTIAL = "update_credential"
    CHECK_SOURCE_ACCESS = "check_source_access"
    CHECK_SOURCE_REFERENCE = "check_source_reference"
    CHECK_SOURCE_REFERENCE_AND_CONFIGURE_CREDENTIAL = (
        "check_source_reference_and_configure_credential"
    )
    RETRY_LATER = "retry_later"
    INSTALL_VALIDATOR = "install_validator"


class ModelSourceValidationReason(StrEnum):
    """Stable, sanitized reasons returned by the validation boundary."""

    CREDENTIAL_VALID = "credential_valid"
    CREDENTIAL_INVALID = "credential_invalid"
    SOURCE_PERMISSION_DENIED = "source_permission_denied"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_NOT_FOUND_OR_INACCESSIBLE = "source_not_found_or_inaccessible"
    SOURCE_RATE_LIMITED = "source_rate_limited"
    VALIDATOR_UNAVAILABLE = "validator_unavailable"
    VALIDATION_TIMEOUT = "validation_timeout"
    VALIDATION_FAILED = "validation_failed"


class CredentialType(StrEnum):
    """Kinds of secrets that may be bound into a model-source envelope."""

    ACCESS_TOKEN = "access_token"


__all__ = [
    "ArtifactStorageType",
    "CredentialType",
    "DOWNLOAD_ERROR_STAGES",
    "DownloadCancellationReason",
    "DOWNLOAD_FAILURE_CATEGORIES",
    "DownloadFailureReason",
    "DownloadFailureStage",
    "DownloadSubscriberType",
    "DownloadTaskStatus",
    "ModelArtifactStatus",
    "MetadataStatus",
    "ModelOriginType",
    "ModelSourceConfigurationState",
    "ModelSourceType",
    "ModelSourceValidationAction",
    "ModelSourceValidationReason",
    "ModelSourceValidationStatus",
    "UnknownDownloadErrorCode",
    "assert_download_error_stage_registry_complete",
    "assert_download_failure_registry_complete",
    "download_error_stage",
]
