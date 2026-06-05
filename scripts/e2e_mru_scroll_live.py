#!/usr/bin/env python3
"""Live E2E driver for mouse-wheel scroll on the MRU Files panel.

Tests three scenarios end-to-end through the REAL app:
  1. Scroll DOWN on MRU panel → top entry disappears from rendered text
  2. Scroll UP when already at top → _scroll_offset stays 0 (clamp)
  3. Scroll DOWN many times past end → _scroll_offset clamps at last row

Run:  TEXTUAL=headless .venv/bin/python scripts/e2e_mru_scroll_live.py [screenshot.svg]
Exit: 0 on all assertions passing, non-zero otherwise.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from textual.events import MouseScrollDown, MouseScrollUp

from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp
from claude_visualizer.ui.panels import MruFilesPanel


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


def _edit_line(file_path: str, n: int, *, session_id: str, cwd: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": f"2024-03-01T12:00:{n:02d}.000Z",
        "sessionId": session_id,
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "tool_use", "id": f"t{n}", "name": "Edit",
                          "input": {"file_path": file_path,
                                    "old_string": f"x = {n}",
                                    "new_string": f"x = {n + 1}"}}],
        },
    })


def _check(cond: bool, label: str, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label} :: {evidence}")
    if not cond:
        raise AssertionError(f"{label}\n  evidence: {evidence}")


async def _pump_mru(pilot, mru: MruFilesPanel, clock: _ManualClock,
                    contains: str, tries: int = 80) -> str:
    for _ in range(tries):
        clock.advance(0.05)
        await pilot.pause()
        text = mru.rendered_text()
        if contains in text:
            return text
    return mru.rendered_text()


async def run(screenshot_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "projects"
        clock = _ManualClock()
        cfg = AppConfig(
            projects_root=root,
            active_window_seconds=3600,
            discovery_interval_seconds=0.05,
            poll_interval_seconds=0.05,
        )
        app = VisualizerApp(cfg, now=clock)

        async with app.run_test(size=(120, 40)) as pilot:
            mru = pilot.app.query_one(MruFilesPanel)
            session = root / "mrutest" / "s.jsonl"

            # Seed 6 distinct files so there are enough rows to scroll
            files = [f"/repo/file_{i}.py" for i in range(6)]
            for i, fp in enumerate(files):
                _append(session, _edit_line(
                    fp, i,
                    session_id="mruSESS0001", cwd="/home/dev/mrutest",
                ))

            # Wait for the last (most-recent) file to appear in MRU
            await _pump_mru(pilot, mru, clock, "file_5.py")
            text_before = mru.rendered_text()
            # MRU is newest-first: file_5.py is at the top
            _check(
                "file_5.py" in text_before,
                "Setup: newest file visible at top before scroll",
                repr(text_before.splitlines()[2][:60] if len(text_before.splitlines()) > 2 else ""),
            )
            _check(
                mru._scroll_offset == 0,
                "Setup: scroll offset starts at 0",
                f"_scroll_offset={mru._scroll_offset}",
            )

            # --- Scenario 1: scroll DOWN once → top entry disappears ----------
            print("[1] Scroll DOWN once → top entry (file_5.py) hidden")
            await pilot._post_mouse_events([MouseScrollDown], "#mru-panel")
            await pilot.pause()
            offset_after_down = mru._scroll_offset
            _check(
                offset_after_down == 1,
                "Scroll down increments _scroll_offset to 1",
                f"_scroll_offset={offset_after_down}",
            )
            text_scrolled = mru.rendered_text()
            _check(
                "file_5.py" not in text_scrolled,
                "Top entry (file_5.py) no longer visible after scroll-down",
                f"text[:120]={text_scrolled[:120]!r}",
            )
            _check(
                "file_4.py" in text_scrolled,
                "Second entry (file_4.py) is now the topmost visible row",
                f"text[:120]={text_scrolled[:120]!r}",
            )

            # --- Scenario 2: scroll UP from offset 1 → offset back to 0 ------
            print("[2] Scroll UP → _scroll_offset back to 0, top entry reappears")
            await pilot._post_mouse_events([MouseScrollUp], "#mru-panel")
            await pilot.pause()
            offset_after_up = mru._scroll_offset
            _check(
                offset_after_up == 0,
                "Scroll up decrements _scroll_offset to 0",
                f"_scroll_offset={offset_after_up}",
            )
            text_restored = mru.rendered_text()
            _check(
                "file_5.py" in text_restored,
                "Top entry (file_5.py) reappears after scroll-up",
                f"text[:120]={text_restored[:120]!r}",
            )

            # --- Scenario 3: scroll UP at top → clamps at 0 ------------------
            print("[3] Scroll UP when already at top → _scroll_offset stays 0")
            _check(mru._scroll_offset == 0, "Pre-condition: at top", f"_scroll_offset={mru._scroll_offset}")
            await pilot._post_mouse_events([MouseScrollUp], "#mru-panel")
            await pilot.pause()
            _check(
                mru._scroll_offset == 0,
                "Scroll up at top clamps at 0",
                f"_scroll_offset={mru._scroll_offset}",
            )

            # --- Scenario 4: scroll DOWN past end → clamps at last row --------
            print("[4] Scroll DOWN 20× past 6 rows → clamps at last row (5)")
            for _ in range(20):
                await pilot._post_mouse_events([MouseScrollDown], "#mru-panel")
                await pilot.pause()
            max_offset = len(mru._rows) - 1
            _check(
                mru._scroll_offset == max_offset,
                f"Scroll down clamps at last row index ({max_offset})",
                f"_scroll_offset={mru._scroll_offset} rows={len(mru._rows)}",
            )

            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            pilot.app.save_screenshot(str(screenshot_path))
            print(f"  Screenshot saved → {screenshot_path}")


def main() -> int:
    screenshot = (
        Path(sys.argv[1]) if len(sys.argv) > 1
        else Path(".tmp/e2e_mru_scroll_live.svg")
    )
    print("=== E2E MRU Scroll Feature Test ===")
    try:
        asyncio.run(run(screenshot))
        print("\nAll assertions PASSED")
        return 0
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
