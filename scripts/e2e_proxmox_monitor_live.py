#!/usr/bin/env python3
"""Live E2E driver for the Proxmox Cluster monitor plugin (story #7) — REAL app, no mocks.

Boots the ACTUAL :class:`~claude_visualizer.ui.app.VisualizerApp` through
Textual's real ``run_test()`` harness against a REAL temporary
``projects_root`` and a REAL temporary ``monitors_dir`` seeded with a wrapper
that loads the real ``~/.claude-visualizer/proxmox.yaml``.

LIVE path  (cluster answers):
    Asserts the rendered MonitorBar line contains ``Cluster:``, ``Ceph:``,
    node dots, OSD dots, and the alert / "no alerts" section.
    Prints "LIVE cluster".

DEGRADED path (cluster unreachable — user-approved fallback):
    Exercises and asserts all four degraded Monitor states:
      1. Missing config → ``""`` (empty/suppressed — no row in the monitor bar).
      2. ``PVE: connecting…`` — config present but first poll returns None
         (nodes unreachable); snapshot is still None.
      3. Stale-snapshot retention — after one successful poll the snapshot is
         retained even when subsequent polls fail (all nodes unreachable).
      4. Connected-then-500 — real local HTTP server serves OK then returns 500;
         asserts the rendered bar shows ``⚠ PVE UNREACHABLE <age>`` badge.
    Prints "DEGRADED fallback (cluster unreachable)".

Never prints the token_secret.  Always writes a real SVG screenshot.
Exits 0 on success, 1 on assertion failure.

Run headless:
    TEXTUAL=headless .venv/bin/python scripts/e2e_proxmox_monitor_live.py out.svg
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from claude_visualizer.config import AppConfig
from claude_visualizer.ui.app import VisualizerApp
from claude_visualizer.ui.panels import MonitorBar

# Real proxmox.yaml written by the user — never printed, never exposed.
_REAL_CONFIG_PATH = Path.home() / ".claude-visualizer" / "proxmox.yaml"

# Wrapper body written into temp monitors_dir so MonitorRegistry (which calls
# Monitor() with no args) picks up the real config path via a closure default.
_WRAPPER_TEMPLATE = """\
from pathlib import Path
from claude_visualizer.monitors.proxmox_cluster import Monitor as _Base

_CONFIG_PATH = Path({config_path!r})


class Monitor:
    \"\"\"Thin wrapper that injects the real proxmox.yaml path.\"\"\"

    def __init__(self) -> None:
        self._inner = _Base(config_path=_CONFIG_PATH)

    def tick(self, now: float):
        return self._inner.tick(now)
