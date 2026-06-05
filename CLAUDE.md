# claude-visualizer — Project Notes

A blocking full-screen TUI that shows live Claude Code activity across **all**
sessions on the machine. It discovers every Claude Code transcript under
`~/.claude/projects`, tails the active ones, parses tool-use events, and feeds
them to panel models.

## Architecture (data flow)

```
discovery.discover() ─► active_set() ─► tailer.read_new() ─► EventExtractor.extract()
   (mtime window + cap)     (complete lines)        (events, +requestId→thinking)
                                                              │
                              pipeline.route_event(event, mru, diff_queue, command_feed)
                                                              │
              ┌───────────────────────────┬──────────────────┴──────────────────┐
              ▼ (FileModifiedEvent)        ▼ (FileModifiedEvent)                  ▼ (CommandEvent)
 models.mru.MruModel.record()   models.diff_queue.DiffQueueModel.record()   models.command_feed.CommandFeedModel.record()
 (top-left MRU panel)           (top-right Live Diff panel; tick→DisplayState) (bottom Commands feed; newest-on-top, no dedup)
```

Routing is by event type: `FileModifiedEvent` → MRU + diff queue; `CommandEvent`
(Bash) → command feed. They never cross panels.

- **`config.py`** — `AppConfig` frozen dataclass. EVERY tunable lives here
  (windows, caps, intervals, byte limits). Nothing is hardcoded elsewhere;
  pass an `AppConfig` through the graph for testability.
