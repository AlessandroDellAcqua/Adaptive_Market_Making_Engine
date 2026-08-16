"""Small replay cursor for deterministic event inspection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lob_sim.data.event_tape import EventTape


@dataclass(slots=True)
class ReplayCursor:
    tape: EventTape
    position: int = 0

    @property
    def done(self) -> bool:
        return self.position >= len(self.tape.events)

    def next(self) -> dict[str, Any]:
        if self.done:
            raise StopIteration
        event = self.tape.events[self.position]
        self.position += 1
        return event

