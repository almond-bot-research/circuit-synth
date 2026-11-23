"""The `cs` umbrella CLI: every recurring circuit-synth workflow as a command.

Agents and humans working in a design repo should reach for these instead of
writing ad-hoc scripts:

  cs build <design_dir>                regenerate schematic + netlists
  cs pcb sync ...                      update the .kicad_pcb from the schematic
  cs pcb apply-layouts ...             seed a board from package layouts
  cs pcb compare ...                   verify connectivity against a reference board
  cs pcb extract-footprints ...        lift footprints out of a board
  cs pcb fab ...                       export the assembler's quote package
  cs pcb route ...                     apply a hand-written routing.py (not an autorouter)
  cs pcb pads|drc|render ...           inspect a board without opening pcbnew
  cs bom export ...                    write the BOM CSV
  cs bom verify ...                    check sourcing and stock against DigiKey/Mouser
  cs parts search|detail|datasheet     pick parts from live distributor data
  cs parts import <LCSC_ID>            onboard a part (symbol/footprint/3D/datasheet)
  cs parts model <part_dir> --from ... install a manufacturer 3D model into a part
  cs parts min-symbol                  generate a minimal box symbol
  cs sim run <design_or_fw_dir>        run firmware simulation suites (firmware on Linux vs part models)
  cs sim pinmap <header> <netlist>     check firmware pin macros against the netlist
  cs sim parts | csrc                  list part models / locate the C simulation runtime
  cs docs snapshot                     snapshot a web page to markdown
  cs setup-kicad                       register ${CIRCUIT_SYNTH_LIB} for the KiCad GUI
  cs kicad-py                          run any script under a pcbnew-capable Python

The `cs pcb` commands re-execute their implementation scripts under a Python
that can import pcbnew (KiCad's bundled interpreter on macOS, the system one
on Linux) because pcbnew is not installable into a normal venv.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from circuit_synth.library import ENV_VAR, ensure_env, library_root


def _pcb_script(name: str, args: list[str]) -> None:
    """Run one of the pcb/scripts under a pcbnew-capable interpreter."""
    from circuit_synth.pcb import scripts
    from circuit_synth.pcb.scripts.kicadpcb import find_kicad_cli, find_kicad_python

    ensure_env()
    env = os.environ.copy()
    env.setdefault("KICAD_CLI", find_kicad_cli())
    script = Path(scripts.__path__[0]) / name
    res = subprocess.run([find_kicad_python(), str(script), *args], env=env)
    sys.exit(res.returncode)


@click.group()
def cli() -> None:
    """circuit-synth workflows for design repos."""


# -------------------------------------------------------------------- build

@cli.command()
@click.argument("design_dirs", nargs=-1, required=True, type=click.Path(exists=True, file_okay=False))
def build(design_dirs: tuple[str, ...]) -> None:
    """Regenerate the KiCad project(s) by running each design's build.py."""
    for design_dir in design_dirs:
        script = Path(design_dir) / "build.py"
        if not script.exists():
            raise SystemExit(f"no build.py in {design_dir}")
        res = subprocess.run([sys.executable, str(script)])
        if res.returncode != 0:
            sys.exit(res.returncode)


# ---------------------------------------------------------------------- pcb

@cli.group()
def pcb() -> None:
    """Board tools (run under KiCad's pcbnew Python)."""


@pcb.command("sync")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
def pcb_sync(project_dir: str, build_name: str) -> None:
    """Create/update the .kicad_pcb from the generated schematic.

    Adds/removes/swaps footprints, assigns pad nets, copies part fields,
    normalizes 3D model paths. Placement and routing are preserved. Parts
    marked DNP=True in Python keep their footprint and get pcbnew's "Do not
    populate" attribute.
    """
    _pcb_script("sync_pcb.py", [str(Path(project_dir).resolve()), build_name])


@pcb.command("apply-layouts")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
@click.argument("mappings", nargs=-1, required=True)
@click.option("--force", is_flag=True, help="Re-apply even over existing groups.")
def pcb_apply_layouts(project_dir: str, build_name: str, mappings: tuple[str, ...], force: bool) -> None:
    """Copy package layouts onto the board as groups.

    MAPPINGS are <instance>=<package_layout_dir>:<source_prefix>, e.g.
    mcu=packages/mcu/layouts/rp2354a:main
    """
    args = [str(Path(project_dir).resolve()), build_name, *mappings]
    if force:
        args.append("--force")
    _pcb_script("apply_package_layouts.py", args)


