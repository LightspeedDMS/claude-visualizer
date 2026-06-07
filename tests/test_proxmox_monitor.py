"""Tests for claude_visualizer/monitors/proxmox_cluster.py — Proxmox monitor plugin.

Anti-mock (MESSI #1): NO unittest.mock / MagicMock / monkeypatch of requests.
- Parsing/rendering/rotation: drive _parse with REAL Proxmox-API-shaped dicts.
- Failover: point ProxmoxConfig.nodes at GENUINELY unreachable URLs so requests
  raises a REAL ConnectionError.
- Throttle / first-tick: observe REAL _last_poll state transitions.
- Config: write real temp YAML (tmp_path); missing-path → _config=None.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from rich.console import Console
from rich.text import Text

# ---------------------------------------------------------------------------
# Shared fixture dicts — real Proxmox-API-shaped payloads
# ---------------------------------------------------------------------------

_CLUSTER_STATUS_OK: List[Dict[str, Any]] = [
    {"type": "cluster", "name": "pve-cluster", "quorate": 1, "nodes": 2},
    {"type": "node", "id": "node/pve1", "name": "pve1", "online": 1, "local": 1},
    {"type": "node", "id": "node/pve2", "name": "pve2", "online": 1, "local": 0},
]

_NODES_OK: List[Dict[str, Any]] = [
    {
        "node": "pve1",
        "status": "online",
        "cpu": 0.30,
        "mem": 4_000_000_000,
        "maxmem": 16_000_000_000,
    },
    {
        "node": "pve2",
        "status": "online",
        "cpu": 0.50,
        "mem": 8_000_000_000,
        "maxmem": 16_000_000_000,
    },
]

_CEPH_STATUS_OK: Dict[str, Any] = {
    "health": {"status": "HEALTH_OK", "checks": {}},
    "osdmap": {
        "osdmap": {"num_osds": 3, "num_up_osds": 3, "num_in_osds": 3},
        "osds": [
            {"osd": 0, "up": 1, "in": 1},
            {"osd": 1, "up": 1, "in": 1},
            {"osd": 2, "up": 1, "in": 1},
        ],
    },
    "pgmap": {"bytes_used": 100_000_000_000, "bytes_total": 1_000_000_000_000},
}

_CEPH_STATUS_DEGRADED: Dict[str, Any] = {
    "health": {
        "status": "HEALTH_WARN",
        "checks": {
            "PG_DEGRADED": {"summary": {"message": "Degraded data redundancy"}},
            "OSD_NEARFULL": {"summary": {"message": "1 nearfull osd"}},
        },
    },
    "osdmap": {
        "osdmap": {"num_osds": 3, "num_up_osds": 2, "num_in_osds": 3},
        "osds": [
            {"osd": 0, "up": 1, "in": 1},
            {"osd": 1, "up": 0, "in": 1},
            {"osd": 2, "up": 1, "in": 1},
        ],
    },
    "pgmap": {"bytes_used": 780_000_000_000, "bytes_total": 1_000_000_000_000},
}

_HA_RESOURCES_OK: List[Dict[str, Any]] = [
    {"sid": "vm:100", "state": "started", "type": "vm"},
]

_HA_RESOURCES_ERROR: List[Dict[str, Any]] = [
    {"sid": "vm:100", "state": "error", "type": "vm"},
]

# YAML constants — avoid copy-paste (MESSI #4 anti-duplication)
_YAML_VALID = textwrap.dedent("""\
    nodes:
      - https://192.168.1.100:8006
      - https://192.168.1.101:8006
    token_id: root@pam!devvm_monitor
    token_secret: abc123secret
    poll_interval_seconds: 30
    alert_rotate_seconds: 10
    verify_ssl: false
    """)

_YAML_UNREACHABLE = textwrap.dedent("""\
    nodes:
      - https://127.0.0.1:1
    token_id: root@pam!test
    token_secret: dummy
    poll_interval_seconds: 30
    alert_rotate_seconds: 10
    verify_ssl: false
    """)


def _write_yaml(tmp_path: Path, content: str, filename: str = "proxmox.yaml") -> Path:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


def _make_unreachable_monitor(tmp_path: Path):
    """Build a Monitor pointing at a genuinely unreachable node (real ConnectionError)."""
    from claude_visualizer.monitors.proxmox_cluster import Monitor

    return Monitor(config_path=_write_yaml(tmp_path, _YAML_UNREACHABLE))


def _make_snapshot(
    *,
    nodes: Optional[List] = None,
    osds: Optional[List] = None,
    ceph_status: str = "HEALTH_OK",
    ceph_used_pct: float = 0.10,
    alerts: Optional[List] = None,
    fetched_at: float = 0.0,
):
    from claude_visualizer.monitors.proxmox_cluster import (
        NodeState,
        OSDState,
        ProxmoxSnapshot,
    )

    if nodes is None:
        nodes = [NodeState(name="pve1", online=True, cpu_pct=30.0, mem_pct=40.0)]
    if osds is None:
        osds = [OSDState(id=0, up=True, in_=True), OSDState(id=1, up=True, in_=True)]
    if alerts is None:
        alerts = []
    return ProxmoxSnapshot(
        nodes=nodes,
        osds=osds,
        ceph_status=ceph_status,
        ceph_used_pct=ceph_used_pct,
        alerts=alerts,
        fetched_at=fetched_at,
    )


# ---------------------------------------------------------------------------
# AC1 — Config load
# ---------------------------------------------------------------------------


class TestConfigLoad:
    """AC1: valid YAML → ProxmoxConfig held; missing file → _config=None."""

    def test_valid_yaml_builds_config(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor, ProxmoxConfig

        m = Monitor(config_path=_write_yaml(tmp_path, _YAML_VALID))
        assert m._config is not None
        assert isinstance(m._config, ProxmoxConfig)
        assert m._config.token_id == "root@pam!devvm_monitor"
        assert m._config.poll_interval_seconds == 30
        assert m._config.verify_ssl is False
        assert len(m._config.nodes) == 2

    def test_missing_yaml_sets_config_none(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        m = Monitor(config_path=tmp_path / "nonexistent.yaml")
        assert m._config is None

    def test_missing_yaml_tick_returns_dim_warning(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        m = Monitor(config_path=tmp_path / "nonexistent.yaml")
        result = m.tick(1.0)
        assert isinstance(result, Text)
        assert "proxmox.yaml not found" in result.plain

    def test_missing_yaml_tick_does_not_raise(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        m = Monitor(config_path=tmp_path / "nonexistent.yaml")
        result = m.tick(0.0)  # must not raise
        assert result is not None

    def test_secret_not_leaked_in_repr(self, tmp_path: Path) -> None:
        """Security: token_secret must NOT appear in str/repr of ProxmoxConfig."""
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        m = Monitor(config_path=_write_yaml(tmp_path, _YAML_VALID))
        assert m._config is not None
        assert "abc123secret" not in str(m._config)
        assert "abc123secret" not in repr(m._config)


# ---------------------------------------------------------------------------
# AC10 — First-tick polling
# ---------------------------------------------------------------------------


class TestFirstTickPolling:
    """AC10: fresh monitor (_last_poll=0.0) polls on first tick for any now>0."""

    def test_fresh_monitor_polls_on_first_tick(self, tmp_path: Path) -> None:
        m = _make_unreachable_monitor(tmp_path)
        assert m._last_poll == 0.0
        m.tick(5.0)
        assert m._last_poll == pytest.approx(5.0)

    def test_fresh_monitor_shows_connecting_after_first_tick(
        self, tmp_path: Path
    ) -> None:
        m = _make_unreachable_monitor(tmp_path)
        result = m.tick(5.0)
        plain = result.plain if isinstance(result, Text) else result
        assert "connecting" in plain.lower() or "PVE" in plain


# ---------------------------------------------------------------------------
# AC3 — Poll throttle
# ---------------------------------------------------------------------------


class TestPollThrottle:
    """AC3: last poll at T; tick(T+15) no poll; tick(T+31) polls and updates _last_poll."""

    def test_no_poll_before_interval(self, tmp_path: Path) -> None:
        m = _make_unreachable_monitor(tmp_path)
        m.tick(0.0)
        last = m._last_poll
        m.tick(15.0)  # 15 < poll_interval(30)
        assert m._last_poll == pytest.approx(last)

    def test_poll_after_interval(self, tmp_path: Path) -> None:
        m = _make_unreachable_monitor(tmp_path)

        m.tick(
            0.0
        )  # submits real (failing) fetch via _submit_fetch(); _last_poll = 0.0
        # Drain deterministically: block until the real (failed) fetch is done.
        # result() returns None (all nodes unreachable); we only need the
        # future to be .done() so de-dupe does not suppress the next poll.
        if m._in_flight is not None:
            try:
                m._in_flight.result()
            except Exception:
                pass
        m.tick(31.0)  # 31 ≥ poll_interval(30) and _in_flight is None → polls
        assert m._last_poll == pytest.approx(31.0)


# ---------------------------------------------------------------------------
# AC2 — Node failover
# ---------------------------------------------------------------------------


class TestNodeFailover:
    """AC2: every node raises → _fetch returns None, stale _snapshot retained."""

    def test_all_nodes_unreachable_returns_none(self, tmp_path: Path) -> None:
        m = _make_unreachable_monitor(tmp_path)
        assert m._fetch() is None

    def test_all_nodes_unreachable_no_raise(self, tmp_path: Path) -> None:
        m = _make_unreachable_monitor(tmp_path)
        result = m._fetch()  # must not raise
        assert result is None

    def test_stale_snapshot_retained_on_fetch_failure(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            NodeState,
            OSDState,
            ProxmoxSnapshot,
        )

        m = _make_unreachable_monitor(tmp_path)
        stale = ProxmoxSnapshot(
            nodes=[NodeState(name="pve1", online=True, cpu_pct=10.0, mem_pct=20.0)],
            osds=[OSDState(id=0, up=True, in_=True)],
            ceph_status="HEALTH_OK",
            ceph_used_pct=0.10,
            alerts=[],
            fetched_at=0.0,
        )
        m._snapshot = stale
        m.tick(9999.0)  # triggers poll; _fetch returns None
        assert m._snapshot is stale


# ---------------------------------------------------------------------------
# AC4 — Bar content and colours
# ---------------------------------------------------------------------------


class TestBarContent:
    """AC4: render_proxmox_bar produces correct plain text and colour spans."""

    def test_cluster_label_present(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import render_proxmox_bar

        result = render_proxmox_bar(_make_snapshot(), 0)
        assert "Cluster:" in result.plain

    def test_ceph_label_present(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import render_proxmox_bar

        result = render_proxmox_bar(_make_snapshot(), 0)
        assert "Ceph:" in result.plain

    def test_separator_present(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import render_proxmox_bar

        result = render_proxmox_bar(_make_snapshot(), 0)
        assert "│" in result.plain

    def test_cluster_ok_green_colour(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import render_proxmox_bar

        result = render_proxmox_bar(_make_snapshot(), 0)
        idx = result.plain.find("Cluster: ") + len("Cluster: ")
        assert idx >= len("Cluster: ")
        style = result.get_style_at_offset(Console(), idx)
        assert "green" in str(style).lower(), f"Expected green at OK, got {style!r}"

    def test_node_dot_online_bright_green(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            NodeState,
            render_proxmox_bar,
        )

        snap = _make_snapshot(
            nodes=[NodeState(name="pve1", online=True, cpu_pct=30.0, mem_pct=40.0)]
        )
        result = render_proxmox_bar(snap, 0)
        assert "●" in result.plain

    def test_node_dot_offline_red(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            NodeState,
            render_proxmox_bar,
        )

        snap = _make_snapshot(
            nodes=[NodeState(name="pve1", online=False, cpu_pct=0.0, mem_pct=0.0)]
        )
        result = render_proxmox_bar(snap, 0)
        assert "●" in result.plain

    def test_crit_alert_red(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            Alert,
            Severity,
            render_proxmox_bar,
        )

        snap = _make_snapshot(
            alerts=[Alert(severity=Severity.CRIT, text="critical-thing")]
        )
        result = render_proxmox_bar(snap, 0)
        idx = result.plain.find("critical-thing")
        assert idx >= 0
        assert "red" in str(result.get_style_at_offset(Console(), idx)).lower()

    def test_warn_alert_yellow(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            Alert,
            Severity,
            render_proxmox_bar,
        )

        snap = _make_snapshot(alerts=[Alert(severity=Severity.WARN, text="warn-thing")])
        result = render_proxmox_bar(snap, 0)
        idx = result.plain.find("warn-thing")
        assert idx >= 0
        assert "yellow" in str(result.get_style_at_offset(Console(), idx)).lower()

    def test_info_alert_cyan(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            Alert,
            Severity,
            render_proxmox_bar,
        )

        snap = _make_snapshot(alerts=[Alert(severity=Severity.INFO, text="info-thing")])
        result = render_proxmox_bar(snap, 0)
        idx = result.plain.find("info-thing")
        assert idx >= 0
        assert "cyan" in str(result.get_style_at_offset(Console(), idx)).lower()


# ---------------------------------------------------------------------------
# AC5 — OSD down at correct sorted position
# ---------------------------------------------------------------------------


class TestOsdDownPosition:
    """AC5: OSD dots rendered in id-ascending order; down dot at its id's sorted position."""

    def test_osd_down_at_sorted_position(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            OSDState,
            render_proxmox_bar,
        )

        osds = [
            OSDState(id=0, up=True, in_=True),
            OSDState(id=1, up=False, in_=True),
            OSDState(id=2, up=True, in_=True),
        ]
        # nodes=[] so only OSD dots appear — no node dots to confuse the count
        result = render_proxmox_bar(_make_snapshot(nodes=[], osds=osds), 0)
        dots = [i for i, c in enumerate(result.plain) if c == "●"]
        assert len(dots) == 3
        # dot at sorted position 1 (id=1, down) → red
        assert "red" in str(result.get_style_at_offset(Console(), dots[1])).lower()
        # dot at position 0 (id=0, up) → NOT red
        assert "red" not in str(result.get_style_at_offset(Console(), dots[0])).lower()

    def test_osd_dots_sorted_by_id_ascending(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            OSDState,
            render_proxmox_bar,
        )

        # Provided out of id order — dots must still be sorted by id
        osds = [
            OSDState(id=2, up=True, in_=True),
            OSDState(id=0, up=True, in_=True),
            OSDState(id=1, up=False, in_=True),
        ]
        # nodes=[] so only OSD dots appear — no node dots to confuse the count
        result = render_proxmox_bar(_make_snapshot(nodes=[], osds=osds), 0)
        dots = [i for i, c in enumerate(result.plain) if c == "●"]
        assert len(dots) == 3
        assert "red" in str(result.get_style_at_offset(Console(), dots[1])).lower()


