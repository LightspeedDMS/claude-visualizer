"""Pure system-resource stats model for the status bar.

No textual import — this is a pure model that can be unit-tested in isolation.
Calls psutil at each tick to read CPU, RAM, disk IO, and network IO counters,
computes a 5-second rolling-average byte rate, and returns an immutable snapshot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import psutil

_ROLLING_WINDOW_SECONDS = 5.0
_IO_SAMPLE_DEQUE_MAX = 20  # MESSI #14: bounded deque


@dataclass
class _IOSample:
    time: float
    disk_read: int
    disk_write: int
    net_recv: int
    net_sent: int


@dataclass(frozen=True)
class SystemStatsSnapshot:
    """Immutable snapshot of one system-stats sample."""

    cpu_pct: float
    ram_pct: float
    ram_free_bytes: int
    disk_read_bps: float
    disk_write_bps: float
    net_down_bps: float
    net_up_bps: float


class SystemStatsModel:
    """Samples psutil counters each tick and returns a rate-computed snapshot.

    Rates are computed as a 5-second rolling average: the oldest sample still
    within the rolling window is used as the reference point, so the rate
    smooths over up to 5 seconds of history rather than a single tick delta.
    On the first tick (no previous sample) all rates are 0.0.
    """

    def __init__(self) -> None:
        self._samples: Deque[_IOSample] = deque(maxlen=_IO_SAMPLE_DEQUE_MAX)

    def tick(self, now: float) -> SystemStatsSnapshot:
        """Sample psutil and return a new snapshot with computed rolling rates.

        On first call (no previous state) all rates are 0.0.  On subsequent
        calls rates use the oldest sample within the 5-second window as the
        reference, clamped to 0.0 so a counter reset never produces a negative
        rate.
        """
        cpu_pct: float = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        disk = psutil.disk_io_counters(perdisk=False)
        net = psutil.net_io_counters(pernic=False)

        cur = _IOSample(
            time=now,
            disk_read=disk.read_bytes if disk is not None else 0,
            disk_write=disk.write_bytes if disk is not None else 0,
            net_recv=net.bytes_recv if net is not None else 0,
            net_sent=net.bytes_sent if net is not None else 0,
        )
        self._samples.append(cur)

        # Find the oldest sample still within the rolling window (deque is
        # oldest-first).  Rate = (current - oldest_in_window) / elapsed.
        window_start = now - _ROLLING_WINDOW_SECONDS
        ref: Optional[_IOSample] = None
        for s in self._samples:
            if s is cur:
                break
            if s.time >= window_start:
                ref = s
                break

        disk_read_bps = 0.0
        disk_write_bps = 0.0
        net_down_bps = 0.0
        net_up_bps = 0.0

        if ref is not None:
            elapsed = max(now - ref.time, 0.001)
            disk_read_bps = max(0.0, (cur.disk_read - ref.disk_read) / elapsed)
            disk_write_bps = max(0.0, (cur.disk_write - ref.disk_write) / elapsed)
            net_down_bps = max(0.0, (cur.net_recv - ref.net_recv) / elapsed)
            net_up_bps = max(0.0, (cur.net_sent - ref.net_sent) / elapsed)

        return SystemStatsSnapshot(
            cpu_pct=cpu_pct,
            ram_pct=vm.percent,
            ram_free_bytes=vm.available,
            disk_read_bps=disk_read_bps,
            disk_write_bps=disk_write_bps,
            net_down_bps=net_down_bps,
            net_up_bps=net_up_bps,
        )
