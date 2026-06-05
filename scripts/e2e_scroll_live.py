#!/usr/bin/env python3
"""Live E2E driver for mouse-wheel scroll on pinned diffs.

Tests three scenarios end-to-end through the REAL app:
  1. Wheel scroll while NOT pinned → _pin_scroll stays 0 (scroll ignored)
  2. Pin then wheel scroll DOWN → _pin_scroll > 0, DisplayState.scroll_offset matches
  3. Re-pin (p key again) resets _pin_scroll to 0

Run:  TEXTUAL=headless .venv/bin/python scripts/e2e_scroll_live.py [screenshot.svg]
Exit: 0 on all assertions passing, non-zero otherwise.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from textual.events import MouseScrollDown

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


def _tall_edit(file_path: str, *, session_id: str, cwd: str,
               model: str = "claude-sonnet-4-6") -> str:
    """A 30-line Edit producing ~59 diff segments — tall enough to scroll."""
    old_lines = "\n".join(f"line_{i} = {i}" for i in range(30))
    new_lines = "\n".join(f"line_{i} = {i * 2}" for i in range(30))
    return json.dumps({
        "type": "assistant",
        "timestamp": "2024-03-01T12:00:00.000Z",
        "sessionId": session_id,
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                          "input": {"file_path": file_path,
                                    "old_string": old_lines,
                                    "new_string": new_lines}}],
        },
    })


def _check(cond: bool, label: str, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label} :: {evidence}")
    if not cond:
        raise AssertionError(f"{label}\n  evidence: {evidence}")


async def _pump_diff(pilot, diff: DiffPanel, clock: _ManualClock,
                     contains: str, tries: int = 80) -> str:
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
            min_pin_seconds=60.0,   # long pin so it never auto-releases during the test
        )
        app = VisualizerApp(cfg, now=clock)

        async with app.run_test(size=(120, 40)) as pilot:
            diff = pilot.app.query_one(DiffPanel)
            diff_queue = pilot.app._diff_queue

            # Seed a tall diff so there's room to scroll
            session = root / "scrolltest" / "s.jsonl"
            _append(session, _tall_edit(
                "/repo/bigfile.py",
                session_id="scrSESS0001", cwd="/home/dev/scrolltest",
            ))
            await _pump_diff(pilot, diff, clock, "bigfile.py")

            # --- Scenario 1: scroll while NOT pinned → no effect on _pin_scroll ---
            print("[1] Wheel scroll while NOT pinned → _pin_scroll stays 0")
            _check(
                diff_queue._pinned_path is None,
                "Pre-condition: diff is NOT pinned",
                f"_pinned_path={diff_queue._pinned_path!r}",
            )
            before = diff_queue._pin_scroll
            await pilot._post_mouse_events([MouseScrollDown], "#top-right")
            await pilot.pause()
            after = diff_queue._pin_scroll
            _check(
                after == 0,
                "Scroll while not pinned leaves _pin_scroll == 0",
                f"_pin_scroll before={before} after={after}",
            )

            # --- Scenario 2: pin then scroll → _pin_scroll advances ------------
            print("[2] Pin (p key) then 3x wheel-down → _pin_scroll > 0")
            await pilot.press("p")
            await pilot.pause()
            _check(
                diff_queue._pinned_path is not None,
                "p key pins the diff",
                f"_pinned_path={diff_queue._pinned_path!r}",
            )
            _check(
                diff_queue._pin_scroll == 0,
                "_pin_scroll starts at 0 on fresh pin",
                f"_pin_scroll={diff_queue._pin_scroll}",
            )

            for _ in range(3):
                await pilot._post_mouse_events([MouseScrollDown], "#top-right")
                await pilot.pause()

            scroll_val = diff_queue._pin_scroll
            _check(
                scroll_val > 0,
                "_pin_scroll > 0 after 3x wheel-down on pinned diff",
                f"_pin_scroll={scroll_val}",
            )

            # DisplayState.scroll_offset must match _pin_scroll
            viewport = pilot.app._diff_viewport_height()
            state = diff_queue.tick(clock(), viewport)
            _check(
                state is not None and state.scroll_offset == scroll_val,
                "DisplayState.scroll_offset matches _pin_scroll",
                f"scroll_offset={state.scroll_offset if state else None} "
                f"_pin_scroll={scroll_val}",
            )
            _check(
                state is not None and state.is_pinned,
                "DisplayState.is_pinned is True",
                f"is_pinned={state.is_pinned if state else None}",
            )

            # --- Scenario 3: re-pin resets _pin_scroll to 0 -------------------
            print("[3] Re-pin (p key again) resets _pin_scroll to 0")
            _check(
                diff_queue._pin_scroll > 0,
                "Sanity: _pin_scroll > 0 before re-pin",
                f"_pin_scroll={diff_queue._pin_scroll}",
            )
            await pilot.press("p")
            await pilot.pause()
            _check(
                diff_queue._pin_scroll == 0,
                "_pin_scroll resets to 0 on re-pin",
                f"_pin_scroll={diff_queue._pin_scroll}",
            )

            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            pilot.app.save_screenshot(str(screenshot_path))
            print(f"  Screenshot saved → {screenshot_path}")


def main() -> int:
    screenshot = (
        Path(sys.argv[1]) if len(sys.argv) > 1
        else Path(".tmp/e2e_scroll_live.svg")
    )
    print("=== E2E Scroll Feature Test ===")
    try:
        asyncio.run(run(screenshot))
        print("\nAll assertions PASSED")
        return 0
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
