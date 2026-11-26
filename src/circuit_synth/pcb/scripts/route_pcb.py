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
and the skew (P minus N).

Lengths are in mm. A net's length is its track on every layer, plus the
height of each via between the outermost layers it joins (the layers of the
tracks that end on it and of an SMD pad it sits in; a through via from F.Cu
to In2.Cu counts that height, not the board's thickness; a via that joins
one layer only, such as a stitching via, counts nothing), plus pad lengths:
the lead or package length behind a pad, taken from the pad's own
pad-to-die length on the board or from `r.pad_lengths({"U1.A4": 1.82})` in
the routing file, which wins. Via heights come from the design's declared
stack-up (build_project's `stackup`), middle of copper to middle of copper.
A design that declares none gets no via heights, and the run says so. All
the copper on the net counts, whatever made it. There is no delay model:
mm on an outer layer and mm on an inner one count the same.

Length matching is planned like the rest: the planner names the segment,
the tool sizes what goes on it and reports it. A marker between two
waypoints names the hop from one to the other, which must be one straight
segment:

- `"skew"` (or `"skew:<amplitude mm>"`) in a `pair` with `skew=<mm>`: when
  P and N differ by more than that, the shorter trace gets small bumps on
  this hop that take the mismatch out. A bump leaves on a 45 degree
  diagonal away from the other trace, runs 3 x the trace width and comes
  back; it is at most the pair's gap high unless an amplitude is given (so
  the gap at most doubles there), and bumps are 3 x the trace width apart.
  With several "skew" hops each takes out the mismatch built up by the end
  of its segment (the pads the pair starts at included) and the last one
  the rest, so the bumps can sit near the bends that cause the mismatch.
