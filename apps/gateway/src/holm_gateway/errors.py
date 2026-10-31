"""Protocol-independent public Gateway status and message policy."""

from typing import Final

from holm_domain import ErrorCode
from holm_protocols import PlatformError
from pydantic import JsonValue

_ERRORS: Final[dict[ErrorCode, tuple[int, str]]] = {
    ErrorCode.INVALID_REQUEST: (400, "请求格式无效，本次调用未执行，请检查参数后重试"),
    ErrorCode.INVALID_API_KEY: (401, "API 密钥无效，本次调用未执行，请检查密钥后重试"),
    ErrorCode.API_KEY_EXPIRED: (401, "API 密钥已过期，本次调用未执行，请创建新密钥后重试"),
    ErrorCode.SERVICE_NOT_AUTHORIZED: (
        403,
        "API 密钥无权调用该 AI 服务，本次调用未执行，请检查密钥范围",
    ),
    ErrorCode.MODEL_NOT_FOUND: (404, "请求的模型不存在，本次调用未执行，请检查 API 模型名"),
    ErrorCode.RATE_LIMIT_EXCEEDED: (429, "请求频率超出限制，本次调用未执行，请稍后重试"),
    ErrorCode.SERVICE_DISABLED: (503, "AI 服务已停用，本次调用未执行，请联系管理员启用服务"),
    ErrorCode.BACKEND_UNAVAILABLE: (503, "AI 服务暂不可用，本次调用未完成，请稍后重试"),
    ErrorCode.GATEWAY_OVERLOADED: (503, "平台当前请求过多，本次调用未执行，请稍后重试"),
    ErrorCode.USAGE_STORAGE_UNAVAILABLE: (
        503,
        "调用记录暂时无法可靠保存，本次调用未执行，请稍后重试",
    ),
    ErrorCode.CONTROL_PLANE_UNAVAILABLE: (
        503,
        "服务路由信息已失效，本次调用未执行，请稍后重试",
    ),
    ErrorCode.INFERENCE_TIMEOUT: (504, "AI 服务响应超时，本次调用未完成，请稍后重试"),
}


def describe_error(error: PlatformError) -> tuple[int, str, dict[str, JsonValue]]:
    if error.code not in _ERRORS:
        raise ValueError("unsupported Gateway error code")
    status_code, message = _ERRORS[error.code]
    if error.code is ErrorCode.INVALID_REQUEST:
        if error.reason == "context_length_exceeded":
            limit = (
                f"（上限 {error.max_context_tokens} tokens）"
                if error.max_context_tokens is not None
                else ""
            )
            message = (
                f"请求超出模型上下文长度{limit}，本次调用未执行，请缩短输入或减小 max_tokens 后重试"
            )
        elif error.unsupported_fields:
            message = (
                f"请求包含不支持的字段：{', '.join(error.unsupported_fields)}，"
                "本次调用未执行，请去掉这些字段后重试"
            )
    limit_details: dict[str, JsonValue] = {}
    if error.code is ErrorCode.RATE_LIMIT_EXCEEDED:
        if error.limit_type not in {"concurrent", "per_minute"} or error.limit is None:
            raise ValueError("rate limit error requires policy details")
        limit_details = {"limit_type": error.limit_type, "limit": error.limit}
        if error.limit_type == "per_minute":
            message = f"已达到该服务的每分钟请求上限（{error.limit} 次），请稍后重试"
        else:
            reason = "排队等待超时" if error.queue_timed_out else "排队已满"
            message = f"已达到该服务的同时请求上限（{error.limit} 个），{reason}，请稍后重试"
    return status_code, message, limit_details
