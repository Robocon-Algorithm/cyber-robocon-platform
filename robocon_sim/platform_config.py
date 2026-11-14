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
import random
import sys
from typing import Any, Callable
import zlib

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
class SideProfile:
    """One robot's physical parameter set, loaded from ``<data>/profiles/<name>.json``."""

    name: str
    label: str
    parameters: dict[str, Any]
    notes: str = ""
    path: str = ""

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return f"{zlib.crc32(canonical.encode('utf-8')):08x}"

    def rating_key(self) -> str:
        """Ratings are only comparable on identical physics, so the digest is part of the key."""
        return f"{self.name}@{self.digest}"

    def public_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "digest": self.digest,
                "notes": self.notes, "parameters": deepcopy(self.parameters)}


# Competition-grade numbers: 5 m/s straight line, 3.5 m/s^2 accel/brake
# (0-5 m/s in 1.4 s over 3.6 m), and mechanism moves as short as a real robot
# manages when the lift/stairs/turn run in parallel with driving.
COMPETITION_PARAMETERS: dict[str, int | str] = {
    "empty_speed_mm_s": 5000, "loaded_speed_mm_s": 5000, "ball_speed_mm_s": 4500,
    "acceleration_mm_s2": 3500, "braking_mm_s2": 3500,
    "turn_90_ms": 400,
    "entry_ms": 4000,
    "ramp_up_ms": 2000, "ramp_down_ms": 1500,
    "stairs_up_ms": 2000, "stairs_down_ms": 1500,
    "l2_up_ms": 1800, "l2_down_ms": 1200,
    "pick_one_ms": 700, "pick_two_ms": 1200,
    "stack_pick_ms": 900, "stack_build_ms": 700, "stack_return_ms": 500,
    "build_base_ms": 600, "build_middle_ms": 800, "build_cap_ms": 800,
    "flip_cap_ms": 1500,
    "receive_ball_ms": 900, "place_ball_ms": 900,
    "put_down_ms": 400, "take_from_ms": 800,
    "ball_transport_ms": 3000,
    "parameter_status": "赛级目标值（5 m/s 直线 + 并行机构动作）",
}

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "baseline": {"label": "赛级基线", "parameters": dict(COMPETITION_PARAMETERS),
                 "notes": "5 m/s 直线；转向、上下楼、坡道与取放动作按并行机构压缩。"},
    "training": {"label": "训练慢速", "parameters": {},
                 "notes": "config.py 的教学初值（3 m/s），用于对比提速前后的差别。"},
}


def builtin_profiles() -> dict[str, SideProfile]:
    out = {}
    for name, spec in BUILTIN_PROFILES.items():
        merged = MatchConfig(parameters=dict(spec["parameters"])).parameters
        out[name] = SideProfile(name=name, label=spec["label"], parameters=merged,
                                notes=spec["notes"])
    return out


@dataclass(frozen=True)
class PerturbationOptions:
    """Seeded jitter applied to a whole series so a run is still reproducible."""

    enabled: bool = True
    applies_to: tuple[str, ...] = ("official",)
    speed_pct: float = 3.0
    accel_pct: float = 3.0
    time_pct: float = 2.0

    def __post_init__(self) -> None:
        for name in ("speed_pct", "accel_pct", "time_pct"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not 0 <= value <= 50:
                raise ValueError(f"perturbation.{name} must be between 0 and 50")
        if not isinstance(self.applies_to, (list, tuple)):
            raise ValueError("perturbation.applies_to must be a list")
        if set(self.applies_to) - {"official", "manual", "auto_round", "all_play"}:
            raise ValueError("perturbation.applies_to contains an unknown batch kind")

    def applies(self, kind: str) -> bool:
        return self.enabled and kind in self.applies_to

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "applies_to": list(self.applies_to),
                "speed_pct": self.speed_pct, "accel_pct": self.accel_pct, "time_pct": self.time_pct}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PerturbationOptions":
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError("perturbation must be an object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown perturbation option(s): {', '.join(sorted(unknown))}")
        payload = dict(data)
        if "applies_to" in payload:
            payload["applies_to"] = tuple(payload["applies_to"])
        return cls(**payload)


def parameter_groups(parameters: dict[str, Any]) -> dict[str, list[str]]:
    speeds = [k for k in parameters if k.endswith("_speed_mm_s")]
    accels = [k for k in parameters if k.endswith("_mm_s2")]
    times = [k for k in parameters if k.endswith("_ms")]
    return {"speed": speeds, "accel": accels, "time": times}