- `"meander:<left|right|both>:<amplitude mm>"` (`"meander:<mm>"` = both) in
  a `route` or `pair` with `length=<mm>`: an accordion on this hop brings
  the net (for a pair: the mean of its two nets, after the skew bumps) to
  that length. It goes to the left or right of travel, or swings both
  ways, at most `amplitude` from the segment (centre-line; for a pair the
  pair's centre-line, both traces meander together and keep their gap).
  The fewest meanders that reach the length are used, all the same height.
  Neighbouring legs keep `r.meander_spacing` (default 3) x the trace width
  of clear space between them, for a pair that many times its gap between
  the facing traces. Several "meander" hops fill up in order.
- `r.match(name, nets, tolerance, length=None)` declares a match group:
  `nets` is a pattern (`"CSI0_D?_?"`) or a list of patterns and, for a link
  that runs through a series part, tuples of the nets that add up to one
  member. Every member has to be within `tolerance` of the longest one, or
  of `length` when given. The run prints each member's length and error,
  measured on the finished board.

What cannot be done is a RoutingError (the segment is too short, the net is
already longer, a marker without its `skew=` / `length=`), and nothing is
written. A group out of tolerance, or a tuned net that later copper put
off its target, is reported after the board is saved, and the run exits 1.
Lengths are tuned and checked to 0.001mm. `r.length(net)` is a net's length
so far, e.g. to lengthen one pair to another: `length=a.length`.

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
routed length of the matching nets: total, via count, and the share of each
layer, of the via heights and of the pad lengths, over all copper on the
board, not only what this run created, with the match groups a net is in.

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
from design_rules import NetRules, load_board_setup  # noqa: E402
from routing_geometry import (  # noqa: E402
    MEANDER_SIDES,
    expand_path,
    heading,
    is_45,
    left_normal,
    locate,
    meander,
    meander_capacity,
    net_length,
    pair_tracks,
    pair_via_setback,
    pair_vias,
    path_length,
    side_of,
    skew_bumps,
    splice,
    via_span,
)
from stackup import copper_depths  # noqa: E402

GROUP = "routing"
# Router.via_type -> pcbnew's via type. routing_geometry.via_span decides
# which layers a type spans.
_VIA_TYPES = {"through": "VIATYPE_THROUGH"}


class RoutingError(Exception):
    """A routing file asked for something inconsistent with the board."""


# What a length is tuned and checked to (mm): the precision the reports print.
RESOLUTION = 0.001

# One routed differential pair, as the board stood when the call returned:
# the length in mm of each net (see "Lengths" in the module docstring),
# skew = p_length - n_length, via_pairs = layer changes, length = the mean
# of the two.
PairReport = collections.namedtuple("PairReport", "nets p_length n_length skew via_pairs length")


class Router:
    """Creates tracks, vias, zones and keepouts from explicit waypoints."""

    def __init__(self, board: "pcbnew.BOARD", rules: NetRules | None = None,
                 stackup: dict | None = None) -> None:
        self.board = board
        self.rules = rules or NetRules()
        self.layers = [board.GetLayerName(layer) for layer in board.GetEnabledLayers().CuStack()]
        # Length tuning (see the module docstring). `depths` is where each
        # copper layer lies in the declared stack-up (None: via heights are
        # not counted, `length_note` says why), `tuning` what the tuned
        # calls did, `groups` the declared match groups.
        self.depths: dict[str, float] | None = None
        self.length_note = "lengths are track + pad lengths; via heights are not counted (no stack-up declared)"
        if stackup:
            depths = copper_depths(stackup)
            if list(depths) == self.layers:
                self.depths = depths
                self.length_note = "lengths are track + via heights from the declared stack-up + pad lengths"
            else:
                self.length_note = (
                    "lengths are track + pad lengths; via heights are not counted (the declared stack-up "
                    f"has {len(depths)} copper layers, the board {len(self.layers)})"
                )
        self.meander_spacing = 3.0
        self.tuning: list[str] = []
        self.groups: list[dict] = []
        self.tuned: list[dict] = []
        self._pad_lengths: dict[str, float] = {}
        self._net_pads: dict[str, list[str]] = {}
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
                self._net_pads.setdefault(pad.GetNetname(), []).append(key)

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

    @staticmethod
    def _tune_token(label: str, item) -> tuple[str, str | None, float | None] | None:
        """A length-tuning marker as ("skew", None, amplitude or None) or
        ("meander", side, amplitude); None for anything else."""
        if not isinstance(item, str) or not (item == "skew" or item.startswith(("skew:", "meander"))):
            return None
        kind, *args = item.split(":")
        try:
            if kind == "skew" and len(args) <= 1:
                amplitude = float(args[0]) if args else None
                if amplitude is None or amplitude > 0:
                    return ("skew", None, amplitude)
            if kind == "meander" and len(args) in (1, 2):
                side = args[0] if len(args) == 2 else "both"
                if side in MEANDER_SIDES and float(args[-1]) > 0:
                    return ("meander", side, float(args[-1]))
        except ValueError:
            pass
        raise RoutingError(f"{label}: {item!r} is not 'skew', 'skew:<mm>' or 'meander:<left|right|both>:<mm>'")

    @staticmethod
    def _mark(label: str, item: str, tune: tuple, a, b) -> dict:
        """The hop a->b as a tuning marker names it."""
        if math.dist(a, b) < 1e-6 or not is_45(a, b):
            raise RoutingError(f"{label}: the hop {a}->{b} marked {tune[0]!r} is not one straight segment")
        return {"text": item, "kind": tune[0], "side": tune[1], "amplitude": tune[2], "a": a, "b": b}

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
              via: tuple[float, float] | None = None, length: float | None = None) -> None:
        """Route through waypoints with 45 degree doglegs.

        See the module docstring for the waypoint forms, the "via",
        "via:<layer>", "sd"/"ds" and layer-name tokens, where the width
        and via size come from when not given, and for `length` with its
        "meander:..." markers.
        """
        n = self._net(net)
        pts: list[tuple[float, float]] = []
        bends: list[str] = []
        marks: list[dict] = []
        bend = "sd"
        mark = None
        # What to create, in order: ("run", layer, polyline, markers on it)
        # and ("via", at, from layer, to layer).
        steps: list[tuple] = []

        def flush() -> None:
            if len(pts) >= 2:
                steps.append(("run", layer, expand_path(pts, bends), marks))

        for item in items:
            if isinstance(item, str) and item in ("sd", "ds"):
                bend = item
                continue
            tune = self._tune_token(net, item)
            if tune:
                if tune[0] == "skew":
                    raise RoutingError(f"{net}: 'skew' is for differential pairs")
                if mark or not pts:
                    raise RoutingError(f"{net}: {item!r} goes between the two waypoints of the hop it marks")
                mark = (item, tune)
                continue
            token = self._token(item)
            if token is None:
                point = self._point(item, n)
                if pts:
                    bends.append(bend)
                if mark:
                    marks.append(self._mark(net, *mark, pts[-1], point))
                    mark = None
                pts.append(point)
                continue
            if mark:
                raise RoutingError(f"{net}: {mark[0]!r} goes between the two waypoints of the hop it marks")
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
                steps.append(("via", last, layer, to))
                layer = to
            else:
                layer = target
            pts, bends, marks = [last], [], []
        if mark:
            raise RoutingError(f"{net}: {mark[0]!r} goes between the two waypoints of the hop it marks")
        flush()
        if length is not None or any(step[0] == "run" and step[3] for step in steps):
            steps = self._tune_route(n, steps, width, via, length)
        for step in steps:
            if step[0] == "run":
                self._segments(n, step[2], step[1], self._width(n, step[1], width))
            else:
                self.via(net, step[1], *(via or ()), between=(step[2], step[3]))

    def _tune_route(self, net: "pcbnew.NETINFO_ITEM", steps: list[tuple], width: float | None,
                    via: tuple[float, float] | None, length: float | None) -> list[tuple]:
        """The steps of a `route` call with its meanders in, so that the net
        comes to `length` mm."""
        name = net.GetNetname()
        if length is None:
            raise RoutingError(f"{name}: 'meander' needs the length to reach: length=<mm>")
        runs = [i for i, step in enumerate(steps) if step[0] == "run"]
        slots = []
        for i in runs:
            _, on, path, marks = steps[i]
            pitch = self._width(net, on, width) * (1 + self.meander_spacing)
            slots += [(i, mark, pitch, *self._spot(name, mark, path)[1:]) for mark in marks]

        def measure() -> float:
            tracks = [(steps[i][1], steps[i][2]) for i in runs]
            vias = [(step[1], self._via_size(net, *(via or ()))[0]) for step in steps if step[0] == "via"]
            return self._measure(net, tracks, vias)["total"]

        before = measure()
        extras = self._lengthen(name, before, length, [slot[1:] for slot in slots])
        steps, said = list(steps), []
        for (i, mark, pitch, a, b), extra in zip(slots, extras):
            _, on, path, marks = steps[i]
            points, note = self._meander(name, mark, a, b, extra, pitch)
            if note:
                steps[i] = ("run", on, splice(path, self._spot(name, mark, path)[0], points), marks)
                said.append(f"{note}, {on} {mark['a']}->{mark['b']}")
        after = measure()
        if abs(after - length) > 2 * RESOLUTION:
            raise RoutingError(f"{name}: the length came out at {after:.3f}mm, not the {length:.3f}mm asked for")
        self.tuning.append(f"{name}: length {before:.3f} -> {after:.3f}: {'; '.join(said) or 'nothing to add'}")
        self.tuned.append({"label": name, "nets": (name,), "skew": None, "length": length})
        return steps

    def pair(self, nets: tuple[str, str], *items, layer: str = "F.Cu", width: float | None = None,
             gap: float | None = None, via: tuple[float, float] | None = None,
             via_gap: float | None = None, p: str | None = None, skew: float | None = None,
             length: float | None = None) -> PairReport:
        """Route a differential pair, `nets` = (P net, N net), along a
        centre-line.

        Items are `(x, y)` centre-line waypoints, the "sd"/"ds" and via
        tokens of `route`, a layer name before the first waypoint, and a
        `(P pad, N pad)` tuple as the first and/or last item. See the
        module docstring for the geometry, `p`, where width, gap and via
        sizes come from, and for `skew` and `length` with their "skew" and
        "meander:..." markers. Returns the pair's lengths and skew.
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
                    "end": None, "end_bend": "sd", "via_to": None, "marks": []}

        sections: list[dict] = []
        cur = section(self._layer(layer))
        bend = "sd"
        finished = False
        mark = None
        start_pads = None
        for item in items:
            if finished:
                raise RoutingError(f"{label}: nothing may follow the end pads")
            if isinstance(item, str) and item in ("sd", "ds"):
                bend = item
                continue
            tune = self._tune_token(label, item)
            if tune:
                if mark or not cur["pts"]:
                    raise RoutingError(f"{label}: {item!r} goes between the two waypoints of the hop it marks")
                mark = (item, tune)
                continue
            if mark and (isinstance(item, str) or isinstance(item[0], str)):
                raise RoutingError(f"{label}: {mark[0]!r} goes between the two waypoints of the hop it marks")
            if isinstance(item, (tuple, list)) and len(item) == 2 and all(isinstance(s, str) for s in item):
                ends = (self._point(item[0], p_net), self._point(item[1], n_net))
                if cur["pts"]:
                    cur["end"], cur["end_bend"] = ends, bend
                    finished = True
                elif sections or cur["start"] is not None:
                    raise RoutingError(f"{label}: pads {item} need a centre-line waypoint before them")
                else:
                    cur["start"], start_pads = ends, tuple(item)
                continue
            token = self._token(item)
            if token is None:
                if isinstance(item, str):
                    raise RoutingError(
                        f"{label}: {item!r} is not a pair waypoint; use (x, y) centre-line points "
                        "and a (P pad, N pad) tuple at the ends"
                    )
                point = (float(item[0]), float(item[1]))
                if cur["pts"]:
                    cur["bends"].append(bend)
                else:
                    cur["start_bend"] = bend
                if mark:
                    cur["marks"].append(self._mark(label, *mark, cur["pts"][-1], point))
                    mark = None
                cur["pts"].append(point)
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
        if mark:
            raise RoutingError(f"{label}: {mark[0]!r} goes between the two waypoints of the hop it marks")
        sections.append(cur)

        # The geometry of each section: its centre-line, where its traces
        # start and end, which side P is on.
        frames: list[dict] = []
        side = {None: 0, "left": 1, "right": -1}[p]
        arrived = None  # (via point, heading into it, (P via, N via))
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
            frames.append({"layer": on, "width": track_width, "pitch": pitch, "centre": centre, "side": side,
                           "ends": (start, start_bend, end, end_bend), "vias": vias, "via_to": sec["via_to"],
                           "marks": sec["marks"]})

        def tracks(frame: dict, centre: list) -> tuple[list, list]:
            try:
                return pair_tracks(centre, frame["pitch"], frame["side"], *frame["ends"])
            except ValueError as e:
                raise RoutingError(f"{label}: {e}") from None

        traces = [tracks(frame, frame["centre"]) for frame in frames]
        if skew is not None or length is not None or any(frame["marks"] for frame in frames):
            traces = self._tune_pair(label, (p_net, n_net), frames, traces, tracks, via_size[0], start_pads,
                                     skew, length)
        for frame, (p_pts, n_pts) in zip(frames, traces):
            self._segments(p_net, p_pts, frame["layer"], frame["width"])
            self._segments(n_net, n_pts, frame["layer"], frame["width"])
            if frame["vias"]:
                for name, at in zip(names, frame["vias"]):
                    self.via(name, at, *via_size, between=(frame["layer"], frame["via_to"]))
        p_length, n_length = (self._measure(net)["total"] for net in (p_net, n_net))
        report = PairReport(names, p_length, n_length, p_length - n_length, len(sections) - 1,
                            (p_length + n_length) / 2)
        self.pairs.append(report)
        return report

    def _tune_pair(self, label: str, nets: tuple, frames: list[dict], traces: list[tuple], tracks,
                   via_diameter: float, start_pads: tuple[str, str] | None, skew: float | None,
                   length: float | None) -> list[tuple]:
        """The (P, N) polylines of a `pair` call, section by section, with
        its skew bumps and meanders in. `traces` are the plain ones and
        `tracks(frame, centre)` makes a section's from a centre-line."""
        names = [net.GetNetname() for net in nets]
        marks = [(i, mark) for i, frame in enumerate(frames) for mark in frame["marks"]]
        skews = [(i, mark) for i, mark in marks if mark["kind"] == "skew"]
        meanders = [(i, mark) for i, mark in marks if mark["kind"] == "meander"]
        if skews and skew is None:
            raise RoutingError(f"{label}: 'skew' needs the mismatch to allow: skew=<mm>")
        if meanders and length is None:
            raise RoutingError(f"{label}: 'meander' needs the length to reach: length=<mm>")

        def measure(traces: list[tuple]) -> list[float]:
            return [
                self._measure(net, [(frame["layer"], trace[k]) for frame, trace in zip(frames, traces)],
                              [(frame["vias"][k], via_diameter) for frame in frames if frame["vias"]])["total"]
                for k, net in enumerate(nets)
            ]

        def offset(frame: dict, k: int) -> float:
            """Where trace k (0: P, 1: N) runs, in mm to the left of the centre-line."""
            return (frame["side"] if k == 0 else -frame["side"]) * frame["pitch"] / 2

        before = measure(traces)
        # Skew: how much each "skew" hop puts on which trace.
        bumps: list[tuple] = []
        if skew is not None:
            mismatch = before[0] - before[1]
            if not skews and abs(mismatch) > skew:
                raise RoutingError(
                    f"{label}: skew {mismatch:+.3f}mm is over the {skew:g}mm allowed; put 'skew' before the "
                    "waypoint that ends the segment to compensate on"
                )
            lead = self._pad_extra(start_pads[0]) - self._pad_extra(start_pads[1]) if start_pads else 0.0
            ahead = [lead]  # P - N by the start of each section
            for p_pts, n_pts in traces:
                ahead.append(ahead[-1] + path_length(p_pts) - path_length(n_pts))
            done = 0.0
            for k, (i, mark) in enumerate(skews):
                if k == len(skews) - 1:
                    built_up = mismatch
                else:
                    upto = [self._spot(label, mark, traces[i][j], offset(frames[i], j))[0] + 2 for j in (0, 1)]
                    built_up = ahead[i] + path_length(traces[i][0][:upto[0]]) - path_length(traces[i][1][:upto[1]])
                if abs(built_up - done) > skew:
                    bumps.append((i, mark, 1 if built_up > done else 0, abs(built_up - done)))
                    done = built_up
        # Length: how much each "meander" hop adds to the centre-line.
        extras: list[float] = []
        slots = [(mark, frames[i]["pitch"] + frames[i]["width"]
                  + self.meander_spacing * (frames[i]["pitch"] - frames[i]["width"]),
                  *self._spot(label, mark, frames[i]["centre"])[1:]) for i, mark in meanders]
        bumped = (before[0] + before[1] + sum(extra for *_, extra in bumps)) / 2
        if length is not None:
            extras = self._lengthen(label, bumped, length, slots)

        centres = [frame["centre"] for frame in frames]
        said_length = []
        for (i, mark), (_, pitch, a, b), extra in zip(meanders, slots, extras):
            points, note = self._meander(label, mark, a, b, extra, pitch)
            if note:
                centres[i] = splice(centres[i], self._spot(label, mark, centres[i])[0], points)
                said_length.append(f"{note}, {frames[i]['layer']} {mark['a']}->{mark['b']}")
        traces = [list(tracks(frame, centre)) for frame, centre in zip(frames, centres)]
        said_skew = []
        for i, mark, k, extra in bumps:
            frame = frames[i]
            index, a, b = self._spot(label, mark, traces[i][k], offset(frame, k))
            try:
                points, count, height = skew_bumps(
                    a, b, extra, mark["amplitude"] or frame["pitch"] - frame["width"], 3 * frame["width"],
                    "left" if offset(frame, k) > 0 else "right",
                )
            except ValueError as e:
                raise RoutingError(f"{label}: {mark['text']!r} hop {mark['a']}->{mark['b']}: {e}") from None
            traces[i][k] = splice(traces[i][k], index, points)
            said_skew.append(f"{count} bump(s) of {height:.3f} on {names[k]}, {frame['layer']} {mark['a']}->{mark['b']}")

        after = measure(traces)
        title = " / ".join(names)
        if skew is not None:
            if abs(after[0] - after[1]) > max(skew, RESOLUTION):
                raise RoutingError(
                    f"{label}: the skew came out at {after[0] - after[1]:+.3f}mm, not within the {skew:g}mm allowed"
                )
            self.tuning.append(
                f"{title}: skew {before[0] - before[1]:+.3f} -> {after[0] - after[1]:+.3f} (allowed {skew:g}): "
                f"{'; '.join(said_skew) or 'nothing to add'}"
            )
        if length is not None:
            mean = (after[0] + after[1]) / 2
            if abs(mean - length) > 2 * RESOLUTION:
                raise RoutingError(f"{label}: the length came out at {mean:.3f}mm, not the {length:.3f}mm asked for")
            self.tuning.append(
                f"{title}: length {bumped:.3f} -> {mean:.3f}: {'; '.join(said_length) or 'nothing to add'}"
            )
        self.tuned.append({"label": title, "nets": tuple(names), "skew": skew, "length": length})
        return [tuple(trace) for trace in traces]

    # ----------------------------------------------------- length tuning

    def _spot(self, label: str, mark: dict, path: list, offset: float = 0.0) -> tuple:
        """Where a marked hop lies on a polyline that runs `offset` mm to
        the left of it: (segment index, the ends of the part it covers)."""
        nx, ny = left_normal(mark["a"], mark["b"])
        a, b = ((point[0] + offset * nx, point[1] + offset * ny) for point in (mark["a"], mark["b"]))
        try:
            return locate(path, a, b)
        except ValueError:
            raise RoutingError(
                f"{label}: nothing is left of the hop {mark['a']}->{mark['b']} marked {mark['kind']!r} to tune on"
            ) from None

    @staticmethod
    def _lengthen(label: str, have: float, target: float, slots: list[tuple]) -> list[float]:
        """What each "meander" hop has to add (mm) to bring `have` mm to
        `target`; they fill up in order and the last takes the rest.
        `slots` are (marker, leg pitch, the two ends of its segment)."""
        need = target - have
        if need < -RESOLUTION:
            raise RoutingError(f"{label}: {have:.3f}mm is already longer than the {target:.3f}mm asked for")
        if need <= RESOLUTION:
            return [0.0] * len(slots)
        if not slots:
            raise RoutingError(
                f"{label}: {have:.3f}mm is {need:.3f}mm short of the {target:.3f}mm asked for; put "
                "'meander:<left|right|both>:<mm>' before the waypoint that ends the segment to lengthen"
            )
        extras = []
        for k, (mark, pitch, a, b) in enumerate(slots):
            room = meander_capacity(math.dist(a, b), mark["amplitude"], pitch, mark["side"])
            extras.append(need if k == len(slots) - 1 else min(need, room))
            need -= extras[-1]
        return extras

    @staticmethod
    def _meander(label: str, mark: dict, a, b, extra: float, pitch: float) -> tuple[list, str]:
        """(the meander for a "meander" hop, what to say about it); no
        note when there is nothing to add."""
        if extra < 1e-9:
            return [a, b], ""
        try:
            points, count, height = meander(a, b, extra, mark["amplitude"], pitch, mark["side"])
        except ValueError as e:
            raise RoutingError(f"{label}: {mark['text']!r} hop {mark['a']}->{mark['b']}: {e}") from None
        return points, f"{count} meander(s) of {height:.3f} ({mark['side']})"

    def _pad_extra(self, spec: str) -> float:
        """The length behind a pad: declared (pad_lengths), else the pad's
        own pad-to-die length."""
        if spec in self._pad_lengths:
            return self._pad_lengths[spec]
        return pcbnew.ToMM(self.pad(spec).GetPadToDieLength())

    def _pad_entries(self, net: str) -> list[tuple]:
        """The pads of a net as routing_geometry.net_length takes them."""
        entries = []
        for spec in self._net_pads.get(net, ()):
            pad = self._pads[spec]
            box = pad.GetBoundingBox()
            on = None  # a through-hole pad: on every layer, and no via sits in it
            if pad.GetAttribute() in (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD_ATTRIB_CONN):
                on = "F.Cu" if pad.IsOnLayer(pcbnew.F_Cu) else "B.Cu"
            corners = tuple(pcbnew.ToMM(v) for v in (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom()))
            entries.append((corners, on, self._pad_extra(spec)))
        return entries

    def _measure(self, net: "pcbnew.NETINFO_ITEM", tracks=(), vias=()) -> dict:
        """The length of a net (routing_geometry.net_length) over the
        copper on the board and copper about to be created: `tracks` as
        (layer, polyline), `vias` as (centre, diameter)."""
        copper, holes = _copper(self.board, self.board.TracksInNet(net.GetNetCode()))
        for on, path in tracks:
            points = [_on_grid(point) for point in path]
            copper += [(on, a, b, math.dist(a, b)) for a, b in zip(points, points[1:])]
        holes += [(_on_grid(at), diameter / 2) for at, diameter in vias]
        return net_length(copper, holes, self._pad_entries(net.GetNetname()), self.depths)

    def pad_lengths(self, lengths: dict[str, float]) -> None:
        """Declare the length (mm) behind pads, `{"REF.PAD": mm}`: the lead
        or package trace between the pad and where the signal really starts
        or ends. It is added to the length of the pad's net, and wins over
        the pad-to-die length the pad carries on the board."""
        for spec, mm in lengths.items():
            self.pad(spec)
            if mm < 0:
                raise RoutingError(f"pad length of {spec} is negative: {mm}")
            self._pad_lengths[spec] = float(mm)

    def length(self, net: str) -> float:
        """Length (mm) of a net as routed so far: track, via heights and
        pad lengths (see the module docstring)."""
        return self._measure(self._net(net))["total"]

    def match(self, name: str, nets, tolerance: float, length: float | None = None) -> None:
        """Declare a match group: every member within `tolerance` mm of
        the longest member, or of `length` mm when given.

        `nets` is a net-name pattern (`*` / `?`, as for `--lengths`) or a
        list of patterns; a tuple of net names in the list is one member
        whose length is the sum of those nets (a link that runs through a
        series part). The group is measured on the finished board, when
        the run is over: it prints each member's length and error, and
        the run exits 1 if one is out of tolerance or has no copper.
        """
        every = None
        members: list[tuple[str, ...]] = []
        for item in [nets] if isinstance(nets, str) else nets:
            if isinstance(item, str):
                if every is None:
                    every = sorted(str(net) for net in self.board.GetNetsByName().keys() if str(net))
                found = [(net,) for net in every if _matches(net, item)]
                if not found:
                    raise RoutingError(f"match group {name}: no net matches {item!r}")
            else:
                found = [tuple(self._net(net).GetNetname() for net in item)]
            members += [member for member in found if member not in members]
        if not members:
            raise RoutingError(f"match group {name}: no nets")
        self.groups.append({"name": name, "members": members, "tolerance": tolerance, "length": length})

    def stale(self, lengths: dict[str, dict]) -> list[str]:
        """What the tuned calls achieved that no longer holds for these
        net lengths (copper added to a net after it was tuned)."""
        problems = []
        for tuned in self.tuned:
            totals = [lengths.get(net, {}).get("total", 0.0) for net in tuned["nets"]]
            mean = sum(totals) / len(totals)
            if tuned["length"] is not None and abs(mean - tuned["length"]) > 2 * RESOLUTION:
                problems.append(
                    f"{tuned['label']}: {mean:.3f}mm on the finished board, not the {tuned['length']:.3f}mm "
                    "it was tuned to"
                )
            if tuned["skew"] is not None and abs(totals[0] - totals[1]) > max(tuned["skew"], RESOLUTION):
                problems.append(
                    f"{tuned['label']}: skew {totals[0] - totals[1]:+.3f}mm on the finished board, over the "
                    f"{tuned['skew']:g}mm allowed"
                )
        return problems

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
        """Length of every routed net so far (see net_lengths)."""
        return net_lengths(self.board, self._pad_entries, self.depths)

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


