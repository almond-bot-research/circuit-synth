"""Behavioural model of the WIZnet W5500 Ethernet controller, written from
the datasheet (version 1.1.0).  Chapter numbers refer to it.

Modelled
- the SPI frame (2.2): 16-bit offset, control byte (block select, R/W,
  operation mode), data phase with address auto-increment, in variable
  length data mode (the board controls SCSn);
- the common register block (4.1): MR with software reset, GAR / SUBR / SHAR
  / SIPR, IR / IMR, SIR / SIMR, RTR / RCR, PHYCFGR (link status, PHY reset),
  VERSIONR;
- eight socket register blocks (4.2) in UDP mode: Sn_MR, Sn_CR (OPEN, CLOSE,
  SEND, SEND_MAC, RECV), Sn_IR / Sn_IMR, Sn_SR, port and destination
  registers, buffer sizes, TX / RX pointers, free and received sizes;
- the 16KB TX and RX memories (3.3) with their 16-bit pointer wrap;
- ARP as far as the host can see it: a SEND to an address that does not
  answer ends in Sn_IR.TIMEOUT after RTR x (RCR + 1);
- received datagrams with the 8-byte header (peer IP, port, length) the
  chip puts in front of the payload;
- the RSTn pin (5.5.1): held low for at least 500us it resets the chip; the
  chip then needs 1ms (PLL lock) before it answers on SPI.  `hang()` makes
  the chip deaf on SPI until such a reset, for recovery tests.

Not modelled: TCP, MACRAW, PPPoE, the PHY itself (auto-negotiation is a
delay), fragmentation, Ethernet framing, ICMP, real ARP traffic.

`violations` lists what the driver did that the datasheet does not allow
(SEND while a SEND is running, buffer sizes over 16KB, more data than fits
a UDP datagram, access in fixed-length mode, ...).

Where the datasheet is ambiguous the model can be switched:
`udp_closes_on_timeout` - the Sn_SR text says a time-out closes the socket
"regardless of previous status"; real UDP sockets are usually seen to stay
open.  Tests run both.
"""

from __future__ import annotations

from ..clock import Clock

# Common registers (table 3)
MR, GAR, SUBR, SHAR, SIPR = 0x0000, 0x0001, 0x0005, 0x0009, 0x000F
INTLEVEL, IR, IMR, SIR, SIMR, RTR, RCR = (
    0x0013,
    0x0015,
    0x0016,
    0x0017,
    0x0018,
    0x0019,
    0x001B,
)
UIPR, UPORTR, PHYCFGR, VERSIONR = 0x0028, 0x002C, 0x002E, 0x0039

# Socket registers (table 4)
SN_MR, SN_CR, SN_IR, SN_SR, SN_PORT, SN_DHAR, SN_DIPR, SN_DPORT = (
    0x00,
    0x01,
    0x02,
    0x03,
    0x04,
    0x06,
    0x0C,
    0x10,
)
SN_MSSR, SN_TOS, SN_TTL, SN_RXBUF_SIZE, SN_TXBUF_SIZE = 0x12, 0x15, 0x16, 0x1E, 0x1F
SN_TX_FSR, SN_TX_RD, SN_TX_WR, SN_RX_RSR, SN_RX_RD, SN_RX_WR = (
    0x20,
    0x22,
    0x24,
    0x26,
    0x28,
    0x2A,
)
SN_IMR, SN_FRAG, SN_KPALVTR = 0x2C, 0x2D, 0x2F

CMD_OPEN, CMD_CLOSE, CMD_SEND, CMD_SEND_MAC, CMD_RECV = 0x01, 0x10, 0x20, 0x21, 0x40
SOCK_CLOSED, SOCK_UDP = 0x00, 0x22
IR_SEND_OK, IR_TIMEOUT, IR_RECV = 0x10, 0x08, 0x04

BROADCAST = 0xFFFFFFFF
UDP_MAX_PAYLOAD = 1472


def ip_int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def ip_str(ip: int) -> str:
    return ".".join(str((ip >> s) & 0xFF) for s in (24, 16, 8, 0))


def ip_parse(s: str) -> int:
    return ip_int(bytes(int(x) for x in s.split(".")))


