#!/usr/bin/env python3
"""Live E2E driver for the per-item TIMESTAMP enhancement — REAL app, no mocks.

Sibling of ``scripts/e2e_diff_panel_live.py`` and ``scripts/e2e_commands_feed_live.py``.
Boots the ACTUAL :class:`~claude_visualizer.ui.app.VisualizerApp` through
Textual's real ``run_test()`` harness against a REAL temporary
``projects_root``, appends REAL transcript JSONL with KNOWN ``timestamp`` fields
for:

- an **Edit** (transcript ``timestamp`` ``…T17:23:45Z``) — surfaces in the
  top-left MRU panel AND, once promoted, the top-right Diff panel header;
- a **Bash** command (transcript ``timestamp`` ``…T09:08:07Z``) — surfaces in
  the bottom Commands feed.

then asserts the rendered MRU row, the Diff header, AND the command row each
display the expected ``HH:MM:SS`` time (the UTC clock time is preserved verbatim
by the parser, so the assertion is deterministic on any machine).  Confirms the
bottom feed's pre-existing timestamp still works, then captures a screenshot SVG
showing timestamps in ALL THREE live panels.

Run headless:
    TEXTUAL=headless .venv/bin/python scripts/e2e_timestamps_live.py out.svg

Failures raise ``AssertionError`` (explicit, non-silent — MESSI #13) and exit 1.
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
    CommandsPanel,
    DiffPanel,
    MruFilesPanel,
)

# Known transcript timestamps → the exact HH:MM:SS each panel must render.
EDIT_TIMESTAMP = "2024-02-01T17:23:45.000Z"
EDIT_HHMMSS = "17:23:45"
BASH_TIMESTAMP = "2024-02-01T09:08:07.000Z"
BASH_HHMMSS = "09:08:07"


def _check(cond: bool, label: str, detail: str = "") -> None:
    """Explicit, non-silent assertion with a human-readable label (MESSI #13)."""
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise AssertionError(label + (f" ({detail})" if detail else ""))


def _tool_line(
    name, inp, *, timestamp, session_id, cwd, model="claude-opus-4-8"
) -> str:
    """One real assistant transcript line carrying a tool_use + a ``timestamp``."""
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": timestamp,
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {"type": "tool_use", "id": "t1", "name": name, "input": inp}
                ],
            },
        }
    )


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


async def _pump(panel, contains: str, pilot, clock, tries: int = 80) -> str:
    """Pause (bounded) until the panel text contains ``contains``; return it.

    Nudges the injected clock a little each iteration so the diff queue can
    promote a freshly-recorded file (it only becomes displayable on a LATER
    tick).  Bounded (anti-unbounded, MESSI #14): at most ``tries`` iterations.
    """
    text = ""
    for _ in range(tries):
        clock.advance(0.05)
        await pilot.pause()
        text = panel.rendered_text()
        if contains in text:
            return text
    return text


class _ManualClock:
    """Test-controlled monotonic clock injected into the app's diff queue."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


async def run(screenshot_path: Path) -> None:
    root = Path(tempfile.mkdtemp(prefix="cv-e2e-ts-")) / "projects"
    session = root / "tsproj" / "session-ts.jsonl"

    clock = _ManualClock()
    cfg = AppConfig(
        projects_root=root,
        active_window_seconds=3600,
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
    )
    app = VisualizerApp(cfg, now=clock)

    async with app.run_test(size=(140, 45)) as pilot:
        mru = pilot.app.query_one(MruFilesPanel)
        diff = pilot.app.query_one(DiffPanel)
        commands = pilot.app.query_one(CommandsPanel)

        # --- An Edit with a KNOWN timestamp → MRU row + Diff header ---
        print("Edit with known transcript timestamp -> MRU row + Diff header")
        _append(
            session,
            _tool_line(
                "Edit",
                {
                    "file_path": "/repo/calc.py",
                    "old_string": "return a - b",
                    "new_string": "return a + b",
                },
                timestamp=EDIT_TIMESTAMP,
                session_id="tsSESS012345",
                cwd="/home/dev/tsproj",
            ),
        )
        mru_text = await _pump(mru, "/repo/calc.py", pilot, clock)
        _check("/repo/calc.py" in mru_text, "MRU shows the edited file")
        mru_rows = [
            ln for ln in mru_text.splitlines()
            if "/repo/calc.py" in ln and EDIT_HHMMSS in ln
        ]
        _check(
            bool(mru_rows),
            f"MRU row displays the timestamp {EDIT_HHMMSS}",
            f"MRU text was:\n{mru_text}",
        )

        diff_text = await _pump(diff, "calc.py", pilot, clock)
        _check("calc.py" in diff_text, "Diff header shows the displayed file")
        _check(
            EDIT_HHMMSS in diff_text,
            f"Diff header displays the timestamp {EDIT_HHMMSS}",
            f"Diff text was:\n{diff_text}",
        )

        # --- A Bash command with a KNOWN timestamp → Commands feed row ---
        print("Bash command with known transcript timestamp -> Commands row")
        _append(
            session,
            _tool_line(
                "Bash",
                {"command": "pytest -q tests/"},
                timestamp=BASH_TIMESTAMP,
                session_id="tsSESS012345",
                cwd="/home/dev/tsproj",
            ),
        )
        cmd_text = await _pump(commands, "pytest -q tests/", pilot, clock)
        _check("pytest -q tests/" in cmd_text, "Commands feed shows the command")
        cmd_rows = [
            ln for ln in cmd_text.splitlines()
            if "pytest -q tests/" in ln and BASH_HHMMSS in ln
        ]
        _check(
            bool(cmd_rows),
            f"Command row displays the timestamp {BASH_HHMMSS} "
            "(the feed's existing timestamp still works)",
            f"Commands text was:\n{cmd_text}",
        )

        # --- Capture the screenshot showing timestamps in ALL THREE panels ---
        app.save_screenshot(str(screenshot_path))
        _check(
            screenshot_path.exists() and screenshot_path.stat().st_size > 0,
            "screenshot SVG captured",
            f"path={screenshot_path}",
        )
        svg = screenshot_path.read_text(encoding="utf-8")
        _check(EDIT_HHMMSS in svg, f"screenshot SVG contains MRU/Diff time {EDIT_HHMMSS}")
        _check(BASH_HHMMSS in svg, f"screenshot SVG contains Commands time {BASH_HHMMSS}")

        print("\n--- MRU panel (live) ---")
        print(mru.rendered_text())
        print("--- Diff panel (live) ---")
        print(diff.rendered_text())
        print("--- Commands panel (live) ---")
        print(commands.rendered_text())

    print(
        f"\nALL LIVE E2E ASSERTIONS PASSED (timestamps in all 3 panels). "
        f"Screenshot: {screenshot_path}"
    )


def main() -> int:
    out = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else (Path(__file__).resolve().parent.parent / ".tmp" / "timestamps_live.svg")
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
