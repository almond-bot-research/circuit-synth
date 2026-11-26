"""A board's layer stack-up: declared by a design, written into the .kicad_pcb.

A design gives `build_project(stackup=stackup([...]))` the dielectric between
each pair of neighbouring copper layers, top to bottom; the copper layer
count follows from it. `cs pcb sync` then keeps the board file's
`(stackup ...)` block equal to the declaration.

    PREPREG = dielectric("prepreg", 0.1194, "S1000-2M 2116", 4.29, 0.018)
    CORE = dielectric("core", 0.2, "S1000-2M", 4.42, 0.018)
    stackup([PREPREG, CORE, PREPREG], inner_copper=0.0152)   # 4 layers

pcbnew's Python API does not expose the stack-up (GetStackupDescriptor
returns an opaque object), so the block is written into the saved file as
text, in the layout pcbnew itself writes; pcbnew carries an existing block
through load/save unchanged. Nothing in KiCad computes impedance from it:
it records the build for the fab and for whoever opens Board Setup, and
the trace geometry that goes with it is declared as net classes
(design_rules.net_class).

Stdlib only: imported by build_project (the circuit-synth environment) and
by `cs pcb sync` (pcbnew Python).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

_KINDS = ("core", "prepreg")


def dielectric(
    kind: str,
    thickness: float,
    material: str = "FR4",
    epsilon_r: float = 4.5,
    loss_tangent: float = 0.02,
) -> dict:
    """One dielectric layer ("core" or "prepreg", thickness in mm); the
    material defaults are KiCad's."""
    if kind not in _KINDS:
        raise ValueError(f"dielectric type must be 'core' or 'prepreg', not {kind!r}")
    if thickness <= 0:
        raise ValueError(f"dielectric thickness must be positive, not {thickness}")
    return {
        "type": kind,
        "thickness": thickness,
        "material": material,
        "epsilon_r": epsilon_r,
        "loss_tangent": loss_tangent,
    }


def stackup(
    dielectrics: Iterable[Mapping],
    *,
    outer_copper: float = 0.035,
    inner_copper: float = 0.0152,
    mask: float = 0.01,
    finish: str = "ENIG",
    impedance_controlled: bool = True,
) -> dict:
    """A stack-up, for `build_project(stackup=...)`.

    `dielectrics` lists the dielectric (see `dielectric`) between each pair
    of neighbouring copper layers from F.Cu down to B.Cu, so N of them make
    a board of N + 1 copper layers. Thicknesses are mm; the copper defaults
    are 1oz outer / 0.5oz inner (the fab's standard multilayer build), the
    mask default is KiCad's.
    """
    layers = [dict(d) for d in dielectrics]
    if not layers or len(layers) % 2 == 0:
        raise ValueError(
            f"a stack-up needs an odd number of dielectrics (copper layers - 1), not {len(layers)}"
        )
    for d in layers:
        dielectric(d["type"], d["thickness"])  # same checks for hand-written dicts
    return {
        "outer_copper": outer_copper,
        "inner_copper": inner_copper,
        "mask": mask,
        "finish": finish,
        "impedance_controlled": impedance_controlled,
        "dielectrics": layers,
    }


def copper_layer_count(spec: Mapping) -> int:
    """Number of copper layers a stack-up describes."""
    return len(spec["dielectrics"]) + 1


