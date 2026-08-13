"""Generate a minimal KiCad symbol for a part without a drawn symbol.

Box symbol with left/right pin rows; pin numbers must match the footprint
pads. Used when importing a part whose footprint exists (extracted from a
board or drawn) but which has no symbol source.

Usage:
  cs parts min-symbol --out <part_dir> --name TPS560430X3FDBVR --ref U \
      --value TPS560430X3FDBVR \
      --footprint "Texas_Instruments_TPS560430X3FDBVR:TPS560430X3FDBVR" \
      --left 1:CB --left 2:GND --left 3:FB --right 4:EN --right 5:VIN --right 6:SW
"""

from __future__ import annotations

from pathlib import Path


def symbol(name: str, ref: str, value: str, footprint: str, left: list, right: list) -> str:
    """left/right: [(number, pin_name)] top-to-bottom."""
    rows = max(len(left), len(right))
    height = max(rows * 2.54 + 2.54, 5.08)
    half_h = height / 2
    width = 12.7
    half_w = width / 2

    pins = []
    for i, (num, pin_name) in enumerate(left):
        y = half_h - 2.54 * (i + 1)
        pins.append(
            f'      (pin passive line (at {-half_w - 3.81:g} {y:g} 0) (length 3.81)\n'
            f'        (name "{pin_name}" (effects (font (size 1.27 1.27))))\n'
            f'        (number "{num}" (effects (font (size 1.27 1.27))))\n'
            f"      )"
        )
    for i, (num, pin_name) in enumerate(right):
        y = half_h - 2.54 * (i + 1)
        pins.append(
            f'      (pin passive line (at {half_w + 3.81:g} {y:g} 180) (length 3.81)\n'
            f'        (name "{pin_name}" (effects (font (size 1.27 1.27))))\n'
            f'        (number "{num}" (effects (font (size 1.27 1.27))))\n'
            f"      )"
        )
    pins_text = "\n".join(pins)

    return f"""(kicad_symbol_lib (version 20211014) (generator kicad_symbol_editor)
  (symbol "{name}" (pin_names (offset 0.254)) (in_bom yes) (on_board yes)
    (property "Reference" "{ref}" (id 0) (at 0 {half_h + 1.27:g} 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "{value}" (id 1) (at 0 {-half_h - 1.27:g} 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "{footprint}" (id 2) (at 0 0 0)
      (effects (font (size 1.27 1.27) italic) hide)
    )
    (property "Datasheet" "" (id 3) (at 0 0 0)
      (effects (font (size 1.27 1.27) italic) hide)
    )
    (symbol "{name}_1_1"
      (rectangle (start {-half_w:g} {half_h:g}) (end {half_w:g} {-half_h:g})
        (stroke (width 0.254) (type default) (color 0 0 0 0))
        (fill (type background))
      )
{pins_text}
    )
  )
)
"""


def parse_pin(spec: str) -> tuple[str, str]:
    """"NUMBER:NAME" -> (number, name)."""
    number, sep, name = spec.partition(":")
    if not sep or not number:
        raise ValueError(f"pin spec {spec!r} must be NUMBER:NAME")
    return (number, name)


def write_symbol(
    out_dir: Path,
    name: str,
    ref: str,
    value: str,
    footprint: str,
    left: list[tuple[str, str]],
    right: list[tuple[str, str]],
) -> Path:
    out = Path(out_dir) / f"{name}.kicad_sym"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(symbol(name, ref, value, footprint, left, right))
    return out
