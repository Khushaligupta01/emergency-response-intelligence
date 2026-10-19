import pytest

from app.services.zone_severity import (
    DEFAULT_BASELINE_WEIGHTS,
    DEFAULT_SEVERITY_THRESHOLDS,
    calculate_baseline_score,
    classify_severity,
    normalize_by_maximum,
)


def test_normalizes_features_against_observed_maximum():
    assert normalize_by_maximum([0, 5, 10]) == [0.0, 0.5, 1.0]


def test_all_zero_feature_normalizes_to_zero():
    assert normalize_by_maximum([0, 0]) == [0.0, 0.0]


def test_baseline_score_renormalizes_weights_for_unavailable_features():
    score = calculate_baseline_score(
        {
            "disaster_exposure": 1.0,
            "population_exposure": 0.5,
            "building_exposure": 0.0,
            "infrastructure_exposure": None,
            "vulnerability": None,
        }
    )
    assert score == pytest.approx((0.4 + 0.125) / 0.8)
    assert sum(DEFAULT_BASELINE_WEIGHTS.values()) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, "Low"),
        (0.249999, "Low"),
        (0.25, "Moderate"),
        (0.5, "High"),
        (0.75, "Critical"),
        (1.0, "Critical"),
    ],
)
def test_classifies_scores_at_documented_thresholds(score, expected):
    assert classify_severity(score) == expected


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan")])
def test_rejects_score_outside_unit_interval(score):
    with pytest.raises(ValueError):
        classify_severity(score)


def test_rejects_invalid_severity_thresholds():
    with pytest.raises(ValueError, match="thresholds"):
        classify_severity(
            0.5,
            thresholds={
                "moderate_min": 0.6,
                "high_min": 0.5,
                "critical_min": 0.75,
            },
        )


def test_documents_expected_default_severity_thresholds():
    assert DEFAULT_SEVERITY_THRESHOLDS == {
        "moderate_min": 0.25,
        "high_min": 0.50,
        "critical_min": 0.75,
    }
