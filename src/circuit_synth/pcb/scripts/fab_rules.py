"""Fab design rules stamped onto every board by `cs pcb sync`.

Boards are fabbed at JLCPCB; these are their published capabilities for
standard multilayer service (1oz outer copper), so DRC catches anything
the fab would reject without per-project setup:

- board setup constraints go into the project's .kicad_pro
  (board.design_settings.rules), overwriting stale values on every sync
- a .kicad_dru custom-rules file is created next to the board (only if
  one doesn't already exist, so per-board additions survive) to relax
  hole-to-hole spacing for same-net via stitching: JLCPCB's 0.5mm
  hole-to-hole minimum applies between different nets, while same-net
  vias can be packed to ~0.254mm
- newly created boards default to 4 copper layers (the repo convention:
  In1 solid GND, In2 power islands)
- copper pours are normalized to the repo convention on every sync:
  0.2mm clearance, 0.1mm minimum width, solid (non-thermal-relief) pad
  connections
"""

from __future__ import annotations

import json
from pathlib import Path

import pcbnew

# Keys and units match KiCad's board.design_settings.rules (mm).
JLCPCB_RULES = {
    "min_clearance": 0.1,
    "min_copper_edge_clearance": 0.3,
    "min_hole_clearance": 0.25,
    "min_hole_to_hole": 0.5,
    "min_microvia_diameter": 0.2,
    "min_microvia_drill": 0.1,
    "min_through_hole_diameter": 0.3,
    "min_track_width": 0.1,
    "min_via_annular_width": 0.075,
    "min_via_diameter": 0.45,
    "min_text_height": 0.8,
    "min_text_thickness": 0.08,
}

JLCPCB_DRU = """\
(version 1)

# JLCPCB's 0.5mm hole-to-hole minimum applies to holes on different nets.
# Same-net via stitching can be packed much tighter (JLC floor ~0.254mm),
# so relax the check for same-net pairs; the board-setup 0.5mm still
# applies between different nets.
(rule "same-net hole to hole"
  (condition "A.Net == B.Net")
  (constraint hole_to_hole (min 0.3mm)))
"""


def apply_fab_rules(project_dir: Path, build_name: str) -> list[str]:
    """Stamp the fab capabilities onto one build. Returns log messages."""
    notes: list[str] = []

    pro_path = project_dir / f"{build_name}.kicad_pro"
    if pro_path.exists():
        data = json.loads(pro_path.read_text())
        rules = (
            data.setdefault("board", {})
            .setdefault("design_settings", {})
            .setdefault("rules", {})
        )
        changed = sorted(k for k, v in JLCPCB_RULES.items() if rules.get(k) != v)
        if changed:
            rules.update(JLCPCB_RULES)
            pro_path.write_text(json.dumps(data, indent=2) + "\n")
            notes.append(f"fab constraints stamped into {pro_path.name}: {', '.join(changed)}")

    dru_path = project_dir / f"{build_name}.kicad_dru"
    if not dru_path.exists():
        dru_path.write_text(JLCPCB_DRU)
        notes.append(f"wrote {dru_path.name} (same-net hole-to-hole exception)")

    return notes


COPPER_LAYERS = 4
ZONE_CLEARANCE = pcbnew.FromMM(0.2)
ZONE_MIN_THICKNESS = pcbnew.FromMM(0.1)


def apply_board_fab_defaults(board, new_board: bool) -> list[str]:
    """Normalize board-level conventions. Returns log messages; the caller
    must refill zones and save if anything is reported."""
    notes: list[str] = []

    if new_board and board.GetCopperLayerCount() != COPPER_LAYERS:
        board.SetCopperLayerCount(COPPER_LAYERS)
        notes.append(f"new board set to {COPPER_LAYERS} copper layers")

    # Defaults for zones drawn later in pcbnew.
    settings = board.GetDesignSettings().GetDefaultZoneSettings()
    settings.m_ZoneClearance = ZONE_CLEARANCE
    settings.m_ZoneMinThickness = ZONE_MIN_THICKNESS
    settings.m_padConnection = pcbnew.ZONE_CONNECTION_FULL
    board.GetDesignSettings().SetDefaultZoneSettings(settings)

    fixed = 0
    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.IsTeardropArea():
            continue
        changed = False
        if zone.GetLocalClearance() != ZONE_CLEARANCE:
            zone.SetLocalClearance(ZONE_CLEARANCE)
            changed = True
        if zone.GetMinThickness() != ZONE_MIN_THICKNESS:
            zone.SetMinThickness(ZONE_MIN_THICKNESS)
            changed = True
        if zone.GetPadConnection() != pcbnew.ZONE_CONNECTION_FULL:
            zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
            changed = True
        fixed += changed
    if fixed:
        notes.append(
            f"{fixed} zone(s) normalized to 0.2mm clearance / 0.1mm min width / solid pad connections"
        )

    return notes
