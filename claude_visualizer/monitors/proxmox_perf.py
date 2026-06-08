"""Proxmox Cluster Performance Monitor plugin for the pluggable monitor bar.

Drop-in monitor: expose ``class Monitor`` with ``tick(now: float) -> str | Text``.
Zero edits to core modules required — follows the Monitor contract exactly.

Renders a single-line cluster-wide performance bar:
    CPU  57% │ RAM  44%  36.0G free │ load  2.0 │ Ceph 10.0M/20.0M· 100/ 200 │ Net ↓ 8.8k ↑11.7k │ ⚙  3/ 5 VMs

Config file: ``~/.claude-visualizer/proxmox.yaml`` (shared with health monitor).

Endpoints polled (first responding node wins):
  - /cluster/resources  → CPU%, RAM%, free RAM, cores, running/total VMs
  - /cluster/ceph/status → Ceph r/w bps + IOPS (absent/error → 0, no crash)
  - /nodes/{node}/rrddata?timeframe=hour&cf=AVERAGE → last-sample netin/netout
    summed across online nodes, loadavg averaged

Background polling (B1 pattern, identical to health monitor):
    ``tick()`` is non-blocking — submits ``_fetch()`` via a daemon thread,
    collecting results on subsequent calls.

Degraded states:
  - No config file → tick() returns ""
  - Never connected (_snapshot is None) → tick() returns ""
  - Latest fetch failed, prior snapshot exists → render_perf_bar(snap, stale=True)
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.text import Text

# Reuse config machinery and helpers from the health monitor — DRY (MESSI #4)
from claude_visualizer.monitors.proxmox_cluster import (
    ProxmoxConfig,
    _load_config,
)

_LOG = logging.getLogger(__name__)

try:
    import requests
    import urllib3
    from requests.exceptions import ConnectionError, HTTPError, Timeout
except ImportError as _imp_err:
    raise ImportError(
        "claude-visualizer Proxmox perf monitor requires 'requests'. "
        "Run: pip install requests>=2.31"
    ) from _imp_err

# ---------------------------------------------------------------------------
# Default config path — injectable for tests
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path.home() / ".claude-visualizer" / "proxmox.yaml"

# ---------------------------------------------------------------------------
# Named constants — no magic numbers
# ---------------------------------------------------------------------------

_BYTES_PER_GIB = 1024**3
_BYTES_PER_MIB = 1024 * 1024
_BYTES_PER_KIB = 1024

# HTTP request timeouts (connect_s, read_s)
_REQUEST_CONNECT_TIMEOUT_S = 1.5
_REQUEST_READ_TIMEOUT_S = 5.0
_REQUEST_TIMEOUT = (_REQUEST_CONNECT_TIMEOUT_S, _REQUEST_READ_TIMEOUT_S)

# Color thresholds for CPU% and RAM%
_PCT_WARN_THRESHOLD = 60.0  # >= this → yellow
_PCT_CRIT_THRESHOLD = 80.0  # >= this → red

# Color thresholds for load average (as fraction of core count)
_LOAD_WARN_FRACTION = 0.7  # >= this × cores → yellow
_LOAD_CRIT_FRACTION = 1.0  # >= this × cores → red

# Bar rendering (identical style to zzz_machine_stats)
_BAR_WIDTH = 8
_BAR_FILL = "█"
_BAR_EMPTY = "░"

# ---------------------------------------------------------------------------
# Thread-safe SSL warning suppression guard
# ---------------------------------------------------------------------------

_ssl_lock: threading.Lock = threading.Lock()
_ssl_warnings_suppressed: bool = False


def _suppress_ssl_warnings_once() -> None:
    """Suppress urllib3 InsecureRequestWarning at most once per process."""
    global _ssl_warnings_suppressed
    with _ssl_lock:
        if not _ssl_warnings_suppressed:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            _ssl_warnings_suppressed = True


# ---------------------------------------------------------------------------
# PerfSnapshot dataclass
# ---------------------------------------------------------------------------


@dataclass
class PerfSnapshot:
    """Immutable snapshot of cluster-wide performance metrics."""

    cpu_pct: float  # weighted-average CPU % across online nodes
    ram_pct: float  # RAM utilisation % across online nodes
    ram_free_bytes: float  # free RAM bytes (Σmaxmem − Σmem) across online nodes
    cores: int  # total vCPU cores across online nodes
    load_avg: float  # mean 1-min load average across online nodes (rrddata)
    ceph_read_bps: float  # Ceph read throughput bytes/s (pgmap)
    ceph_write_bps: float  # Ceph write throughput bytes/s (pgmap)
    ceph_read_iops: float  # Ceph read IOPS (pgmap)
    ceph_write_iops: float  # Ceph write IOPS (pgmap)
    net_in_bps: float  # total cluster inbound bytes/s (rrddata last sample)
    net_out_bps: float  # total cluster outbound bytes/s (rrddata last sample)
    running_vms: int  # running qemu + lxc items
    total_vms: int  # all qemu + lxc items
    fetched_at: float  # monotonic time of fetch


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _bar_colour(pct: float) -> str:
    """Map a utilisation percentage to a Rich color name (for bar fill)."""
    if pct >= _PCT_CRIT_THRESHOLD:
        return "red"
    if pct >= _PCT_WARN_THRESHOLD:
        return "yellow"
    return "green"


def _render_bar(out: "Text", pct: float) -> None:
    """Append a 12-char bar to out, colored by percentage."""
    filled = min(_BAR_WIDTH, round(_BAR_WIDTH * pct / 100))
    out.append(
        _BAR_FILL * filled + _BAR_EMPTY * (_BAR_WIDTH - filled),
        style=_bar_colour(pct),
    )


def _fmt_rate(bps: float) -> str:
    """Format a byte rate as a 7-char left-padded human-readable string (matches machine stats)."""
    if bps < 1024:
        s = f"{int(bps)}B/s"
    elif bps < 1024**2:
        val = bps / 1024
        s = f"{val:.1f}K/s" if val < 100 else f"{val:.0f}K/s"
    elif bps < 1024**3:
        val = bps / 1024**2
        s = f"{val:.1f}M/s" if val < 100 else f"{val:.0f}M/s"
    else:
        val = bps / 1024**3
        s = f"{val:.1f}G/s" if val < 100 else f"{val:.0f}G/s"
    return s.ljust(7)


_BYTES_PER_TIB = 1024**4


def _fmt_ceph_bps(bps: float) -> str:
    """Format Ceph throughput as a 6-char right-justified string.

    Tiers (largest first so large values render compactly):
      >= 1 TiB/s → X.Xt or XXXt   (fits in 6 chars for sane values)
      >= 1 GiB/s → X.XG or XXXG
      >= 1 MiB/s → X.XM or XXXM
      >= 1 KiB/s → X.Xk or XXXk
      otherwise  → integer bytes
    """
    if bps >= _BYTES_PER_TIB:
        val = bps / _BYTES_PER_TIB
        s = f"{val:.1f}T" if val < 100 else f"{val:.0f}T"
    elif bps >= _BYTES_PER_GIB:
        val = bps / _BYTES_PER_GIB
        s = f"{val:.1f}G" if val < 100 else f"{val:.0f}G"
    elif bps >= _BYTES_PER_MIB:
        val = bps / _BYTES_PER_MIB
        s = f"{val:.1f}M" if val < 100 else f"{val:.0f}M"
    elif bps >= _BYTES_PER_KIB:
        val = bps / _BYTES_PER_KIB
        s = f"{val:.1f}k" if val < 100 else f"{val:.0f}k"
    else:
        s = f"{bps:.0f}"
    return s.rjust(6)


def _pct_color(pct: float) -> str:
    """Map a utilisation percentage to a Rich color name."""
    if pct >= _PCT_CRIT_THRESHOLD:
        return "red"
    if pct >= _PCT_WARN_THRESHOLD:
        return "yellow"
    return "green"


def _load_color(load: float, cores: int) -> str:
    """Map a load average to a Rich color name relative to core count."""
    threshold = float(cores) if cores > 0 else 1.0
    ratio = load / threshold
    if ratio >= _LOAD_CRIT_FRACTION:
        return "red"
    if ratio >= _LOAD_WARN_FRACTION:
        return "yellow"
    return "green"


def render_perf_bar(snap: PerfSnapshot, *, stale: bool = False) -> Text:
    """Render the one-line cluster performance bar as a Rich Text.

    Layout uses fixed-width numeric fields and visual bars to prevent jitter:
        CPU ██░░░░░░  57% │ RAM ████░░░░  44%  36.0G free │ load   2.0 │
        Ceph r:  10.0M w:  20.0M · r:   100 w:   200 io/s │ Net ↓ 8.8K/s  ↑11.7K/s  │ ⚙  3/ 5 VMs

    stale=True: entire line rendered dim (fetch failed, prior data shown).
    """
    out = Text(no_wrap=True, overflow="ellipsis")

    # free RAM: right-justify to 6 chars (covers "0.0G" … "999.9G")
    ram_free_gib = snap.ram_free_bytes / _BYTES_PER_GIB
    free_str = f"{ram_free_gib:.1f}G"

    out.append(" CPU ")
    _render_bar(out, snap.cpu_pct)
    out.append(f" {snap.cpu_pct:>3.0f}%")
    out.append(" │ ")

    # RAM
    out.append("RAM ")
    _render_bar(out, snap.ram_pct)
    out.append(f" {snap.ram_pct:>3.0f}%")
    out.append(f"  {free_str:>6} free")
    out.append(" │ ")

    # Load average
    out.append("load ")
    out.append(
        f"{snap.load_avg:5.1f}",
        style=_load_color(snap.load_avg, snap.cores),
    )
    out.append(" │ ")

    # Ceph throughput (r:/w: prefixes, fixed 6-char right-justified values)
    out.append("Ceph r:")
    out.append(_fmt_ceph_bps(snap.ceph_read_bps))
    out.append(" w:")
    out.append(_fmt_ceph_bps(snap.ceph_write_bps))

    # Ceph IOPS (r:/w: prefixes, fixed 5-char right-justified, "io/s" suffix)
    out.append(" · r:")
    out.append(f"{snap.ceph_read_iops:5.0f}")
    out.append(" w:")
    out.append(f"{snap.ceph_write_iops:5.0f}")
    out.append(" io/s")
    out.append(" │ ")

    # Network — _fmt_rate produces 7-char padded string (matching machine stats)
    out.append("Net ↓")
    out.append(_fmt_rate(snap.net_in_bps))
    out.append(" ↑")
    out.append(_fmt_rate(snap.net_out_bps))
    out.append(" │ ")

    # VM counts
    out.append("⚙ ")
    out.append(f"{snap.running_vms:2d}/{snap.total_vms:2d} VMs")

    if stale:
        out.stylize("dim")

    return out


# ---------------------------------------------------------------------------
# Fetch helpers — module-level so _fetch stays concise
# ---------------------------------------------------------------------------

# Type alias for the aggregated resource tuple returned by _aggregate_resources
_ResourceAgg = Tuple[float, float, float, int, List[str], int, int]


def _aggregate_resources(resources: List[Dict[str, Any]]) -> _ResourceAgg:
    """Aggregate /cluster/resources data over ONLINE nodes.

    Returns (cpu_pct, ram_pct, ram_free_bytes, cores,
             online_node_names, running_vms, total_vms).
    """
    total_cpu_weighted = 0.0
    total_maxcpu = 0
    total_mem = 0.0
    total_maxmem = 0.0
    online_node_names: List[str] = []
    running_vms = 0
    total_vms = 0

    for item in resources:
        itype = item.get("type")
        if itype == "node" and item.get("status") == "online":
            maxcpu = int(item.get("maxcpu", 0) or 0)
            total_cpu_weighted += float(item.get("cpu", 0.0) or 0.0) * maxcpu
            total_maxcpu += maxcpu
            total_mem += float(item.get("mem", 0) or 0)
            total_maxmem += float(item.get("maxmem", 0) or 0)
            node_name = item.get("node", "")
            if node_name:
                online_node_names.append(node_name)
        elif itype in ("qemu", "lxc"):
            total_vms += 1
            if item.get("status") == "running":
                running_vms += 1

    cpu_pct = (total_cpu_weighted / total_maxcpu * 100.0) if total_maxcpu > 0 else 0.0
    ram_pct = (total_mem / total_maxmem * 100.0) if total_maxmem > 0 else 0.0
    ram_free_bytes = total_maxmem - total_mem
    return (
        cpu_pct,
        ram_pct,
        ram_free_bytes,
        total_maxcpu,
        online_node_names,
        running_vms,
        total_vms,
    )


def _fetch_ceph_rates(
    get_fn: Any,
) -> Tuple[float, float, float, float]:
    """Fetch /cluster/ceph/status and return (read_bps, write_bps, read_iops, write_iops).

    Returns (0.0, 0.0, 0.0, 0.0) when the endpoint is absent or errors.
    """
    try:
        ceph_data: Dict[str, Any] = get_fn("/api2/json/cluster/ceph/status")
        pgmap = ceph_data.get("pgmap", {}) or {}
        return (
            float(pgmap.get("read_bytes_sec", 0) or 0),
            float(pgmap.get("write_bytes_sec", 0) or 0),
            float(pgmap.get("read_op_per_sec", 0) or 0),
            float(pgmap.get("write_op_per_sec", 0) or 0),
        )
    except (
        ConnectionError,
        Timeout,
        HTTPError,
        OSError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        _LOG.debug("Ceph status unavailable (using 0 rates): %s", exc)
        return 0.0, 0.0, 0.0, 0.0


def _fetch_node_rrd(
    get_fn: Any,
    node_name: str,
) -> Tuple[float, float, Optional[float]]:
    """Fetch the last rrddata sample for one node.

    Returns (net_in_bps, net_out_bps, load_avg_or_None).
    Missing fields default to 0; errors return (0, 0, None).
    """
    try:
        rrd: List[Dict[str, Any]] = get_fn(
            f"/api2/json/nodes/{node_name}/rrddata?timeframe=hour&cf=AVERAGE"
        )
        if rrd:
            last = rrd[-1]
            return (
                float(last.get("netin", 0) or 0),
                float(last.get("netout", 0) or 0),
                float(last.get("loadavg", 0) or 0),
            )
    except (
        ConnectionError,
        Timeout,
        HTTPError,
        OSError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        _LOG.debug("rrddata fetch failed for node %s (using 0): %s", node_name, exc)
    return 0.0, 0.0, None


# ---------------------------------------------------------------------------
# Monitor class — entry point for MonitorRegistry
# ---------------------------------------------------------------------------


class Monitor:
    """Proxmox cluster performance monitor plugin.

    tick(now) returns a Rich Text perf bar or "" when no data.
    Non-blocking B1 pattern: daemon thread + Future, same as health monitor.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        _path = config_path if config_path is not None else _DEFAULT_CONFIG_PATH
        try:
            self._config: Optional[ProxmoxConfig] = _load_config(_path)
        except FileNotFoundError:
            self._config = None

        if self._config is not None and not self._config.verify_ssl:
            _suppress_ssl_warnings_once()

        # Perf-specific poll interval — separate from the shared poll_interval_seconds
        # so the perf bar updates ~1s while the health monitor stays at 30s.
        self._poll_interval_seconds: float = 1.0
        if self._config is not None:
            try:
                import yaml  # already a dep (proxmox_cluster imports it)

                raw = yaml.safe_load(_path.read_text()) or {}
                self._poll_interval_seconds = max(
                    0.1, float(raw.get("perf_poll_interval_seconds", 1.0))
                )
            except Exception:
                self._poll_interval_seconds = 1.0

        self._snapshot: Optional[PerfSnapshot] = None
        self._last_poll: float = 0.0
        self._polled_once: bool = False
        self._fetch_failed: bool = False
        self._last_success_at: float = 0.0
        self._in_flight: Optional[concurrent.futures.Future[Optional[PerfSnapshot]]] = (
            None
        )

    def _submit_fetch(self) -> concurrent.futures.Future[Optional[PerfSnapshot]]:
        """Submit _fetch() to a new daemon thread; return its Future (B1 pattern)."""
        fut: concurrent.futures.Future[Optional[PerfSnapshot]] = (
            concurrent.futures.Future()
        )

        def _run() -> None:
            try:
                fut.set_result(self._fetch())
            except Exception as exc:
                _LOG.debug("Perf fetch thread raised: %s", exc, exc_info=True)
                fut.set_exception(exc)

        threading.Thread(target=_run, name="pve-perf-poll", daemon=True).start()
        return fut

    def tick(self, now: float) -> "str | Text":
        """Return the rendered perf bar — NEVER blocks on network I/O (B1)."""
        if self._config is None:
            return ""

        # Collect completed in-flight result (non-blocking)
        if self._in_flight is not None and self._in_flight.done():
            try:
                fresh = self._in_flight.result()
                if fresh is not None:
                    self._snapshot = fresh
                    self._last_success_at = now
                    self._fetch_failed = False
                else:
                    self._fetch_failed = True
            except Exception as exc:
                _LOG.debug(
                    "Perf fetch failed (keeping stale snapshot): %s", exc, exc_info=True
                )
                self._fetch_failed = True
            self._in_flight = None

        # Submit a new fetch if due and none in-flight
        should_poll = not self._polled_once or (
            now - self._last_poll >= self._poll_interval_seconds
        )
        if should_poll and self._in_flight is None:
            self._in_flight = self._submit_fetch()
            self._last_poll = now
            self._polled_once = True

        if self._snapshot is None:
            return ""
        if self._fetch_failed:
            return render_perf_bar(self._snapshot, stale=True)
        return render_perf_bar(self._snapshot)

    def _fetch(self) -> Optional[PerfSnapshot]:
        """Try each node; return first successful PerfSnapshot or None (B1 thread)."""
        if self._config is None:
            return None

        cfg = self._config
        headers = {"Authorization": f"PVEAPIToken={cfg.token_id}={cfg.token_secret}"}
        verify = cfg.verify_ssl

        for base_url in cfg.nodes:
            try:
                base = base_url.rstrip("/")

                def _get(path: str, _base: str = base) -> Any:
                    resp = requests.get(
                        f"{_base}{path}",
                        headers=headers,
                        verify=verify,
                        timeout=_REQUEST_TIMEOUT,
                    )
                    resp.raise_for_status()
                    return resp.json()["data"]

                resources: List[Dict[str, Any]] = _get("/api2/json/cluster/resources")
                (
                    cpu_pct,
                    ram_pct,
                    ram_free_bytes,
                    cores,
                    online_nodes,
                    running_vms,
                    total_vms,
                ) = _aggregate_resources(resources)

                ceph_read_bps, ceph_write_bps, ceph_read_iops, ceph_write_iops = (
                    _fetch_ceph_rates(_get)
                )

                total_net_in = 0.0
                total_net_out = 0.0
                load_samples: List[float] = []
                for node_name in online_nodes:
                    net_in, net_out, load = _fetch_node_rrd(_get, node_name)
                    total_net_in += net_in
                    total_net_out += net_out
                    if load is not None:
                        load_samples.append(load)

                load_avg = (
                    sum(load_samples) / len(load_samples) if load_samples else 0.0
                )

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
                    net_in_bps=total_net_in,
                    net_out_bps=total_net_out,
                    running_vms=running_vms,
                    total_vms=total_vms,
                    fetched_at=time.monotonic(),
                )

            except (
                ConnectionError,
                Timeout,
                HTTPError,
                OSError,
                KeyError,
                ValueError,
                TypeError,
            ) as exc:
                _LOG.debug("Node %s unreachable for perf fetch: %s", base_url, exc)
                continue

        return None
