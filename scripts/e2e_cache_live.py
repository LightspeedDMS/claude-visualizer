#!/usr/bin/env python3
"""Live E2E driver for the SQLite persistence cache.

Tests three scenarios end-to-end through the REAL app:
  1. First launch creates the cache DB and persists file + command events
  2. Restart (new app, same cache_path, empty projects_root) restores
     the previous session's files into the MRU panel from cache alone
  3. Eviction: recording N > max events keeps only the newest max entries

Run:  TEXTUAL=headless .venv/bin/python scripts/e2e_cache_live.py [screenshot.svg]
Exit: 0 on all assertions passing, non-zero otherwise.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from claude_visualizer.cache import CacheDB
from claude_visualizer.config import AppConfig
from claude_visualizer.events import CommandEvent, FileModifiedEvent, FileOp
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


def _edit_jsonl(file_path: str, n: int, *, session_id: str, cwd: str) -> str:
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


def _bash_jsonl(command: str, *, session_id: str, cwd: str) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": "2024-03-01T12:00:59.000Z",
        "sessionId": session_id,
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "tool_use", "id": "tcmd", "name": "Bash",
                          "input": {"command": command}}],
        },
    })


def _make_file_event(file_path: str, n: int = 0) -> FileModifiedEvent:
    return FileModifiedEvent(
        file_path=file_path,
        op=FileOp.EDIT,
        old_string=f"x = {n}",
        new_string=f"x = {n + 1}",
        replace_all=False,
        full_content=None,
        model="claude-sonnet-4-6",
        session_id="evictSESS0001",
        project_tag="evictproj",
        is_subagent=False,
        used_thinking=False,
        thinking_chars=0,
        ts=datetime(2024, 3, 1, 12, 0, n % 60, tzinfo=timezone.utc),
        source_path="/tmp/evict.jsonl",
    )


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
    with tempfile.TemporaryDirectory() as cache_td:
        cache_path = Path(cache_td) / "test_cache.db"

        # ── Scenario 1: first launch creates DB and persists events ──────────
        print("[1] First launch → DB created, events persisted to cache")
        with tempfile.TemporaryDirectory() as td1:
            root1 = Path(td1) / "projects"
            clock1 = _ManualClock()
            cfg1 = AppConfig(
                projects_root=root1,
                cache_path=cache_path,
                cache_max_file_events=50,
                cache_max_command_events=100,
                discovery_interval_seconds=0.05,
                poll_interval_seconds=0.05,
                active_window_seconds=3600,
            )
            app1 = VisualizerApp(cfg1, now=clock1)
            session1 = root1 / "proj1" / "s.jsonl"
            _append(session1, _edit_jsonl("/repo/run1_alpha.py", 1,
                                          session_id="run1SESS0001",
                                          cwd="/home/dev/proj1"))
            _append(session1, _edit_jsonl("/repo/run1_beta.py", 2,
                                          session_id="run1SESS0001",
                                          cwd="/home/dev/proj1"))
            _append(session1, _bash_jsonl("pytest -q", session_id="run1SESS0001",
                                          cwd="/home/dev/proj1"))

            async with app1.run_test(size=(120, 40)) as pilot:
                mru = pilot.app.query_one(MruFilesPanel)
                await _pump_mru(pilot, mru, clock1, "run1_beta.py")

                mru_text = mru.rendered_text()
                _check("run1_beta.py" in mru_text,
                       "Events visible in MRU during first run",
                       f"mru_text[:80]={mru_text[:80]!r}")

            # App unmounted — cache flushed to disk
            _check(cache_path.exists(),
                   "DB file created at configured path",
                   f"path={cache_path}  exists={cache_path.exists()}")

            db1 = CacheDB(cache_path)
            file_evts = db1.load_file_events()
            cmd_evts = db1.load_command_events()
            db1.close()
            _check(len(file_evts) >= 2,
                   "File events persisted to DB",
                   f"file_events={len(file_evts)}")
            _check(len(cmd_evts) >= 1,
                   "Command events persisted to DB",
                   f"command_events={len(cmd_evts)}")
            persisted_paths = [e.file_path for e in file_evts]
            _check("/repo/run1_alpha.py" in persisted_paths,
                   "run1_alpha.py found in persisted file events",
                   f"persisted_paths={persisted_paths}")

        # ── Scenario 2: restart with empty projects_root restores state ──────
        print("[2] Restart (empty projects_root) → previous state restored from cache")
        with tempfile.TemporaryDirectory() as td2:
            root2 = Path(td2) / "projects2"
            root2.mkdir(parents=True, exist_ok=True)  # empty — no new events
            clock2 = _ManualClock()
            cfg2 = AppConfig(
                projects_root=root2,
                cache_path=cache_path,       # SAME cache as run 1
                discovery_interval_seconds=0.05,
                poll_interval_seconds=0.05,
                active_window_seconds=3600,
            )
            app2 = VisualizerApp(cfg2, now=clock2)

            async with app2.run_test(size=(120, 40)) as pilot2:
                mru2 = pilot2.app.query_one(MruFilesPanel)
                # Cache replay happens in on_mount; a few pauses let repaints settle
                for _ in range(20):
                    clock2.advance(0.05)
                    await pilot2.pause()

                mru_text2 = mru2.rendered_text()
                _check("run1_beta.py" in mru_text2,
                       "run1_beta.py restored from cache on restart",
                       f"mru_text[:120]={mru_text2[:120]!r}")
                _check("run1_alpha.py" in mru_text2,
                       "run1_alpha.py restored from cache on restart",
                       f"mru_text[:200]={mru_text2[:200]!r}")

                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                pilot2.app.save_screenshot(str(screenshot_path))
                print(f"  Screenshot saved → {screenshot_path}")

        # ── Scenario 3: eviction trims DB to cache_max_file_events ───────────
        print("[3] Eviction: 55 file events recorded with max=50 → DB has 50 rows")
        evict_db_path = Path(cache_td) / "evict.db"
        db_evict = CacheDB(evict_db_path)
        for i in range(55):
            db_evict.record_file_event(_make_file_event(f"/repo/evict_{i}.py", i),
                                       max_rows=50)
        loaded = db_evict.load_file_events()
        db_evict.close()
        _check(len(loaded) == 50,
               "DB trimmed to exactly 50 rows after 55 inserts",
               f"rows={len(loaded)}")
        loaded_paths = [e.file_path for e in loaded]
        _check("/repo/evict_0.py" not in loaded_paths,
               "Oldest entry (evict_0.py) was evicted",
               f"first_kept={loaded_paths[0]!r}")
        _check("/repo/evict_54.py" in loaded_paths,
               "Newest entry (evict_54.py) was retained",
               f"last_kept={loaded_paths[-1]!r}")


def main() -> int:
    screenshot = (
        Path(sys.argv[1]) if len(sys.argv) > 1
        else Path(".tmp/e2e_cache_live.svg")
    )
    print("=== E2E Cache Persistence Test ===")
    try:
        asyncio.run(run(screenshot))
        print("\nAll assertions PASSED")
        return 0
    except AssertionError as exc:
        print(f"\nFAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
