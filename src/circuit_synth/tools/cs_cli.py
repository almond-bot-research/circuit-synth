"""The `cs` umbrella CLI: every recurring circuit-synth workflow as a command.

Agents and humans working in a design repo should reach for these instead of
writing ad-hoc scripts:

  cs build <design_dir>                regenerate schematic + netlists
  cs pcb sync ...                      update the .kicad_pcb from the schematic
  cs pcb apply-layouts ...             seed a board from package layouts
  cs pcb compare ...                   verify connectivity against a reference board
  cs pcb extract-footprints ...        lift footprints out of a board
  cs pcb fab ...                       export the assembler's quote package
  cs bom export ...                    write the BOM CSV
  cs bom verify ...                    check sourcing against DigiKey/Mouser
  cs parts search|detail|datasheet     pick parts from live distributor data
  cs parts import <LCSC_ID>            onboard a part (symbol/footprint/3D/datasheet)
  cs parts model <part_dir> --from ... install a manufacturer 3D model into a part
  cs parts min-symbol                  generate a minimal box symbol
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
    normalizes 3D model paths. Placement and routing are preserved.
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
    paste/fiducial/MPN issues the assembler would flag.
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
    """Write <build>.bom.csv from the build's canonical circuit JSON."""
    from circuit_synth.manufacturing.bom_csv import export

    export(Path(project_dir).resolve(), build_name)


@bom.command("verify")
@click.argument("builds", nargs=-1)
@click.option("--only", help="Check a single MPN.")
def bom_verify(builds: tuple[str, ...], only: str | None) -> None:
    """Check MPNs and distributor numbers against DigiKey/Mouser.

    BUILDS are <project_dir> <build_name> pairs; with none given, every
    layouts/<build>/<build>.json project under the current directory is
    checked. Exits non-zero when any part fails.
    """
    from circuit_synth.manufacturing.sourcing_check import verify

    if len(builds) % 2:
        raise SystemExit("BUILDS must be <project_dir> <build_name> pairs")
    pairs = [(Path(builds[i]), builds[i + 1]) for i in range(0, len(builds), 2)]
    sys.exit(verify(pairs, only=only))


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
