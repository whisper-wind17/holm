"""SGLang HTTP Runtime Adapter sharing the canonical OpenAI-compatible boundary."""

import re

from holm_domain import ErrorCode
from holm_protocols import PlatformError

from holm_gateway.vllm_adapter import VllmRuntimeAdapter, _context_error


class SglangRuntimeAdapter(VllmRuntimeAdapter):
    """Translate SGLang OpenAI-compatible responses into canonical events.

    The certified M3a surface uses the same HTTP/JSON/SSE contract as vLLM.
    Runtime-specific launch flags remain in ``runtime-compat.yaml``.
    """

    __slots__ = ()

    @staticmethod
    def _context_error(value: object) -> PlatformError:
        # SGLang 0.5.13 uses a top-level error object with param=null.
        if isinstance(value, dict) and value.get("object") == "error":
            message = value.get("message")
            if isinstance(message, str):
                match = re.match(
                    r"Requested token count exceeds the model's maximum context length "
                    r"of ([0-9]+) tokens\."
                    r"|The input \([0-9]+ tokens\) is longer than the "
                    r"model's context length \(([0-9]+) tokens\)\.",
                    message,
                )
                if match:
                    return _context_error(next(part for part in match.groups() if part))
        return PlatformError(ErrorCode.INVALID_REQUEST)


__all__ = ["SglangRuntimeAdapter"]
