"""One-command part onboarding from LCSC/EasyEDA (cs parts import).

Given an LCSC id (Cxxxxx), fetches the EasyEDA CAD data and writes a complete
part directory following the repo conventions:

    <out>/<Manufacturer>_<MPN>/
        <MPN>.kicad_sym       symbol lib (nickname = file stem, symbol = MPN)
        <footprint>.kicad_mod unless the package exists in a standard library
        <footprint>.step/.wrl 3D models, referenced by bare filename
        <MPN>.pdf             datasheet

Footprint lib nickname = part directory name. When the footprint already
exists in a bundled standard library (Standard_Packages, C0603, ...), the
symbol references that library instead and no local copy is written.
Conversion is delegated to easyeda2kicad; this module only applies the
directory and naming conventions on top.
"""

from __future__ import annotations

import re
from pathlib import Path

from circuit_synth.library import footprint_lib_dirs

_MODEL_DIR_TOKEN = "__CS_MODEL_DIR__"


def sanitize(name: str) -> str:
    """Directory-safe name: runs of non-alphanumerics become underscores."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def standard_footprint_lib(footprint_name: str) -> str:
    """Return the bundled standard lib nickname carrying this footprint, if any."""
    for lib_dir in footprint_lib_dirs():
        if (lib_dir / f"{footprint_name}.kicad_mod").is_file():
            return lib_dir.name
    return ""


def _strip_model_block(text: str) -> str:
    """Remove the (model ...) block from a footprint s-expression."""
    start = text.find("(model ")
    if start == -1:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[:start].rstrip() + "\n" + text[i + 1 :].lstrip("\n\t ")
    return text


def _fixup_footprint(path: Path, model_file: str) -> None:
    """Apply repo conventions to an easyeda2kicad-exported .kicad_mod.

    - drop the "easyeda2kicad:" lib prefix baked into the module name
    - reference the 3D model by bare filename (cs pcb sync rewrites it to a
      stable ${CIRCUIT_SYNTH_LIB}/${KIPRJMOD} path), or drop the block when
      no model could be downloaded
    """
    text = path.read_text()
    text = text.replace("(module easyeda2kicad:", "(module ", 1)
    text = text.replace('(footprint "easyeda2kicad:', '(footprint "', 1)
    if model_file:
        text = re.sub(
            r'\(model "' + re.escape(_MODEL_DIR_TOKEN) + r'/[^"]*"',
            f'(model "{model_file}"',
            text,
        )
    else:
        text = _strip_model_block(text)
    path.write_text(text)


def import_part(
    lcsc_id: str,
    out_root: Path,
    name: str = "",
    datasheet: bool = True,
    overwrite: bool = False,
) -> int:
    from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
    from easyeda2kicad.easyeda.easyeda_importer import (
        Easyeda3dModelImporter,
        EasyedaFootprintImporter,
        EasyedaSymbolImporter,
    )
    from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
    from easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
    from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad

    lcsc_id = lcsc_id.upper()
    if not re.fullmatch(r"C\d+", lcsc_id):
        print(f"{lcsc_id!r} does not look like an LCSC id (C12345); find it on lcsc.com")
        return 1

    api = EasyedaApi()
    cad_data = api.get_cad_data_of_component(lcsc_id=lcsc_id)
    if not cad_data:
        print(f"EasyEDA has no CAD data for {lcsc_id}")
        return 1

    symbol = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data).get_symbol()
    footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()
    mpn = symbol.info.mpn or symbol.info.name
    manufacturer = symbol.info.manufacturer

    part_name = name or sanitize(f"{manufacturer}_{mpn}")
    part_dir = Path(out_root) / part_name
    if part_dir.exists() and any(part_dir.iterdir()) and not overwrite:
        print(f"{part_dir} already exists; pass --overwrite to replace its files")
        return 1
    part_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- footprint
    std_lib = standard_footprint_lib(footprint.info.name)
    footprint_lib = std_lib or part_name
    model_file = ""
    if not std_lib:
        model = Easyeda3dModelImporter(
            easyeda_cp_cad_data=cad_data, download_raw_3d_model=True, api=api
        ).output
        if model is not None:
            Exporter3dModelKicad(model_3d=model).export(
                output_dir=str(part_dir), overwrite=True
            )
            for ext in ("step", "wrl"):
                if (part_dir / f"{model.name}.{ext}").is_file():
                    model_file = f"{model.name}.{ext}"
                    break
        fp_path = part_dir / f"{footprint.info.name}.kicad_mod"
        ExporterFootprintKicad(footprint=footprint).export(
            footprint_full_path=str(fp_path),
            model_3d_path=_MODEL_DIR_TOKEN,
            model_3d_extension=model_file.rsplit(".", 1)[-1] if model_file else "step",
        )
        _fixup_footprint(fp_path, model_file)

    # ---------------------------------------------------------------- symbol
    symbol.info.name = mpn
    symbol.info.package = footprint.info.name
    sym_path = part_dir / f"{mpn}.kicad_sym"
    if sym_path.exists():
        sym_path.unlink()  # save_to_lib appends to existing libs; one part per file
    ExporterSymbolKicad(symbol=symbol, lib_path=str(sym_path)).save_to_lib(
        lib_path=str(sym_path), footprint_lib_name=footprint_lib, overwrite=True
    )

    # ------------------------------------------------------------- datasheet
    datasheet_note = "skipped"
    if datasheet:
        from .catalog import download
        from .part_search import _datasheet_url

        pdf_path = part_dir / (re.sub(r"[^A-Za-z0-9._-]+", "_", mpn) + ".pdf")
        for url in filter(None, [_datasheet_url(mpn), symbol.info.datasheet]):
            if url.startswith("//"):
                url = "https:" + url
            try:
                download(url, pdf_path)
                datasheet_note = f"{pdf_path.name}  ({url})"
                break
            except Exception as exc:
                print(f"datasheet fetch failed from {url}: {exc}")
        else:
            datasheet_note = "unavailable; try cs parts datasheet or the manufacturer site"

    # ---------------------------------------------------------------- report
    print(f"{lcsc_id}: {manufacturer} {mpn} -> {part_dir}")
    print(f"  symbol:    {sym_path.name}")
    if std_lib:
        print(f"  footprint: {std_lib}:{footprint.info.name} (bundled standard lib, no local copy)")
    else:
        print(f"  footprint: {footprint.info.name}.kicad_mod")
        print(f"  3d model:  {model_file or 'none available'}")
    print(f"  datasheet: {datasheet_note}")
    ref = (symbol.info.prefix or "U").rstrip("?")
    print("\nparts.py snippet:")
    print(
        f'    Component(\n        symbol="{mpn}:{mpn}",\n        ref="{ref}",\n'
        f'        footprint="{footprint_lib}:{footprint.info.name}",\n    )'
    )
    return 0
