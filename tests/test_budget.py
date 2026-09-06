import pytest

from zzz_ddtool.budget import DiscBudget


def test_budget_stops_after_limit() -> None:
    budget = DiscBudget(3)
    assert budget.consume()
    assert budget.consume()
    assert budget.consume()
    assert not budget.consume()
    assert budget.exhausted
    assert budget.processed == 3


def test_budget_rejects_zero() -> None:
    with pytest.raises(ValueError):
        DiscBudget(0)
