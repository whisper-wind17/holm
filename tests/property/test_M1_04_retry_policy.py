"""M1-04 property checks for bounded monotonic retry delay."""

from holm_domain import RetryPolicy


def test_M1_04_retry_delay_is_positive_monotonic_and_capped_for_many_bounds() -> None:
    for max_attempts in range(1, 33):
        for base_delay in (0.01, 0.5, 1.0, 7.5):
            cap = base_delay * 17
            policy = RetryPolicy(
                max_attempts=max_attempts,
                base_delay_seconds=base_delay,
                max_delay_seconds=cap,
            )
            delays = [policy.delay_after(attempt) for attempt in range(1, max_attempts + 1)]
            assert all(0 < delay <= cap for delay in delays)
            assert delays == sorted(delays)
