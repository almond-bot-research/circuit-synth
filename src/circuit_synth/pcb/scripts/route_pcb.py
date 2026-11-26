"""Apply a hand-written routing file to a board (`cs pcb route`).

Routing as code: the designer (human or agent) decides every trace and
writes it down as waypoints; this script only turns that into copper. It
is not an autorouter. The routing file is a Python file next to the board
that defines `route(r)` and calls the `Router` API:

    def route(r):
        r.zone("GND", ["In1.Cu"], [(88, 60), (132, 60), (132, 117), (88, 117)])
        r.keepout(["F.Cu", "B.Cu"], [...])
        r.route("CAN0_TX", "U3.1", (125.0, 70.2), "U4.2")
        r.route("SPI0_SCK", "U11.4", (112, 80), "via", (116, 84), "via", "U3.10")
        r.route("COAX_A", "J13.1", (200, 150), "via:In7.Cu", (190, 140), "via:F.Cu", "C242.1")
        r.pair(("USB_RX_P", "USB_RX_N"), ("J22.A22", "J22.A23"), (140.15, 101), (140.15, 103),
               "via:In2.Cu", (140.15, 70), "via:F.Cu", (140.15, 66), ("U13.9", "U13.10"))
        r.stitch("GND", "C11.2", dx=0.9)          # pad -> short stub -> via

Waypoints are `"REF.PAD"` (pad centre; the pad's net must match),
`(x, y)` in board mm, or `r.at("REF.PAD", dx, dy)`. Consecutive waypoints
are joined with 45 degree doglegs ("sd" = straight then diagonal, the
default; put "ds" before a waypoint for diagonal then straight).

Layers are the board's own enabled copper layers (`r.layers`); naming any
other is a RoutingError that lists them. `"via:<layer>"` drops a via at the
current point and continues on that layer; a bare `"via"` swaps F.Cu/B.Cu;
a layer name on its own switches layer without a via (e.g. when starting
on a through-hole pad). Vias are through vias whichever layers they join
(`r.via_type`; routing_geometry.via_span is where another kind would get
its span).

Differential pairs: `r.pair((P net, N net), ...)` routes both traces along
one centre-line of `(x, y)` waypoints, `width + gap` apart centre to centre,
with the same tokens as `route`. Corners are mitred so the gap holds round
every bend (the outer trace is longer than the inner one; that is the skew a
turn costs). A `(P pad, N pad)` tuple as the first or last item connects the
pads: the pair is coupled from the first centre-line waypoint to the last,
and each pad is joined to its trace's end of that section with a dogleg in
the bend mode in effect ("sd": straight out of the pad, then diagonal). P
is on the side of the centre-line its start pad is on; give `p="left"` or
`p="right"` (of the direction of travel) when the route does not start at
pads or they are in line with it. A pair that would have to cross to reach
its end pads is an error. At `"via:<layer>"` the traces fan out on 45
degree diagonals to two vias side by side across the direction of travel
(via copper `via_gap` apart) and fan back in on the new layer, so the pair
must leave its vias straight on (or straight back) and needs a centre-line
segment either side. `pair` returns, and the run prints, each trace's length
and the skew (P minus N; track length, via barrels not counted). Length
matching is not automatic: add the detour where you want it.

Widths and sizes: a number passed in the call always wins (`width=`,
`gap=`, `via=(diameter, drill)`, `via_gap=`). Otherwise `route`, `track`
and `stitch` use the Router-wide `r.width` / `r.via_size` if set, else the
net's class (design_rules.NetRules): `track_width`, `via_diameter` /
`via_drill`, and for pairs `diff_pair_width`, `diff_pair_gap` and
`diff_pair_via_gap`, each with the class's override for the current layer
where the design declared one, so one call follows the per-layer impedance
geometry through its layer changes. `r.width` and `r.via_size` start as
0.2 and (0.6, 0.3) on a design that declares no net classes of its own
(build_project's `netclass_defs`), which is how routing files written
before net classes drove the Router keep producing the same copper, and as
None - every net takes its class's geometry - on one that does.

Everything the script creates goes into one board group (`routing`), and
that group is deleted and rebuilt on every run, so the routing file stays
the source of truth while hand-placed copper and package-layout groups
are left alone. Zones are refilled before saving.

`--lengths[=PATTERN]` (repeatable; all nets without a pattern) prints the
routed length of the matching nets: total, per layer, and via count, over
all copper on the board, not only what this run created.

Usage: route_pcb.py <project_dir> <build_name> [routing.py] [--no-fill] [--lengths[=PATTERN] ...]
"""