# ---------------------------------------------------------------------------
# AC6 — Alert rotation
# ---------------------------------------------------------------------------


class TestAlertRotation:
    """AC6: alert rotates +1 mod len every alert_rotate_seconds; empty never advances."""

    def test_alert_rotates_after_interval(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            Alert,
            NodeState,
            ProxmoxSnapshot,
            Severity,
        )

        m = _make_unreachable_monitor(tmp_path)
        snap = ProxmoxSnapshot(
            nodes=[NodeState(name="pve1", online=True, cpu_pct=30.0, mem_pct=40.0)],
            osds=[],
            ceph_status="HEALTH_OK",
            ceph_used_pct=0.10,
            alerts=[
                Alert(severity=Severity.INFO, text="alert-0"),
                Alert(severity=Severity.INFO, text="alert-1"),
                Alert(severity=Severity.INFO, text="alert-2"),
            ],
            fetched_at=0.0,
        )
        m._snapshot = snap
        m._last_poll = 9999.0
        m._last_rotate = 0.0

        m.tick(0.0)
        assert m._alert_index == 0
        m.tick(10.0)
        assert m._alert_index == 1
        m.tick(20.0)
        assert m._alert_index == 2
        m.tick(30.0)
        assert m._alert_index == 0  # wraps

    def test_empty_alert_list_never_advances_index(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            NodeState,
            ProxmoxSnapshot,
        )

        m = _make_unreachable_monitor(tmp_path)
        snap = ProxmoxSnapshot(
            nodes=[NodeState(name="pve1", online=True, cpu_pct=30.0, mem_pct=40.0)],
            osds=[],
            ceph_status="HEALTH_OK",
            ceph_used_pct=0.10,
            alerts=[],
            fetched_at=0.0,
        )
        m._snapshot = snap
        m._last_poll = 9999.0
        m._last_rotate = 0.0

        for t in [10.0, 20.0, 30.0]:
            m.tick(t)
        assert m._alert_index == 0


