"""Framework-independent security-audit classifications.

The values in this module are the single executable source for audit actions,
objects and outcomes.  Some actions have no HTTP producer yet; freezing their
classification now lets later milestones add producers without inventing a
second vocabulary.
"""

from __future__ import annotations

from enum import StrEnum


class AuditActorType(StrEnum):
    """The identity class responsible for an audited operation."""

    ANONYMOUS = "anonymous"
    USER = "user"
    SYSTEM = "system"


class AuditAction(StrEnum):
    """Supported audit action classifications."""

    LOGIN = "auth.login"
    LOGOUT = "auth.logout"
    PASSWORD_CHANGE = "account.password_change"
    ACCOUNT_CHANGE = "account.change"
    PERMISSION_CHANGE = "permission.change"
    AUTHORIZATION_CHECK = "permission.authorization_check"
    API_KEY_CHANGE = "api_key.change"
    MODEL_SOURCE_CREDENTIAL_CHANGE = "model_source.credential_change"
    DOWNLOAD_CHANGE = "download.change"
    ARTIFACT_DELETE = "model_artifact.delete"
    MODEL_INSTANCE_LIFECYCLE = "model_instance.lifecycle"
    AI_SERVICE_CHANGE = "ai_service.change"
    SYSTEM_CONFIG_CHANGE = "system_config.change"


class AuditObjectType(StrEnum):
    """Kinds of objects that may be named by an audit event."""

    USER = "user"
    SESSION = "session"
    PERMISSION = "permission"
    API_KEY = "api_key"
    MODEL_SOURCE = "model_source"
    DOWNLOAD_TASK = "download_task"
    MODEL_ARTIFACT = "model_artifact"
    MODEL_INSTANCE = "model_instance"
    AI_SERVICE = "ai_service"
    SYSTEM_CONFIG = "system_config"


class AuditResult(StrEnum):
    """Objective result of the attempted action, including denied attempts."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"


__all__ = [
    "AuditAction",
    "AuditActorType",
    "AuditObjectType",
    "AuditResult",
]