from __future__ import annotations

import collections
import fnmatch
import importlib.util
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pcbnew  # noqa: E402
from design_rules import NetRules  # noqa: E402
from routing_geometry import (  # noqa: E402
    expand_path,
    heading,
    is_45,
    pair_tracks,
    pair_via_setback,
    pair_vias,
    path_length,
    side_of,
    via_span,
)

GROUP = "routing"
# Router.via_type -> pcbnew's via type. routing_geometry.via_span decides
# which layers a type spans.
_VIA_TYPES = {"through": "VIATYPE_THROUGH"}


class RoutingError(Exception):
    """A routing file asked for something inconsistent with the board."""


# One routed differential pair: lengths in mm of track (via barrels not
# counted), skew = p_length - n_length, via_pairs = layer changes.
PairReport = collections.namedtuple("PairReport", "nets p_length n_length skew via_pairs")


class Router:
    """Creates tracks, vias, zones and keepouts from explicit waypoints."""

    def __init__(self, board: "pcbnew.BOARD", rules: NetRules | None = None) -> None:
        self.board = board
        self.rules = rules or NetRules()
        self.layers = [board.GetLayerName(layer) for layer in board.GetEnabledLayers().CuStack()]
        self.group = pcbnew.PCB_GROUP(board)
        self.group.SetName(GROUP)
        board.Add(self.group)
        # Router-wide defaults, used before the net class when set (see the
        # module docstring).
        class_driven = bool(self.rules.declared)
        self.width: float | None = None if class_driven else 0.2
        self.via_size: tuple[float, float] | None = None if class_driven else (0.6, 0.3)
        self.via_type = "through"
        self.stats = {"tracks": 0, "vias": 0, "zones": 0, "length": 0.0}
        self.pairs: list[PairReport] = []
        self._pads: dict[str, "pcbnew.PAD"] = {}
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                # multi-pad pins (e.g. two solder pins per pole): first wins,
                # the rest are reachable as REF.PAD#2, #3...
                key = f"{fp.GetReference()}.{pad.GetNumber()}"
                n = 2
                while key in self._pads:
                    key = f"{fp.GetReference()}.{pad.GetNumber()}#{n}"
                    n += 1
                self._pads[key] = pad

    # ------------------------------------------------------------ lookup

    def pad(self, spec: str) -> "pcbnew.PAD":
        """The pad named `REF.PAD` (`REF.PAD#2` for a pin's second pad)."""
        try:
            return self._pads[spec]
        except KeyError:
            raise RoutingError(f"no pad {spec!r} on the board") from None

    def at(self, spec: str, dx: float = 0.0, dy: float = 0.0) -> tuple[float, float]:
        """Board coordinates (mm) of a pad centre, optionally offset."""
        p = self.pad(spec).GetPosition()
        return (round(pcbnew.ToMM(p.x) + dx, 6), round(pcbnew.ToMM(p.y) + dy, 6))

    def _net(self, name: str) -> "pcbnew.NETINFO_ITEM":
        net = self.board.FindNet(name)
        if net is None and not name.startswith("/"):
            net = self.board.FindNet("/" + name)
        if net is None:
            raise RoutingError(f"no net {name!r} on the board")
        return net

    def _point(self, item, net: "pcbnew.NETINFO_ITEM") -> tuple[float, float]:
        if isinstance(item, str):
            pad = self.pad(item)
            if pad.GetNetname() != net.GetNetname():
                raise RoutingError(
                    f"{item} is on net {pad.GetNetname()!r}, not {net.GetNetname()!r}"
                )
            return self.at(item)
        return (float(item[0]), float(item[1]))

    def _layer(self, name: str) -> str:
        if name not in self.layers:
            raise RoutingError(f"no copper layer {name!r} on the board; valid: {', '.join(self.layers)}")
        return name

    def _token(self, item) -> tuple[str, str | None] | None:
        """A layer-change item as ("via", target layer or None for the
        F.Cu/B.Cu swap) or ("layer", name); None for anything else."""
        if not isinstance(item, str):
            return None
        if item == "via":
            return ("via", None)
        if item.startswith("via:"):
            return ("via", self._layer(item[4:]))
        if item in self.layers or item.endswith(".Cu"):
            return ("layer", self._layer(item))
        return None

    def _width(self, net: "pcbnew.NETINFO_ITEM", layer: str, width: float | None) -> float:
        return width or self.width or self.rules.value(net.GetNetname(), "track_width", layer)

    def _via_size(self, net: "pcbnew.NETINFO_ITEM", size: float | None = None,
                  drill: float | None = None) -> tuple[float, float]:
        name = net.GetNetname()
        default = self.via_size or (self.rules.value(name, "via_diameter"), self.rules.value(name, "via_drill"))
        return (size or default[0], drill or default[1])

    # ---------------------------------------------------------- creation

    def _add(self, item) -> None:
        self.board.Add(item)
        self.group.AddItem(item)

    def track(self, net: str, points, layer: str = "F.Cu", width: float | None = None) -> None:
        """Exact polyline on one layer, no dogleg expansion."""
        n = self._net(net)
        pts = [self._point(p, n) for p in points]
        self._segments(n, pts, layer, self._width(n, layer, width))

    def _segments(self, net, pts, layer: str, width: float) -> None:
        lid = self.board.GetLayerID(self._layer(layer))
        for a, b in zip(pts, pts[1:]):
            if not is_45(a, b):
                print(f"  note: {net.GetNetname()} segment {a}->{b} is not on a 45 degree angle")
            t = pcbnew.PCB_TRACK(self.board)
            t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
            t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
            t.SetWidth(pcbnew.FromMM(width))
            t.SetLayer(lid)
            t.SetNet(net)
            self._add(t)
            self.stats["tracks"] += 1
        self.stats["length"] += path_length(pts)

    def via(self, net: str, at, size: float | None = None, drill: float | None = None,
            between: tuple[str, str] | None = None) -> None:
        """Via at a point or pad spec. `between` names the two layers it
        joins; a through via (the only `via_type` so far) spans the board
        regardless."""
        n = self._net(net)
        x, y = self._point(at, n) if isinstance(at, str) else (float(at[0]), float(at[1]))
        try:
            top, bottom = via_span(self.via_type, self.layers, *(between or (self.layers[0], self.layers[-1])))
        except ValueError as e:
            raise RoutingError(f"{net}: {e}") from None
        diameter, hole = self._via_size(n, size, drill)
        v = pcbnew.PCB_VIA(self.board)
        v.SetViaType(getattr(pcbnew, _VIA_TYPES[self.via_type]))
        v.SetLayerPair(self.board.GetLayerID(top), self.board.GetLayerID(bottom))
        v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(diameter))
        v.SetDrill(pcbnew.FromMM(hole))
        v.SetNet(n)
        self._add(v)
        self.stats["vias"] += 1

    def _via_target(self, net: str, layer: str, target: str | None) -> str:
        """The layer a via token continues on (bare "via": the F.Cu/B.Cu swap)."""
        to = target or ("B.Cu" if layer == "F.Cu" else "F.Cu")
        if to == layer:
            raise RoutingError(f"{net}: 'via:{to}' while already on {to}")
        return to

    def route(self, net: str, *items, layer: str = "F.Cu", width: float | None = None,
              via: tuple[float, float] | None = None) -> None:
        """Route through waypoints with 45 degree doglegs.

        See the module docstring for the waypoint forms, the "via",
        "via:<layer>", "sd"/"ds" and layer-name tokens, and where the
        width and via size come from when not given.
        """
        n = self._net(net)
        pts: list[tuple[float, float]] = []
        bends: list[str] = []
        bend = "sd"

        def flush() -> None:
            if len(pts) >= 2:
                self._segments(n, expand_path(pts, bends), layer, self._width(n, layer, width))

        for item in items:
            if isinstance(item, str) and item in ("sd", "ds"):
                bend = item
                continue
            token = self._token(item)
            if token is None:
                if pts:
                    bends.append(bend)
                pts.append(self._point(item, n))
                continue
            kind, target = token
            if not pts:
                if kind == "via":
                    raise RoutingError(f"{net}: 'via' before the first waypoint")
                layer = target
                continue
            flush()
            last = pts[-1]
            if kind == "via":
                to = self._via_target(net, layer, target)
                self.via(net, last, *(via or ()), between=(layer, to))
                layer = to
            else:
                layer = target
            pts, bends = [last], []
        flush()

    def pair(self, nets: tuple[str, str], *items, layer: str = "F.Cu", width: float | None = None,
             gap: float | None = None, via: tuple[float, float] | None = None,
             via_gap: float | None = None, p: str | None = None) -> PairReport:
        """Route a differential pair, `nets` = (P net, N net), along a
        centre-line.

        Items are `(x, y)` centre-line waypoints, the "sd"/"ds" and via
        tokens of `route`, a layer name before the first waypoint, and a
        `(P pad, N pad)` tuple as the first and/or last item. See the
        module docstring for the geometry, `p`, and where width, gap and
        via sizes come from. Returns the pair's lengths and skew.
        """
        p_net, n_net = self._net(nets[0]), self._net(nets[1])
        names = (p_net.GetNetname(), n_net.GetNetname())
        label = "/".join(names)
        if self.rules.class_of(names[0]) != self.rules.class_of(names[1]):
            raise RoutingError(
                f"{label}: the two nets are in different net classes "
                f"({self.rules.class_of(names[0])}, {self.rules.class_of(names[1])})"
            )
        if p not in (None, "left", "right"):
            raise RoutingError(f"{label}: p must be 'left' or 'right', not {p!r}")
        via_size = self._via_size(p_net, *(via or ()))
        if via_gap is None:
            via_gap = self.rules.value(names[0], "diff_pair_via_gap")
        via_pitch = via_size[0] + via_gap

        # Split the items into one section per layer: its centre-line
        # waypoints, and what it starts from / ends on.
        def section(on: str) -> dict:
            return {"layer": on, "pts": [], "bends": [], "start": None, "start_bend": "sd",
                    "end": None, "end_bend": "sd", "via_to": None}

        sections: list[dict] = []
        cur = section(self._layer(layer))
        bend = "sd"
        finished = False
        for item in items:
            if finished:
                raise RoutingError(f"{label}: nothing may follow the end pads")
            if isinstance(item, str) and item in ("sd", "ds"):
                bend = item
                continue
            if isinstance(item, (tuple, list)) and len(item) == 2 and all(isinstance(s, str) for s in item):
                ends = (self._point(item[0], p_net), self._point(item[1], n_net))
                if cur["pts"]:
                    cur["end"], cur["end_bend"] = ends, bend
                    finished = True
                elif sections or cur["start"] is not None:
                    raise RoutingError(f"{label}: pads {item} need a centre-line waypoint before them")
                else:
                    cur["start"] = ends
                continue
            token = self._token(item)
            if token is None:
                if isinstance(item, str):
                    raise RoutingError(
                        f"{label}: {item!r} is not a pair waypoint; use (x, y) centre-line points "
                        "and a (P pad, N pad) tuple at the ends"
                    )
                if cur["pts"]:
                    cur["bends"].append(bend)
                else:
                    cur["start_bend"] = bend
                cur["pts"].append((float(item[0]), float(item[1])))
                continue
            kind, target = token
            if kind == "layer":
                if cur["pts"] or sections:
                    raise RoutingError(f"{label}: a pair changes layer with 'via:{target}', not a bare layer name")
                cur["layer"] = target
                continue
            if not cur["pts"]:
                raise RoutingError(f"{label}: 'via' before the first centre-line waypoint")
            cur["via_to"] = self._via_target(label, cur["layer"], target)
            sections.append(cur)
            cur = section(cur["via_to"])
            cur["pts"].append(sections[-1]["pts"][-1])
        sections.append(cur)

        side = {None: 0, "left": 1, "right": -1}[p]
        arrived = None  # (via point, heading into it, (P via, N via))
        p_length = n_length = 0.0
        for sec in sections:
            on = sec["layer"]
            track_width = width or self.rules.value(names[0], "diff_pair_width", on)
            pitch = track_width + (gap or self.rules.value(names[0], "diff_pair_gap", on))
            setback = pair_via_setback(pitch, via_pitch)
            centre = expand_path(sec["pts"], sec["bends"])
            if len(centre) < 2:
                raise RoutingError(
                    f"{label}: needs a centre-line segment (two waypoints) on {on} between its pads and vias"
                )
            start, start_bend = sec["start"], sec["start_bend"]
            end, end_bend = sec["end"], sec["end_bend"]
            if arrived is not None:
                at, into, start = arrived
                out = heading(centre[0], centre[1])
                if abs(out[0] * into[0] + out[1] * into[1]) < 1 - 1e-6:
                    raise RoutingError(
                        f"{label}: must leave its vias at {at} straight on or straight back "
                        "(they sit side by side across the way it arrived); add a waypoint before turning"
                    )
                centre[0] = self._set_back(label, centre[0], centre[1], setback)
                side = side_of(start[0], start[1], centre[0], centre[1])
                start_bend = "sd"
            elif start is not None:
                pad_side = side_of(start[0], start[1], centre[0], centre[1])
                if side and pad_side and pad_side != side:
                    raise RoutingError(f"{label}: p={p!r} puts P on the other side from its start pad")
                side = side or pad_side
            if not side:
                raise RoutingError(
                    f"{label}: cannot tell which side of the centre-line P runs on (no start pads, or they "
                    "are in line with the route); pass p='left' or p='right'"
                )
            vias = None
            if sec["via_to"]:
                at, into = centre[-1], heading(centre[-2], centre[-1])
                centre[-1] = self._set_back(label, centre[-1], centre[-2], setback)
                vias = pair_vias(at, into, via_pitch, side)
                end, end_bend = vias, "sd"
                arrived = (at, into, vias)
            elif end is not None:
                pad_side = side_of(end[0], end[1], centre[-2], centre[-1])
                if pad_side and pad_side != side:
                    raise RoutingError(
                        f"{label}: arrives with P on the {'left' if side > 0 else 'right'} but its end pad "
                        "is on the other side; the pair cannot cross itself"
                    )
            try:
                p_pts, n_pts = pair_tracks(centre, pitch, side, start, start_bend, end, end_bend)
            except ValueError as e:
                raise RoutingError(f"{label}: {e}") from None
            self._segments(p_net, p_pts, on, track_width)
            self._segments(n_net, n_pts, on, track_width)
            p_length += path_length(p_pts)
            n_length += path_length(n_pts)
            if vias:
                for name, at in zip(names, vias):
                    self.via(name, at, *via_size, between=(on, sec["via_to"]))
        report = PairReport(names, p_length, n_length, p_length - n_length, len(sections) - 1)
        self.pairs.append(report)
        return report

    @staticmethod
    def _set_back(label: str, end, other, distance: float) -> tuple[float, float]:
        """`end` moved `distance` towards `other`: where a pair stops being
        coupled before its vias."""
        length = math.dist(end, other)
        if length <= distance + 1e-6:
            raise RoutingError(
                f"{label}: the centre-line segment at the vias near {end} is {length:.3f}mm; the fan-out "
                f"to the vias alone needs {distance:.3f}mm"
            )
        return (end[0] + (other[0] - end[0]) * distance / length, end[1] + (other[1] - end[1]) * distance / length)

    def lengths(self) -> dict[str, dict]:
        """Routed copper per net so far (see net_lengths)."""
        return net_lengths(self.board)

    def stitch(self, net: str, pad: str, dx: float = 0.0, dy: float = 0.0,
               width: float | None = None, layer: str | None = None) -> None:
        """Short stub from a pad to a via at (dx, dy) from its centre.

        The usual plane connection for an SMD power pin. The stub runs on
        the pad's own layer unless `layer` is given.
        """
        p = self.pad(pad)
        lay = layer or ("F.Cu" if p.IsOnLayer(pcbnew.F_Cu) else "B.Cu")
        self.route(net, pad, self.at(pad, dx, dy), layer=lay, width=width)
        self.via(net, self.at(pad, dx, dy))

    def zone(self, net: str, layers, points, priority: int = 0, name: str = "",
             clearance: float = 0.2, min_width: float = 0.1) -> None:
        """Copper pour on one or more layers (repo convention: solid pads)."""
        z = self._zone(layers, points)
        z.SetNet(self._net(net))
        z.SetAssignedPriority(priority)
        z.SetLocalClearance(pcbnew.FromMM(clearance))
        z.SetMinThickness(pcbnew.FromMM(min_width))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
        if name:
            z.SetZoneName(name)
        self.stats["zones"] += 1

    def keepout(self, layers, points, name: str = "", tracks: bool = True,
                vias: bool = True, pours: bool = True) -> None:
        """Rule area that forbids tracks / vias / pours (pads stay legal)."""
        z = self._zone(layers, points)
        z.SetIsRuleArea(True)
        z.SetDoNotAllowTracks(tracks)
        z.SetDoNotAllowVias(vias)
        # KiCad 9 renamed this setter
        (getattr(z, "SetDoNotAllowZoneFills", None) or z.SetDoNotAllowCopperPour)(pours)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
        if name:
            z.SetZoneName(name)

    def _zone(self, layers, points) -> "pcbnew.ZONE":
        z = pcbnew.ZONE(self.board)
        ls = pcbnew.LSET()
        for layer in [layers] if isinstance(layers, str) else layers:
            ls.AddLayer(self.board.GetLayerID(self._layer(layer)))
        z.SetLayerSet(ls)
        outline = z.Outline()
        outline.NewOutline()
        for x, y in points:
            outline.Append(pcbnew.FromMM(float(x)), pcbnew.FromMM(float(y)))
        self._add(z)
        return z


