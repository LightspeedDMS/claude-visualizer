"""Tests for models/monitor_registry.py — the pluggable monitor registry.

The registry is pure (no Textual import). It scans a directory for *.py files,
imports each one, and calls tick(now) on each Monitor instance. Anti-mock:
all tests use real temporary .py files written into tmp_path.
"""

from __future__ import annotations

from pathlib import Path

from claude_visualizer.config import AppConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(monitors_dir: Path, **extra) -> AppConfig:
    """Build an AppConfig pointing at a custom monitors_dir."""
    return AppConfig(monitors_dir=monitors_dir, cache_path=None, **extra)


def _write_monitor(directory: Path, filename: str, return_value: str) -> Path:
    """Write a minimal Monitor class that returns a fixed string from tick()."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        f"class Monitor:\n"
        f"    def tick(self, now: float):\n"
        f"        return {return_value!r}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Purity guard
# ---------------------------------------------------------------------------


class TestPurity:
    def test_no_textual_import(self):
        import claude_visualizer.models.monitor_registry as reg_mod

        source = Path(reg_mod.__file__).read_text(encoding="utf-8")
        assert "import textual" not in source
        assert "from textual" not in source


# ---------------------------------------------------------------------------
# AC1 — alphabetical load order
# ---------------------------------------------------------------------------


class TestAlphabeticalLoad:
    def test_load_order_alphabetical(self, tmp_path: Path):
        """Monitors load and tick in filename-ascending order (AC1)."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        _write_monitor(tmp_path, "aaa_first.py", "aaa")
        _write_monitor(tmp_path, "mmm_middle.py", "mmm")
        _write_monitor(tmp_path, "zzz_last.py", "zzz")

        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        lines = registry.tick(0.0)
        assert lines == [
            "aaa",
            "mmm",
            "zzz",
        ], f"Expected alphabetical order ['aaa','mmm','zzz'], got {lines}"

    def test_underscore_prefixed_files_not_loaded(self, tmp_path: Path):
        """Files starting with _ (e.g. __init__.py, _helper.py) are skipped (AC1)."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        _write_monitor(tmp_path, "aaa_visible.py", "visible")
        (tmp_path / "__init__.py").write_text("# package", encoding="utf-8")
        (tmp_path / "_helper.py").write_text(
            'class Monitor:\n    def tick(self, now):\n        return "hidden"\n',
            encoding="utf-8",
        )

        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        lines = registry.tick(0.0)
        assert lines == ["visible"], f"Expected only ['visible'], got {lines}"


# ---------------------------------------------------------------------------
# Monitor class discovery
# ---------------------------------------------------------------------------


class TestMonitorDiscovery:
    def test_valid_monitor_loads_and_ticks(self, tmp_path: Path):
        """A file exposing a Monitor class is loaded and its tick() is called."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        _write_monitor(tmp_path, "mymon.py", "hello")
        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        lines = registry.tick(0.0)
        assert "hello" in lines

    def test_file_without_monitor_class_skipped_and_logged(self, tmp_path: Path):
        """A file without a Monitor class is skipped; error is in load_errors."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        (tmp_path / "nomonitor.py").write_text(
            "x = 42  # no Monitor class here\n", encoding="utf-8"
        )
        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        lines = registry.tick(0.0)
        assert lines == []
        assert any("nomonitor.py" in e for e in registry.load_errors)

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        """When monitors_dir does not exist, tick() returns [] with no errors."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        nonexistent = tmp_path / "does_not_exist"
        registry = MonitorRegistry(_make_config(nonexistent))
        registry.load()

        assert registry.tick(0.0) == []
        assert registry.load_errors == []


# ---------------------------------------------------------------------------
# AC2 — empty suppression (registry passes through, renderer suppresses)
# ---------------------------------------------------------------------------


class TestEmptySuppression:
    def test_monitor_returning_empty_string_passes_through(self, tmp_path: Path):
        """A monitor returning '' contributes to the registry result (renderer suppresses)."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        _write_monitor(tmp_path, "empty_mon.py", "")
        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        lines = registry.tick(0.0)
        # The registry itself includes the empty line in results;
        # suppression happens in render_monitor_bar (renderer's responsibility).
        assert lines == [""]

    def test_monitor_returning_rich_text_empty_passes_through(self, tmp_path: Path):
        """A monitor returning Text('') contributes an empty Text to results."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        (tmp_path / "rich_empty.py").write_text(
            "from rich.text import Text\n"
            "class Monitor:\n"
            "    def tick(self, now):\n"
            "        return Text('')\n",
            encoding="utf-8",
        )
        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        lines = registry.tick(0.0)
        assert len(lines) == 1
        from rich.text import Text as RichText

        assert isinstance(lines[0], RichText)
        assert lines[0].plain == ""


