"""Export a build's BOM CSV from its canonical circuit JSON.

One row per part group (same value + manufacturer part number), columns as
an assembly house expects them: reference designators, quantity, value,
package, manufacturer, MPN, then distributor numbers.

The source is `<build>.json`, which the build regenerates from the part
definitions in Python every run - not the schematic. circuit-synth preserves
the fields already on a symbol when it updates a schematic in place, so a
`.kicad_sch` can still carry the distributor number a part had two edits
ago; the JSON cannot. Sourcing data therefore never comes from the part
libraries either, whose imported symbols and footprints name whatever part
their geometry was drawn from.

Groups with no MPN (fiducials, silkscreen logos) are dropped: there is
nothing to buy for them.

Usage:
  cs bom export <project_dir> <build_name>
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

COLUMNS = ["Refs", "Qty", "Value", "Package", "Manufacturer", "MPN", "DigiKey", "LCSC"]


def ref_key(ref: str) -> tuple[str, int]:
    match = re.match(r"([A-Za-z]+)(\d+)", ref)
    return (match.group(1), int(match.group(2))) if match else (ref, 0)


def components(circuit: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}

    def walk(node: dict) -> None:
        out.update(node.get("components") or {})
        for sub in node.get("subcircuits") or []:
            walk(sub)

    walk(circuit)
    return out


def export(project_dir: Path, build_name: str) -> Path:
    circuit = json.loads((project_dir / f"{build_name}.json").read_text())

    groups: dict[tuple[str, str], dict] = {}
    dropped: list[str] = []
    for ref, comp in components(circuit).items():
        fields = comp.get("_extra_fields") or {}
        mpn = fields.get("Partnumber", "")
        if not mpn:
            dropped.append(ref)
            continue
        group = groups.setdefault(
            (comp.get("value", ""), mpn),
            {
                "refs": [],
                "Value": comp.get("value", ""),
                "Package": comp.get("footprint", "").split(":")[-1],
                "Manufacturer": fields.get("Manufacturer", ""),
                "MPN": mpn,
                "DigiKey": fields.get("DigiKey", ""),
                "LCSC": fields.get("LCSC", ""),
            },
        )
        group["refs"].append(ref)

    rows = []
    for group in sorted(groups.values(), key=lambda g: ref_key(min(g["refs"], key=ref_key))):
        refs = sorted(group["refs"], key=ref_key)
        rows.append(
            {
                "Refs": " ".join(refs),
                "Qty": len(refs),
                **{key: group[key] for key in COLUMNS[2:]},
            }
        )

    out = project_dir / f"{build_name}.bom.csv"
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[{build_name}] bom: {len(rows)} part numbers -> {out}")
    if dropped:
        print(f"  no MPN, omitted: {', '.join(sorted(dropped, key=ref_key))}")
    return out
