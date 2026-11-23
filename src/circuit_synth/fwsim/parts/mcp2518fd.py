"""Behavioural model of the Microchip MCP2518FD CAN FD controller, written
from the datasheet (DS20006027B).  Section / register numbers in the
comments refer to that document.

It is meant to be driven by a firmware driver written in another language
and shares no definitions with any driver: every address, bit position and
reset value below was typed from the datasheet.

What is modelled
- SPI instructions RESET, READ, WRITE (section 4, table 4-1), byte-wise SFR
  access with address increment, word-wise RAM access.  The CRC instructions
  are not modelled (the firmware does not use them) and are reported.
- The register file: OSC, IOCON, ECCCON, ECCSTAT, DEVID, CiCON, bit timing,
  TDC, TBC, CiINT and the interrupt status registers, CiTREC, CiBDIAG0/1,
  TEF, TXQ, FIFO 1-31 control / status / user address, filters and masks,
  with reset values and "Configuration mode only" protection.
- RAM allocation in the documented order (TEF, TXQ, FIFO1..31; section 3.3)
  and the TX / RX / TEF object layouts (tables 3-5 to 3-7).
- Operating modes and the INT, INT0/GPIO0, INT1/GPIO1 pins.
- Error counters and error states as seen through CiTREC / CiBDIAG1.

`violations` collects everything the driver did that the datasheet forbids
or does not define (writing configuration bits outside Configuration mode,
multi-byte IOCON writes, unaligned RAM access, UINC on an empty FIFO, SPI
faster than 0.85 x SYSCLK / 2, ...).  Tests assert it stays empty.

Where the datasheet is silent the model takes the reading that is hardest
on the firmware and says so:
- mode changes are only accepted to and from Configuration mode;
- bus-off does not recover by itself: the controller stays off the bus until
  it goes through Configuration mode;
- entering Configuration mode resets the FIFOs (FRESET note in registers
  3-23 / 3-26 / 3-29) and the error counters.
"""

from __future__ import annotations

from ..canbus import Frame, Node, dlc_to_len
from ..clock import Clock

RAM_START, RAM_END = 0x400, 0xC00

# Register addresses, tables 3-1 and 3-2
C1CON, C1NBTCFG, C1DBTCFG, C1TDC, C1TBC, C1TSCON, C1VEC, C1INT = (
    0x000,
    0x004,
    0x008,
    0x00C,
    0x010,
    0x014,
    0x018,
    0x01C,
)
C1RXIF, C1TXIF, C1RXOVIF, C1TXATIF, C1TXREQ, C1TREC, C1BDIAG0, C1BDIAG1 = (
    0x020,
    0x024,
    0x028,
    0x02C,
    0x030,
    0x034,
    0x038,
    0x03C,
)
C1TEFCON, C1TEFSTA, C1TEFUA = 0x040, 0x044, 0x048
C1TXQCON, C1TXQSTA, C1TXQUA = 0x050, 0x054, 0x058
C1FIFOCON1 = 0x05C  # FIFO m: +12 * (m - 1); STA at +4, UA at +8
C1FLTCON0 = 0x1D0  # 8 registers, 4 filters each
C1FLTOBJ0, C1MASK0 = 0x1F0, 0x1F4  # filter m: +8 * m
OSC, IOCON, CRCREG, ECCCON, ECCSTAT, DEVID = 0xE00, 0xE04, 0xE08, 0xE0C, 0xE10, 0xE14

PLSIZE_BYTES = [8, 12, 16, 20, 24, 32, 48, 64]

MODE_NORMAL_FD, MODE_SLEEP, MODE_INT_LOOPBACK, MODE_LISTEN_ONLY = 0, 1, 2, 3
MODE_CONFIG, MODE_EXT_LOOPBACK, MODE_NORMAL_20, MODE_RESTRICTED = 4, 5, 6, 7
MODE_NAMES = {
    0: "Normal FD",
    1: "Sleep",
    2: "Internal loopback",
    3: "Listen only",
    4: "Configuration",
    5: "External loopback",
    6: "Normal 2.0",
    7: "Restricted",
}

# CiINT flag bits (register 3-14); the enables are the same bits << 16
TXIF, RXIF, TBCIF, MODIF, TEFIF = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4
ECCIF, SPICRCIF, TXATIF, RXOVIF = 1 << 8, 1 << 9, 1 << 10, 1 << 11
SERRIF, CERRIF, WAKIF, IVMIF = 1 << 12, 1 << 13, 1 << 14, 1 << 15
INT_CLEARABLE = IVMIF | WAKIF | CERRIF | SERRIF | MODIF | TBCIF


class Fifo:
    """State of the TXQ (index 0) or FIFO 1..31."""

    def __init__(self, index: int):
        self.index = index
        self.con = 0x00600480 if index == 0 else 0x00600400  # reset values
        self.head = 0
        self.tail = 0
        self.count = 0
        self.base = 0  # RAM offset, set by allocation
        self.sta_sticky = 0  # TXABT / TXLARB / TXERR / TXATIF / RXOVIF
        self.attempts = 0

    @property
    def is_tx(self):
        return self.index == 0 or bool(self.con & (1 << 7))

    @property
    def size(self):
        return ((self.con >> 24) & 0x1F) + 1

    @property
    def payload(self):
        return PLSIZE_BYTES[(self.con >> 29) & 7]

    @property
    def timestamped(self):
        return not self.is_tx and bool(self.con & (1 << 5))

    @property
    def obj_size(self):
        return 8 + (4 if self.timestamped else 0) + self.payload

    @property
    def txreq(self):
        return bool(self.con & (1 << 9))

    def reset(self):
        self.head = self.tail = self.count = 0
        self.sta_sticky = 0
        self.con &= ~((1 << 9) | (1 << 8))  # TXREQ, UINC


