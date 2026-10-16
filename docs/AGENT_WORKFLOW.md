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

## Board design rules

Every `cs pcb sync` stamps the board conventions, so they never have to be
copied between boards by hand:

- JLCPCB fab minimums into `<build>.kicad_pro`
  (`board.design_settings.rules`), and a base `<build>.kicad_dru`
  (`pcb/scripts/fab_rules.py`). Copper pours are normalized to 0.2mm
  clearance / 0.1mm min width / solid pad connections, and the stamped
  `THT pads: thermal relief` rule overrides that for through-hole pads
  (0.5mm spokes, 0.3mm gap), so the convention is solid for SMD, thermal
  reliefs for through-hole: THT pins are hand/selective-wave soldered
  after reflow, and a solid tie to a plane sinks too much heat. The rule
  is per pad, so a mixed footprint (USB-C receptacle) keeps its SMD pads
  solid; a pad/footprint local zone-connection override still wins. Don't
  change pad connections per zone in pcbnew: sync resets every zone.
- The standard net classes (`pcb/scripts/design_rules.py`,
  `STANDARD_NET_CLASSES`) into `net_settings.classes`. Missing classes are
  added and drifted values of the standard ones are reset; other
  (design-specific) classes are left alone. `cs build` stamps them too.

  | Class           | Clearance | Track | Via (dia/drill) |
  |-----------------|-----------|-------|-----------------|
  | `Default`       | 0.13      | 0.2   | 0.6 / 0.3       |
  | `Power_24V`     | 0.3       | 3.0   | 1.2 / 0.6       |
  | `Power_24V_SRC` | 0.2       | 0.7   | 1.2 / 0.6       |
  | `Power_5V`      | 0.13      | 0.3   | 0.8 / 0.4       |

- DRC rules carried by part dirs: a part dir may ship a `rules.kicad_dru`
  fragment for rules that belong to the package rather than to a board
  (e.g. a clearance exception for a fine-pitch footprint). When any
  footprint from that lib is on the board, its rules are appended to
  `<build>.kicad_dru`. Scope the condition to the lib so the rule only
  fires on its part:

  ```
  (version 1)

  # Comment lines directly above a rule are copied with it.
  (rule "LM74502 fine-pitch pins"
    (condition "A.memberOfFootprint('TI_LM74502QDDFRQ1:*') || B.memberOfFootprint('TI_LM74502QDDFRQ1:*')")
    (constraint clearance (min 0.25mm)))
  ```

All `.kicad_dru` stamping is append-if-missing by rule name: an existing
rule is never rewritten (so per-board edits survive, and editing a rule at
its source does not reach boards that already have it - delete the rule
from the board's file and re-sync). Rule names must be unique across part
dirs.

Net-class assignments are per design (net names differ per board) and are
declared in the design's `build.py`:

```python
build_project(
    app(),
    DESIGN_DIR / "layouts",
    "default",
    parts_dirs=PARTS_DIRS,
    netclasses={
        "Power_24V": ["/V24_IN", "V24_F", "V24"],
        "Power_24V_SRC": ["/input_protection/SRC"],
        "Power_5V": ["VBUS", "VBUS_FILT"],
    },
)
```

Values are KiCad net-name patterns (`*`/`?` wildcards) matched against the
full board net name, as shown in pcbnew: hierarchical-label nets carry a
`/` prefix (`/V24_IN`), global/power nets do not (`VBUS`). The argument
replaces the project's `net_settings.netclass_patterns` on every build
(order kept); without it, patterns already in the `.kicad_pro` are left
alone. The class must be a standard class or one already defined in the
`.kicad_pro`; patterns that match no net in the netlist print a warning.
`cs pcb sync` never changes the assignments.

## Parts and sourcing

```bash
cs parts search "<query>" [--in-stock]           # DigiKey + Mouser keyword search
cs parts detail <MPN>                            # full distributor parameters
cs parts import <LCSC_ID> --out <parts_root>     # one-command part onboarding (below)
cs parts datasheet <MPN> --out <part_dir>        # download the datasheet PDF
cs parts min-symbol ...                          # generate minimal KiCad symbols
cs bom export <project_dir> <build>              # BOM CSV from the schematic
cs bom verify [<project_dir> <build>] [--only MPN] [--low-stock BOARDS]
cs docs snapshot <url> <out.md>                  # HTML -> markdown snapshot
```

`cs bom verify` also checks stock on the distributor numbers the BOM records
and prints it per part (`DigiKey 4,513,879 / Mouser 2,046,812`). A part
fails when its DigiKey listing (the primary source) has no stock, or when no
recorded listing has any. A backup listing (Mouser) at 0, or any listing
holding fewer than `--low-stock` boards' worth (default 100 x the part's
per-board quantity), is a warning. Lookup errors are warnings, never out of
stock.

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
