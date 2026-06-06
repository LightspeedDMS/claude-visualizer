#!/usr/bin/env python3
"""Live E2E driver for the Diff panel (story #3, chunk B) — REAL app, no mocks.

This is *not* a unit test: it boots the ACTUAL :class:`VisualizerApp` through
Textual's real ``run_test()`` harness against a REAL temporary ``projects_root``,
appends REAL JSONL transcript lines (an Edit, a Write, and a thinking-turn whose
``thinking`` block shares a ``requestId`` with its ``tool_use``), and asserts the
LIVE Diff panel reflects each one end-to-end through the real pipeline:

  * Edit          → colour-mapped unified diff (red DEL above green ADD) + header
                    (short model · filename · origin) in the top-right panel.
  * Write         → whole-file additions with the ``whole-file write`` label
                    and all-green ``+`` lines (no fabricated removals).
  * Thinking-turn → the 🧠 glyph in the header (the engine flagged
                    ``used_thinking`` from the shared requestId).
  * AC9 sync      → the displayed file is highlighted (▶) in the MRU list.

A screenshot SVG of the running full-screen app is captured so the rendered
colours/header/highlight are inspectable as an artifact.  The diff queue's clock
is injected (a manual clock) so dwell/scroll/advance are deterministic with no
real sleeps; everything else is the genuine app loop + pipeline.

Run:  .venv/bin/python scripts/e2e_diff_panel_live.py [screenshot.svg]
Exit: 0 on every assertion passing, non-zero (with a printed reason) otherwise.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp
from claude_visualizer.ui.panels import (
    MRU_HIGHLIGHT_MARKER,
    THINKING_GLYPH,
    DiffPanel,
    MruFilesPanel,
)


class _ManualClock:
    """Test-controlled monotonic clock so the queue advances deterministically."""

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


def _tool_line(name: str, inp: dict, *, session_id: str, cwd: str,
               model: str = "claude-opus-4-8", request_id: str | None = None) -> str:
    entry: dict = {
        "type": "assistant",
        "timestamp": "2024-03-01T10:00:00.000Z",
        "sessionId": session_id,
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "tool_use", "id": "t1", "name": name, "input": inp}],
        },
    }
    if request_id is not None:
        entry["requestId"] = request_id
    return json.dumps(entry)


def _thinking_line(*, session_id: str, cwd: str, request_id: str,
                   model: str = "claude-opus-4-8") -> str:
    """A ``{type:"thinking"}`` entry that PRECEDES the tool_use, same requestId."""
    return json.dumps({
        "type": "assistant",
        "timestamp": "2024-03-01T10:00:01.000Z",
        "sessionId": session_id,
        "cwd": cwd,
        "requestId": request_id,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{
                "type": "thinking",
                "thinking": "Reasoning hard before this write.",
                "signature": "sig-e2e",
            }],
        },
    })


async def _pump_diff(pilot, contains: str, clock: _ManualClock, tries: int = 80) -> str:
    """Nudge the clock + pause until the Diff panel text contains ``contains``."""
    panel = pilot.app.query_one(DiffPanel)
    text = ""
    for _ in range(tries):
        clock.advance(0.05)
        await pilot.pause()
        text = panel.rendered_text()
        if contains in text:
            return text
    return text


def _span_styles(panel: DiffPanel) -> str:
    return " ".join(str(s.style) for s in panel._renderable.spans)


def _check(cond: bool, label: str, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label} :: {evidence}")
    if not cond:
        raise AssertionError(f"{label} — evidence: {evidence}")


async def run(screenshot_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "projects"
        clock = _ManualClock()
        # Real production-ish config, fixture root, fast discovery/poll so the
        # live pipeline surfaces appended lines promptly under the harness.
        cfg = AppConfig(
            projects_root=root,
            active_window_seconds=3600,
            discovery_interval_seconds=0.05,
            poll_interval_seconds=0.05,
            cache_path=None,
        )
        app = VisualizerApp(cfg, now=clock)

        async with app.run_test(size=(120, 40)) as pilot:
            diff = pilot.app.query_one(DiffPanel)
            mru = pilot.app.query_one(MruFilesPanel)

            # --- 1) EDIT: colored DEL + ADD diff + header in the top-right ----
            print("[1] EDIT — colored unified diff + header")
            edit_session = root / "calc-proj" / "edit.jsonl"
            _append(edit_session, _tool_line(
                "Edit",
                {"file_path": "/repo/calc.py",
                 "old_string": "return a - b",
                 "new_string": "return a + b"},
                session_id="editSESS1234", cwd="/home/dev/calculator",
            ))
            text = await _pump_diff(pilot, "calc.py", clock)
            styles = _span_styles(diff)
            _check("opus-4-8" in text, "AC3 header short model", repr("opus-4-8"))
            _check("calc.py" in text, "AC3 header filename", repr("calc.py"))
            _check("calculator" in text, "AC3 header project origin", repr("calculator"))
            _check("editSESS" in text, "AC3 header short session", repr("editSESS"))
            _check("- return a - b" in text, "AC1 DEL line present", repr("- return a - b"))
            _check("+ return a + b" in text, "AC1 ADD line present", repr("+ return a + b"))
            _check("#e06c75" in styles, "AC1 DEL coloured red", f"spans={styles!r}")
            _check("#98c379" in styles, "AC1 ADD coloured green", f"spans={styles!r}")

            # AC9: the displayed file is highlighted (▶) in the MRU list.
            mru_text = mru.rendered_text()
            hl_rows = [ln for ln in mru_text.splitlines()
                       if "/repo/calc.py" in ln and MRU_HIGHLIGHT_MARKER in ln]
            _check(bool(hl_rows), "AC9 displayed file highlighted in MRU",
                   f"row={hl_rows[0].strip()!r}" if hl_rows else f"mru={mru_text!r}")

            # --- 2) WRITE: whole-file additions + label -----------------------
            print("[2] WRITE — whole-file additions + label")
            write_session = root / "fresh-proj" / "write.jsonl"
            _append(write_session, _tool_line(
                "Write",
                {"file_path": "/repo/brand_new.py",
                 "content": "import os\nprint(os.getcwd())"},
                session_id="writeSESS999", cwd="/home/dev/fresh",
            ))
            text = await _pump_diff(pilot, "brand_new.py", clock)
            wstyles = _span_styles(diff)
            _check("whole-file write" in text, "AC2 whole-file write label",
                   repr("whole-file write"))
            _check("+ import os" in text, "AC2 first addition", repr("+ import os"))
            _check("+ print(os.getcwd())" in text, "AC2 second addition",
                   repr("+ print(os.getcwd())"))
            _check("#98c379" in wstyles, "AC2 additions coloured green",
                   f"spans={wstyles!r}")

            # --- 3) THINKING-TURN: 🧠 glyph in header -------------------------
            print("[3] THINKING-TURN — 🧠 glyph in header")
            think_session = root / "think-proj" / "think.jsonl"
            req = "req_e2e_THINK"
            tcwd = "/home/dev/thinkproj"
            _append(think_session, _thinking_line(
                session_id="thinkSESS01", cwd=tcwd, request_id=req))
            _append(think_session, _tool_line(
                "Write",
                {"file_path": "/repo/thoughtful.py", "content": "x = 1"},
                session_id="thinkSESS01", cwd=tcwd, request_id=req,
            ))
            text = await _pump_diff(pilot, "thoughtful.py", clock)
            _check(THINKING_GLYPH in text, "AC3/AC4 brain glyph in header",
                   f"glyph={THINKING_GLYPH!r} in header")
            _check("thoughtful.py" in text, "AC3 thinking file in header",
                   repr("thoughtful.py"))

            # AC9 again: the thinking file is now the highlighted MRU row.
            mru_text = mru.rendered_text()
            hl_rows = [ln for ln in mru_text.splitlines()
                       if "/repo/thoughtful.py" in ln and MRU_HIGHLIGHT_MARKER in ln]
            _check(bool(hl_rows), "AC9 highlight follows queue to thinking file",
                   f"row={hl_rows[0].strip()!r}" if hl_rows else f"mru={mru_text!r}")

            # --- screenshot of the running full-screen app --------------------
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            pilot.app.save_screenshot(str(screenshot_path))
            _check(screenshot_path.exists() and screenshot_path.stat().st_size > 0,
                   "screenshot SVG captured", f"path={screenshot_path}")

    print(f"\nALL LIVE E2E ASSERTIONS PASSED. Screenshot: {screenshot_path}")


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / ".tmp" / "diff_panel_live.svg"
    )
    try:
        asyncio.run(run(out))
    except AssertionError as exc:  # explicit, non-silent failure (MESSI #13)
        print(f"\nLIVE E2E FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
