"""Seed a design board with the published package layouts.

For each subcircuit instance whose package publishes a layout (a standalone
build under packages/<pkg>/layouts/<build>/), copy the relative placement of
the package's components onto the design's matching components (matched by
address suffix) along with the package board's routing (tracks, vias, zones),
drop the block at a staging spot, and group it so it can be dragged into
place as one unit. This is the "package layouts merged into the main board"
starting point; the design layout is then refined from there.

Routing nets are remapped through the matched footprints' pads: for every
source pad, the source net maps to the net the design's netlist put on the
same pad, so copied copper lands on the design's nets. Copper on nets that
no matched pad touches is skipped with a warning.

Re-running a mapping reuses the instance's group, deletes the routing copied
by earlier runs, and re-stages the block.

Usage:
  cs pcb apply-layouts <project_dir> <build_name> \
      <instance>=<package_layout_dir>:<source_prefix> [...]

Example:
  cs pcb apply-layouts designs/mantis/layouts/default default \
      mcu=packages/mcu/layouts/rp2354a:main
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pcbnew  # noqa: E402

from kicadpcb import load_addresses  # noqa: E402


def load_source(layout_dir: Path, source_prefix: str):
    """Load the package board; return (board, {address_suffix: footprint})."""
    boards = list(layout_dir.glob("*.kicad_pcb"))
    if not boards:
        raise SystemExit(f"no .kicad_pcb in {layout_dir}")
    board = pcbnew.LoadBoard(str(boards[0]))
    src_addresses = load_addresses(layout_dir, boards[0].stem)  # ref -> address

    fps = {}
    for fp in board.GetFootprints():
        field = fp.GetFieldByName("address")
        addr = field.GetText() if field else src_addresses.get(fp.GetReference())
        if not addr:
            continue
        if source_prefix:
            if not addr.startswith(source_prefix + "."):
                continue
            addr = addr[len(source_prefix) + 1 :]
        fps[addr] = fp
    if not fps:
        raise SystemExit(f"no addressed footprints found in {boards[0]}")
    return board, fps


def pad_net_map(src_fps, target_fps):
    """source netcode -> target netcode, derived from matching pad numbers.

    Both boards' pad nets come from netlists generated off the same Python
    subcircuit, so each source net maps to exactly one design net; a conflict
    means one of the boards is out of sync with its schematic.
    """
    net_map = {0: 0}
    conflicts = set()
    for suffix, sfp in src_fps.items():
        tfp = target_fps.get(suffix)
        if tfp is None:
            continue
        for spad in sfp.Pads():
            number = spad.GetNumber()
            if not number:
                continue
            tpad = tfp.FindPadByNumber(number)
            if tpad is None:
                continue
            code, tcode = spad.GetNetCode(), tpad.GetNetCode()
            if code in net_map and net_map[code] != tcode:
                conflicts.add(spad.GetNetname())
                continue
            net_map[code] = tcode
    return net_map, conflicts


def clear_group_copper(board, instance: str) -> int:
    """Delete tracks/vias/zones copied into the instance's group by earlier runs."""
    removed = 0
    for item in list(board.GetTracks()) + list(board.Zones()):
        group = item.GetParentGroup()
        if group is not None and group.GetName() == instance:
            group.RemoveItem(item)
            board.Delete(item)
            removed += 1
    return removed


def copy_copper(board, group, src_board, net_map, move) -> tuple[int, set]:
    """Copy the source board's tracks, vias, and zones into the group."""
    copied, skipped = 0, set()
    for item in list(src_board.GetTracks()) + list(src_board.Zones()):
        target_code = net_map.get(item.GetNetCode())
        if target_code is None:
            skipped.add(item.GetNetname())
            continue
        dup = item.Duplicate().Cast()
        board.Add(dup)
        dup.Move(move)
        dup.SetNetCode(target_code)
        group.AddItem(dup)
        copied += 1
    return copied, skipped


def apply(project_dir: Path, build_name: str, mappings: list[str], force: bool) -> None:
    pcb_path = project_dir / f"{build_name}.kicad_pcb"
    board = pcbnew.LoadBoard(str(pcb_path))
    addresses = load_addresses(project_dir, build_name)  # ref -> address
    by_address = {}
    for fp in board.GetFootprints():
        field = fp.GetFieldByName("address")
        addr = field.GetText() if field else addresses.get(fp.GetReference())
        if addr:
            by_address[addr] = fp

    bbox = board.GetBoardEdgesBoundingBox()
    staging_y = pcbnew.ToMM(bbox.GetBottom()) + 25 if bbox.GetWidth() > 0 else 100.0
    staging_x = pcbnew.ToMM(bbox.GetLeft()) if bbox.GetWidth() > 0 else 100.0

    for i, mapping in enumerate(mappings):
        instance, _, source = mapping.partition("=")
        source_dir, _, source_prefix = source.partition(":")
        src_board, src_fps = load_source(Path(source_dir).resolve(), source_prefix)

        ax = sum(pcbnew.ToMM(fp.GetPosition().x) for fp in src_fps.values()) / len(src_fps)
        ay = sum(pcbnew.ToMM(fp.GetPosition().y) for fp in src_fps.values()) / len(src_fps)

        group = next((g for g in board.Groups() if g.GetName() == instance), None)
        if group is None:
            group = pcbnew.PCB_GROUP(board)
            group.SetName(instance)
            board.Add(group)
        replaced = clear_group_copper(board, instance)

        cx, cy = staging_x + 20 + i * 40, staging_y
        moved, missing, targets = 0, [], {}
        for suffix, sfp in sorted(src_fps.items()):
            addr = f"{instance}.{suffix}" if instance else suffix
            fp = by_address.get(addr)
            if fp is None:
                missing.append(addr)
                continue
            if fp.IsFlipped() != sfp.IsFlipped():
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
            pos = sfp.GetPosition()
            fp.SetPosition(pcbnew.VECTOR2I_MM(cx + pcbnew.ToMM(pos.x) - ax, cy + pcbnew.ToMM(pos.y) - ay))
            fp.SetOrientationDegrees(sfp.GetOrientation().AsDegrees())
            parent = fp.GetParentGroup()
            if parent is not None and parent.GetName() != instance:
                parent.RemoveItem(fp)
            group.AddItem(fp)
            targets[suffix] = fp
            moved += 1

        net_map, conflicts = pad_net_map(src_fps, targets)
        copied, skipped = copy_copper(
            board, group, src_board, net_map, pcbnew.VECTOR2I_MM(cx - ax, cy - ay)
        )

        note = f", replaced {replaced} routed items" if replaced else ""
        print(
            f"{instance}: placed {moved} components, copied {copied} routed items "
            f"as group at ({cx:.0f}, {cy:.0f}){note}"
        )
        if missing:
            print(f"  no match for: {', '.join(missing)}")
        if conflicts:
            print(f"  net map conflicts (kept first mapping): {', '.join(sorted(conflicts))}")
        if skipped:
            print(f"  skipped routing on unmapped nets: {', '.join(sorted(skipped))}")

    pcbnew.SaveBoard(str(pcb_path), board)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    apply(Path(args[0]).resolve(), args[1], args[2:], "--force" in sys.argv)
