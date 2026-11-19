"""Clairvoyant baseline: the best fixed provider sequence per segment under the
TRUE simulated parameters. Used only to measure regret in simulation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wfo.models import Segment


class SequenceRouter:
    def __init__(self, sequences: Mapping[str, Sequence[str]], name: str = "oracle") -> None:
        self.sequences = {k: list(v) for k, v in sequences.items()}
        self.name = name

    def choose(
        self, segment: Segment, candidates: Sequence[str], depth: int, remaining_usd: float
    ) -> str | None:
        allowed = set(candidates)
        for p in self.sequences.get(segment.key, []):
            if p in allowed:
                return p
        return None

    def observe(
        self, segment: Segment, provider: str, depth: int, success: bool, found: bool = True
    ) -> None:
        return None

    def state_dict(self) -> dict[str, Any]:
        return {"sequences": self.sequences}

    def load_state(self, state: Mapping[str, Any]) -> None:
        self.sequences = {k: list(v) for k, v in state.get("sequences", {}).items()}
