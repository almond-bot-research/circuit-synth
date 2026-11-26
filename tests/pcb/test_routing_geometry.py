"""Geometry helpers behind `cs pcb route` (no pcbnew needed)."""

import math

import pytest

from circuit_synth.pcb.scripts.routing_geometry import dogleg, expand_path, is_45, path_length


def test_aligned_points_are_one_segment():
    assert dogleg((0, 0), (5, 0)) == [(5, 0)]
    assert dogleg((0, 0), (0, -3)) == [(0, -3)]
    assert dogleg((0, 0), (2, -2)) == [(2, -2)]


def test_straight_then_diagonal():
    assert dogleg((0, 0), (5, 2), "sd") == [(3, 0), (5, 2)]
    assert dogleg((0, 0), (-2, 5), "sd") == [(0, 3), (-2, 5)]


def test_diagonal_then_straight():
    assert dogleg((0, 0), (5, 2), "ds") == [(2, 2), (5, 2)]
    assert dogleg((0, 0), (-2, -5), "ds") == [(-2, -2), (-2, -5)]


def test_bad_bend_mode():
    with pytest.raises(ValueError):
        dogleg((0, 0), (1, 2), "zz")


def test_expand_path_is_all_45_and_merges_collinear():
    pts = expand_path([(0, 0), (2, 0), (5, 0), (9, 3), (9, 3)], ["sd", "sd", "ds", "sd"])
    assert pts == [(0, 0), (5, 0), (8, 3), (9, 3)]
    assert all(is_45(a, b) for a, b in zip(pts, pts[1:]))


def test_expand_path_needs_one_bend_per_hop():
    with pytest.raises(ValueError):
        expand_path([(0, 0), (1, 1)], [])


def test_path_length():
    assert math.isclose(path_length([(0, 0), (3, 0), (4, 1)]), 3 + math.sqrt(2))


# ------------------------------------------------------- differential pairs

from circuit_synth.pcb.scripts.routing_geometry import (  # noqa: E402
    heading,
    left_normal,
    offset_path,
    pair_tracks,
    pair_via_setback,
    pair_vias,
    side_of,
    via_span,
)

T22 = math.tan(math.radians(22.5))  # what a 45 degree corner costs per unit of offset


def _approx(points):
    return [pytest.approx(p, abs=1e-9) for p in points]


def _seg_distance(a, b, p):
    """Perpendicular distance from p to the infinite line a-b."""
    ux, uy = heading(a, b)
    return abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux)


def test_left_is_left_on_screen():
    # y grows downward on a board: heading east, the left hand points north (-y)
    assert left_normal((0, 0), (5, 0)) == pytest.approx((0, -1))
    assert left_normal((0, 0), (0, 5)) == pytest.approx((1, 0))   # heading south: east
    assert heading((1, 1), (4, 5)) == pytest.approx((0.6, 0.8))
    with pytest.raises(ValueError):
        heading((1, 1), (1, 1))


def test_offset_straight_path():
    assert offset_path([(0, 0), (10, 0)], 0.17) == _approx([(0, -0.17), (10, -0.17)])
    assert offset_path([(0, 0), (10, 0)], -0.17) == _approx([(0, 0.17), (10, 0.17)])


