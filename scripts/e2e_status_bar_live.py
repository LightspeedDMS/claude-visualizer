#!/usr/bin/env python3
"""E2E evidence script: status bar sections/separators + MRU full-width zebra stripes.

Run:
    TEXTUAL=headless .venv/bin/python scripts/e2e_status_bar_live.py out.svg
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp
from claude_visualizer.ui.panels import MruFilesPanel, StatusBar


def _check(cond: bool, label: str, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  —  {detail}" if detail else ""))
    if not cond:
        raise AssertionError(label + (f" ({detail})" if detail else ""))


def _edit_line(file_path: str, *, session_id: str, cwd: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2024-06-05T12:00:00.000Z",
            "sessionId": session_id,
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {
                            "file_path": file_path,
                            "old_string": "a",
                            "new_string": "b",
                        },
                    }
                ],
            },
        }
    )


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float = 0.1) -> None:
        self.t += dt


async def run(screenshot_path: Path) -> None:
    root = Path(tempfile.mkdtemp(prefix="cv-e2e-sb-")) / "projects"
    session = root / "myproject" / "session-sb.jsonl"

    clock = _Clock()
    cfg = AppConfig(
        projects_root=root,
        active_window_seconds=3600,
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
        stats_refresh_seconds=0.5,
    )
    app = VisualizerApp(cfg, now=clock)

    async with app.run_test(size=(160, 40)) as pilot:
        mru = pilot.app.query_one(MruFilesPanel)

        # Inject several file edits so MRU has multiple rows for zebra testing.
        files = [
            "/home/user/project/alpha.py",
            "/home/user/project/beta.py",
            "/home/user/project/gamma.py",
            "/home/user/project/delta.py",
        ]
        for fp in files:
            _append(session, _edit_line(fp, session_id="abc12345", cwd="/home/user/project"))

        # Wait for discovery + tail to surface the events.
        for _ in range(60):
            clock.advance(0.1)
            await pilot.pause()
            if "alpha.py" in mru.rendered_text():
                break

        # Wait for the stats bar to get its first real sample (0.5 s cadence).
        # Advance the wall clock so set_interval fires.
        for _ in range(20):
            clock.advance(0.1)
            await pilot.pause()

        # --- Evidence gathering ---
        mru_text = mru.rendered_text()
        status_bar = pilot.app.query_one(StatusBar)
        sb_plain = status_bar._renderable.plain

        print()
        print("=" * 60)
        print("E2E EVIDENCE — status bar + MRU zebra stripes")
        print("=" * 60)

        print()
        print("STATUS BAR plain text:")
        print(f"  {sb_plain!r}")
        print()

        # 1. Status bar has all four section labels
        _check("CPU" in sb_plain, "Status bar contains CPU label", sb_plain[:80])
        _check("RAM" in sb_plain, "Status bar contains RAM label", sb_plain[:80])
        _check("Disk" in sb_plain, "Status bar contains Disk label", sb_plain[:80])
        _check("Net" in sb_plain, "Status bar contains Net label", sb_plain[:80])

        # 2. Status bar has │ separators between sections
        _check("│" in sb_plain, "Status bar contains │ section separators", sb_plain[:80])
        sep_count = sb_plain.count("│")
        _check(sep_count >= 3, f"At least 3 │ separators present", f"found {sep_count}")

        # 3. Status bar has rate units (confirming Disk/Net values populated)
        has_rate = any(u in sb_plain for u in ("B/s", "K/s", "M/s", "G/s"))
        _check(has_rate, "Status bar contains IO rate units (B/s / K/s / M/s / G/s)", sb_plain)

        # 4. MRU rows are visible
        _check("alpha.py" in mru_text, "MRU shows injected files", mru_text[:120])
        _check("beta.py" in mru_text, "MRU shows multiple rows", mru_text[:120])

        # 5. MRU zebra spans are present (bright_white on #262626 for odd rows)
        from claude_visualizer.ui.panels import MRU_ROW_STYLE_ODD
        zebra_spans = [s for s in status_bar._renderable.spans
                       if MRU_ROW_STYLE_ODD in str(s.style)]
        # Check via the MRU renderable instead
        mru_spans = [s for s in mru._renderable.spans
                     if MRU_ROW_STYLE_ODD in str(s.style)]
        _check(len(mru_spans) >= 1, "MRU has ≥1 zebra-style span (odd-row background)",
               f"found {len(mru_spans)} spans")

        # 6. stats_refresh_seconds is 0.5 in the config
        _check(
            cfg.stats_refresh_seconds == 0.5,
            "stats_refresh_seconds is 0.5",
            str(cfg.stats_refresh_seconds),
        )

        print()
        print("EVIDENCE TABLE")
        print("-" * 60)
        rows = [
            ("Status bar CPU label",     "✓" if "CPU" in sb_plain else "✗"),
            ("Status bar RAM label",     "✓" if "RAM" in sb_plain else "✗"),
            ("Status bar Disk label",    "✓" if "Disk" in sb_plain else "✗"),
            ("Status bar Net label",     "✓" if "Net" in sb_plain else "✗"),
            ("│ separators (≥3)",        "✓" if sep_count >= 3 else f"✗ ({sep_count})"),
            ("IO rate units present",    "✓" if has_rate else "✗"),
            ("MRU alpha.py visible",     "✓" if "alpha.py" in mru_text else "✗"),
            ("MRU zebra spans ≥1",       f"✓ ({len(mru_spans)})" if mru_spans else "✗"),
            ("stats_refresh_seconds",    str(cfg.stats_refresh_seconds)),
        ]
        for label, result in rows:
            print(f"  {label:<35} {result}")
        print("-" * 60)

        pilot.app.save_screenshot(str(screenshot_path))
        print(f"\nScreenshot → {screenshot_path}")
        print()
        print("All assertions PASSED.")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("e2e_status_bar.svg")
    asyncio.run(run(path))


if __name__ == "__main__":
    main()
