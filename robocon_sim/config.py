"""Public teaching parameters and private reproducible match settings."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


RATING_TIMING_PROFILE = "br-3000-place-2000"

DEFAULT_PARAMETERS: dict[str, int | str] = {
    "empty_speed_mm_s": 3000, "loaded_speed_mm_s": 3000,
    "ball_speed_mm_s": 3000, "acceleration_mm_s2": 1000,
    "braking_mm_s2": 1000, "turn_90_ms": 1500,
    "pick_one_ms": 1500, "pick_two_ms": 2500,
    "stack_pick_ms": 2000, "stack_build_ms": 1500,
    "stack_return_ms": 1000, "build_base_ms": 1000,
    "build_middle_ms": 1500, "build_cap_ms": 1500,
    "flip_cap_ms": 6000, "l2_up_ms": 5000, "l2_down_ms": 4000,
    "receive_ball_ms": 2000, "place_ball_ms": 2000,
    "put_down_ms": 750, "take_from_ms": 1500,
    "entry_ms": 8000, "ramp_up_ms": 6000, "ramp_down_ms": 5000,
    "stairs_up_ms": 6000, "stairs_down_ms": 5000,
    "retry_return_ms": 3000, "retry_hold_ms": 15000,
    "box_clearance_ms": 2000, "retry_clearance_ms": 2000,
    "central_clearance_ms": 2000, "ball_transport_ms": 6000,
    "tr_retry_return_ms": 3000, "tr_retry_clearance_ms": 2000,
    "parameter_status": "教学初值，未经实车标定",
}


@dataclass
class MatchConfig:
    duration_ms: int = 180000
    tick_ms: int = 100
    seed: int = 0
    parameters: dict[str, Any] = field(default_factory=dict)
    # Per-side overrides on top of `parameters`, keyed by team. Empty by default,
    # which keeps both robots on one physical model. A side profile with a
    # different speed set only needs to be spelled out for the side that differs.
    sides: dict[str, dict[str, Any]] = field(default_factory=dict)
    supply_delays_ms: dict[str, int] = field(default_factory=lambda: {"red": 9000, "blue": 10000})
    faults: list[dict[str, Any]] = field(default_factory=list)
    auto_entry: bool = True

    def __post_init__(self) -> None:
        def positive_int(value: Any, name: str) -> None:
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        def check_parameters(values: Any, name: str) -> None:
            if not isinstance(values, dict) or set(values) - set(DEFAULT_PARAMETERS):
                raise ValueError(f"{name} must contain only published parameter names")
            for key, value in values.items():
                if key == "parameter_status":
                    if not isinstance(value, str):
                        raise ValueError(f"{name}.parameter_status must be a string")
                else:
                    positive_int(value, f"{name}.{key}")
        positive_int(self.duration_ms, "duration_ms")
        positive_int(self.tick_ms, "tick_ms")
        if type(self.seed) is not int or type(self.auto_entry) is not bool:
            raise ValueError("seed must be an integer and auto_entry a boolean")
        check_parameters(self.parameters, "parameters")
        if not isinstance(self.sides, dict) or set(self.sides) - {"red", "blue"}:
            raise ValueError("sides must be an object keyed by red and blue")
        for team, values in self.sides.items():
            check_parameters(values, f"sides.{team}")
        if not isinstance(self.supply_delays_ms, dict) or set(self.supply_delays_ms) != {"red", "blue"}:
            raise ValueError("supply_delays_ms must contain red and blue")
        for name, value in self.supply_delays_ms.items():
            positive_int(value, f"supply_delays_ms.{name}")
        if not isinstance(self.faults, list) or any(not isinstance(f, dict) for f in self.faults):
            raise ValueError("faults must be a list of fault objects")
        self.parameters = {**DEFAULT_PARAMETERS, **deepcopy(self.parameters)}
        self.sides = {team: deepcopy(values) for team, values in deepcopy(self.sides).items()}
        self.supply_delays_ms = deepcopy(self.supply_delays_ms)
        self.faults = deepcopy(self.faults)

    def merged_parameters(self, team: str) -> dict[str, Any]:
        """The physical parameter set a single robot runs with."""
        override = self.sides.get(team)
        if not override:
            return self.parameters
        return {**self.parameters, **override}

    def sides_differ(self) -> bool:
        return self.merged_parameters("red") != self.merged_parameters("blue")

    def to_dict(self) -> dict[str, Any]:
        return deepcopy({"duration_ms": self.duration_ms, "tick_ms": self.tick_ms,
                         "seed": self.seed, "parameters": self.parameters, "sides": self.sides,
                         "supply_delays_ms": self.supply_delays_ms,
                         "faults": self.faults, "auto_entry": self.auto_entry})

    def public_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        del data["seed"]
        del data["faults"]
        # Each policy is told its own parameters explicitly; the opponent's set is
        # not part of the public context.
        del data["sides"]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MatchConfig:
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown match configuration field")
        return cls(**deepcopy(data))
