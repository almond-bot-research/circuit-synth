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

Preflight gates the export: DRC errors or unconnected items abort, and
the assembler's paste conventions are checked (no paste apertures on
pure-THT parts, paste on the THT pads of mixed SMT+THT parts) along
with fiducial count and MPN coverage. Those report as warnings so a
deliberate exception doesn't block an order.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pcbnew

from kicadpcb import find_kicad_cli  # noqa: E402


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
