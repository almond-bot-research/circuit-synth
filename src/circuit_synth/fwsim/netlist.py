"""Reader for KiCad netlists (the s-expression `.net` format).

A simulation should take its wiring from the netlist, not from a
hand-written table: which chip sits on which chip select, which pin drives
which load.  Then a pin-map mistake in the firmware, or a change in the
schematic, changes what the simulated firmware talks to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def _parse(text: str):
    tokens = _TOKEN.findall(text)
    pos = 0

    def read():
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok != "(":
            return tok[1:-1] if tok.startswith('"') else tok
        items = []
        while tokens[pos] != ")":
            items.append(read())
        pos += 1
        return items

    return read()


def _children(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def _value(node, key):
    found = _children(node, key)
    return found[0][1] if found and len(found[0]) > 1 else None


@dataclass
class Component:
    ref: str
    value: str
    address: str | None
    pins: dict[str, str] = field(default_factory=dict)  # pin number -> net
    pin_names: dict[str, str] = field(default_factory=dict)  # pin number -> function

    def net(self, pin) -> str:
        return self.pins[str(pin)]

    def pin_by_name(self, name: str) -> str:
        """Pin number whose function name contains `name`."""
        hits = [p for p, fn in self.pin_names.items() if fn and name in fn]
        if len(hits) != 1:
            raise KeyError(f"{self.ref}: {len(hits)} pins match {name!r}")
        return hits[0]


@dataclass
class Netlist:
    components: dict[str, Component]
    nets: dict[str, list[tuple[str, str]]]  # net -> [(ref, pin)]

    def by_address(self, address: str) -> Component:
        hits = [c for c in self.components.values() if c.address == address]
        if len(hits) != 1:
            raise KeyError(f"{len(hits)} components at address {address!r}")
        return hits[0]

    def by_value(self, needle: str) -> list[Component]:
        return [c for c in self.components.values() if needle in c.value]

    def others_on(self, net: str, ref: str) -> list[tuple[str, str]]:
        return [(r, p) for r, p in self.nets[net] if r != ref]


def load(path: str | Path) -> Netlist:
    """Read a KiCad netlist (the `.net` file `cs build` writes next to a layout)."""
    tree = _parse(Path(path).read_text())
    components: dict[str, Component] = {}
    for comp in _children(_children(tree, "components")[0], "comp"):
        props = {
            _value(p, "name"): _value(p, "value") for p in _children(comp, "property")
        }
        ref = _value(comp, "ref")
        components[ref] = Component(
            ref, _value(comp, "value") or "", props.get("address")
        )
    nets: dict[str, list[tuple[str, str]]] = {}
    for net in _children(_children(tree, "nets")[0], "net"):
        name = _value(net, "name")
        nodes = []
        for node in _children(net, "node"):
            ref, pin = _value(node, "ref"), _value(node, "pin")
            nodes.append((ref, pin))
            components[ref].pins[pin] = name
            components[ref].pin_names[pin] = _value(node, "pinfunction") or ""
        nets[name] = nodes
    return Netlist(components, nets)


class Board:
    """Questions a simulation asks of a netlist."""

    def __init__(self, netlist: Netlist | str | Path):
        self.net = netlist if isinstance(netlist, Netlist) else load(netlist)

    def one(self, value: str) -> Component:
        """The only component whose value contains `value`."""
        hits = self.net.by_value(value)
        if len(hits) != 1:
            raise KeyError(f"{len(hits)} components match {value!r}")
        return hits[0]

    def gpio_nets(
        self, mcu: Component, pattern: str = r"GPIO(\d+)(?:_\w+)?"
    ) -> dict[int, str]:
        """GPIO number -> net, from the MCU's pin function names."""
        out: dict[int, str] = {}
        for pin, fn in mcu.pin_names.items():
            m = re.fullmatch(pattern, fn or "")
            if m:
                out[int(m.group(1))] = mcu.pins[pin]
        return out

    def on_net(self, net: str) -> list[Component]:
        return [self.net.components[ref] for ref, _ in self.net.nets[net]]

    def chip_on(self, net: str, pin_name: str, value: str) -> Component | None:
        """The component of type `value` whose pin named `pin_name` is on `net`
        (None if there is none, or more than one)."""
        hits = [
            c
            for c in self.net.by_value(value)
            if any(
                c.pins[p] == net and pin_name in (c.pin_names[p] or "") for p in c.pins
            )
        ]
        return hits[0] if len(hits) == 1 else None

    def has_pull(self, net: str, rail: str) -> bool:
        """A resistor from `net` to `rail` (e.g. "GND", "V3V3")."""
        return any(
            c.ref.startswith("R") and rail in c.pins.values() for c in self.on_net(net)
        )

    @staticmethod
    def unconnected(net: str) -> bool:
        return net.startswith("unconnected")
