# ABOUTME: Unit tests for temporal-anchor parsing/validation (no network).
# ABOUTME: Pins at-most-one anchor, ISO parsing, and 0..1 confidence.

from datetime import datetime

import pytest

from openbrain.brain.extraction.temporal import (
    TemporalValidationError,
    parse_temporal_anchor,
)


def test_parse_temporal_point_anchor():
    out = parse_temporal_anchor(
        {
            "occurred_at": "2025-05-01T12:00:00Z",
            "occurred_window": None,
            "confidence": 0.8,
        }
    )
    assert isinstance(out["occurred_at"], datetime)
    assert out["occurred_window"] is None
    assert out["confidence"] == 0.8


def test_parse_temporal_window_anchor():
    out = parse_temporal_anchor(
        {
            "occurred_at": None,
            "occurred_window": {
                "lower": "2025-05-01T00:00:00Z",
                "upper": "2025-05-07T00:00:00Z",
            },
            "confidence": 0.5,
        }
    )
    assert out["occurred_at"] is None
    assert isinstance(out["occurred_window"]["lower"], datetime)
    assert isinstance(out["occurred_window"]["upper"], datetime)


def test_parse_temporal_no_anchor():
    out = parse_temporal_anchor(
        {"occurred_at": None, "occurred_window": None, "confidence": 0}
    )
    assert out["occurred_at"] is None
    assert out["occurred_window"] is None
    assert out["confidence"] == 0


def test_parse_temporal_both_set_raises():
    with pytest.raises(TemporalValidationError, match="both"):
        parse_temporal_anchor(
            {
                "occurred_at": "2025-05-01T12:00:00Z",
                "occurred_window": {
                    "lower": "2025-05-01T00:00:00Z",
                    "upper": "2025-05-07T00:00:00Z",
                },
                "confidence": 0.5,
            }
        )


def test_parse_temporal_invalid_iso_raises():
    with pytest.raises(TemporalValidationError):
        parse_temporal_anchor(
            {"occurred_at": "not a date", "occurred_window": None, "confidence": 0.5}
        )


def test_parse_temporal_out_of_range_confidence_raises():
    with pytest.raises(TemporalValidationError):
        parse_temporal_anchor(
            {"occurred_at": None, "occurred_window": None, "confidence": 2}
        )
