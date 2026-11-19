"""Spec §22.1: what a sandbox used, worked out from the samples the Docker daemon gives while it runs."""

from typing import Any

from harnessgateway.domain.outcome import Measured
from harnessgateway.drivers.meter import measured


def sample(cpu_ns: int, memory: int, **more: int) -> dict[str, Any]:
    return {"cpu_stats": {"cpu_usage": {"total_usage": cpu_ns}}, "memory_stats": {"usage": memory, **more}}


def test_cpu_time_is_the_running_total_and_memory_is_the_most_seen() -> None:
    samples = [sample(1_000_000_000, 50_000_000), sample(2_500_000_000, 300_000_000),
               sample(2_600_000_000, 80_000_000)]
    assert measured(samples) == Measured(cpu_seconds=2.6, memory_peak_bytes=300_000_000)


def test_a_daemon_that_keeps_the_peak_itself_is_believed_over_the_samples() -> None:
    # Between two samples a second apart, memory can rise and fall unseen. Where the kernel keeps the peak, use it.
    samples = [sample(1_000_000_000, 50_000_000, max_usage=900_000_000),
               sample(2_000_000_000, 60_000_000, max_usage=900_000_000)]
    assert measured(samples) == Measured(cpu_seconds=2.0, memory_peak_bytes=900_000_000)


def test_the_empty_samples_of_a_container_that_has_ended_change_nothing() -> None:
    samples = [sample(3_000_000_000, 70_000_000), sample(0, 0), {"cpu_stats": {}, "memory_stats": {}}]
    assert measured(samples) == Measured(cpu_seconds=3.0, memory_peak_bytes=70_000_000)


def test_with_no_sample_nothing_was_measured() -> None:
    assert measured([]) is None
