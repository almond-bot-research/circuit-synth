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


# ------------------------------------------------------------ length tuning

from circuit_synth.pcb.scripts.routing_geometry import (  # noqa: E402
    locate,
    meander,
    meander_capacity,
    net_length,
    skew_bumps,
    splice,
)


def _local(points, a, b):
    """Points as (along a->b, to the left of it)."""
    (ux, uy), (nx, ny) = heading(a, b), left_normal(a, b)
    return [((p[0] - a[0]) * ux + (p[1] - a[1]) * uy, (p[0] - a[0]) * nx + (p[1] - a[1]) * ny) for p in points]


def _legs(points, a, b):
    """Where along a->b the path has a segment square to it (a meander leg)."""
    local = _local(points, a, b)
    return sorted(p[0] for p, q in zip(local, local[1:]) if abs(p[0] - q[0]) < 1e-9 and abs(p[1] - q[1]) > 1e-9)


def _between(a, b, c, d):
    """Smallest distance between segments a-b and c-d (they do not cross)."""

    def to_segment(p, s, e):
        length = math.dist(s, e)
        t = max(0.0, min(length, ((p[0] - s[0]) * (e[0] - s[0]) + (p[1] - s[1]) * (e[1] - s[1])) / length))
        return math.dist(p, (s[0] + (e[0] - s[0]) * t / length, s[1] + (e[1] - s[1]) * t / length))

    return min(to_segment(a, c, d), to_segment(b, c, d), to_segment(c, a, b), to_segment(d, a, b))


def _side_by_side(a, b, c, d):
    """True when segments a-b and c-d are parallel and overlap along their direction."""
    (ux, uy), (vx, vy) = heading(a, b), heading(c, d)
    if abs(ux * vy - uy * vx) > 1e-9:
        return False
    start, end = sorted(((p[0] - a[0]) * ux + (p[1] - a[1]) * uy) for p in (c, d))
    return min(end, math.dist(a, b)) - max(start, 0.0) > 1e-9


MEANDER_EXTRAS = [0.004, 0.05, 0.3, 0.7, 1.0, 2.5, 6.0, 11.3]


@pytest.mark.parametrize("side", ["left", "right", "both"])
@pytest.mark.parametrize("extra", MEANDER_EXTRAS)
def test_meander_adds_the_length_asked_for(extra, side):
    a, b = (10.0, 20.0), (30.0, 20.0)
    amplitude, pitch = 1.2, 0.44
    points, count, height = meander(a, b, extra, amplitude, pitch, side)
    # the requirement is 0.01mm; the geometry is exact
    assert path_length(points) == pytest.approx(20.0 + extra, abs=1e-9)
    assert points[0] == a and points[-1] == b
    assert all(is_45(p, q) for p, q in zip(points, points[1:]))
    assert count >= 1 and 0 < height <= amplitude + 1e-12
    # it goes as far out as it says, on the side asked for, and never further than allowed
    across = [v for _, v in _local(points, a, b)]
    assert max(abs(v) for v in across) == pytest.approx(height)
    if side == "left":
        assert min(across) > -1e-9
    if side == "right":
        assert max(across) < 1e-9
    # and stays on the segment: the ends keep a straight piece
    assert min(u for u, v in _local(points, a, b) if abs(v) > 1e-9) >= pitch / 2
    assert max(u for u, v in _local(points, a, b) if abs(v) > 1e-9) <= 20.0 - pitch / 2


@pytest.mark.parametrize("side", ["left", "right", "both"])
@pytest.mark.parametrize("extra", MEANDER_EXTRAS)
def test_meander_legs_are_a_leg_pitch_apart(extra, side):
    a, b = (10.0, 20.0), (30.0, 20.0)
    pitch = 0.44   # 0.11mm trace: 0.11 + 3 x 0.11
    points, count, height = meander(a, b, extra, 1.2, pitch, side)
    legs = _legs(points, a, b)
    # low meanders are trapezoids without a leg square to the route; the others have two per meander
    # (one-sided) or one more than there are meanders (alternating sides)
    assert len(legs) in (0, 2 * count if side != "both" else count + 1)
    assert all(q - p == pytest.approx(pitch) for p, q in zip(legs, legs[1:]))
    # and no two stretches of it run side by side any closer than that: the copper of a 0.11mm
    # trace keeps 0.33mm (3 x its width) of clear space
    segments = list(zip(points, points[1:]))
    beside = [(p, q, r, s) for i, (p, q) in enumerate(segments) for r, s in segments[i + 2:] if _side_by_side(p, q, r, s)]
    assert len(beside) >= len(legs) - 1
    assert all(_between(p, q, r, s) >= pitch - 1e-9 for p, q, r, s in beside)


