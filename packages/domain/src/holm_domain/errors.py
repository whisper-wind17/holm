"""Protocol-independent stable error classification."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    CAPACITY = "capacity"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    RESYNC = "resync"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_TENSOR_PARALLEL_SIZE = "invalid_tensor_parallel_size"
    UNSUPPORTED_GPU_TOPOLOGY = "unsupported_gpu_topology"
    REMOTE_CODE_NOT_ALLOWED = "remote_code_not_allowed"
    INVALID_API_KEY = "invalid_api_key"
    API_KEY_EXPIRED = "api_key_expired"
    SERVICE_NOT_AUTHORIZED = "service_not_authorized"
    MODEL_NOT_FOUND = "model_not_found"
    VERSION_CONFLICT = "version_conflict"
    DUPLICATE_RESOLVED_REVISION = "duplicate_resolved_revision"
    INSUFFICIENT_STORAGE = "insufficient_storage"
    DOWNLOAD_TASK_STATE_MISMATCH = "download_task_state_mismatch"
    DOWNLOAD_TASK_IDENTITY_MISMATCH = "download_task_identity_mismatch"
    DOWNLOAD_PROCESS_FAILED = "download_process_failed"
    STAGING_FILESYSTEM_MISMATCH = "staging_filesystem_mismatch"
    STAGING_CLEANUP_FAILED = "staging_cleanup_failed"
    ARTIFACT_FILE_LIST_INVALID = "artifact_file_list_invalid"
    ARTIFACT_MANIFEST_HASH_FAILED = "artifact_manifest_hash_failed"
    MODEL_LAYOUT_UNSUPPORTED = "model_layout_unsupported"
    ARTIFACT_REQUIRED_FILES_INVALID = "artifact_required_files_invalid"
    ARTIFACT_SAFETENSORS_INVALID = "artifact_safetensors_invalid"
    ARTIFACT_RUNTIME_INCOMPATIBLE = "artifact_runtime_incompatible"
    EXTERNAL_PATH_UNREADABLE = "external_path_unreadable"
    ARTIFACT_UNSAFE_FILE = "artifact_unsafe_file"
    ARTIFACT_PUBLISH_FAILED = "artifact_publish_failed"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    SERVICE_DISABLED = "service_disabled"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    GATEWAY_OVERLOADED = "gateway_overloaded"
    USAGE_STORAGE_UNAVAILABLE = "usage_storage_unavailable"
    CONTROL_PLANE_UNAVAILABLE = "control_plane_unavailable"
    INFERENCE_TIMEOUT = "inference_timeout"
    RESYNC_REQUIRED = "resync_required"


GATEWAY_ERROR_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.INVALID_REQUEST,
        ErrorCode.INVALID_API_KEY,
        ErrorCode.API_KEY_EXPIRED,
        ErrorCode.SERVICE_NOT_AUTHORIZED,
        ErrorCode.MODEL_NOT_FOUND,
        ErrorCode.RATE_LIMIT_EXCEEDED,
        ErrorCode.SERVICE_DISABLED,
        ErrorCode.BACKEND_UNAVAILABLE,
        ErrorCode.GATEWAY_OVERLOADED,
        ErrorCode.USAGE_STORAGE_UNAVAILABLE,
        ErrorCode.CONTROL_PLANE_UNAVAILABLE,
        ErrorCode.INFERENCE_TIMEOUT,
    }
)


ERROR_CATEGORIES: Final = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: ErrorCategory.VALIDATION,
        ErrorCode.INVALID_TENSOR_PARALLEL_SIZE: ErrorCategory.VALIDATION,
        ErrorCode.UNSUPPORTED_GPU_TOPOLOGY: ErrorCategory.VALIDATION,
        ErrorCode.REMOTE_CODE_NOT_ALLOWED: ErrorCategory.VALIDATION,
        ErrorCode.INVALID_API_KEY: ErrorCategory.AUTHENTICATION,
        ErrorCode.API_KEY_EXPIRED: ErrorCategory.AUTHENTICATION,
        ErrorCode.SERVICE_NOT_AUTHORIZED: ErrorCategory.AUTHORIZATION,
        ErrorCode.MODEL_NOT_FOUND: ErrorCategory.NOT_FOUND,
        ErrorCode.VERSION_CONFLICT: ErrorCategory.CONFLICT,
        ErrorCode.DUPLICATE_RESOLVED_REVISION: ErrorCategory.CONFLICT,
        ErrorCode.INSUFFICIENT_STORAGE: ErrorCategory.CAPACITY,
        ErrorCode.DOWNLOAD_TASK_STATE_MISMATCH: ErrorCategory.CONFLICT,
        ErrorCode.DOWNLOAD_TASK_IDENTITY_MISMATCH: ErrorCategory.CONFLICT,
        ErrorCode.DOWNLOAD_PROCESS_FAILED: ErrorCategory.DEPENDENCY,
        ErrorCode.STAGING_FILESYSTEM_MISMATCH: ErrorCategory.DEPENDENCY,
        ErrorCode.STAGING_CLEANUP_FAILED: ErrorCategory.DEPENDENCY,
        ErrorCode.ARTIFACT_FILE_LIST_INVALID: ErrorCategory.VALIDATION,
        ErrorCode.ARTIFACT_MANIFEST_HASH_FAILED: ErrorCategory.VALIDATION,
        ErrorCode.MODEL_LAYOUT_UNSUPPORTED: ErrorCategory.VALIDATION,
        ErrorCode.ARTIFACT_REQUIRED_FILES_INVALID: ErrorCategory.VALIDATION,
        ErrorCode.ARTIFACT_SAFETENSORS_INVALID: ErrorCategory.VALIDATION,
        ErrorCode.ARTIFACT_RUNTIME_INCOMPATIBLE: ErrorCategory.VALIDATION,
        ErrorCode.EXTERNAL_PATH_UNREADABLE: ErrorCategory.VALIDATION,
        ErrorCode.ARTIFACT_UNSAFE_FILE: ErrorCategory.VALIDATION,
        ErrorCode.ARTIFACT_PUBLISH_FAILED: ErrorCategory.DEPENDENCY,
        ErrorCode.RATE_LIMIT_EXCEEDED: ErrorCategory.CAPACITY,
        ErrorCode.GATEWAY_OVERLOADED: ErrorCategory.CAPACITY,
        ErrorCode.SERVICE_DISABLED: ErrorCategory.DEPENDENCY,
        ErrorCode.BACKEND_UNAVAILABLE: ErrorCategory.DEPENDENCY,
        ErrorCode.USAGE_STORAGE_UNAVAILABLE: ErrorCategory.DEPENDENCY,
        ErrorCode.CONTROL_PLANE_UNAVAILABLE: ErrorCategory.DEPENDENCY,
        ErrorCode.INFERENCE_TIMEOUT: ErrorCategory.TIMEOUT,
        ErrorCode.RESYNC_REQUIRED: ErrorCategory.RESYNC,
    }
)


def classify_error(code: ErrorCode) -> ErrorCategory:
    """Return the single category assigned to a stable error code."""
    return ERROR_CATEGORIES[code]
