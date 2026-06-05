"""Tests for AppConfig dataclass — all tunables, defaults, and overrides."""

from pathlib import Path

import pytest

from claude_visualizer.config import AppConfig


class TestAppConfigDefaults:
    """AC10: config.py exposes configurable projects_root with sensible defaults."""

    def test_default_projects_root(self):
        cfg = AppConfig()
        assert cfg.projects_root == Path.home() / ".claude" / "projects"

    def test_default_active_window_seconds(self):
        cfg = AppConfig()
        assert cfg.active_window_seconds == 120

    def test_default_max_active_files(self):
        cfg = AppConfig()
        assert cfg.max_active_files == 64

    def test_default_discovery_interval_seconds(self):
        cfg = AppConfig()
        assert cfg.discovery_interval_seconds == 5.0

    def test_default_poll_interval_seconds(self):
        cfg = AppConfig()
        assert cfg.poll_interval_seconds == 0.3

    def test_default_seed_tail_bytes(self):
        cfg = AppConfig()
        assert cfg.seed_tail_bytes == 65536

    def test_default_max_line_bytes(self):
        cfg = AppConfig()
        assert cfg.max_line_bytes == 1_000_000

    def test_default_mru_max(self):
        cfg = AppConfig()
        assert cfg.mru_max == 50

    def test_default_diff_max_lines(self):
        cfg = AppConfig()
        assert cfg.diff_max_lines == 500

    def test_default_min_dwell_seconds(self):
        cfg = AppConfig()
        assert cfg.min_dwell_seconds == 3.0

    def test_default_max_dwell_seconds(self):
        cfg = AppConfig()
        assert cfg.max_dwell_seconds == 12.0

    def test_default_diff_queue_max(self):
        cfg = AppConfig()
        assert cfg.diff_queue_max == 32

    def test_default_diff_refresh_seconds(self):
        cfg = AppConfig()
        assert cfg.diff_refresh_seconds == 0.2

    def test_default_requestid_map_max(self):
        cfg = AppConfig()
        assert cfg.requestid_map_max == 256

    def test_default_command_feed_max(self):
        cfg = AppConfig()
        assert cfg.command_feed_max == 100

    def test_dwell_bounds_are_ordered(self):
        """MIN_DWELL must not exceed MAX_DWELL or the dwell window is empty."""
        cfg = AppConfig()
        assert cfg.min_dwell_seconds <= cfg.max_dwell_seconds


class TestAppConfigOverrides:
    """Overrides work when constructed with explicit values."""

    def test_override_projects_root(self):
        p = Path("/tmp/test-projects")
        cfg = AppConfig(projects_root=p)
        assert cfg.projects_root == p

    def test_override_active_window_seconds(self):
        cfg = AppConfig(active_window_seconds=60)
        assert cfg.active_window_seconds == 60

    def test_override_max_active_files(self):
        cfg = AppConfig(max_active_files=10)
        assert cfg.max_active_files == 10

    def test_override_seed_tail_bytes(self):
        cfg = AppConfig(seed_tail_bytes=4096)
        assert cfg.seed_tail_bytes == 4096

    def test_override_mru_max(self):
        cfg = AppConfig(mru_max=100)
        assert cfg.mru_max == 100

    def test_override_max_line_bytes(self):
        cfg = AppConfig(max_line_bytes=500_000)
        assert cfg.max_line_bytes == 500_000

    def test_override_diff_max_lines(self):
        cfg = AppConfig(diff_max_lines=120)
        assert cfg.diff_max_lines == 120

    def test_override_min_dwell_seconds(self):
        cfg = AppConfig(min_dwell_seconds=1.0)
        assert cfg.min_dwell_seconds == 1.0

    def test_override_max_dwell_seconds(self):
        cfg = AppConfig(max_dwell_seconds=20.0)
        assert cfg.max_dwell_seconds == 20.0

    def test_override_diff_queue_max(self):
        cfg = AppConfig(diff_queue_max=8)
        assert cfg.diff_queue_max == 8

    def test_override_diff_refresh_seconds(self):
        cfg = AppConfig(diff_refresh_seconds=0.05)
        assert cfg.diff_refresh_seconds == 0.05

    def test_override_requestid_map_max(self):
        cfg = AppConfig(requestid_map_max=16)
        assert cfg.requestid_map_max == 16

    def test_override_command_feed_max(self):
        cfg = AppConfig(command_feed_max=200)
        assert cfg.command_feed_max == 200


class TestAppConfigImmutability:
    """AppConfig is a frozen dataclass — mutation raises."""

    def test_frozen_dataclass(self):
        cfg = AppConfig()
        with pytest.raises(Exception):
            cfg.mru_max = 99  # type: ignore
