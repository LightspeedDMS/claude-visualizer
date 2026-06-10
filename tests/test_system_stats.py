"""Tests for the pure SystemStatsModel (no textual, psutil mocked as external boundary).

psutil is an external OS boundary (not application logic), so mocking it here is
legitimate — we want deterministic, fast unit tests that don't depend on the actual
system's CPU/RAM/disk/net state.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest


class TestVolumeUsageDataclass:
    """RED: VolumeUsage frozen dataclass + volumes field on SystemStatsSnapshot."""

    def test_volume_usage_has_mountpoint_and_free_pct(self):
        """VolumeUsage must have mountpoint str and free_pct float."""
        from claude_visualizer.models.system_stats import VolumeUsage

        v = VolumeUsage(mountpoint="/data", free_pct=73.5)
        assert v.mountpoint == "/data"
        assert v.free_pct == 73.5

    def test_volume_usage_is_frozen(self):
        """VolumeUsage must be immutable (frozen dataclass)."""
        from claude_visualizer.models.system_stats import VolumeUsage

        v = VolumeUsage(mountpoint="/mnt", free_pct=50.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            v.mountpoint = "/other"  # type: ignore[misc]

    def test_snapshot_volumes_defaults_to_empty_tuple(self):
        """SystemStatsSnapshot.volumes defaults to () — backward-compatible."""
        from claude_visualizer.models.system_stats import SystemStatsSnapshot

        snap = SystemStatsSnapshot(
            cpu_pct=10.0,
            ram_pct=50.0,
            ram_free_bytes=1_000_000,
            disk_read_bps=0.0,
            disk_write_bps=0.0,
            net_down_bps=0.0,
            net_up_bps=0.0,
        )
        assert snap.volumes == ()

    def test_snapshot_volumes_can_be_set(self):
        """SystemStatsSnapshot.volumes can carry VolumeUsage entries."""
        from claude_visualizer.models.system_stats import (
            SystemStatsSnapshot,
            VolumeUsage,
        )

        v = VolumeUsage(mountpoint="/home", free_pct=40.0)
        snap = SystemStatsSnapshot(
            cpu_pct=10.0,
            ram_pct=50.0,
            ram_free_bytes=1_000_000,
            disk_read_bps=0.0,
            disk_write_bps=0.0,
            net_down_bps=0.0,
            net_up_bps=0.0,
            volumes=(v,),
        )
        assert len(snap.volumes) == 1
        assert snap.volumes[0].mountpoint == "/home"


class TestVolumesSampling:
    """RED: SystemStatsModel.tick() enumerates and caches real mounted volumes."""

    def test_tick_returns_non_empty_volumes_on_real_machine(self):
        """tick() must return >=1 VolumeUsage entry on a real machine (real psutil)."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        model = SystemStatsModel()
        snap = model.tick(1.0)
        assert len(snap.volumes) >= 1, "Expected at least one mounted volume"
        for v in snap.volumes:
            assert v.mountpoint, "mountpoint must be non-empty"
            assert 0.0 <= v.free_pct <= 100.0, f"free_pct out of range: {v.free_pct}"

    def test_volumes_throttled_within_5s_and_resampled_after(self):
        """Two ticks within <5s reuse the same cached tuple; a tick >5s resamples.

        Uses a call counter patched onto psutil.disk_partitions to detect whether
        the model actually called psutil again (resampled) or returned the cache.
        psutil is an external OS boundary so patching it here is legitimate.
        """
        from unittest.mock import patch

        import psutil

        from claude_visualizer.models.system_stats import SystemStatsModel

        real_partitions = psutil.disk_partitions(all=False)
        if not real_partitions:
            pytest.skip("no disk partitions on this machine")

        call_count = [0]
        original_disk_partitions = psutil.disk_partitions

        def counting_disk_partitions(all: bool = False):  # noqa: A002
            call_count[0] += 1
            return original_disk_partitions(all=all)

        with patch(
            "claude_visualizer.models.system_stats.psutil.disk_partitions",
            side_effect=counting_disk_partitions,
        ):
            model = SystemStatsModel()
            snap1 = model.tick(0.0)  # first tick — samples volumes (call 1)
            snap2 = model.tick(2.0)  # within 5s — must use cache (no new call)
            assert snap1.volumes is snap2.volumes, "Cache must be reused within 5s"
            assert (
                call_count[0] == 1
            ), f"Expected 1 disk_partitions call within 5s, got {call_count[0]}"

            _snap3 = model.tick(6.0)  # >5s later — must resample (call 2)
            assert (
                call_count[0] == 2
            ), f"Expected 2 disk_partitions calls after 5s, got {call_count[0]}"

    def test_pseudo_mounts_filtered_from_volumes(self):
        """No /proc, /sys, /dev, /run, /snap mounts appear in volumes."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        _FILTERED_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap")

        model = SystemStatsModel()
        snap = model.tick(1.0)

        for v in snap.volumes:
            assert not any(
                v.mountpoint.startswith(pfx) for pfx in _FILTERED_PREFIXES
            ), f"Pseudo/transient mountpoint leaked through filter: {v.mountpoint}"


class TestShouldIncludeMount:
    """Deterministic unit tests for the pure _should_include_mount helper.

    Each test passes plain strings — no psutil, no mocking.
    """

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fn(mountpoint: str, fstype: str) -> bool:
        from claude_visualizer.models.system_stats import _should_include_mount

        return _should_include_mount(mountpoint, fstype)

    # ------------------------------------------------------------------ Finding A: network/fuse fstypes excluded
    def test_nfs_fstype_excluded(self):
        """nfs filesystem type must be excluded (prevents hung statvfs)."""
        assert self._fn("/mnt/nfs_share", "nfs") is False

    def test_nfs4_fstype_excluded(self):
        """nfs4 filesystem type must be excluded."""
        assert self._fn("/mnt/nfs4_share", "nfs4") is False

    def test_cifs_fstype_excluded(self):
        """cifs filesystem type must be excluded (Samba/Windows share)."""
        assert self._fn("/mnt/win_share", "cifs") is False

    def test_smbfs_fstype_excluded(self):
        """smbfs filesystem type must be excluded."""
        assert self._fn("/mnt/smb", "smbfs") is False

    def test_smb3_fstype_excluded(self):
        """smb3 filesystem type must be excluded."""
        assert self._fn("/mnt/smb3", "smb3") is False

    def test_fuse_sshfs_excluded(self):
        """fuse.sshfs filesystem type must be excluded (remote FUSE mount)."""
        assert self._fn("/mnt/remote", "fuse.sshfs") is False

    def test_fuse_s3fs_excluded(self):
        """fuse.s3fs must be excluded (S3 object storage FUSE)."""
        assert self._fn("/mnt/s3bucket", "fuse.s3fs") is False

    def test_fuse_gvfsd_gvfs_excluded(self):
        """fuse.gvfsd-fuse must be excluded (GNOME virtual filesystem)."""
        assert self._fn("/run/user/1000/gvfs", "fuse.gvfsd-fuse") is False

    def test_fuse_portal_excluded(self):
        """fuse.portal must be excluded."""
        assert self._fn("/run/user/1000/.portal", "fuse.portal") is False

    def test_gvfs_fstype_excluded(self):
        """gvfs filesystem type must be excluded."""
        assert self._fn("/run/user/1000/gvfs", "gvfs") is False

    def test_fuse_rclone_excluded(self):
        """fuse.rclone must be excluded (cloud storage FUSE)."""
        assert self._fn("/mnt/cloud", "fuse.rclone") is False

    # ------------------------------------------------------------------ Finding B: /run/media USB automounts INCLUDED
    def test_run_media_vfat_included(self):
        """USB automount at /run/media/<user>/<label> with vfat must be INCLUDED."""
        assert self._fn("/run/media/seba/USBDRIVE", "vfat") is True

    def test_run_media_ntfs_included(self):
        """USB automount at /run/media with ntfs must be INCLUDED."""
        assert self._fn("/run/media/alice/WinDisk", "ntfs") is True

    def test_run_media_ext4_included(self):
        """USB automount at /run/media with ext4 must be INCLUDED."""
        assert self._fn("/run/media/bob/BackupDisk", "ext4") is True

    def test_run_media_exfat_included(self):
        """USB automount at /run/media with exfat must be INCLUDED."""
        assert self._fn("/run/media/carol/Flash", "exfat") is True

    # ------------------------------------------------------------------ Normal real mounts included
    def test_root_ext4_included(self):
        """Root filesystem / with ext4 must be included."""
        assert self._fn("/", "ext4") is True

    def test_home_xfs_included(self):
        """/home with xfs must be included."""
        assert self._fn("/home", "xfs") is True

    def test_boot_efi_vfat_included(self):
        """/boot/efi with vfat must be included."""
        assert self._fn("/boot/efi", "vfat") is True

    def test_data_btrfs_included(self):
        """/data with btrfs must be included."""
        assert self._fn("/data", "btrfs") is True

    # ------------------------------------------------------------------ Pseudo/transient mounts excluded
    def test_proc_excluded(self):
        """/proc prefix must be excluded."""
        assert self._fn("/proc", "proc") is False

    def test_proc_subpath_excluded(self):
        """/proc/sys/fs must be excluded (sub-path)."""
        assert self._fn("/proc/sys/fs", "proc") is False

    def test_sys_excluded(self):
        """/sys prefix must be excluded."""
        assert self._fn("/sys", "sysfs") is False

    def test_dev_excluded(self):
        """/dev prefix must be excluded."""
        assert self._fn("/dev", "devtmpfs") is False

    def test_dev_shm_excluded(self):
        """/dev/shm must be excluded."""
        assert self._fn("/dev/shm", "tmpfs") is False

    def test_snap_excluded(self):
        """/snap/core/123 must be excluded."""
        assert self._fn("/snap/core/123", "squashfs") is False

    def test_run_lock_excluded(self):
        """/run/lock (system /run, non-media) must be excluded."""
        assert self._fn("/run/lock", "tmpfs") is False

    def test_run_user_gvfs_excluded(self):
        """/run/user/1000/gvfs must be excluded (system /run, gvfsd-fuse)."""
        assert self._fn("/run/user/1000/gvfs", "fuse.gvfsd-fuse") is False

    # ------------------------------------------------------------------ tmpfs always excluded (any path)
    def test_tmpfs_at_root_excluded(self):
        """tmpfs at / must be excluded (shouldn't happen but guard it)."""
        assert self._fn("/", "tmpfs") is False

    def test_tmpfs_at_mnt_excluded(self):
        """tmpfs at /mnt/ramdisk must be excluded."""
        assert self._fn("/mnt/ramdisk", "tmpfs") is False


class TestNoTextualImport:
    def test_no_textual_import(self):
        """models/system_stats must not import textual (pure model, UI-free)."""
        from pathlib import Path

        import claude_visualizer.models.system_stats as stats_mod

        source = Path(stats_mod.__file__).read_text(encoding="utf-8")
        assert "import textual" not in source
        assert "from textual" not in source


def _make_disk_counters(read_bytes: int = 0, write_bytes: int = 0) -> MagicMock:
    c = MagicMock()
    c.read_bytes = read_bytes
    c.write_bytes = write_bytes
    return c


def _make_net_counters(bytes_recv: int = 0, bytes_sent: int = 0) -> MagicMock:
    c = MagicMock()
    c.bytes_recv = bytes_recv
    c.bytes_sent = bytes_sent
    return c


def _make_vm(percent: float = 50.0, available: int = 4_000_000_000) -> MagicMock:
    vm = MagicMock()
    vm.percent = percent
    vm.available = available
    return vm


class TestFirstTickZeroRates:
    def test_first_tick_returns_zero_disk_rates(self):
        """First tick has no previous state → disk rates must be 0.0."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        with (
            patch("psutil.cpu_percent", return_value=10.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch(
                "psutil.disk_io_counters", return_value=_make_disk_counters(1024, 512)
            ),
            patch("psutil.net_io_counters", return_value=_make_net_counters(2048, 256)),
        ):
            model = SystemStatsModel()
            snap = model.tick(1.0)
            assert snap.disk_read_bps == 0.0
            assert snap.disk_write_bps == 0.0

    def test_first_tick_returns_zero_net_rates(self):
        """First tick has no previous state → net rates must be 0.0."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        with (
            patch("psutil.cpu_percent", return_value=10.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters()),
            patch("psutil.net_io_counters", return_value=_make_net_counters(999, 888)),
        ):
            model = SystemStatsModel()
            snap = model.tick(1.0)
            assert snap.net_down_bps == 0.0
            assert snap.net_up_bps == 0.0


