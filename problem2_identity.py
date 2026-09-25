"""Versioned identity helpers used after the frozen Problem-2 Stage 1 run."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from candidate_manager_problem2 import Problem2CandidateError, json_sha256


def _normalize_json_mapping_keys(value: Any) -> Any:
    """Return the value represented by an official JSON round trip.

    Integer mapping keys are sorted numerically by ``json.dumps`` while the
    same keys are sorted lexicographically after reading the JSON file.  This
    normalisation makes a plan hash independent of that in-memory/disk state.
    """

    if isinstance(value, Mapping):
        normalized: Dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key in normalized:
                raise Problem2CandidateError(
                    "JSON mapping keys collide after string normalization")
            normalized[text_key] = _normalize_json_mapping_keys(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_mapping_keys(item) for item in value]
    return value


def plan_json_sha256(plan: Mapping[str, Any]) -> str:
    """Hash an official plan with stable in-memory/on-disk semantics."""

    if not isinstance(plan, Mapping):
        raise Problem2CandidateError("official plan must be a mapping")
    return json_sha256(_normalize_json_mapping_keys(plan))
