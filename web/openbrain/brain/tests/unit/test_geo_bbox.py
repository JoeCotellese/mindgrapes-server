# ABOUTME: Unit tests for the pure bounding-box helpers (#43).
# ABOUTME: Covers antimeridian detection so the box-crossing-180 split is correct.
from openbrain.brain.services.geo import crosses_antimeridian


def test_normal_box_does_not_cross():
    assert crosses_antimeridian(-10.0, 10.0) is False


def test_box_crossing_antimeridian():
    # min_lng east of max_lng means the viewport wraps past +/-180.
    assert crosses_antimeridian(170.0, -170.0) is True


def test_degenerate_equal_edges_does_not_cross():
    assert crosses_antimeridian(5.0, 5.0) is False