class Network:
    """The Ethernet segment the W5500 is plugged into."""

    def __init__(self):
        self.hosts: dict[int, object] = (
            {}
        )  # ip -> object with udp_in(src_ip, src_port, dst_ip, dst_port, payload)
        self.chips: list["W5500"] = []
        self.log: list = []

    def add_host(self, host):
        self.hosts[host.ip] = host
        host.network = self
        return host

    def resolves(self, ip: int) -> bool:
        """Would an ARP request for `ip` be answered?"""
        return ip in self.hosts and getattr(self.hosts[ip], "up", True)

    def from_chip(
        self, chip: "W5500", src_port: int, dst_ip: int, dst_port: int, payload: bytes
    ):
        self.log.append(("chip", chip.sipr, src_port, dst_ip, dst_port, payload))
        targets = (
            self.hosts.values()
            if dst_ip == BROADCAST or dst_ip == chip.subnet_broadcast()
            else [self.hosts[dst_ip]] if dst_ip in self.hosts else []
        )
        for host in list(targets):
            if getattr(host, "up", True):
                host.udp_in(chip.sipr, src_port, dst_ip, dst_port, payload)

    def to_chips(
        self, src_ip: int, src_port: int, dst_ip: int, dst_port: int, payload: bytes
    ):
        self.log.append(("host", src_ip, src_port, dst_ip, dst_port, payload))
        for chip in self.chips:
            chip.udp_in(src_ip, src_port, dst_ip, dst_port, payload)


class SimHost:
    """A computer on the segment, as far as UDP goes."""

    def __init__(self, ip: str | int):
        self.ip = ip_parse(ip) if isinstance(ip, str) else ip
        self.up = True
        self.network: Network | None = None
        self.received: list[tuple[int, int, int, bytes]] = (
            []
        )  # (src_ip, src_port, dst_port, payload)
        self.on_udp = None

    def udp_in(self, src_ip, src_port, dst_ip, dst_port, payload):
        self.received.append((src_ip, src_port, dst_port, payload))
        if self.on_udp:
            self.on_udp(src_ip, src_port, dst_port, payload)

    def sendto(self, payload: bytes, dst_ip: str | int, dst_port: int, src_port: int):
        dst = ip_parse(dst_ip) if isinstance(dst_ip, str) else dst_ip
        if self.up:
            self.network.to_chips(self.ip, src_port, dst, dst_port, bytes(payload))

    def take(self, dst_port: int | None = None) -> list[tuple[int, int, int, bytes]]:
        out = [r for r in self.received if dst_port is None or r[2] == dst_port]
        self.received = [r for r in self.received if r not in out]
        return out


class Socket:
    def __init__(self, n: int):
        self.n = n
        self.reg = bytearray(0x30)
        self.reg[SN_DHAR : SN_DHAR + 6] = b"\xff" * 6
        self.reg[SN_TTL] = 0x80
        self.reg[SN_RXBUF_SIZE] = 2
        self.reg[SN_TXBUF_SIZE] = 2
        self.reg[SN_IMR] = 0xFF
        self.reg[SN_FRAG] = 0x40
        self.sr = SOCK_CLOSED
        self.ir = 0
        self.tx = bytearray(16 * 1024)
        self.rx = bytearray(16 * 1024)
        self.tx_rd = self.tx_wr = 0
        self.rx_wr = 0
        self.rx_rd_committed = 0
        self.sending = False
        self.last_arp_ip = None

    def r16(self, off):
        return (self.reg[off] << 8) | self.reg[off + 1]

    @property
    def tx_size(self):
        return self.reg[SN_TXBUF_SIZE] * 1024

    @property
    def rx_size(self):
        return self.reg[SN_RXBUF_SIZE] * 1024


