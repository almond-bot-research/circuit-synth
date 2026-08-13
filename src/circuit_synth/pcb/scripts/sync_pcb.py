"""Create or update a build's .kicad_pcb from its generated schematic.

The Python source is the source of truth: footprints are added for new
components (at a staging grid below the board), removed for deleted ones,
swapped when the footprint id changed, and every pad gets its net from the
schematic netlist. Existing placement, tracks, and zones are preserved.

Each footprint also gets the part's fields (address, Manufacturer,
Partnumber, distributor numbers) copied from Python, because the board - not
the schematic - is what the IPC-2581 fab export reads them from. Footprint
3D model paths are normalized to stable references (models live next to
their footprints; see kicadpcb.normalize_models).

Usage:
  cs pcb sync <project_dir> <build_name>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pcbnew  # noqa: E402

from kicadpcb import (  # noqa: E402
    assign_nets,
    export_netlist,
    fp_lib_paths,
    load_fields,
    normalize_models,
    place_staging,
    set_field,
)


def load_footprint(libs: dict[str, Path], fpid: str):
    lib, name = fpid.split(":", 1)
    path = libs.get(lib)
    if path is None:
        raise SystemExit(f"footprint lib {lib!r} not in fp-lib-table")
    fp = pcbnew.FootprintLoad(str(path), name)
    if fp is None:
        raise SystemExit(f"footprint {name!r} not found in {path}")
    # FootprintLoad leaves the lib nickname off the FPID; set it so the board
    # matches the netlist id and later syncs are no-ops
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp


def copy_text_style(old, new) -> None:
    """Carry a text item's placement and styling from old footprint to new."""
    new.SetFPRelativePosition(old.GetFPRelativePosition())
    new.SetTextAngle(old.GetTextAngle())
    new.SetLayer(old.GetLayer())
    new.SetVisible(old.IsVisible())
    new.SetAttributes(old.GetAttributes())


def sync(project_dir: Path, build_name: str) -> None:
    pcb_path = project_dir / f"{build_name}.kicad_pcb"
    netlist = export_netlist(project_dir, build_name)
    fields = load_fields(project_dir, build_name)
    libs = fp_lib_paths(project_dir)

    if pcb_path.exists():
        board = pcbnew.LoadBoard(str(pcb_path))
    else:
        board = pcbnew.CreateEmptyBoard()

    existing = {fp.GetReference(): fp for fp in board.GetFootprints()}

    removed = [ref for ref in existing if ref not in netlist.comps]
    for ref in removed:
        board.Delete(existing.pop(ref))

    added, swapped = [], []
    staging_index = 0
    for ref, fpid in sorted(netlist.comps.items()):
        fp = existing.get(ref)
        if fp is not None and fp.GetFPID().GetUniStringLibId() != fpid:
            new_fp = load_footprint(libs, fpid)
            new_fp.SetReference(ref)
            new_fp.SetValue(fp.GetValue())
            new_fp.SetPosition(fp.GetPosition())
            new_fp.SetOrientation(fp.GetOrientation())
            if fp.IsFlipped():
                new_fp.Flip(new_fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            # Preserve the designer's text tweaks (moved/hidden silk refs)
            # across the swap; everything else comes from the new footprint.
            for field_name in ("Reference", "Value"):
                old_field = fp.GetFieldByName(field_name)
                new_field = new_fp.GetFieldByName(field_name)
                if old_field is not None and new_field is not None:
                    copy_text_style(old_field, new_field)
            old_texts: dict[str, list] = {}
            for item in fp.GraphicalItems():
                if isinstance(item, pcbnew.PCB_TEXT):
                    old_texts.setdefault(item.GetText(), []).append(item)
            for item in list(new_fp.GraphicalItems()):
                if not isinstance(item, pcbnew.PCB_TEXT):
                    continue
                # Consume old texts one-for-one: pcbnew normalizes "%R" to
                # "${REFERENCE}" on load, so a footprint can carry several
                # items with the same text.
                remaining = old_texts.get(item.GetText())
                if remaining:
                    copy_text_style(remaining.pop(0), item)
                else:
                    # Designer had deleted it from the board. Blank instead of
                    # Remove(): removal corrupts pcbnew's SWIG iteration state
                    # for subsequently loaded footprints, and hide flags don't
                    # serialize for non-field texts. Empty text plots nothing.
                    item.SetText("")
                    item.SetVisible(False)
            board.Delete(fp)
            board.Add(new_fp)
            fp = new_fp
            existing[ref] = fp
            swapped.append(ref)
        elif fp is None:
            fp = load_footprint(libs, fpid)
            fp.SetReference(ref)
            board.Add(fp)
            place_staging(fp, staging_index)
            staging_index += 1
            existing[ref] = fp
            added.append(ref)

        comp_fields = fields.get(ref, {})
        addr = comp_fields.get("address")
        if addr:
            # The old set_field id bug stored addresses in the mandatory
            # Description field; blank those artifacts (mandatory fields
            # cannot be removed, so RemoveField would be a silent no-op).
            desc = fp.GetFieldByName("Description")
            if desc is not None and desc.GetText() == addr:
                desc.SetText("")
        for name, value in comp_fields.items():
            set_field(fp, name, value)
        if netlist.paths.get(ref):
            fp.SetPath(pcbnew.KIID_PATH(netlist.paths[ref]))

    assign_nets(board, netlist)
    remodeled = normalize_models(board, project_dir, libs)
    pcbnew.SaveBoard(str(pcb_path), board)
    print(f"[{build_name}] pcb sync: +{len(added)} -{len(removed)} swapped {len(swapped)} -> {pcb_path}")
    if added:
        print(f"  added (staged below board): {', '.join(added)}")
    if swapped:
        print(f"  footprint swapped: {', '.join(swapped)}")
    if remodeled:
        print(f"  3d model paths rewritten: {remodeled}")


if __name__ == "__main__":
    sync(Path(sys.argv[1]).resolve(), sys.argv[2])