@pcb.command("fab")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
@click.option("--zip", "zip_path", default="", help="Also write the package as a zip here.")
def pcb_fab(project_dir: str, build_name: str, zip_path: str) -> None:
    """Write the assembler's quote package to <project_dir>/fab/.

    X2 gerbers (all layers), drill + map, position CSV, IPC-2581 with
    MPN/MFR/DigiKey columns, the BOM CSV, and the ECAD sources
    (board/project/rules). Aborts on DRC errors and warns on
    paste/fiducial/MPN issues the assembler would flag. Do-not-populate
    parts are left out of the position file and the BOM, flagged in the
    IPC-2581, and listed in <build>-dnp.csv.
    """
    from circuit_synth.manufacturing.bom_csv import export as bom_export_fn

    bom_export_fn(Path(project_dir).resolve(), build_name)
    args = [str(Path(project_dir).resolve()), build_name]
    if zip_path:
        args += ["--zip", zip_path]
    _pcb_script("fab_export.py", args)


@pcb.command("compare")
@click.argument("reference_board", type=click.Path(exists=True, dir_okay=False))
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
@click.argument("ref_map", type=click.Path(exists=True, dir_okay=False))
def pcb_compare(reference_board: str, project_dir: str, build_name: str, ref_map: str) -> None:
    """Verify schematic connectivity against a reference board's pad nets."""
    _pcb_script(
        "compare_to_board.py",
        [reference_board, str(Path(project_dir).resolve()), build_name, ref_map],
    )


@pcb.command("extract-footprints")
@click.argument("board", type=click.Path(exists=True, dir_okay=False))
@click.argument("specs", nargs=-1, required=True)
def pcb_extract_footprints(board: str, specs: tuple[str, ...]) -> None:
    """Lift footprints out of a board into part dirs.

    SPECS are <ref>=<out_dir>:<footprint_name>.
    """
    _pcb_script("extract_footprints.py", [str(Path(board).resolve()), *specs])