def copper_layer_names(count: int) -> list[str]:
    """KiCad's copper layer names for a board of `count` layers, top to bottom."""
    return ["F.Cu", *(f"In{i}.Cu" for i in range(1, count - 1)), "B.Cu"]


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def stackup_block(spec: Mapping) -> str:
    """The `(stackup ...)` list for a board file, formatted as pcbnew 9
    writes it at its place inside `(setup ...)` (two tabs deep; the first
    line carries no indent)."""

    def layer(name: str, kind: str, thickness: float | None = None, d: Mapping | None = None) -> str:
        rows = [f"(layer {_quote(name)}", f"\t(type {_quote(kind)})"]
        if thickness is not None:
            rows.append(f"\t(thickness {_num(thickness)})")
        if d is not None:
            rows += [
                f"\t(material {_quote(d['material'])})",
                f"\t(epsilon_r {_num(d['epsilon_r'])})",
                f"\t(loss_tangent {_num(d['loss_tangent'])})",
            ]
        return "\n".join(rows) + "\n)"

    copper = copper_layer_names(copper_layer_count(spec))
    items = [
        layer("F.SilkS", "Top Silk Screen"),
        layer("F.Paste", "Top Solder Paste"),
        layer("F.Mask", "Top Solder Mask", spec["mask"]),
    ]
    for i, name in enumerate(copper):
        outer = name in ("F.Cu", "B.Cu")
        items.append(layer(name, "copper", spec["outer_copper"] if outer else spec["inner_copper"]))
        if i < len(spec["dielectrics"]):
            d = spec["dielectrics"][i]
            items.append(layer(f"dielectric {i + 1}", d["type"], d["thickness"], d))
    items += [
        layer("B.Mask", "Bottom Solder Mask", spec["mask"]),
        layer("B.Paste", "Bottom Solder Paste"),
        layer("B.SilkS", "Bottom Silk Screen"),
        f"(copper_finish {_quote(spec['finish'])})",
        f"(dielectric_constraints {'yes' if spec['impedance_controlled'] else 'no'})",
    ]
    body = "\n".join(items).replace("\n", "\n\t\t\t")
    return f"(stackup\n\t\t\t{body}\n\t\t)"


# ------------------------------------------------------------ board file


def _skip_string(text: str, i: int) -> int:
    """Index just past the quoted string opening at text[i]."""
    i += 1
    while text[i] != '"':
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _list_end(text: str, start: int) -> int:
    """Index just past the list opening at text[start]."""
    depth, i = 0, start
    while True:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1


def _child(text: str, start: int, name: str) -> tuple[int, int] | None:
    """(start, end) of the first list called `name` directly inside the
    list opening at text[start]. Parentheses inside quoted strings (a
    title, a material name) do not count."""
    i = start + 1
    while True:
        ch = text[i]
        if ch == '"':
            i = _skip_string(text, i)
        elif ch == "(":
            if re.match(r"\(\s*" + re.escape(name) + r"(?=[\s()])", text[i : i + len(name) + 16]):
                return i, _list_end(text, i)
            i = _list_end(text, i)
        elif ch == ")":
            return None
        else:
            i += 1


def _tokens(sexpr: str) -> list:
    """Tokens of an s-expression with numbers as floats, to compare two
    blocks regardless of whitespace and number formatting."""
    out: list = []
    for tok in re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+', sexpr):
        try:
            out.append(float(tok))
        except ValueError:
            out.append(tok)
    return out


def read_stackup_block(text: str) -> tuple[int, int, int] | None:
    """Locate a board file's stack-up: (setup start, block start, block
    end), with the block positions -1 when the board has none; None when
    the text has no `(setup ...)` at all."""
    try:
        root = text.index("(")
        setup = _child(text, root, "setup")
        if setup is None:
            return None
        block = _child(text, setup[0], "stackup")
    except (IndexError, ValueError):
        raise ValueError("not a well-formed board file (unbalanced parentheses or quotes)") from None
    return (setup[0], *(block or (-1, -1)))


def write_stackup(pcb_path: Path, spec: Mapping) -> list[str]:
    """Make a saved board file's `(stackup ...)` block describe `spec`.
    Returns log messages.

    An existing block is replaced, a missing one is added at the top of
    `(setup ...)`; a block that already says the same (whatever its
    whitespace or number formatting) leaves the file untouched, so
    repeating the call changes nothing. Call it after pcbnew has saved the
    board.
    """
    text = pcb_path.read_text()
    found = read_stackup_block(text)
    if found is None:
        raise ValueError(f"{pcb_path.name} has no (setup ...) section to hold a stack-up")
    setup, start, end = found
    block = stackup_block(spec)
    if start < 0:
        at = setup + len("(setup")
        pcb_path.write_text(text[:at] + "\n\t\t" + block + text[at:])
    elif _tokens(text[start:end]) == _tokens(block):
        return []
    else:
        pcb_path.write_text(text[:start] + block + text[end:])
    count = copper_layer_count(spec)
    return [f"stack-up written to {pcb_path.name}: {count} copper layers, {len(spec['dielectrics'])} dielectrics"]
