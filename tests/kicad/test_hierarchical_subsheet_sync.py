"""
Regression tests: a resync must reach hierarchical sub-sheets.

kicad-sch-api returns a schematic's sheet entries as dicts. When the
synchronizer only read them as objects, no sub-sheet was ever loaded, so a
component added inside a subcircuit never reached that subcircuit's sheet.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import kicad_sch_api as ksa

from circuit_synth import Component, Net, circuit
from circuit_synth.kicad.schematic.hierarchical_synchronizer import (
    HierarchicalSynchronizer,
)


def _design(with_cap: bool):
    @circuit(name="sub")
    def sub(vcc, gnd):
        r = Component("Device:R", ref="R", value="1k")
        r[1] += vcc
        r[2] += gnd
        if with_cap:
            c = Component("Device:C", ref="C", value="100nF")
            c[1] += vcc
            c[2] += gnd

    @circuit(name="main")
    def main():
        vcc, gnd = Net("VCC"), Net("GND")
        sub(vcc, gnd)

    return main()


class TestHierarchicalSubsheetSync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.project_dir = self.tmp / "proj"
        _design(with_cap=False).generate_kicad_project(
            str(self.project_dir), force_regenerate=True
        )

    def test_loads_subsheets(self):
        sync = HierarchicalSynchronizer(str(self.project_dir / "main.kicad_pro"))
        children = [(c.name, c.file_path.name) for c in sync.root_sheet.children]
        self.assertEqual(children, [("sub", "sub.kicad_sch")])

    def test_resync_adds_component_to_subsheet(self):
        _design(with_cap=True).generate_kicad_project(
            str(self.project_dir), force_regenerate=False
        )
        sub = ksa.Schematic.load(str(self.project_dir / "sub.kicad_sch"))
        refs = sorted(c.reference for c in sub.components)
        self.assertEqual(refs, ["C1", "R1"])


if __name__ == "__main__":
    unittest.main()