@dataclass(frozen=True)
class PlatformConfig:
    default_match: MatchConfig
    scenarios: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: deepcopy(DEFAULT_SCENARIOS))
    frame: FrameOptions = field(default_factory=FrameOptions)
    profiles: dict[str, SideProfile] = field(default_factory=builtin_profiles)
    default_profiles: dict[str, str] = field(default_factory=lambda: {"red": "baseline", "blue": "baseline"})
    perturbation: PerturbationOptions = field(default_factory=PerturbationOptions)
    schema: int = SCHEMA_VERSION
    path: str = ""

    def perturb(self, parameters: dict[str, Any], key: str) -> tuple[dict[str, Any], dict[str, float]]:
        """One seeded jitter set for a whole series, so both games share it.

        Symmetric on purpose: the pair swaps sides, and giving one side a slower
        robot would turn a physics test into an unfair fight.
        """
        groups = parameter_groups(parameters)
        seed = zlib.crc32(key.encode("utf-8"))
        rng = random.Random(seed)
        out = dict(parameters)
        factors: dict[str, float] = {}
        for group, percent in (("speed", self.perturbation.speed_pct),
                               ("accel", self.perturbation.accel_pct),
                               ("time", self.perturbation.time_pct)):
            factor = 1 + rng.uniform(-percent, percent) / 100
            factors[group] = round(factor, 5)
            for name in groups[group]:
                out[name] = max(1, int(round(out[name] * factor)))
        return out, factors

    def profile(self, name: str | None, team: str) -> SideProfile:
        chosen = name or self.default_profiles.get(team) or ""
        found = self.profiles.get(chosen)
        if found is not None:
            return found
        if self.profiles:
            return self.profiles[sorted(self.profiles)[0]]
        return builtin_profiles()["baseline"]

    def matches(self, count: int, overrides: dict[str, Any] | None = None, *,
                red_profile: str | None = None, blue_profile: str | None = None) -> list[dict[str, Any]]:
        """Expand one series into `count` frozen per-match configurations.

        Each side gets its own parameter set. When both sides resolve to the same
        profile the match keeps a single shared set, which is what ranked play
        requires; a perturbation run only spells out the side that differs.
        """
        if count < 1:
            raise ValueError("count must be positive")
        red = self.profile(red_profile, "red")
        blue = self.profile(blue_profile, "blue")
        base = self.default_match.to_dict()
        base["parameters"] = deepcopy(red.parameters)
        base["sides"] = {} if red.parameters == blue.parameters else {"blue": deepcopy(blue.parameters)}
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
            "perturbation": self.perturbation.to_dict(),
            "profiles": [self.profiles[name].public_dict() for name in sorted(self.profiles)],
            "default_profiles": dict(self.default_profiles),
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
        "profiles": dict(config.default_profiles),
        "scenarios": deepcopy(DEFAULT_SCENARIOS),
        "frame": config.frame.to_dict(),
        "perturbation": config.perturbation.to_dict(),
    }


def profile_dict(name: str) -> dict[str, Any]:
    """The document written to a fresh ``profiles/<name>.json``.

    Only the parameters that differ from the built-in teaching defaults are
    written, so a file doubles as the list of what was changed.
    """
    spec = BUILTIN_PROFILES[name]
    return {"label": spec["label"], "notes": spec["notes"],
            "parameters": dict(spec["parameters"])}


