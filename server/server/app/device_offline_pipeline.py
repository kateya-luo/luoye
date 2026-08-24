"""Pure planning helpers for recorder-card offline ASR slices."""
from __future__ import annotations

from typing import Iterable


def ready_asr_windows(*, total_ms: int, total_bytes: int, sample_rate: int,
                      bytes_per_sample: int, window_ms: int,
                      covered: Iterable[tuple[int, int]], sealed: bool
                      ) -> list[tuple[int, int, int]]:
    """Return fully verified windows as ``(index, start_ms, end_ms)``.

    Full fixed-size windows may run while the public upload is still in progress.
    A short tail is returned only after `/complete` seals the recording.
    """
    if total_ms <= 0 or total_bytes <= 0 or window_ms <= 0:
        return []
    merged = list(covered)
    output: list[tuple[int, int, int]] = []
    start_ms = 0
    index = 0
    while start_ms < total_ms:
        end_ms = min(total_ms, start_ms + window_ms)
        short_tail = end_ms - start_ms < window_ms
        start_byte = start_ms * sample_rate * bytes_per_sample // 1000
        end_byte = min(total_bytes, end_ms * sample_rate * bytes_per_sample // 1000)
        verified = any(start_byte >= left and end_byte <= right for left, right in merged)
        if verified and (sealed or not short_tail):
            output.append((index, start_ms, end_ms))
        start_ms = end_ms
        index += 1
    return output
