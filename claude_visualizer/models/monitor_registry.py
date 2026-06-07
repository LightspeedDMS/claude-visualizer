"""Pluggable monitor registry — pure, no Textual import.

Scans ``config.monitors_dir`` for ``*.py`` files, imports each one, instantiates
the ``Monitor`` class it exposes, and calls ``tick(now)`` on every instance each
refresh cycle. Instances are retained so per-monitor state persists across ticks.

Extension model
---------------
Drop a ``.py`` file in ``~/.claude-visualizer/monitors/`` exposing::

    class Monitor:
        def tick(self, now: float) -> str | rich.text.Text:
            ...

- A non-empty return value contributes one bar line (renderer decides display).
- An empty string ``""`` or ``Text("")`` passes through to the renderer, which
  suppresses the line so no blank row appears (AC2).
- Files whose name starts with ``_`` are skipped (excludes ``__init__.py``).
- If ``monitors_dir`` does not exist → empty monitor list, no error.
- Any exception during import / missing ``Monitor`` / instantiation → caught,
  formatted into ``load_errors``, file skipped, scan continues.
- Any exception during a monitor's ``tick()`` → per-slot ⚠ warning string
  truncated to ``config.monitor_error_width``; other monitors continue (AC5).
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple, Union

from rich.text import Text

if TYPE_CHECKING:
    from claude_visualizer.config import AppConfig

# Type alias for what a monitor may return.
MonitorLine = Union[str, Text]


class MonitorRegistry:
    """Scans and ticks pluggable monitor modules.

    Usage::

        registry = MonitorRegistry(config)
        registry.load()          # scan dir, import modules, log errors
        lines = registry.tick(now)  # list[str | Text], one entry per monitor
    """

    def __init__(self, config: "AppConfig") -> None:
        self._config = config
        # list of (filename_stem, instance) retained so state persists across
        # ticks.  Populated by load(); empty until then.
        self._monitors: List[Tuple[str, object]] = []
        # Errors accumulated during load() — each entry is a human-readable
        # "<filename>: <error>" string.  Never raises; errors here are advisory.
        self.load_errors: List[str] = []

    def load(self) -> None:
        """Scan ``monitors_dir`` and import all eligible monitor modules.

        Safe to call once on startup. Re-calling replaces the monitor list.
        Alphabetical filename order is preserved (AC1). Files whose name starts
        with ``_`` are skipped. Missing directory → empty list, no error.
        """
        self._monitors = []
        self.load_errors = []

        monitors_dir = Path(self._config.monitors_dir)
        if not monitors_dir.exists():
            return

        for py_file in sorted(monitors_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue  # skip __init__.py, _helpers.py, etc. (AC1)
            self._load_one(py_file)

    def _load_one(self, py_file: Path) -> None:
        """Import one monitor file and append its instance to ``_monitors``.

        Any failure (syntax error, missing Monitor attribute, instantiation
        exception) is caught, formatted as ``"<filename>: <error>"``, appended
        to ``load_errors``, and the file is skipped.
        """
        filename = py_file.name
        # Collision-free module key derived from the file's ABSOLUTE path (not
        # just the stem) so two monitors sharing a filename in different dirs
        # cannot shadow each other in sys.modules.
        path_digest = hashlib.sha1(str(py_file.resolve()).encode("utf-8")).hexdigest()[
            :8
        ]
        module_name = f"_cv_monitor_{py_file.stem}_{path_digest}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot create module spec for {filename}")
            module = importlib.util.module_from_spec(spec)
            # Register in sys.modules so relative imports inside the module work;
            # use a unique key to prevent collisions across different monitor dirs.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            monitor_cls = getattr(module, "Monitor")
            instance = monitor_cls()
            self._monitors.append((filename, instance))
        except Exception as exc:
            self.load_errors.append(f"{filename}: {exc}")

    def tick(self, now: float) -> List[MonitorLine]:
        """Call ``tick(now)`` on every loaded monitor; return results in load order.

        Per-monitor exceptions are caught: the failing slot is replaced with
        ``"⚠ <filename>: <truncated error>"`` (AC5). Other monitors continue.
        The list is returned in alphabetical filename order (AC1).
        """
        results: List[MonitorLine] = []
        error_width = self._config.monitor_error_width
        for filename, instance in self._monitors:
            try:
                line: MonitorLine = instance.tick(now)  # type: ignore[attr-defined]
                results.append(line)
            except Exception as exc:
                warning = f"⚠ {filename}: {exc}"[:error_width]
                results.append(warning)
        return results
