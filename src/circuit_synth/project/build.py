"""Project generation helpers.

Each package/design owns a `layouts/` dir with one KiCad project per build
(`layouts/<build>/<build>.kicad_*`). The schematic and netlists are
regenerated from Python; the `.kicad_pcb` in the same directory is the
human-owned layout, updated separately by the layout tooling (`cs pcb ...`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from circuit_synth import Circuit
from circuit_synth.library import ENV_VAR as _LIB_ENV
from circuit_synth.library import ensure_env, footprint_lib_dirs, library_root
from circuit_synth.pcb.scripts.kicadpcb import find_kicad_cli

from .address import existing_refs, finalize_design
from .schematic_labels import compute_global_nets, parse_spans, rewrite_labels

# Historical namespace string (this module started life as
# almond_bot.common.build): changing it would churn every annotation UUID in
# every circuit JSON already generated with it, so it stays.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "almond_bot.common.build")


def parts_dir(module_file: str | Path) -> Path:
    """Return the `parts/` dir of the package that owns `module_file`.

    Assumes the src layout `<pkg>/src/<namespace>/<pkg>/...` with parts at
    `<pkg>/parts` (workspace members are installed editable, so `__file__`
    stays inside the repo).
    """
    return Path(module_file).resolve().parents[3] / "parts"


def setup_symbol_dirs(*dirs: Path) -> None:
    """Point circuit-synth's symbol cache at part libraries.

    The bundled standard libraries are always included; explicitly passed
    directories take precedence.

    Must be called before any Component is constructed. Each entry in
    KICAD_SYMBOL_DIR is an individual part directory: kicad-sch-api validates
    entries non-recursively (it only globs `*.kicad_sym` at the top level),
    so parts roots would be rejected even though scanning is recursive.

    Fails if two different roots provide a part directory with the same name:
    duplicated part dirs inevitably drift apart (fixes land in one copy and
    not the others), so shared parts must live in exactly one place.
    """
    part_dirs: list[Path] = []
    seen: dict[str, Path] = {}
    for root in [*dirs, *footprint_lib_dirs()]:
        root = Path(root).resolve()
        if any(root.glob("*.kicad_sym")):
            part_dirs.append(root)
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            if any(sub.glob("*.kicad_sym")):
                dup = seen.get(sub.name)
                if dup is not None and dup != sub:
                    raise SystemExit(
                        f"duplicate part library {sub.name!r}:\n  {dup}\n  {sub}\n"
                        "Keep a single copy in a shared parts dir (listed in "
                        "PARTS_DIRS by every design that uses it) and delete the rest."
                    )
                if dup is None:
                    seen[sub.name] = sub
                    part_dirs.append(sub)
    os.environ["KICAD_SYMBOL_DIR"] = ":".join(str(p) for p in part_dirs)


def _lib_uri(target: Path, project_dir: Path) -> str:
    """Stable lib-table URI: ${CIRCUIT_SYNTH_LIB} for bundled libraries
    (machine independent), ${KIPRJMOD}-relative for everything else
    (checkout independent within a repo)."""
    lib = library_root()
    try:
        rel = target.resolve().relative_to(lib)
        return "${CIRCUIT_SYNTH_LIB}/" + rel.as_posix()
    except ValueError:
        return "${KIPRJMOD}/" + Path(os.path.relpath(target, project_dir)).as_posix()


def _lib_tables(project_dir: Path, parts_dirs: list[Path]) -> None:
    """Write sym-lib-table / fp-lib-table for a project.

    Lib nickname convention: the part directory name is the footprint lib
    nickname; the symbol file stem is the symbol lib nickname.
    First part dir wins when the same lib is carried by several packages.
    """
    fp_libs: dict[str, Path] = {}
    sym_libs: dict[str, Path] = {}

    def add_lib_dir(part: Path) -> None:
        if any(part.glob("*.kicad_mod")):
            fp_libs.setdefault(part.name, part)
        for sym in sorted(part.glob("*.kicad_sym")):
            sym_libs.setdefault(sym.stem, sym)

    for root in parts_dirs:
        root = Path(root).resolve()
        if not root.is_dir():
            continue
        for part in sorted(p for p in root.iterdir() if p.is_dir()):
            add_lib_dir(part)
    for std in footprint_lib_dirs():
        add_lib_dir(std)

    project_dir = project_dir.resolve()

    fp_rows = "".join(
        f'    (lib\n        (name "{name}")\n        (type "KiCad")\n'
        f'        (uri "{_lib_uri(path, project_dir)}")\n        (options "")\n'
        f'        (descr "part lib: {name}")\n    )\n'
        for name, path in sorted(fp_libs.items())
    )
    (project_dir / "fp-lib-table").write_text(f"(fp_lib_table\n    (version 7)\n{fp_rows})\n")

    sym_rows = "".join(
        f'    (lib\n        (name "{name}")\n        (type "KiCad")\n'
        f'        (uri "{_lib_uri(path, project_dir)}")\n        (options "")\n'
        f'        (descr "part lib: {name}")\n    )\n'
        for name, path in sorted(sym_libs.items())
    )
    (project_dir / "sym-lib-table").write_text(f"(sym_lib_table\n    (version 7)\n{sym_rows})\n")


_QUOTED_RE = re.compile(r'("(?:[^"\\]|\\.)*")')
_FLOAT_RE = re.compile(r"(?<![\w.\-])-?\d+\.\d+(?![\w.])")


def _canonicalize_numbers(path: Path) -> None:
    """Rewrite bare float tokens in an s-expression file to minimal form.

    The schematic writer emits some values as int and some as float
    depending on how they were produced, and that typing is not stable
    across platforms (0 vs 0.0000). Stripping trailing zeros outside quoted
    strings makes regenerated files byte-identical everywhere.
    """

    def canon(match: re.Match) -> str:
        text = match.group(0).rstrip("0").rstrip(".")
        return "0" if text in ("-0", "") else text

    parts = _QUOTED_RE.split(path.read_text())
    # re.split with a capturing group alternates non-quoted / quoted segments
    for i in range(0, len(parts), 2):
        parts[i] = _FLOAT_RE.sub(canon, parts[i])
    path.write_text("".join(parts))


def _normalize_circuit_json(path: Path) -> None:
    """Strip run-to-run noise from circuit-synth's canonical circuit JSON.

    tstamps embed Python object ids and annotation uuids are random per run;
    nothing downstream reads either, but they churn every rebuild.
    """
    text = path.read_text()
    data = json.loads(text)

    def walk(circ: dict) -> None:
        name = circ.get("name", "")
        if "tstamps" in circ:
            circ["tstamps"] = f"/{name}/"
        for i, ann in enumerate(circ.get("annotations", [])):
            if "uuid" in ann:
                ann["uuid"] = str(uuid.uuid5(_UUID_NAMESPACE, f"{name}|annotation|{i}"))
        for sub in circ.get("subcircuits", []):
            walk(sub)

    walk(data)
    out = json.dumps(data, indent=2)
    path.write_text(out + "\n" if text.endswith("\n") else out)


_FOOTPRINT_PROP_RE = re.compile(r'(\(property "Footprint" ")((?:[^"\\]|\\.)*)(")')


def _sync_footprints(project_dir: Path, build_name: str) -> None:
    """Update each symbol instance's Footprint property from the circuit JSON.

    The schematic writer only syncs existing sheets structurally; a footprint
    change on an existing component in Python never reaches the frozen
    schematic, so the exported netlist - and therefore `cs pcb sync` - would
    keep placing the old footprint. This pass rewrites the instance
    "Footprint" property in place, leaving everything else byte-identical.
    """
    circuit_json = json.loads((project_dir / f"{build_name}.json").read_text())

    scopes: dict[str, dict[str, str]] = {}

    def collect(circ: dict) -> None:
        comps = circ.get("components", {})
        if isinstance(comps, dict):
            comps = [{"ref": ref, **info} for ref, info in comps.items()]
        scopes[circ["name"]] = {c["ref"]: c.get("footprint", "") for c in comps}
        for sub in circ.get("subcircuits", []):
            collect(sub)

    collect(circuit_json)

    for name, footprints in scopes.items():
        path = project_dir / f"{name}.kicad_sch"
        if not path.exists() or not footprints:
            continue
        text = path.read_text()
        tree = parse_spans(text)
        symbols = [n for n in tree if isinstance(n, list) and n and n[0] == "symbol"]
        changed = False
        for sym in reversed(symbols):  # back-to-front keeps earlier spans valid
            props = {p[1]: p[2] for p in sym if isinstance(p, list) and p and p[0] == "property" and len(p) > 2}
            footprint = footprints.get(props.get("Reference"))
            if footprint is None or props.get("Footprint") == footprint:
                continue
            start, end = sym.span
            segment = _FOOTPRINT_PROP_RE.sub(
                lambda m: m.group(1) + footprint + m.group(3), text[start:end], count=1
            )
            text = text[:start] + segment + text[end:]
            changed = True
        if changed:
            path.write_text(text)


def _export_netlist(project_dir: Path, build_name: str) -> None:
    """Export <build>.net from the generated schematic with kicad-cli.

    circuit-synth's own netlist writer stamps a wall-clock date and fresh
    uuids on every run; kicad-cli reads the byte-stable schematic instead, so
    the committed netlist only changes when the circuit does. The date header
    is blanked and the absolute project prefix kicad-cli embeds in `source`
    and library `uri` paths is relativized for the same reason (otherwise the
    netlist churns when rebuilt from a different checkout location).
    """
    sch = project_dir / f"{build_name}.kicad_sch"
    out = project_dir / f"{build_name}.net"
    out.unlink(missing_ok=True)
    res = subprocess.run(
        [find_kicad_cli(), "sch", "export", "netlist", str(sch), "-o", str(out)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0 or not out.exists():
        raise RuntimeError(f"kicad-cli netlist export failed: {res.stderr.strip()}")
    text = re.sub(r'\(date "[^"]*"\)', '(date "")', out.read_text(), count=1)
    # The tool string carries distro suffixes ("9.0.8+dfsg-1" on Debian), so
    # it would churn between machines running the same KiCad release.
    text = re.sub(r'\(tool "Eeschema[^"]*"\)', '(tool "Eeschema")', text, count=1)
    text = text.replace(str(project_dir.resolve()) + "/", "")
    text = text.replace(str(library_root()), "${CIRCUIT_SYNTH_LIB}")
    out.write_text(text)


def build_project(
    circuit: Circuit,
    layouts_dir: str | Path,
    build_name: str,
    parts_dirs: list[Path],
    ref_map: dict[str, str] | Path | None = None,
) -> Path:
    """Generate the KiCad project (schematic + netlists) for one build.

    Returns the project directory (`layouts/<build_name>`).
    """
    ensure_env()
    out = Path(layouts_dir) / build_name
    # Keep the designators the existing schematic already assigned: the
    # schematic sync matches components by designator, so letting them
    # renumber (anything declared after a newly added part shifts) would also
    # scramble which symbol gets which value, footprint, and fields.
    addressed = finalize_design(circuit, ref_map, keep_refs=existing_refs(out))
    global_nets = compute_global_nets(circuit)
    circuit.name = build_name

    out.mkdir(parents=True, exist_ok=True)

    result = circuit.generate_kicad_project(
        str(out),
        generate_pcb=False,
        update_source_refs=False,
        generate_ratsnest=False,
    )
    if not result.get("success"):
        raise RuntimeError(f"KiCad project generation failed: {result.get('error')}")

    circuit.generate_flattened_json_netlist(str(out / f"{build_name}.flat.json"))
    _normalize_circuit_json(out / f"{build_name}.json")
    for sch in out.glob("*.kicad_sch"):
        _canonicalize_numbers(sch)
    _sync_footprints(out, build_name)
    rewrite_labels(out, build_name, global_nets)
    _lib_tables(out, list(parts_dirs))
    # After label rewriting: the labels define the connectivity kicad-cli sees
    _export_netlist(out, build_name)

    print(f"[{build_name}] {len(addressed)} addressed components -> {out}")
    return out
