"""Install a 3D model into an existing part directory (cs parts model).

`cs parts import` covers parts that LCSC stocks, but EasyEDA has no CAD
data for many Western-catalog parts (TI, WAGO, Molex, Panasonic, ...).
Those manufacturers publish STEP files on their own sites; this command
drops such a file into the part dir and stamps the footprint's (model ...)
block so the model propagates to boards on the next `cs pcb sync`.

Manufacturer STEP files carry no alignment relative to our footprint, so
the block defaults to zero offset/rotation. Verify with --preview (renders
the footprint+model from above with kicad-cli) and iterate with --rotate /
--offset; re-running replaces the model block in place.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

MODEL_EXTS = {".step", ".stp", ".wrl"}


def _fetch(source: str, tmp: Path) -> list[Path]:
    """Materialize SOURCE (local file/dir, URL, or zip of either) as model files."""
    if re.match(r"https?://", source):
        name = Path(source.split("?")[0]).name or "model"
        dest = tmp / name
        req = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        source_path = dest
    else:
        source_path = Path(source)
        if not source_path.exists():
            raise SystemExit(f"{source} does not exist")

    if source_path.is_dir():
        files = [p for p in source_path.iterdir() if p.suffix.lower() in MODEL_EXTS]
    elif zipfile.is_zipfile(source_path):
        extracted = tmp / "unzipped"
        with zipfile.ZipFile(source_path) as z:
            z.extractall(extracted)
        files = [p for p in extracted.rglob("*") if p.suffix.lower() in MODEL_EXTS]
    else:
        files = [source_path]

    steps = [p for p in files if p.suffix.lower() in {".step", ".stp"}]
    if len(steps) != 1:
        raise SystemExit(
            f"expected exactly one STEP file in {source}, found "
            f"{[p.name for p in steps] or 'none'}"
        )
    # Keep a WRL only when it pairs with the STEP (same stem).
    return steps + [
        p for p in files if p.suffix.lower() == ".wrl" and p.stem == steps[0].stem
    ]


def _extract_model_block(text: str) -> str | None:
    i = text.find("(model ")
    if i < 0:
        return None
    depth, j = 0, i
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
        j += 1
    return None


_PREVIEW_BOARD_SCRIPT = """\
import sys
import pcbnew

lib, name, out = sys.argv[1], sys.argv[2], sys.argv[3]
board = pcbnew.CreateEmptyBoard()
fp = pcbnew.FootprintLoad(lib, name)
assert fp is not None, f"{name} not found in {lib}"
models = fp.Models()
for i in range(models.size()):
    f = models[i].m_Filename
    if "/" not in f:
        models[i].m_Filename = lib + "/" + f
fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(100), pcbnew.FromMM(100)))
fp.Reference().SetVisible(False)
fp.Value().SetVisible(False)
board.Add(fp)

# Frame the render on the part: a board outline just around the footprint.
box = fp.GetBoundingBox()
box.Inflate(pcbnew.FromMM(1.5))
rect = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECTANGLE)
rect.SetStart(pcbnew.VECTOR2I(box.GetLeft(), box.GetTop()))
rect.SetEnd(pcbnew.VECTOR2I(box.GetRight(), box.GetBottom()))
rect.SetLayer(pcbnew.Edge_Cuts)
rect.SetWidth(pcbnew.FromMM(0.1))
board.Add(rect)
pcbnew.SaveBoard(out, board)
"""


def _render_preview(mod_path: Path, out_png: Path, tmp: Path) -> None:
    from circuit_synth.pcb.scripts.kicadpcb import find_kicad_cli, find_kicad_python

    script = tmp / "preview_board.py"
    script.write_text(_PREVIEW_BOARD_SCRIPT)
    board_path = tmp / "preview.kicad_pcb"
    build = subprocess.run(
        [find_kicad_python(), str(script), str(mod_path.parent.resolve()),
         mod_path.stem, str(board_path)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        print(f"preview board failed: {build.stderr.strip()}", file=sys.stderr)
        return
    render = subprocess.run(
        [find_kicad_cli(), "pcb", "render", str(board_path), "-o", str(out_png),
         "--side", "top", "--width", "800", "--height", "600"],
        capture_output=True,
        text=True,
    )
    if render.returncode == 0:
        print(f"preview rendered to {out_png}")
    else:
        print(f"preview render failed: {render.stderr.strip()}", file=sys.stderr)


def install_model(
    part_dir: Path,
    source: str,
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0),
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
    preview: str = "",
) -> int:
    mods = sorted(part_dir.glob("*.kicad_mod"))
    if len(mods) != 1:
        raise SystemExit(
            f"{part_dir} must contain exactly one .kicad_mod, found {len(mods)}"
        )
    mod = mods[0]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        files = _fetch(source, tmp)
        for f in files:
            target = part_dir / f.name
            if f.resolve() != target.resolve():
                shutil.copy(f, target)
        step = next(p for p in files if p.suffix.lower() in {".step", ".stp"})
        # Reference the WRL when present (KiCad swaps to STEP for exports).
        ref = next((p.name for p in files if p.suffix.lower() == ".wrl"), step.name)

        block = (
            f'\t(model "{ref}"\n'
            f"\t\t(offset (xyz {offset[0]:g} {offset[1]:g} {offset[2]:g}))\n"
            f"\t\t(scale (xyz 1 1 1))\n"
            f"\t\t(rotate (xyz {rotate[0]:g} {rotate[1]:g} {rotate[2]:g}))\n"
            f"\t)\n"
        )
        text = mod.read_text()
        existing = _extract_model_block(text)
        if existing is not None:
            text = text.replace(existing + "\n", block, 1).replace(existing, block.rstrip("\n"), 1)
        else:
            k = text.rstrip().rfind(")")
            text = text.rstrip()[:k] + block + ")\n"
        mod.write_text(text)
        print(f"{mod.name}: model {ref} (offset {offset}, rotate {rotate})")

        if preview:
            _render_preview(mod, Path(preview), tmp)

    print("run `cs pcb sync` on affected layouts to propagate; check alignment in the 3D viewer")
    return 0