def test_offset_around_a_45_degree_corner():
    # east, then south-east: the left-hand trace is on the outside of the bend
    centre = [(0, 0), (10, 0), (15, 5)]
    d = 0.17
    outer, inner = offset_path(centre, d), offset_path(centre, -d)
    # the corner moves along the bisector: d across the first segment and
    # d * tan(22.5) along it, forwards on the outside and back on the inside
    assert outer[1] == pytest.approx((10 + d * T22, -d))
    assert inner[1] == pytest.approx((10 - d * T22, d))
    # ends stay square to the centre-line
    assert outer[0] == pytest.approx((0, -d)) and inner[0] == pytest.approx((0, d))
    assert outer[2] == pytest.approx((15 + d / math.sqrt(2), 5 - d / math.sqrt(2)))
    # both traces are still on 45 degree angles and exactly d from the centre-line
    for trace in (outer, inner):
        assert all(is_45(a, b) for a, b in zip(trace, trace[1:]))
        for (a, b), (ta, tb) in zip(zip(centre, centre[1:]), zip(trace, trace[1:])):
            assert _seg_distance(a, b, ta) == pytest.approx(d)
            assert _seg_distance(a, b, tb) == pytest.approx(d)
    # lengths: each of the two segments at the corner gains/loses d * tan(22.5)
    assert path_length(outer) == pytest.approx(path_length(centre) + 2 * d * T22)
    assert path_length(inner) == pytest.approx(path_length(centre) - 2 * d * T22)


def test_offset_around_a_90_degree_corner():
    assert offset_path([(0, 0), (5, 0), (5, 5)], -0.2) == _approx([(0, 0.2), (4.8, 0.2), (4.8, 5)])
    assert offset_path([(0, 0), (5, 0), (5, 5)], 0.2) == _approx([(0, -0.2), (5.2, -0.2), (5.2, 5)])


def test_offset_rejects_paths_it_cannot_follow():
    with pytest.raises(ValueError, match="doubles back"):
        offset_path([(0, 0), (5, 0), (0, 0)], 0.1)
    # a 0.05mm jog between two 45 degree corners cannot hold a 0.3mm offset on the inside
    with pytest.raises(ValueError, match="too short"):
        offset_path([(0, 0), (2, 0), (2.05, 0.05), (2.05, 3)], -0.3)
    with pytest.raises(ValueError):
        offset_path([(0, 0)], 0.1)


def test_pair_gap_is_constant_and_skew_is_what_the_corner_costs():
    centre = [(0, 0), (10, 0), (15, 5)]
    pitch = 0.19 + 0.15
    p, n = pair_tracks(centre, pitch, 1)   # P on the left: outside of this bend
    for (pa, pb), (na, nb) in zip(zip(p, p[1:]), zip(n, n[1:])):
        assert _seg_distance(pa, pb, na) == pytest.approx(pitch)
        assert _seg_distance(pa, pb, nb) == pytest.approx(pitch)
    assert path_length(p) - path_length(n) == pytest.approx(2 * pitch * T22)   # 0.2817mm
    # the other way round, N is the longer one by the same amount
    p, n = pair_tracks(centre, pitch, -1)
    assert path_length(p) - path_length(n) == pytest.approx(-2 * pitch * T22)


def test_pair_skew_cancels_over_opposite_bends_and_adds_over_equal_ones():
    pitch = 0.3
    jog = [(0, 0), (10, 0), (15, 5), (25, 5)]            # right then left
    p, n = pair_tracks(jog, pitch, 1)
    assert path_length(p) == pytest.approx(path_length(n))
    assert path_length(p) == pytest.approx(path_length(jog))
    turn = [(0, 0), (10, 0), (15, 5), (15, 15)]          # right twice: a 90 degree turn
    p, n = pair_tracks(turn, pitch, 1)
    assert path_length(p) - path_length(n) == pytest.approx(4 * pitch * T22)
    assert (path_length(p) + path_length(n)) / 2 == pytest.approx(path_length(turn))


def test_pair_fans_in_from_pads_symmetrically():
    # pads 0.5mm apart either side of the centre-line, pair 0.34mm apart
    pads = ((0.0, -0.25), (0.0, 0.25))
    p, n = pair_tracks([(1.0, 0), (6.0, 0)], 0.34, 1, start=pads)
    # "sd": straight out of each pad, then a 45 degree jog onto its trace
    assert p == _approx([(0, -0.25), (0.92, -0.25), (1.0, -0.17), (6.0, -0.17)])
    assert n == _approx([(0, 0.25), (0.92, 0.25), (1.0, 0.17), (6.0, 0.17)])
    assert path_length(p) == pytest.approx(path_length(n))
    # "ds": jog first, then straight (collinear with the coupled run, so one segment)
    p, n = pair_tracks([(1.0, 0), (6.0, 0)], 0.34, 1, start=pads, start_bend="ds")
    assert p == _approx([(0, -0.25), (0.08, -0.17), (6.0, -0.17)])
    assert n == _approx([(0, 0.25), (0.08, 0.17), (6.0, 0.17)])


