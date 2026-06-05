"""Tests for the CLI entry point (__main__).

The runnable ``main()`` blocks on a full-screen Textual app, so it is not
invoked here; instead the *testable seam* — argument parsing into an
:class:`AppConfig` and app construction — is exercised directly.  This keeps
the entry point thin (it only parses args, builds config + app, and runs) while
the config-building logic is fully verified.
"""

from __future__ import annotations

from pathlib import Path

from claude_visualizer.__main__ import build_app, build_config
from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp


class TestBuildConfig:
    def test_projects_root_parsed(self):
        cfg = build_config(["--projects-root", "/tmp/fixture-root"])
        assert cfg.projects_root == Path("/tmp/fixture-root")

    def test_default_projects_root_is_home_claude_projects(self):
        cfg = build_config([])
        assert cfg.projects_root == Path.home() / ".claude" / "projects"

    def test_active_window_override(self):
        cfg = build_config(["--active-window", "45"])
        assert cfg.active_window_seconds == 45.0

    def test_max_active_files_override(self):
        cfg = build_config(["--max-active-files", "8"])
        assert cfg.max_active_files == 8

    def test_poll_interval_override(self):
        cfg = build_config(["--poll-interval", "0.25"])
        assert cfg.poll_interval_seconds == 0.25

    def test_discovery_interval_override(self):
        cfg = build_config(["--discovery-interval", "2.5"])
        assert cfg.discovery_interval_seconds == 2.5

    def test_mru_max_override(self):
        cfg = build_config(["--mru-max", "20"])
        assert cfg.mru_max == 20

    def test_command_feed_max_override(self):
        cfg = build_config(["--command-feed-max", "7"])
        assert cfg.command_feed_max == 7

    def test_returns_appconfig_instance(self):
        assert isinstance(build_config([]), AppConfig)

    def test_unset_optionals_keep_defaults(self):
        defaults = AppConfig()
        cfg = build_config(["--projects-root", "/x"])
        # Only projects_root changed; the rest stay at their defaults.
        assert cfg.active_window_seconds == defaults.active_window_seconds
        assert cfg.max_active_files == defaults.max_active_files
        assert cfg.poll_interval_seconds == defaults.poll_interval_seconds
        assert cfg.discovery_interval_seconds == defaults.discovery_interval_seconds
        assert cfg.mru_max == defaults.mru_max


class TestBuildApp:
    def test_build_app_returns_visualizer_app(self, tmp_path: Path):
        cfg = AppConfig(projects_root=tmp_path)
        app = build_app(cfg)
        assert isinstance(app, VisualizerApp)

    def test_build_app_uses_supplied_config(self, tmp_path: Path):
        cfg = AppConfig(projects_root=tmp_path, mru_max=7)
        app = build_app(cfg)
        # The app must run against exactly the config it was given.
        assert app._config is cfg
