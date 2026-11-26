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
cs pcb route <project_dir> <build> [--lengths PATTERN]   # apply routing.py (hand-planned, below)
cs pcb pads|drc|render <project_dir> <build> ...  # pad positions, DRC with coordinates, a picture
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
- The library footprint is the source of truth for a footprint's 3D
  models: when a placed footprint's model list (file, offset, rotation,
  scale, opacity, visibility) differs from its library footprint's, sync
  replaces it in place and reports `3d models refreshed from the library: N
  footprint(s) (refs)`. Nothing else about the footprint changes, so a
  corrected model offset in a part dir needs no delete-and-re-add. A model
  file is looked up by filename in the footprint's own lib dir first and
  in the project's other lib dirs only after, so two part dirs may ship a
  model under the same filename. A library footprint none of whose models
  can be found leaves the board's list alone.

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
  added and drifted values of the standard ones are reset; classes the
  design declares in `build.py` are stamped the same way (below), and any
  other class is left alone. `cs build` stamps them too.

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

### A design's own net classes

A design that needs more than the standard classes declares them in its
`build.py`, with `net_class` from `circuit_synth.project`:

```python
from circuit_synth.project import build_project, net_class

NET_CLASSES = [
    # 50 ohm single-ended: 0.18 on F.Cu / B.Cu, 0.11 on the inner signal layers
    net_class("GMSL_50R", track_width=0.18, layers={"inner": {"track_width": 0.11}}),
    # 85 ohm pair: 0.19 / 0.15 outer, 0.13 / 0.15 inner
    net_class("USB3_85R", diff_pair_width=0.19, diff_pair_gap=0.15, diff_pair_via_gap=0.3,
              layers={"inner": {"diff_pair_width": 0.13}}),
]

build_project(
    app(), DESIGN_DIR / "layouts", "default", parts_dirs=PARTS_DIRS,
    netclass_defs=NET_CLASSES,
    netclasses={"GMSL_50R": ["/gmsl_?/COAX_?"], "USB3_85R": ["UPHY_RX*", "UPHY_TX*"]},
)
```

`net_class(name, clearance=, track_width=, via_diameter=, via_drill=,
diff_pair_width=, diff_pair_gap=, diff_pair_via_gap=, priority=0,
layers=)` takes mm; whatever is not given is the `Default` class's value.
`diff_pair_via_gap` is the copper gap between the two vias of a pair's
layer change. `layers` overrides `track_width`, `diff_pair_width`,
`diff_pair_gap` and `clearance` per layer, keyed `"outer"` (F.Cu and
B.Cu), `"inner"`, or a copper layer name such as `"In7.Cu"`; a layer name
wins over outer/inner. Via sizes are not per layer (vias are through
vias). When a net matches the patterns of several classes, the lowest
`priority` wins, then the name, as in KiCad. A declared class with a
standard class's name replaces it for that design.

Where the declaration goes:

- `<build>.kicad_pro`: each class, next to the standard ones. `cs build`
  and `cs pcb sync` both stamp them and reset drifted values, so edit
  `build.py`, not Board Setup.
- `<build>.kicad_dru`: KiCad's classes hold one value per field, so the
  per-layer values become generated rules named `net class <Class>:
  <scope>` (track width, clearance) and `net class <Class>: <scope> pair`
  (pair width and gap), each carrying a "Generated from the design's
  netclass_defs" comment. Unlike every other rule in the file these follow
  the Python: they are rewritten when the declaration changes and removed
  when it no longer has per-layer values, and they sit directly below the
  `(version 1)` line so that any board or part rule after them wins. Do not
  edit them; a hand-written rule without the comment is never touched.
  The values are stamped as optimums (`opt`), the way KiCad treats a
  class's own values: pcbnew's interactive router picks them up, and DRC
  keeps enforcing only the fab minimums, so a neck-down at a fine-pitch pad
  is not an error. Where a layer's pair gap is tighter than the clearance,
  a third rule (`... pair gap`) lets the two traces of a pair sit that
  close on that layer.
