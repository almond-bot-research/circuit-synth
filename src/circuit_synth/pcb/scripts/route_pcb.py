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
        r.stitch("GND", "C11.2", dx=0.9)          # pad -> short stub -> via

Waypoints are `"REF.PAD"` (pad centre; the pad's net must match),
`(x, y)` in board mm, or `r.at("REF.PAD", dx, dy)`. Consecutive waypoints
are joined with 45 degree doglegs ("sd" = straight then diagonal, the
default; put "ds" before a waypoint for diagonal then straight). `"via"`
drops a via at the current point and swaps F.Cu/B.Cu; a layer name
switches layer without one (e.g. when starting on a through-hole pad).

Everything the script creates goes into one board group (`routing`), and
that group is deleted and rebuilt on every run, so the routing file stays
the source of truth while hand-placed copper and package-layout groups
are left alone. Zones are refilled before saving.

Usage: route_pcb.py <project_dir> <build_name> [routing.py] [--no-fill]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pcbnew  # noqa: E402
from routing_geometry import expand_path, is_45, path_length  # noqa: E402

GROUP = "routing"
_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


class RoutingError(Exception):
    """A routing file asked for something inconsistent with the board."""


class Router:
    """Creates tracks, vias, zones and keepouts from explicit waypoints."""

    def __init__(self, board: "pcbnew.BOARD") -> None:
        self.board = board
        self.group = pcbnew.PCB_GROUP(board)
        self.group.SetName(GROUP)
        board.Add(self.group)
        self.width = 0.2
        self.via_size = (0.6, 0.3)
        self.stats = {"tracks": 0, "vias": 0, "zones": 0, "length": 0.0}
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

    # ---------------------------------------------------------- creation

    def _add(self, item) -> None:
        self.board.Add(item)
        self.group.AddItem(item)

    def track(self, net: str, points, layer: str = "F.Cu", width: float | None = None) -> None:
        """Exact polyline on one layer, no dogleg expansion."""
        n = self._net(net)
        pts = [self._point(p, n) for p in points]
        self._segments(n, pts, layer, width or self.width)

    def _segments(self, net, pts, layer: str, width: float) -> None:
        lid = self.board.GetLayerID(layer)
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

    def via(self, net: str, at, size: float | None = None, drill: float | None = None) -> None:
        """Through via at a point or pad spec."""
        n = self._net(net)
        x, y = self._point(at, n) if isinstance(at, str) else (float(at[0]), float(at[1]))
        v = pcbnew.PCB_VIA(self.board)
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(size or self.via_size[0]))
        v.SetDrill(pcbnew.FromMM(drill or self.via_size[1]))
        v.SetNet(n)
        self._add(v)
        self.stats["vias"] += 1

    def route(self, net: str, *items, layer: str = "F.Cu", width: float | None = None,
              via: tuple[float, float] | None = None) -> None:
        """Route through waypoints with 45 degree doglegs.

        See the module docstring for the waypoint forms and the "via",
        "sd"/"ds" and layer-name tokens.
        """
        n = self._net(net)
        w = width or self.width
        pts: list[tuple[float, float]] = []
        bends: list[str] = []
        bend = "sd"

        def flush() -> None:
            if len(pts) >= 2:
                self._segments(n, expand_path(pts, bends), layer, w)

        for item in items:
            if isinstance(item, str) and item in ("sd", "ds"):
                bend = item
            elif isinstance(item, str) and (item == "via" or item in _LAYERS):
                if not pts:
                    if item == "via":
                        raise RoutingError(f"{net}: 'via' before the first waypoint")
                    layer = item
                    continue
                flush()
                last = pts[-1]
                if item == "via":
                    self.via(net, last, *(via or ()))
                    layer = "B.Cu" if layer == "F.Cu" else "F.Cu"
                else:
                    layer = item
                pts, bends = [last], []
            else:
                if pts:
                    bends.append(bend)
                pts.append(self._point(item, n))
        flush()

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
            ls.AddLayer(self.board.GetLayerID(layer))
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
    if len(args) < 2:
        print(__doc__)
        return 2
    project_dir, build = Path(args[0]), args[1]
    routing = Path(args[2]) if len(args) > 2 else project_dir / "routing.py"
    pcb_path = project_dir / f"{build}.kicad_pcb"
    board = pcbnew.LoadBoard(str(pcb_path))
    removed = clear_previous(board)
    router = Router(board)
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
