"""Synchronous, read-only strategy SDK. No networking and no world mutations."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from robocon_sim.planning import quote_action

SDK_VERSION = "0.1.0"
PROTOCOL_VERSION = 2


def _freeze(value: Any) -> Any:
    # Already-frozen values are reused instead of rebuilt. The map is frozen once
    # at startup and injected into every step by the worker, so this is the fast
    # path that keeps 21 KB of geometry out of the per-frame cost.
    if value is None or isinstance(value, (bool, int, float, str)):
        # Immutable scalars need no copy; deepcopy per leaf was measurable on a
        # payload with ~1000 leaves.
        return value
    if isinstance(value, (ReadOnlyView, tuple)):
        return value
    if isinstance(value, dict):
        return ReadOnlyView(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, ReadOnlyView):
        return {key: _thaw(item) for key, item in object.__getattribute__(value, "_data").items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return deepcopy(value)


class ReadOnlyView(Mapping[str, Any]):
    """Fields support both ``view.name`` and ``view['name']``.

    Field names take priority over mapping helper methods (notably obs.items).
    Conversion returns a new mutable copy, never access to the underlying world.
    """
    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]):
        object.__setattr__(self, "_data", MappingProxyType({key: _freeze(value) for key, value in data.items()}))

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_"):
            data = object.__getattribute__(self, "_data")
            if name in data:
                return data[name]
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("Observation fields are read-only")

    def __delattr__(self, name: str) -> None:
        raise TypeError("Observation fields are read-only")

    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, "_data")[key]

    def __iter__(self) -> Iterator[str]:
        return iter(object.__getattribute__(self, "_data"))

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_data"))

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReadOnlyView:
        return cls(data)


class Observation(ReadOnlyView):
    """One immutable common-boundary snapshot plus the team's private receipts."""
    __slots__ = ()


class Quote(ReadOnlyView):
    """A local estimate, not a reservation or a promise of future success."""
    __slots__ = ()


@dataclass(frozen=True)
class Action:
    kind: str
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", ReadOnlyView(deepcopy(dict(self.params))))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": _thaw(self.params)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        if set(data) != {"kind", "params"} or not isinstance(data["kind"], str) or not isinstance(data["params"], dict):
            raise ValueError("Action must contain kind and params")
        return cls(data["kind"], data["params"])

    @staticmethod
    def move_to(point_id: str) -> Action:
        return Action("move_to", {"point_id": point_id})

    @staticmethod
    def traverse(connection_id: str, destination_layer: str) -> Action:
        return Action("traverse", {"connection_id": connection_id, "destination_layer": destination_layer})

    @staticmethod
    def pick_stack(stack_id: str, expected_version: int) -> Action:
        return Action("pick_stack", {"stack_id": stack_id, "expected_version": expected_version})

    @staticmethod
    def build_stack(site_id: str, stack_id: str, expected_version: int) -> Action:
        return Action("build_stack", {"site_id": site_id, "stack_id": stack_id, "expected_version": expected_version})

    @staticmethod
    def put_down_stack(stack_id: str, bay_id: str, expected_version: int) -> Action:
        return Action("put_down_stack", {"stack_id": stack_id, "bay_id": bay_id, "expected_version": expected_version})

    @staticmethod
    def pick(item_ids: list[str] | tuple[str, ...]) -> Action:
        return Action("pick", {"item_ids": list(item_ids)})

    @staticmethod
    def build(site_id: str, item_id: str, level: str, expected_version: int) -> Action:
        return Action("build", {"site_id": site_id, "item_id": item_id, "level": level, "expected_version": expected_version})

    @staticmethod
    def flip_cap(site_id: str, expected_version: int) -> Action:
        return Action("flip_cap", {"site_id": site_id, "expected_version": expected_version})

    @staticmethod
    def receive_ball() -> Action:
        return Action("receive_ball", {})

    @staticmethod
    def place_ball() -> Action:
        return Action("place_ball", {})

    @staticmethod
    def take_from(item_id: str, source_id: str, expected_version: int) -> Action:
        return Action("take_from", {"item_id": item_id, "source_id": source_id, "expected_version": expected_version})

    @staticmethod
    def put_down(item_id: str, slot_id: str, expected_version: int) -> Action:
        return Action("put_down", {"item_id": item_id, "slot_id": slot_id, "expected_version": expected_version})


class Decision(ReadOnlyView):
    """One primary command and optionally one independent ball service request."""
    __slots__ = ()

    def __init__(self, based_on_seq: int | dict[str, Any], primary: dict[str, Any] | None=None,
                 request_ball: bool=False):
        if isinstance(based_on_seq, dict):
            super().__init__(based_on_seq)
        else:
            super().__init__({"based_on_seq": based_on_seq, "primary": primary, "request_ball": request_ball})


class StrategyAPI:
    """Local pure calculator and decision constructors for a single callback."""
    def __init__(self, obs: Observation | dict[str, Any], max_quotes: int=16):
        self._obs = obs if isinstance(obs, Observation) else Observation(obs)
        self._plain_obs: dict[str, Any] | None = None
        self._max_quotes, self._quotes = max_quotes, 0

    def quote(self, action: Action | dict[str, Any], after_stop: bool=False) -> Quote:
        if self._quotes >= self._max_quotes:
            raise RuntimeError(f"quote budget exceeded ({self._max_quotes} per callback)")
        self._quotes += 1
        data = action.to_dict() if isinstance(action, Action) else deepcopy(action)
        if self._plain_obs is None:
            self._plain_obs = self._obs.to_dict()
        return Quote(quote_action(self._plain_obs, data, after_stop))

    def execute(self, quote: Quote | dict[str, Any], request_ball: bool=False) -> Decision:
        data = quote.to_dict() if isinstance(quote, Quote) else deepcopy(quote)
        if not data.get("ok"):
            raise ValueError(f"Cannot execute rejected quote: {data.get('error')}")
        if data.get("preview_only"):
            raise ValueError("PREVIEW_ONLY: cancel, wait for stop, then quote again")
        if data.get("based_on_seq") != self._obs["seq"]:
            raise ValueError("STALE_OBSERVATION: quote again on this callback")
        return Decision(self._obs["seq"], {"kind": "execute", "action": data["action"],
            "quote_seq": data["based_on_seq"], "preview_only": False}, request_ball)

    def cancel(self, action_id: str, request_ball: bool=False) -> Decision:
        return Decision(self._obs["seq"], {"kind": "cancel", "action_id": action_id}, request_ball)

    def request_ball(self) -> Decision:
        return Decision(self._obs["seq"], None, True)


class Strategy:
    """Override these synchronous callbacks. Returning None keeps current action."""
    def on_start(self, context: Any) -> None:
        pass

    def on_step(self, obs: Observation, api: StrategyAPI) -> Decision | None:
        return None


__all__ = ["Strategy", "Action", "Quote", "Decision", "Observation", "StrategyAPI",
           "SDK_VERSION", "PROTOCOL_VERSION"]
