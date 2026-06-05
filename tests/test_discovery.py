"""Tests for discovery.py — file discovery and active-set selection.

All tests build a REAL temporary directory tree (anti-mock). Behaviour:
- discover(root) returns every ``*.jsonl`` under root, including
  ``*/subagents/agent-*.jsonl``, but NEVER ``*.meta.json`` sidecars.
- active_set(paths, config) keeps only files modified within
  ``active_window_seconds``, capped at ``max_active_files`` (most-recent kept).
- Both tolerate files vanishing / permission errors per-file (skip, no raise).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.discovery import active_set, discover

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _touch(path: Path, content: str = "{}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _set_mtime(path: Path, seconds_ago: float) -> None:
    """Set both atime and mtime to `seconds_ago` before now."""
    when = time.time() - seconds_ago
    os.utime(path, (when, when))


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_empty_root_returns_empty(self, tmp_path: Path):
        assert discover(tmp_path) == []

    def test_missing_root_returns_empty(self, tmp_path: Path):
        missing = tmp_path / "no-such-dir"
        assert discover(missing) == []

    def test_finds_top_level_jsonl(self, tmp_path: Path):
        f = _touch(tmp_path / "proj" / "abc123.jsonl")
        result = discover(tmp_path)
        assert str(f) in result

    def test_finds_subagent_jsonl(self, tmp_path: Path):
        sub = _touch(tmp_path / "proj" / "sess" / "subagents" / "agent-xyz.jsonl")
        result = discover(tmp_path)
        assert str(sub) in result

    def test_finds_both_session_and_subagent(self, tmp_path: Path):
        sess = _touch(tmp_path / "proj" / "main.jsonl")
        sub = _touch(tmp_path / "proj" / "main" / "subagents" / "agent-1.jsonl")
        result = discover(tmp_path)
        assert str(sess) in result
        assert str(sub) in result
        assert len(result) == 2

    def test_excludes_meta_json(self, tmp_path: Path):
        _touch(tmp_path / "proj" / "abc.jsonl")
        meta = _touch(tmp_path / "proj" / "abc.meta.json", content="{}")
        result = discover(tmp_path)
        assert str(meta) not in result
        assert all(not p.endswith(".meta.json") for p in result)

    def test_excludes_non_jsonl_files(self, tmp_path: Path):
        _touch(tmp_path / "proj" / "real.jsonl")
        _touch(tmp_path / "proj" / "notes.txt", content="hi")
        _touch(tmp_path / "proj" / "data.json", content="{}")
        result = discover(tmp_path)
        assert all(p.endswith(".jsonl") for p in result)
        assert len(result) == 1

    def test_returns_absolute_paths(self, tmp_path: Path):
        _touch(tmp_path / "proj" / "abc.jsonl")
        result = discover(tmp_path)
        assert all(os.path.isabs(p) for p in result)

    def test_accepts_str_root(self, tmp_path: Path):
        f = _touch(tmp_path / "proj" / "abc.jsonl")
        result = discover(str(tmp_path))
        assert str(f) in result

    def test_deeply_nested_jsonl_found(self, tmp_path: Path):
        deep = _touch(tmp_path / "a" / "b" / "c" / "d" / "deep.jsonl")
        result = discover(tmp_path)
        assert str(deep) in result

    def test_directory_named_jsonl_is_skipped(self, tmp_path: Path):
        # A *directory* whose name ends in .jsonl is matched by rglob but is
        # NOT a file — it must be skipped, not returned.
        weird_dir = tmp_path / "proj" / "looks-like.jsonl"
        weird_dir.mkdir(parents=True)
        real = _touch(tmp_path / "proj" / "real.jsonl")
        result = discover(tmp_path)
        assert str(weird_dir.resolve()) not in result
        assert str(real) in result
        assert len(result) == 1


# ---------------------------------------------------------------------------
# active_set()
# ---------------------------------------------------------------------------


class TestActiveSet:
    def test_empty_paths_returns_empty(self):
        cfg = AppConfig(active_window_seconds=120)
        assert active_set([], cfg) == []

    def test_fresh_file_is_active(self, tmp_path: Path):
        f = _touch(tmp_path / "fresh.jsonl")
        _set_mtime(f, seconds_ago=1)
        cfg = AppConfig(active_window_seconds=120)
        assert active_set([str(f)], cfg) == [str(f)]

    def test_old_file_is_inactive(self, tmp_path: Path):
        old = _touch(tmp_path / "old.jsonl")
        _set_mtime(old, seconds_ago=10_000)
        cfg = AppConfig(active_window_seconds=120)
        assert active_set([str(old)], cfg) == []

    def test_mixed_fresh_and_old(self, tmp_path: Path):
        fresh = _touch(tmp_path / "fresh.jsonl")
        old = _touch(tmp_path / "old.jsonl")
        _set_mtime(fresh, seconds_ago=5)
        _set_mtime(old, seconds_ago=9_999)
        cfg = AppConfig(active_window_seconds=120)
        result = active_set([str(fresh), str(old)], cfg)
        assert result == [str(fresh)]

    def test_boundary_just_inside_window_active(self, tmp_path: Path):
        f = _touch(tmp_path / "edge.jsonl")
        _set_mtime(f, seconds_ago=50)
        cfg = AppConfig(active_window_seconds=120)
        assert str(f) in active_set([str(f)], cfg)

    def test_cap_keeps_most_recently_modified(self, tmp_path: Path):
        # Five fresh files, cap of 2 → keep the 2 most-recently-modified.
        files = []
        for i in range(5):
            f = _touch(tmp_path / f"f{i}.jsonl")
            _set_mtime(f, seconds_ago=float(i + 1))  # f0 newest, f4 oldest
            files.append(str(f))
        cfg = AppConfig(active_window_seconds=120, max_active_files=2)
        result = active_set(files, cfg)
        assert len(result) == 2
        assert str(tmp_path / "f0.jsonl") in result
        assert str(tmp_path / "f1.jsonl") in result
        assert str(tmp_path / "f4.jsonl") not in result

    def test_result_sorted_newest_first(self, tmp_path: Path):
        a = _touch(tmp_path / "a.jsonl")
        b = _touch(tmp_path / "b.jsonl")
        c = _touch(tmp_path / "c.jsonl")
        _set_mtime(a, seconds_ago=30)
        _set_mtime(b, seconds_ago=10)
        _set_mtime(c, seconds_ago=20)
        cfg = AppConfig(active_window_seconds=120, max_active_files=10)
        result = active_set([str(a), str(b), str(c)], cfg)
        # newest (b, 10s) → c (20s) → a (30s)
        assert result == [str(b), str(c), str(a)]

    def test_vanished_file_skipped(self, tmp_path: Path):
        present = _touch(tmp_path / "present.jsonl")
        _set_mtime(present, seconds_ago=1)
        missing = str(tmp_path / "vanished.jsonl")  # never created
        cfg = AppConfig(active_window_seconds=120)
        result = active_set([str(present), missing], cfg)
        assert result == [str(present)]

    def test_cap_applies_only_to_active_files(self, tmp_path: Path):
        # 3 fresh + 2 old, cap of 10 → only the 3 fresh survive (old excluded
        # by window, not by cap).
        fresh = []
        for i in range(3):
            f = _touch(tmp_path / f"fresh{i}.jsonl")
            _set_mtime(f, seconds_ago=float(i + 1))
            fresh.append(str(f))
        for i in range(2):
            o = _touch(tmp_path / f"old{i}.jsonl")
            _set_mtime(o, seconds_ago=5_000.0)
        cfg = AppConfig(active_window_seconds=120, max_active_files=10)
        result = active_set(
            fresh
            + [
                str(tmp_path / "old0.jsonl"),
                str(tmp_path / "old1.jsonl"),
            ],
            cfg,
        )
        assert len(result) == 3
        assert set(result) == set(fresh)


# ---------------------------------------------------------------------------
# Integration: discover() + active_set() over one tree
# ---------------------------------------------------------------------------


class TestDiscoverActiveIntegration:
    def test_full_tree_pipeline(self, tmp_path: Path):
        root = tmp_path / "projects"
        sess = _touch(root / "proj" / "main.jsonl")
        sub = _touch(root / "proj" / "main" / "subagents" / "agent-1.jsonl")
        old = _touch(root / "proj" / "stale.jsonl")
        _touch(root / "proj" / "main.meta.json", content="{}")  # excluded
        _set_mtime(sess, seconds_ago=2)
        _set_mtime(sub, seconds_ago=4)
        _set_mtime(old, seconds_ago=10_000)

        discovered = discover(root)
        # 3 jsonl files discovered (meta.json excluded)
        assert len(discovered) == 3
        assert all(not p.endswith(".meta.json") for p in discovered)

        cfg = AppConfig(active_window_seconds=120, max_active_files=64)
        active = active_set(discovered, cfg)
        # stale file dropped by the window; session + subagent remain.
        assert set(active) == {str(sess), str(sub)}
        # newest-first ordering: sess (2s) before sub (4s)
        assert active == [str(sess), str(sub)]