def clear_previous(board: "pcbnew.BOARD") -> int:
    """Delete the previous run's group and everything in it."""
    removed = 0
    for g in list(board.Groups()):
        if g.GetName() != GROUP:
            continue
        for item in list(g.GetItems()):
            board.Delete(item.Cast() if hasattr(item, "Cast") else item)
            removed += 1
        board.Delete(g)
    return removed


def net_lengths(board: "pcbnew.BOARD") -> dict[str, dict]:
    """Routed copper per net: {net: {"layers": {layer: mm}, "vias": n}}.

    Counts every track, arc and via on the board - hand-placed copper and
    package-layout groups as well as the routing group - because a link's
    length budget is about all of it. Via barrels add no length.
    """
    out: dict[str, dict] = {}
    for item in board.GetTracks():
        entry = out.setdefault(item.GetNetname(), {"layers": {}, "vias": 0})
        if item.GetClass() == "PCB_VIA":
            entry["vias"] += 1
        else:
            layer = board.GetLayerName(item.GetLayer())
            entry["layers"][layer] = entry["layers"].get(layer, 0.0) + item.GetLength() / pcbnew.FromMM(1)
    return out


def print_lengths(lengths: dict[str, dict], patterns: list[str], layers: list[str]) -> None:
    """The per-net length table for the nets matching any pattern (full
    board net names, `*`/`?` wildcards; a pattern without the leading "/"
    of a hierarchical net matches too)."""
    rows = [
        (net, entry)
        for net, entry in sorted(lengths.items())
        if any(fnmatch.fnmatchcase(net, pat) or fnmatch.fnmatchcase(net.lstrip("/"), pat) for pat in patterns)
    ]
    print(f"  lengths (mm), {len(rows)} net(s):")
    wide = max([len(net) for net, _ in rows] + [3])
    print(f"    {'net':{wide}s} {'total':>9s} {'vias':>5s}  per layer")
    for net, entry in rows:
        per_layer = ", ".join(f"{name} {entry['layers'][name]:.3f}" for name in layers if name in entry["layers"])
        print(f"    {net:{wide}s} {sum(entry['layers'].values()):9.3f} {entry['vias']:5d}  {per_layer}")


