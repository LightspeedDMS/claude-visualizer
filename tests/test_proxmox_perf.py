"""Tests for claude_visualizer/monitors/proxmox_perf.py — cluster-wide perf monitor.

Anti-mock (MESSI #1): NO unittest.mock / MagicMock.
- Snapshot aggregation: real local http.server on 127.0.0.1:0 serving known JSON.
- Rendering/colors: build PerfSnapshot directly and inspect Rich Text styles.
- Degraded states: no-config → ""; no snapshot → ""; stale → dim.
- Non-blocking: tick() against unreachable 127.0.0.1:1 returns < 0.5s.
"""

from __future__ import annotations

import json
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict

import pytest
from rich.console import Console
from rich.text import Text

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, content: str, filename: str = "proxmox.yaml") -> Path:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


def _yaml_for_url(url: str) -> str:
    return textwrap.dedent(f"""\
        nodes:
          - {url}
        token_id: root@pam!test
        token_secret: dummy
        poll_interval_seconds: 30
        alert_rotate_seconds: 10
        verify_ssl: false
        """)


# ---------------------------------------------------------------------------
# Real HTTP server fixture data — 3 online nodes + 1 offline node
#
# Online nodes: pve1, pve2, pve3
#   pve1: cpu=0.20, maxcpu=8,  mem=4G,  maxmem=16G
#   pve2: cpu=0.50, maxcpu=8,  mem=8G,  maxmem=16G
#   pve3: cpu=0.80, maxcpu=16, maxmem=32G, mem=16G
#
# Offline: pve4 — should NOT contribute to aggregation
#
# Expected aggregates (online only):
#   cores = 8 + 8 + 16 = 32
#   cpu_weighted = 0.20*8 + 0.50*8 + 0.80*16 = 1.6 + 4.0 + 12.8 = 18.4
#   cpu_pct = 18.4 / 32 * 100 = 57.5%
#   mem = 4G + 8G + 16G = 28G
#   maxmem = 16G + 16G + 32G = 64G
#   ram_pct = 28/64 * 100 = 43.75%
#   ram_free = 64G - 28G = 36G
#
# VMs: pve1 has 2 running qemu, pve4 offline qemu (should count in total)
#   running = 3 (2 qemu + 1 lxc)
#   total = 5 (3 running + 1 stopped qemu + 1 stopped lxc)
#
# Ceph pgmap: read_bytes_sec=10MB/s, write_bytes_sec=20MB/s,
#             read_op_per_sec=100, write_op_per_sec=200
#
# rrddata (per online node, last sample):
#   pve1: netin=1000, netout=2000, loadavg=1.0
#   pve2: netin=3000, netout=4000, loadavg=2.0
#   pve3: netin=5000, netout=6000, loadavg=3.0
#   Expected: net_in=9000, net_out=12000, load=mean(1.0,2.0,3.0)=2.0
# ---------------------------------------------------------------------------

_GB = 1024**3

_CLUSTER_RESOURCES = [
    # Online nodes
    {
        "type": "node",
        "id": "node/pve1",
        "node": "pve1",
        "status": "online",
        "cpu": 0.20,
        "maxcpu": 8,
        "mem": 4 * _GB,
        "maxmem": 16 * _GB,
        "disk": 100,
        "maxdisk": 1000,
    },
    {
        "type": "node",
        "id": "node/pve2",
        "node": "pve2",
        "status": "online",
        "cpu": 0.50,
        "maxcpu": 8,
        "mem": 8 * _GB,
        "maxmem": 16 * _GB,
        "disk": 200,
        "maxdisk": 1000,
    },
    {
        "type": "node",
        "id": "node/pve3",
        "node": "pve3",
        "status": "online",
        "cpu": 0.80,
        "maxcpu": 16,
        "mem": 16 * _GB,
        "maxmem": 32 * _GB,
        "disk": 300,
        "maxdisk": 2000,
    },
    # Offline node — must NOT contribute to CPU/RAM aggregation
    {
        "type": "node",
        "id": "node/pve4",
        "node": "pve4",
        "status": "offline",
        "cpu": 0.0,
        "maxcpu": 8,
        "mem": 0,
        "maxmem": 16 * _GB,
        "disk": 0,
        "maxdisk": 1000,
    },
    # VMs/containers
    {
        "type": "qemu",
        "id": "qemu/100",
        "name": "vm100",
        "status": "running",
        "node": "pve1",
    },
    {
        "type": "qemu",
        "id": "qemu/101",
        "name": "vm101",
        "status": "running",
        "node": "pve1",
    },
    {
        "type": "qemu",
        "id": "qemu/102",
        "name": "vm102",
        "status": "stopped",
        "node": "pve2",
    },
    {
        "type": "lxc",
        "id": "lxc/200",
        "name": "ct200",
        "status": "running",
        "node": "pve2",
    },
    {
        "type": "lxc",
        "id": "lxc/201",
        "name": "ct201",
        "status": "stopped",
        "node": "pve3",
    },
]

