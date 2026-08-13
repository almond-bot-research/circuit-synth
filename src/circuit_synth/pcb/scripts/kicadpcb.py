"""Shared helpers for the pcbnew-based layout tools.

These scripts run under a Python that ships pcbnew (see package docstring).
They read the build outputs of a circuit-synth project directory
(`<build>.kicad_sch`, `<build>.json`, `fp-lib-table`) and maintain the
human-owned `<build>.kicad_pcb` alongside them.

Stdlib + pcbnew only: this module is also imported from the main
circuit-synth environment (for `find_kicad_cli` / `find_kicad_python`), so
pcbnew imports stay inside functions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_MAC_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
_MAC_KICAD_PYTHON = (
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
)


# ---------------------------------------------------------------- kicad discovery

def find_kicad_cli() -> str:
    """Locate the kicad-cli binary (KICAD_CLI env override, PATH, macOS app)."""
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    if Path(_MAC_KICAD_CLI).exists():
        return _MAC_KICAD_CLI
    raise RuntimeError(
        "kicad-cli not found: install KiCad 9 or set KICAD_CLI to the binary"
    )


def find_kicad_python() -> str:
    """Locate a Python interpreter that can import pcbnew.

    Order: KICAD_PYTHON env override, the macOS KiCad app bundle, then the
    system python3 (Linux installs pcbnew into the system site-packages).
    """
    env = os.environ.get("KICAD_PYTHON")
    if env:
        return env
    if Path(_MAC_KICAD_PYTHON).exists():
        return _MAC_KICAD_PYTHON
    # `which python3` from inside a venv finds the venv interpreter (which
    # has no pcbnew), so probe the system interpreter explicitly too.
    candidates = ["/usr/bin/python3", shutil.which("python3") or "python3"]
    for candidate in dict.fromkeys(candidates):
        probe = subprocess.run(
            [candidate, "-c", "import pcbnew"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError(
        "no pcbnew-capable Python found: install KiCad 9 or set KICAD_PYTHON"
    )


def library_root() -> Path | None:
    """circuit-synth's bundled library dir, from the CIRCUIT_SYNTH_LIB env var.

    The `cs` CLI sets this before dispatching to a pcbnew script; the value is
    also what `${CIRCUIT_SYNTH_LIB}` expands to in lib tables and model paths.
    """
    env = os.environ.get("CIRCUIT_SYNTH_LIB")
    return Path(env) if env else None


# ---------------------------------------------------------------- sexpr

def parse_sexpr(text: str):
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+', text)

    def parse(idx: int):
        assert tokens[idx] == "("
        idx += 1
        out = []
        while tokens[idx] != ")":
            tok = tokens[idx]
            if tok == "(":
                node, idx = parse(idx)
                out.append(node)
            else:
                out.append(tok[1:-1].replace('\\"', '"') if tok.startswith('"') else tok)
                idx += 1
        return out, idx + 1

    node, _ = parse(0)
    return node


def items(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def one(node, key, default=None):
    found = items(node, key)
    return found[0] if found else default


# ---------------------------------------------------------------- netlist

class Netlist:
    """Parsed KiCad netlist (exported from the generated schematic)."""

    def __init__(self, comps, pad_nets, paths):
        self.comps = comps        # ref -> footprint "LIB:NAME"
        self.pad_nets = pad_nets  # (ref, pad) -> net name
        self.paths = paths        # ref -> KIID path string "/sheet-uuid/symbol-uuid"

    def nets(self) -> dict[str, set]:
        out: dict[str, set] = {}
        for (ref, pad), net in self.pad_nets.items():
            out.setdefault(net, set()).add((ref, pad))
        return out


def export_netlist(project_dir: Path, build_name: str) -> Netlist:
    sch = project_dir / f"{build_name}.kicad_sch"
    with tempfile.NamedTemporaryFile(suffix=".net", delete=False) as tmp:
        out = Path(tmp.name)
    subprocess.run(
        [find_kicad_cli(), "sch", "export", "netlist", str(sch), "-o", str(out)],
        check=False,
        capture_output=True,
    )
    tree = parse_sexpr(out.read_text())

    comps = {}
    paths = {}
    for comp in items(one(tree, "components") or [], "comp"):
        ref = one(comp, "ref")[1]
        comps[ref] = (one(comp, "footprint") or ["", ""])[1]
        sheetpath = one(comp, "sheetpath")
        tstamps = (one(comp, "tstamps") or ["", ""])[1]
        sheet_tstamps = ""
        if sheetpath:
            sheet_tstamps = (one(sheetpath, "tstamps") or ["", ""])[1]
        paths[ref] = (sheet_tstamps.rstrip("/") + "/" + tstamps.strip("/")).replace("//", "/")

    pad_nets = {}
    for net in items(one(tree, "nets") or [], "net"):
        name = one(net, "name")[1]
        for node in items(net, "node"):
            ref = one(node, "ref")[1]
            pad = one(node, "pin")[1]
            pad_nets[(ref, pad)] = name
    return Netlist(comps, pad_nets, paths)


# ---------------------------------------------------------------- addresses

def load_fields(project_dir: Path, build_name: str) -> dict[str, dict[str, str]]:
    """ref -> extra fields (address, Manufacturer, Partnumber, DigiKey, ...).

    These come from the canonical circuit JSON, i.e. the part definitions in
    Python, and are what the fab exports (BOM, IPC-2581) read off the board.
    `ki_*` fields are KiCad's own (footprint filters and the like), which
    symbol libraries carry and pcbnew re-stamps on every save.
    """
    data = json.loads((project_dir / f"{build_name}.json").read_text())
    out: dict[str, dict[str, str]] = {}

    def walk(circuit: dict) -> None:
        for ref, comp in circuit.get("components", {}).items():
            extra = {
                k: v
                for k, v in (comp.get("_extra_fields") or {}).items()
                if v and not k.startswith("ki_")
            }
            if extra:
                out[ref] = extra
        for sub in circuit.get("subcircuits", []):
            walk(sub)

    walk(data)
    return out


def load_addresses(project_dir: Path, build_name: str) -> dict[str, str]:
    """ref -> address from the canonical circuit JSON."""
    return {
        ref: fields["address"]
        for ref, fields in load_fields(project_dir, build_name).items()
        if fields.get("address")
    }


# ---------------------------------------------------------------- libraries

def expand_uri(uri: str, project_dir: Path) -> str:
    uri = uri.replace("${KIPRJMOD}", str(project_dir))
    lib = library_root()
    if lib is not None:
        uri = uri.replace("${CIRCUIT_SYNTH_LIB}", str(lib))
    return uri


def fp_lib_paths(project_dir: Path) -> dict[str, Path]:
    table = project_dir / "fp-lib-table"
    out: dict[str, Path] = {}
    if not table.exists():
        return out
    tree = parse_sexpr(table.read_text())
    for lib in items(tree, "lib"):
        name = one(lib, "name")[1]
        uri = expand_uri(one(lib, "uri")[1], project_dir)
        out[name] = Path(uri).resolve()
    return out


# ---------------------------------------------------------------- 3d models

def model_index(libs: dict[str, Path]) -> dict[str, Path]:
    """3D model filename -> absolute path, across every footprint lib dir.

    Models live next to the footprints that use them (part dirs carry their
    own .step/.wrl; the circuit-synth standard library carries models for its
    generic footprints).
    """
    exts = {".wrl", ".step", ".stp"}
    out: dict[str, Path] = {}
    for path in sorted(set(libs.values())):
        if not path.is_dir():
            continue
        for f in sorted(path.iterdir()):
            if f.suffix.lower() in exts:
                # Case-insensitive: imports reference e.g. R0603.STEP while
                # the file on disk is R0603.step.
                out.setdefault(f.name.lower(), f.resolve())
    return out


def model_ref(model_path: Path, project_dir: Path) -> str:
    """The stable reference to store in a board for a model file.

    Files under the circuit-synth library use `${CIRCUIT_SYNTH_LIB}` (machine
    independent, resolved via env/KiCad path variable); everything else is
    `${KIPRJMOD}`-relative, which is checkout independent for files in the
    same repo as the project.
    """
    lib = library_root()
    if lib is not None:
        try:
            rel = model_path.resolve().relative_to(lib.resolve())
            return "${CIRCUIT_SYNTH_LIB}/" + rel.as_posix()
        except ValueError:
            pass
    rel = os.path.relpath(model_path.resolve(), project_dir.resolve())
    return "${KIPRJMOD}/" + Path(rel).as_posix()


def normalize_models(board, project_dir: Path, libs: dict[str, Path]) -> int:
    """Point every footprint 3D model at a file we can actually find.

    Model paths arrive in many shapes (bare filenames from part libs, stale
    `${KIPRJMOD}/../..` chains from older conventions); each is matched by
    filename against the models carried by the project's footprint libs and
    rewritten to a stable reference. Unknown filenames are left untouched.
    Returns the number of rewritten paths.
    """
    index = model_index(libs)
    changed = 0
    for fp in board.GetFootprints():
        models = fp.Models()
        # Index access returns a reference; iterating the SWIG vector yields
        # copies whose mutation is silently lost.
        for i in range(models.size()):
            current = str(models[i].m_Filename)
            if not current:
                continue
            found = index.get(Path(current).name.lower())
            if found is None:
                continue
            new = model_ref(found, project_dir)
            if new != current:
                models[i].m_Filename = new
                changed += 1
    return changed


# ---------------------------------------------------------------- pcbnew ops

def set_field(fp, name: str, value: str) -> None:
    import pcbnew

    field = fp.GetFieldByName(name)
    if field is None:
        # Field ids 0-4 are the mandatory fields (Reference/Value/Footprint/
        # Datasheet/Description) and pcbnew coerces their names, so a new
        # user field must get an id past 4 and past every existing field.
        next_id = max([f.GetId() for f in fp.GetFields()] + [4]) + 1
        field = pcbnew.PCB_FIELD(fp, next_id, name)
        field.SetVisible(False)
        field.SetText(value)
        fp.AddField(field)
    else:
        field.SetText(value)


def assign_nets(board, netlist: Netlist) -> list[str]:
    """Set pad nets from the netlist. Returns warnings."""
    import pcbnew

    warnings = []
    net_items: dict[str, object] = {}
    for net_name in sorted(set(netlist.pad_nets.values())):
        existing = board.FindNet(net_name)
        if existing is None:
            existing = pcbnew.NETINFO_ITEM(board, net_name)
            board.Add(existing)
        net_items[net_name] = existing

    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            key = (ref, str(pad.GetNumber()))
            net = netlist.pad_nets.get(key)
            if net is None:
                if pad.GetNumber():  # unnumbered pads (fiducial) have no net
                    pad.SetNetCode(0)
            else:
                pad.SetNetCode(net_items[net].GetNetCode())
    return warnings


def place_staging(fp, index: int, origin=(200.0, 220.0), pitch=(4.0, 4.0), columns=20) -> None:
    import pcbnew

    col, row = index % columns, index // columns
    fp.SetPosition(pcbnew.VECTOR2I_MM(origin[0] + col * pitch[0], origin[1] + row * pitch[1]))
