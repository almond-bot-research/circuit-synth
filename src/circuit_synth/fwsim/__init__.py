"""Firmware-in-the-loop simulation for circuit-synth designs.

Run a board's real firmware on Linux, against models of the board's parts,
wired the way the netlist says.  It finds logic, sequencing and pin-map
errors before a board exists; it says nothing about analog behaviour or
timing margins, and nothing about mistakes the models share with the
firmware.

    netlist     read a KiCad netlist and trace it (Board)
    pinmap      check a firmware header's pin macros against the netlist
    clock       simulated time and events
    canbus      frame-level CAN / CAN FD bus with arbitration, ACK, errors
    parts       device models written from datasheets (`cs sim parts`)
    harness     FirmwareSim: firmware as a shared library + csrc/cs_sim.c
    runner      find and run a design's simulation suites (`cs sim run`)

See docs/FIRMWARE_SIM.md.
"""

from .canbus import CanBus, Frame, Node, SimNode
from .clock import Clock
from .harness import CSRC_DIR, FirmwareSim
from .netlist import Board, Netlist
from .netlist import load as load_netlist

__all__ = [
    "Board",
    "CSRC_DIR",
    "CanBus",
    "Clock",
    "FirmwareSim",
    "Frame",
    "Netlist",
    "Node",
    "SimNode",
    "load_netlist",
]
