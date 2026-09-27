"""Pre-dispatch estimates (D08): explainable, conservative, never invented.

An estimate says what one provider turn is expected to use of one metric, as
a range. Admission reserves the high end. The heuristics are deliberately
simple so a person can check them:

- turns: exactly one per invocation;
- anything else (estimated cost, tokens): from this provider's recent
  observed turns. The high end is the largest recent turn plus headroom
  (50% with fewer than five samples, 25% after that). With no observed turn
  the estimate is unknown, and admission applies its bounded unknown-size
  policy instead of pretending a number.

No calibrated completion probability is claimed (spec 7.4). Re-estimation
happens naturally: every finished turn adds a sample, and finishing reserves
are resized from the new estimate."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Sequence

TURNS = "turns"
MAX_SAMPLES = 20
FEW_SAMPLES = 5


@dataclass(frozen=True)
class Estimate:
    metric: str
    low: Decimal | None
    high: Decimal | None  # what admission reserves; None: unknown
    quality: str  # exact | estimated | unknown
    basis: str
    samples: int = 0

    @property
    def known(self) -> bool:
        return self.high is not None

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "low": None if self.low is None else str(self.low),
            "high": None if self.high is None else str(self.high), "quality": self.quality,
            "basis": self.basis, "samples": self.samples,
        }


def _quantize(value: Decimal, like: Sequence[Decimal]) -> Decimal:
    """Round up to the finest precision seen in the samples (at least cents
    for money-like values), so estimates stay readable and never round down."""
    places = max([max(0, -s.as_tuple().exponent) for s in like] + [2])
    step = Decimal(1).scaleb(-min(places, 8))
    return value.quantize(step, rounding=ROUND_CEILING)


def estimate_turn(metric: str, history: Sequence[Decimal]) -> Estimate:
    """history: this provider's known per-turn quantities, newest last."""
    if metric == TURNS:
        return Estimate(metric, Decimal(1), Decimal(1), "exact", "one invocation", 0)
    samples = [Decimal(h) for h in history if h is not None and Decimal(h) >= 0][-MAX_SAMPLES:]
    if not samples:
        return Estimate(metric, None, None, "unknown", "no observed turn yet", 0)
    headroom = Decimal("1.5") if len(samples) < FEW_SAMPLES else Decimal("1.25")
    high = _quantize(max(samples) * headroom, samples)
    basis = f"largest of {len(samples)} recent turn{'s' if len(samples) != 1 else ''} x{headroom}"
    return Estimate(metric, min(samples), high, "estimated", basis, len(samples))


def finishing_units(purpose: str, *, review_rounds: int, repair_turns: int) -> int:
    """How many turns the finishing reserve holds for a purpose: the initial
    review plus one re-review after a repair, and the repairs themselves."""
    if purpose == "review":
        return review_rounds
    if purpose == "repair":
        return repair_turns
    raise ValueError(f"no finishing reserve for {purpose!r}")
