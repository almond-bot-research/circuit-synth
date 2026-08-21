"""Generate the assembler's quote package from a board (cs pcb fab).

Writes <layout_dir>/fab/ in the layout the assembly house asks for
(see the design repo's AGENTS.md):

- gerbers/: X2 gerbers for every copper layer plus mask, paste,
  silkscreen, and edge cuts, and the Excellon drill with a gerber map
- <build>-pos.csv: placement for every footprint, fiducials and THT
  parts included
- <build>.xml: IPC-2581 with Manufacturer/MPN/DigiKey BOM columns read
  off the footprint fields (stamped by `cs pcb sync` from parts.py)
- <build>.bom.csv: copied from the layout dir (`cs bom export` runs it)
- <build>.kicad_pcb/.kicad_pro/.kicad_dru: the ECAD source, listed as
  "optional but helpful" on the assembler's constraints page

Preflight gates the export: DRC errors or unconnected items abort, and
so does copper spacing below the fab's gerber-measured minimum
(fab_rules.MIN_COPPER_SPACING_MM). That check is net-blind - it merges
each copper layer's tracks, pads, vias, and zone fills into islands the
way the fab sees the gerbers, then measures gaps between islands -
because KiCad's own clearance engine skips same-net pairs, which is
exactly where sub-3.5mil slivers (parallel same-net tracks, tight via
stitching) hide. The assembler's paste conventions are also checked (no
paste apertures on pure-THT parts, paste on the THT pads of mixed
SMT+THT parts) along with fiducial count and MPN coverage. Those report
as warnings so a deliberate exception doesn't block an order.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pcbnew

from fab_rules import MIN_COPPER_SPACING_MM  # noqa: E402
from kicadpcb import find_kicad_cli  # noqa: E402


def _segment_distance(a1, a2, b1, b2) -> float:
    def point_to_segment(p, s1, s2) -> float:
        vx, vy = s2[0] - s1[0], s2[1] - s1[1]
        wx, wy = p[0] - s1[0], p[1] - s1[1]
        length_sq = vx * vx + vy * vy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
        dx, dy = p[0] - (s1[0] + t * vx), p[1] - (s1[1] + t * vy)
        return (dx * dx + dy * dy) ** 0.5

    return min(
        point_to_segment(a1, b1, b2),
        point_to_segment(a2, b1, b2),
        point_to_segment(b1, a1, a2),
        point_to_segment(b2, a1, a2),
    )


def check_copper_spacing(board) -> list[str]:
    """Net-blind spacing check: merge each copper layer into islands (the
    fab's view of the gerbers) and report island-to-island gaps below
    MIN_COPPER_SPACING_MM. Returns violation strings."""
    to_mm = pcbnew.ToMM
    max_error = pcbnew.FromMM(0.001)
    grow = pcbnew.FromMM(MIN_COPPER_SPACING_MM + 0.01)
    violations: list[str] = []

    for layer in board.GetEnabledLayers().CuStack():
        merged = pcbnew.SHAPE_POLY_SET()
        for track in board.GetTracks():
            if track.IsOnLayer(layer):
                track.TransformShapeToPolygon(merged, layer, 0, max_error, pcbnew.ERROR_INSIDE)
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.IsOnLayer(layer):
                    pad.TransformShapeToPolygon(merged, layer, 0, max_error, pcbnew.ERROR_INSIDE)
        for zone in board.Zones():
            if zone.GetIsRuleArea():
                continue
            if zone.IsOnLayer(layer) and zone.IsFilled():
                fill = zone.GetFilledPolysList(layer)
                for i in range(fill.OutlineCount()):
                    merged.AddOutline(fill.Outline(i))
                    for h in range(fill.HoleCount(i)):
                        merged.AddHole(fill.Hole(i, h), merged.OutlineCount() - 1)
        merged.Simplify()

        # Segments of each island's boundary (outline plus any holes, so a
        # feature sitting inside a pour knockout is measured too).
        islands = []
        for i in range(merged.OutlineCount()):
            chains = [merged.Outline(i)]
            chains += [merged.Hole(i, h) for h in range(merged.HoleCount(i))]
            segments = []
            for chain in chains:
                pts = [
                    (to_mm(chain.CPoint(k).x), to_mm(chain.CPoint(k).y))
                    for k in range(chain.PointCount())
                ]
                segments += [(pts[k], pts[(k + 1) % len(pts)]) for k in range(len(pts))]
            islands.append((segments, merged.Outline(i).BBox()))

        layer_name = board.GetLayerName(layer)
        for i in range(len(islands)):
            for j in range(i + 1, len(islands)):
                box_i = pcbnew.BOX2I(islands[i][1].GetPosition(), islands[i][1].GetSize())
                box_i.Inflate(grow)
                if not box_i.Intersects(islands[j][1]):
                    continue

                # Only compare boundary segments near the other island.
                def near(segments, bbox):
                    box = pcbnew.BOX2I(bbox.GetPosition(), bbox.GetSize())
                    box.Inflate(grow)
                    x0, y0 = to_mm(box.GetLeft()), to_mm(box.GetTop())
                    x1, y1 = to_mm(box.GetRight()), to_mm(box.GetBottom())
                    return [
                        (a, b)
                        for a, b in segments
                        if max(min(a[0], b[0]), x0) <= min(max(a[0], b[0]), x1)
                        and max(min(a[1], b[1]), y0) <= min(max(a[1], b[1]), y1)
                    ]

                best, best_at = 1e9, None
                for a1, a2 in near(islands[i][0], islands[j][1]):
                    for b1, b2 in near(islands[j][0], islands[i][1]):
                        d = _segment_distance(a1, a2, b1, b2)
                        if d < best:
                            best = d
                            best_at = ((a1[0] + b1[0]) / 2, (a1[1] + b1[1]) / 2)
                if best < MIN_COPPER_SPACING_MM:
                    violations.append(
                        f"{layer_name}: {best:.4f}mm copper gap at "
                        f"({best_at[0]:.3f}, {best_at[1]:.3f})"
                    )
    return violations


def run_cli(*args: str) -> None:
    result = subprocess.run([find_kicad_cli(), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"kicad-cli {' '.join(args[:3])} failed:\n{result.stderr.strip()}")


def preflight(board, board_path: Path) -> list[str]:
    """Return warnings; raise SystemExit on hard failures (DRC errors)."""
    warnings: list[str] = []

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report = Path(tmp.name)
    run_cli(
        "pcb", "drc", str(board_path),
        "-o", str(report), "--format", "json", "--severity-error",
    )
    drc = json.loads(report.read_text())
    report.unlink()
    errors = drc.get("violations", [])
    unconnected = drc.get("unconnected_items", [])
    if errors or unconnected:
        for v in errors[:10]:
            print(f"  DRC: {v.get('type')}: {v.get('description', '')[:100]}", file=sys.stderr)
        raise SystemExit(
            f"aborting: DRC reports {len(errors)} error(s) and "
            f"{len(unconnected)} unconnected item(s); fix them or ship a board you trust"
        )

    spacing = check_copper_spacing(board)
    if spacing:
        for v in spacing[:20]:
            print(f"  spacing: {v}", file=sys.stderr)
        raise SystemExit(
            f"aborting: {len(spacing)} copper gap(s) below the fab's "
            f"{MIN_COPPER_SPACING_MM}mm minimum spacing (measured net-blind on "
            f"final copper, the way the fab reads the gerbers)"
        )

    fiducials = 0
    for fp in board.GetFootprints():
        name = fp.GetFPID().GetLibItemName().wx_str().lower()
        if "fiducial" in name:
            fiducials += 1
            continue

        pads = list(fp.Pads())
        smd = [p for p in pads if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
        tht = [p for p in pads if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH]

        def has_paste(pad) -> bool:
            return pad.IsOnLayer(pcbnew.F_Paste) or pad.IsOnLayer(pcbnew.B_Paste)

        # Assembler stencil conventions: pure-THT parts get no apertures,
        # THT pads of mixed parts (e.g. USB shield legs) keep theirs.
        if tht and not smd and any(has_paste(p) for p in tht):
            warnings.append(f"{fp.GetReference()}: pure-THT part has paste apertures")
        if tht and smd and not all(has_paste(p) for p in tht):
            warnings.append(f"{fp.GetReference()}: mixed SMT+THT part missing paste on THT pads")

        field = fp.GetFieldByName("Partnumber")
        if pads and (field is None or not field.GetText()):
            warnings.append(f"{fp.GetReference()}: no Partnumber (fine for TPs/mounting holes)")

    if fiducials < 3:
        warnings.append(f"only {fiducials} fiducials; the assembler wants at least 3")
    return warnings


def export(project_dir: Path, build_name: str, zip_path: str) -> int:
    board_path = project_dir / f"{build_name}.kicad_pcb"
    if not board_path.exists():
        raise SystemExit(f"{board_path} does not exist")
    board = pcbnew.LoadBoard(str(board_path))

    warnings = preflight(board, board_path)
    for w in warnings:
        print(f"  warn: {w}")

    fab = project_dir / "fab"
    gerbers = fab / "gerbers"
    gerbers.mkdir(parents=True, exist_ok=True)

    copper = ["F.Cu"]
    copper += [f"In{i}.Cu" for i in range(1, board.GetCopperLayerCount() - 1)]
    copper += ["B.Cu"]
    layers = copper + [
        "F.Mask", "B.Mask", "F.Paste", "B.Paste",
        "F.Silkscreen", "B.Silkscreen", "Edge.Cuts",
    ]
    run_cli("pcb", "export", "gerbers", str(board_path),
            "-o", f"{gerbers}/", "--layers", ",".join(layers))
    run_cli("pcb", "export", "drill", str(board_path),
            "-o", f"{gerbers}/", "--generate-map", "--map-format", "gerberx2")
    run_cli("pcb", "export", "pos", str(board_path),
            "-o", str(fab / f"{build_name}-pos.csv"), "--format", "csv", "--units", "mm")
    run_cli("pcb", "export", "ipc2581", str(board_path),
            "-o", str(fab / f"{build_name}.xml"),
            "--bom-col-mfg-pn", "Partnumber", "--bom-col-mfg", "Manufacturer",
            "--bom-col-dist-pn", "DigiKey", "--bom-col-dist", "DigiKey")

    bom = project_dir / f"{build_name}.bom.csv"
    if bom.exists():
        (fab / bom.name).write_bytes(bom.read_bytes())
    else:
        print(f"  warn: {bom.name} not found; run `cs bom export` and re-run")

    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        src = project_dir / f"{build_name}{ext}"
        if src.exists():
            (fab / src.name).write_bytes(src.read_bytes())

    files = sorted(p for p in fab.rglob("*") if p.is_file())
    print(f"[{build_name}] fab package: {len(files)} files -> {fab}")

    if zip_path:
        out = Path(zip_path).expanduser()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, Path("fab") / p.relative_to(fab))
        print(f"  zipped -> {out}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    zip_out = ""
    if "--zip" in args:
        i = args.index("--zip")
        zip_out = args[i + 1]
        del args[i : i + 2]
    if len(args) != 2:
        raise SystemExit("usage: fab_export.py <project_dir> <build_name> [--zip PATH]")
    sys.exit(export(Path(args[0]).resolve(), args[1], zip_out))
