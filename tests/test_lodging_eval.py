"""Evaluation plumbing checks, independent of real account/data and production imports."""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("lodging_eval", Path(__file__).parents[1] / "scripts/lodging_eval.py")
assert spec and spec.loader
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def response(label="query", confidence=1.0):
    return {"answers": {"intent": {"type": "choice", "choice": label, "confidence": confidence,
        "probabilities": {k: float(k == label) for k in evaluation.CRITERIA}}}}


def test_invalid_model_response_never_counts_as_valid():
    for body in (response("unknown"), response(confidence=float("nan")), response(confidence=True), {}):
        with pytest.raises((ValueError, KeyError)):
            evaluation.validate_response(body)
    bad = response()
    bad["answers"]["intent"]["probabilities"]["create"] = .5
    with pytest.raises(ValueError):
        evaluation.validate_response(bad)


def test_low_confidence_errors_not_hidden_in_accuracy():
    rows = [{"id": "a", "expected": "quote", "predicted": "create", "confidence": .2, "latency_ms": 10},
            {"id": "b", "expected": "query", "error": "timeout"}]
    result = evaluation.metrics(rows)
    assert result["accuracy_all"] == 0
    assert result["errors"] == 1
    assert result["write_intent_false_positives"] == ["a"]
    assert result["accepted_false_write"] == []
    rule = evaluation.metrics([{"id": "r", "expected": "query", "predicted": "query", "latency_ms": 0.1}])
    assert rule["confidence_available"] is False
    assert rule["accepted_at_0_8"] == 0


def test_api_state_must_not_include_expected_label():
    # Request builder receives only message and context, never expected answer/split.
    case = {"message": "测试", "context": "无", "expected": "other"}
    state = evaluation.make_state(case)
    assert "expected" not in state and "other" not in str(state)
