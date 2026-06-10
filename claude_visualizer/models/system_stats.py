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

# Volume-sampling throttle: resample at most every 5 seconds (disk_usage can be slow).
_VOLUME_SAMPLE_INTERVAL = 5.0

# Pseudo/transient filesystem types — never shown in the volume indicator.
# Network and FUSE remote types are included to prevent statvfs blocking on
# hung remote mounts (nfs, cifs, sshfs, etc.).
_FILTERED_FSTYPES: frozenset[str] = frozenset(
    {
        "squashfs",
        "tmpfs",
        "devtmpfs",
        "overlay",
        "proc",
        "sysfs",
        "cgroup",
        "cgroup2",
        "devpts",
        "securityfs",
        "pstore",
        "bpf",
        "autofs",
        "hugetlbfs",
        "mqueue",
        "debugfs",
        "tracefs",
        "fusectl",
        # Network filesystems — statvfs can block indefinitely on hung mounts
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb3",
        # FUSE remote mounts — same blocking risk
        "fuse.sshfs",
        "fuse.s3fs",
        "fuse.gvfsd-fuse",
        "fuse.portal",
        "gvfs",
        "fuse.rclone",
    }
)

# Mountpoint prefixes that are always pseudo/transient.
# NOTE: "/run/media" is intentionally NOT in this list — USB automounts
# under /run/media/<user>/<label> are real local data volumes and must be shown.
_FILTERED_MOUNT_PREFIXES: tuple[str, ...] = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/snap",
)


def _should_include_mount(mountpoint: str, fstype: str) -> bool:
    """Return True only for real local data volumes worth showing in the status bar.

    Evaluation order:
    1. Exclude by fstype first (network/fuse/pseudo types are always excluded).
    2. Allow /run/media/... mounts (USB automounts) BEFORE the prefix filter,
       because /run is otherwise excluded.
    3. Exclude by mountpoint prefix (pseudo/transient paths).
    4. Otherwise include.
    """
    # 1. Fstype filter — catches network mounts regardless of path
    if fstype in _FILTERED_FSTYPES:
        return False

    # 2. /run/media exception — USB automounts are real local volumes
    if mountpoint == "/run/media" or mountpoint.startswith("/run/media/"):
        return True

    # 3. Prefix filter — excludes pseudo/transient paths (including /run)
    if any(
        mountpoint == pfx or mountpoint.startswith(pfx + "/")
        for pfx in _FILTERED_MOUNT_PREFIXES
    ):
        return False

    # 4. Everything else is a real local mount
    return True


@dataclass(frozen=True)
class VolumeUsage:
    """Immutable snapshot of one mounted volume's free-space percentage."""

    mountpoint: str
    free_pct: float  # 0.0 – 100.0: percentage of space that is FREE


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
    volumes: tuple[VolumeUsage, ...] = ()


class SystemStatsModel:
    """Samples psutil counters each tick and returns a rate-computed snapshot.

    Rates are computed as a 5-second rolling average: the oldest sample still
    within the rolling window is used as the reference point, so the rate
    smooths over up to 5 seconds of history rather than a single tick delta.
    On the first tick (no previous sample) all rates are 0.0.

    Volume free-space is enumerated via psutil.disk_partitions on the first tick
    and then at most every _VOLUME_SAMPLE_INTERVAL seconds (throttled, because
    disk_usage can be slow on some mounts).  Pseudo/transient filesystems are
    excluded by fstype and mountpoint-prefix filters.
    """

    def __init__(self) -> None:
        self._samples: Deque[_IOSample] = deque(maxlen=_IO_SAMPLE_DEQUE_MAX)
        self._cached_volumes: tuple[VolumeUsage, ...] = ()
        self._last_vol_sample: float = (
            -_VOLUME_SAMPLE_INTERVAL
        )  # forces first-tick sample

    def _sample_volumes(self) -> tuple[VolumeUsage, ...]:
        """Enumerate real mounted volumes, filtering pseudo/transient mounts.

        Each disk_usage call is wrapped in a try/except so a single
        inaccessible mount never aborts the whole enumeration (MESSI #13).
        Returns a tuple sorted by mountpoint for stable ordering.
        """
        results: list[VolumeUsage] = []
        try:
            partitions = psutil.disk_partitions(all=False)
        except Exception:
            return ()  # can't enumerate — return empty, don't crash

        for part in partitions:
            if not _should_include_mount(part.mountpoint, part.fstype):
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                free_pct = 100.0 - usage.percent
                results.append(
                    VolumeUsage(mountpoint=part.mountpoint, free_pct=free_pct)
                )
            except (PermissionError, OSError, FileNotFoundError):
                # Skip mounts that can't be read — MESSI #13: explicit, not silent
                continue

        results.sort(key=lambda v: v.mountpoint)
        return tuple(results)

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

        # Throttled volume sampling: resample at most every _VOLUME_SAMPLE_INTERVAL s.
        # _last_vol_sample starts at -_VOLUME_SAMPLE_INTERVAL so the first tick always
        # samples immediately (no blank bar on first paint).
        if now - self._last_vol_sample >= _VOLUME_SAMPLE_INTERVAL:
            self._cached_volumes = self._sample_volumes()
            self._last_vol_sample = now

        return SystemStatsSnapshot(
            cpu_pct=cpu_pct,
            ram_pct=vm.percent,
            ram_free_bytes=vm.available,
            disk_read_bps=disk_read_bps,
            disk_write_bps=disk_write_bps,
            net_down_bps=net_down_bps,
            net_up_bps=net_up_bps,
            volumes=self._cached_volumes,
        )