class W5500:
    def __init__(self, clock: Clock, network: Network):
        self.clock = clock
        self.network = network
        network.chips.append(self)
        self.violations: list[str] = []
        self.cable = True  # a link partner is connected
        self.autoneg_us = (
            50_000.0  # real auto-negotiation takes seconds; shortened for simulation
        )
        self.udp_closes_on_timeout = True
        self.tx_delay_us = 20.0  # from SEND to SEND_OK, on top of the wire time
        self.powered_reset_done = True
        self.spi_frames = 0
        self.spi_bytes = 0
        self.tx_datagrams = 0
        self.rx_dropped = 0
        self.arp_requests = 0
        self.rstn = 1
        self.hung = False
        self.hardware_resets = 0
        self._rst_low_since = 0.0
        self._ready_at = 0.0
        self.hardware_reset()

    # ------------------------------------------------------------------
    def _violation(self, text: str):
        self.violations.append(f"W5500 @{self.clock.now:.0f}us: {text}")

    def hardware_reset(self):
        """Power-on (RC on RSTn): registers at defaults, PHY restarts."""
        self._reset_registers()
        self._phy_restart()

    # --- RSTn pin (5.5.1: TRC 500us minimum, TPL 1ms maximum) -----------------
    def set_rstn(self, level: int):
        level = 1 if level else 0
        if level == self.rstn:
            return
        self.rstn = level
        if level == 0:
            self._rst_low_since = self.clock.now
            self.link = False
            self._phy_epoch += 1
            return
        if self.clock.now - self._rst_low_since < 500:
            self._violation("RSTn low for less than 500us")
        self.hung = False
        self.hardware_resets += 1
        self.hardware_reset()
        self._ready_at = self.clock.now + 1000

    def hang(self):
        """Fault injection: the chip stops answering on SPI (latch-up, a
        corrupted state machine).  Only a hardware reset brings it back."""
        self.hung = True
        self.link = False
        self._phy_epoch += 1

    def _reset_registers(self):
        self.common = bytearray(0x40)
        self.common[RTR : RTR + 2] = (0x07D0).to_bytes(2, "big")
        self.common[RCR] = 0x08
        self.common[0x001C] = 0x28  # PTIMER
        self.common[0x0026:0x0028] = b"\xff\xff"  # PMRU
        self.common[PHYCFGR] = 0xB8  # RST=1, OPMD=0, OPMDC=111
        self.common[VERSIONR] = 0x04
        self.ir = 0
        self.socks = [Socket(n) for n in range(8)]

    def _phy_restart(self):
        self.link = False
        self._phy_epoch = getattr(self, "_phy_epoch", 0) + 1
        epoch = self._phy_epoch
        self.clock.after(self.autoneg_us, lambda: self._phy_up(epoch))

    def _phy_up(self, epoch):
        if epoch == self._phy_epoch and self.cable and self.common[PHYCFGR] & 0x80:
            self.link = True

    def set_cable(self, connected: bool):
        self.cable = connected
        if connected:
            self._phy_restart()
        else:
            self.link = False

    # --- addresses ------------------------------------------------------
    @property
    def sipr(self) -> int:
        return ip_int(self.common[SIPR : SIPR + 4])

    @property
    def subr(self) -> int:
        return ip_int(self.common[SUBR : SUBR + 4])

    @property
    def gar(self) -> int:
        return ip_int(self.common[GAR : GAR + 4])

    @property
    def mac(self) -> bytes:
        return bytes(self.common[SHAR : SHAR + 6])

    def subnet_broadcast(self) -> int:
        return (self.sipr | ~self.subr) & 0xFFFFFFFF

    # --- interrupt pin --------------------------------------------------
    def _sir(self) -> int:
        return sum(1 << s.n for s in self.socks if s.ir)

    @property
    def int_n(self) -> int:
        return (
            0
            if (self.ir & self.common[IMR]) or (self._sir() & self.common[SIMR])
            else 1
        )

    # ------------------------------------------------------------------
    # SPI (chapter 2)
    # ------------------------------------------------------------------
    def spi(self, hdr: bytes, tx: bytes | None, length: int) -> bytes:
        """One frame, SCSn low to SCSn high.  Returns the bytes read."""
        self.spi_frames += 1
        self.spi_bytes += len(hdr) + length
        if self.rstn == 0 or self.hung:
            return bytes(length)  # in reset, or deaf: MISO stays low, writes are lost
        if self.clock.now < self._ready_at:
            self._violation(
                "SPI access less than 1ms after RSTn was released (PLL not locked)"
            )
            return bytes(length)
        if len(hdr) != 3:
            self._violation(f"SPI frame header of {len(hdr)} bytes")
            return bytes(length)
        addr = (hdr[0] << 8) | hdr[1]
        bsb, write, om = hdr[2] >> 3, bool(hdr[2] & 4), hdr[2] & 3
        if om != 0:
            self._violation(
                "fixed length data mode used; SCSn is controlled by the MCU on this board"
            )
        if length < 1:
            self._violation("SPI frame without a data phase")
        if write != (tx is not None):
            self._violation("R/W bit does not match the direction of the data phase")
        out = bytearray(length)
        block = bsb & 3
        sock = self.socks[bsb >> 2]
        if block == 0 and bsb != 0:
            self._violation(f"reserved block {bsb:05b} selected")
            return bytes(out)
        for i in range(length):
            a = (addr + i) & 0xFFFF
            if bsb == 0:
                if write:
                    self._write_common(a, tx[i])
                else:
                    out[i] = self._read_common(a)
            elif block == 1:
                if write:
                    self._write_sock(sock, a, tx[i])
                else:
                    out[i] = self._read_sock(sock, a)
            elif block == 2:
                if not sock.tx_size:
                    self._violation(f"socket {sock.n} TX buffer has size 0")
                elif write:
                    sock.tx[a & (sock.tx_size - 1)] = tx[i]
                else:
                    out[i] = sock.tx[a & (sock.tx_size - 1)]
            else:
                if not sock.rx_size:
                    self._violation(f"socket {sock.n} RX buffer has size 0")
                elif write:
                    self._violation("write to an RX buffer")
                else:
                    out[i] = sock.rx[a & (sock.rx_size - 1)]
        return bytes(out)

    # --- common block ---------------------------------------------------
    def _read_common(self, a: int) -> int:
        if a == IR:
            return self.ir
        if a == SIR:
            return self._sir()
        if a == PHYCFGR:
            v = self.common[PHYCFGR] & 0xF8
            return v | (0x07 if self.link else 0x00)  # 100M full duplex when up
        return self.common[a] if a < len(self.common) else 0

    def _write_common(self, a: int, v: int):
        if a == MR:
            if v & 0x80:
                self._reset_registers()  # self-clearing; the PHY keeps its link
                return
            self.common[MR] = v & 0x3A
        elif a == IR:
            self.ir &= ~v
        elif a == SIR:
            pass
        elif a == PHYCFGR:
            was = self.common[PHYCFGR]
            self.common[PHYCFGR] = v & 0xF8
            if was & 0x80 and not v & 0x80:
                self.link = False
                self._phy_epoch += 1
            elif not was & 0x80 and v & 0x80:
                self._phy_restart()
        elif a in (VERSIONR,) or UIPR <= a < UIPR + 6:
            pass
        elif a < len(self.common):
            self.common[a] = v

    # --- socket block -----------------------------------------------------
    def _read_sock(self, s: Socket, a: int) -> int:
        if a >= 0x30:
            return 0
        if a == SN_CR:
            return 0  # cleared as soon as the command is accepted
        if a == SN_IR:
            return s.ir
        if a == SN_SR:
            return s.sr
        if a in (SN_TX_FSR, SN_TX_FSR + 1):
            free = s.tx_size - ((s.r16(SN_TX_WR) - s.tx_rd) & 0xFFFF)
            return (max(free, 0) >> (8 if a == SN_TX_FSR else 0)) & 0xFF
        if a in (SN_TX_RD, SN_TX_RD + 1):
            return (s.tx_rd >> (8 if a == SN_TX_RD else 0)) & 0xFF
        if a in (SN_RX_RSR, SN_RX_RSR + 1):
            used = (s.rx_wr - s.rx_rd_committed) & 0xFFFF
            return (used >> (8 if a == SN_RX_RSR else 0)) & 0xFF
        if a in (SN_RX_WR, SN_RX_WR + 1):
            return (s.rx_wr >> (8 if a == SN_RX_WR else 0)) & 0xFF
        return s.reg[a]

    def _write_sock(self, s: Socket, a: int, v: int):
        if a >= 0x30:
            return
        if a == SN_CR:
            self._command(s, v)
        elif a == SN_IR:
            s.ir &= ~v
        elif a in (
            SN_SR,
            SN_TX_FSR,
            SN_TX_FSR + 1,
            SN_TX_RD,
            SN_TX_RD + 1,
            SN_RX_RSR,
            SN_RX_RSR + 1,
            SN_RX_WR,
            SN_RX_WR + 1,
        ):
            pass  # read only
        elif a in (SN_RXBUF_SIZE, SN_TXBUF_SIZE):
            if v not in (0, 1, 2, 4, 8, 16):
                self._violation(f"socket {s.n} buffer size {v}KB is not 0/1/2/4/8/16")
                return
            s.reg[a] = v
            total = sum(x.reg[a] for x in self.socks)
            # checked when the last socket has been written (the driver writes all eight)
            if s.n == 7 and total > 16:
                self._violation(f"buffer sizes add up to {total}KB, 16KB available")
        else:
            s.reg[a] = v

    def _raise(self, s: Socket, bit: int):
        if s.reg[SN_IMR] & bit:
            s.ir |= bit

    def _command(self, s: Socket, cmd: int):
        if cmd == CMD_OPEN:
            proto = s.reg[SN_MR] & 0x0F
            if proto != 0x02:
                self._violation(
                    f"socket {s.n}: OPEN with protocol {proto} (only UDP is modelled)"
                )
                return
            s.sr = SOCK_UDP
            s.tx_rd = 0
            s.reg[SN_TX_WR : SN_TX_WR + 2] = b"\0\0"
            s.rx_wr = s.rx_rd_committed = 0
            s.reg[SN_RX_RD : SN_RX_RD + 2] = b"\0\0"
            s.sending = False
            s.last_arp_ip = None
        elif cmd == CMD_CLOSE:
            s.sr = SOCK_CLOSED
            s.sending = False
        elif cmd in (CMD_SEND, CMD_SEND_MAC):
            self._send(s, cmd == CMD_SEND_MAC)
        elif cmd == CMD_RECV:
            s.rx_rd_committed = s.r16(SN_RX_RD)
        else:
            self._violation(f"socket {s.n}: command 0x{cmd:02X} not modelled")

    def _send(self, s: Socket, with_mac: bool):
        if s.sr != SOCK_UDP:
            self._violation(f"socket {s.n}: SEND while not open")
            return
        if s.sending:
            self._violation(f"socket {s.n}: SEND before the previous SEND completed")
            return
        wr = s.r16(SN_TX_WR)
        n = (wr - s.tx_rd) & 0xFFFF
        if n == 0 or n > s.tx_size:
            self._violation(
                f"socket {s.n}: SEND of {n} bytes with a {s.tx_size} byte buffer"
            )
            return
        if n > UDP_MAX_PAYLOAD:
            self._violation(
                f"socket {s.n}: {n} bytes do not fit one UDP datagram (no fragmentation)"
            )
        payload = bytes(s.tx[(s.tx_rd + i) & (s.tx_size - 1)] for i in range(n))
        dst_ip, dst_port = ip_int(s.reg[SN_DIPR : SN_DIPR + 4]), s.r16(SN_DPORT)
        s.sending = True
        epoch = self._sock_epoch(s)
        broadcast = dst_ip in (BROADCAST, self.subnet_broadcast())
        if not self.link:
            # No carrier: the frame goes nowhere.  The chip still reports
            # the SEND as done (nothing checks the wire for UDP broadcast);
            # for unicast the ARP request gets no answer.
            reachable = broadcast
        elif broadcast or with_mac:
            reachable = True
        else:
            on_link = (dst_ip & self.subr) == (self.sipr & self.subr)
            next_hop = dst_ip if on_link else self.gar
            if self.common[MR] & 0x02 or s.last_arp_ip != dst_ip:
                self.arp_requests += 1
            reachable = self.network.resolves(next_hop)
            if reachable:
                s.last_arp_ip = dst_ip

        def done():
            if self._sock_epoch(s) != epoch or not s.sending:
                return
            s.sending = False
            s.tx_rd = wr
            if reachable:
                self.tx_datagrams += 1
                self._raise(s, IR_SEND_OK)
                if self.link:
                    self.network.from_chip(
                        self, s.r16(SN_PORT), dst_ip, dst_port, payload
                    )
            else:
                self._raise(s, IR_TIMEOUT)
                if self.udp_closes_on_timeout:
                    s.sr = SOCK_CLOSED

        if reachable:
            # wire time at 100Mbit/s plus a little processing
            self.clock.after(self.tx_delay_us + (n + 66) * 0.08, done)
        else:
            rtr = (self.common[RTR] << 8) | self.common[RTR + 1]
            self.clock.after(rtr * 100.0 * (self.common[RCR] + 1), done)

    def _sock_epoch(self, s: Socket):
        return id(s)  # a software reset replaces the socket objects

    # ------------------------------------------------------------------
    # network side
    # ------------------------------------------------------------------
    def udp_in(
        self, src_ip: int, src_port: int, dst_ip: int, dst_port: int, payload: bytes
    ):
        if not self.link or self.hung or self.rstn == 0:
            return
        broadcast = dst_ip in (BROADCAST, self.subnet_broadcast())
        if not broadcast and dst_ip != self.sipr:
            return
        for s in self.socks:
            if s.sr != SOCK_UDP or s.r16(SN_PORT) != dst_port:
                continue
            if broadcast and s.reg[SN_MR] & 0x40:  # BCASTB
                continue
            need = len(payload) + 8
            used = (s.rx_wr - s.rx_rd_committed) & 0xFFFF
            if need > s.rx_size - used:
                self.rx_dropped += 1
                return
            packet = (
                src_ip.to_bytes(4, "big")
                + src_port.to_bytes(2, "big")
                + len(payload).to_bytes(2, "big")
                + payload
            )
            for i, b in enumerate(packet):
                s.rx[(s.rx_wr + i) & (s.rx_size - 1)] = b
            s.rx_wr = (s.rx_wr + need) & 0xFFFF
            self._raise(s, IR_RECV)
            return
        # no socket on that port: the real chip answers with ICMP port unreachable
