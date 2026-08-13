# Agent Workflow: the `cs` CLI

`cs` is the single entrypoint for building circuit projects, syncing boards,
and sourcing parts. Design repos (e.g. circuits-py) contain only designs;
everything an agent needs to operate on them is a `cs` subcommand - prefer
these over ad-hoc scripts.

## Build and layout

```bash
cs build <dir>                                   # run a design/package build.py
cs pcb sync <project_dir> <build>                # schematic -> .kicad_pcb (keeps placement)
cs pcb apply-layouts <project_dir> <build> inst=layout:prefix ...
cs pcb compare <ref.kicad_pcb> <project_dir> <build> <map.json>
cs pcb extract-footprints <board> ref=out_dir:name ...
cs kicad-py <script> [args]                      # escape hatch: run under pcbnew Python
cs setup-kicad                                   # register ${CIRCUIT_SYNTH_LIB} in KiCad
```

- `cs build` wraps `circuit_synth.project.build_project`: byte-stable
  schematic generation, deterministic net labels, footprint property sync,
  lib-table writing, and `kicad-cli` netlist export. Rebuilding an unchanged
  circuit produces byte-identical files.
- `cs pcb ...` commands re-exec themselves under a pcbnew-capable Python
  (KiCad's bundled interpreter on macOS, the system one on Linux). Override
  discovery with `KICAD_PYTHON` / `KICAD_CLI`.
- `cs pcb sync` preserves manual placement, routing, and text tweaks
  (moved/hidden silk refs) even when a component's footprint id changes; 3D
  model paths are rewritten to stable `${CIRCUIT_SYNTH_LIB}` /
  `${KIPRJMOD}` references.

## Parts and sourcing

```bash
cs parts search "<query>" [--in-stock]           # DigiKey + Mouser keyword search
cs parts detail <MPN>                            # full distributor parameters
cs parts import <LCSC_ID> --out <parts_root>     # one-command part onboarding (below)
cs parts datasheet <MPN> --out <part_dir>        # download the datasheet PDF
cs parts min-symbol ...                          # generate minimal KiCad symbols
cs bom export <project_dir> <build>              # BOM CSV from the schematic
cs bom verify [<project_dir> <build>] [--only MPN]
cs docs snapshot <url> <out.md>                  # HTML -> markdown snapshot
```

Credentials live in `~/.circuit_synth/credentials` (dotenv format):

```
DIGIKEY_CLIENT_ID=...       # https://developer.digikey.com
DIGIKEY_CLIENT_SECRET=...
MOUSER_API_KEY=...          # https://www.mouser.com/api-hub
```

Missing credentials degrade gracefully (the affected distributor is skipped
with a warning). Never commit credentials to a repo.

## Standard library and part conventions

- `circuit_synth/library/footprints/` ships generic passives (`R0603`,
  `C0603`, `C0805`, `C1206`, `L0603`, `LED0603`) and generic IC packages
  (`Standard_Packages`: SOT-23-3/5/8, SOT-563, SOD-323, SOIC-8, 3.2x2.5
  crystal), each with co-located `.step`/`.wrl` models. Reference them as
  `<Lib>:<Footprint>` (e.g. `Standard_Packages:SOT-23-5_L3.0-W1.7-P0.95-LS2.8-BR`);
  `build_project` adds the lib-table entries automatically.
- Unique parts live in a design repo part dir with everything co-located:
  `parts/<Part>/{*.kicad_sym, *.kicad_mod, *.step, *.wrl, <MPN>.pdf}`.
  Footprint `(model ...)` entries are bare filenames; `cs pcb sync`
  normalizes them. Every IC part dir carries its datasheet PDF.

## Adding a new part

After vetting the part with `cs parts search`/`detail` (spec, stock, price),
find its LCSC id on lcsc.com and run:

```bash
cs parts import C165948 --out packages/<pkg>/parts
```

This fetches the EasyEDA CAD data and writes a complete part directory to
the conventions above: `<Manufacturer>_<MPN>/` with the symbol lib
(`<MPN>.kicad_sym`, footprint lib nickname = directory name), the footprint
with its `.step`/`.wrl` models referenced by bare filename, and the
datasheet PDF. When the package already exists in the bundled standard
libraries the symbol references it (e.g. `Standard_Packages:SOT-23-5_...`)
and no local footprint is written. The command prints a ready-to-paste
`Component(...)` snippet for `parts.py`. Use `--name` to override the
directory name (e.g. when the manufacturer string is non-ASCII) and
`--overwrite` to refresh an existing part.