@pcb.command("route")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
@click.option("--routing", "routing", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Routing file (default: <project_dir>/routing.py).")
@click.option("--no-fill", is_flag=True, help="Skip the zone refill.")
def pcb_route(project_dir: str, build_name: str, routing: str | None, no_fill: bool) -> None:
    """Apply a hand-written routing file (tracks, vias, zones) to the board.

    Not an autorouter: routing.py defines route(r) and lists every trace
    as waypoints (pads or coordinates, joined with 45 degree doglegs).
    The copper it creates lives in one board group, `routing`, which is
    rebuilt on every run; everything else on the board is left alone.
    See circuit_synth/pcb/scripts/route_pcb.py for the Router API.
    """
    args = [str(Path(project_dir).resolve()), build_name]
    args.append(str(Path(routing).resolve()) if routing else str(Path(project_dir).resolve() / "routing.py"))
    if no_fill:
        args.append("--no-fill")
    _pcb_script("route_pcb.py", args)


def _board_path(project_dir: str, build_name: str) -> str:
    return str(Path(project_dir).resolve() / f"{build_name}.kicad_pcb")


@pcb.command("pads")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
@click.option("--ref", "refs", multiple=True, help="Only these footprints (repeatable).")
@click.option("--net", "nets", multiple=True, help="Only these nets (repeatable).")
@click.option("--box", default=None, help="Only pads inside x0,y0,x1,y1 (mm).")
def pcb_pads(project_dir: str, build_name: str, refs: tuple[str, ...], nets: tuple[str, ...], box: str | None) -> None:
    """List pads (REF.PAD, pin function, net, x, y, layer, size)."""
    args = ["pads", _board_path(project_dir, build_name)]
    if refs:
        args += ["--ref", *refs]
    if nets:
        args += ["--net", *nets]
    if box:
        args += ["--box", box]
    _pcb_script("board_inspect.py", args)


@pcb.command("drc")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
@click.option("--all", "all_severities", is_flag=True, help="Include warnings.")
@click.option("--limit", default=40, show_default=True, help="Max items listed per section.")
def pcb_drc(project_dir: str, build_name: str, all_severities: bool, limit: int) -> None:
    """Run DRC and print violations and unrouted items with coordinates."""
    args = ["drc", _board_path(project_dir, build_name), "--limit", str(limit)]
    if all_severities:
        args.append("--all")
    _pcb_script("board_inspect.py", args)


@pcb.command("render")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
@click.argument("out", type=click.Path(dir_okay=False))
@click.option("--box", default=None, help="Region x0,y0,x1,y1 in board mm (default: whole page).")
@click.option("--layers", default="F.Cu,B.Cu,Edge.Cuts", show_default=True)
@click.option("--px", default=1600, show_default=True, help="Output width in pixels.")
def pcb_render(project_dir: str, build_name: str, out: str, box: str | None, layers: str, px: int) -> None:
    """Render the board, or a region of it, to PNG or SVG."""
    args = ["render", _board_path(project_dir, build_name), str(Path(out).resolve()),
            "--layers", layers, "--px", str(px)]
    if box:
        args += ["--box", box]
    _pcb_script("board_inspect.py", args)


@cli.command("kicad-py", context_settings={"ignore_unknown_options": True})
@click.argument("script", type=click.Path(exists=True, dir_okay=False))
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def kicad_py(script: str, args: tuple[str, ...]) -> None:
    """Run an arbitrary script under a pcbnew-capable Python."""
    from circuit_synth.pcb.scripts.kicadpcb import find_kicad_cli, find_kicad_python

    ensure_env()
    env = os.environ.copy()
    env.setdefault("KICAD_CLI", find_kicad_cli())
    res = subprocess.run([find_kicad_python(), script, *args], env=env)
    sys.exit(res.returncode)


# ---------------------------------------------------------------------- bom

@cli.group()
def bom() -> None:
    """BOM export and sourcing verification."""


@bom.command("export")
@click.argument("project_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("build_name")
def bom_export(project_dir: str, build_name: str) -> None:
    """Write <build>.bom.csv from the build's canonical circuit JSON.

    Parts with no MPN and parts marked DNP=True are left out.
    """
    from circuit_synth.manufacturing.bom_csv import export

    export(Path(project_dir).resolve(), build_name)


@bom.command("verify")
@click.argument("builds", nargs=-1)
@click.option("--only", help="Check a single MPN.")
@click.option(
    "--low-stock",
    type=click.IntRange(min=0),
    default=100,
    show_default=True,
    metavar="BOARDS",
    help="Warn when a recorded listing's stock covers fewer than BOARDS boards "
    "(stock < BOARDS x the part's per-board quantity). 0 turns the warning off.",
)
def bom_verify(builds: tuple[str, ...], only: str | None, low_stock: int) -> None:
    """Check MPNs, distributor numbers and stock against DigiKey/Mouser.

    BUILDS are <project_dir> <build_name> pairs; with none given, every
    layouts/<build>/<build>.json project under the current directory is
    checked. Stock is read from the DigiKey/Mouser numbers the BOM records:
    a part fails when its DigiKey listing (the primary source) has no
    stock, or when no recorded listing has any; a backup listing at 0 or
    any listing below --low-stock is a warning. Lookup errors are warnings,
    never out of stock. Exits non-zero when any part fails.
    """
    from circuit_synth.manufacturing.sourcing_check import verify

    if len(builds) % 2:
        raise SystemExit("BUILDS must be <project_dir> <build_name> pairs")
    pairs = [(Path(builds[i]), builds[i + 1]) for i in range(0, len(builds), 2)]
    sys.exit(verify(pairs, only=only, low_stock=low_stock))


# ---------------------------------------------------------------------- sim

@cli.group()
def sim() -> None:
    """Firmware-in-the-loop simulation (docs/FIRMWARE_SIM.md)."""


@sim.command("run")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("-k", "pattern", default=None, help="Only tests matching this substring.")
@click.option("-v", "verbose", is_flag=True, help="List every test.")
@click.option("--no-build", is_flag=True, help="Skip `make sim`.")
def sim_run(path: str, pattern: str | None, verbose: bool, no_build: bool) -> None:
    """Build and run the simulation suites in PATH (a firmware or design directory)."""
    from circuit_synth.fwsim import runner

    suites = runner.find_suites(path)
    if not suites:
        raise SystemExit(f"no simulation suite (a tests/ directory with test_*.py) in {path}")
    failed = 0
    for suite in suites:
        click.echo(f"== {suite}")
        failed += runner.run_suite(suite, pattern, verbose, build=not no_build) != 0
    sys.exit(1 if failed else 0)


@sim.command("pinmap")
@click.argument("header", type=click.Path(exists=True, dir_okay=False))
@click.argument("netlist", type=click.Path(exists=True, dir_okay=False))
@click.option("--mcu", required=True, help="Text in the MCU's value in the netlist, e.g. RP2354.")
@click.option("--prefix", default="PIN_", show_default=True, help="Pin macros are <prefix><net name>.")
@click.option("--unused", default=None, help="Macro listing the GPIOs the firmware treats as unconnected.")
def sim_pinmap(header: str, netlist: str, mcu: str, prefix: str, unused: str | None) -> None:
    """Check a firmware header's pin macros against the MCU's nets in a KiCad netlist."""
    from circuit_synth.fwsim import pinmap
    from circuit_synth.fwsim.netlist import Board

    board = Board(netlist)
    _, gpio_net = pinmap.mcu_gpio_nets(board, mcu)
    unused_set = pinmap.header_int_list(header, unused) if unused else None
    if unused and unused_set is None:
        raise SystemExit(f"{header} has no #define {unused} {{ ... }}")
    problems = pinmap.check_pin_macros(pinmap.header_defines(header), gpio_net, prefix=prefix, unused=unused_set)
    for p in problems:
        click.echo(p)
    connected = sum(1 for n in gpio_net.values() if not Board.unconnected(n))
    click.echo(f"{connected} MCU nets checked, {len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


@sim.command("parts")
def sim_parts() -> None:
    """List the device models that ship with circuit-synth."""
    from circuit_synth.fwsim.parts import MODELS

    for name, text in MODELS.items():
        click.echo(f"{name:12} {text}")


@sim.command("csrc")
def sim_csrc() -> None:
    """Print the directory of the C simulation runtime (cs_sim.h / cs_sim.c)."""
    from circuit_synth.fwsim import CSRC_DIR

    click.echo(str(CSRC_DIR))


# -------------------------------------------------------------------- parts

@cli.group()
def parts() -> None:
    """Part selection against live DigiKey/Mouser catalog data."""


@parts.command("search")
@click.argument("query")
@click.option("--limit", default=10, show_default=True, help="Max results per distributor.")
@click.option("--in-stock", is_flag=True, help="Only parts with stock on hand.")
@click.option("--package", default="", help="Filter by package/case text (e.g. 0603, SOT-23).")
def parts_search(query: str, limit: int, in_stock: bool, package: str) -> None:
    """Search both catalogs for candidates matching QUERY."""
    from circuit_synth.manufacturing.part_search import search

    sys.exit(search(query, limit=limit, in_stock=in_stock, package=package))


@parts.command("detail")
@click.argument("mpn")
def parts_detail(mpn: str) -> None:
    """Full catalog record for one MPN: parameters, offers, stock, pricing."""
    from circuit_synth.manufacturing.part_search import detail

    sys.exit(detail(mpn))


@parts.command("datasheet")
@click.argument("mpn")
@click.option(
    "--out",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory to save into (use the part's directory).",
)
@click.option("--filename", default="", help="Override the output filename.")
def parts_datasheet(mpn: str, out: str, filename: str) -> None:
    """Download the datasheet PDF for an MPN (DigiKey first, then Mouser)."""
    from circuit_synth.manufacturing.part_search import fetch_datasheet

    sys.exit(fetch_datasheet(mpn, Path(out), filename))


@parts.command("import")
@click.argument("lcsc_id")
@click.option(
    "--out",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="parts/ root of the owning package.",
)
@click.option("--name", default="", help="Part directory name (default: Manufacturer_MPN).")
@click.option("--no-datasheet", is_flag=True, help="Skip the datasheet download.")
@click.option("--overwrite", is_flag=True, help="Replace an existing part directory's files.")
def parts_import(lcsc_id: str, out: str, name: str, no_datasheet: bool, overwrite: bool) -> None:
    """Import symbol, footprint, 3D model, and datasheet for LCSC_ID.

    Fetches the EasyEDA CAD data behind an LCSC part number (find it on
    lcsc.com) and writes a complete part directory. Footprints that exist in
    the bundled standard libraries are referenced instead of copied.
    """
    from circuit_synth.manufacturing.part_import import import_part

    sys.exit(import_part(lcsc_id, Path(out), name=name, datasheet=not no_datasheet, overwrite=overwrite))


@parts.command("model")
@click.argument("part_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--from",
    "source",
    required=True,
    help="STEP file, zip, directory, or URL (manufacturer download).",
)
@click.option(
    "--rotate",
    default="0,0,0",
    show_default=True,
    help="Model rotation in degrees, x,y,z.",
)
@click.option(
    "--offset",
    default="0,0,0",
    show_default=True,
    help="Model offset in mm, x,y,z.",
)
@click.option(
    "--preview",
    default="",
    help="Render the footprint+model from above to this PNG for alignment checks.",
)
def parts_model(part_dir: str, source: str, rotate: str, offset: str, preview: str) -> None:
    """Install a 3D model into PART_DIR and stamp its footprint's model block.

    For parts LCSC doesn't stock (so `cs parts import` can't fetch a model):
    download the STEP from the manufacturer and install it here. Re-running
    replaces the model block, so iterate on --rotate/--offset until the
    --preview render matches the silkscreen.
    """
    from circuit_synth.manufacturing.part_model import install_model

    def triple(value: str) -> tuple[float, float, float]:
        parts_ = [float(v) for v in value.split(",")]
        if len(parts_) != 3:
            raise SystemExit(f"expected x,y,z - got {value!r}")
        return (parts_[0], parts_[1], parts_[2])

    sys.exit(
        install_model(
            Path(part_dir),
            source,
            rotate=triple(rotate),
            offset=triple(offset),
            preview=preview,
        )
    )


@parts.command("min-symbol")
@click.option("--out", required=True, type=click.Path(file_okay=False), help="Part directory to write into.")
@click.option("--name", required=True, help="Symbol name (also the file stem).")
@click.option("--ref", required=True, help="Reference prefix (R, C, U, ...).")
@click.option("--value", required=True, help="Value field.")
@click.option("--footprint", required=True, help="Footprint id LIB:NAME.")
@click.option("--left", "left_pins", multiple=True, help="Left-side pin NUMBER:NAME (top to bottom).")
@click.option("--right", "right_pins", multiple=True, help="Right-side pin NUMBER:NAME (top to bottom).")
def parts_min_symbol(
    out: str,
    name: str,
    ref: str,
    value: str,
    footprint: str,
    left_pins: tuple[str, ...],
    right_pins: tuple[str, ...],
) -> None:
    """Generate a minimal box symbol whose pin numbers match the footprint."""
    from circuit_synth.tools.min_symbol import parse_pin, write_symbol

    if not left_pins and not right_pins:
        raise SystemExit("at least one --left or --right pin is required")
    path = write_symbol(
        Path(out),
        name,
        ref,
        value,
        footprint,
        [parse_pin(p) for p in left_pins],
        [parse_pin(p) for p in right_pins],
    )
    print(f"wrote {path}")


# --------------------------------------------------------------------- docs

@cli.group()
def docs() -> None:
    """Reference document management."""


@docs.command("snapshot")
@click.argument("url")
@click.argument("out", type=click.Path(dir_okay=False))
@click.option("--title", default="", help="Document title (H1).")
@click.option("--note", default="", help="Preamble under the title.")
def docs_snapshot(url: str, out: str, title: str, note: str) -> None:
    """Snapshot URL into a markdown file, only rewriting on real changes."""
    from circuit_synth.tools.html_snapshot import snapshot

    sys.exit(snapshot(url, Path(out), title=title, note=note))


# -------------------------------------------------------------------- setup

@cli.command("setup-kicad")
def setup_kicad() -> None:
    """Register the ${CIRCUIT_SYNTH_LIB} path variable in KiCad's user config.

    The CLI tools set it as an environment variable themselves; this makes
    the KiCad GUI resolve bundled footprints and 3D models too.
    """
    roots = [
        Path.home() / ".config" / "kicad",
        Path.home() / "Library" / "Preferences" / "kicad",
    ]
    config_dirs = sorted(
        (d for root in roots if root.is_dir() for d in root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    if not config_dirs:
        raise SystemExit("no KiCad user config found; run KiCad once first")

    target = str(library_root())
    for config_dir in config_dirs:
        common = config_dir / "kicad_common.json"
        data = json.loads(common.read_text()) if common.exists() else {}
        env = data.setdefault("environment", {}).setdefault("vars", None)
        if env is None:
            data["environment"]["vars"] = {}
        data["environment"]["vars"][ENV_VAR] = target
        common.write_text(json.dumps(data, indent=2) + "\n")
        print(f"{common}: {ENV_VAR} = {target}")


if __name__ == "__main__":
    cli()
