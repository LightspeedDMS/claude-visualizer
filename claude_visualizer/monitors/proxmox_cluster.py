"""Proxmox Cluster Monitor plugin for the pluggable monitor bar (story #7).

Drop-in monitor: expose ``class Monitor`` with ``tick(now: float) -> str | Text``.
Zero edits to core modules required — follows the Monitor contract exactly.

Config file: ``~/.claude-visualizer/proxmox.yaml`` (path injectable for tests).
Install: ``install.sh``'s ``seed_monitors()`` copies this file automatically.

API poll (every 30s by default): 5 GETs per node — /cluster/status,
/cluster/ceph/status, /cluster/ha/resources, /nodes, and
/nodes/{node}/ceph/osd (CRUSH tree for per-OSD up/in state).
First node answering all five endpoints wins (failover).
Stale snapshot is kept on total failure.

Background polling (B1 fix):
    ``tick()`` is non-blocking — it submits ``_fetch()`` via ``_submit_fetch()``,
    which starts a plain daemon ``threading.Thread`` that drives a standalone
    ``concurrent.futures.Future``.  Daemon threads mean interpreter / app exit
    is never delayed by an in-flight network call.  No ``ThreadPoolExecutor``
    is used: the ``ThreadPoolExecutor(initializer=_daemon_init)`` approach
    raised ``RuntimeError("cannot set daemon status of active thread")`` on
    Python 3.11 because initializers run inside an already-started thread.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from rich.text import Text

_LOG = logging.getLogger(__name__)

try:
    import requests
    import urllib3
    from requests.exceptions import ConnectionError, HTTPError, Timeout
except ImportError as _imp_err:
    raise ImportError(
        "claude-visualizer Proxmox monitor requires 'requests'. "
        "Run: pip install requests>=2.31"
    ) from _imp_err

# ---------------------------------------------------------------------------
# Default config path — injectable for tests (no live file read in unit tests)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path.home() / ".claude-visualizer" / "proxmox.yaml"

# Guard: urllib3 InsecureRequestWarning suppressed at most once per process.
_ssl_warnings_suppressed: bool = False

# ---------------------------------------------------------------------------
# Enums and dataclasses
# ---------------------------------------------------------------------------


class Severity(Enum):
    """Alert severity: lower value = higher priority."""

    CRIT = 0
    WARN = 1
    INFO = 2


@dataclass
class Alert:
    severity: Severity
    text: str


@dataclass
class OSDState:
    id: int
    up: bool
    in_: bool  # 'in' is a keyword; use in_


@dataclass
class NodeState:
    name: str
    online: bool
    cpu_pct: float
    mem_pct: float


@dataclass
class ProxmoxConfig:
    """Loaded from proxmox.yaml. token_secret excluded from repr for security."""

    nodes: List[str]
    token_id: str
    token_secret: str
    poll_interval_seconds: float
    alert_rotate_seconds: float
    verify_ssl: bool

    def __repr__(self) -> str:  # SECURITY: never expose token_secret
        return (
            f"ProxmoxConfig(nodes={self.nodes!r}, token_id={self.token_id!r}, "
            f"token_secret=<redacted>, "
            f"poll_interval_seconds={self.poll_interval_seconds}, "
            f"alert_rotate_seconds={self.alert_rotate_seconds}, "
            f"verify_ssl={self.verify_ssl})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass
class ProxmoxSnapshot:
    nodes: List[NodeState]
    osds: List[OSDState]
    ceph_status: str  # e.g. "HEALTH_OK", "HEALTH_WARN", "HEALTH_ERR"
    ceph_used_pct: float  # 0.0–1.0
    alerts: List[Alert]
    fetched_at: float
    quorate: bool = True  # Defect 4: from type=="cluster" entry in /cluster/status


# ---------------------------------------------------------------------------
# Known Ceph health check codes by severity
# ---------------------------------------------------------------------------

_CRIT_CEPH_CODES = frozenset(
    {
        "OSD_DOWN",
        "OSD_OUT",
        "PG_AVAILABILITY",
        "PG_DAMAGED",
        "POOL_FULL",
        "MON_DOWN",
        "OBJECT_UNFOUND",
        "MDS_DAMAGE",
    }
)

_WARN_CEPH_CODES = frozenset(
    {
        "PG_DEGRADED",
        "OSD_NEARFULL",
        "OSD_BACKFILLFULL",
        "POOL_NEARFULL",
        "POOL_BACKFILLFULL",
        "DEVICE_HEALTH_DEGRADED",
        "DEVICE_HEALTH",
        "SLOW_OPS",
        "MON_CLOCK_SKEW",
        "MON_DISK_LOW",
        "OSDMAP_FLAGS",
        "FS_DEGRADED",
        "MDS_DEGRADED",
    }
)

_INFO_CEPH_CODES = frozenset(
    {
        "PG_NOT_SCRUBBED",
        "PG_NOT_DEEP_SCRUBBED",
        "OBJECT_MISPLACED",
    }
)

# HA resource states → severity
_HA_CRIT_STATES = frozenset({"error"})
_HA_WARN_STATES = frozenset({"migrate", "relocate", "freeze"})


# ---------------------------------------------------------------------------
# CRUSH tree OSD flattener (Bug #8 Defect 1)
# ---------------------------------------------------------------------------


def _flatten_crush_osds(node: Any) -> List[OSDState]:
    """Recursively walk a Proxmox CRUSH tree node and collect OSD leaves.

    The tree from ``/nodes/{node}/ceph/osd`` has the shape::

        root → {type: None, children: [{type: "root", children: [
            {type: "host", children: [
                {type: "osd", id: "2", status: "up", in: 1, ...},
                ...
            ]},
            ...
        ]}]}

    Only dicts with ``type == "osd"`` are collected; all other node types
    (``None``, ``"root"``, ``"host"``) are traversed recursively.
    Returns an empty list when ``node`` is ``None`` or not a dict.
    """
    if not isinstance(node, dict):
        return []
    results: List[OSDState] = []
    if node.get("type") == "osd":
        results.append(
            OSDState(
                id=int(node["id"]),
                up=(node.get("status") == "up"),
                in_=bool(node.get("in", 0)),
            )
        )
    for child in node.get("children") or []:
        results.extend(_flatten_crush_osds(child))
    return results


# ---------------------------------------------------------------------------
# Alert builder
# ---------------------------------------------------------------------------


def build_alerts(
    *,
    ceph_health: Dict[str, Any],
    ha_resources: List[Dict[str, Any]],
    nodes: List["NodeState"],
    ceph_used_pct: float,
) -> List[Alert]:
    """Build sorted (CRIT→WARN→INFO, stable) alert list from telemetry.

    Unknown ceph.health.checks codes are passed through verbatim (AC8).
    Sort is STABLE within each severity (Python's sort is stable).
    """
    raw: List[Alert] = []

    # --- Ceph health checks ---------------------------------------------------
    checks: Dict[str, Any] = ceph_health.get("checks", {})
    for code, detail in checks.items():
        msg: str = detail.get("summary", {}).get("message", code)
        # Bug #8 Defect 3: filter subscription-related warnings (code OR message)
        if "subscription" in code.lower() or "subscription" in msg.lower():
            continue
        if code in _CRIT_CEPH_CODES:
            sev = Severity.CRIT
        elif code in _WARN_CEPH_CODES:
            sev = Severity.WARN
        elif code in _INFO_CEPH_CODES:
            sev = Severity.INFO
        else:
            # AC8 — unknown code passed through verbatim; assign WARN by default
            sev = Severity.WARN
        raw.append(Alert(severity=sev, text=msg))

    # --- Node offline ---------------------------------------------------------
    for node in nodes:
        if not node.online:
            raw.append(Alert(severity=Severity.CRIT, text=f"Node {node.name} offline"))

    # --- Node CPU/RAM high ---------------------------------------------------
    for node in nodes:
        if node.cpu_pct >= 95.0:
            raw.append(
                Alert(
                    severity=Severity.WARN,
                    text=f"Node {node.name} CPU {node.cpu_pct:.0f}%",
                )
            )
        if node.mem_pct >= 95.0:
            raw.append(
                Alert(
                    severity=Severity.WARN,
                    text=f"Node {node.name} RAM {node.mem_pct:.0f}%",
                )
            )

    # --- HA resources ---------------------------------------------------------
    for res in ha_resources:
        state = res.get("state", "")
        sid = res.get("sid", "?")
        if state in _HA_CRIT_STATES:
            raw.append(Alert(severity=Severity.CRIT, text=f"HA {sid} in {state}"))
        elif state in _HA_WARN_STATES:
            raw.append(Alert(severity=Severity.WARN, text=f"HA {sid} {state}"))

    # --- Ceph capacity --------------------------------------------------------
    if ceph_used_pct >= 0.90:
        raw.append(
            Alert(severity=Severity.CRIT, text=f"Ceph {ceph_used_pct * 100:.0f}% full")
        )
    elif ceph_used_pct >= 0.75:
        raw.append(
            Alert(severity=Severity.WARN, text=f"Ceph {ceph_used_pct * 100:.0f}% full")
        )
    elif ceph_used_pct >= 0.65:
        raw.append(
            Alert(severity=Severity.INFO, text=f"Ceph {ceph_used_pct * 100:.0f}% full")
        )

    # Stable sort by severity value (CRIT=0 first)
    raw.sort(key=lambda a: a.severity.value)
    return raw


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

_COLOUR_FOR_CEPH: Dict[str, str] = {
    "HEALTH_OK": "green",
    "HEALTH_WARN": "yellow",
    "HEALTH_ERR": "red",
}

_COLOUR_FOR_ALERT_SEV: Dict[Severity, str] = {
    Severity.CRIT: "red",
    Severity.WARN: "yellow",
    Severity.INFO: "cyan",
}


def _humanize_age(seconds: float) -> str:
    """Convert an age in seconds to a human-readable string: Ns, Nm, or Nh."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def render_proxmox_bar(
    snapshot: ProxmoxSnapshot,
    alert_index: int,
    *,
    stale_for: Optional[float] = None,
) -> Text:
    """Render the one-line Proxmox cluster status bar as a Rich ``Text``.

    Layout:
        [⚠ PVE UNREACHABLE <age> │ ] Cluster: OK/WARN/ERR │ Ceph: … │
        <id>● per node │ ● per OSD (id asc) osds │ ↻ ⚑ <alert>

    ``stale_for``: when not None, prepend a bold-red unreachable badge followed
    by the last-known cluster data (stale snapshot retained per AC2).
    """
    out = Text(no_wrap=True, overflow="ellipsis")

    # --- Unreachable badge (when fetch has failed after a prior success) ----
    if stale_for is not None:
        out.append(f"⚠ PVE UNREACHABLE {_humanize_age(stale_for)}", style="bold red")
        out.append(" │", style="dim")

    # --- Cluster verdict (Defect 4: quorum-based, not node-offline-based) ----
    # Proxmox shows cluster GREEN when quorate, even with a node down.
    # quorate=True  → OK  (green) — cluster has quorum
    # quorate=False → ERR (red)   — quorum lost
    if snapshot.quorate:
        cluster_label, cluster_colour = "OK", "green"
    else:
        cluster_label, cluster_colour = "ERR", "red"

    out.append(" Cluster: ")
    out.append(cluster_label, style=cluster_colour)

    out.append(" │ ", style="dim")

    # --- Ceph section ---------------------------------------------------------
    ceph_colour = _COLOUR_FOR_CEPH.get(snapshot.ceph_status, "yellow")
    out.append("Ceph: ")
    out.append(snapshot.ceph_status, style=ceph_colour)

    out.append(" │ ", style="dim")

    # --- Node dots ------------------------------------------------------------
    for node in snapshot.nodes:
        out.append(f"{node.name}")
        dot_colour = "bright_green" if node.online else "red"
        out.append("●", style=dot_colour)
        out.append(" ")

    out.append("│ ", style="dim")

    # --- OSD dots (sorted by id ascending) ------------------------------------
    sorted_osds = sorted(snapshot.osds, key=lambda o: o.id)
    for osd in sorted_osds:
        dot_colour = "bright_green" if osd.up else "red"
        out.append("●", style=dot_colour)
    if sorted_osds:
        out.append(" osds")

    out.append(" │ ", style="dim")

    # --- Rotating alert -------------------------------------------------------
    out.append("↻ ")
    alerts = snapshot.alerts
    if not alerts:
        out.append("no alerts", style="dim")
    else:
        idx = alert_index % len(alerts)
        alert = alerts[idx]
        out.append("⚑ ")
        alert_colour = _COLOUR_FOR_ALERT_SEV.get(alert.severity, "white")
        out.append(alert.text, style=alert_colour)

    return out


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _load_config(path: Path) -> ProxmoxConfig:
    """Load ProxmoxConfig from a YAML file. Raises FileNotFoundError if absent."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ProxmoxConfig(
        nodes=raw["nodes"],
        token_id=raw["token_id"],
        token_secret=raw["token_secret"],
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 30)),
        alert_rotate_seconds=float(raw.get("alert_rotate_seconds", 10)),
        verify_ssl=bool(raw.get("verify_ssl", False)),
    )


# ---------------------------------------------------------------------------
# Monitor class — entry point for MonitorRegistry
# ---------------------------------------------------------------------------


class Monitor:
    """Proxmox cluster monitor plugin.

    ``tick(now)`` returns a Rich ``Text`` bar line or a dim warning string.
    Requires ``~/.claude-visualizer/proxmox.yaml`` (path injectable for tests).

    Background polling (B1):
        ``_fetch()`` runs in a daemon thread created by ``_submit_fetch()``,
        which starts a plain ``threading.Thread(daemon=True)`` that resolves
        a standalone ``concurrent.futures.Future``.  ``tick()`` never blocks
        on network I/O — it calls ``_submit_fetch()`` and returns the current
        snapshot immediately.  The in-flight ``Future`` is stored in
        ``_in_flight``; a second ``tick()`` while a fetch is running skips
        submission (de-dupe).  Daemon threads mean app exit is never delayed
        by an in-progress HTTP call.

    Why no ThreadPoolExecutor:
        ``ThreadPoolExecutor(initializer=_daemon_init)`` where ``_daemon_init``
        sets ``threading.current_thread().daemon = True`` raises
        ``RuntimeError("cannot set daemon status of active thread")`` on
        Python 3.11 — you cannot daemonize an already-started thread.  This
        broke the entire pool (``BrokenThreadPool``) so ``_fetch`` never ran.
        The manual-thread approach avoids the pool entirely.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
    ) -> None:
        global _ssl_warnings_suppressed

        _path = config_path if config_path is not None else _DEFAULT_CONFIG_PATH
        try:
            self._config: Optional[ProxmoxConfig] = _load_config(_path)
        except FileNotFoundError:
            self._config = None

        # N1: suppress urllib3 InsecureRequestWarning once per process when
        # verify_ssl=False (default) so it doesn't corrupt the full-screen TUI.
        if self._config is not None and not self._config.verify_ssl:
            if not _ssl_warnings_suppressed:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                _ssl_warnings_suppressed = True

        self._snapshot: Optional[ProxmoxSnapshot] = None
        self._last_poll: float = 0.0
        self._polled_once: bool = False
        self._alert_index: int = 0
        self._last_rotate: float = 0.0
        self._fetch_failed: bool = False
        self._last_success_at: float = 0.0
        self._in_flight: Optional[
            concurrent.futures.Future[Optional[ProxmoxSnapshot]]
        ] = None

    def _submit_fetch(self) -> "concurrent.futures.Future[Optional[ProxmoxSnapshot]]":
        """Submit ``_fetch()`` to a new daemon thread; return its Future.

        Creates a fresh ``concurrent.futures.Future``, starts a daemon
        ``threading.Thread`` that resolves it, and returns the future.
        Daemon threads exit with the interpreter — app exit is never blocked
        by an in-flight HTTP call.
        """
        fut: concurrent.futures.Future[  # type: ignore[type-arg]
            Optional[ProxmoxSnapshot]
        ] = concurrent.futures.Future()

        def _run() -> None:
            try:
                fut.set_result(self._fetch())
            except Exception as exc:
                fut.set_exception(exc)

        threading.Thread(target=_run, name="pve-poll", daemon=True).start()
        return fut

    def tick(self, now: float) -> "str | Text":
        """Return the rendered bar line — NEVER blocks on network I/O (B1).

        Poll submission:
          - If no poll has been made yet, OR the poll interval has elapsed,
            AND no fetch is currently in-flight → submit ``_fetch()`` via
            ``_submit_fetch()``, update ``_last_poll`` / ``_polled_once``
            immediately (poll *initiated*, not completed).
          - If a fetch is in-flight, skip submission (de-dupe).

        Result collection:
          - If an in-flight fetch has completed, collect the result; if it
            returned a non-None snapshot, update ``self._snapshot``.
          - If still running, leave snapshot unchanged.

        Return value:
          - ``PVE: connecting…`` while ``_snapshot is None``.
          - ``⚠ proxmox.yaml not found`` if config is missing.
          - Rendered bar from current snapshot otherwise.
        """
        if self._config is None:
            return ""  # no proxmox.yaml → suppress the monitor entirely (no row)

        cfg = self._config

        # --- Collect completed in-flight result (non-blocking) ---------------
        if self._in_flight is not None and self._in_flight.done():
            try:
                fresh = self._in_flight.result()
                if fresh is not None:
                    self._snapshot = fresh
                    self._last_success_at = now
                    self._fetch_failed = False
                else:
                    self._fetch_failed = True  # all nodes failed → keep stale snapshot
            except Exception:
                self._fetch_failed = True  # fetch raised → keep stale snapshot
            self._in_flight = None

        # --- Submit a new fetch if due and none in-flight --------------------
        should_poll = not self._polled_once or (
            now - self._last_poll >= cfg.poll_interval_seconds
        )
        if should_poll and self._in_flight is None:
            self._in_flight = self._submit_fetch()
            self._last_poll = now
            self._polled_once = True

        # --- Render current state --------------------------------------------
        if self._snapshot is None:
            return "PVE: connecting…"

        alerts = self._snapshot.alerts
        if alerts and (now - self._last_rotate >= cfg.alert_rotate_seconds):
            self._alert_index = (self._alert_index + 1) % len(alerts)
            self._last_rotate = now

        if self._fetch_failed:
            return render_proxmox_bar(
                self._snapshot, self._alert_index, stale_for=now - self._last_success_at
            )
        return render_proxmox_bar(self._snapshot, self._alert_index)

    def _fetch(self) -> Optional[ProxmoxSnapshot]:
        """Try each node in order; return first successful parse or None.

        Runs in a background daemon thread (B1). Directly callable for
        unit tests that want synchronous behaviour.
        """
        if self._config is None:
            return None

        cfg = self._config
        headers = {"Authorization": f"PVEAPIToken={cfg.token_id}={cfg.token_secret}"}
        verify = cfg.verify_ssl

        for base_url in cfg.nodes:
            try:
                base = base_url.rstrip("/")

                def _get(path: str) -> Any:
                    resp = requests.get(
                        f"{base}{path}",
                        headers=headers,
                        verify=verify,
                        timeout=(1.5, 5),  # (connect, read) — tighter connect bound
                    )
                    resp.raise_for_status()
                    return resp.json()["data"]

                cluster_status = _get("/api2/json/cluster/status")
                ceph_status = _get("/api2/json/cluster/ceph/status")
                ha_resources = _get("/api2/json/cluster/ha/resources")
                nodes = _get("/api2/json/nodes")

                # Bug #8 Defect 1: per-OSD state from CRUSH tree.
                # Pick the first ONLINE node name from cluster_status for the path.
                # osdmap.osds is null on real Proxmox; this endpoint has the data.
                osd_tree: Optional[Dict[str, Any]] = None
                for _entry in cluster_status:
                    if _entry.get("type") == "node" and bool(_entry.get("online", 0)):
                        _node_name = _entry.get("name", "")
                        if _node_name:
                            try:
                                osd_tree = _get(
                                    f"/api2/json/nodes/{_node_name}/ceph/osd"
                                )
                            except (
                                ConnectionError,
                                Timeout,
                                HTTPError,
                                OSError,
                                KeyError,
                                ValueError,
                                TypeError,
                            ) as _exc:
                                _LOG.debug(
                                    "OSD tree fetch failed on node %s: %s",
                                    _node_name,
                                    _exc,
                                )
                                osd_tree = None
                            break

                return self._parse(
                    cluster_status=cluster_status,
                    ceph_status=ceph_status,
                    ha_resources=ha_resources,
                    nodes=nodes,
                    osd_tree=osd_tree,
                )
            except (
                ConnectionError,
                Timeout,
                HTTPError,
                OSError,
                KeyError,
                ValueError,
                TypeError,
            ):
                continue  # N2: malformed response → try next node

        return None  # all nodes unreachable

    def _parse(
        self,
        *,
        cluster_status: List[Dict[str, Any]],
        ceph_status: Dict[str, Any],
        ha_resources: List[Dict[str, Any]],
        nodes: List[Dict[str, Any]],
        osd_tree: Optional[Dict[str, Any]],
    ) -> ProxmoxSnapshot:
        """Build a ProxmoxSnapshot from raw Proxmox API dicts.

        ``osd_tree`` is the response from ``GET /nodes/{node}/ceph/osd``.
        When provided, OSD state comes from the CRUSH tree via
        ``_flatten_crush_osds()``; the ``osdmap.osds`` field (null on real
        Proxmox) is ignored.  Pass ``None`` when unavailable — results in
        an empty OSD list.

        Node list is sorted alphabetically by name (Bug #8 Defect 2).
        """
        # Build a name → node-detail map from /nodes response
        nodes_by_name: Dict[str, Dict[str, Any]] = {n["node"]: n for n in nodes}

        # Defect 4: extract quorate from the type=="cluster" entry.
        # If no cluster entry is found, default to False (can't confirm quorum).
        quorate: bool = False
        for entry in cluster_status:
            if entry.get("type") == "cluster":
                quorate = bool(entry.get("quorate", 0))
                break

        # Build NodeState list from /cluster/status entries of type "node"
        # Bug #8 Defect 2: sort alphabetically by name so display is canonical.
        node_states: List[NodeState] = []
        for entry in cluster_status:
            if entry.get("type") != "node":
                continue
            name = entry.get("name", "")
            online = bool(entry.get("online", 0))
            detail = nodes_by_name.get(name, {})
            cpu_pct = float(detail.get("cpu", 0.0)) * 100.0
            maxmem = float(detail.get("maxmem", 1) or 1)
            mem_used = float(detail.get("mem", 0))
            mem_pct = (mem_used / maxmem) * 100.0 if maxmem > 0 else 0.0
            node_states.append(
                NodeState(name=name, online=online, cpu_pct=cpu_pct, mem_pct=mem_pct)
            )
        node_states.sort(key=lambda n: n.name)

        # Build OSDState list from CRUSH tree (Bug #8 Defect 1).
        # osdmap.osds is null on real Proxmox — use /nodes/{node}/ceph/osd tree.
        if osd_tree is not None:
            osd_states: List[OSDState] = _flatten_crush_osds(osd_tree.get("root"))
        else:
            osd_states = []

        # Ceph health
        ceph_health_block = ceph_status.get("health", {})
        ceph_health_status: str = ceph_health_block.get("status", "HEALTH_UNKNOWN")

        # Ceph capacity
        pgmap = ceph_status.get("pgmap", {})
        bytes_used = float(pgmap.get("bytes_used", 0))
        bytes_total = float(pgmap.get("bytes_total", 1) or 1)
        ceph_used_pct = bytes_used / bytes_total if bytes_total > 0 else 0.0

        alerts = build_alerts(
            ceph_health=ceph_health_block,
            ha_resources=ha_resources,
            nodes=node_states,
            ceph_used_pct=ceph_used_pct,
        )

        return ProxmoxSnapshot(
            nodes=node_states,
            osds=osd_states,
            ceph_status=ceph_health_status,
            ceph_used_pct=ceph_used_pct,
            alerts=alerts,
            fetched_at=time.monotonic(),
            quorate=quorate,
        )