def _on_grid(point) -> tuple[float, float]:
    """A point (mm) where pcbnew will put it: on the board's 1nm grid."""
    return (pcbnew.ToMM(pcbnew.FromMM(point[0])), pcbnew.ToMM(pcbnew.FromMM(point[1])))


def _copper(board: "pcbnew.BOARD", items) -> tuple[list, list]:
    """Board tracks, arcs and vias as routing_geometry.net_length takes
    them: ([(layer, start, end, length)], [(centre, pad radius)]), mm."""
    tracks, vias = [], []
    for item in items:
        if item.GetClass() == "PCB_VIA":
            at = item.GetPosition()
            # (BOARD.TracksInNet hands vias out as plain PCB_TRACK objects)
            pad = item.Cast().GetWidth(pcbnew.F_Cu)
            vias.append(((pcbnew.ToMM(at.x), pcbnew.ToMM(at.y)), pcbnew.ToMM(pad) / 2))
        else:
            start, end = item.GetStart(), item.GetEnd()
            tracks.append((board.GetLayerName(item.GetLayer()), (pcbnew.ToMM(start.x), pcbnew.ToMM(start.y)),
                           (pcbnew.ToMM(end.x), pcbnew.ToMM(end.y)), item.GetLength() / pcbnew.FromMM(1)))
    return tracks, vias