def test_meander_uses_the_fewest_meanders_that_reach_the_length():
    a, b = (0.0, 0.0), (20.0, 0.0)
    assert meander(a, b, 1.0, 1.2, 0.44)[1] == 1
    points, count, height = meander(a, b, 6.0, 1.2, 0.44)
    assert count == 3 and height == pytest.approx(1.151, abs=1e-3)   # 2 would need 1.65mm: over the 1.2mm limit
    assert meander(a, b, 6.0, 1.2, 0.44, "both")[1] == 3
    assert meander(a, b, 6.0, 3.2, 0.44)[1] == 1                    # with the room, one tall meander


def test_meander_on_a_diagonal_segment_stays_on_45_degree_angles():
    a, b = (5.0, 5.0), (15.0, 15.0)
    points, count, height = meander(a, b, 3.0, 0.8, 0.5, "right")
    assert path_length(points) == pytest.approx(math.dist(a, b) + 3.0, abs=1e-9)
    assert all(is_45(p, q) for p, q in zip(points, points[1:]))
    assert max(abs(v) for _, v in _local(points, a, b)) == pytest.approx(height)
    assert max(v for _, v in _local(points, a, b)) < 1e-9          # right of travel only


def test_meander_of_nothing_is_the_segment_itself():
    assert meander((0, 0), (5, 0), 0.0, 1.0, 0.4) == ([(0, 0), (5, 0)], 0, 0.0)


def test_meander_that_cannot_fit_says_what_would():
    a, b = (0.0, 0.0), (5.0, 0.0)
    # 5mm of segment at a 0.44mm leg pitch: 5 one-sided meanders of 0.5mm
    assert meander_capacity(5.0, 0.5, 0.44) == pytest.approx(5 * (2 * 0.5 - 4 * (2 - math.sqrt(2)) * 0.44 / (2 + math.sqrt(2))))
    assert meander_capacity(5.0, 0.5, 0.44, "both") > meander_capacity(5.0, 0.5, 0.44)   # twice the legs
    with pytest.raises(ValueError, match=r"holds at most 5 meander\(s\) of 0.500mm: \+3.490mm"):
        meander(a, b, 4.0, 0.5, 0.44)
    assert path_length(meander(a, b, 3.49, 0.5, 0.44)[0]) == pytest.approx(8.49, abs=1e-9)
    with pytest.raises(ValueError, match="too short for a meander"):
        meander((0, 0), (0.9, 0), 0.2, 0.5, 0.44)
    assert meander_capacity(0.9, 0.5, 0.44) == 0
    with pytest.raises(ValueError, match="'left', 'right' or 'both'"):
        meander(a, b, 1.0, 0.5, 0.44, "up")


