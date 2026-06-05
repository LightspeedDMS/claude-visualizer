# claude-visualizer

> Real-time terminal dashboard for live Claude Code activity across every session on the machine.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

`claude-visualizer` discovers every Claude Code session running on the host — including nested subagents — and streams their activity into a single live three-panel TUI.

No credentials, no API keys, no network: it reads only the transcript files Claude Code writes to `~/.claude/projects`.

---

## Dashboard

```
┌───────────────────────┬─┬─────────────────────────────────────┐
│ MRU Files             │ │ Live Diff                           │
│  newest-modified-     │ │  colour-mapped diff of the most-    │
│  first list across    │ │  recently modified file; auto-      │
│  all sessions         │ │  scrolls tall diffs                 │
├───────────────────────┴─┴─────────────────────────────────────┤
│ Commands  —  rolling log of every Bash command, all sessions   │
└────────────────────────────────────────────────────────────────┘
```

Use `←` / `→` to resize the MRU panel and `↑` / `↓` to resize the Commands panel.

---

### Top-left — MRU Files

A most-recently-used list of every file modified across all sessions, newest at the top. Re-modifying a file moves its row back to the top; at capacity the least-recently-used entry falls off the bottom. Rows alternate subtle background shades (zebra stripes) so entries stay visually distinct even when long paths wrap to a second line.

```
17:23:45  ▶ [EDIT] /home/user/project/calc.py    myproject · a1b2c3d4
09:08:07    [WRITE] /home/user/project/utils.py  myproject · a1b2c3d4  ⤷sub
```

Each row: `time  [EDIT|WRITE]  path   project · session  ⤷sub`

- `time` — transcript timestamp (`HH:MM:SS`), or `--:--:--` when absent
- `▶` — marks the file currently shown in the Live Diff panel
- `⤷sub` — indicates the event came from a subagent transcript
- `project · session` — `basename(cwd)` of the session and the first 8 chars of the session ID

---

### Top-right — Live Diff

A colour-mapped diff of the most recently modified file, driven by a FIFO display queue that coalesces by file and owns its own timing (3 s minimum dwell, 12 s maximum, then auto-advances to the next queued file). On startup the panel skips directly to the most-recently-modified file rather than animating through the full cache replay.

```
17:23:45 · opus-4-8 · 🧠 · calc.py · myproject · a1b2c3d4
  - return a - b
  + return a + b
```

Header: `time · model · [🧠] · filename · project · session`

- **Edit** — unified diff: added lines green (`+`), removed lines red (`-`), context dim
- **Write** — labelled `whole-file write`, all-green additions; no fabricated removals (a read-only tail cannot know the prior on-disk content)
- **🧠** — shown only when the edit's request used extended thinking (a `{type:"thinking"}` block sharing the same `requestId`)
- **`+N more`** badge when files are queued behind the current view
- **`…(truncated, N more lines)`** footer when a diff exceeds the line cap
- When the queue is idle the panel rests on the latest diff and never blanks

---

### Bottom — Commands

A rolling log of every `Bash` command executed across all sessions and subagents, newest at the top. Identical commands are **not** deduplicated — each invocation gets its own row. At capacity the oldest entry scrolls off.

```
17:23:45  pytest -q tests/                  myproject · a1b2c3d4
09:08:07  docker compose up -d              otherproj · e5f6g7h8
09:08:07  ruff check .                      myproject · subSESS0  ⤷sub
```

Each row: `time  command  project · session  ⤷sub`

Commands longer than the panel width are truncated with a `…` ellipsis, always on one line.

---

## Requirements

Python **3.11 or newer**. No credentials, no API keys, no external services.