def net_lengths(board: "pcbnew.BOARD", pads=None, depths: dict[str, float] | None = None) -> dict[str, dict]:
    """The length of every net with copper on the board:

        {net: {"layers": {layer: mm}, "vias": n, "via_length": mm,
               "pad_length": mm, "total": mm}}

    Counts every track, arc and via on the board - hand-placed copper and
    package-layout groups as well as the routing group - because a link's
    length budget is about all of it. `pads(net)` gives the net's pads and
    `depths` the depth of each copper layer, both as
    routing_geometry.net_length takes them; without them pad lengths and
    via heights add nothing (Router.lengths passes both).
    """
    copper: dict[str, list] = {}
    for item in board.GetTracks():
        copper.setdefault(item.GetNetname(), []).append(item)
    return {
        net: net_length(*_copper(board, items), pads(net) if pads else (), depths)
        for net, items in copper.items()
    }


def _matches(net: str, pattern: str) -> bool:
    """Whether a full board net name matches a `*`/`?` pattern; a pattern
    without the leading "/" of a hierarchical net matches too."""
    return fnmatch.fnmatchcase(net, pattern) or fnmatch.fnmatchcase(net.lstrip("/"), pattern)


def _shares(entry: dict, layers: list[str]) -> str:
    """What a net's length is made of: track per layer, via heights, pad lengths."""
    parts = [f"{name} {entry['layers'][name]:.3f}" for name in layers if name in entry["layers"]]
    parts += [f"{what} {entry[key]:.3f}" for what, key in (("vias", "via_length"), ("pads", "pad_length"))
              if entry.get(key)]
    return ", ".join(parts)


