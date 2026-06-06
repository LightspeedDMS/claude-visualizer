#!/usr/bin/env python3
"""Live E2E driver for the Commands feed (story #4) — REAL app, no mocks.

Sibling of ``scripts/e2e_diff_panel_live.py``.  Boots the ACTUAL
:class:`~claude_visualizer.ui.app.VisualizerApp` through Textual's real
``run_test()`` harness against a REAL temporary ``projects_root``, appends REAL
``Bash`` tool_use JSONL lines from **two sessions and a subagent** (plus an
``Edit`` so the MRU/Diff panels are populated), and asserts the live BOTTOM
Commands panel satisfies AC1–AC5.  Captures a screenshot SVG showing all THREE
live panels.

Run headless:
    TEXTUAL=headless .venv/bin/python scripts/e2e_commands_feed_live.py out.svg

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
    SUBAGENT_MARKER,
    TRUNCATION_ELLIPSIS,
)


def _check(cond: bool, label: str, detail: str = "") -> None:
    """Explicit, non-silent assertion with a human-readable label (MESSI #13)."""
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise AssertionError(label + (f" ({detail})" if detail else ""))


def _tool_line(name, inp, *, session_id, cwd, model="claude-opus-4-8") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-01-15T10:00:00.000Z",
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


async def _pump(panel, contains: str, pilot, tries: int = 60) -> str:
    """Pause (bounded) until the panel text contains ``contains``; return it."""
    text = ""
    for _ in range(tries):
        await pilot.pause()
        text = panel.rendered_text()
        if contains in text:
            return text
    return text


async def _settle(pilot, pauses: int = 10) -> None:
    for _ in range(pauses):
        await pilot.pause()


async def run(screenshot_path: Path) -> None:
    root = Path(tempfile.mkdtemp(prefix="cv-e2e-cmd-")) / "projects"
    sess1 = root / "alpha" / "session-one.jsonl"
    sess2 = root / "beta" / "session-two.jsonl"
    sub = root / "alpha" / "sess" / "subagents" / "agent-aaa.jsonl"

    # command_feed_max=3 so the overflow assertion (AC4) is exercised live.
    cfg = AppConfig(
        projects_root=root,
        active_window_seconds=3600,
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
        command_feed_max=3,
        cache_path=None,
    )
    app = VisualizerApp(cfg)

    async with app.run_test(size=(140, 45)) as pilot:
        commands = pilot.app.query_one(CommandsPanel)
        mru = pilot.app.query_one(MruFilesPanel)
        diff = pilot.app.query_one(DiffPanel)

        # Populate MRU + Diff so the screenshot shows all three live panels.
        _append(
            sess1,
            _tool_line(
                "Edit",
                {
                    "file_path": "/repo/calc.py",
                    "old_string": "return a - b",
                    "new_string": "return a + b",
                },
                session_id="alphaSESS01",
                cwd="/home/dev/alpha",
            ),
        )

        # --- AC1: two sessions + a subagent, newest-on-top, origin tags ---
        print("AC1: Bash commands from any session (incl. subagents), newest-on-top")
        _append(
            sess1,
            _tool_line(
                "Bash", {"command": "pytest -q tests/"},
                session_id="alphaSESS01", cwd="/home/dev/alpha",
            ),
        )
        await _pump(commands, "pytest -q tests/", pilot)
        _append(
            sub,
            _tool_line(
                "Bash", {"command": "ruff check ."},
                session_id="subSESS0002", cwd="/home/dev/alpha",
            ),
        )
        await _pump(commands, "ruff check .", pilot)
        _append(
            sess2,
            _tool_line(
                "Bash", {"command": "git status"},
                session_id="betaSESS0003", cwd="/home/dev/beta",
            ),
        )
        text = await _pump(commands, "git status", pilot)

        for needle in ("pytest -q tests/", "ruff check .", "git status"):
            _check(needle in text, f"command present: {needle!r}")
        _check(
            text.index("git status")
            < text.index("ruff check .")
            < text.index("pytest -q tests/"),
            "newest-on-top across two sessions + subagent",
        )
        _check("alpha" in text and "beta" in text, "project origin tags present")
        _check("betaSESS" in text, "short session id present")
        sub_rows = [
            ln for ln in text.splitlines()
            if "ruff check ." in ln and SUBAGENT_MARKER in ln
        ]
        _check(bool(sub_rows), "subagent row carries the sub marker")

        # --- AC2: same command twice -> BOTH rows (no dedup) ---
        print("AC2: repeated identical commands are each shown (no dedup)")
        for _ in range(2):
            _append(
                sess2,
                _tool_line(
                    "Bash", {"command": "make build"},
                    session_id="betaSESS0003", cwd="/home/dev/beta",
                ),
            )
        await _pump(commands, "make build", pilot)
        await _settle(pilot)
        dup_text = commands.rendered_text()
        _check(
            dup_text.count("make build") == 2,
            "two identical 'make build' rows (no dedup)",
            f"count={dup_text.count('make build')}",
        )

        # --- AC3: long command truncated with the ellipsis ---
        print("AC3: each row shows command truncated to panel width + time + origin")
        long_cmd = "echo " + "Z" * 300
        _append(
            sess2,
            _tool_line(
                "Bash", {"command": long_cmd},
                session_id="betaSESS0003", cwd="/home/dev/beta",
            ),
        )
        trunc_text = await _pump(commands, "echo ZZZ", pilot)
        _check(TRUNCATION_ELLIPSIS in trunc_text, "long command shows ellipsis")
        _check("Z" * 300 not in trunc_text, "long command was truncated (not full)")

        # --- AC4: overflow past command_feed_max=3 -> oldest fall off ---
        print("AC4: at capacity, oldest entries scroll off the bottom")
        for i in range(5):
            _append(
                sess2,
                _tool_line(
                    "Bash", {"command": f"overflow-step-{i}"},
                    session_id="betaSESS0003", cwd="/home/dev/beta",
                ),
            )
        await _pump(commands, "overflow-step-4", pilot)
        await _settle(pilot, 12)
        ovr_text = commands.rendered_text()
        step_rows = [ln for ln in ovr_text.splitlines() if "overflow-step-" in ln]
        _check(len(step_rows) == 3, "feed capped at command_feed_max=3",
               f"rows={len(step_rows)}")
        for gone in ("overflow-step-0", "overflow-step-1"):
            _check(gone not in ovr_text, f"oldest scrolled off: {gone}")
        for kept in ("overflow-step-2", "overflow-step-3", "overflow-step-4"):
            _check(kept in ovr_text, f"newest retained: {kept}")
        _check(
            ovr_text.index("overflow-step-4") < ovr_text.index("overflow-step-2"),
            "capped feed still newest-on-top",
        )

        # --- AC5: live — repopulate a readable feed for the screenshot ---
        print("AC5: the feed updates live as commands execute")
        for path, cmd, sid, cwd in (
            (sess1, "pytest -q tests/", "alphaSESS01", "/home/dev/alpha"),
            (sub, "ruff check .", "subSESS0002", "/home/dev/alpha"),
            (sess2, "docker compose up -d", "betaSESS0003", "/home/dev/beta"),
        ):
            _append(path, _tool_line("Bash", {"command": cmd},
                                     session_id=sid, cwd=cwd))
        live_text = await _pump(commands, "docker compose up -d", pilot)
        _check("docker compose up -d" in live_text,
               "newly-executed command surfaced live")
        # Let the diff queue promote the recorded Edit for the screenshot.
        await _settle(pilot, 20)

        app.save_screenshot(str(screenshot_path))
        _check(screenshot_path.exists() and screenshot_path.stat().st_size > 0,
               "screenshot SVG captured", f"path={screenshot_path}")

        print("\n--- Commands panel (live) ---")
        print(commands.rendered_text())
        print("--- MRU panel (live) ---")
        print(mru.rendered_text())
        print("--- Diff panel (live) ---")
        print(diff.rendered_text())

    print(f"\nALL LIVE E2E ASSERTIONS PASSED (AC1-AC5). Screenshot: {screenshot_path}")


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / ".tmp" / "commands_feed_live.svg"
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