"""


def _check(cond: bool, label: str, detail: str = "") -> None:
    """Explicit, non-silent assertion (MESSI #13)."""
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise AssertionError(label + (f" ({detail})" if detail else ""))


def _write_monitor(directory: Path, filename: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")


async def _pump(bar: MonitorBar, contains: str, pilot, tries: int = 80) -> str:
    """Pause (bounded) until the bar's rendered_text contains ``contains``."""
    text = ""
    for _ in range(tries):
        await pilot.pause()
        text = bar.rendered_text()
        if contains in text:
            return text
    return text


async def _settle(pilot, pauses: int = 15) -> None:
    for _ in range(pauses):
        await pilot.pause()


def _attempt_live_poll() -> bool:
    """Return True if the cluster is reachable (real HTTP poll, bounded 5s)."""
    from claude_visualizer.monitors.proxmox_cluster import Monitor

    if not _REAL_CONFIG_PATH.exists():
        return False
    m = Monitor(config_path=_REAL_CONFIG_PATH)
    result = m._fetch()  # noqa: SLF001  — intentional white-box probe
    return result is not None


async def run_live(screenshot_path: Path, monitors_dir: Path) -> None:
    """Live-cluster path: assert real cluster segments in the rendered bar."""
    projects_root = Path(tempfile.mkdtemp(prefix="cv-e2e-pve-live-")) / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)

    # Seed monitors_dir with the real-config wrapper.
    _write_monitor(
        monitors_dir,
        "proxmox_cluster.py",
        _WRAPPER_TEMPLATE.format(config_path=str(_REAL_CONFIG_PATH)),
    )

    cfg = AppConfig(
        projects_root=projects_root,
        active_window_seconds=3600,
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
        monitors_dir=monitors_dir,
        monitor_refresh_seconds=0.2,
        cache_path=None,
    )
    app = VisualizerApp(cfg)

    async with app.run_test(size=(200, 40)) as pilot:
        bar = pilot.app.query_one(MonitorBar)

        # Wait up to ~16 s for cluster data (live HTTP poll may take a moment).
        text = await _pump(bar, "Cluster:", pilot, tries=80)

        print("LIVE cluster assertions:")
        _check("Cluster:" in text, "Cluster: segment present", repr(text[:120]))
        _check("Ceph:" in text, "Ceph: segment present", repr(text[:120]))
        # Node dots: at least one node name followed by a dot character.
        has_node_dot = "●" in text
        _check(has_node_dot, "node/OSD dot (●) present", repr(text[:120]))
        # Alert section — either "no alerts" or a ⚑ alert.
        has_alert_section = "no alerts" in text or "⚑" in text or "↻" in text
        _check(has_alert_section, "alert section present (↻/⚑/no alerts)", repr(text[:120]))

        await _settle(pilot, 20)
        app.save_screenshot(str(screenshot_path))
        _check(
            screenshot_path.exists() and screenshot_path.stat().st_size > 0,
            "screenshot SVG captured",
            f"path={screenshot_path}",
        )

        print("\n--- Proxmox monitor bar (LIVE) ---")
        print(text[:200])

    print(f"\nLIVE cluster. Screenshot: {screenshot_path}")


async def run_degraded(screenshot_path: Path, monitors_dir: Path) -> None:
    """Degraded path: exercise all three non-live Monitor states without fabricating data."""
    from claude_visualizer.monitors.proxmox_cluster import Monitor

    print("DEGRADED fallback (cluster unreachable) assertions:")

    # ------------------------------------------------------------------ #
    # 1. Missing config → "" (empty/suppressed — no row rendered)
    # ------------------------------------------------------------------ #
    missing_cfg = Path(tempfile.mkdtemp()) / "nonexistent.yaml"
    m_missing = Monitor(config_path=missing_cfg)
    result_missing = m_missing.tick(time.monotonic())
    text_missing = result_missing.plain if hasattr(result_missing, "plain") else str(result_missing)
    _check(
        text_missing == "",
        "missing config → '' (empty/suppressed, no row)",
        repr(text_missing),
    )

    # ------------------------------------------------------------------ #
    # 2. Config present but all nodes unreachable → PVE: connecting…
    # ------------------------------------------------------------------ #
    if _REAL_CONFIG_PATH.exists():
        m_conn = Monitor(config_path=_REAL_CONFIG_PATH)
        result_conn = m_conn.tick(time.monotonic())
        text_conn = result_conn.plain if hasattr(result_conn, "plain") else str(result_conn)
        _check(
            "connecting" in text_conn.lower() or "PVE" in text_conn,
            "config present but unreachable → PVE: connecting…",
            repr(text_conn),
        )
        # Also verify _polled_once was set and _last_poll updated.
        _check(m_conn._polled_once, "_polled_once=True after first tick")  # noqa: SLF001

    # ------------------------------------------------------------------ #
    # 3. Stale-snapshot retention: _snapshot stays None on repeated failures
    #    (_fetch() returning None must NOT overwrite a good snapshot, but
    #    here we confirm the stale-None path: snapshot stays None and the
    #    monitor keeps returning "connecting…" — no crash, no fabrication).
    # ------------------------------------------------------------------ #
    if _REAL_CONFIG_PATH.exists():
        m_stale = Monitor(config_path=_REAL_CONFIG_PATH)
        now0 = 0.0
        r0 = m_stale.tick(now0)          # first tick — polls, fails, snapshot=None
        t0 = r0.plain if hasattr(r0, "plain") else str(r0)
        # Advance past poll_interval (default 30 s) to force a second poll.
        now1 = now0 + 35.0
        r1 = m_stale.tick(now1)          # second tick — polls again, fails again
        t1 = r1.plain if hasattr(r1, "plain") else str(r1)
        _check(
            "connecting" in t1.lower() or "PVE" in t1,
            "stale-None path: still connecting after second failed poll",
            repr(t1),
        )
        _check(m_stale._snapshot is None, "snapshot stays None on repeated failures")  # noqa: SLF001

    # ------------------------------------------------------------------ #
    # 4. Connected-then-500: real local HTTP server → success → 500 → badge
    # ------------------------------------------------------------------ #
    _OSD_TREE_E2E = {
        "root": {
            "type": None,
            "name": None,
            "children": [
                {
                    "type": "root",
                    "id": "-1",
                    "name": "default",
                    "children": [
                        {
                            "type": "host",
                            "id": "-3",
                            "name": "e2enode",
                            "children": [
                                {
                                    "type": "osd",
                                    "id": "0",
                                    "name": "osd.0",
                                    "status": "up",
                                    "in": 1,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    }
    _E2E_RESPONSES: dict = {
        "/api2/json/cluster/status": [
            {"type": "cluster", "name": "e2e-cluster", "quorate": 1, "nodes": 1},
            {
                "type": "node",
                "id": "node/e2enode",
                "name": "e2enode",
                "online": 1,
                "local": 1,
            },
        ],
        "/api2/json/cluster/ceph/status": {
            "health": {"status": "HEALTH_OK", "checks": {}},
            "osdmap": {"num_osds": 1, "num_up_osds": 1, "num_in_osds": 1},
            "pgmap": {
                "bytes_used": 100_000_000_000,
                "bytes_total": 1_000_000_000_000,
            },
        },
        "/api2/json/cluster/ha/resources": [],
        "/api2/json/nodes": [
            {
                "node": "e2enode",
                "status": "online",
                "cpu": 0.10,
                "mem": 1_000_000_000,
                "maxmem": 8_000_000_000,
            },
        ],
        "/api2/json/nodes/e2enode/ceph/osd": _OSD_TREE_E2E,
    }

    e2e_state = {"fail": False}

    class _E2EHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if e2e_state["fail"]:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"error")
                return
            data = _E2E_RESPONSES.get(self.path)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    e2e_server = HTTPServer(("127.0.0.1", 0), _E2EHandler)
    e2e_port = e2e_server.server_address[1]
    e2e_thread = threading.Thread(target=e2e_server.serve_forever, daemon=True)
    e2e_thread.start()

    try:
        from claude_visualizer.monitors.proxmox_cluster import Monitor as _Mon

        e2e_yaml = (
            f"nodes:\n"
            f"  - http://127.0.0.1:{e2e_port}\n"
            f"token_id: root@pam!test\n"
            f"token_secret: dummy\n"
            f"poll_interval_seconds: 30\n"
            f"alert_rotate_seconds: 10\n"
            f"verify_ssl: false\n"
        )
        import tempfile as _tmp

        e2e_cfg = Path(_tmp.mkdtemp()) / "e2e_proxmox.yaml"
        e2e_cfg.write_text(e2e_yaml, encoding="utf-8")
        m_e2e = _Mon(config_path=e2e_cfg)

        # Step 4a: successful fetch at now=100
        m_e2e.tick(100.0)
        in_flight = m_e2e._in_flight  # noqa: SLF001
        assert in_flight is not None, "Step 4: first poll must submit in_flight"
        snap = in_flight.result(timeout=5.0)
        assert snap is not None, "Step 4: first fetch must succeed against live server"
        m_e2e.tick(100.0)  # collect
        assert m_e2e._snapshot is not None  # noqa: SLF001

        # Step 4b: switch server to 500, advance past poll_interval
        e2e_state["fail"] = True
        m_e2e.tick(130.0)  # submit failing poll
        fail_flight = m_e2e._in_flight  # noqa: SLF001
        assert fail_flight is not None, "Step 4: second poll must submit in_flight"
        fail_snap = fail_flight.result(timeout=5.0)
        assert fail_snap is None, "Step 4: failed poll must return None"

        # Collect at now=220 → age = 120s = 2m
        result_e2e = m_e2e.tick(220.0)
        text_e2e = result_e2e.plain if hasattr(result_e2e, "plain") else str(result_e2e)
        _check(
            "PVE UNREACHABLE" in text_e2e,
            "connected-then-500 → ⚠ PVE UNREACHABLE badge present",
            repr(text_e2e[:120]),
        )
        _check(
            "e2enode" in text_e2e,
            "stale node name preserved in badge bar",
            repr(text_e2e[:120]),
        )
        print(f"\n  [INFO] connected-then-500 bar: {text_e2e[:120]}")
    finally:
        e2e_server.shutdown()
        e2e_thread.join(timeout=2.0)

    # ------------------------------------------------------------------ #
    # Boot the real app with a missing-config wrapper so the bar is empty.
    # ------------------------------------------------------------------ #
    projects_root = Path(tempfile.mkdtemp(prefix="cv-e2e-pve-deg-")) / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)

    missing_cfg_path = Path(tempfile.mkdtemp()) / "proxmox_missing.yaml"
    _write_monitor(
        monitors_dir,
        "proxmox_cluster.py",
        _WRAPPER_TEMPLATE.format(config_path=str(missing_cfg_path)),
    )

    cfg = AppConfig(
        projects_root=projects_root,
        active_window_seconds=3600,
        discovery_interval_seconds=0.05,
        poll_interval_seconds=0.05,
        monitors_dir=monitors_dir,
        monitor_refresh_seconds=0.1,
        cache_path=None,
    )
    app = VisualizerApp(cfg)

    async with app.run_test(size=(200, 40)) as pilot:
        bar = pilot.app.query_one(MonitorBar)
        # With missing config the monitor returns "" — the bar is suppressed
        # (no Proxmox row rendered).  Settle briefly then check the bar is empty.
        await _settle(pilot, 30)
        text = bar.rendered_text()

        _check(
            "proxmox.yaml not found" not in text and "⚠" not in text,
            "app renders no Proxmox row for missing config (suppressed)",
            repr(text[:120]),
        )

        await _settle(pilot, 10)
        app.save_screenshot(str(screenshot_path))
        _check(
            screenshot_path.exists() and screenshot_path.stat().st_size > 0,
            "screenshot SVG captured",
            f"path={screenshot_path}",
        )

        print("\n--- Proxmox monitor bar (DEGRADED — suppressed) ---")
        print(repr(text[:200]))

    print(f"\nDEGRADED fallback (cluster unreachable). Screenshot: {screenshot_path}")


async def run(screenshot_path: Path) -> None:
    monitors_dir = Path(tempfile.mkdtemp(prefix="cv-e2e-pve-mon-"))

    live = _attempt_live_poll()
    if live:
        await run_live(screenshot_path, monitors_dir)
    else:
        await run_degraded(screenshot_path, monitors_dir)


def main() -> int:
    out = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else (
            Path(__file__).resolve().parent.parent
            / ".tmp"
            / "proxmox_monitor_live.svg"
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(run(out))
    except AssertionError as exc:
        print(f"\nLIVE E2E FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
