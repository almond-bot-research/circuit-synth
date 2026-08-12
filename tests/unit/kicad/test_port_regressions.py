"""Regression tests for bugs found while porting the Almond boards.

Covers:
- circle graphics written in (center)/(end) form (easyeda-converted symbols)
- net connection identifiers must be pin numbers, not (non-unique) pin names
- KiCad netlist node refs must be bare designators, deduplicated
"""

import textwrap

import sexpdata

from circuit_synth.kicad.kicad_symbol_parser import _parse_graphic_element
from circuit_synth.kicad.netlist_exporter import generate_net_entry
from circuit_synth.kicad.sch_gen.symbol_geometry import SymbolBoundingBoxCalculator


def _parse_sexpr(text):
    return sexpdata.loads(text)


class TestCircleEndForm:
    def test_circle_with_end_gets_radius(self):
        elem = _parse_sexpr("(circle (center -5.08 3.81) (end -4.7 3.81))")
        shape = _parse_graphic_element(elem)
        assert shape["shape_type"] == "circle"
        assert shape["radius"] is not None
        assert abs(shape["radius"] - 0.38) < 1e-6

    def test_circle_with_radius_unchanged(self):
        elem = _parse_sexpr("(circle (center 0 0) (radius 1.27))")
        shape = _parse_graphic_element(elem)
        assert shape["radius"] == 1.27

    def test_bounding_box_tolerates_missing_radius(self):
        # Legacy cached data may still carry radius=None
        symbol_data = {
            "pins": [],
            "graphics": [
                {
                    "shape_type": "circle",
                    "points": [],
                    "start": None,
                    "end": None,
                    "center": [0.0, 0.0],
                    "radius": None,
                    "stroke_width": 0.0,
                    "stroke_type": "default",
                    "fill_type": "none",
                },
                {
                    "shape_type": "rectangle",
                    "points": [],
                    "start": [-6.35, 5.08],
                    "end": [6.35, -5.08],
                    "center": None,
                    "radius": None,
                    "stroke_width": 0.0,
                    "stroke_type": "default",
                    "fill_type": "background",
                },
            ],
        }
        width, height = SymbolBoundingBoxCalculator.get_symbol_dimensions(symbol_data)
        assert width > 0 and height > 0


class TestPinIdentifierPriority:
    def test_connections_use_pin_numbers(self, tmp_path):
        import json

        from circuit_synth.kicad.sch_gen.circuit_loader import load_circuit_hierarchy

        # Two pins share the name "IO" (like the ESD862's A/B channels); the
        # loader must keep them distinct by using the pin number.
        circuit_data = {
            "name": "test",
            "components": {},
            "nets": {
                "CAN_H": [
                    {"component": "D1", "pin": {"number": "1", "name": "IO", "type": "unspecified"}},
                    {"component": "D1", "pin": {"number": "1", "name": "IO", "type": "unspecified"}},
                ],
                "CAN_L": [
                    {"component": "D1", "pin": {"number": "2", "name": "IO", "type": "unspecified"}},
                ],
            },
            "subcircuits": [],
        }
        json_file = tmp_path / "test.json"
        json_file.write_text(json.dumps(circuit_data))
        top, _ = load_circuit_hierarchy(str(json_file))

        nets_iter = top.nets.values() if isinstance(top.nets, dict) else top.nets
        nets = {net.name: net for net in nets_iter}
        assert nets["CAN_H"].connections == [("D1", "1")]  # deduplicated, by number
        assert nets["CAN_L"].connections == [("D1", "2")]


class TestPinlessNetsExported:
    def test_parent_scope_keeps_pass_through_nets(self):
        from circuit_synth.core.circuit import Circuit
        from circuit_synth.core.decorators import set_current_circuit
        from circuit_synth.core.net import Net
        from circuit_synth.core.netlist_exporter import NetlistExporter

        # A net created at the top level but only used inside subcircuits
        # (e.g. a rail shared between two child sheets) must still appear in
        # the top scope's JSON: the schematic writer derives hierarchical
        # sheet pins from the parent scope's net list.
        top = Circuit("top")
        set_current_circuit(top)
        try:
            Net("V3V3")
        finally:
            set_current_circuit(None)

        data = NetlistExporter(top).to_dict()
        assert "V3V3" in data["nets"]
        assert data["nets"]["V3V3"]["nodes"] == []


class TestNetlistNodeRefs:
    def test_node_refs_are_bare_designators(self):
        nodes = [
            {"component": "/ldo_5v_3v3/U1", "pin": {"number": "1", "name": "IN", "type": "passive"}},
            {"component": "U1", "pin": {"number": "1", "name": "IN", "type": "passive"}},
            {"component": "C1", "pin": {"number": "2", "name": "2", "type": "passive"}},
        ]
        entry = generate_net_entry("/ldo_5v_3v3/V5V", nodes)
        node_refs = [
            item[1][1]
            for item in entry
            if isinstance(item, list) and item and item[0] == "node"
        ]
        # Path stripped and the duplicate (U1, pin 1) collapsed
        assert node_refs == ["U1", "C1"]

    def test_exported_netlist_has_bare_subcircuit_refs(self, tmp_path):
        import json
        import re

        from circuit_synth.kicad.netlist_exporter import convert_json_to_netlist

        circuit_data = {
            "name": "top",
            "components": {},
            "nets": {},
            "subcircuits": [
                {
                    "name": "child",
                    "components": {
                        "R1": {
                            "symbol": "Device:R",
                            "ref": "R1",
                            "value": "1k",
                            "footprint": "R0603:R0603",
                            "pins": [
                                {"pin_id": "1", "name": "1", "num": "1", "func": "passive", "unit": 1, "x": 0, "y": 0, "length": 2.54, "orientation": 0},
                                {"pin_id": "2", "name": "2", "num": "2", "func": "passive", "unit": 1, "x": 0, "y": 0, "length": 2.54, "orientation": 0},
                            ],
                        }
                    },
                    "nets": {
                        "SIG": [
                            {"component": "R1", "pin": {"number": "1", "name": "1", "type": "passive"}},
                            {"component": "R1", "pin": {"number": "1", "name": "1", "type": "passive"}},
                        ]
                    },
                    "subcircuits": [],
                }
            ],
        }
        json_file = tmp_path / "top.json"
        json_file.write_text(json.dumps(circuit_data))
        out = tmp_path / "top.net"
        convert_json_to_netlist(str(json_file), str(out))
        text = out.read_text()

        node_refs = re.findall(r'\(node \(ref "([^"]+)"\) \(pin "1"\)', text)
        assert node_refs.count("R1") == 1, text  # bare ref, deduplicated
        assert not [r for r in node_refs if "/" in r], text