class Mcp2518fd(Node):
    def __init__(
        self, name: str, clock: Clock, osc_hz: float = 40e6, ua_absolute: bool = False
    ):
        self.name = name
        self.clock = clock
        self.osc_hz = osc_hz
        self.clock_present = True  # the external 40MHz oscillator is running
        self.ua_absolute = (
            ua_absolute  # how CiFIFOUA reports addresses (datasheet is ambiguous)
        )
        self.violations: list[str] = []
        self.dominant_events: list[tuple[float, str]] = []
        self.gpio0_ext = 0  # level seen on a floating GPIO0 (board pull-down)
        self.gpio1_ext = 0
        self.pin_history: list[tuple[float, object]] = []  # GPIO0 level changes
        self.spi_transactions = 0
        self.spi_bytes = 0
        self.ram = bytearray(RAM_END - RAM_START)
        self.ram_valid = bytearray(
            (RAM_END - RAM_START) // 4
        )  # word written since power-on
        self.power_on_reset()

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------
    def power_on_reset(self):
        """VDD came up: registers at reset values and RAM contents unknown."""
        import random

        rnd = random.Random(hash(self.name) & 0xFFFF)
        self.ram[:] = bytes(rnd.randrange(256) for _ in range(len(self.ram)))
        self.ram_valid[:] = bytes(len(self.ram_valid))
        self._reset_registers()

    def _reset_registers(self):
        self.reg: dict[int, int] = {}
        self.con = 0x04980760  # register 3-7: REQOP=100, OPMOD=100, TXQEN, STEF, WFT=11, WAKFIL, PXEDIS, ISOCRCEN
        self.reg[C1NBTCFG] = 0x003E0F0F
        self.reg[C1DBTCFG] = 0x000E0303
        self.reg[C1TDC] = 0x00021000
        self.reg[C1TSCON] = 0
        self.int_flags = 0
        self.int_enable = 0
        self.tec = 0
        self.rec = 0
        self.bus_off = False
        self.bdiag0 = 0
        self.bdiag1 = 0
        self.tefcon = 0x00000400
        self.tef_head = self.tef_tail = self.tef_count = 0
        self.tef_ovif = False
        self.fifos = [Fifo(i) for i in range(32)]
        self.fltcon = [0] * 32  # one byte per filter: FLTEN | FxBP
        self.fltobj = [0] * 32
        self.mask = [0] * 32
        self.osc = 0x00000060  # register 3-1: CLKODIV = 11
        self.iocon = 0x03000003  # register 3-2: PM1, PM0, TRIS1, TRIS0
        self.ecccon = 0
        self.eccstat = 0
        self.crcreg = 0
        self.tbc_offset = 0.0
        self.opmod = MODE_CONFIG
        self._mode_change_pending = None
        self._allocate()
        self._note_pin()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _violation(self, text: str):
        self.violations.append(f"{self.name} @{self.clock.now:.0f}us: {text}")

    @property
    def sysclk_hz(self) -> float:
        f = self.osc_hz
        if self.osc & 1:  # PLLEN: x10
            f *= 10
        if self.osc & (1 << 4):  # SCLKDIV
            f /= 2
        return f

    def _allocate(self):
        """RAM layout, section 3.3: TEF, then TXQ, then FIFO1..FIFO31."""
        offset = 0
        self.tef_base = 0
        if self.con & (1 << 19):  # STEF
            tef_obj = 12 if self.tefcon & (1 << 5) else 8
            offset += (((self.tefcon >> 24) & 0x1F) + 1) * tef_obj
        for fifo in self.fifos:
            if fifo.index == 0 and not self.con & (1 << 20):  # TXQEN
                fifo.base = offset
                continue
            fifo.base = offset
            if fifo.index == 0 or self._fifo_in_use(fifo):
                offset += fifo.size * fifo.obj_size
        self.ram_used = offset

    def _fifo_in_use(self, fifo: Fifo) -> bool:
        # Every FIFO occupies its configured space; the reset configuration
        # (1 message, 8 bytes) of unused FIFOs would also take RAM, but the
        # datasheet's memory figure only shows configured FIFOs.  The model
        # counts a FIFO once the driver has written its control register.
        return getattr(fifo, "configured", False)

    def ram_overflowed(self) -> bool:
        return self.ram_used > len(self.ram)

    # --- pins -----------------------------------------------------------
    def _fifo_status(self, f: Fifo) -> int:
        if f.is_tx:
            not_full = f.count < f.size
            half = f.count <= f.size // 2
            empty = f.count == 0
            flags = (1 if not_full else 0) | (2 if half else 0) | (4 if empty else 0)
        else:
            not_empty = f.count > 0
            half = f.count >= (f.size + 1) // 2
            full = f.count == f.size
            flags = (1 if not_empty else 0) | (2 if half else 0) | (4 if full else 0)
        return flags | f.sta_sticky

    def _rfif(self) -> int:
        bits = 0
        for f in self.fifos[1:]:
            if (
                not f.is_tx
                and getattr(f, "configured", False)
                and (self._fifo_status(f) & f.con & 0x07)
            ):
                bits |= 1 << f.index
        return bits

    def _tfif(self) -> int:
        bits = 0
        for f in self.fifos:
            if f.is_tx and (
                f.index == 0 and self.con & (1 << 20) or getattr(f, "configured", False)
            ):
                if self._fifo_status(f) & f.con & 0x07:
                    bits |= 1 << f.index
        return bits

    def _rxovif(self) -> int:
        return sum(
            1 << f.index
            for f in self.fifos[1:]
            if not f.is_tx and f.sta_sticky & 0x08 and f.con & 0x08
        )

    def _txatif(self) -> int:
        return sum(
            1 << f.index
            for f in self.fifos
            if f.is_tx and f.sta_sticky & 0x10 and f.con & 0x10
        )

    def _int_flags(self) -> int:
        flags = self.int_flags & INT_CLEARABLE
        if self._tfif():
            flags |= TXIF
        if self._rfif():
            flags |= RXIF
        if self._rxovif():
            flags |= RXOVIF
        if self._txatif():
            flags |= TXATIF
        if self.eccstat & 0x06 and self.ecccon & 0x06:
            flags |= ECCIF
        tef_flags = self._tef_status() & self.tefcon & 0x0F
        if tef_flags:
            flags |= TEFIF
        return flags

    @property
    def int_n(self) -> int:
        """INT pin, active low (section 6.0.1)."""
        return 0 if (self._int_flags() & self.int_enable) else 1

    def gpio0(self):
        """Level driven on pin 9 (INT0/GPIO0/XSTBY): 0, 1 or None for high-Z."""
        if self.iocon & (1 << 6):  # XSTBYEN: transceiver standby control
            return 1 if self.opmod == MODE_SLEEP else 0
        if not self.iocon & (1 << 24):  # PM0 = 0: TX interrupt output, active low
            return 0 if (self._int_flags() & self.int_enable & TXIF) else 1
        if self.iocon & 1:  # TRIS0 = 1: input
            return None
        return (self.iocon >> 8) & 1  # LAT0

    def gpio1(self):
        if not self.iocon & (1 << 25):
            return 0 if (self._int_flags() & self.int_enable & RXIF) else 1
        if self.iocon & 2:
            return None
        return (self.iocon >> 9) & 1

    def _note_pin(self):
        level = self.gpio0()
        if not self.pin_history or self.pin_history[-1][1] != level:
            self.pin_history.append((self.clock.now, level))

    def clko_toggling(self) -> bool:
        """CLKO/SOF pin carries a free-running clock (IOCON.SOF = 0)."""
        return self.clock_present and not self.iocon & (1 << 29)

    # ------------------------------------------------------------------
    # SPI (section 4)
    # ------------------------------------------------------------------
    def spi(self, mosi: bytes, sck_hz: float | None = None) -> bytes:
        """One instruction, from nCS falling to nCS rising."""
        self.spi_transactions += 1
        self.spi_bytes += len(mosi)
        miso = bytearray(len(mosi))
        if sck_hz is not None and sck_hz > 0.85 * self.sysclk_hz / 2 * 1.0001:
            self._violation(f"SCK {sck_hz / 1e6:.2f}MHz above 0.85 x SYSCLK / 2")
        if len(mosi) < 2:
            self._violation("instruction shorter than command + address")
            return bytes(miso)
        cmd = mosi[0] >> 4
        addr = ((mosi[0] & 0x0F) << 8) | mosi[1]
        if cmd == 0b0000:
            if addr != 0 or len(mosi) != 2:
                self._violation("RESET with an address or data")
            if self.opmod != MODE_CONFIG:
                self._violation(
                    "RESET issued outside Configuration mode (section 4.1.1)"
                )
            self._reset_registers()  # RAM is not changed
            return bytes(miso)
        if cmd not in (0b0011, 0b0010):
            self._violation(f"SPI instruction {cmd:04b} is not modelled")
            return bytes(miso)
        if not self.clock_present:
            # No SYSCLK: the SPI interface cannot synchronise; nothing answers.
            return bytes(miso)
        data = mosi[2:]
        if RAM_START <= addr < RAM_END:
            if addr & 3 or len(data) % 4:
                self._violation(
                    f"RAM access not word aligned: addr 0x{addr:03X} len {len(data)}"
                )
            addr &= ~3
            for i in range(0, len(data) - len(data) % 4, 4):
                a = RAM_START + (addr - RAM_START + i) % (RAM_END - RAM_START)
                if cmd == 0b0011:
                    miso[2 + i : 6 + i] = self._ram_read(a - RAM_START, "SPI")
                else:
                    self.ram[a - RAM_START : a - RAM_START + 4] = data[i : i + 4]
                    self.ram_valid[(a - RAM_START) // 4] = 1
            return bytes(miso)
        iocon_bytes = 0
        for i in range(len(data)):
            a = (addr + i) & 0xFFF
            if cmd == 0b0011:
                miso[2 + i] = (self._read_reg(a & ~3) >> (8 * (a & 3))) & 0xFF
            else:
                if IOCON <= a < IOCON + 4:
                    iocon_bytes += 1
                self._write_reg_byte(a & ~3, a & 3, data[i])
        if iocon_bytes > 1:
            self._violation(
                "IOCON written with a multi-byte instruction (register 3-2 note 2)"
            )
        self._note_pin()
        return bytes(miso)

    def _ram_read(self, offset: int, who: str) -> bytes:
        if self.ecccon & 1 and not self.ram_valid[offset // 4]:
            # ECC enabled and the word was never written: parity is garbage
            self.eccstat |= 0x04 | ((RAM_START + offset) << 16)
            self._violation(
                f"{who} read of uninitialised RAM word 0x{RAM_START + offset:03X} with ECC on"
            )
        return bytes(self.ram[offset : offset + 4])

    # ------------------------------------------------------------------
    # register read
    # ------------------------------------------------------------------
    def _ua(self, offset: int) -> int:
        return offset + (RAM_START if self.ua_absolute else 0)

    def _tef_status(self) -> int:
        size = ((self.tefcon >> 24) & 0x1F) + 1
        return (
            (1 if self.tef_count else 0)
            | (2 if self.tef_count >= (size + 1) // 2 else 0)
            | (4 if self.tef_count == size else 0)
            | (8 if self.tef_ovif else 0)
        )

    def _read_reg(self, a: int) -> int:
        if a == C1CON:
            busy = self.bus is not None and self.on_bus() and not self.bus.is_idle()
            return (
                (self.con & ~(0xE0 << 16) & ~(1 << 11))
                | (self.opmod << 21)
                | (busy << 11)
            )
        if a in (C1NBTCFG, C1DBTCFG, C1TDC, C1TSCON):
            return self.reg[a]
        if a == C1TBC:
            if not self.reg[C1TSCON] & (1 << 16):
                return 0
            pre = (self.reg[C1TSCON] & 0x3FF) + 1
            return (
                int((self.clock.now - self.tbc_offset) * self.sysclk_hz / 1e6 / pre)
                & 0xFFFFFFFF
            )
        if a == C1VEC:
            return 0x40400040  # not used by the firmware; reset value
        if a == C1INT:
            return self._int_flags() | (self.int_enable << 16)
        if a == C1RXIF:
            return self._rfif()
        if a == C1TXIF:
            return self._tfif()
        if a == C1RXOVIF:
            return self._rxovif()
        if a == C1TXATIF:
            return self._txatif()
        if a == C1TXREQ:
            return sum(1 << f.index for f in self.fifos if f.is_tx and f.txreq)
        if a == C1TREC:
            v = (self.tec & 0xFF) << 8 | (self.rec & 0xFF)
            txwarn, rxwarn = self.tec > 95, self.rec > 95
            v |= (rxwarn << 17) | (txwarn << 18) | ((txwarn or rxwarn) << 16)
            v |= ((self.rec > 127) << 19) | ((self.tec > 127) << 20)
            v |= (
                self.bus_off or self.opmod == MODE_CONFIG
            ) << 21  # TXBO set in Configuration mode
            return v
        if a == C1BDIAG0:
            return self.bdiag0
        if a == C1BDIAG1:
            return self.bdiag1
        if a == C1TEFCON:
            return self.tefcon
        if a == C1TEFSTA:
            return self._tef_status()
        if a == C1TEFUA:
            return self._ua(
                self.tef_base + self.tef_tail * (12 if self.tefcon & (1 << 5) else 8)
            )
        if C1TXQCON <= a < C1FLTCON0 and a != 0x04C:
            idx, which = divmod(a - C1TXQCON, 12)
            f = self.fifos[idx]
            if which == 0:
                # FRESET "is set while in Configuration mode and is
                # automatically cleared in Normal mode" (register 3-29 note 3)
                freset = (1 << 10) if self.opmod == MODE_CONFIG else 0
                return (f.con & ~(1 << 10)) | freset | (0x80 if idx == 0 else 0)
            if which == 4:
                ci = f.tail if f.is_tx else f.head
                return self._fifo_status(f) | (ci << 8)
            if self.opmod == MODE_CONFIG:
                self._violation(
                    f"FIFO{idx} user address read in Configuration mode (register 3-31 note 1)"
                )
            pos = f.head if f.is_tx else f.tail
            return self._ua(f.base + pos * f.obj_size)
        if C1FLTCON0 <= a < C1FLTCON0 + 32:
            i = a - C1FLTCON0
            return sum(self.fltcon[i + k] << (8 * k) for k in range(4))
        if C1FLTOBJ0 <= a < C1FLTOBJ0 + 8 * 32:
            m, which = divmod(a - C1FLTOBJ0, 8)
            return self.fltobj[m] if which == 0 else self.mask[m]
        if a == OSC:
            v = self.osc & 0x7D
            running = self.clock_present and not self.osc & 4
            v |= (running << 10) | ((self.osc & 1) << 8) | (((self.osc >> 4) & 1) << 12)
            return v
        if a == IOCON:
            g0, g1 = self.gpio0(), self.gpio1()
            g0 = self.gpio0_ext if g0 is None else g0
            g1 = self.gpio1_ext if g1 is None else g1
            return (self.iocon & ~(3 << 16)) | (g0 << 16) | (g1 << 17)
        if a == CRCREG:
            return self.crcreg
        if a == ECCCON:
            return self.ecccon
        if a == ECCSTAT:
            return self.eccstat
        if a == DEVID:
            return 0x00  # reset value in register 3-6 is all zero ("R-0")
        return 0

    # ------------------------------------------------------------------
    # register write
    # ------------------------------------------------------------------
    def _config_only(self, what: str) -> bool:
        if self.opmod != MODE_CONFIG:
            self._violation(f"{what} written outside Configuration mode")
            return False
        return True

    def _write_reg_byte(self, a: int, b: int, value: int):
        shift = 8 * b
        mask = 0xFF << shift
        v32 = value << shift

        def merged(old: int, writable: int) -> int:
            w = writable & mask
            return (old & ~w) | (v32 & w)

        if a == C1CON:
            if b == 3:
                self.con = merged(self.con, 0xFF000000)
                if value & 0x08:  # ABAT
                    self._abort_all()
                self._request_mode(value & 7)
            elif b == 2:
                new = merged(self.con, 0x001F0000)
                if new != self.con and self._config_only(
                    "CiCON TXQEN/STEF/SERR2LOM/ESIGM/RTXAT"
                ):
                    self.con = new
                    self._allocate()
            elif b == 1:
                new_brsdis = merged(self.con, 1 << 12)
                new_wake = merged(self.con, 0x0700)
                if (new_wake ^ self.con) & 0x0100 and not self._config_only(
                    "CiCON WAKFIL"
                ):
                    new_wake = (new_wake & ~0x0100) | (self.con & 0x0100)
                self.con = (new_wake & ~(1 << 12)) | (new_brsdis & (1 << 12))
            else:
                new = merged(self.con, 0x7F)
                if new != self.con and self._config_only("CiCON PXEDIS/ISOCRCEN/DNCNT"):
                    self.con = new
            return
        if a in (C1NBTCFG, C1DBTCFG, C1TDC):
            writable = {C1NBTCFG: 0xFFFF7F7F, C1DBTCFG: 0xFF1F0F0F, C1TDC: 0x03037F3F}[
                a
            ]
            new = merged(self.reg[a], writable)
            if new != self.reg[a] and self._config_only(
                f"bit timing register 0x{a:03X}"
            ):
                self.reg[a] = new
            return
        if a == C1TSCON:
            self.reg[a] = merged(self.reg[a], 0x000703FF)
            if not self.reg[a] & (1 << 16):
                self.tbc_offset = self.clock.now
            return
        if a == C1TBC:
            self.tbc_offset = self.clock.now
            return
        if a == C1INT:
            if b >= 2:
                self.int_enable = merged(self.int_enable << 16, 0xFF1F0000) >> 16
            else:
                # HS/C flags: writing 0 clears, writing 1 leaves the flag alone
                clearable = INT_CLEARABLE & mask
                self.int_flags &= ~(clearable & ~v32)
            return
        if a == C1TXREQ:
            for f in self.fifos:
                if v32 & mask & (1 << f.index) and f.is_tx:
                    self._set_txreq(f)
            return
        if a == C1BDIAG0:
            self.bdiag0 = merged(self.bdiag0, 0xFFFFFFFF)
            return
        if a == C1BDIAG1:
            self.bdiag1 = merged(self.bdiag1, 0xFBBFFFFF)
            return
        if a == C1TEFCON:
            if b == 3:
                new = merged(self.tefcon, 0x1F000000)
                if new != self.tefcon and self._config_only("TEF size"):
                    self.tefcon = new
                    self._allocate()
            elif b == 1:
                if value & 0x04:
                    self.tef_head = self.tef_tail = self.tef_count = 0
                if value & 0x01:
                    if self.tef_count == 0:
                        self._violation("UINC on empty TEF")
                    else:
                        self.tef_tail = (self.tef_tail + 1) % (
                            ((self.tefcon >> 24) & 0x1F) + 1
                        )
                        self.tef_count -= 1
            elif b == 0:
                new = merged(self.tefcon, 0x2F)
                if (new ^ self.tefcon) & 0x20 and not self._config_only("TEFTSEN"):
                    new = (new & ~0x20) | (self.tefcon & 0x20)
                self.tefcon = new
                self._allocate()
            return
        if a == C1TEFSTA:
            if b == 0 and not value & 0x08:
                self.tef_ovif = False
            return
        if C1TXQCON <= a < C1FLTCON0 and a != 0x04C:
            idx, which = divmod(a - C1TXQCON, 12)
            self._write_fifo(self.fifos[idx], which, b, value)
            return
        if C1FLTCON0 <= a < C1FLTCON0 + 32:
            m = a - C1FLTCON0 + b
            old = self.fltcon[m]
            new = value & 0x9F
            if (old & 0x80) and (new & 0x80) and (old ^ new) & 0x1F:
                self._violation(
                    f"filter {m} FIFO pointer changed while the filter is enabled"
                )
                new = (new & ~0x1F) | (old & 0x1F)
            if new & 0x80 and (new & 0x1F) == 0:
                self._violation(f"filter {m} points at FIFO 0 (the TXQ)")
            self.fltcon[m] = new
            return
        if C1FLTOBJ0 <= a < C1FLTOBJ0 + 8 * 32:
            m, which = divmod(a - C1FLTOBJ0, 8)
            if which == 0:
                if self.fltcon[m] & 0x80:
                    self._violation(
                        f"filter object {m} written while the filter is enabled"
                    )
                    return
                self.fltobj[m] = merged(self.fltobj[m], 0x7FFFFFFF)
            else:
                self.mask[m] = merged(self.mask[m], 0x7FFFFFFF)
            return
        if a == OSC:
            if b == 0:
                new = (self.osc & ~0x7D) | (value & 0x7D)
                if (new ^ self.osc) & 0x11 and not self._config_only(
                    "OSC PLLEN/SCLKDIV"
                ):
                    new = (new & ~0x11) | (self.osc & 0x11)
                self.osc = new
                if self.sysclk_hz > 40e6 * 1.001:
                    self._violation(
                        f"SYSCLK {self.sysclk_hz / 1e6:.0f}MHz: PLL enabled on a 40MHz clock"
                    )
            return
        if a == IOCON:
            self.iocon = merged(self.iocon, 0x73000343)
            return
        if a == ECCCON:
            self.ecccon = merged(self.ecccon, 0x00007F07)
            return
        if a == ECCSTAT:
            if b == 0:
                self.eccstat &= ~(0x06 & ~value)
            return
        if a == CRCREG:
            self.crcreg = merged(self.crcreg, 0x03000000)
            if b == 2:
                self.crcreg &= ~(0x00030000 & ~v32)
            return
        # read-only or unimplemented registers: ignored

    def _write_fifo(self, f: Fifo, which: int, b: int, value: int):
        if which == 8:
            return  # user address is read only
        if which == 4:
            if b == 0:
                # TXABT, TXLARB, TXERR, TXATIF, RXOVIF: write 0 to clear
                f.sta_sticky &= value | ~0xF8
            return
        if b == 3 or (b == 0 and (value ^ f.con) & 0xA0):
            # PLSIZE, FSIZE (byte 3), TXEN, RXTSEN (byte 0): Configuration mode only
            if not self._config_only(f"FIFO{f.index} PLSIZE/FSIZE/TXEN/RXTSEN"):
                if b == 3:
                    return
                value = (value & ~0xA0) | (f.con & 0xA0)
        f.configured = True
        if b == 3:
            f.con = (f.con & 0x00FFFFFF) | (value << 24)
            self._allocate()
        elif b == 2:
            f.con = (f.con & 0xFF00FFFF) | ((value & 0x7F) << 16)
        elif b == 0:
            f.con = (f.con & 0xFFFFFF00) | (value & (0x95 if f.index == 0 else 0xFF))
            self._allocate()
        else:  # byte 1: FRESET, TXREQ, UINC
            if value & 0x04:
                f.reset()
            if value & 0x01:
                self._uinc(f)
            if value & 0x02:
                if f.is_tx:
                    self._set_txreq(f)
            elif f.is_tx and f.txreq:
                # clearing TXREQ while set requests an abort
                self._abort(f)

    def _uinc(self, f: Fifo):
        if self.opmod == MODE_CONFIG:
            self._violation(f"UINC on FIFO{f.index} in Configuration mode")
            return
        if f.is_tx:
            if f.count == f.size:
                self._violation(f"UINC on full TX FIFO{f.index}")
                return
            f.head = (f.head + 1) % f.size
            f.count += 1
        else:
            if f.count == 0:
                self._violation(f"UINC on empty RX FIFO{f.index}")
                return
            f.tail = (f.tail + 1) % f.size
            f.count -= 1

    def _set_txreq(self, f: Fifo):
        f.sta_sticky &= ~0xE0  # TXABT / TXLARB / TXERR cleared when TXREQ is set
        if f.count == 0:
            return  # nothing queued: the request clears at once
        f.con |= 1 << 9
        f.attempts = 0
        self.kick()

    def _abort(self, f: Fifo):
        if self.bus is not None and self.bus.transmitting(self) and self._tx_fifo is f:
            f.abort_pending = True  # takes effect when the current attempt ends
            return
        f.con &= ~(1 << 9)
        f.sta_sticky |= 0x80  # TXABT

    def _abort_all(self):
        for f in self.fifos:
            if f.is_tx and f.txreq:
                self._abort(f)
        self.con &= ~(
            1 << 27
        )  # "module will clear this bit when all transmissions were aborted"

    # ------------------------------------------------------------------
    # modes
    # ------------------------------------------------------------------
    def _request_mode(self, mode: int):
        if mode == self.opmod and self._mode_change_pending is None:
            return
        if mode == MODE_SLEEP:
            self._violation("Sleep mode is not modelled")
            return
        if mode != MODE_CONFIG and self.opmod != MODE_CONFIG:
            self._violation(
                f"mode change {MODE_NAMES[self.opmod]} -> {MODE_NAMES[mode]} without going "
                "through Configuration mode (not defined in the datasheet)"
            )
            return
        self._mode_change_pending = mode
        if mode == MODE_CONFIG:
            # after the message in progress, if any
            if self.bus is None or not self.bus.transmitting(self):
                self._enter_mode(mode)
        elif mode == MODE_INT_LOOPBACK or self.bus is None:
            self._enter_mode(mode)
        else:
            # bus integration: 11 consecutive recessive bits
            self._integrate(mode)

    def _integrate(self, mode: int):
        if self._mode_change_pending != mode:
            return
        bit_us = 1e6 / self.nominal_bps()
        bus = self.bus
        if bus.stuck_dominant or bus._current is not None:
            self.clock.after(11 * bit_us, lambda: self._integrate(mode))
            return
        self.clock.after(11 * bit_us, lambda: self._finish_integration(mode))

    def _finish_integration(self, mode: int):
        if self._mode_change_pending != mode:
            return
        if self.bus.stuck_dominant:
            self._integrate(mode)
            return
        self._enter_mode(mode)

    def _enter_mode(self, mode: int):
        self._mode_change_pending = None
        if mode == self.opmod:
            return
        self.opmod = mode
        self.int_flags |= MODIF
        if mode == MODE_CONFIG:
            for f in self.fifos:
                f.reset()
            self.tef_head = self.tef_tail = self.tef_count = 0
            self.tec = self.rec = 0
            self.bus_off = False
        else:
            if self.ram_overflowed():
                self._violation(
                    f"FIFO configuration needs {self.ram_used} bytes of RAM, 2048 available"
                )
            self.kick()

    # ------------------------------------------------------------------
    # CAN node interface
    # ------------------------------------------------------------------
    def on_bus(self) -> bool:
        return self.clock_present and self.opmod in (
            MODE_NORMAL_FD,
            MODE_NORMAL_20,
            MODE_LISTEN_ONLY,
            MODE_RESTRICTED,
            MODE_EXT_LOOPBACK,
        )

    def active(self) -> bool:
        return (
            self.clock_present
            and not self.bus_off
            and self.opmod
            in (MODE_NORMAL_FD, MODE_NORMAL_20, MODE_RESTRICTED, MODE_EXT_LOOPBACK)
        )

    def nominal_bps(self) -> float:
        r = self.reg[C1NBTCFG]
        tq = 1 + ((r >> 16) & 0xFF) + 1 + ((r >> 8) & 0x7F) + 1
        return self.sysclk_hz / (((r >> 24) + 1) * tq)

    def data_bps(self) -> float:
        r = self.reg[C1DBTCFG]
        tq = 1 + ((r >> 16) & 0x1F) + 1 + ((r >> 8) & 0x0F) + 1
        return self.sysclk_hz / (((r >> 24) + 1) * tq)

    def nominal_sample_point(self) -> float:
        r = self.reg[C1NBTCFG]
        t1, t2 = ((r >> 16) & 0xFF) + 1, ((r >> 8) & 0x7F) + 1
        return (1 + t1) / (1 + t1 + t2)

    def data_sample_point(self) -> float:
        r = self.reg[C1DBTCFG]
        t1, t2 = ((r >> 16) & 0x1F) + 1, ((r >> 8) & 0x0F) + 1
        return (1 + t1) / (1 + t1 + t2)

    def fd_capable(self) -> bool:
        return self.opmod != MODE_NORMAL_20

    def drove_dominant(self, what: str):
        self.dominant_events.append((self.clock.now, what))

    # --- transmit -------------------------------------------------------
    def tx_pending(self) -> Frame | None:
        if (
            self.opmod not in (MODE_NORMAL_FD, MODE_NORMAL_20, MODE_EXT_LOOPBACK)
            or self.bus_off
        ):
            return None
        best = None
        for f in self.fifos:
            if f.is_tx and f.txreq and f.count:
                pri = ((f.con >> 16) & 0x1F, -f.index)
                if best is None or pri > best[0]:
                    best = (pri, f)
        if best is None:
            return None
        f = best[1]
        self._tx_fifo = f
        return self._decode_tx_object(f)

    def _decode_tx_object(self, f: Fifo) -> Frame | None:
        base = f.base + f.tail * f.obj_size
        if base + f.obj_size > len(self.ram):
            self._violation(f"FIFO{f.index} object beyond the end of RAM")
            return None
        words = [
            int.from_bytes(self._ram_read(base + 4 * i, "transmitter"), "little")
            for i in range(2)
        ]
        t0, t1 = words
        dlc = t1 & 0x0F
        ide, rtr, brs, fdf, esi = [(t1 >> n) & 1 for n in (4, 5, 6, 7, 8)]
        if self.opmod == MODE_NORMAL_20 and fdf:
            self._violation("FD frame queued in Normal CAN 2.0 mode")
        sid = t0 & 0x7FF
        eid = (t0 >> 11) & 0x3FFFF
        can_id = (sid << 18 | eid) if ide else sid
        n = 0 if rtr else dlc_to_len(dlc, bool(fdf))
        if n > f.payload:
            self.bdiag1 |= 1 << 31  # DLCMM
            self._violation(
                f"TX DLC {dlc} larger than FIFO{f.index} payload size {f.payload}"
            )
            n = f.payload
        data = bytearray()
        for i in range(0, n, 4):
            data += self._ram_read(base + 8 + i, "transmitter")
        brs = bool(brs and fdf and not self.con & (1 << 12))  # BRSDIS
        esi = bool(fdf and (esi or self.tec > 127))
        self._tx_seq = t1 >> 9
        self._tx_words = (t0, t1)
        return Frame(
            can_id,
            bytes(data[:n]),
            ext=bool(ide),
            rtr=bool(rtr and not fdf),
            fd=bool(fdf),
            brs=brs,
            esi=esi,
            dlc=dlc,
        )

    def tx_done(self, frame: Frame):
        f = self._tx_fifo
        f.tail = (f.tail + 1) % f.size
        f.count -= 1
        f.attempts = 0
        if self.tec:
            self.tec -= 1
        self._error_state_changed()
        self.bdiag1 = (self.bdiag1 & 0xFFFF0000) | (
            (self.bdiag1 + 1) & 0xFFFF
        )  # EFMSGCNT
        if f.count == 0:
            f.con &= ~(1 << 9)  # TXREQ clears when everything queued was sent
        if getattr(f, "abort_pending", False):
            f.abort_pending = False
            f.con &= ~(1 << 9)
        if self.con & (1 << 19):  # STEF
            self._store_tef()
        if self.opmod == MODE_EXT_LOOPBACK:
            self._store_rx(frame)
        self._after_tx()

    def tx_lost_arbitration(self, frame: Frame):
        self._tx_fifo.sta_sticky |= 0x40  # TXLARB

    def tx_error(self, frame: Frame, kind: str):
        f = self._tx_fifo
        f.sta_sticky |= 0x20  # TXERR
        self.int_flags |= IVMIF
        if kind == "ack":
            self.bdiag1 |= 1 << 18  # NACKERR
            # ISO 11898-1: an error-passive transmitter that sees no dominant
            # bit after its ACK error does not increment TEC any further.
            if self.tec <= 127:
                self.tec += 8
        else:
            self.bdiag1 |= (
                1 << 16 if frame.brs is False else 1 << 24
            )  # bit0 error (nominal / data)
            self.tec += 8
        nt = (self.bdiag0 >> 8) & 0xFF
        self.bdiag0 = (self.bdiag0 & ~0xFF00) | (min(nt + 1, 255) << 8)
        if self.tec > 255:
            self.bus_off = True
            self.bdiag1 |= 1 << 23  # TXBOERR
        self._error_state_changed()
        f.attempts += 1
        if getattr(f, "abort_pending", False):
            f.abort_pending = False
            f.con &= ~(1 << 9)
            f.sta_sticky |= 0x80
        elif self.con & (1 << 16):  # RTXAT: limited attempts
            limit = {0: 1, 1: 4}.get((f.con >> 21) & 3)
            if limit is not None and f.attempts >= limit:
                f.sta_sticky |= 0x10  # TXATIF
                f.con &= ~(1 << 9)
        self._after_tx()

    def _after_tx(self):
        if self._mode_change_pending == MODE_CONFIG:
            self._enter_mode(MODE_CONFIG)
        self._note_pin()

    def _error_state_changed(self):
        state = (
            self.bus_off,
            self.tec > 127,
            self.rec > 127,
            self.tec > 95,
            self.rec > 95,
        )
        if state != getattr(self, "_last_error_state", (False,) * 5):
            self.int_flags |= CERRIF
        self._last_error_state = state

    def _store_tef(self):
        size = ((self.tefcon >> 24) & 0x1F) + 1
        if self.tef_count == size:
            self.tef_ovif = True
            return
        obj = 12 if self.tefcon & (1 << 5) else 8
        base = self.tef_base + self.tef_head * obj
        self.ram[base : base + 4] = self._tx_words[0].to_bytes(4, "little")
        self.ram[base + 4 : base + 8] = self._tx_words[1].to_bytes(4, "little")
        self.tef_head = (self.tef_head + 1) % size
        self.tef_count += 1

    # --- receive --------------------------------------------------------
    def rx(self, frame: Frame):
        if self.rec:
            self.rec -= 1
            self._error_state_changed()
        self.bdiag1 = (self.bdiag1 & 0xFFFF0000) | ((self.bdiag1 + 1) & 0xFFFF)
        if frame.fd and frame.esi:
            self.bdiag1 |= 1 << 30
        self._store_rx(frame)

    def rx_error(self):
        if self.opmod == MODE_LISTEN_ONLY:
            return  # error counters are frozen while only listening
        self.rec = min(self.rec + 1, 255)
        self.int_flags |= IVMIF
        self._error_state_changed()

    def _filter_hit(self, frame: Frame):
        for m in range(32):
            if not self.fltcon[m] & 0x80:
                continue
            obj, mask = self.fltobj[m], self.mask[m]
            if mask & (1 << 30) and bool(obj & (1 << 30)) != frame.ext:  # MIDE / EXIDE
                continue
            if frame.ext:
                value = ((frame.can_id >> 18) & 0x7FF) | (
                    (frame.can_id & 0x3FFFF) << 11
                )
                care = mask & 0x1FFFFFFF
            else:
                value = frame.can_id & 0x7FF
                care = mask & 0x7FF
            if (value ^ obj) & care:
                continue
            return m, self.fltcon[m] & 0x1F
        return None

    def _store_rx(self, frame: Frame):
        hit = self._filter_hit(frame)
        if hit is None:
            return
        m, idx = hit
        f = self.fifos[idx]
        if f.is_tx or not getattr(f, "configured", False):
            self._violation(
                f"filter {m} points at FIFO{idx}, which is not a receive FIFO"
            )
            return
        if f.count == f.size:
            f.sta_sticky |= 0x08  # RXOVIF: message lost
            return
        base = f.base + f.head * f.obj_size
        if base + f.obj_size > len(self.ram):
            self._violation(f"FIFO{f.index} object beyond the end of RAM")
            return
        if frame.ext:
            r0 = ((frame.can_id >> 18) & 0x7FF) | ((frame.can_id & 0x3FFFF) << 11)
        else:
            r0 = frame.can_id & 0x7FF
        r1 = (
            (frame.dlc & 0x0F)
            | (frame.ext << 4)
            | (frame.rtr << 5)
            | (frame.brs << 6)
            | (frame.fd << 7)
            | (frame.esi << 8)
            | (m << 11)
        )
        words = [r0, r1]
        if f.timestamped:
            words.append(self._read_reg(C1TBC))
        raw = b"".join(w.to_bytes(4, "little") for w in words)
        data = frame.data
        if len(data) > f.payload:
            self.bdiag1 |= 1 << 31  # DLCMM
            data = data[: f.payload]
        raw += data + bytes(-len(data) % 4)
        self.ram[base : base + len(raw)] = raw
        for w in range(base // 4, (base + len(raw) + 3) // 4):
            self.ram_valid[w] = 1
        f.head = (f.head + 1) % f.size
        f.count += 1

    # ------------------------------------------------------------------
    # fault injection for tests
    # ------------------------------------------------------------------
    def brown_out(self):
        """VDD dipped below VPORL and came back: power-on reset."""
        self.power_on_reset()
        if self.bus is not None:
            self.bus.kick()