def seed_platform_files(root: str | Path) -> list[Path]:
    """Write editable defaults for a fresh data directory; never overwrites."""
    root = Path(root)
    written = []
    document = root / 'platform.json'
    if not document.exists():
        root.mkdir(parents=True, exist_ok=True)
        document.write_text(json.dumps(platform_dict(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        written.append(document)
    profile_dir = root / 'profiles'
    if not any(profile_dir.glob('*.json')):
        profile_dir.mkdir(parents=True, exist_ok=True)
        for name in BUILTIN_PROFILES:
            path = profile_dir / (name + '.json')
            path.write_text(json.dumps(profile_dict(name), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            written.append(path)
    return written


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
    """Reads ``platform.json`` plus ``profiles/*.json`` and reloads on change.

    Both the document and the profile directory are watched, so editing one side
    profile is picked up by the next created series without restarting anything.
    """

    def __init__(self, path: str | Path, *, warn: Callable[[str], None] | None = None) -> None:
        self.path = Path(path)
        self.profile_dir = self.path.parent / 'profiles'
        self._warn = warn or (lambda message: print(message, file=sys.stderr))
        self._current: PlatformConfig | None = None
        self._stamp: object = None
        self._warnings: list[dict[str, Any]] = []

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return list(self._warnings)

    def _stamp_now(self) -> tuple[Any, Any]:
        """Content hashes of the document and every profile file.

        Timestamps are not enough on their own: two saves inside one filesystem
        tick produce an identical mtime and size, and the second edit would be
        silently ignored. The files are a couple of kilobytes, and this runs once
        per created series, not per frame.
        """
        def digest_of(path: Path) -> int | None:
            try:
                return zlib.crc32(path.read_bytes())
            except OSError:
                return None

        files = []
        try:
            for entry in sorted(self.profile_dir.glob('*.json')):
                files.append((entry.name, digest_of(entry)))
        except OSError:
            files = []
        return digest_of(self.path), tuple(files)

    def current(self) -> PlatformConfig:
        stamp = self._stamp_now()
        if self._current is not None and stamp == self._stamp:
            return self._current
        return self.reload(stamp=stamp)

    def _load_profiles(self, default_profiles: dict[str, str]) -> dict[str, SideProfile]:
        profiles = builtin_profiles()
        files = sorted(self.profile_dir.glob('*.json')) if self.profile_dir.is_dir() else []
        loaded = {}
        for entry in files:
            try:
                raw = json.loads(entry.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict):
                    raise ValueError("profile must be a JSON object")
                parameters = raw.get("parameters") or {}
                if not isinstance(parameters, dict):
                    raise ValueError("profile parameters must be an object")
                merged = MatchConfig(parameters=parameters).parameters
                loaded[entry.stem] = SideProfile(
                    name=entry.stem,
                    label=str(raw.get("label") or entry.stem),
                    parameters=merged,
                    notes=str(raw.get("notes") or ""),
                    path=str(entry),
                )
            except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                self._record_warning(entry, exc)
        if loaded:
            # Files on disk win; built-ins remain as fallbacks for the other names.
            profiles.update(loaded)
        for team, name in default_profiles.items():
            if name not in profiles:
                self._record_warning(self.profile_dir, ValueError(
                    f"default {team} profile '{name}' does not exist; using '{sorted(profiles)[0]}'"))
                default_profiles[team] = sorted(profiles)[0]
        return profiles

    def _record_warning(self, path: Path, exc: BaseException) -> None:
        self._warnings.append({"path": str(path), "error": str(exc)})
        del self._warnings[:-10]
        self._warn(f"platform configuration ignored ({path}): {exc}")

    def reload(self, *, stamp: object = None) -> PlatformConfig:
        stamp = self._stamp_now() if stamp is None else stamp
        document_stamp, _files = stamp
        raw: dict[str, Any] = {}
        if document_stamp is not None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict):
                    raise ValueError("platform configuration must be a JSON object")
            except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                self._record_warning(self.path, exc)
                if self._current is not None:
                    # Keep serving the last good document; a broken edit must not
                    # silently reset the platform to factory defaults.
                    self._stamp = stamp
                    return self._current
                raw = {}
        try:
            schema = int(raw.get("schema", SCHEMA_VERSION))
            if schema != SCHEMA_VERSION:
                raise ValueError(f"unsupported platform schema {schema}")
            default_match = MatchConfig.from_dict(raw.get("default_match") or {})
            default_profiles = raw.get("profiles") or {}
            if not isinstance(default_profiles, dict):
                raise ValueError("profiles must map red/blue to a profile name")
            unknown = set(default_profiles) - {"red", "blue"}
            if unknown:
                raise ValueError(f"unknown profile slot(s): {', '.join(sorted(unknown))}")
            defaults = {"red": "baseline", "blue": "baseline"}
            defaults.update({team: str(name) for team, name in default_profiles.items()})
            config = PlatformConfig(
                default_match=default_match,
                scenarios=_parse_scenarios(raw.get("scenarios"), default_match),
                frame=FrameOptions.from_dict(raw.get("frame")),
                perturbation=PerturbationOptions.from_dict(raw.get("perturbation")),
                profiles=self._load_profiles(defaults),
                default_profiles=defaults,
                schema=schema,
                path=str(self.path),
            )
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            self._record_warning(self.path, exc)
            if self._current is not None:
                self._stamp = stamp
                return self._current
            config = replace(default_platform(), path=str(self.path))
        self._current = config
        self._stamp = stamp
        return config