@pytest.mark.parametrize("extra", [0.1, 1.7, 4.2])
@pytest.mark.parametrize("side", ["left", "both"])
def test_pair_meanders_together_and_keeps_its_gap(extra, side):
    # 0.115mm traces 0.15mm apart; the legs' facing traces 3 x the gap apart
    width, gap = 0.115, 0.15
    pitch = width + gap
    leg_pitch = pitch + width + 3 * gap
    a, b = (10.0, 20.0), (30.0, 20.0)
    centre, count, height = meander(a, b, extra, 1.5, leg_pitch, side)
    p, n = pair_tracks(centre, pitch, 1)
    # both traces gain exactly what the centre-line gained: the meander adds no skew
    assert path_length(p) == pytest.approx(20.0 + extra, abs=1e-9)
    assert path_length(n) == pytest.approx(20.0 + extra, abs=1e-9)
    # the gap holds along every segment, corners included
    for (pa, pb), (na, nb) in zip(zip(p, p[1:]), zip(n, n[1:])):
        assert _seg_distance(pa, pb, na) == pytest.approx(pitch)
        assert _seg_distance(pa, pb, nb) == pytest.approx(pitch)
    assert min(_between(pa, pb, na, nb) for pa, pb in zip(p, p[1:]) for na, nb in zip(n, n[1:])) == pytest.approx(pitch)
    # legs of the two traces alternate: a pair's own pitch inside a leg, then no less than
    # width + 3 x gap (centre to centre) to the next leg's nearest trace: 3 x gap of clear space
    legs = sorted(_legs(p, a, b) + _legs(n, a, b))
    spacings = [q - r for r, q in zip(legs, legs[1:])]
    assert all(s == pytest.approx(pitch) or s >= width + 3 * gap - 1e-9 for s in spacings)
    if legs:
        assert min(s for s in spacings if s > pitch + 1e-9) == pytest.approx(width + 3 * gap)
    # the outer trace goes pitch / 2 beyond the centre-line's amplitude
    assert max(abs(v) for _, v in _local(p + n, a, b)) == pytest.approx(height + pitch / 2)


@pytest.mark.parametrize("extra", [0.01, 0.124, 0.2, 0.53, 1.0])
def test_skew_bumps_add_the_length_within_their_amplitude(extra):
    a, b = (10.0, 5.0), (25.0, 5.0)
    amplitude, top = 0.15, 0.345   # the pair's gap, 3 x its 0.115mm trace
    points, count, height = skew_bumps(a, b, extra, amplitude, top, "left")
    assert path_length(points) == pytest.approx(15.0 + extra, abs=1e-9)
    assert points[0] == a and points[-1] == b
    assert all(is_45(p, q) for p, q in zip(points, points[1:]))
    # as few bumps as the amplitude limit allows, all the same height
    per_bump = 2 * (math.sqrt(2) - 1)
    assert count == math.ceil(extra / (per_bump * amplitude) - 1e-9)
    assert height == pytest.approx(extra / (per_bump * count)) and height <= amplitude + 1e-12
    local = _local(points, a, b)
    assert max(v for _, v in local) == pytest.approx(height)
    assert min(v for _, v in local) > -1e-9                       # only ever away from the other trace
    # each bump: up on a diagonal, `top` along, down on a diagonal; `top` of plain trace between two
    tops = [(p, q) for p, q in zip(local, local[1:]) if abs(p[1] - height) < 1e-9 and abs(q[1] - height) < 1e-9]
    assert len(tops) == count and all(q[0] - p[0] == pytest.approx(top) for p, q in tops)
    flats = [(p, q) for p, q in zip(local, local[1:]) if abs(p[1]) < 1e-9 and abs(q[1]) < 1e-9]
    assert len(flats) == count + 1 and all(q[0] - p[0] >= top - 1e-9 for p, q in flats[1:-1])


def test_skew_bumps_go_to_the_side_asked_for_and_must_fit():
    points, count, height = skew_bumps((0, 0), (0, 10), 0.2, 0.15, 0.3, "right")
    assert min(x for x, _ in points) == pytest.approx(-height)    # heading south, right is west
    assert max(x for x, _ in points) == pytest.approx(0)
    with pytest.raises(ValueError, match=r"5 bump\(s\) of 0.128mm need 6.380mm; the segment has 5.000mm"):
        skew_bumps((0, 0), (5, 0), 0.53, 0.15, 0.51, "left")
    assert skew_bumps((0, 0), (5, 0), 0.0, 0.15, 0.3, "left") == ([(0, 0), (5, 0)], 0, 0.0)


def test_locate_finds_the_segment_a_hop_runs_along():
    path = [(0, 0), (10, 0), (15, 5), (15, 12)]
    assert locate(path, (2, 0), (8, 0)) == (0, pytest.approx((2, 0)), pytest.approx((8, 0)))
    # clipped to what the path covers: here the hop reaches past the corner
    assert locate(path, (6, 0), (12, 0)) == (0, pytest.approx((6, 0)), pytest.approx((10, 0)))
    assert locate(path, (15, 6), (15, 11))[0] == 2
    with pytest.raises(ValueError, match="no segment"):
        locate(path, (2, 1), (8, 1))                               # parallel, but beside the path
    with pytest.raises(ValueError, match="no segment"):
        locate(path, (8, 0), (2, 0))                               # against the direction of travel


