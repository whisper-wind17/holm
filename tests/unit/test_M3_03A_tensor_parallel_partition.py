"""M3-03A tensor-parallel partition domain rules."""

import pytest
from holm_domain import (
    TensorParallelPartitionResult,
    validate_tensor_parallel_partition,
)


@pytest.mark.parametrize(
    ("attention_heads", "kv_heads", "tp", "expected"),
    [
        (16, 4, 4, TensorParallelPartitionResult.COMPATIBLE),
        (15, 4, 4, TensorParallelPartitionResult.INCOMPATIBLE),
        (16, 3, 4, TensorParallelPartitionResult.INCOMPATIBLE),
        (16, None, 4, TensorParallelPartitionResult.UNAVAILABLE),
        (None, None, 4, TensorParallelPartitionResult.UNAVAILABLE),
    ],
)
def test_M3_03A_TC_GPU_03_partition_result_is_three_state(
    attention_heads: int | None,
    kv_heads: int | None,
    tp: int,
    expected: TensorParallelPartitionResult,
) -> None:
    assert validate_tensor_parallel_partition(attention_heads, kv_heads, tp) is expected