def match_groups(groups: list[dict], lengths: dict[str, dict]) -> list[dict]:
    """The declared match groups (Router.match) measured against net
    lengths: per group its target (the given length, else the longest
    member), a row (label, nets, length, error) per member - both None for
    a member with a net that has no copper - and whether all are within
    tolerance."""
    reports = []
    for group in groups:
        sums = [
            sum(lengths[net]["total"] for net in nets) if all(net in lengths for net in nets) else None
            for nets in group["members"]
        ]
        target = group["length"] if group["length"] is not None else max((s for s in sums if s is not None), default=None)
        rows = [
            ("+".join(nets), nets, total, None if total is None else total - target)
            for nets, total in zip(group["members"], sums)
        ]
        ok = all(error is not None and abs(error) <= group["tolerance"] + 1e-9 for *_, error in rows)
        reports.append({**group, "target": target, "rows": rows, "ok": ok})
    return reports


def print_groups(reports: list[dict], lengths: dict[str, dict], layers: list[str]) -> None:
    """Each match group: its target and verdict, then a row per member
    with its length, its error and what the length is made of."""
    if not reports:
        return
    print("  match groups (mm; error = length - target):")
    for report in reports:
        chained = any(len(nets) > 1 for _, nets, _, _ in report["rows"])
        target = "none (nothing routed)" if report["target"] is None else f"{report['target']:.3f}"
        print(
            f"    {report['name']}: {len(report['rows'])} {'member' if chained else 'net'}(s), target {target}"
            f"{'' if report['length'] is not None or report['target'] is None else ' (the longest)'}, "
            f"tolerance {report['tolerance']:g}: {'ok' if report['ok'] else 'OUT OF TOLERANCE'}"
        )
        wide = max(len(label) for label, *_ in report["rows"])
        for label, nets, total, error in report["rows"]:
            if total is None:
                print(f"      {label:{wide}s}  not routed  <- out")
                continue
            made_of = {"layers": {}, "via_length": 0.0, "pad_length": 0.0}
            for net in nets:
                for name, mm in lengths[net]["layers"].items():
                    made_of["layers"][name] = made_of["layers"].get(name, 0.0) + mm
                made_of["via_length"] += lengths[net]["via_length"]
                made_of["pad_length"] += lengths[net]["pad_length"]
            out = "" if abs(error) <= report["tolerance"] + 1e-9 else "  <- out"
            print(f"      {label:{wide}s} {total:9.3f} {error:+8.3f}  {_shares(made_of, layers)}{out}")


