"""Independent allocation checkpoints for every virtual counter channel."""

from dataclasses import asdict, dataclass
from decimal import Decimal
from math import isfinite
from typing import Any

from .const import INCREASE, NET
from .sources import number


@dataclass
class Counter:
    mode: str
    total: Decimal = Decimal(0)
    source: str | None = None
    baseline: Decimal | None = None
    last_seen: float = 0
    last_reset: str | None = None
    pending_reset: Decimal | None = None


class CounterLedger:
    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        self.counters: dict[str, Counter] = {}
        for key, row in (stored or {}).items():
            row = dict(row)
            for field in ("total", "baseline", "pending_reset"):
                value = row[field]
                row[field] = number(value) if value is not None else None
                if value is not None and row[field] is None:
                    raise ValueError("Invalid counter checkpoint")
            if row["total"] is None or row["mode"] not in (INCREASE, NET):
                raise ValueError("Invalid counter checkpoint")
            if row["mode"] == INCREASE and row["total"] < 0:
                raise ValueError("Invalid counter total")
            if not isinstance(row["last_seen"], (int, float)) or not isfinite(
                row["last_seen"]
            ):
                raise ValueError("Invalid counter timestamp")
            if row["mode"] == INCREASE and any(
                row[field] is not None and row[field] < 0
                for field in ("baseline", "pending_reset")
            ):
                raise ValueError("Invalid counter baseline")
            self.counters[key] = Counter(**row)

    def ensure(self, key: str, mode: str) -> Counter:
        if key not in self.counters:
            self.counters[key] = Counter(mode=mode)
        if self.counters[key].mode != mode:
            raise ValueError("A stored counter's mode cannot change")
        return self.counters[key]

    def rebind(
        self,
        key: str,
        mode: str,
        source: str | None,
        value: Decimal | None,
        timestamp: float,
        reset: str | None = None,
    ) -> None:
        counter = self.ensure(key, mode)
        counter.source = source
        counter.baseline = (
            value if value is not None and (mode == NET or value >= 0) else None
        )
        counter.last_seen = timestamp
        counter.last_reset = reset
        counter.pending_reset = None

    def observe(
        self,
        key: str,
        value: Decimal | None,
        timestamp: float,
        reset: str | None = None,
    ) -> bool:
        counter = self.counters[key]
        if value is None or not value.is_finite() or timestamp <= counter.last_seen:
            return False
        if counter.mode == INCREASE and value < 0:
            return False
        previous = counter.baseline
        counter.last_seen = timestamp
        if previous is None:
            counter.baseline = value
            counter.last_reset = reset
            return True
        if (
            reset is not None
            and counter.last_reset is not None
            and reset != counter.last_reset
        ):
            delta = value
        elif counter.mode == INCREASE and value < previous:
            if counter.pending_reset is None or value < counter.pending_reset:
                counter.pending_reset = value
                return True
            delta = value - counter.pending_reset
        else:
            delta = value - previous
        counter.total += delta
        counter.baseline = value
        counter.last_reset = reset
        counter.pending_reset = None
        return True

    def total(self, key: str) -> Decimal:
        return self.counters[key].total if key in self.counters else Decimal(0)

    def as_dict(self) -> dict[str, Any]:
        result = {}
        for key, counter in self.counters.items():
            row = asdict(counter)
            for field in ("total", "baseline", "pending_reset"):
                row[field] = str(row[field]) if row[field] is not None else None
            result[key] = row
        return result
