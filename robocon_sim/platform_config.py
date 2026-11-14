"""Hot-reloadable platform configuration: match defaults, scenario tables, frame options.

The file lives next to the data root (``<data>/platform.json``). It is read once
per created series, so editing it affects the next job and never a running or
queued one: every series freezes its expanded per-match configs in SQLite when it
is created. A malformed file keeps the last good configuration and records a
warning instead of taking the server down.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import sys
from typing import Any, Callable

from .config import MatchConfig

SCHEMA_VERSION = 1

# One template per pair of games. ``matches()`` picks ``templates[index // 2]``,
# so a ten-game set gets five templates and a two-game set gets one.
DEFAULT_SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "1": [{}],
    "2": [{}],
    "10": [
        {"supply_delays_ms": {"red": 9000, "blue": 10000}},
        {"supply_delays_ms": {"red": 8000, "blue": 11000}},
        {"supply_delays_ms": {"red": 11000, "blue": 9000}},
        {"supply_delays_ms": {"red": 9500, "blue": 10500}},
        {"supply_delays_ms": {"red": 9000, "blue": 10000},
         "faults": [{"team": "red", "role": "TR", "material_kind": "box",
                     "occurrence": 1, "mode": "upper_slip", "phase": "transport"}]},
    ],
}


@dataclass(frozen=True)
class FrameOptions:
    keyframe_interval: int = 100
    max_keyframe_bytes: int = 65536
    max_delta_ops: int = 512
    flush_interval: int = 100

    def __post_init__(self) -> None:
        if self.keyframe_interval < 1:
            raise ValueError("frame.keyframe_interval must be positive")
        if self.max_keyframe_bytes < 1 or self.max_delta_ops < 0 or self.flush_interval < 1:
            raise ValueError("frame limits must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"keyframe_interval": self.keyframe_interval,
                "max_keyframe_bytes": self.max_keyframe_bytes,
                "max_delta_ops": self.max_delta_ops,
                "flush_interval": self.flush_interval}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FrameOptions":
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError("frame must be an object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown frame option(s): {', '.join(sorted(unknown))}")
        return cls(**data)


@dataclass(frozen=True)
class PlatformConfig:
    default_match: MatchConfig
    scenarios: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: deepcopy(DEFAULT_SCENARIOS))
    frame: FrameOptions = field(default_factory=FrameOptions)
    schema: int = SCHEMA_VERSION
    path: str = ""

    def matches(self, count: int, overrides: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Expand one series into `count` frozen per-match configurations."""
        if count < 1:
            raise ValueError("count must be positive")
        base = self.default_match.to_dict()
        if overrides:
            base.update(deepcopy(overrides))
        base = MatchConfig.from_dict(base).to_dict()
        templates = self.scenarios.get(str(count)) or [{}]
        seed = int(base["seed"])
        expanded = []
        for index in range(count):
            patch = templates[min(index // 2, len(templates) - 1)]
            data = {**base, **deepcopy(patch)}
            data["seed"] = seed + index // 2
            expanded.append(MatchConfig.from_dict(data).to_dict())
        return expanded

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "default_match": self.default_match.public_dict(),
            "scenarios": deepcopy(self.scenarios),
            "frame": self.frame.to_dict(),
            "path": self.path,
        }


def default_platform() -> PlatformConfig:
    return PlatformConfig(default_match=MatchConfig(), scenarios=deepcopy(DEFAULT_SCENARIOS),
                          frame=FrameOptions())


def platform_dict() -> dict[str, Any]:
    """The document written to a fresh ``platform.json``."""
    config = default_platform()
    return {
        "schema": SCHEMA_VERSION,
        "default_match": {"duration_ms": config.default_match.duration_ms,
                          "tick_ms": config.default_match.tick_ms,
                          "seed": 0,
                          "parameters": {},
                          "supply_delays_ms": config.default_match.supply_delays_ms,
                          "faults": [],
                          "auto_entry": True},
        "scenarios": deepcopy(DEFAULT_SCENARIOS),
        "frame": config.frame.to_dict(),
    }


def _parse_scenarios(raw: Any, default_match: MatchConfig) -> dict[str, list[dict[str, Any]]]:
    if raw is None:
        return deepcopy(DEFAULT_SCENARIOS)
    if not isinstance(raw, dict):
        raise ValueError("scenarios must be an object keyed by match count")
    parsed: dict[str, list[dict[str, Any]]] = {}
    base = default_match.to_dict()
    for key, templates in raw.items():
        count = str(key)
        if count not in ("1", "2", "10"):
            raise ValueError(f"unsupported scenario set '{count}'")
        if not isinstance(templates, list) or not templates:
            raise ValueError(f"scenarios['{count}'] must be a non-empty list")
        rows = []
        for template in templates:
            if not isinstance(template, dict):
                raise ValueError(f"scenarios['{count}'] entries must be objects")
            # Validate every template once so a bad file is rejected at load time,
            # not in the middle of a series.
            MatchConfig.from_dict({**base, **deepcopy(template)})
            rows.append(deepcopy(template))
        parsed[count] = rows
    return parsed


class ConfigStore:
    """Reads ``platform.json`` and reloads it when the file changes on disk."""

    def __init__(self, path: str | Path, *, warn: Callable[[str], None] | None = None) -> None:
        self.path = Path(path)
        self._warn = warn or (lambda message: print(message, file=sys.stderr))
        self._current: PlatformConfig | None = None
        self._stamp: tuple[int, int] | None = None
        self._warnings: list[dict[str, Any]] = []

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return list(self._warnings)

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def current(self) -> PlatformConfig:
        stamp = self._file_stamp()
        if self._current is not None and stamp == self._stamp:
            return self._current
        return self.reload(stamp=stamp)

    def reload(self, *, stamp: tuple[int, int] | None = None) -> PlatformConfig:
        stamp = self._file_stamp() if stamp is None else stamp
        if stamp is None:
            if self._current is None:
                self._current = replace(default_platform(), path=str(self.path))
            self._stamp = None
            return self._current
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("platform configuration must be a JSON object")
            schema = int(raw.get("schema", SCHEMA_VERSION))
            if schema != SCHEMA_VERSION:
                raise ValueError(f"unsupported platform schema {schema}")
            default_match = MatchConfig.from_dict(raw.get("default_match") or {})
            config = PlatformConfig(
                default_match=default_match,
                scenarios=_parse_scenarios(raw.get("scenarios"), default_match),
                frame=FrameOptions.from_dict(raw.get("frame")),
                schema=schema,
                path=str(self.path),
            )
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            message = f"platform configuration ignored ({self.path}): {exc}"
            self._warnings.append({"path": str(self.path), "error": str(exc)})
            del self._warnings[:-10]
            self._warn(message)
            if self._current is None:
                self._current = replace(default_platform(), path=str(self.path))
            self._stamp = stamp
            return self._current
        self._current = config
        self._stamp = stamp
        return config
