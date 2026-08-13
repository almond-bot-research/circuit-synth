"""Verify a generated schematic against a reference board's pad nets.

Compares net partitions: every pad of every mapped component must group with
the same set of pads in both. Net names are ignored (they usually differ
between the reference design and the regenerated one); single-pad nets are
ignored.

Usage:
  cs pcb compare <reference.kicad_pcb> <project_dir> <build_name> <ref_map.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pcbnew  # noqa: E402

from kicadpcb import export_netlist, load_addresses  # noqa: E402


def main() -> int:
    ref_board_path, project_dir, build_name, map_path = sys.argv[1:5]
    mapping = json.loads(Path(map_path).read_text())
    ref_to_addr = mapping["refs"]
    pad_swaps = mapping.get("pad_swaps", {})

    # reference board: (address, pad) -> net
    board = pcbnew.LoadBoard(ref_board_path)
    ref_nets: dict[str, set] = {}
    unmapped = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in ref_to_addr:
            if ref:
                unmapped.append(ref)
            continue
        addr = ref_to_addr[ref]
        swap = pad_swaps.get(ref, {})
        for pad in fp.Pads():
            num = str(pad.GetNumber())
            if not num or not pad.GetNetname():
                continue
            num = swap.get(num, num)
            ref_nets.setdefault(pad.GetNetname(), set()).add((addr, num))
    if unmapped:
        print(f"reference components not mapped: {sorted(set(unmapped))}")

    # our netlist: (address, pad) -> net
    project = Path(project_dir)
    netlist = export_netlist(project, build_name)
    addresses = load_addresses(project, build_name)  # ref -> address
    our_nets: dict[str, set] = {}
    for (ref, pad), net in netlist.pad_nets.items():
        addr = addresses.get(ref)
        if addr:
            our_nets.setdefault(net, set()).add((addr, pad))

    mapped_addrs = set(ref_to_addr.values())

    def partition(nets: dict[str, set]) -> dict[frozenset, str]:
        out = {}
        for name, pads in nets.items():
            pads = frozenset(p for p in pads if p[0] in mapped_addrs)
            if len(pads) >= 2:
                out[pads] = name
        return out

    ref_part = partition(ref_nets)
    our_part = partition(our_nets)

    issues = 0
    for pads in sorted(set(ref_part) - set(our_part), key=lambda s: sorted(s)):
        issues += 1
        print(f"net only in reference ({ref_part[pads]}): {sorted(pads)}")
    for pads in sorted(set(our_part) - set(ref_part), key=lambda s: sorted(s)):
        issues += 1
        print(f"net only in ours ({our_part[pads]}): {sorted(pads)}")

    matched = len(set(ref_part) & set(our_part))
    print(f"\n{len(mapped_addrs)} mapped components, {matched} multi-pad nets matched exactly")
    print("CONNECTIVITY MATCH" if issues == 0 else f"{issues} MISMATCHES")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
