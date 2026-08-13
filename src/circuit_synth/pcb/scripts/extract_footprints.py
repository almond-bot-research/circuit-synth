"""Extract footprints from a board into part library directories.

Lifts footprints for parts a repo does not already carry out of an existing
KiCad board (e.g. a released design being ported), one part directory per
footprint.

Usage:
  cs pcb extract-footprints <board.kicad_pcb> <ref>=<out_dir>:<fp_name> [...]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pcbnew


def extract(board_path: Path, specs: list[str]) -> None:
    board = pcbnew.LoadBoard(str(board_path))
    by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}

    for spec in specs:
        ref, _, dest = spec.partition("=")
        out_dir, _, fp_name = dest.rpartition(":")
        src = by_ref.get(ref)
        if src is None:
            raise SystemExit(f"reference {ref!r} not on board")

        dup = pcbnew.FOOTPRINT(src)
        dup.SetPosition(pcbnew.VECTOR2I(0, 0))
        dup.SetOrientationDegrees(0)
        dup.Reference().SetText("REF**")
        for pad in dup.Pads():
            pad.SetNetCode(0)
        dup.SetFPID(pcbnew.LIB_ID(Path(out_dir).name, fp_name))

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        io = pcbnew.PCB_IO_MGR.PluginFind(pcbnew.PCB_IO_MGR.KICAD_SEXP)
        io.FootprintSave(str(out), dup)
        pads = sorted({str(p.GetNumber()) for p in dup.Pads()})
        print(f"{ref} -> {out}/{fp_name}.kicad_mod  pads={pads}")


if __name__ == "__main__":
    extract(Path(sys.argv[1]).resolve(), sys.argv[2:])