def load_routing(path: Path):
    spec = importlib.util.spec_from_file_location("routing", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "route"):
        raise SystemExit(f"{path} does not define route(r)")
    return mod.route


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    fill = "--no-fill" not in argv
    length_patterns = [a.partition("=")[2] or "*" for a in argv if a == "--lengths" or a.startswith("--lengths=")]
    if len(args) < 2:
        print(__doc__)
        return 2
    project_dir, build = Path(args[0]), args[1]
    routing = Path(args[2]) if len(args) > 2 else project_dir / "routing.py"
    pcb_path = project_dir / f"{build}.kicad_pcb"
    board = pcbnew.LoadBoard(str(pcb_path))
    removed = clear_previous(board)
    router = Router(board, NetRules.load(project_dir, build))
    try:
        load_routing(routing)(router)
    except RoutingError as e:
        print(f"routing error: {e}")
        return 1
    if fill:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(str(pcb_path))
    s = router.stats
    print(
        f"[{build}] route: {s['tracks']} segments ({s['length']:.0f}mm), {s['vias']} vias, "
        f"{s['zones']} zones (replaced {removed} items) -> {pcb_path}"
    )
    if router.pairs:
        print("  pairs (mm; skew = P - N):")
        for pair in router.pairs:
            print(
                f"    {' / '.join(pair.nets)}: P {pair.p_length:.3f}, N {pair.n_length:.3f}, "
                f"skew {pair.skew:+.3f}, {pair.via_pairs} via pair(s)"
            )
    if length_patterns:
        print_lengths(net_lengths(board), length_patterns, router.layers)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