def test_pair_ends_on_its_pads_and_reports_the_fan_out_skew():
    # end pads staggered along the route: the far pad's trace is longer by the stagger
    centre = [(0, 0), (5, 0)]
    p, n = pair_tracks(centre, 0.3, 1, end=((6.0, -0.15), (6.4, 0.15)))
    assert p[0] == pytest.approx((0, -0.15)) and n[0] == pytest.approx((0, 0.15))
    assert p[-1] == pytest.approx((6.0, -0.15)) and n[-1] == pytest.approx((6.4, 0.15))
    assert all(is_45(a, b) for trace in (p, n) for a, b in zip(trace, trace[1:]))
    assert path_length(n) - path_length(p) == pytest.approx(0.4)


def test_pair_side_must_be_plus_or_minus_one():
    with pytest.raises(ValueError):
        pair_tracks([(0, 0), (5, 0)], 0.3, 0)


def test_side_of():
    assert side_of((0, -1), (0, 1), (0, 0), (5, 0)) == 1     # north of an eastward route: left
    assert side_of((0, 1), (0, -1), (0, 0), (5, 0)) == -1
    assert side_of((1, 0), (3, 0), (0, 0), (5, 0)) == 0      # in line with the route
    assert side_of((-1, 0), (1, 0), (0, 5), (0, 0)) == 1     # heading north, west is left


def test_pair_vias_sit_across_the_route_and_the_fan_out_is_a_45():
    pitch, via_pitch = 0.34, 0.9
    setback = pair_via_setback(pitch, via_pitch)
    assert setback == pytest.approx(0.28)
    at = (10.0, 0.0)
    p_via, n_via = pair_vias(at, (1, 0), via_pitch, 1)
    assert p_via == pytest.approx((10, -0.45)) and n_via == pytest.approx((10, 0.45))
    assert pair_vias(at, (1, 0), via_pitch, -1) == (n_via, p_via)
    # the coupled run stops `setback` short of the vias; each trace then
    # reaches its via on one exact diagonal, the two the same length
    p, n = pair_tracks([(0, 0), (at[0] - setback, 0)], pitch, 1, end=(p_via, n_via))
    assert p == _approx([(0, -0.17), (9.72, -0.17), (10, -0.45)])
    assert n == _approx([(0, 0.17), (9.72, 0.17), (10, 0.45)])
    assert path_length(p) == pytest.approx(path_length(n))
    # heading north-west, the vias sit on the other diagonal
    k = math.sqrt(0.5)
    p_via, n_via = pair_vias((0, 0), (-k, -k), 1.0, 1)
    assert p_via == pytest.approx((-0.5 * k, 0.5 * k)) and n_via == pytest.approx((0.5 * k, -0.5 * k))


def test_through_via_spans_the_board_whatever_it_joins():
    layers = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
    assert via_span("through", layers, "F.Cu", "In3.Cu") == ("F.Cu", "B.Cu")
    assert via_span("through", layers, "In2.Cu", "In4.Cu") == ("F.Cu", "B.Cu")
    with pytest.raises(ValueError, match="through vias only"):
        via_span("blind", layers, "F.Cu", "In1.Cu")
    with pytest.raises(ValueError, match="In7.Cu.*valid: F.Cu, In1.Cu, In2.Cu, In3.Cu, In4.Cu, B.Cu"):
        via_span("through", layers, "F.Cu", "In7.Cu")