- `<build>.board_setup.json`: the declaration itself (with the layer count
  and stack-up below), written by `cs build` for the pcbnew-side tools
  and removed again when a design declares nothing. Commit it with the
  other build outputs.

`cs pcb route` takes its widths, pair gaps and via sizes from these classes
(next section).

### Copper layers and stack-up

A new board gets 4 copper layers unless the design says otherwise:

```python
from circuit_synth.project import build_project, dielectric, stackup

PREPREG = dielectric("prepreg", 0.1194, "S1000-2M 2116", 4.29, 0.018)   # type, mm, material, er, loss tangent
CORE = dielectric("core", 0.2, "S1000-2M", 4.42, 0.018)

build_project(
    ...,
    stackup=stackup([PREPREG, CORE] * 4 + [PREPREG], mask=0.03),   # 9 dielectrics: 10 copper layers
)
```

- `copper_layers=N` alone sets the layer count; `stackup=` implies it (give
  one or the other; they must agree).
- `stackup(dielectrics, outer_copper=0.035, inner_copper=0.0152, mask=0.01,
  finish="ENIG", impedance_controlled=True)` lists the dielectric between
  each pair of neighbouring copper layers from F.Cu down to B.Cu.
- `cs pcb sync` applies both: a new board is created with the declared
  layer count, an existing board with fewer layers is grown to it (copper
  on In1/In2 stays where it is; a board with more layers is only reported,
  because removing layers deletes copper), and the board file's
  `(stackup ...)` block is kept equal to the declaration. pcbnew's Python
  API cannot set the stack-up, so sync writes the block into the saved
  file; pcbnew then carries it through every later load/save. Repeating
  the sync changes nothing.
- KiCad computes nothing from the stack-up. It documents the build for the
  fab; the trace geometry that goes with it is what the net classes
  declare. The board thickness in Board Setup is not derived from it.

## Routing as code: `cs pcb route`

Routing is hand-planned, not autorouted: `<project_dir>/routing.py` defines
`route(r)` and lists every trace as waypoints; `cs pcb route` turns that
into tracks, vias and zones in one board group (`routing`) that is deleted
and rebuilt on every run. Hand-placed copper and package-layout groups are
left alone. Check the result with `cs pcb drc` and `cs pcb render`.

```python
def route(r):
    r.zone("GND", ["In1.Cu", "In3.Cu"], [(40, 40), (260, 40), (260, 170), (40, 170)])
    r.route("CAN0_TX", "U3.1", (125.0, 70.2), "U4.2")
    r.route("/gmsl_a/COAX_A", "J13.1", (202.0, 158.6), "via:In7.Cu",
            (188.25, 131.75), "via:F.Cu", "C242.1")
    r.pair(("UPHY_RX1_P", "UPHY_RX1_N"), ("J22.A22", "J22.A23"),
           (140.15, 100.6), (140.15, 101.9), "via:In2.Cu",
           (140.15, 66.6), "via:F.Cu",
           (140.15, 65.21), (139.2, 64.26), "ds", ("U13.9", "U13.10"))
    r.stitch("GND", "C11.2", dx=0.9)
```

| Call | What it makes |
|---|---|
| `r.route(net, *items, layer="F.Cu", width=None, via=None)` | one trace through waypoints, joined with 45 degree doglegs |
| `r.pair((p_net, n_net), *items, layer="F.Cu", width=None, gap=None, via=None, via_gap=None, p=None)` | both traces of a differential pair along one centre-line; returns the lengths and skew |
| `r.track(net, points, layer="F.Cu", width=None)` | an exact polyline, no dogleg expansion |
| `r.via(net, at, size=None, drill=None)` | one via |
| `r.stitch(net, pad, dx, dy)` | pad, short stub, via: the usual plane connection |
| `r.zone(net, layers, points, ...)` / `r.keepout(layers, points, ...)` | a pour / a rule area |
| `r.at("REF.PAD", dx, dy)`, `r.pad("REF.PAD")`, `r.layers`, `r.lengths()` | lookups |

Items of `route`:

