#!/usr/bin/env python3
"""Live E2E driver for the pluggable Monitor Bar (story #6) — REAL app, no mocks.

Sibling of ``scripts/e2e_commands_feed_live.py``.  Boots the ACTUAL
:class:`~claude_visualizer.ui.app.VisualizerApp` through Textual's real
``run_test()`` harness against a REAL temporary ``projects_root`` and a REAL
temporary ``monitors_dir`` seeded with controlled monitor files.  Asserts the
live BOTTOM monitor bar satisfies AC1–AC5.  Captures a screenshot SVG.

Run headless:
    TEXTUAL=headless .venv/bin/python scripts/e2e_monitor_bar_live.py out.svg

Failures raise ``AssertionError`` (explicit, non-silent — MESSI #13) and exit 1.

Acceptance criteria verified:
    AC1 — monitors load and tick in alphabetical filename order.
    AC2 — a monitor returning "" is suppressed (no blank row rendered).
    AC3 — bar height == number of non-suppressed (active) monitors.
    AC4 — bundled zzz_machine_stats.py produces CPU / RAM / Disk / Net content.
    AC5 — a monitor whose tick() raises shows a ⚠ <filename>: … warning row.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp
from claude_visualizer.ui.panels import MonitorBar


def _check(cond: bool, label: str, detail: str = "") -> None:
    """Explicit, non-silent assertion with a human-readable label (MESSI #13)."""
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise AssertionError(label + (f" ({detail})" if detail else ""))


def _write_monitor(directory: Path, filename: str, body: str) -> None:
    """Write a monitor .py file into directory (creates dir if needed)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")


async def _pump(bar: MonitorBar, contains: str, pilot, tries: int = 80) -> str:
    """Pause (bounded) until the bar's rendered_text contains ``contains``."""
    text = ""
    for _ in range(tries):
        await pilot.pause()
        text = bar.rendered_text()
        if contains in text:
            return text
    return text


async def _settle(pilot, pauses: int = 15) -> None:
    for _ in range(pauses):
        await pilot.pause()


async def run(screenshot_path: Path) -> None:
    projects_root = Path(tempfile.mkdtemp(prefix="cv-e2e-mon-")) / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    monitors_dir = Path(tempfile.mkdtemp(prefix="cv-e2e-mon-dir-"))

    # --- Seed monitors_dir with controlled monitor files --------------------

    # AC1 — three monitors in NON-alphabetical write order; assertion checks
    # they tick in alphabetical order.
    _write_monitor(
        monitors_dir,
        "ccc_third.py",
        "class Monitor:\n    def tick(self, now):\n        return 'monitor-C'\n",
    )
    _write_monitor(
        monitors_dir,
        "aaa_first.py",
        "class Monitor:\n    def tick(self, now):\n        return 'monitor-A'\n",
    )

    # AC2 — a monitor that returns "" should be suppressed (no blank row).
    _write_monitor(
        monitors_dir,
        "bbb_suppress.py",
        "class Monitor:\n    def tick(self, now):\n        return ''\n",
    )

    # AC5 — a monitor whose tick() raises shows a ⚠ warning row.
    _write_monitor(
        monitors_dir,
        "ddd_raiser.py",
        "class Monitor:\n"
        "    def tick(self, now):\n"
        "        raise RuntimeError('e2e-boom')\n",
    )

    # AC4 — zzz_machine_stats is bundled in the package monitors/ dir.  The
    # registry loads monitors_dir (our temp dir) only; to include the bundled
    # monitor we copy it into our temp dir so it sorts last (zzz_).
    from claude_visualizer.monitors import zzz_machine_stats

    bundled_src = Path(zzz_machine_stats.__file__)
    (monitors_dir / bundled_src.name).write_bytes(bundled_src.read_bytes())

    cfg = AppConfig(
        projects_root=projects_root,
        active_window_seconds=3600,
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
        monitors_dir=monitors_dir,
        monitor_refresh_seconds=0.1,
        cache_path=None,
    )
    app = VisualizerApp(cfg)

    async with app.run_test(size=(160, 50)) as pilot:
        bar = pilot.app.query_one(MonitorBar)

        # Wait for monitor-A (first alphabetically) to appear in the bar.
        text = await _pump(bar, "monitor-A", pilot)

        # --- AC1: alphabetical load order -----------------------------------
        print("AC1: monitors load and tick in alphabetical filename order")
        _check("monitor-A" in text, "aaa_first.py content present", repr(text[:80]))
        _check("monitor-C" in text, "ccc_third.py content present", repr(text[:80]))
        # A must appear before C in the rendered text.
        _check(
            text.index("monitor-A") < text.index("monitor-C"),
            "monitor-A precedes monitor-C (alphabetical tick order)",
        )

        # --- AC2: empty-returning monitor is suppressed ---------------------
        print("AC2: monitor returning '' produces no blank row")
        # bbb_suppress returns "" — it must NOT appear as a blank line.
        # The rendered_text should not contain a blank-only line between A and C.
        lines_between = [
            ln
            for ln in text.splitlines()
            if not ln.strip()  # blank lines
        ]
        _check(
            len(lines_between) == 0,
            "no blank rows from suppressed monitor",
            f"blank lines={lines_between!r}",
        )

        # --- AC3: bar height == active monitor count ------------------------
        print("AC3: bar height equals the number of non-suppressed monitors")
        # Active monitors: aaa_first (A), ccc_third (C), ddd_raiser (warning),
        # zzz_machine_stats (stats line) = 4 active rows.
        # bbb_suppress is the only suppressed one.
        # bar.display must be True (at least one active monitor).
        _check(bar.display, "MonitorBar.display is True when monitors are active")
        # Pump until all 4 active rows are present (stats line may lag slightly).
        for _ in range(20):
            await pilot.pause()
            steady_text = bar.rendered_text()
            rendered_lines = [ln for ln in steady_text.splitlines() if ln.strip()]
            if len(rendered_lines) == 4:
                break
        _check(
            len(rendered_lines) == 4,
            "bar height equals active monitor count (4)",
            f"got {len(rendered_lines)}: {rendered_lines}",
        )

        # --- AC4: zzz_machine_stats produces CPU/RAM/Disk/Net content -------
        print("AC4: bundled zzz_machine_stats.py produces CPU/RAM/Disk/Net content")
        # Wait for stats content to appear.
        stats_text = await _pump(bar, "CPU", pilot)
        _check("CPU" in stats_text, "CPU label present in stats bar")
        _check("RAM" in stats_text, "RAM label present in stats bar")
        _check("Disk" in stats_text, "Disk label present in stats bar")
        _check("Net" in stats_text, "Net label present in stats bar")

        # --- AC5: raising monitor shows ⚠ warning row -----------------------
        print("AC5: monitor whose tick() raises shows ⚠ <filename>: … warning")
        warn_text = await _pump(bar, "⚠", pilot)
        _check("⚠" in warn_text, "warning glyph present for raising monitor")
        _check(
            "ddd_raiser.py" in warn_text,
            "raising monitor filename in warning row",
            repr(warn_text[:120]),
        )
        _check(
            "e2e-boom" in warn_text,
            "exception message in warning row",
            repr(warn_text[:120]),
        )

        await _settle(pilot, 20)

        app.save_screenshot(str(screenshot_path))
        _check(
            screenshot_path.exists() and screenshot_path.stat().st_size > 0,
            "screenshot SVG captured",
            f"path={screenshot_path}",
        )

        print("\n--- Monitor bar (live) ---")
        print(bar.rendered_text())

    print(
        f"\nALL LIVE E2E ASSERTIONS PASSED (AC1-AC5). Screenshot: {screenshot_path}"
    )


def main() -> int:
    out = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else (
            Path(__file__).resolve().parent.parent / ".tmp" / "monitor_bar_live.svg"
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(run(out))
    except AssertionError as exc:  # explicit, non-silent failure (MESSI #13)
        print(f"\nLIVE E2E FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
