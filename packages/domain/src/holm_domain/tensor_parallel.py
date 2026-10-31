"""Framework-independent tensor-parallel model partition checks."""

from enum import StrEnum


class TensorParallelPartitionResult(StrEnum):
    """Three-state result that never conflates missing metadata with success."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNAVAILABLE = "unavailable"


def validate_tensor_parallel_partition(
    attention_heads: int | None,
    kv_heads: int | None,
    tensor_parallel_size: int,
) -> TensorParallelPartitionResult:
    """Judge all known head counts while preserving incomplete knowledge."""

    if tensor_parallel_size <= 0:
        return TensorParallelPartitionResult.INCOMPATIBLE
    known = (attention_heads, kv_heads)
    if any(value is not None and value % tensor_parallel_size != 0 for value in known):
        return TensorParallelPartitionResult.INCOMPATIBLE
    if any(value is None for value in known):
        return TensorParallelPartitionResult.UNAVAILABLE
    return TensorParallelPartitionResult.COMPATIBLE


__all__ = ["TensorParallelPartitionResult", "validate_tensor_parallel_partition"]
