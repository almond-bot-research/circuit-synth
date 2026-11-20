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