# ---------------------------------------------------------------------------
# AC5 — per-monitor error isolation
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    def test_raising_monitor_shows_warning_others_still_render(self, tmp_path: Path):
        """A tick() exception in one monitor produces a ⚠ slot; others continue (AC5)."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        _write_monitor(tmp_path, "aaa_good.py", "healthy")
        (tmp_path / "zzz_bad.py").write_text(
            "class Monitor:\n"
            "    def tick(self, now):\n"
            "        raise RuntimeError('boom')\n",
            encoding="utf-8",
        )

        cfg = _make_config(tmp_path, monitor_error_width=60)
        registry = MonitorRegistry(cfg)
        registry.load()

        lines = registry.tick(0.0)
        # The healthy monitor's line is present.
        assert "healthy" in lines
        # The bad monitor's slot is a ⚠ warning string.
        warning_lines = [ln for ln in lines if isinstance(ln, str) and "⚠" in ln]
        assert warning_lines, f"Expected a ⚠ warning line, got: {lines}"
        # The warning mentions the bad filename.
        assert any("zzz_bad.py" in ln for ln in warning_lines)
        # Warning is truncated to monitor_error_width.
        for wl in warning_lines:
            assert len(wl) <= 60, f"Warning exceeds error_width=60: {wl!r}"

    def test_raising_monitor_does_not_crash_tick(self, tmp_path: Path):
        """registry.tick() itself does NOT raise even if a monitor's tick() raises."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        (tmp_path / "crash.py").write_text(
            "class Monitor:\n"
            "    def tick(self, now):\n"
            "        raise ValueError('kaboom')\n",
            encoding="utf-8",
        )
        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        # Must not raise:
        result = registry.tick(0.0)
        assert isinstance(result, list)

    def test_bad_import_skipped_and_logged(self, tmp_path: Path):
        """A .py that raises at import time is skipped, recorded in load_errors."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        (tmp_path / "boom_import.py").write_text(
            "raise ImportError('broken at import')\n",
            encoding="utf-8",
        )
        _write_monitor(tmp_path, "zzz_ok.py", "still-works")

        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        lines = registry.tick(0.0)
        # The good monitor still produces output.
        assert "still-works" in lines
        # The bad file is recorded.
        assert any("boom_import.py" in e for e in registry.load_errors)


# ---------------------------------------------------------------------------
# Instance state persistence
# ---------------------------------------------------------------------------


class TestInstanceStatePersistence:
    def test_stateful_monitor_increments_across_ticks(self, tmp_path: Path):
        """A Monitor with instance state returns increasing values across ticks."""
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        (tmp_path / "counter.py").write_text(
            "class Monitor:\n"
            "    def __init__(self):\n"
            "        self._count = 0\n"
            "    def tick(self, now):\n"
            "        self._count += 1\n"
            "        return f'count={self._count}'\n",
            encoding="utf-8",
        )
        registry = MonitorRegistry(_make_config(tmp_path))
        registry.load()

        r1 = registry.tick(0.0)
        r2 = registry.tick(1.0)
        assert r1 == ["count=1"]
        assert r2 == ["count=2"]


# ---------------------------------------------------------------------------
# N2 — non-str/non-Text coercion (misbehaving plugin safety)
# ---------------------------------------------------------------------------


class TestN2NonStrCoercion:
    """N2: _filter_active_monitor_lines coerces non-str/non-Text truthy values
    to str so render_monitor_bar never raises TypeError on a bad plugin."""

    def test_non_str_non_text_coerced_to_str(self):
        """A truthy non-str/non-Text value (e.g. int 5) is coerced to str."""
        from claude_visualizer.ui.panels import _filter_active_monitor_lines

        result = _filter_active_monitor_lines([5])
        assert result == ["5"], f"Expected ['5'] from coercion of int 5, got {result!r}"

    def test_render_monitor_bar_non_str_no_raise(self):
        """render_monitor_bar does NOT raise TypeError when a monitor returns
        a truthy non-str/non-Text value (e.g. an integer or list)."""
        from claude_visualizer.ui.panels import render_monitor_bar

        # Must not raise:
        result = render_monitor_bar([5, 3.14, True])
        assert (
            "5" in result.plain
        ), f"Expected '5' in rendered output, got: {result.plain!r}"
