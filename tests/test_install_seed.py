"""AC6 — install.sh monitor seeding: real bash subprocess, real temp HOME.

Anti-mock: uses real temp directories, runs the ACTUAL install.sh bash script
via subprocess with a --seed-monitors-only seam that exercises only the seed
logic (no venv/pip work triggered).  Verifies:

  - zzz_machine_stats.py is copied into <HOME>/.claude-visualizer/monitors/
  - A stale (different-content) copy is overwritten by re-seeding
  - A user file with a DIFFERENT name in that dir is left untouched
  - Re-running is idempotent (exit 0 both times, software monitor refreshed,
    user file still intact)
  - __init__.py is NOT copied into the user dir
  - After seeding, MonitorRegistry.tick() returns a non-empty CPU/RAM line
    (proves the bar would NOT be empty on a fresh install)

All assertions use real file-system state — no mocks anywhere.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Absolute path to the install.sh in the repo root (resolved at import time so
# tests are independent of cwd).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_SH = _REPO_ROOT / "install.sh"
# The shipped monitor file we expect to be seeded.
_SHIPPED_MONITOR = (
    _REPO_ROOT / "claude_visualizer" / "monitors" / "zzz_machine_stats.py"
)


def _run_seed(tmp_home: Path) -> subprocess.CompletedProcess:
    """Run install.sh --seed-monitors-only with the given HOME."""
    env = {**os.environ, "HOME": str(tmp_home)}
    return subprocess.run(
        ["bash", str(_INSTALL_SH), "--seed-monitors-only"],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )


class TestAC6InstallSeedMonitors:
    """AC6: install.sh seeds software monitors into ~/.claude-visualizer/monitors/.

    First group of tests: basic copy, overwrite, and user-file preservation.
    """

    def test_shipped_monitor_copied_to_home(self, tmp_path: Path):
        """zzz_machine_stats.py is copied into <HOME>/.claude-visualizer/monitors/."""
        tmp_home = tmp_path / "home"
        tmp_home.mkdir()

        result = _run_seed(tmp_home)
        assert result.returncode == 0, (
            f"--seed-monitors-only failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        seeded = tmp_home / ".claude-visualizer" / "monitors" / "zzz_machine_stats.py"
        assert seeded.exists(), f"Expected seeded file at {seeded}"
        # Content must match the shipped source exactly.
        assert (
            seeded.read_bytes() == _SHIPPED_MONITOR.read_bytes()
        ), "Seeded file content differs from shipped source"

    def test_stale_copy_overwritten(self, tmp_path: Path):
        """A stale (different-content) software monitor copy is overwritten."""
        tmp_home = tmp_path / "home"
        mon_dir = tmp_home / ".claude-visualizer" / "monitors"
        mon_dir.mkdir(parents=True)
        stale = mon_dir / "zzz_machine_stats.py"
        stale.write_text("# stale content — must be overwritten", encoding="utf-8")

        result = _run_seed(tmp_home)
        assert (
            result.returncode == 0
        ), f"Seed failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        assert (
            stale.read_bytes() == _SHIPPED_MONITOR.read_bytes()
        ), "Stale copy was NOT overwritten by seed"

    def test_user_file_preserved(self, tmp_path: Path):
        """A user-added file with a DIFFERENT name is left untouched."""
        tmp_home = tmp_path / "home"
        mon_dir = tmp_home / ".claude-visualizer" / "monitors"
        mon_dir.mkdir(parents=True)
        user_file = mon_dir / "my_custom_monitor.py"
        user_content = "# my precious custom monitor\nclass Monitor:\n    pass\n"
        user_file.write_text(user_content, encoding="utf-8")

        result = _run_seed(tmp_home)
        assert (
            result.returncode == 0
        ), f"Seed failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        assert user_file.exists(), "User file was deleted by seed"
        assert (
            user_file.read_text(encoding="utf-8") == user_content
        ), "User file content was modified by seed"


class TestAC6InstallSeedMonitorsExtra:
    """AC6: second group — idempotency, __init__.py exclusion, registry proof."""

    def test_idempotent_double_run(self, tmp_path: Path):
        """Running seed twice is idempotent: exit 0 both times, software monitor
        refreshed, user file still intact after both runs."""
        tmp_home = tmp_path / "home"
        mon_dir = tmp_home / ".claude-visualizer" / "monitors"
        mon_dir.mkdir(parents=True)
        user_file = mon_dir / "my_custom_monitor.py"
        user_content = "# user monitor\nclass Monitor:\n    pass\n"
        user_file.write_text(user_content, encoding="utf-8")

        r1 = _run_seed(tmp_home)
        assert r1.returncode == 0, f"First seed run failed:\n{r1.stdout}\n{r1.stderr}"

        r2 = _run_seed(tmp_home)
        assert r2.returncode == 0, f"Second seed run failed:\n{r2.stdout}\n{r2.stderr}"

        seeded = mon_dir / "zzz_machine_stats.py"
        assert (
            seeded.read_bytes() == _SHIPPED_MONITOR.read_bytes()
        ), "Software monitor wrong after second run"
        assert (
            user_file.read_text(encoding="utf-8") == user_content
        ), "User file changed after second run"

    def test_init_py_not_copied(self, tmp_path: Path):
        """__init__.py from the package monitors dir is NOT copied into user dir."""
        tmp_home = tmp_path / "home"
        tmp_home.mkdir()

        result = _run_seed(tmp_home)
        assert (
            result.returncode == 0
        ), f"Seed failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        init_in_user_dir = tmp_home / ".claude-visualizer" / "monitors" / "__init__.py"
        assert (
            not init_in_user_dir.exists()
        ), "__init__.py must NOT be copied into the user monitors dir"

    def test_registry_loads_seeded_monitor(self, tmp_path: Path):
        """After seeding, MonitorRegistry pointed at the user dir discovers the
        machine-stats monitor and tick() returns a non-empty CPU/RAM line.

        This is gate 7 (regression proof): on a fresh install the bar would NOT
        be empty after seeding.
        """
        from rich.text import Text

        from claude_visualizer.config import AppConfig
        from claude_visualizer.models.monitor_registry import MonitorRegistry

        tmp_home = tmp_path / "home"
        tmp_home.mkdir()

        result = _run_seed(tmp_home)
        assert (
            result.returncode == 0
        ), f"Seed failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"

        user_monitors_dir = tmp_home / ".claude-visualizer" / "monitors"
        cfg = AppConfig(monitors_dir=user_monitors_dir, cache_path=None)
        registry = MonitorRegistry(cfg)
        registry.load()

        import time

        lines = registry.tick(time.monotonic())

        non_empty = [
            (ln.plain if isinstance(ln, Text) else ln)
            for ln in lines
            if (isinstance(ln, Text) and ln.plain) or (isinstance(ln, str) and ln)
        ]
        assert non_empty, (
            f"tick() returned no non-empty lines after seeding — "
            f"bar would be empty for real users. "
            f"load_errors={registry.load_errors}"
        )
        combined = " ".join(non_empty)
        assert (
            "CPU" in combined
        ), f"Expected 'CPU' in tick() output after seeding, got: {combined[:200]}"
