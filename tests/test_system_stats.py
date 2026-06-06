"""Tests for the pure SystemStatsModel (no textual, psutil mocked as external boundary).

psutil is an external OS boundary (not application logic), so mocking it here is
legitimate — we want deterministic, fast unit tests that don't depend on the actual
system's CPU/RAM/disk/net state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


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