class TestSecondTickComputesRate:
    def test_second_tick_computes_disk_read_rate(self):
        """Two ticks 1.0 s apart with disk read advancing 1024 bytes → 1024.0 bps."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        model = SystemStatsModel()
        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(0, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(0, 0)),
        ):
            model.tick(0.0)

        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(1024, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(0, 0)),
        ):
            snap = model.tick(1.0)

        assert snap.disk_read_bps == 1024.0

    def test_second_tick_computes_disk_write_rate(self):
        """Two ticks 1.0 s apart with disk write advancing 512 bytes → 512.0 bps."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        model = SystemStatsModel()
        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(0, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(0, 0)),
        ):
            model.tick(0.0)

        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(0, 512)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(0, 0)),
        ):
            snap = model.tick(1.0)

        assert snap.disk_write_bps == 512.0

    def test_second_tick_computes_net_rates(self):
        """Two ticks 2.0 s apart with net advancing 2000/1000 bytes → 1000/500 bps."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        model = SystemStatsModel()
        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(0, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(0, 0)),
        ):
            model.tick(0.0)

        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(0, 0)),
            patch(
                "psutil.net_io_counters", return_value=_make_net_counters(2000, 1000)
            ),
        ):
            snap = model.tick(2.0)

        assert snap.net_down_bps == 1000.0
        assert snap.net_up_bps == 500.0


class TestCounterResetClampsToZero:
    def test_counter_reset_disk_read_clamped(self):
        """If disk read counter goes DOWN (reset), rate must be 0.0 not negative."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        model = SystemStatsModel()
        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(5000, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(0, 0)),
        ):
            model.tick(0.0)

        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(100, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(0, 0)),
        ):
            snap = model.tick(1.0)

        assert snap.disk_read_bps == 0.0

    def test_counter_reset_net_down_clamped(self):
        """If net recv counter goes DOWN (reset), rate must be 0.0 not negative."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        model = SystemStatsModel()
        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(0, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(9999, 0)),
        ):
            model.tick(0.0)

        with (
            patch("psutil.cpu_percent", return_value=5.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters(0, 0)),
            patch("psutil.net_io_counters", return_value=_make_net_counters(10, 0)),
        ):
            snap = model.tick(1.0)

        assert snap.net_down_bps == 0.0


class TestCpuAndRamPassthrough:
    def test_cpu_pct_matches_psutil(self):
        """tick() returns psutil.cpu_percent verbatim in cpu_pct."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        with (
            patch("psutil.cpu_percent", return_value=73.5),
            patch("psutil.virtual_memory", return_value=_make_vm(percent=40.0)),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters()),
            patch("psutil.net_io_counters", return_value=_make_net_counters()),
        ):
            snap = SystemStatsModel().tick(1.0)

        assert snap.cpu_pct == 73.5

    def test_ram_pct_matches_psutil(self):
        """tick() returns virtual_memory().percent verbatim in ram_pct."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        with (
            patch("psutil.cpu_percent", return_value=10.0),
            patch("psutil.virtual_memory", return_value=_make_vm(percent=88.0)),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters()),
            patch("psutil.net_io_counters", return_value=_make_net_counters()),
        ):
            snap = SystemStatsModel().tick(1.0)

        assert snap.ram_pct == 88.0

    def test_ram_free_bytes_matches_psutil(self):
        """tick() returns virtual_memory().available verbatim in ram_free_bytes."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        with (
            patch("psutil.cpu_percent", return_value=10.0),
            patch(
                "psutil.virtual_memory", return_value=_make_vm(available=1_234_567_890)
            ),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters()),
            patch("psutil.net_io_counters", return_value=_make_net_counters()),
        ):
            snap = SystemStatsModel().tick(1.0)

        assert snap.ram_free_bytes == 1_234_567_890


class TestDiskIoNoneReturnsZero:
    def test_disk_io_none_returns_zero_rates(self):
        """When psutil.disk_io_counters() returns None, rates must be 0.0."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        with (
            patch("psutil.cpu_percent", return_value=10.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=None),
            patch("psutil.net_io_counters", return_value=_make_net_counters()),
        ):
            snap = SystemStatsModel().tick(1.0)

        assert snap.disk_read_bps == 0.0
        assert snap.disk_write_bps == 0.0

    def test_net_io_none_returns_zero_rates(self):
        """When psutil.net_io_counters() returns None, rates must be 0.0."""
        from claude_visualizer.models.system_stats import SystemStatsModel

        with (
            patch("psutil.cpu_percent", return_value=10.0),
            patch("psutil.virtual_memory", return_value=_make_vm()),
            patch("psutil.disk_io_counters", return_value=_make_disk_counters()),
            patch("psutil.net_io_counters", return_value=None),
        ):
            snap = SystemStatsModel().tick(1.0)

        assert snap.net_down_bps == 0.0
        assert snap.net_up_bps == 0.0