def test_splice_replaces_part_of_a_segment():
    path = [(0, 0), (10, 0), (15, 5)]
    detour = [(2, 0), (3, 1), (5, 1), (6, 0)]
    assert splice(path, 0, detour) == [(0, 0), (2, 0), (3, 1), (5, 1), (6, 0), (10, 0), (15, 5)]
    # a detour that starts or ends on the segment's own ends adds no zero-length hop
    assert splice(path, 0, [(0, 0), (1, 1), (9, 1), (10, 0)]) == [(0, 0), (1, 1), (9, 1), (10, 0), (15, 5)]


DEPTHS = {"F.Cu": 0.0175, "In1.Cu": 0.162, "In2.Cu": 0.3772, "B.Cu": 0.5118}


def test_net_length_is_track_plus_via_height_plus_pad_length():
    tracks = [("F.Cu", (0, 0), (10, 0), 10.0), ("In2.Cu", (10, 0), (30, 0), 20.0), ("F.Cu", (30, 0), (35, 0), 5.0)]
    vias = [((10, 0), 0.3), ((30, 0), 0.3)]
    pads = [((-0.2, -0.2, 0.2, 0.2), "F.Cu", 1.25), ((34.8, -0.2, 35.2, 0.2), "F.Cu", 0.0)]
    got = net_length(tracks, vias, pads, DEPTHS)
    assert got["layers"] == {"F.Cu": 15.0, "In2.Cu": 20.0} and got["vias"] == 2
    # each via counts the height between the layers it joins there, not the board's thickness
    assert got["via_length"] == pytest.approx(2 * (0.3772 - 0.0175))
    assert got["pad_length"] == 1.25
    assert got["total"] == pytest.approx(35.0 + 2 * 0.3597 + 1.25)


def test_net_length_without_a_stack_up_counts_no_via_height():
    tracks = [("F.Cu", (0, 0), (10, 0), 10.0), ("B.Cu", (10, 0), (30, 0), 20.0)]
    got = net_length(tracks, [((10, 0), 0.3)])
    assert (got["vias"], got["via_length"], got["pad_length"], got["total"]) == (1, 0.0, 0.0, 30.0)


def test_via_height_follows_what_the_via_joins():
    here = ((10, 0), 0.3)
    # a track that ends inside the via's pad counts as joined, one that ends elsewhere does not
    near = [("F.Cu", (0, 0), (9.8, 0), 9.8), ("B.Cu", (10.1, 0.1), (20, 0), 9.9), ("In1.Cu", (10.5, 0), (20, 0), 9.5)]
    assert net_length(near, [here], (), DEPTHS)["via_length"] == pytest.approx(0.5118 - 0.0175)
    # three layers at one via: the outermost two
    tee = [("F.Cu", (0, 0), (10, 0), 10.0), ("In1.Cu", (10, 0), (20, 0), 10.0), ("In2.Cu", (10, 0), (10, 9), 9.0)]
    assert net_length(tee, [here], (), DEPTHS)["via_length"] == pytest.approx(0.3772 - 0.0175)
    # tracks on one layer only (a stitch to a plane): no length
    assert net_length([("F.Cu", (0, 0), (10, 0), 10.0)], [here], (), DEPTHS)["via_length"] == 0
    # a via in an SMD pad joins the pad's layer; a through-hole pad (no layer) says nothing
    inner = [("In2.Cu", (10, 0), (20, 0), 10.0)]
    assert net_length(inner, [here], [((9.5, -0.5, 10.5, 0.5), "F.Cu", 0.0)], DEPTHS)["via_length"] == pytest.approx(0.3597)
    assert net_length(inner, [here], [((9.5, -0.5, 10.5, 0.5), None, 0.0)], DEPTHS)["via_length"] == 0
    assert net_length(inner, [here], [((11.5, -0.5, 12.5, 0.5), "F.Cu", 0.0)], DEPTHS)["via_length"] == 0
