"""Configurable engineering-baseline zone impact scoring."""

from collections.abc import Mapping
import math


DEFAULT_BASELINE_WEIGHTS: dict[str, float] = {
    "disaster_exposure": 0.40,
    "population_exposure": 0.25,
    "building_exposure": 0.15,
    "infrastructure_exposure": 0.10,
    "vulnerability": 0.10,
}

DEFAULT_SEVERITY_THRESHOLDS: dict[str, float] = {
    "moderate_min": 0.25,
    "high_min": 0.50,
    "critical_min": 0.75,
}


def normalize_by_maximum(values: list[float]) -> list[float]:
    """Scale non-negative finite feature values relative to the observed maximum."""
    if not values:
        raise ValueError("At least one value is required for normalization.")
    normalized_values: list[float] = []
    for value in values:
        if not math.isfinite(value) or value < 0:
            raise ValueError("Normalization inputs must be finite and non-negative.")
    maximum = max(values)
    if maximum == 0:
        return [0.0] * len(values)
    for value in values:
        normalized_values.append(min(1.0, value / maximum))
    return normalized_values


def calculate_baseline_score(
    features: Mapping[str, float | None],
    *,
    weights: Mapping[str, float] = DEFAULT_BASELINE_WEIGHTS,
) -> float:
    """Calculate a weighted score, renormalizing weights for available features."""
    active: list[tuple[float, float]] = []
    for name, weight in weights.items():
        if not math.isfinite(weight) or weight < 0:
            raise ValueError(f"Weight for {name} must be finite and non-negative.")
        value = features.get(name)
        if value is None:
            continue
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"Feature {name} must be a finite value from 0 to 1.")
        if weight > 0:
            active.append((value, weight))
    total_weight = sum(weight for _, weight in active)
    if total_weight <= 0:
        raise ValueError("At least one available feature must have positive weight.")
    score = sum(value * weight for value, weight in active) / total_weight
    return min(1.0, max(0.0, score))


def classify_severity(
    score: float,
    *,
    thresholds: Mapping[str, float] = DEFAULT_SEVERITY_THRESHOLDS,
) -> str:
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("Impact score must be a finite value from 0 to 1.")
    moderate_min = thresholds["moderate_min"]
    high_min = thresholds["high_min"]
    critical_min = thresholds["critical_min"]
    if not 0 < moderate_min < high_min < critical_min <= 1:
        raise ValueError("Severity thresholds must satisfy 0 < moderate < high < critical <= 1.")
    if score >= critical_min:
        return "Critical"
    if score >= high_min:
        return "High"
    if score >= moderate_min:
        return "Moderate"
    return "Low"
