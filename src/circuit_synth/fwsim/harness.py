"""Run firmware, built as a shared library with `csrc/cs_sim.c`, against
Python models in simulated time.

    class MySim(FirmwareSim):
        def __init__(self):
            super().__init__("build/sim/libfw.so")
            self.chip = SomeModel(self.clock)
            self._spi = SPI_FN(self._on_spi)          # keep a reference
            self.lib.my_hal_set_spi_callback(self._spi)

        def _on_spi(self, tx, rx, n, now_ns):
            self.enter(now_ns)                        # models catch up first
            ...
            self.sync()                               # then publish pins / events

        def sync_pins(self):
            self.lib_u8("my_irq_pin").value = self.chip.int_n == 0

`run()` hands control to the firmware (`cs_sim_run`); it returns to Python
whenever the firmware touches the HAL or a model event is due.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from .clock import Clock

ADVANCE_FN = ctypes.CFUNCTYPE(None, ctypes.c_uint64)
_NO_EVENT = 0xFFFFFFFFFFFFFFFF

CSRC_DIR = Path(__file__).resolve().parent / "csrc"


class FirmwareSim:
    def __init__(self, lib: str | Path | ctypes.CDLL, clock: Clock | None = None):
        self.lib = lib if isinstance(lib, ctypes.CDLL) else ctypes.CDLL(str(lib))
        self.clock = clock or Clock()
        self.lib.cs_sim_run.argtypes = [ctypes.c_uint64]
        self.lib.cs_sim_stopped.restype = ctypes.c_bool
        self._now = ctypes.c_uint64.in_dll(self.lib, "cs_sim_now_ns")
        self._next_event = ctypes.c_uint64.in_dll(self.lib, "cs_sim_next_event_ns")
        self._advance = ADVANCE_FN(self._on_advance)
        self.lib.cs_sim_reset()
        self.lib.cs_sim_set_advance(self._advance)

    # --- firmware <-> models -------------------------------------------------
    def enter(self, now_ns: int):
        """Call first in every HAL callback: bring the models up to the
        firmware's time."""
        self.clock.advance_to(now_ns / 1000)

    def sync(self):
        """Call last in every HAL callback, and after poking a model from a
        test: publish pin states and the next model event to the firmware."""
        self.sync_pins()
        nxt = self.clock.next_event()
        # rounded up: the C side must not stop a fraction of a nanosecond short
        self._next_event.value = (
            _NO_EVENT if nxt is None else max(0, int(nxt * 1000) + 1)
        )

    def sync_pins(self):
        """Override: copy model outputs (interrupt lines, ...) into the HAL."""

    def _on_advance(self, now_ns: int):
        self.enter(now_ns)
        self.sync()

    def lib_var(self, name: str, ctype=ctypes.c_uint8):
        """A global variable of the firmware library."""
        return ctype.in_dll(self.lib, name)

    # --- running -----------------------------------------------------------------
    @property
    def now_ms(self) -> float:
        return self._now.value / 1e6

    @property
    def stopped(self) -> bool:
        return bool(self.lib.cs_sim_stopped())

    def run(self, ms: float):
        self.sync()
        self.lib.cs_sim_run(int(self._now.value / 1000 + ms * 1000))

    def run_until(self, cond, timeout_ms: float = 1000, step_ms: float = 0.2) -> bool:
        deadline = self.now_ms + timeout_ms
        while self.now_ms < deadline and not self.stopped:
            if cond():
                return True
            self.run(step_ms)
        return bool(cond())
