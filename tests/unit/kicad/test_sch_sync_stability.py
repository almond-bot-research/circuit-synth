"""Regression tests: adding/removing parts must not disturb the rest of a design.

Found when a part was added to a laid-out board (circuits-py jelly_legs):

- designators renumbered (everything declared after the new part shifted),
  and the sync matched by designator, so symbols swapped values, footprints,
  and fields
- fallback strategies recycled a deleted part's symbol for an unrelated part
- new symbols were added without their user fields (address, MPN, ...)
- matched symbols never picked up sourcing-field changes
- EDGE_RIGHT placed several new symbols on top of each other, shorting nets
"""

from types import SimpleNamespace

from circuit_synth.kicad.schematic.placement import PlacementEngine, PlacementStrategy
from circuit_synth.kicad.schematic.sync_strategies import (
    AddressMatchStrategy,
    circuit_user_fields,
)
from circuit_synth.kicad.schematic.synchronizer import APISynchronizer
from circuit_synth.project.address import finalize_design


def _comp(ref, address=None):
    fields = {"address": address} if address else {}
    return SimpleNamespace(ref=ref, _extra_fields=fields)


def _circuit(comps, subcircuits=()):
    return SimpleNamespace(
        name="c",
        _components={c.ref: c for c in comps},
        _subcircuits=list(subcircuits),
    )


class TestKeepRefs:
    def test_inserted_part_does_not_renumber_existing(self):
        # Python order assigned R1..R3 with the new part in the middle
        old_a, new, old_b = _comp("R1", "a"), _comp("R2", "new"), _comp("R3", "b")
        circuit = _circuit([old_a, new, old_b])
        finalize_design(circuit, keep_refs={"a": "R1", "b": "R2"})
        assert (old_a.ref, old_b.ref) == ("R1", "R2")
        assert new.ref == "R3"

    def test_removed_part_designator_is_not_reused(self):
        kept, new = _comp("R1", "kept"), _comp("R2", "new")
        circuit = _circuit([kept, new])
        # "gone" (R2) was deleted from Python but is still in the schematic
        finalize_design(circuit, keep_refs={"kept": "R1", "gone": "R2"})
        assert kept.ref == "R1"
        assert new.ref == "R3"

    def test_prefix_change_is_not_pinned(self):
        # A resistor replaced by a capacitor at the same address
        comp = _comp("C1", "x")
        circuit = _circuit([comp, _comp("C2", "y")])
        finalize_design(circuit, keep_refs={"x": "R7", "y": "C1"})
        assert comp.ref == "C2"

    def test_subcircuit_components_keep_refs(self):
        sub_comp = _comp("C1", "cap")
        sub = _circuit([sub_comp])
        sub.name = "ldo"
        top = _circuit([_comp("C2", "top_cap")], [sub])
        finalize_design(top, keep_refs={"ldo.cap": "C5", "top_cap": "C9"})
        assert sub_comp.ref == "C5"
        assert set(top._components) == {"C9"}
        assert set(sub._components) == {"C5"}

    def test_no_previous_schematic_keeps_python_order(self):
        a, b = _comp("R1", "a"), _comp("R2", "b")
        finalize_design(_circuit([a, b]), keep_refs={})
        assert (a.ref, b.ref) == ("R1", "R2")


def _kicad(ref, address, **props):
    properties = {"address": {"name": "address", "value": address}} if address else {}
    for name, value in props.items():
        properties[name] = {"name": name, "value": value}
    data = SimpleNamespace(reference=ref, properties=properties, hidden_properties=set())
    return SimpleNamespace(
        reference=ref, properties=properties, _data=data, _collection=None
    )


def _circuit_comp(ref, address, **fields):
    original = SimpleNamespace(properties={"address": address, **fields})
    return {"reference": ref, "original": original}


class TestAddressMatching:
    def test_matches_by_address_across_designator_shift(self):
        circuit = {"R28": _circuit_comp("R28", "vsense_top")}
        kicad = {"R24": _kicad("R24", "vsense_top"), "R28": _kicad("R28", "hall_pullup")}
        assert AddressMatchStrategy().match_components(circuit, kicad) == {"R28": "R24"}

    def test_address_conflict_blocks_fallback_match(self):
        assert APISynchronizer._addresses_conflict(
            _circuit_comp("R41", "uvlo_bottom"), _kicad("R23", "old_gate_res")
        )
        assert not APISynchronizer._addresses_conflict(
            _circuit_comp("R41", "x"), _kicad("R41", "x")
        )

    def test_user_fields_skip_bookkeeping(self):
        comp = _circuit_comp(
            "R1",
            "a",
            Partnumber="RC0603",
            hierarchy_path="/x",
            _circuit_synth_hierarchy_path="/",
            ki_keywords="res",
        )
        assert circuit_user_fields(comp) == {"address": "a", "Partnumber": "RC0603"}


class TestUserFieldSync:
    def test_updates_loaded_property_in_place(self):
        kicad = _kicad("R1", "a", Partnumber="OLD")
        prop = kicad.properties["Partnumber"]
        assert APISynchronizer._set_user_property(kicad, "Partnumber", "NEW")
        # Same dict object: the writer keeps its position / visibility
        assert kicad.properties["Partnumber"] is prop
        assert prop["value"] == "NEW"
        assert not APISynchronizer._set_user_property(kicad, "Partnumber", "NEW")

    def test_new_property_is_hidden(self):
        kicad = _kicad("R1", "a")
        assert APISynchronizer._set_user_property(kicad, "Mouser", "123")
        assert kicad.properties["Mouser"] == "123"
        assert "Mouser" in kicad._data.hidden_properties


class TestEdgePlacement:
    def test_successive_parts_do_not_overlap(self):
        schematic = SimpleNamespace(components=[])
        engine = PlacementEngine(schematic)
        engine._estimate_component_size = lambda comp: (20.0, 10.0)

        def place():
            x, y = engine.find_position(PlacementStrategy.EDGE_RIGHT, component=object())
            schematic.components.append(SimpleNamespace(position=SimpleNamespace(x=x, y=y)))
            return x

        xs = [place() for _ in range(3)]
        assert all(b - a >= 20.0 for a, b in zip(xs, xs[1:]))
