#!/usr/bin/env python3
"""Live E2E driver for the click-to-pin and keyboard-pin features.

Tests both pin mechanisms end-to-end through the REAL app:
  1. mouse_down on an MRU row → diff panel shows "📌 pinned"
  2. `p` key on highlighted file → diff panel shows "📌 pinned"

Run:  TEXTUAL=headless .venv/bin/python scripts/e2e_pin_live.py [screenshot.svg]
Exit: 0 on all assertions passing, non-zero otherwise.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp
from claude_visualizer.ui.panels import DiffPanel, MruFilesPanel


class _ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


def _edit_line(file_path: str, old: str, new: str, *, session_id: str,
               cwd: str, model: str = "claude-sonnet-4-6") -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": "2024-03-01T17:00:00.000Z",
        "sessionId": session_id,
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                          "input": {"file_path": file_path,
                                    "old_string": old, "new_string": new}}],
        },
    })


def _check(cond: bool, label: str, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label} :: {evidence}")
    if not cond:
        raise AssertionError(f"{label}\n  evidence: {evidence}")


async def _pump(pilot, mru: MruFilesPanel, contains: str, tries: int = 80) -> str:
    """Wait until the MRU panel text contains ``contains``."""
    for _ in range(tries):
        await pilot.pause()
        text = mru.rendered_text()
        if contains in text:
            return text
    return mru.rendered_text()


async def _pump_diff(pilot, diff: DiffPanel, clock: _ManualClock,
                     contains: str, tries: int = 80) -> str:
    """Advance clock + pause until Diff panel text contains ``contains``."""
    for _ in range(tries):
        clock.advance(0.05)
        await pilot.pause()
        text = diff.rendered_text()
        if contains in text:
            return text
    return diff.rendered_text()


async def run(screenshot_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "projects"
        clock = _ManualClock()
        cfg = AppConfig(
            projects_root=root,
            active_window_seconds=3600,
            discovery_interval_seconds=0.05,
            poll_interval_seconds=0.05,
            min_pin_seconds=10.0,
        )
        app = VisualizerApp(cfg, now=clock)

        async with app.run_test(size=(120, 40)) as pilot:
            diff = pilot.app.query_one(DiffPanel)
            mru = pilot.app.query_one(MruFilesPanel)

            # --- Seed two files into the pipeline so MRU has rows -----------
            session = root / "pintest" / "s.jsonl"
            _append(session, _edit_line(
                "/repo/alpha.py", "x = 1", "x = 2",
                session_id="pinSESS0001", cwd="/home/dev/pintest",
            ))
            _append(session, _edit_line(
                "/repo/beta.py", "y = 1", "y = 2",
                session_id="pinSESS0001", cwd="/home/dev/pintest",
            ))

            # Wait for beta.py to appear in MRU (it's the most recent)
            await _pump(pilot, mru, "beta.py")
            # Diff panel must show beta.py (most recent)
            diff_text = await _pump_diff(pilot, diff, clock, "beta.py")
            _check("beta.py" in diff_text, "Setup: diff shows most-recent file",
                   repr(diff_text[:80]))

            # --- Test 1: mouse_down on row 2 (first MRU entry = beta.py) ----
            print("[1] mouse_down on MRU row 2 (offset y=2) → pin beta.py")
            await pilot.mouse_down("#mru-panel", offset=(10, 2))
            await pilot.pause()
            diff_text = diff.rendered_text()
            _check("📌 pinned" in diff_text,
                   "mouse_down pins the clicked row",
                   f"diff_title={diff_text.splitlines()[0]!r}")

            # --- Advance clock past pin so queue unfreezes ------------------
            clock.advance(15.0)   # past min_pin_seconds=10
            # Record a new event so the unpin condition (new_event_since_pin) is met
            _append(session, _edit_line(
                "/repo/gamma.py", "z = 1", "z = 2",
                session_id="pinSESS0001", cwd="/home/dev/pintest",
            ))
            diff_text = await _pump_diff(pilot, diff, clock, "gamma.py")
            _check("📌 pinned" not in diff_text,
                   "Pin released after min_pin_seconds + new event",
                   f"diff_title={diff_text.splitlines()[0]!r}")

            # --- Test 2: `p` key pins currently displayed file ---------------
            print("[2] `p` key → pin currently displayed file")
            # gamma.py is now shown; press p to pin it
            await pilot.press("p")
            await pilot.pause()
            diff_text = diff.rendered_text()
            _check("📌 pinned" in diff_text,
                   "p key pins the currently displayed diff",
                   f"diff_title={diff_text.splitlines()[0]!r}")
            _check("gamma.py" in diff_text,
                   "p key pins gamma.py (the highlighted file)",
                   f"diff_header={diff_text.splitlines()[1] if len(diff_text.splitlines()) > 1 else ''!r}")

            # --- Screenshot ---------------------------------------------------
            pilot.app.save_screenshot(str(screenshot_path))
            print(f"  Screenshot saved → {screenshot_path}")


def main() -> int:
    import asyncio
    screenshot = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("e2e_pin_live.svg")
    print("=== E2E Pin Feature Test ===")
    try:
        asyncio.run(run(screenshot))
        print("\nAll assertions PASSED")
        return 0
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
