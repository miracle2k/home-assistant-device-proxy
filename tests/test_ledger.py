"""Exact counter allocation, resets, checkpoint continuity, and net readings."""

from decimal import Decimal

import pytest

from custom_components.appliance_meter.const import INCREASE, NET
from custom_components.appliance_meter.ledger import CounterLedger


def test_rebind_counts_only_new_appliance_usage():
    ledger = CounterLedger()
    ledger.rebind("dryer:energy", INCREASE, "plug", Decimal("42.1"), 1)
    ledger.observe("dryer:energy", Decimal("42.3"), 2)
    ledger.rebind("dehumidifier:energy", INCREASE, "plug", Decimal("42.3"), 2)
    ledger.observe("dehumidifier:energy", Decimal("42.6"), 3)
    ledger.rebind("dryer:energy", INCREASE, "plug", Decimal("42.6"), 3)
    ledger.observe("dryer:energy", Decimal("42.7"), 4)
    assert ledger.total("dryer:energy") == Decimal("0.3")
    assert ledger.total("dehumidifier:energy") == Decimal("0.3")


def test_replacement_and_offline_handover_start_new_baselines():
    ledger = CounterLedger()
    ledger.rebind("dryer:energy", INCREASE, "one", Decimal(10), 1)
    ledger.observe("dryer:energy", Decimal(11), 2)
    ledger.rebind("dryer:energy", INCREASE, "two", None, 3)
    ledger.observe("dryer:energy", Decimal(2000), 4)
    ledger.observe("dryer:energy", Decimal(2001), 5)
    assert ledger.total("dryer:energy") == 2


def test_two_independent_counters_on_one_virtual_device():
    ledger = CounterLedger()
    for key, initial in (("energy", 100), ("water", 800)):
        ledger.rebind(f"machine:{key}", INCREASE, key, Decimal(initial), 1)
    ledger.observe("machine:energy", Decimal("100.2"), 2)
    ledger.observe("machine:water", Decimal(810), 2)
    assert ledger.total("machine:energy") == Decimal("0.2")
    assert ledger.total("machine:water") == 10


def test_transient_zero_and_checkpoint_do_not_double_count():
    ledger = CounterLedger()
    ledger.rebind("energy", INCREASE, "source", Decimal(100), 1)
    ledger.observe("energy", Decimal(0), 2)
    restored = CounterLedger(ledger.as_dict())
    restored.observe("energy", Decimal(101), 3)
    assert restored.total("energy") == 1


def test_confirmed_reset_keeps_total_monotonic():
    ledger = CounterLedger()
    ledger.rebind("energy", INCREASE, "source", Decimal(100), 1)
    ledger.observe("energy", Decimal(101), 2)
    ledger.observe("energy", Decimal("0.01"), 3)
    ledger.observe("energy", Decimal("0.21"), 4)
    assert ledger.total("energy") == Decimal("1.2")


def test_net_totals_can_decrease_and_be_negative():
    ledger = CounterLedger()
    ledger.rebind("net", NET, "source", Decimal(50), 1)
    ledger.observe("net", Decimal(52), 2)
    ledger.observe("net", Decimal(48), 3)
    assert ledger.total("net") == -2
    restored = CounterLedger(ledger.as_dict())
    restored.observe("net", Decimal(49), 4)
    assert restored.total("net") == -1


def test_explicit_reset_is_not_a_negative_net_increment():
    ledger = CounterLedger()
    ledger.rebind("net", NET, "source", Decimal(100), 1, "first-cycle")
    ledger.observe("net", Decimal(101), 2, "first-cycle")
    ledger.observe("net", Decimal(2), 3, "second-cycle")
    assert ledger.total("net") == 3


@pytest.mark.parametrize(
    "value", [None, Decimal("NaN"), Decimal("Infinity"), Decimal(-1)]
)
def test_invalid_or_negative_increasing_values_are_ignored(value):
    ledger = CounterLedger()
    ledger.rebind("energy", INCREASE, "source", Decimal(100), 1)
    assert not ledger.observe("energy", value, 2)
    ledger.observe("energy", Decimal(101), 3)
    assert ledger.total("energy") == 1


def test_stale_reports_and_missing_checkpoint_fields():
    ledger = CounterLedger()
    ledger.rebind("energy", INCREASE, "source", Decimal(100), 10)
    assert not ledger.observe("energy", Decimal(20), 9)
    ledger.observe("energy", Decimal(101), 11)
    data = ledger.as_dict()
    data["energy"]["total"] = "NaN"
    with pytest.raises(ValueError):
        CounterLedger(data)
