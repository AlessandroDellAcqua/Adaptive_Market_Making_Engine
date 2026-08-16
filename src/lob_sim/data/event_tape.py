"""A compact, JSON-serializable record of a deterministic simulation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EventTape:
    """Append-only event log used for auditability and replay."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def append(self, kind: str, timestamp: float, **payload: Any) -> None:
        if not kind:
            raise ValueError("event kind must be non-empty")
        self.events.append({"kind": kind, "timestamp": timestamp, **payload})

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.events)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.events, indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> EventTape:
        events = json.loads(payload)
        if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
            raise ValueError("event tape JSON must contain a list of objects")
        return cls(events=events)

    def copy(self) -> EventTape:
        return EventTape(events=[dict(event) for event in self.events])

