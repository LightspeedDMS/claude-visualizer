"""Command-line entry point for claude-visualizer.

Both ``claude-visualizer`` (the console script declared in ``pyproject.toml``)
and ``python -m claude_visualizer`` resolve here.  The module stays deliberately
thin: parse arguments into an :class:`~claude_visualizer.config.AppConfig`,
construct the :class:`~claude_visualizer.ui.app.VisualizerApp`, and run it
blocking/full-screen.  All tunables map 1:1 to ``AppConfig`` fields so the live
app and fixture-driven tests share exactly one configuration object.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp

_DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-visualizer",
        description=(
            "Blocking full-screen TUI showing live Claude Code file activity "
            "across all sessions on this machine."
        ),
    )
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=_DEFAULT_ROOT,
        help="Directory scanned for session transcripts "
        "(default: ~/.claude/projects).",
    )
    parser.add_argument(
        "--active-window",
        type=float,
        default=None,
        help="Seconds since last modification a file is still 'active' "
        "(default: %d)." % int(AppConfig().active_window_seconds),
    )
    parser.add_argument(
        "--max-active-files",
        type=int,
        default=None,
        help="Hard cap on simultaneously tailed files "
        "(default: %d)." % AppConfig().max_active_files,
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Active-file poll cadence in seconds "
        "(default: %s)." % AppConfig().poll_interval_seconds,
    )
    parser.add_argument(
        "--discovery-interval",
        type=float,
        default=None,
        help="Discovery rescan cadence in seconds "
        "(default: %s)." % AppConfig().discovery_interval_seconds,
    )
    parser.add_argument(
        "--mru-max",
        type=int,
        default=None,
        help="Maximum files retained in the MRU panel "
        "(default: %d)." % AppConfig().mru_max,
    )
    parser.add_argument(
        "--command-feed-max",
        type=int,
        default=None,
        help="Maximum Bash commands retained in the bottom Commands feed "
        "(default: %d)." % AppConfig().command_feed_max,
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Disable SQLite persistence cache "
        "(default: cache stored at ~/.claude-visualizer/cache.db).",
    )
    return parser


def build_config(argv: Optional[List[str]] = None) -> AppConfig:
    """Parse ``argv`` into an :class:`AppConfig`.

    Only flags the user actually supplied override the defaults; every unset
    optional falls back to the ``AppConfig`` default so the production values
    stand unless explicitly tuned.
    """
    args = _build_parser().parse_args(argv)

    overrides: dict = {"projects_root": args.projects_root}
    if args.active_window is not None:
        overrides["active_window_seconds"] = args.active_window
    if args.max_active_files is not None:
        overrides["max_active_files"] = args.max_active_files
    if args.poll_interval is not None:
        overrides["poll_interval_seconds"] = args.poll_interval
    if args.discovery_interval is not None:
        overrides["discovery_interval_seconds"] = args.discovery_interval
    if args.mru_max is not None:
        overrides["mru_max"] = args.mru_max
    if args.command_feed_max is not None:
        overrides["command_feed_max"] = args.command_feed_max
    if args.no_cache:
        overrides["cache_path"] = None

    return AppConfig(**overrides)


def build_app(config: AppConfig) -> VisualizerApp:
    """Construct the Textual application for ``config`` (no run side-effect)."""
    return VisualizerApp(config)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point: parse args, build the app, and run it blocking."""
    config = build_config(sys.argv[1:] if argv is None else argv)
    app = build_app(config)
    app.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