- **`events.py`** — immutable event dataclasses. `FileModifiedEvent`
  (Write→`full_content`; Edit→`old_string`/`new_string`/`replace_all`) and
  `CommandEvent` (Bash). Events are arrival-ordered, NOT timestamp-sorted.
  `FileModifiedEvent` also carries `used_thinking: bool=False` /
  `thinking_chars: int=0` (story #3), populated by the parser's correlator.
- **`parser.py`** — two entry points over one impl. `EventExtractor(config)` is
  the **stateful** parser: hold ONE per live stream and feed lines in order via
  `extract(line, source_path)`; it carries a **bounded** `requestId →
  thinking_chars` LRU map so extended-thinking blocks correlate to the later
  `tool_use` they precede (see gotcha). `parse_line(line, source_path)` is the
  **stateless** single-line wrapper (throwaway extractor) kept for callers/tests
  that parse a line in isolation. Both gate on `type=="assistant"` +
  `message.content` is a list and never raise (every error path returns `[]`).
- **`diffing.py`** — PURE (no `textual`/`rich`) diff computation for the diff
  panel. `compute_diff(event, config) -> list[DiffSegment]`. Edit → `difflib`
  line diff (ADD/DEL/CONTEXT); Write → labelled WHOLE-FILE additions (a HEADER
  marker + all-ADD, **no** fabricated before-state — read-only can't know prior
  disk content). Body truncated to `config.diff_max_lines` with a
  `…(truncated, N more lines)` footer. Colour is DATA (`COLOR_FOR_KIND`:
  ADD→green, DEL→red, CONTEXT→dim) so the UI renders; logic stays testable.
- **`models/diff_queue.py`** — PURE (no `textual`) FIFO display-queue state
  machine. `DiffQueueModel(config, now)` (clock injected for determinism);
  `tick(now, viewport_height) -> DisplayState`. Owns ALL timing/scroll/advance:
  coalesce-by-file (re-record updates in place, keeps position), bounded dwell
  (`min/max_dwell_seconds`), time-proportional auto-scroll, idle-rests-on-latest
  (never blanks), overflow drops stalest-UNSEEN at `diff_queue_max` and surfaces
  `plus_n_more`. The UI is a thin renderer of `DisplayState`.
- **`tailer.py`** — incremental byte-offset tailer (`TailState` + `read_new`).
- **`discovery.py`** — `discover()` + `active_set()`.
- **`models/mru.py`** — pure (UI-free) MRU model for the recently-modified
  files panel.
- **`models/command_feed.py`** — pure (UI-free) rolling **log** for the bottom
  Commands panel. `CommandFeedModel(config)` backed by
  `collections.deque(maxlen=config.command_feed_max)`. `record(CommandEvent)`
  **appends** (NO dedup — identical commands each get a row); at capacity the
  `maxlen` deque evicts the **oldest** automatically (AC4). `rows()` returns a
  newest-on-top **snapshot** of `CommandFeedEntry`s (command, ts, project_tag,
  `short_session = session_id[:8]`, is_subagent). Contrast with the diff queue,
  which coalesces — the command feed never merges.
- **`pipeline.py`** — async orchestration (`Pipeline` + pure `route_event`).
  Runs discovery + tail + parse in one asyncio loop and produces `Event`s onto
  a **bounded** `asyncio.Queue`. The producer; the UI is the consumer.
- **`ui/panels.py`** — pure formatters + thin `Static` widgets (no IO/parse on
  the render path). MRU: `format_mru_row`/`render_mru` + `MruFilesPanel`. Diff
  (story #3): `shorten_model` (strips `claude-`), `format_diff_header`
  (`HH:MM:SS · model · 🧠 · filename · origin`; time leads, 🧠 only when `used_thinking`),
  `render_diff_body` (colours via `COLOR_FOR_KIND`, appends `+N more`),
  `render_diff` (title+header+body, waiting text when state is `None`/empty) +
  the `DiffPanel` widget. **`diff_viewport_height(height, *, chrome_rows,
  default)`** is the PURE shared helper (panel height − chrome, floored at 1,
  `default` pre-layout) — the SINGLE source of truth used by BOTH the app's tick
  and the unit tests (do not re-derive the arithmetic in `app.py`). Commands
  (story #4): `truncate_command(text, width)` (collapses newlines to one line,
  `…` ellipsis, `len ≤ width`), `format_command_row(entry, width)` (whole row
  fits `width` — the command is the flexible field, time/origin suffix fixed;
  `HH:MM:SS` or `MISSING_TIME_TEXT`; `project · session` + `⤷sub`),
  `render_commands(model, width)` (newest-on-top block, waiting text when empty)
  + the `CommandsPanel` widget. (`PlaceholderPanel` was removed once story #4
  filled the bottom region — MESSI #12 anti-orphan.)
- **Per-item timestamp (all three panels).** `_format_time(ts) -> "HH:MM:SS"`
  (or `MISSING_TIME_TEXT` `--:--:--` when `ts is None`, fail-soft display only)
  is the SINGLE time formatter (one `strftime`, one `TIME_FORMAT`) — DRY,
  MESSI #4. ALL THREE renderers call it: `format_mru_row` and
  `format_command_row` lay the time immediately before the `project · session`
  origin (aligned columns); `format_diff_header` LEADS the header with it. The
  time is fed by per-row `ts` fields all sourced from `event.ts`:
  `MruEntry.ts` (set in `MruModel.record`), `DisplayState.ts` (set from `evt.ts`
  in `DiffQueueModel._build_state`, `None` in `_empty_state`), and the
  pre-existing `CommandFeedEntry.ts`. `event.ts` itself is the parser's
  `_parse_timestamp` of the transcript `timestamp` (ISO-8601, UTC clock kept
  verbatim — no local-tz shift, so `HH:MM:SS` assertions are deterministic).
- **`ui/app.py`** — `VisualizerApp` (blocking full-screen Textual app); 3-region
  grid; runs the pipeline as a worker; `route_event(event, mru, diff_queue,
  command_feed=…)` feeds ALL THREE models (`MruModel`, `DiffQueueModel`,
  `CommandFeedModel`). All three panels are mounted: `MruFilesPanel` (#mru-panel),
  `DiffPanel` (#top-right), `CommandsPanel` (#bottom). The periodic
  `set_interval(diff_refresh_seconds, _refresh_panels)` tick calls
  `DiffQueueModel.tick(now, diff_viewport_height(panel.size.height))`, repaints
  the `DiffPanel` from the returned `DisplayState`, mirrors `state.file_path`
  into `MruModel.highlighted_path` (AC9) and repaints the MRU panel, and repaints
  the Commands feed at `_commands_width()` (the panel's `content_size.width`,
  already excluding border/padding; default `_COMMANDS_DEFAULT_WIDTH` pre-layout)
  — AC5. The Commands feed is also repainted immediately in the consume loop so a
  command surfaces promptly. Clock injected (`now=`) for deterministic tests;
  production uses `time.monotonic`.
- **`__main__.py`** — CLI entry (`build_config`/`build_app`/`main`); `--projects-root`
  + tunable overrides (incl. `--mru-max`, `--command-feed-max`). Both
  `claude-visualizer` and `python -m claude_visualizer`.

## Load-bearing behaviors / gotchas

### parser: `project_tag = basename(cwd)`
`project_tag` is derived as `os.path.basename(entry["cwd"])`. Real Claude Code
**assistant** entries DO carry a `cwd` field. If a transcript's assistant
entries omit `cwd`, every event gets `project_tag == ""`.
> This was the root cause of the original failing test
> `test_normal_fixture_project_tag`: the fixture's assistant lines lacked
> `cwd`. Fix was to the fixture (`session_normal.jsonl`), not the parser —
> the parser's derivation is correct.

### parser: requestId → thinking correlation (`parser.py`, story #3)
Extended-thinking is NOT a field on the tool_use. It is a SEPARATE jsonl entry
— `message.content[]` block `{type:"thinking", thinking, signature}` — that
PRECEDES the `tool_use` entry but shares the same **entry-level** `requestId`
(top-level on the entry, NOT inside `message`). So `used_thinking`/
`thinking_chars` enrichment is **stateful across lines**:
- `EventExtractor` records `requestId → Σ len(thinking)` as lines stream in
  order; when a later `tool_use` entry with the same `requestId` produces a
  `FileModifiedEvent`, it is enriched (`used_thinking=True`, `thinking_chars=N`).
- The map is a **bounded** `OrderedDict` LRU capped at `config.requestid_map_max`
  (thinking always precedes its tool_use, so only a small recent window is ever
  needed — never grows for the process lifetime; MESSI #14).
- A pending entry is **consumed only when a `FileModifiedEvent` for it is
  actually produced** (peek is non-destructive). This is why the thinking-only
  entry doesn't erase its own state before the tool_use entry can read it.
- **`Pipeline` holds ONE long-lived `EventExtractor`** (in `__init__`, used by
  `_drain_active_tailers`) so correlation works across the live stream. The
  stateless `parse_line` only sees same-entry thinking, never cross-entry.
- There is NO native "effort level" field — the 🧠 marker is driven solely by
  this correlation.

### tailer invariants (`tailer.py`)
- **Complete lines only.** A trailing fragment (bytes after the last `\n`) is
  held in `partial_buffer` and NEVER parsed until a newline completes it.
- **`size_seen`** tracks the byte offset consumed; re-reads never double-emit.
- **Cold-start seed.** First attach (`inode is None`) seeks
  `max(0, size - seed_tail_bytes)` and discards the leading partial line — no
  full-history replay. If the seeded chunk has no newline yet, the discarded
  head stays "open" across reads (`pending_seed_skip`) so its terminating
  newline does not surface as a spurious blank line.
- **Rotation/truncation self-heal.** `current_size < size_seen` (truncation)
  OR inode change (logrotate) → reset (`size_seen=0`, clear buffer, re-seed).
- **OOM guard.** Lines longer than `max_line_bytes` are dropped.
- **Vanish tolerance.** Missing/permission-denied file → `[]` (never raises).

### discovery (`discovery.py`)
- `discover(root)` uses `Path.rglob("*.jsonl")`, so subagent transcripts under
  `*/subagents/agent-*.jsonl` are included automatically. `*.meta.json`
  sidecars are excluded by construction (the glob only matches `.jsonl`).
  Directories named `*.jsonl` are skipped (not files).
- `active_set(paths, config)` keeps files with
  `now - mtime <= active_window_seconds`, sorts **newest-first**, and caps at
  `max_active_files`. Per-file vanish/permission errors are skipped.

### MRU model (`models/mru.py`)
- Pure — NO `textual` import (enforced by `test_no_textual_import`).
- Backed by `OrderedDict[file_path -> MruEntry]`. `record()` dedups by path
  (move-to-front), refreshes origin fields, derives
  `short_session = session_id[:8]`, and evicts least-recently-used at
  `mru_max`. `rows()` returns a newest-first **snapshot** list.
- `highlighted_path: str | None` is set every diff tick (`ui/app.py`) to the
  file currently shown in the Diff panel; `render_mru` renders that row with
  `MRU_HIGHLIGHT_MARKER` (`▶`) + `MRU_HIGHLIGHT_STYLE` (`bold reverse`) — the
  F3↔F4 / diff↔MRU sync of AC9.

### pipeline + UI wiring (`pipeline.py`, `ui/app.py`)
- **Producer/consumer split.** `Pipeline` only *produces* events onto a bounded
  `asyncio.Queue`. Routing into panel models is the pure, synchronous
  `route_event(event, model)` — used by BOTH the UI worker and the tests. The
  UI render path does **no IO and no parsing**; it only reads `MruModel`.
- **`Pipeline._watch_loop` tolerates a missing `projects_root`.** `watchfiles.awatch`
  raises `FileNotFoundError` on a non-existent path, and `~/.claude/projects` may
  not exist on a fresh machine. So the watcher is wrapped: `_wait_for_root` polls
  (draining tailers each tick) until the root appears, then `_watch_existing_root`
  attaches `awatch`; if the root is removed mid-watch, `FileNotFoundError` is
  caught and control returns to the wait phase. **Never** create the root — it is
  read-only; just wait for it.
- **Liveness = change-driven + poll-driven.** `awatch(..., rust_timeout=poll_ms,
  yield_on_timeout=True)` yields an empty change set every `poll_interval` even
  with no FS events, so the active tailers are drained on a bounded cadence
  regardless of notification delivery. A separate `_discovery_loop` rebuilds the
  active set every `discovery_interval` (this is what picks up new sessions mid-run, AC7).
- **Bounded queue back-pressure.** `_enqueue` uses `put_nowait`; on `QueueFull` it
  discards the OLDEST event then enqueues — freshness over completeness for a live
  feed. Never unbounded memory growth.
- **`run_test()` is the real app.** UI tests use Textual's `async with app.run_test()
  as pilot:` — this runs the ACTUAL app + pipeline against a fixture root. Append a
  JSONL line, `await pilot.pause()`, assert `query_one(MruFilesPanel).rendered_text()`.
  `MruFilesPanel.rendered_text()` exists specifically so tests read panel state without
  scraping the compositor.
- **AC9 highlight sync (`ui/app.py::_refresh_panels`).** Every tick sets
  `MruModel.highlighted_path = state.file_path` and repaints the MRU panel, so
  the file shown in the diff is the `▶`-prefixed, `bold reverse` row in the MRU
  list. When the queue advances, the highlight follows; when idle it rests on
  the latest (AC7). A `None`/empty state simply clears the highlight.
- **Clean teardown — no deferred tick after panels are gone (story #3 carry-over
  fix).** `set_interval` returns a Textual `Timer`; the app stores it in
  `self._refresh_timer` and **`on_unmount` calls `.stop()` on it FIRST** (then
  clears `_running`, then stops the pipeline). Once stopped, the timer's
  `_task is None` and no further tick is scheduled — so a tick can no longer fire
  into a tree whose panels were removed (which previously raised `NoMatches: No
  nodes match '#top-right'`). Belt-and-suspenders: **`_refresh_panels` is wrapped
  in `try/except NoMatches`** (from `textual.css.query`) so even a stray
  post-unmount tick is a no-op. Regression-tested (`tests/test_ui.py::
  TestVisualizerAppTeardown`): two `run_test()` contexts back-to-back in one
  process, the timer's `_task` is `None` after unmount, and calling
  `_refresh_panels()` post-teardown does not raise.
- **E2E evidence.** `scripts/e2e_diff_panel_live.py` is the **diff-panel** live
  driver: it boots the REAL app via `run_test()` against a temp `projects_root`,
  appends real JSONL for an **Edit** (asserts red DEL + green ADD + header), a
  **Write** (asserts `whole-file write` label + all-green additions), and a
  **thinking-turn** (thinking block + tool_use sharing a `requestId`; asserts the
  `🧠` header glyph), asserts the displayed file is `▶`-highlighted in the MRU
  (AC9), and writes a real SVG via `pilot.app.save_screenshot`. Run:
  `TEXTUAL=headless .venv/bin/python scripts/e2e_diff_panel_live.py out.svg`.
  `scripts/e2e_commands_feed_live.py` is the **commands-feed** (story #4) live
  driver: it boots the REAL app, appends real `Bash` JSONL from **two sessions +
  a subagent** (plus an Edit so MRU/Diff are populated), and asserts AC1
  (newest-on-top + `⤷sub`), AC2 (no dedup — identical command appears twice),
  AC3 (long command truncated with `…`), AC4 (overflow past `command_feed_max`
  drops the oldest), AC5 (live), then writes an SVG showing all THREE panels.
  Run: `TEXTUAL=headless .venv/bin/python scripts/e2e_commands_feed_live.py out.svg`.
  `scripts/e2e_timestamps_live.py` is the **per-item timestamp** live driver: it
  boots the REAL app, appends an **Edit** (transcript `timestamp …T17:23:45Z`)
  and a **Bash** command (`…T09:08:07Z`) with KNOWN timestamps, and asserts the
  rendered MRU row, the Diff header, AND the command row each display the
  expected `HH:MM:SS`, then writes an SVG showing the time in all three panels.
  Run: `TEXTUAL=headless .venv/bin/python scripts/e2e_timestamps_live.py out.svg`.
  Full-screen boot of the installed binary against a fixture:
  `TEXTUAL=headless timeout 5 .venv/bin/claude-visualizer --projects-root <fixture>`
  — it runs full-screen until the deadline (timeout → exit 124, no traceback);
  with no timeout it blocks until `q`/`Ctrl+C` and restores the terminal cleanly.

### Development install: use EDITABLE (`pip install -e .`)
`install.sh` does a NON-editable `pip install "$PROJECT_DIR"` — it COPIES the
sources into `.venv/.../site-packages`. The console script
`.venv/bin/claude-visualizer` AND the `scripts/e2e_*_live.py` drivers (run as
`scripts/foo.py`, so `sys.path[0]` is `scripts/`, NOT the repo root) then import
that COPY — so working-tree edits do NOT take effect for the binary or the live
E2E scripts until you reinstall. Symptom seen during the timestamp work: the
live MRU row rendered with NO time column even though the unit tests (which put
the repo root on `sys.path`) were green, because the live driver imported a
stale site-packages `panels.py`. Fix: `.venv/bin/python -m pip install -e .`
(editable) so the binary, the scripts, and the working tree are ONE source of
truth. `python -c` from the repo dir resolves to the working tree (cwd on path),
which is why it masked the discrepancy — always validate the live binary/scripts
after a fresh checkout or a non-editable install.

## Testing

```bash
.venv/bin/pytest -q                                   # full suite
.venv/bin/pytest --cov=claude_visualizer --cov-report=term-missing
```

- Anti-mock: tailer/discovery tests use **real temp files** (`tmp_path`).
- Coverage gate is `fail_under = 90` (see `pyproject.toml`). The few uncovered
  lines in `tailer.py`/`discovery.py` are TOCTOU vanish-guards (file deleted
  between `stat` and the next syscall) — only reachable via a filesystem race,
  intentionally not mocked.
- `__main__.py` is omitted from coverage (entry point).

## Linting

**MANDATORY: run `./lint.sh` and fix all issues before ending any working session.**

```bash
./lint.sh          # ruff + black --check + mypy — must all pass before stopping
```

What each tool checks:
- **ruff** — unused imports, undefined names, pyflakes/pycodestyle style rules
- **black** — deterministic code formatting (88-char lines, Python 3.11 target)
- **mypy** — static type checking (`ignore_missing_imports = true`)

To auto-fix formatting violations reported by black:
```bash
.venv/bin/black claude_visualizer/ tests/
```

To auto-fix ruff violations:
```bash
.venv/bin/ruff check --fix claude_visualizer/ tests/
```

Tool config lives in `pyproject.toml` under `[tool.black]`, `[tool.ruff]`, and `[tool.mypy]`.