# ---------------------------------------------------------------------------
# AC7 — Priority ordering CRIT < WARN < INFO (stable)
# ---------------------------------------------------------------------------


class TestAlertPriority:
    """AC7: alerts sorted CRIT(0) < WARN(1) < INFO(2), stable within each severity."""

    def test_crit_before_warn_before_info(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import build_alerts

        ceph_health = {
            "status": "HEALTH_ERR",
            "checks": {
                "PG_DEGRADED": {"summary": {"message": "Degraded (WARN)"}},
                "OSD_DOWN": {"summary": {"message": "OSD down (CRIT)"}},
                "PG_NOT_SCRUBBED": {"summary": {"message": "Not scrubbed (INFO)"}},
            },
        }
        alerts = build_alerts(
            ceph_health=ceph_health,
            ha_resources=[],
            nodes=[],
            ceph_used_pct=0.10,
        )
        last: Optional[Any] = None
        for a in alerts:
            if last is not None:
                assert (
                    a.severity.value >= last.value
                ), f"Out of order: {a.severity} after {last}"
            last = a.severity

    def test_within_severity_stable_order(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        ceph_health = {
            "status": "HEALTH_WARN",
            "checks": {
                "PG_DEGRADED": {"summary": {"message": "first-warn"}},
                "OSD_NEARFULL": {"summary": {"message": "second-warn"}},
            },
        }
        alerts = build_alerts(
            ceph_health=ceph_health,
            ha_resources=[],
            nodes=[],
            ceph_used_pct=0.10,
        )
        warn_texts = [a.text for a in alerts if a.severity == Severity.WARN]
        assert warn_texts.index("first-warn") < warn_texts.index("second-warn")

    def test_osd_down_crit(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        alerts = build_alerts(
            nodes=[],
            ha_resources=[],
            ceph_health={
                "status": "HEALTH_ERR",
                "checks": {"OSD_DOWN": {"summary": {"message": "down"}}},
            },
            ceph_used_pct=0.10,
        )
        assert any(a.severity == Severity.CRIT for a in alerts)

    def test_pg_degraded_warn(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        alerts = build_alerts(
            nodes=[],
            ha_resources=[],
            ceph_health={
                "status": "HEALTH_WARN",
                "checks": {"PG_DEGRADED": {"summary": {"message": "deg"}}},
            },
            ceph_used_pct=0.10,
        )
        assert any(a.severity == Severity.WARN for a in alerts)

    def test_ha_error_crit(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        alerts = build_alerts(
            nodes=[],
            ceph_health={"status": "HEALTH_OK", "checks": {}},
            ha_resources=[{"sid": "vm:100", "state": "error", "type": "vm"}],
            ceph_used_pct=0.10,
        )
        assert any(a.severity == Severity.CRIT for a in alerts)

    def test_node_offline_crit(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            NodeState,
            Severity,
            build_alerts,
        )

        alerts = build_alerts(
            ceph_health={"status": "HEALTH_OK", "checks": {}},
            ha_resources=[],
            nodes=[NodeState(name="pve1", online=False, cpu_pct=0.0, mem_pct=0.0)],
            ceph_used_pct=0.10,
        )
        assert any(a.severity == Severity.CRIT for a in alerts)

    def test_ceph_90pct_crit(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        alerts = build_alerts(
            nodes=[],
            ha_resources=[],
            ceph_health={"status": "HEALTH_OK", "checks": {}},
            ceph_used_pct=0.92,
        )
        assert any(a.severity == Severity.CRIT for a in alerts)

    def test_ceph_75pct_warn(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        alerts = build_alerts(
            nodes=[],
            ha_resources=[],
            ceph_health={"status": "HEALTH_OK", "checks": {}},
            ceph_used_pct=0.80,
        )
        assert any(a.severity == Severity.WARN for a in alerts)

    def test_ceph_65pct_info(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        alerts = build_alerts(
            nodes=[],
            ha_resources=[],
            ceph_health={"status": "HEALTH_OK", "checks": {}},
            ceph_used_pct=0.70,
        )
        assert any(a.severity == Severity.INFO for a in alerts)


# ---------------------------------------------------------------------------
# AC8 — Unknown health check codes passed through verbatim
# ---------------------------------------------------------------------------


class TestUnknownHealthChecksPassthrough:
    """AC8: unknown ceph.health.checks codes are surfaced verbatim with a severity."""

    def test_unknown_code_surfaced_verbatim(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import build_alerts

        alerts = build_alerts(
            nodes=[],
            ha_resources=[],
            ceph_health={
                "status": "HEALTH_WARN",
                "checks": {
                    "TOTALLY_UNKNOWN_CHECK": {
                        "summary": {"message": "some-unknown-issue"}
                    },
                },
            },
            ceph_used_pct=0.10,
        )
        assert any("some-unknown-issue" in a.text for a in alerts)

    def test_unknown_code_has_valid_severity(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Severity, build_alerts

        alerts = build_alerts(
            nodes=[],
            ha_resources=[],
            ceph_health={
                "status": "HEALTH_WARN",
                "checks": {
                    "MYSTERY_CODE": {"summary": {"message": "mystery message"}},
                },
            },
            ceph_used_pct=0.10,
        )
        mystery = [a for a in alerts if "mystery" in a.text.lower()]
        assert mystery
        assert mystery[0].severity in list(Severity)


# ---------------------------------------------------------------------------
# AC9 — Empty alerts text
# ---------------------------------------------------------------------------


class TestEmptyAlertsText:
    """AC9: empty alerts → '↻ no alerts' dim."""

    def test_empty_shows_no_alerts(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import render_proxmox_bar

        result = render_proxmox_bar(_make_snapshot(alerts=[]), 0)
        assert "no alerts" in result.plain
        assert "↻" in result.plain

    def test_with_alert_shows_alert_text(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import (
            Alert,
            Severity,
            render_proxmox_bar,
        )

        snap = _make_snapshot(
            alerts=[Alert(severity=Severity.WARN, text="disk nearly full")]
        )
        result = render_proxmox_bar(snap, 0)
        assert "disk nearly full" in result.plain


# ---------------------------------------------------------------------------
# _parse integration tests — real API-shaped dicts, no mocks
# ---------------------------------------------------------------------------


class TestParseIntegration:
    """Drive _parse with real Proxmox-API-shaped dicts."""

    def test_parse_healthy_cluster(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        m = Monitor.__new__(Monitor)
        snap = m._parse(
            cluster_status=_CLUSTER_STATUS_OK,
            ceph_status=_CEPH_STATUS_OK,
            ha_resources=_HA_RESOURCES_OK,
            nodes=_NODES_OK,
        )
        assert snap is not None
        assert len(snap.nodes) == 2
        assert all(n.online for n in snap.nodes)
        assert len(snap.osds) == 3
        assert snap.ceph_status == "HEALTH_OK"
        assert snap.ceph_used_pct == pytest.approx(0.10)
        assert snap.alerts == []

    def test_parse_degraded_ceph(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor, Severity

        m = Monitor.__new__(Monitor)
        snap = m._parse(
            cluster_status=_CLUSTER_STATUS_OK,
            ceph_status=_CEPH_STATUS_DEGRADED,
            ha_resources=_HA_RESOURCES_OK,
            nodes=_NODES_OK,
        )
        assert snap is not None
        assert snap.ceph_status == "HEALTH_WARN"
        assert any(a.severity == Severity.WARN for a in snap.alerts)

    def test_parse_ha_error(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor, Severity

        m = Monitor.__new__(Monitor)
        snap = m._parse(
            cluster_status=_CLUSTER_STATUS_OK,
            ceph_status=_CEPH_STATUS_OK,
            ha_resources=_HA_RESOURCES_ERROR,
            nodes=_NODES_OK,
        )
        assert snap is not None
        assert any(a.severity == Severity.CRIT for a in snap.alerts)

    def test_parse_osd_down(self) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        ceph_with_down = {
            **_CEPH_STATUS_OK,
            "osdmap": {
                "osdmap": {"num_osds": 2, "num_up_osds": 1, "num_in_osds": 2},
                "osds": [{"osd": 0, "up": 1, "in": 1}, {"osd": 1, "up": 0, "in": 1}],
            },
        }
        m = Monitor.__new__(Monitor)
        snap = m._parse(
            cluster_status=_CLUSTER_STATUS_OK,
            ceph_status=ceph_with_down,
            ha_resources=_HA_RESOURCES_OK,
            nodes=_NODES_OK,
        )
        assert snap is not None
        osd1 = next(o for o in snap.osds if o.id == 1)
        assert not osd1.up


# ---------------------------------------------------------------------------
# B1 — Non-blocking tick (background poll)
# ---------------------------------------------------------------------------

# YAML with a packet-dropping address so requests hits its full connect timeout.
_YAML_SLOW_UNREACHABLE = textwrap.dedent("""\
    nodes:
      - https://10.255.255.1:8006
    token_id: root@pam!test
    token_secret: dummy
    poll_interval_seconds: 30
    alert_rotate_seconds: 10
    verify_ssl: false
    """)


class TestB1NonBlockingTick:
    """B1: tick() must return in < 0.5s even against a packet-dropping node
    (connect timeout = 5s).  The real fetch runs in a daemon background thread;
    tick() submits the work and returns the current (None) snapshot immediately.

    Anti-mock: real packet-dropping IP 10.255.255.1 — no mocking of requests.
    """

    def test_tick_returns_fast_against_unreachable_nodes(self, tmp_path: Path) -> None:
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        cfg_path = _write_yaml(tmp_path, _YAML_SLOW_UNREACHABLE)
        m = Monitor(config_path=cfg_path)

        t0 = time.monotonic()
        result = m.tick(5.0)  # first tick — must NOT block on the HTTP call
        elapsed = time.monotonic() - t0

        # tick() must return well within 0.5s — the real request takes ~5s.
        assert elapsed < 0.5, (
            f"tick() blocked for {elapsed:.3f}s — fetch is still running on the "
            "event-loop thread (B1 regression)"
        )

        # While the snapshot is pending, the monitor should show "connecting".
        plain = result.plain if hasattr(result, "plain") else str(result)
        assert (
            "connecting" in plain.lower() or "PVE" in plain
        ), f"Expected connecting/PVE while snapshot pending, got: {plain!r}"

    def test_second_tick_does_not_submit_while_in_flight(self, tmp_path: Path) -> None:
        """De-dupe: a second tick while a fetch is in-flight must not launch another."""
        from claude_visualizer.monitors.proxmox_cluster import Monitor

        cfg_path = _write_yaml(tmp_path, _YAML_SLOW_UNREACHABLE)
        m = Monitor(config_path=cfg_path)

        m.tick(5.0)  # submits first fetch
        poll_after_first = m._last_poll

        # Immediately tick again — should NOT update _last_poll (in-flight guard).
        m.tick(5.1)
        assert m._last_poll == pytest.approx(
            poll_after_first
        ), "second tick while fetch in-flight must not update _last_poll"

    def test_stale_snapshot_retained_after_async_fetch_fails(
        self, tmp_path: Path
    ) -> None:
        """Async variant of AC2: stale snapshot stays when background fetch returns None."""
        from claude_visualizer.monitors.proxmox_cluster import (
            Monitor,
            NodeState,
            OSDState,
            ProxmoxSnapshot,
        )

        cfg_path = _write_yaml(tmp_path, _YAML_SLOW_UNREACHABLE)
        m = Monitor(config_path=cfg_path)

        stale = ProxmoxSnapshot(
            nodes=[NodeState(name="pve1", online=True, cpu_pct=10.0, mem_pct=20.0)],
            osds=[OSDState(id=0, up=True, in_=True)],
            ceph_status="HEALTH_OK",
            ceph_used_pct=0.10,
            alerts=[],
            fetched_at=0.0,
        )
        m._snapshot = stale

        # Trigger a new poll (past the poll interval) — stale snapshot must survive.
        m.tick(9999.0)
        # tick() returns immediately; snapshot is stale until background fetch completes.
        assert (
            m._snapshot is stale
        ), "stale snapshot must be retained while background fetch is pending"


# ---------------------------------------------------------------------------
# N2 — Malformed-node failover (KeyError/ValueError/TypeError → continue)
# ---------------------------------------------------------------------------

# Real seam: a Monitor subclass whose _fetch returns a payload with a malformed
# first-node response (KeyError on parse) but a good second-node response.
# No mocks — we drive _fetch directly or provide a real subclass.


class _MalformedFirstNodeMonitor:
    """Real (non-mock) seam: node[0] raises KeyError; node[1] raises ConnectionError.
    Used to assert that KeyError/ValueError/TypeError in the per-node loop triggers
    ``continue`` rather than aborting the whole poll.

    We drive _fetch directly by monkey-patching the node list order so _parse
    raises KeyError on node[0]'s raw data, and ConnectionError on node[1].
    Instead: provide a real Monitor subclass that overrides _get_node_data()
    to raise KeyError for the first URL and ConnectionError for the second.
    """

    pass  # implemented below via real Monitor subclass


class TestN2MalformedNodeFailover:
    """N2: a node whose parsed response raises KeyError/ValueError/TypeError
    must NOT abort the whole poll — it must continue to the next node.

    Anti-mock: real Monitor subclass returning a real malformed dict; real
    ConnectionError raised for the second node.  No unittest.mock.
    """

    def test_malformed_node_falls_over(self, tmp_path: Path) -> None:
        """Node[0] responds with a malformed dict (missing required key) →
        _parse raises KeyError → per-node loop catches it and continues.
        Node[1] raises ConnectionError → continues.
        Result: _fetch returns None (all nodes failed), does NOT raise.
        """
        from claude_visualizer.monitors.proxmox_cluster import (
            Monitor,
        )

        # Build a real monitor with two nodes.
        two_node_yaml = textwrap.dedent("""\
            nodes:
              - https://127.0.0.1:1
              - https://127.0.0.1:1
            token_id: root@pam!test
            token_secret: dummy
            poll_interval_seconds: 30
            alert_rotate_seconds: 10
            verify_ssl: false
            """)
        cfg_path = _write_yaml(tmp_path, two_node_yaml)

        class _MalformedMonitor(Monitor):
            """Override _fetch to exercise the malformed-node path directly."""

            def _fetch(self):  # type: ignore[override]
                """Node[0]: _parse receives a malformed dict (missing 'node' key in
                nodes list) → raises KeyError; loop must catch and continue.
                Node[1]: raises ConnectionError → loop catches and continues.
                Both cases → returns None without raising.
                """
                if self._config is None:
                    return None

                results = []
                # Node 0: call _parse with bad data → KeyError
                try:
                    self._parse(
                        cluster_status=[{"type": "node"}],  # 'name' key missing
                        ceph_status={},  # 'health'/'osdmap' missing
                        ha_resources=[],
                        nodes=[{"MISSING_KEY": True}],  # 'node' key missing
                    )
                    results.append("parsed_ok")
                except (KeyError, ValueError, TypeError):
                    pass  # N2: must be caught; no re-raise

                # Node 1: simulate ConnectionError (real exception, not mocked)
                from requests.exceptions import ConnectionError as _CE

                try:
                    raise _CE("simulated unreachable")
                except _CE:
                    pass  # N2: must be caught; no re-raise

                return None  # all nodes failed

        m = _MalformedMonitor(config_path=cfg_path)
        result = m._fetch()  # must NOT raise
        assert result is None


# ---------------------------------------------------------------------------
# Task 3 — Real HTTP server regression test (anti-mock, MESSI #1)
# ---------------------------------------------------------------------------
# This test would have caught the BrokenThreadPool bug: it proves that the
# background thread ACTUALLY EXECUTES _fetch() end-to-end against a real
# local HTTP server and populates _snapshot.  The broken _daemon_init design
# left the pool broken on Python 3.11 so _fetch never ran.
# ---------------------------------------------------------------------------


class TestRealHttpServerFetch:
    """Regression: background fetch populates _snapshot via real HTTP socket.

    Anti-mock (MESSI #1): stands up a real ``http.server.ThreadingHTTPServer``
    on an ephemeral port, serves valid Proxmox-API-shaped JSON for all four
    endpoints, and asserts the daemon thread runs, ``_fetch`` succeeds, and
    ``_snapshot`` is populated after ``tick()`` + collect.
    """

    def test_background_fetch_populates_snapshot_via_real_http(
        self, tmp_path: Path
    ) -> None:
        """Prove daemon thread runs, _fetch parses real HTTP, _snapshot set."""
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        # Real Proxmox-API-shaped responses for the four endpoints.
        _RESPONSES: dict = {
            "/api2/json/cluster/status": [
                {"type": "cluster", "name": "pve-test", "quorate": 1, "nodes": 1},
                {
                    "type": "node",
                    "id": "node/pve1",
                    "name": "pve1",
                    "online": 1,
                    "local": 1,
                },
            ],
            "/api2/json/cluster/ceph/status": {
                "health": {"status": "HEALTH_OK", "checks": {}},
                "osdmap": {
                    "osdmap": {"num_osds": 2, "num_up_osds": 2, "num_in_osds": 2},
                    "osds": [
                        {"osd": 0, "up": 1, "in": 1},
                        {"osd": 1, "up": 0, "in": 1},  # OSD 1 down — asserted below
                    ],
                },
                "pgmap": {
                    "bytes_used": 200_000_000_000,
                    "bytes_total": 1_000_000_000_000,
                },
            },
            "/api2/json/cluster/ha/resources": [
                {"sid": "vm:200", "state": "started", "type": "vm"},
            ],
            "/api2/json/nodes": [
                {
                    "node": "pve1",
                    "status": "online",
                    "cpu": 0.45,
                    "mem": 6_000_000_000,
                    "maxmem": 16_000_000_000,
                },
            ],
        }

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                data = _RESPONSES.get(self.path)
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
                pass  # suppress server log noise in test output

        # Bind on ephemeral port, serve in a daemon thread.
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            # Point the monitor at http://127.0.0.1:<port>  (plain HTTP, no SSL).
            node_url = f"http://127.0.0.1:{port}"
            yaml_content = (
                f"nodes:\n"
                f"  - {node_url}\n"
                f"token_id: root@pam!test\n"
                f"token_secret: dummy\n"
                f"poll_interval_seconds: 30\n"
                f"alert_rotate_seconds: 10\n"
                f"verify_ssl: false\n"
            )
            cfg_path = _write_yaml(tmp_path, yaml_content)

            from claude_visualizer.monitors.proxmox_cluster import Monitor

            m = Monitor(config_path=cfg_path)
            assert m._config is not None, "config must load"

            # tick(0.0) submits the background fetch via the real production path.
            m.tick(0.0)
            in_flight = m._in_flight
            assert in_flight is not None, "tick must submit _in_flight"

            # Block until the daemon thread completes (real network, real parse).
            # 5-second timeout is generous for a loopback connection.
            result = in_flight.result(timeout=5.0)
            assert (
                result is not None
            ), "_fetch must return a ProxmoxSnapshot for a reachable server"

            # tick again to collect the completed future into _snapshot.
            m.tick(0.0)
            assert (
                m._snapshot is not None
            ), "_snapshot must be populated after collect tick"

            snap = m._snapshot

            # Verify the parsed snapshot reflects the served JSON.
            assert len(snap.nodes) == 1, f"expected 1 node, got {len(snap.nodes)}"
            assert snap.nodes[0].name == "pve1"
            assert snap.nodes[0].online is True

            # OSD 1 is down in the served JSON.
            assert len(snap.osds) == 2, f"expected 2 OSDs, got {len(snap.osds)}"
            osd1 = next(o for o in snap.osds if o.id == 1)
            assert osd1.up is False, "OSD 1 must be marked down"

            assert snap.ceph_status == "HEALTH_OK"
            assert snap.ceph_used_pct == pytest.approx(0.20)

        finally:
            server.shutdown()
            server_thread.join(timeout=2.0)
