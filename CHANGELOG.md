# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).  
This project adheres to [Semantic Versioning](https://semver.org/).

---

## [0.1.0] — 2026-06-05

Initial release.

### Added

**Three-panel TUI**

- **MRU Files panel** (top-left) — most-recently-used list of modified files across all sessions, newest at the top; coalesces by path; evicts LRU at configurable capacity; zebra-striped rows for visual separation of wrapped entries.
- **Live Diff panel** (top-right) — colour-mapped diff (green additions, red deletions, dim context) driven by a coalesced FIFO display queue with configurable min/max dwell and time-proportional auto-scroll; rests on the latest diff when idle (never blanks); skips directly to the most-recently-modified file on startup rather than animating through cache replay.
- **Commands panel** (bottom) — rolling log of every `Bash` command across all sessions, newest at the top; no deduplication; long commands truncated with `…`; overflow drops the oldest entry.

**Cross-session discovery**

- Discovers all Claude Code transcript files under `~/.claude/projects` via `mtime` window, including nested subagent transcripts (`subagents/agent-*.jsonl`).
- Picks up new sessions and transcript files while running — no restart required.
- Incremental byte-offset tailer: complete lines only, cold-start seed near the tail (no full-history replay), rotation/truncation tolerant, OOM-guarded.

**Event parsing**

- Parses `Write`, `Edit`, `Bash`, and `mcp__*` tool-use events from assistant JSONL entries.
- Stateful `requestId → thinking_chars` correlation: extended-thinking blocks (`{type:"thinking"}`) that precede a `tool_use` entry sharing the same `requestId` are detected and surfaced as the 🧠 indicator in the Live Diff header.
- Bounded LRU map for the correlation state — never grows unboundedly.

**Per-item timestamps**

- Every row in all three panels shows the transcript timestamp (`HH:MM:SS`); displays `--:--:--` gracefully when absent.

**Subagent awareness**

- All panels flag subagent events with a `⤷sub` indicator.

**Pin feature**

- Press `p` to pin the currently displayed diff; the panel holds that file and ignores the queue until a new event arrives and a minimum hold time elapses.
- Mouse-wheel scroll within a pinned diff.

**Keyboard controls**

- `q` / `Ctrl-C` — quit and restore terminal.
- `←` / `→` — resize MRU panel.
- `↑` / `↓` — resize Commands panel.
- `p` — pin/unpin Live Diff.

**MCP tool support**

- `mcp__<server>__<tool>` calls are rendered as `Server::tool key=val …` command rows in the Commands panel.

**Configurable via CLI flags**

- `--projects-root`, `--active-window`, `--max-active-files`, `--poll-interval`, `--discovery-interval`, `--mru-max`, `--command-feed-max`.

**Test suite**

- 421 automated tests; anti-mock (real temp files, real Textual `run_test()` harness); 90 % coverage gate.
- Three live E2E scripts (`e2e_diff_panel_live.py`, `e2e_commands_feed_live.py`, `e2e_timestamps_live.py`) that boot the real app and assert rendered panel content.

**Lint baseline**

- `lint.sh` — ruff + black + mypy; all checks pass clean on release.

[0.1.0]: https://github.com/LightspeedDMS/claude-visualizer/releases/tag/v0.1.0