_CEPH_STATUS = {
    "health": {"status": "HEALTH_OK"},
    "pgmap": {
        "read_bytes_sec": 10 * 1024 * 1024,  # 10 MB/s
        "write_bytes_sec": 20 * 1024 * 1024,  # 20 MB/s
        "read_op_per_sec": 100,
        "write_op_per_sec": 200,
    },
}

# rrddata for each online node — last sample is the only one we care about
_RRDDATA_PVE1 = [
    {"netin": 0.0, "netout": 0.0, "loadavg": 0.5},
    {"netin": 1000.0, "netout": 2000.0, "loadavg": 1.0},
]
_RRDDATA_PVE2 = [
    {"netin": 0.0, "netout": 0.0, "loadavg": 0.5},
    {"netin": 3000.0, "netout": 4000.0, "loadavg": 2.0},
]
_RRDDATA_PVE3 = [
    {"netin": 0.0, "netout": 0.0, "loadavg": 0.5},
    {"netin": 5000.0, "netout": 6000.0, "loadavg": 3.0},
]


def _build_responses() -> Dict[str, Any]:
    """Build the full path→data mapping for the test HTTP server."""
    return {
        "/api2/json/cluster/resources": _CLUSTER_RESOURCES,
        "/api2/json/cluster/ceph/status": _CEPH_STATUS,
        # rrddata uses query params — handler matches by path prefix
        "/api2/json/nodes/pve1/rrddata": _RRDDATA_PVE1,
        "/api2/json/nodes/pve2/rrddata": _RRDDATA_PVE2,
        "/api2/json/nodes/pve3/rrddata": _RRDDATA_PVE3,
    }


class _PerfHandler(BaseHTTPRequestHandler):
    """Serves known JSON for perf monitor endpoints."""

    responses: Dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802
        # Strip query string for routing
        path = self.path.split("?")[0]
        data = self.responses.get(path)
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
        pass  # suppress log noise