- A waypoint: `"REF.PAD"` (the pad centre; the pad must be on the net;
  `REF.PAD#2` for a pin's second pad), `(x, y)` in board mm, or
  `r.at("REF.PAD", dx, dy)`.
- `"sd"` / `"ds"` before a waypoint: how the hop to it bends when the two
  points are not aligned. `"sd"` (the default) runs straight first and
  finishes on the diagonal, `"ds"` the other way round.
- `"via:<layer>"`: drop a via at the current point and continue on that
  layer, e.g. `"via:In7.Cu"`. A bare `"via"` swaps F.Cu and B.Cu.
- A layer name on its own: continue on that layer without a via (to start
  on a through-hole pad's inner or back layer).

Layers are the board's own enabled copper layers; any other name is a
routing error that lists the valid ones. Vias are through vias whatever two
layers they join (the fab's standard service), so an inner-layer hop leaves
a stub to the far side of the board.

Differential pairs (`r.pair`): the items are `(x, y)` waypoints of the
pair's centre-line and the same tokens. The two traces run `width + gap`
apart centre to centre, with mitred corners, so the gap holds round every
bend and the trace on the outside of a bend is the longer one.

- Pads: a `(P pad, N pad)` tuple as the first and/or last item. The pair is
  coupled from the first centre-line waypoint to the last; each pad is
  joined to its trace's end of that section by a dogleg in the bend mode in
  effect (`"sd"`: straight out of the pad, then a diagonal onto the trace).
  Put the first waypoint where the pair should become coupled.
- Which side P runs on follows from its start pad. Pass `p="left"` or
  `p="right"` (of the direction of travel) when the route does not start
  at pads, or the pads are in line with it. A pair that would have to cross
  itself to reach its end pads is an error.
- Layer change: at `"via:<layer>"` the traces fan out on 45 degree
  diagonals to two vias side by side across the direction of travel, their
  copper `diff_pair_via_gap` apart, and fan back in on the new layer. The
  pair therefore has to leave its vias straight on or straight back, and
  needs a centre-line segment on each side of them. Mind the fab's
  hole-to-hole minimum when choosing the via gap (0.6/0.3 vias need 0.2 or
  more).
- Every run prints `P <mm>, N <mm>, skew <mm>, <n> via pair(s)` per pair
  (skew is P minus N; track length, via barrels not counted). A 45 degree
  bend costs 0.83 x (width + gap) of skew; opposite bends cancel. There is
  no automatic length matching: add the detour where you want it.

Widths, gaps and via sizes: a number passed in the call always wins.
Otherwise `route`, `track`, `stitch` and `via` use the Router-wide
`r.width` / `r.via_size` if set, else the net's class: `track_width` and
`via_diameter` / `via_drill`, and for pairs `diff_pair_width`,
`diff_pair_gap` and `diff_pair_via_gap`, each with the class's override for
the layer the segment is on. One call therefore follows the per-layer
impedance geometry through its layer changes, and the routing file names no
numbers. `r.width` and `r.via_size` start as 0.2 and (0.6, 0.3) for a design
that declares no net classes of its own, so routing files written before
net classes drove the Router produce the same copper as before, and as
`None` (every net takes its class's geometry, the standard classes
included) for a design with `netclass_defs`. Setting them in the routing
file overrides either way.

Lengths: `cs pcb route <project_dir> <build> --lengths 'UPHY_*'`
(repeatable; `'*'` for every net) prints each matching net's routed length
in total and per layer, and its via count, over all copper on the board and
not only the routing group. `r.lengths()` returns the same numbers inside a
routing file, e.g. to check a link budget.

```
  pairs (mm; skew = P - N):
    UPHY_RX1_P / UPHY_RX1_N: P 42.164, N 42.139, skew +0.025, 2 via pair(s)
  lengths (mm), 3 net(s):
    net                total  vias  per layer
    /gmsl_a/COAX_A    41.745     2  F.Cu 3.910, In7.Cu 32.545, B.Cu 5.290
    UPHY_RX1_N        42.139     2  F.Cu 6.583, In2.Cu 35.557
    UPHY_RX1_P        42.164     2  F.Cu 6.607, In2.Cu 35.557
```

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