| Package | Version | Purpose |
|---------|---------|---------|
| [`textual`](https://github.com/Textualize/textual) | `≥ 0.47` | Full-screen TUI framework |
| [`watchfiles`](https://github.com/samuelcolvin/watchfiles) | `≥ 0.21` | Efficient filesystem change notification |
| [`rich`](https://github.com/Textualize/rich) | `≥ 13` | Terminal colour and text rendering |

---

## Installation

### Recommended — isolated install via [pipx](https://pypa.github.io/pipx/)

```bash
pipx install .
```

### Into a virtual environment

```bash
python -m venv .venv
.venv/bin/pip install .
```

### Quick install script (creates `.venv` + shell alias)

```bash
./install.sh
source ~/.bashrc   # or ~/.zshrc
```

`install.sh` is idempotent: re-running it reinstalls the package and refreshes the alias without duplicating it. After sourcing your shell config the `claude-visualizer` alias is available in any terminal.

> **Development installs:** use `pip install -e .` (editable) so the binary
> and E2E scripts always reflect the working tree without reinstalling.

---

## Usage

```bash
# Watch the default transcript root (~/.claude/projects)
claude-visualizer

# Point at a specific tree (useful for testing against a fixture)
claude-visualizer --projects-root ~/.claude/projects

# Increase MRU list capacity and command-feed history
claude-visualizer --mru-max 100 --command-feed-max 200

# Both the console script and python -m are equivalent
python -m claude_visualizer
```

New sessions that appear while the app is running are discovered and tailed automatically — no restart required.

---

## Keyboard controls

| Key | Action |
|-----|--------|
| `q` or `Ctrl-C` | Quit, restore terminal |
| `←` | Narrow the MRU panel (−2 columns) |
| `→` | Widen the MRU panel (+2 columns) |
| `↑` | Expand the Commands panel (+1 row) |
| `↓` | Shrink the Commands panel (−1 row) |
| `p` | Pin / unpin the currently displayed diff |

---

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--projects-root PATH` | `~/.claude/projects` | Directory scanned for session transcripts |
| `--active-window SECONDS` | `120` | How recently a file must have been modified to be tailed |
| `--max-active-files N` | `64` | Hard cap on simultaneously tailed files |
| `--poll-interval SECONDS` | `0.3` | Active-file poll cadence |
| `--discovery-interval SECONDS` | `5.0` | How often the transcript tree is re-scanned for new sessions |
| `--mru-max N` | `50` | Maximum files retained in the MRU panel |
| `--command-feed-max N` | `100` | Maximum Bash commands retained in the Commands feed |

---

## Architecture

```
~/.claude/projects/**/*.jsonl
        │
        ▼
 discovery.discover()      ← rglob("*.jsonl"), includes subagent transcripts
        │  active_set()    ← mtime window + cap, newest-first
        ▼
 tailer.read_new()         ← incremental byte-offset tailing, complete lines only
        │                     cold-start seed near tail (no full-history replay)
        ▼
 EventExtractor.extract()  ← stateful parser; Write/Edit → FileModifiedEvent,
        │                     Bash → CommandEvent; requestId→thinking correlation
        ▼
 Pipeline (asyncio)        ← bounded queue, change-driven + poll-driven liveness
        │  route_event()
        ├──────────────────► MruModel        → MRU Files panel
        ├──────────────────► DiffQueueModel  → Live Diff panel (tick owns timing)
        └──────────────────► CommandFeedModel→ Commands panel
```

Key design decisions:

- **Read-only, no credentials.** The app only reads transcript files; it never calls the Claude API or modifies any state.
- **No full-history replay.** On cold-start, each file is seeded 64 KB from the end. Only new lines written after the app starts are processed.
- **Rotation and truncation tolerant.** The tailer detects inode changes (logrotate) and truncations and resets cleanly.
- **Bounded memory.** Every accumulating structure is capped: active file set, diff queue, MRU list, command feed, and the parser's `requestId` correlation map.
- **Pure render path.** The UI never does I/O or parsing; it only reads model state and renders it.

---

## Development

```bash
# Editable install (required for scripts/ to reflect the working tree)
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install pytest pytest-asyncio pytest-cov

# Run the full test suite
.venv/bin/pytest -q

# Run with coverage (gate: 90 %)
.venv/bin/pytest --cov=claude_visualizer --cov-report=term-missing
```

Tests are **anti-mock**: discovery, tailer, and pipeline tests drive real temporary files; UI tests run the actual Textual application through its `run_test()` harness against a fixture `projects_root`.

### Live E2E scripts

Three scripts boot the real app against a synthetic transcript tree and assert rendered panel content:

```bash
# Diff panel: Edit diff, Write (whole-file), extended-thinking 🧠 header
TEXTUAL=headless .venv/bin/python scripts/e2e_diff_panel_live.py out.svg

# Commands feed: newest-on-top, no-dedup, truncation, overflow, live update
TEXTUAL=headless .venv/bin/python scripts/e2e_commands_feed_live.py out.svg

# Per-item timestamps in all three panels
TEXTUAL=headless .venv/bin/python scripts/e2e_timestamps_live.py out.svg
```

Each script prints `[PASS]`/`[FAIL]` per assertion and writes an SVG screenshot.

---

## License

Released under the [MIT License](LICENSE).