def _start_server(responses: Dict[str, Any]) -> tuple:
    """Start a real HTTP server. Returns (server, port, thread)."""
    _PerfHandler.responses = responses

    server = HTTPServer(("127.0.0.1", 0), _PerfHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port, t


# ---------------------------------------------------------------------------
# Helper: build a PerfSnapshot for render/color tests
# ---------------------------------------------------------------------------


def _make_perf_snapshot(
    *,
    cpu_pct: float = 30.0,
    ram_pct: float = 40.0,
    ram_free_bytes: float = 36 * _GB,
    cores: int = 32,
    load_avg: float = 2.0,
    ceph_read_bps: float = 10 * 1024 * 1024,
    ceph_write_bps: float = 20 * 1024 * 1024,
    ceph_read_iops: float = 100.0,
    ceph_write_iops: float = 200.0,
    net_in_bps: float = 9000.0,
    net_out_bps: float = 12000.0,
    running_vms: int = 3,
    total_vms: int = 5,
    fetched_at: float = 1.0,
) -> Any:
    from claude_visualizer.monitors.proxmox_perf import PerfSnapshot

    return PerfSnapshot(
        cpu_pct=cpu_pct,
        ram_pct=ram_pct,
        ram_free_bytes=ram_free_bytes,
        cores=cores,
        load_avg=load_avg,
        ceph_read_bps=ceph_read_bps,
        ceph_write_bps=ceph_write_bps,
        ceph_read_iops=ceph_read_iops,
        ceph_write_iops=ceph_write_iops,
        net_in_bps=net_in_bps,
        net_out_bps=net_out_bps,
        running_vms=running_vms,
        total_vms=total_vms,
        fetched_at=fetched_at,
    )


# ===========================================================================
# TEST CLASSES
# ===========================================================================


class TestNoImportTextual:
    """proxmox_perf.py must NOT import textual at module level (pure monitor)."""

    def test_no_textual_import(self) -> None:
        """proxmox_perf.py must NOT import textual at module level (pure monitor)."""
        import claude_visualizer.monitors.proxmox_perf as perf_mod

        source = Path(perf_mod.__file__).read_text(encoding="utf-8")
        assert "import textual" not in source


# ---------------------------------------------------------------------------
# AC1 — Config load (reuses proxmox_cluster's config machinery)
# ---------------------------------------------------------------------------


class TestConfigLoad:
    """No proxmox.yaml → tick() returns ''."""

    def test_missing_yaml_tick_returns_empty_string(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        m = Monitor(config_path=tmp_path / "nonexistent.yaml")
        assert m.tick(1.0) == ""

    def test_valid_yaml_loads_config(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        assert m._config is not None

    def test_secret_not_leaked_in_repr(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        assert m._config is not None
        assert "dummy" not in str(m._config)
        assert "dummy" not in repr(m._config)


# ---------------------------------------------------------------------------
# AC2 — Never-connected → tick() returns ""
# ---------------------------------------------------------------------------


class TestNeverConnected:
    """Before first successful snapshot → tick() returns ''."""

    def test_no_snapshot_returns_empty_string(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        # port 1 is unreachable; snapshot never set
        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        result = m.tick(1.0)
        assert result == "", f"Expected '' when no snapshot, got {result!r}"


# ---------------------------------------------------------------------------
# AC3 — Snapshot aggregation (real HTTP server)
# ---------------------------------------------------------------------------


class TestSnapshotAggregation:
    """Drive Monitor against a real local HTTP server and assert aggregates."""

    def test_aggregates_cpu_pct(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            # cpu_pct = (0.20*8 + 0.50*8 + 0.80*16) / 32 * 100 = 57.5
            assert snap.cpu_pct == pytest.approx(57.5, abs=0.1)
        finally:
            server.shutdown()

    def test_aggregates_ram_pct(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            # ram_pct = 28G/64G * 100 = 43.75
            assert snap.ram_pct == pytest.approx(43.75, abs=0.1)
        finally:
            server.shutdown()

    def test_aggregates_ram_free(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            # ram_free = 64G - 28G = 36G
            expected_free = 36 * _GB
            assert snap.ram_free_bytes == pytest.approx(expected_free, rel=0.01)
        finally:
            server.shutdown()

    def test_aggregates_cores(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            assert snap.cores == 32  # 8+8+16 online only
        finally:
            server.shutdown()

    def test_aggregates_load_avg(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            # mean(1.0, 2.0, 3.0) = 2.0
            assert snap.load_avg == pytest.approx(2.0, abs=0.1)
        finally:
            server.shutdown()

    def test_aggregates_ceph_rates(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            assert snap.ceph_read_bps == pytest.approx(10 * 1024 * 1024, rel=0.01)
            assert snap.ceph_write_bps == pytest.approx(20 * 1024 * 1024, rel=0.01)
            assert snap.ceph_read_iops == pytest.approx(100.0, abs=0.1)
            assert snap.ceph_write_iops == pytest.approx(200.0, abs=0.1)
        finally:
            server.shutdown()

    def test_aggregates_net_rates(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            # net_in = 1000+3000+5000 = 9000
            # net_out = 2000+4000+6000 = 12000
            assert snap.net_in_bps == pytest.approx(9000.0, abs=1.0)
            assert snap.net_out_bps == pytest.approx(12000.0, abs=1.0)
        finally:
            server.shutdown()

    def test_aggregates_running_and_total_vms(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            # running: vm100, vm101, ct200 = 3
            # total: vm100,vm101,vm102,ct200,ct201 = 5
            assert snap.running_vms == 3
            assert snap.total_vms == 5
        finally:
            server.shutdown()

    def test_offline_node_excluded_from_cpu_ram(self, tmp_path: Path) -> None:
        """pve4 is offline — its cpu/mem must NOT contribute to aggregation."""
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            # cores must be 32 (not 40 which would include offline pve4's 8 cores)
            assert snap.cores == 32

        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# AC4 — render_perf_bar: plain text contains expected segments
# ---------------------------------------------------------------------------


class TestRenderPerfBar:
    """render_perf_bar produces the expected layout segments."""

    def test_contains_cpu_segment(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(cpu_pct=57.5)
        result = render_perf_bar(snap)
        assert isinstance(result, Text)
        assert "CPU" in result.plain

    def test_contains_ram_segment(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(ram_pct=43.0)
        result = render_perf_bar(snap)
        assert "RAM" in result.plain

    def test_contains_load_segment(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot()
        result = render_perf_bar(snap)
        assert "load" in result.plain.lower()

    def test_contains_ceph_segment(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot()
        result = render_perf_bar(snap)
        plain = result.plain
        assert "Ceph" in plain

    def test_contains_net_segment(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot()
        result = render_perf_bar(snap)
        assert "Net" in result.plain
        assert "↓" in result.plain
        assert "↑" in result.plain

    def test_contains_vms_segment(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(running_vms=3, total_vms=5)
        result = render_perf_bar(snap)
        plain = result.plain
        # Should show running/total
        assert "3" in plain and "5" in plain
        assert "VM" in plain

    def test_contains_free_ram(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(ram_free_bytes=36 * _GB)
        result = render_perf_bar(snap)
        assert "free" in result.plain

    def test_contains_separator_pipes(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot()
        result = render_perf_bar(snap)
        assert "│" in result.plain

    def test_fixed_width_cpu_pct_field(self) -> None:
        """CPU% is rendered as a fixed-width field to prevent jitter."""
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap_low = _make_perf_snapshot(cpu_pct=5.0)
        snap_high = _make_perf_snapshot(cpu_pct=100.0)
        plain_low = render_perf_bar(snap_low).plain
        plain_high = render_perf_bar(snap_high).plain
        # Find the position of 'RAM' in both — it should not shift
        pos_low = plain_low.find("RAM")
        pos_high = plain_high.find("RAM")
        assert (
            pos_low == pos_high
        ), f"'RAM' position shifted from {pos_low} to {pos_high} — CPU% not fixed-width"


# ---------------------------------------------------------------------------
# AC5 — Color thresholds
# ---------------------------------------------------------------------------


class TestColorThresholds:
    """CPU%/RAM% green <60, yellow >=60, red >=80; load relative to cores."""

    def _get_pct_style(self, render_fn: Any, field: str, pct: float, **kw: Any) -> str:
        result = render_fn(**kw)
        # Find the % value in the plain text, then look at the style at that offset
        plain = result.plain
        console = Console()
        # Find the numeric value right after "CPU " or "RAM "
        marker = f"{field} "
        idx = plain.find(marker)
        assert idx >= 0, f"'{field}' not found in {plain!r}"
        # The pct value follows immediately; scan for the digit position
        pos = idx + len(marker)
        # skip spaces
        while pos < len(plain) and plain[pos] == " ":
            pos += 1
        style = str(result.get_style_at_offset(console, pos))
        return style.lower()

    def test_cpu_low_is_green(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(cpu_pct=30.0)
        style = self._get_pct_style(render_perf_bar, "CPU", 30.0, snap=snap)
        assert "green" in style, f"CPU 30% should be green, got: {style!r}"

    def test_cpu_mid_is_yellow(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(cpu_pct=65.0)
        style = self._get_pct_style(render_perf_bar, "CPU", 65.0, snap=snap)
        assert "yellow" in style, f"CPU 65% should be yellow, got: {style!r}"

    def test_cpu_high_is_red(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(cpu_pct=85.0)
        style = self._get_pct_style(render_perf_bar, "CPU", 85.0, snap=snap)
        assert "red" in style, f"CPU 85% should be red, got: {style!r}"

    def test_ram_low_is_green(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(ram_pct=30.0)
        style = self._get_pct_style(render_perf_bar, "RAM", 30.0, snap=snap)
        assert "green" in style, f"RAM 30% should be green, got: {style!r}"

    def test_ram_mid_is_yellow(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(ram_pct=65.0)
        style = self._get_pct_style(render_perf_bar, "RAM", 65.0, snap=snap)
        assert "yellow" in style, f"RAM 65% should be yellow, got: {style!r}"

    def test_ram_high_is_red(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(ram_pct=85.0)
        style = self._get_pct_style(render_perf_bar, "RAM", 85.0, snap=snap)
        assert "red" in style, f"RAM 85% should be red, got: {style!r}"

    def test_load_low_is_green(self) -> None:
        """load 2.0 with 32 cores → load/cores=0.0625 < 0.7 → green."""
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(load_avg=2.0, cores=32)
        result = render_perf_bar(snap)
        plain = result.plain
        console = Console()
        # find "load " then the value
        idx = plain.lower().find("load ")
        assert idx >= 0
        pos = idx + len("load ")
        while pos < len(plain) and plain[pos] == " ":
            pos += 1
        style = str(result.get_style_at_offset(console, pos)).lower()
        assert "green" in style, f"load 2.0/32 cores should be green, got: {style!r}"

    def test_load_mid_is_yellow(self) -> None:
        """load 20.0 with 32 cores → 20/32=0.625 >= 0.7? No → try load 24."""
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        # 0.7*32 = 22.4 → load=23 should be yellow (>= 0.7*cores but < 1.0*cores)
        snap = _make_perf_snapshot(load_avg=23.0, cores=32)
        result = render_perf_bar(snap)
        plain = result.plain
        console = Console()
        idx = plain.lower().find("load ")
        pos = idx + len("load ")
        while pos < len(plain) and plain[pos] == " ":
            pos += 1
        style = str(result.get_style_at_offset(console, pos)).lower()
        assert "yellow" in style, f"load 23/32 should be yellow, got: {style!r}"

    def test_load_high_is_red(self) -> None:
        """load 32.0 with 32 cores → 1.0 >= 1.0*cores → red."""
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(load_avg=32.0, cores=32)
        result = render_perf_bar(snap)
        plain = result.plain
        console = Console()
        idx = plain.lower().find("load ")
        pos = idx + len("load ")
        while pos < len(plain) and plain[pos] == " ":
            pos += 1
        style = str(result.get_style_at_offset(console, pos)).lower()
        assert "red" in style, f"load 32/32 should be red, got: {style!r}"


# ---------------------------------------------------------------------------
# AC6 — Degraded: stale → dim
# ---------------------------------------------------------------------------


class TestStaleRendering:
    """When stale=True, render_perf_bar renders the whole line dim."""

    def test_stale_true_renders_dim(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot()
        result = render_perf_bar(snap, stale=True)
        console = Console()
        # Check the first character's style — it should be dim
        style = str(result.get_style_at_offset(console, 0)).lower()
        assert "dim" in style, f"stale=True must render line as dim, got: {style!r}"

    def test_stale_false_not_dim(self) -> None:
        from claude_visualizer.monitors.proxmox_perf import render_perf_bar

        snap = _make_perf_snapshot(cpu_pct=30.0)  # green CPU
        result = render_perf_bar(snap, stale=False)
        console = Console()
        # Find "CPU " in plain and check style at value position
        plain = result.plain
        idx = plain.find("CPU ")
        pos = idx + len("CPU ")
        while pos < len(plain) and plain[pos] == " ":
            pos += 1
        style = str(result.get_style_at_offset(console, pos)).lower()
        assert "dim" not in style, f"stale=False must not render dim, got: {style!r}"

    def test_monitor_stale_tick_returns_dim_line(self, tmp_path: Path) -> None:
        """Monitor with prior snapshot + failed latest fetch → renders dim."""
        from claude_visualizer.monitors.proxmox_perf import Monitor

        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        # Manually inject a snapshot so we're past "never connected"
        m._snapshot = _make_perf_snapshot()
        m._last_success_at = 0.0
        m._fetch_failed = True
        result = m.tick(100.0)
        # Should return a Text (the rendered bar)
        assert isinstance(result, Text), f"Expected Text for stale, got {type(result)}"
        console = Console()
        style = str(result.get_style_at_offset(console, 0)).lower()
        assert "dim" in style, f"Stale monitor must render dim line, got: {style!r}"


# ---------------------------------------------------------------------------
# B1 — Non-blocking tick
# ---------------------------------------------------------------------------


class TestNonBlockingTick:
    """tick() must return in <0.5s even against unreachable nodes."""

    def test_tick_returns_fast_against_unreachable(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        # port 1 is closed; requests will get ConnectionRefused immediately
        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)

        t0 = time.monotonic()
        m.tick(1.0)
        elapsed = time.monotonic() - t0

        assert (
            elapsed < 0.5
        ), f"tick() blocked for {elapsed:.3f}s — must be non-blocking (B1)"

    def test_second_tick_does_not_submit_while_in_flight(self, tmp_path: Path) -> None:
        """De-dupe: second tick while fetch in-flight does not update _last_poll."""
        from claude_visualizer.monitors.proxmox_perf import Monitor

        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        m.tick(1.0)
        first_poll = m._last_poll
        m.tick(1.1)
        assert m._last_poll == pytest.approx(
            first_poll
        ), "second tick while in-flight must not update _last_poll"


# ---------------------------------------------------------------------------
# AC7 — Tick collects result and populates _snapshot via real HTTP
# ---------------------------------------------------------------------------


class TestTickCollectsSnapshot:
    """tick() collects a completed future and populates _snapshot."""

    def test_tick_collects_snapshot_from_real_http(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)

            m.tick(0.0)
            assert m._in_flight is not None
            # Wait for fetch
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None

            # Second tick collects the result
            result = m.tick(0.0)
            assert m._snapshot is not None
            # Now we have a snapshot — result should be a Text bar, not ""
            assert isinstance(
                result, Text
            ), f"Expected Text after snapshot, got {result!r}"

        finally:
            server.shutdown()

    def test_rendered_bar_contains_correct_cpu(self, tmp_path: Path) -> None:
        """Full round-trip: real HTTP → snapshot → rendered bar has expected CPU%."""
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            m._in_flight.result(timeout=5.0)
            result = m.tick(0.0)
            assert isinstance(result, Text)
            plain = result.plain
            # CPU should be around 57-58%
            assert (
                "57" in plain or "58" in plain
            ), f"Expected ~57% CPU in bar, got: {plain!r}"
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# AC8 — Stale tracking: success clears dim, failure sets dim
# ---------------------------------------------------------------------------


class TestStaleTracking:
    """_fetch_failed cleared on success; set on failure."""

    def test_successful_fetch_clears_fetch_failed(self, tmp_path: Path) -> None:
        server, port, _ = _start_server(_build_responses())
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            # Pre-set as failed
            m._fetch_failed = True

            m.tick(0.0)
            m._in_flight.result(timeout=5.0)
            m.tick(0.0)  # collect

            assert m._fetch_failed is False, "_fetch_failed must be cleared on success"
        finally:
            server.shutdown()

    def test_failed_fetch_sets_fetch_failed(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        # Pre-inject a snapshot so failure is detectable (vs never-connected)
        m._snapshot = _make_perf_snapshot()

        m.tick(0.0)
        if m._in_flight is not None:
            try:
                m._in_flight.result(timeout=3.0)
            except Exception:
                pass
        # Now drive a collect tick
        m.tick(0.0)

        assert m._fetch_failed is True, "_fetch_failed must be True after failed fetch"


# ---------------------------------------------------------------------------
# AC9 — Poll throttle: respects poll_interval_seconds
# ---------------------------------------------------------------------------


class TestPollThrottle:
    """tick() respects poll_interval_seconds (same pattern as health monitor)."""

    def test_no_poll_before_interval(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        m.tick(0.0)
        last = m._last_poll
        m.tick(15.0)  # 15 < poll_interval(30)
        assert m._last_poll == pytest.approx(last)

    def test_poll_after_interval(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_perf import Monitor

        cfg = _write_yaml(tmp_path, _yaml_for_url("http://127.0.0.1:1"))
        m = Monitor(config_path=cfg)
        m.tick(0.0)
        if m._in_flight is not None:
            try:
                m._in_flight.result(timeout=3.0)
            except Exception:
                pass
        m.tick(31.0)
        assert m._last_poll == pytest.approx(31.0)


# ---------------------------------------------------------------------------
# AC10 — Missing rrddata fields treated as 0
# ---------------------------------------------------------------------------


class TestMissingRrddataFields:
    """Missing netin/netout/loadavg fields in rrddata treated as 0."""

    def test_missing_fields_treated_as_zero(self, tmp_path: Path) -> None:
        # Server returns rrddata with no netin/netout/loadavg
        _GB_local = 1024**3
        resources = [
            {
                "type": "node",
                "id": "node/pve1",
                "node": "pve1",
                "status": "online",
                "cpu": 0.20,
                "maxcpu": 4,
                "mem": 2 * _GB_local,
                "maxmem": 8 * _GB_local,
            },
        ]
        ceph = {
            "health": {"status": "HEALTH_OK"},
            "pgmap": {
                "read_bytes_sec": 0,
                "write_bytes_sec": 0,
                "read_op_per_sec": 0,
                "write_op_per_sec": 0,
            },
        }
        rrd = [{"time": 1}]  # no netin/netout/loadavg keys

        responses = {
            "/api2/json/cluster/resources": resources,
            "/api2/json/cluster/ceph/status": ceph,
            "/api2/json/nodes/pve1/rrddata": rrd,
        }

        server, port, _ = _start_server(responses)
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None
            assert snap.net_in_bps == pytest.approx(0.0, abs=0.1)
            assert snap.net_out_bps == pytest.approx(0.0, abs=0.1)
            assert snap.load_avg == pytest.approx(0.0, abs=0.1)
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# AC11 — Ceph endpoint absent → Ceph fields zero, no crash
# ---------------------------------------------------------------------------


class TestCephEndpointAbsent:
    """If /cluster/ceph/status fails (404/500), Ceph fields are 0."""

    def test_no_ceph_endpoint_returns_zero_rates(self, tmp_path: Path) -> None:
        _GB_local = 1024**3
        resources = [
            {
                "type": "node",
                "id": "node/pve1",
                "node": "pve1",
                "status": "online",
                "cpu": 0.10,
                "maxcpu": 4,
                "mem": 1 * _GB_local,
                "maxmem": 8 * _GB_local,
            },
        ]
        rrd = [{"netin": 100.0, "netout": 200.0, "loadavg": 0.5}]

        # Intentionally omit ceph/status so server returns 404
        responses = {
            "/api2/json/cluster/resources": resources,
            "/api2/json/nodes/pve1/rrddata": rrd,
        }

        server, port, _ = _start_server(responses)
        try:
            from claude_visualizer.monitors.proxmox_perf import Monitor

            cfg = _write_yaml(tmp_path, _yaml_for_url(f"http://127.0.0.1:{port}"))
            m = Monitor(config_path=cfg)
            m.tick(0.0)
            snap = m._in_flight.result(timeout=5.0)
            assert snap is not None, "snapshot must be returned even when Ceph 404s"
            assert snap.ceph_read_bps == pytest.approx(0.0, abs=0.01)
            assert snap.ceph_write_bps == pytest.approx(0.0, abs=0.01)
        finally:
            server.shutdown()
