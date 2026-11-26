"""Pure geometry helpers for hand-written routing files (`cs pcb route`).

Stdlib only, no pcbnew: unit-testable from the main environment and
importable by the pcbnew scripts.

Single traces: `dogleg` / `expand_path` turn waypoints into a 45 degree
polyline. Differential pairs: `pair_tracks` builds both traces of a pair
from the same kind of centre-line (`offset_path` on either side, mitred so
the gap stays constant round every corner), `pair_vias` / `pair_via_setback`
place the two vias of a layer change. Board coordinates throughout: mm, x
to the right, y down, so "left of travel" is the left-hand side as seen on
screen.
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


# ------------------------------------------------------- differential pairs


def heading(a: Point, b: Point) -> Point:
    """Unit vector from a to b."""
    length = math.dist(a, b)
    if length < _EPS:
        raise ValueError(f"no direction between coincident points {a} and {b}")
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def left_normal(a: Point, b: Point) -> Point:
    """Unit normal on the left of travel a->b as seen on the board (y down):
    heading east it points north (-y)."""
    ux, uy = heading(a, b)
    return (uy, -ux)


def offset_path(points: list[Point], offset: float) -> list[Point]:
    """The polyline parallel to `points`, `offset` mm to the left of travel
    (negative: to the right).

    Corners are mitred: each vertex moves to the intersection of the two
    shifted segments, so the spacing to the original is `offset` along
    every segment. The trace on the outside of a bend comes out longer and
    the one on the inside shorter, by `offset * tan(turn / 2)` per adjacent
    segment (0.414 x offset for a 45 degree bend).

    Raises ValueError when the path doubles back on itself or a segment is
    too short to hold its corners at this offset.
    """
    if len(points) < 2:
        raise ValueError("a path needs at least two points")
    normals = [left_normal(a, b) for a, b in zip(points, points[1:])]
    out: list[Point] = []
    for i, p in enumerate(points):
        n1 = normals[max(i - 1, 0)]
        n2 = normals[min(i, len(normals) - 1)]
        k = 1 + n1[0] * n2[0] + n1[1] * n2[1]
        if k < _EPS:
            raise ValueError(f"path doubles back on itself at {p}")
        out.append((p[0] + offset * (n1[0] + n2[0]) / k, p[1] + offset * (n1[1] + n2[1]) / k))
    for (a, b), (oa, ob) in zip(zip(points, points[1:]), zip(out, out[1:])):
        along = (ob[0] - oa[0]) * (b[0] - a[0]) + (ob[1] - oa[1]) * (b[1] - a[1])
        if along < _EPS * math.dist(a, b):
            raise ValueError(
                f"segment {a}->{b} ({math.dist(a, b):.3f}mm) is too short for its corners at offset {abs(offset):.3f}mm"
            )
    return out


def side_of(p: Point, n: Point, a: Point, b: Point) -> int:
    """Which side of travel a->b point `p` lies on relative to point `n`:
    +1 left, -1 right, 0 when the two are in line with the travel."""
    nx, ny = left_normal(a, b)
    across = (p[0] - n[0]) * nx + (p[1] - n[1]) * ny
    return 0 if abs(across) < _EPS else (1 if across > 0 else -1)


def pair_tracks(
    centre: list[Point],
    pitch: float,
    p_side: int,
    start: tuple[Point, Point] | None = None,
    start_bend: str = "sd",
    end: tuple[Point, Point] | None = None,
    end_bend: str = "sd",
) -> tuple[list[Point], list[Point]]:
    """Both traces of a differential pair on one layer, as (P, N) polylines.

    `centre` is the pair's centre-line, already a 45 degree polyline (see
    expand_path); the traces run `pitch / 2` either side of it (pitch =
    trace width + gap, centre to centre), P on the left of travel for
    `p_side=+1` and on the right for `-1`.

    `start` / `end` are the (P, N) points the traces begin and finish at
    when those are not on the coupled section itself: pad centres, or the
    centres of the pair's vias. Each is joined to its trace's end of the
    coupled section with a dogleg in the given bend mode, so where the
    centre-line starts is where the pair becomes coupled.
    """
    if p_side not in (1, -1):
        raise ValueError("p_side must be +1 (P left of travel) or -1 (P right)")
    tracks = []
    for side, k in ((p_side, 0), (-p_side, 1)):
        path = offset_path(centre, side * pitch / 2)
        if start is not None:
            path = _join(_join([start[k]], dogleg(start[k], path[0], start_bend)), path[1:])
        if end is not None:
            path = _join(path, dogleg(path[-1], end[k], end_bend))
        tracks.append(path)
    return tracks[0], tracks[1]


def _join(path: list[Point], more: list[Point]) -> list[Point]:
    """Append points, dropping zero-length hops and merging collinear ones."""
    out = list(path)
    for p in more:
        if math.dist(p, out[-1]) < _EPS:
            continue
        if len(out) >= 2 and _collinear(out[-2], out[-1], p):
            out[-1] = p
        else:
            out.append(p)
    return out


def pair_via_setback(pitch: float, via_pitch: float) -> float:
    """How far before (and after) its vias a pair stops being coupled: the
    traces leave the coupled section on 45 degree diagonals that end on the
    via centres, `via_pitch` apart."""
    return abs(via_pitch - pitch) / 2


def pair_vias(at: Point, along: Point, via_pitch: float, p_side: int) -> tuple[Point, Point]:
    """(P, N) via centres for a pair changing layer at centre-line point
    `at`: side by side across the direction of travel `along` (a unit
    vector), `via_pitch` apart, each on its own trace's side."""
    nx, ny = along[1], -along[0]
    half = p_side * via_pitch / 2
    return (at[0] + half * nx, at[1] + half * ny), (at[0] - half * nx, at[1] - half * ny)


def via_span(kind: str, layers: list[str], from_layer: str, to_layer: str) -> tuple[str, str]:
    """(top, bottom) copper layers a via of this kind occupies when it
    joins `from_layer` to `to_layer` on a board with these copper layers.

    Only through vias exist today (the fab's standard service): whatever
    two layers they join, they span the whole stack. A blind or buried
    kind would return a shorter span here.
    """
    for layer in (from_layer, to_layer):
        if layer not in layers:
            raise ValueError(f"no copper layer {layer!r}; valid: {', '.join(layers)}")
    if kind != "through":
        raise ValueError(f"via type {kind!r} is not supported (through vias only)")
    return layers[0], layers[-1]