def print_lengths(lengths: dict[str, dict], patterns: list[str], layers: list[str],
                  reports: list[dict] | None = None) -> None:
    """The per-net length table for the nets matching any pattern (full
    board net names, `*`/`?` wildcards; a pattern without the leading "/"
    of a hierarchical net matches too): total, via count, what the total
    is made of, and the net's error in each match group it is in."""
    rows = [
        (net, entry)
        for net, entry in sorted(lengths.items())
        if any(_matches(net, pat) for pat in patterns)
    ]
    groups: dict[str, list[str]] = {}
    for report in reports or ():
        for _, nets, _, error in report["rows"]:
            for net in nets:
                groups.setdefault(net, []).append(
                    f"[match {report['name']} {'not routed' if error is None else f'{error:+.3f}'}]"
                )
    print(f"  lengths (mm), {len(rows)} net(s):")
    wide = max([len(net) for net, _ in rows] + [3])
    print(f"    {'net':{wide}s} {'total':>9s} {'vias':>5s}  per layer")
    for net, entry in rows:
        row = f"    {net:{wide}s} {entry['total']:9.3f} {entry['vias']:5d}  {_shares(entry, layers)}"
        print("  ".join([row, *groups.get(net, [])]))


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
    router = Router(board, NetRules.load(project_dir, build), load_board_setup(project_dir, build).get("stackup"))
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
    if router.pairs or router.tuning or router.groups or length_patterns:
        print(f"  {router.length_note}")
    if router.pairs:
        print("  pairs (mm; skew = P - N):")
        for pair in router.pairs:
            print(
                f"    {' / '.join(pair.nets)}: P {pair.p_length:.3f}, N {pair.n_length:.3f}, "
                f"skew {pair.skew:+.3f}, {pair.via_pairs} via pair(s)"
            )
    if router.tuning:
        print("  tuning (mm):")
        for line in router.tuning:
            print(f"    {line}")
    # The board is measured as a whole only when something asks for it.
    problems: list[str] = []
    if router.tuned or router.groups or length_patterns:
        lengths = router.lengths()
        reports = match_groups(router.groups, lengths)
        print_groups(reports, lengths, router.layers)
        if length_patterns:
            print_lengths(lengths, length_patterns, router.layers, reports)
        problems = router.stale(lengths)
        problems += [f"match group {report['name']} is out of tolerance" for report in reports if not report["ok"]]
    for problem in problems:
        print(f"length error: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
