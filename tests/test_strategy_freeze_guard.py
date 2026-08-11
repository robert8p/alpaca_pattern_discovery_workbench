import pytest

from app.research_ledger import REQUIRED_DEPLOYMENT_METHODOLOGY_FIELDS, _validate_deployment_methodology


def _complete_methodology():
    return {field: f"fixed:{field}" for field in REQUIRED_DEPLOYMENT_METHODOLOGY_FIELDS}


def test_complete_strategy_methodology_is_required():
    with pytest.raises(ValueError, match="complete deployment methodology"):
        _validate_deployment_methodology(None)


def test_missing_execution_or_allocation_field_blocks_freeze():
    methodology = _complete_methodology()
    methodology.pop("simultaneous_signal_handling")
    with pytest.raises(ValueError, match="simultaneous_signal_handling"):
        _validate_deployment_methodology(methodology)


def test_complete_methodology_is_preserved_exactly_for_hashing():
    methodology = _complete_methodology()
    assert _validate_deployment_methodology(methodology) == methodology
