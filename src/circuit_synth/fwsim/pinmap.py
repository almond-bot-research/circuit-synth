"""Pin-map conformance: a firmware header against the board's netlist.

The convention: the firmware names every MCU pin it uses `<PREFIX><net>`
where `<net>` is the net name in the netlist, e.g.

    #define PIN_CAN0_CS_N   29      // net CAN0_CS_N is on GPIO29

`check_pin_macros` then fails for a macro with the wrong number, a net on
the MCU that has no macro, and a macro whose net does not exist.  Design
specific checks (which chip is on which chip select, pull resistors,
clocks) build on `netlist.Board`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .netlist import Board, Component

_DEFINE = re.compile(r"^#define\s+(\w+)\s+(\w+?)[uUlL]*\s*(?://.*|/\*.*)?$", re.M)


def header_defines(path: str | Path) -> dict[str, int]:
    """Numeric `#define`s of a C header (decimal or hex; names that alias
    another numeric macro are resolved)."""
    out: dict[str, int] = {}
    for name, value in _DEFINE.findall(Path(path).read_text()):
        if value in out:
            out[name] = out[value]
            continue
        try:
            out[name] = int(value, 0)
        except ValueError:
            pass
    return out


def header_int_list(path: str | Path, macro: str) -> set[int] | None:
    """`#define MACRO { 1, 2, 3 }` as a set, or None if it is not there."""
    m = re.search(
        rf"#define\s+{re.escape(macro)}\s*\{{([^}}]*)\}}", Path(path).read_text()
    )
    if not m:
        return None
    return {int(x, 0) for x in m.group(1).split(",") if x.strip()}


def check_pin_macros(
    defines: dict[str, int],
    gpio_net: dict[int, str],
    *,
    prefix: str = "PIN_",
    unused: set[int] | None = None,
) -> list[str]:
    """Compare `<prefix><net>` macros with the MCU's GPIO nets.

    `unused`, if given, is the firmware's list of GPIOs it treats as not
    connected; it must equal the unconnected GPIOs of the netlist.
    Returns a list of problems (empty = conformant).
    """
    problems: list[str] = []
    connected = {g: n for g, n in gpio_net.items() if not Board.unconnected(n)}
    for gpio, net in sorted(connected.items()):
        macro = prefix + net
        if macro not in defines:
            problems.append(
                f"net {net} is on GPIO{gpio} but the firmware has no {macro}"
            )
        elif defines[macro] != gpio:
            problems.append(
                f"{macro} = {defines[macro]} but net {net} is on GPIO{gpio}"
            )
    for macro, gpio in defines.items():
        if macro.startswith(prefix) and macro[len(prefix) :] not in connected.values():
            problems.append(
                f"{macro} = {gpio}: the netlist has no MCU net {macro[len(prefix):]}"
            )
    if unused is not None:
        floating = {g for g, n in gpio_net.items() if Board.unconnected(n)}
        if unused != floating:
            problems.append(
                f"unused GPIOs {sorted(unused)} != unconnected GPIOs in the netlist {sorted(floating)}"
            )
    return problems


# --- RP2040 / RP2350 --------------------------------------------------------------
# Function select F1 (SPI) of GPIO0..29: (SPI instance, signal).  The pattern
# is the same on the RP2040 and the RP2350 (datasheets, "GPIO functions").
_SPI_ROLES = ["RX", "CSn", "SCK", "TX"]
RP2_SPI_F1 = {
    gpio: (0 if (gpio // 8) % 2 == 0 else 1, _SPI_ROLES[gpio % 4]) for gpio in range(30)
}


def check_rp2_spi(
    name: str, instance: int, sck: int, mosi: int, miso: int
) -> list[str]:
    """Is hardware SPI `instance` available on these three GPIOs?"""
    problems = []
    for sig, gpio, role in (
        ("SCK", sck, "SCK"),
        ("MOSI", mosi, "TX"),
        ("MISO", miso, "RX"),
    ):
        have = RP2_SPI_F1.get(gpio)
        if have != (instance, role):
            what = f"SPI{have[0]} {have[1]}" if have else "not a GPIO of this package"
            problems.append(
                f"{name}_{sig} on GPIO{gpio}: that pin is {what}, not SPI{instance} {role}"
            )
    return problems


def mcu_gpio_nets(board: Board, mcu_value: str) -> tuple[Component, dict[int, str]]:
    mcu = board.one(mcu_value)
    return mcu, board.gpio_nets(mcu)
