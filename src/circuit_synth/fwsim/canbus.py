"""Frame-level CAN / CAN FD bus model.

This is not a bit-level simulator.  It models what firmware can observe on
a bus and what a node must never do:

- arbitration: when the bus goes idle, the pending frame with the lowest
  arbitration field wins; the others wait and retry;
- acknowledgement: a frame is ACKed only if another node on the bus is
  active (not listen-only, not off-bus) and can decode it.  Without one the
  transmitter gets an ACK error, sends an error flag and retries;
- format and bit-rate conflicts: an active node that cannot decode a frame
  (classic-only node seeing an FD frame, or a node at another bit rate)
  destroys it with an error frame, as real controllers do;
- frame duration, from the bit counts of ISO 11898-1 and the nominal / data
  bit rates (bit-rate switching), so load and overflow tests run at
  realistic frame rates;
- termination: every bus counts its terminations.  With none, no frame gets
  through (no recessive return path).  One or two work; the count is
  exposed so tests can assert a termination policy (see `terminations`).

What it does NOT model: analog levels, edge rates, reflections, sample-point
or oscillator tolerance, transceiver loop delay / TDC, exact stuff bits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clock import Clock

CAN_FD_LEN = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]


def dlc_to_len(dlc: int, fd: bool) -> int:
    return CAN_FD_LEN[dlc] if fd else min(dlc, 8)


def len_to_dlc(length: int) -> int:
    for dlc, n in enumerate(CAN_FD_LEN):
        if n >= length:
            return dlc
    raise ValueError(length)


@dataclass(frozen=True)
class Frame:
    can_id: int
    data: bytes = b""
    ext: bool = False
    rtr: bool = False
    fd: bool = False
    brs: bool = False
    esi: bool = False
    dlc: int | None = None  # defaults to the DLC for len(data)

    def __post_init__(self):
        if self.dlc is None:
            object.__setattr__(self, "dlc", len_to_dlc(len(self.data)))
        if not self.rtr and len(self.data) != dlc_to_len(self.dlc, self.fd):
            raise ValueError(
                f"data length {len(self.data)} does not match DLC {self.dlc}"
            )
        if not self.fd and (self.brs or self.esi):
            raise ValueError("BRS / ESI only exist in FD frames")

    def arbitration_key(self) -> tuple:
        """Sort key: the lower key wins arbitration (dominant = 0 first)."""
        if self.ext:
            base, low = self.can_id >> 18, self.can_id & 0x3FFFF
            # 11 base ID bits, SRR=1, IDE=1, 18 ID bits, RTR
            return (base, 1, 1, low, int(self.rtr))
        # 11 ID bits, RTR (classic) / RRS=0 (FD), IDE=0
        return (self.can_id, int(self.rtr and not self.fd), 0, 0, 0)

    def bit_counts(self, stuff_ratio: float = 0.1) -> tuple[float, float]:
        """(bits at the nominal rate, bits at the data rate).

        `stuff_ratio` is the fraction of stuffable bits that carry a stuff
        bit: 0 is the best case, 0.25 the worst (one per four after the
        first), 0.1 a typical payload.
        """
        n = 0 if self.rtr else len(self.data)
        if not self.fd:
            # SOF + ID + RTR + IDE + r0 + DLC + data + CRC15 are stuffed
            stuffable = (54 if self.ext else 34) + 8 * n
            fixed = 10 + 3  # CRC del, ACK, ACK del, EOF(7), IFS(3)
            return stuffable * (1 + stuff_ratio) + fixed, 0.0
        # FD.  Arbitration phase up to and including BRS, at the nominal rate
        arb = 36 if self.ext else 17
        # Data phase: ESI, DLC, data, then stuff count (4), CRC and fixed stuff bits
        crc = (17 + 6) if n <= 16 else (21 + 7)
        data_phase = (5 + 8 * n) * (1 + stuff_ratio) + 4 + crc
        tail = 1 + 2 + 7 + 3  # CRC del, ACK, ACK del, EOF, IFS
        if self.brs:
            return arb * (1 + stuff_ratio) + tail, data_phase
        return arb * (1 + stuff_ratio) + tail + data_phase, 0.0

    def duration_us(
        self, nominal_bps: float, data_bps: float, stuff_ratio: float = 0.1
    ) -> float:
        nom, dat = self.bit_counts(stuff_ratio)
        return nom / nominal_bps * 1e6 + (dat / data_bps * 1e6 if dat else 0.0)


class Node:
    """Interface between a CAN controller (model) and the bus."""

    name = "node"
    bus: "CanBus | None" = None

    # --- queried by the bus -------------------------------------------
    def on_bus(self) -> bool:
        """Takes part in bus traffic at all (not in configuration / off)."""
        raise NotImplementedError

    def active(self) -> bool:
        """May drive the bus: ACK, error flags, transmit."""
        raise NotImplementedError

    def nominal_bps(self) -> float:
        raise NotImplementedError

    def data_bps(self) -> float:
        raise NotImplementedError

    def fd_capable(self) -> bool:
        raise NotImplementedError

    def tx_pending(self) -> Frame | None:
        return None

    # --- callbacks from the bus ---------------------------------------
    def tx_done(self, frame: Frame):
        pass

    def tx_error(self, frame: Frame, kind: str):
        """kind: 'ack' (nobody acknowledged) or 'bit' (frame destroyed)."""

    def tx_lost_arbitration(self, frame: Frame):
        pass

    def rx(self, frame: Frame):
        pass

    def rx_error(self):
        """An error frame was seen while receiving."""

    def drove_dominant(self, what: str):
        """Called whenever this node drives the bus (frame, ACK, error flag)."""

    # --- helpers --------------------------------------------------------
    def can_decode(self, frame: Frame, tx: "Node") -> bool:
        if abs(self.nominal_bps() - tx.nominal_bps()) > 0.01 * tx.nominal_bps():
            return False
        if frame.fd:
            if not self.fd_capable():
                return False
            if (
                frame.brs
                and abs(self.data_bps() - tx.data_bps()) > 0.01 * tx.data_bps()
            ):
                return False
        return True

    def kick(self):
        if self.bus is not None:
            self.bus.kick()


@dataclass
class BusStats:
    frames_ok: int = 0
    ack_errors: int = 0
    destroyed: int = 0
    arbitration_losses: int = 0
    busy_us: float = 0.0
    log: list = field(default_factory=list)


class CanBus:
    ERROR_FRAME_BITS = 6 + 8 + 3  # error flag, delimiter, intermission

    def __init__(self, clock: Clock, name: str, external_terminations: int = 0):
        self.clock = clock
        self.name = name
        self.nodes: list[Node] = []
        self.external_terminations = external_terminations
        self.term_sources: list = []  # callables returning True when a relay is closed
        self.stuck_dominant = False
        self.stuff_ratio = 0.1
        self.stats = BusStats()
        self._busy_until = 0.0
        self._current: tuple[Node, Frame] | None = None
        self._kick_scheduled = False
        self.keep_log = False

    # --- topology -------------------------------------------------------
    def attach(self, node: Node):
        node.bus = self
        self.nodes.append(node)
        return node

    def terminations(self) -> int:
        return self.external_terminations + sum(1 for src in self.term_sources if src())

    def is_idle(self) -> bool:
        return (
            self._current is None
            and not self.stuck_dominant
            and self.clock.now >= self._busy_until
        )

    def transmitting(self, node: Node) -> bool:
        return self._current is not None and self._current[0] is node

    def idle_since(self) -> float:
        return self._busy_until

    # --- traffic --------------------------------------------------------
    def kick(self):
        """A node has something to send (or the bus just went idle)."""
        if self._kick_scheduled or self._current is not None:
            return
        self._kick_scheduled = True
        self.clock.at(max(self.clock.now, self._busy_until), self._start)

    def _start(self):
        self._kick_scheduled = False
        if self._current is not None or self.stuck_dominant:
            return
        contenders = []
        for node in self.nodes:
            if node.on_bus() and node.active():
                frame = node.tx_pending()
                if frame is not None:
                    contenders.append((frame.arbitration_key(), id(node), node, frame))
        if not contenders:
            return
        contenders.sort(key=lambda c: (c[0], c[1]))
        _, _, tx, frame = contenders[0]
        for _, _, loser, lost in contenders[1:]:
            self.stats.arbitration_losses += 1
            loser.drove_dominant("arbitration")
            loser.tx_lost_arbitration(lost)
        tx.drove_dominant("frame")
        self._current = (tx, frame)
        duration = frame.duration_us(tx.nominal_bps(), tx.data_bps(), self.stuff_ratio)
        self.clock.after(duration, lambda: self._finish(tx, frame, duration))

    def _finish(self, tx: Node, frame: Frame, duration: float):
        self._current = None
        self.stats.busy_us += duration
        others = [n for n in self.nodes if n is not tx and n.on_bus()]
        error_us = self.ERROR_FRAME_BITS / tx.nominal_bps() * 1e6
        outcome = "ok"
        if self.terminations() == 0:
            # No termination: the bus never returns to recessive cleanly.
            outcome = "bit"
        elif any(n.active() and not n.can_decode(frame, tx) for n in others):
            outcome = "bit"
            for n in others:
                if n.active() and not n.can_decode(frame, tx):
                    n.drove_dominant("error flag")
        elif not any(n.active() and n.can_decode(frame, tx) for n in others):
            outcome = "ack"

        if outcome == "ok":
            self.stats.frames_ok += 1
            for n in others:
                if n.can_decode(frame, tx):
                    if n.active():
                        n.drove_dominant("ack")
                    n.rx(frame)
            self._busy_until = self.clock.now
            tx.tx_done(frame)
        else:
            if outcome == "ack":
                self.stats.ack_errors += 1
            else:
                self.stats.destroyed += 1
            for n in others:
                n.rx_error()
            self.stats.busy_us += error_us
            self._busy_until = self.clock.now + error_us
            tx.tx_error(frame, outcome)
        if self.keep_log:
            self.stats.log.append((self.clock.now, tx.name, frame, outcome))
        self.kick()


class SimNode(Node):
    """A simple external CAN node (a motor, a host's native CAN port...)."""

    def __init__(
        self,
        name: str,
        nominal_bps=1_000_000,
        data_bps=5_000_000,
        fd=True,
        listen_only=False,
    ):
        self.name = name
        self._nominal = nominal_bps
        self._data = data_bps
        self._fd = fd
        self.listen_only = listen_only
        self.present = True
        self.queue: list[Frame] = []
        self.received: list[Frame] = []
        self.sent: list[Frame] = []
        self.tx_errors: list[str] = []
        self.rx_errors = 0
        self.give_up_after_errors: int | None = None
        self._errors_in_a_row = 0
        self.on_rx = None  # optional callback(frame)

    def on_bus(self):
        return self.present

    def active(self):
        return self.present and not self.listen_only

    def nominal_bps(self):
        return self._nominal

    def data_bps(self):
        return self._data

    def fd_capable(self):
        return self._fd

    def send(self, frame: Frame):
        self.queue.append(frame)
        self.kick()

    def tx_pending(self):
        return self.queue[0] if self.queue else None

    def tx_done(self, frame):
        self.queue.pop(0)
        self.sent.append(frame)
        self._errors_in_a_row = 0

    def tx_error(self, frame, kind):
        self.tx_errors.append(kind)
        self._errors_in_a_row += 1
        if (
            self.give_up_after_errors
            and self._errors_in_a_row >= self.give_up_after_errors
        ):
            self.queue.pop(0)
            self._errors_in_a_row = 0

    def rx(self, frame):
        self.received.append(frame)
        if self.on_rx:
            self.on_rx(frame)

    def rx_error(self):
        self.rx_errors += 1
