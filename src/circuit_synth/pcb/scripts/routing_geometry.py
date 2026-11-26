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


# ------------------------------------------------------------ length tuning

_SQRT2 = math.sqrt(2)
# Every corner of a meander is cut on a 45 degree diagonal, this share of
# the leg pitch either side of the corner: a U-turn then is half a regular
# octagon (cut, flat and cut the same length).
_CHAMFER = 1 / (2 + _SQRT2)
# What cutting a corner saves, per mm of cut.
_CUT = 2 - _SQRT2
MEANDER_SIDES = ("left", "right", "both")


def _meander_corners(count: int, side: str) -> int:
    return 2 * count + 2 if side == "both" else 4 * count


def _meander_added(count: int, height: float, pitch: float, side: str) -> float:
    """What `count` meanders of this height add to a segment's length."""
    return 2 * count * height - _meander_corners(count, side) * _CUT * min(pitch * _CHAMFER, height / 2)


def _meander_fit(length: float, pitch: float, side: str) -> int:
    """How many meanders a segment of this length holds. One-sided, the
    meanders stand two leg pitches apart; alternating sides, one. Either
    end keeps half a leg pitch of straight segment beyond the corner cuts."""
    runs = int((length - pitch - 2 * pitch * _CHAMFER + _EPS) // pitch)
    return max(runs if side == "both" else (runs + 1) // 2, 0)


def _check_side(side: str) -> None:
    if side not in MEANDER_SIDES:
        raise ValueError(f"side must be 'left', 'right' or 'both', not {side!r}")


def meander_capacity(length: float, amplitude: float, pitch: float, side: str = "left") -> float:
    """The most length (mm) a meander can add to a straight segment of
    `length` mm: as many meanders as fit, each as tall as `amplitude`."""
    _check_side(side)
    count = _meander_fit(length, pitch, side)
    return _meander_added(count, amplitude, pitch, side) if count else 0.0


def meander(
    a: Point, b: Point, extra: float, amplitude: float, pitch: float, side: str = "left"
) -> tuple[list[Point], int, float]:
    """An accordion on the straight segment a->b: (polyline from a to b that
    is `extra` mm longer than the segment, number of meanders, their height).

    Each meander leaves the segment square to it, runs `pitch` along, and
    comes back, so neighbouring legs are `pitch` apart centre to centre
    (the caller derives it from the trace: its width plus the clear space
    wanted between legs). `side` is where they go as seen in the direction
    of travel: "left" or "right" of the segment, each meander standing on
    it, or "both", swinging from one side to the other. Corners are cut on
    45 degree diagonals, so a 45-degree segment gives a 45-degree meander.

    The height is how far the polyline moves from the segment, at most
    `amplitude`. The fewest meanders that can reach `extra` are used, all
    the same height, sized so the added length is exact. They sit in the
    middle of the segment. A meander too low for a leg degenerates to a
    trapezoid (a diagonal out, a flat, a diagonal back).

    Raises ValueError when the segment cannot hold that much.
    """
    _check_side(side)
    if extra < _EPS:
        return [a, b], 0, 0.0
    if amplitude <= 0 or pitch <= 0:
        raise ValueError("amplitude and leg pitch must be positive")
    length = math.dist(a, b)
    most = _meander_fit(length, pitch, side)
    if not most:
        raise ValueError(
            f"a {length:.3f}mm segment is too short for a meander at a {pitch:.3f}mm leg pitch "
            f"(one needs {2 * pitch * (1 + _CHAMFER):.3f}mm)"
        )
    count = next((n for n in range(1, most + 1) if _meander_added(n, amplitude, pitch, side) >= extra - 1e-12), 0)
    if not count:
        raise ValueError(
            f"+{extra:.3f}mm does not fit: a {length:.3f}mm segment at a {pitch:.3f}mm leg pitch holds at most "
            f"{most} meander(s) of {amplitude:.3f}mm: +{_meander_added(most, amplitude, pitch, side):.3f}mm"
        )
    corners, cut = _meander_corners(count, side), pitch * _CHAMFER
    if 2 * cut <= amplitude and extra > _meander_added(count, 2 * cut, pitch, side):
        height = (extra + corners * _CUT * cut) / (2 * count)     # legs with full corner cuts
    else:
        height = extra / (2 * count - corners * _CUT / 2)         # too low for a leg: the cuts are height / 2
    height = min(height, amplitude)
    if side == "both":
        levels = [height if i % 2 == 0 else -height for i in range(count)]
    else:
        out = height if side == "left" else -height
        levels = [out if i % 2 == 0 else 0.0 for i in range(2 * count - 1)]
    u = (length - len(levels) * pitch) / 2
    square: list[Point] = [(0.0, 0.0), (u, 0.0)]
    for level in levels:
        square += [(u, level), (u + pitch, level)]
        u += pitch
    square += [(u, 0.0), (length, 0.0)]
    return _to_board(_cut_corners(square, min(cut, height / 2)), a, b), count, height


def skew_bumps(
    a: Point, b: Point, extra: float, amplitude: float, top: float, side: str
) -> tuple[list[Point], int, float]:
    """Small bumps on the straight trace segment a->b, for the shorter trace
    of a differential pair: (polyline from a to b that is `extra` mm longer
    than the segment, number of bumps, their height).

    A bump leaves the segment on a 45 degree diagonal, runs `top` mm
    parallel to it and comes back on a diagonal, to the "left" or "right"
    of travel (the caller picks the side away from the other trace). It
    adds 0.83 x its height. The fewest bumps that keep the height within
    `amplitude` are used, all the same height, sized so the added length is
    exact, with `top` mm of plain trace between two and at least half that
    at either end, in the middle of the segment.

    Raises ValueError when the segment is too short for them.
    """
    if side not in ("left", "right"):
        raise ValueError(f"side must be 'left' or 'right', not {side!r}")
    if extra < _EPS:
        return [a, b], 0, 0.0
    if amplitude <= 0 or top <= 0:
        raise ValueError("amplitude and top length must be positive")
    per_bump = 2 * (_SQRT2 - 1)
    count = math.ceil(extra / (per_bump * amplitude) - 1e-9)
    height = extra / (per_bump * count)
    length = math.dist(a, b)
    need = count * (2 * height + 2 * top)
    if need > length + _EPS:
        raise ValueError(f"{count} bump(s) of {height:.3f}mm need {need:.3f}mm; the segment has {length:.3f}mm")
    u = (length - need + top) / 2
    out = height if side == "left" else -height
    local: list[Point] = [(0.0, 0.0)]
    for _ in range(count):
        local += [(u, 0.0), (u + height, out), (u + height + top, out), (u + 2 * height + top, 0.0)]
        u += 2 * height + 2 * top
    local.append((length, 0.0))
    return _to_board(local, a, b), count, height


def _cut_corners(points: list[Point], cut: float) -> list[Point]:
    """The polyline with every corner replaced by a diagonal from `cut`
    before it to `cut` after it."""
    out = [points[0]]
    for before, corner, after in zip(points, points[1:], points[2:]):
        (ix, iy), (ox, oy) = heading(before, corner), heading(corner, after)
        out += [(corner[0] - cut * ix, corner[1] - cut * iy), (corner[0] + cut * ox, corner[1] + cut * oy)]
    out.append(points[-1])
    return out


def _to_board(local: list[Point], a: Point, b: Point) -> list[Point]:
    """Points given as (mm along a->b, mm to the left of it) in board
    coordinates, from exactly `a` to exactly `b`."""
    (ux, uy), (nx, ny) = heading(a, b), left_normal(a, b)
    inner = [(a[0] + u * ux + v * nx, a[1] + u * uy + v * ny) for u, v in local[1:-1]]
    return _join(_join([a], inner), [b])


def locate(path: list[Point], a: Point, b: Point) -> tuple[int, Point, Point]:
    """Where the straight run a->b lies on a polyline: (index of the
    segment that runs along it in the same direction, the two ends of the
    part of a->b that segment covers).

    Finds the trace segment a routing-file hop turned into after doglegs
    merged collinear hops, corners were mitred or the ends set back.
    Raises ValueError when no segment runs along a->b.
    """
    ux, uy = heading(a, b)
    span = math.dist(a, b)
    for i, (p, q) in enumerate(zip(path, path[1:])):
        if any(abs((pt[0] - a[0]) * uy - (pt[1] - a[1]) * ux) > _EPS for pt in (p, q)):
            continue
        start, end = ((pt[0] - a[0]) * ux + (pt[1] - a[1]) * uy for pt in (p, q))
        low, high = max(start, 0.0), min(end, span)
        if high - low > _EPS:
            return i, (a[0] + low * ux, a[1] + low * uy), (a[0] + high * ux, a[1] + high * uy)
    raise ValueError(f"no segment runs along {a}->{b}")


def splice(path: list[Point], index: int, points: list[Point]) -> list[Point]:
    """The polyline with `points` (a detour that starts and ends on the
    segment after `path[index]`) put into that segment."""
    return _join(_join(path[: index + 1], points), path[index + 1 :])


def net_length(tracks, vias, pads=(), depths: dict[str, float] | None = None) -> dict:
    """The length of one net's copper, as `cs pcb route` counts it:

        {"layers": {layer: mm of track}, "vias": count, "via_length": mm,
         "pad_length": mm, "total": mm}

    `tracks` are (layer, start, end, length) per segment or arc, `vias`
    (centre, pad radius), `pads` (bounding box (x0, y0, x1, y1), copper
    layer or None for a through-hole pad, extra length in mm).

    A via counts the height between the outermost layers it joins: those
    of the tracks that end inside its pad, and of an SMD pad it sits in.
    The heights come from `depths` (stackup.copper_depths); without it, or
    for a via that joins fewer than two layers (a stitch to a plane), a
    via adds nothing. `pad_length` is the sum of the pads' extra lengths.
    """
    layers: dict[str, float] = {}
    for layer, _, _, length in tracks:
        layers[layer] = layers.get(layer, 0.0) + length
    via_length = 0.0
    if depths and vias:
        cell = max(max(radius for _, radius in vias), 0.5)
        ends: dict[tuple[int, int], list] = {}
        for layer, start, end, _ in tracks:
            for x, y in (start, end):
                ends.setdefault((math.floor(x / cell), math.floor(y / cell)), []).append((x, y, layer))
        boxes: dict[tuple[int, int], list] = {}
        for (x0, y0, x1, y1), layer, _ in pads:
            if layer is None:
                continue
            for i in range(math.floor(x0 / cell), math.floor(x1 / cell) + 1):
                for j in range(math.floor(y0 / cell), math.floor(y1 / cell) + 1):
                    boxes.setdefault((i, j), []).append((x0, y0, x1, y1, layer))
        for (x, y), radius in vias:
            cx, cy = math.floor(x / cell), math.floor(y / cell)
            joined = {
                layer
                for i in (cx - 1, cx, cx + 1)
                for j in (cy - 1, cy, cy + 1)
                for ex, ey, layer in ends.get((i, j), ())
                if math.hypot(ex - x, ey - y) <= radius + _EPS
            }
            joined |= {layer for x0, y0, x1, y1, layer in boxes.get((cx, cy), ()) if x0 <= x <= x1 and y0 <= y <= y1}
            known = [depths[layer] for layer in joined if layer in depths]
            if len(known) > 1:
                via_length += max(known) - min(known)
    pad_length = sum(extra for _, _, extra in pads)
    return {
        "layers": layers,
        "vias": len(vias),
        "via_length": via_length,
        "pad_length": pad_length,
        "total": sum(layers.values()) + via_length + pad_length,
    }
