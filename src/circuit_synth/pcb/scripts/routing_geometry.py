"""Pure geometry helpers for hand-written routing files (`cs pcb route`).

Stdlib only, no pcbnew: unit-testable from the main environment and
importable by the pcbnew scripts.
"""

from __future__ import annotations

import math

Point = tuple[float, float]

_EPS = 1e-6


def is_45(a: Point, b: Point) -> bool:
    """True if a->b is horizontal, vertical or an exact 45 degree diagonal."""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return dx < _EPS or dy < _EPS or abs(dx - dy) < _EPS


def dogleg(a: Point, b: Point, bend: str = "sd") -> list[Point]:
    """Join a to b with 45 degree routing; returns the points after `a`.

    Aligned points give one segment. Otherwise two: `bend="sd"` runs the
    straight (horizontal/vertical) part first and finishes on the diagonal,
    `bend="ds"` leaves on the diagonal and finishes straight.
    """
    if bend not in ("sd", "ds"):
        raise ValueError(f"bend must be 'sd' or 'ds', not {bend!r}")
    dx, dy = b[0] - a[0], b[1] - a[1]
    if is_45(a, b):
        return [b]
    diag = min(abs(dx), abs(dy))
    sx, sy = math.copysign(1, dx), math.copysign(1, dy)
    if bend == "ds":
        mid = (a[0] + sx * diag, a[1] + sy * diag)
    elif abs(dx) > abs(dy):
        mid = (b[0] - sx * diag, a[1])
    else:
        mid = (a[0], b[1] - sy * diag)
    return [(round(mid[0], 6), round(mid[1], 6)), b]


def expand_path(points: list[Point], bends: list[str]) -> list[Point]:
    """Expand waypoints into a 45-degree polyline.

    `bends[i]` is the dogleg mode for the hop from `points[i]` to
    `points[i + 1]`. Zero-length hops are dropped and collinear points
    merged.
    """
    if len(bends) != len(points) - 1:
        raise ValueError("need one bend mode per hop")
    out: list[Point] = [points[0]]
    for nxt, bend in zip(points[1:], bends):
        for p in dogleg(out[-1], nxt, bend):
            if math.dist(p, out[-1]) < _EPS:
                continue
            if len(out) >= 2 and _collinear(out[-2], out[-1], p):
                out[-1] = p
            else:
                out.append(p)
    return out


def _collinear(a: Point, b: Point, c: Point) -> bool:
    cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
    dot = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
    return abs(cross) < _EPS and dot > 0


def path_length(points: list[Point]) -> float:
    """Total polyline length."""
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))
