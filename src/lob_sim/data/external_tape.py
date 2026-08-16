"""Persisted event tapes for the external Project 1 order-flow simulator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from lob_sim.synthetic.generator import MarketPath, MarketPoint


@dataclass(frozen=True, slots=True)
class ExternalEventTape:
    """A replayable reference path plus external exchange events.

    The tape stores the event records emitted by Project 1's generator rather
    than the strategy's orders. This allows several market-making strategies to
    replay the same external scenario while their own orders interact with the
    matching engine independently.
    """

    market_path: MarketPath
    events: tuple[dict[str, Any], ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected = max(0, len(self.market_path.points) - 1)
        if len(self.events) != expected:
            raise ValueError(
                f"event tape contains {len(self.events)} events but the path requires {expected}"
            )
        previous_timestamp = -1
        event_ids: set[str] = set()
        for index, event in enumerate(self.events, start=1):
            self._validate_event(event, index)
            event_id = event["event_id"]
            if event_id in event_ids:
                raise ValueError(f"event {index} has a duplicate event_id")
            event_ids.add(event_id)
            timestamp = event["timestamp"]
            if timestamp < previous_timestamp:
                raise ValueError("external event timestamps must be non-decreasing")
            if timestamp > expected:
                raise ValueError("external event timestamp exceeds tape event range")
            previous_timestamp = timestamp

    @property
    def digest(self) -> str:
        """Stable SHA-256 identifier for audit logs and reproducibility checks."""

        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "lob_sim.external_event_tape.v1",
            "market_path": {
                "seed": self.market_path.seed,
                "points": [asdict(point) for point in self.market_path.points],
            },
            "events": list(self.events),
            "metadata": self.metadata,
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> ExternalEventTape:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("external event tape JSON must contain an object")
        return cls.from_dict(decoded)

    def write(self, path: str | Path, *, indent: int = 2) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")

    def with_toxic_response(self, response_ticks: float) -> ExternalEventTape:
        """Add a causal next-step price response to aggressive event flow.

        A market buy moves the next reference point up and a market sell moves
        it down. The original Gaussian path increment is retained, so the
        response is a controlled treatment layered on top of the same noise.
        The event at interval ``t`` only changes the point observed at ``t+1``.
        """

        if response_ticks < 0:
            raise ValueError("response_ticks cannot be negative")
        points = self.market_path.points
        adjusted = [points[0]]
        for index, event in enumerate(self.events):
            previous = adjusted[-1]
            original_next = points[index + 1]
            base_move = original_next.reference_price_ticks - points[index].reference_price_ticks
            direction = 0
            if event.get("event_type") == "market":
                direction = 1 if event.get("side") == "buy" else -1
            price = max(
                1,
                round(previous.reference_price_ticks + base_move + direction * response_ticks),
            )
            half_spread = (original_next.best_ask_ticks - original_next.best_bid_ticks) / 2
            adjusted.append(
                replace(
                    original_next,
                    reference_price_ticks=price,
                    best_bid_ticks=round(price - half_spread),
                    best_ask_ticks=round(price + half_spread),
                )
            )
        return ExternalEventTape(
            market_path=MarketPath(points=tuple(adjusted), seed=self.market_path.seed),
            events=self.events,
            metadata={**self.metadata, "toxic_response_ticks": response_ticks},
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExternalEventTape:
        if payload.get("format") != "lob_sim.external_event_tape.v1":
            raise ValueError("unsupported external event-tape format")
        path_payload = payload.get("market_path")
        if not isinstance(path_payload, dict):
            raise ValueError("external event tape requires a market_path object")
        points_payload = path_payload.get("points")
        if not isinstance(points_payload, list):
            raise ValueError("market_path.points must be a list")
        points = tuple(MarketPoint(**point) for point in points_payload)
        events_payload = payload.get("events")
        if not isinstance(events_payload, list) or any(
            not isinstance(event, dict) for event in events_payload
        ):
            raise ValueError("external event tape events must be a list of objects")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("external event tape metadata must be an object")
        return cls(
            market_path=MarketPath(
                points=points,
                seed=int(path_payload.get("seed", 0)),
            ),
            events=tuple(dict(event) for event in events_payload),
            metadata=dict(metadata),
        )

    @classmethod
    def read(cls, path: str | Path) -> ExternalEventTape:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("external event tape JSON must contain an object")
        return cls.from_dict(payload)

    @staticmethod
    def _validate_event(event: dict[str, Any], position: int) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError(f"event {position} requires a non-empty event_id")
        event_type = event.get("event_type")
        if event_type not in {"limit", "market", "cancel", "modify"}:
            raise ValueError(f"event {position} has an invalid event_type")
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError(f"event {position} timestamp must be a non-negative integer")
        if event_type == "limit":
            if event.get("side") not in {"buy", "sell"}:
                raise ValueError(f"event {position} limit requires side")
            if not isinstance(event.get("order_id"), str) or not event["order_id"]:
                raise ValueError(f"event {position} limit requires order_id")
            if not isinstance(event.get("price"), int) or event["price"] <= 0:
                raise ValueError(f"event {position} limit requires positive price")
            if not isinstance(event.get("quantity"), int) or event["quantity"] <= 0:
                raise ValueError(f"event {position} limit requires positive quantity")
        elif event_type == "market":
            if event.get("side") not in {"buy", "sell"}:
                raise ValueError(f"event {position} market requires side")
            if not isinstance(event.get("quantity"), int) or event["quantity"] <= 0:
                raise ValueError(f"event {position} market requires positive quantity")
        elif event_type == "cancel":
            if not isinstance(event.get("order_id"), str) or not event["order_id"]:
                raise ValueError(f"event {position} cancel requires order_id")
        else:
            if not isinstance(event.get("order_id"), str) or not event["order_id"]:
                raise ValueError(f"event {position} modify requires order_id")
            if not isinstance(event.get("quantity"), int) or event["quantity"] < 0:
                raise ValueError(f"event {position} modify requires non-negative quantity")
