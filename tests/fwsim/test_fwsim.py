"""circuit_synth.fwsim: models, netlist tracing, pin-map check, harness, runner, CLI."""

import ctypes
import subprocess
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from circuit_synth.fwsim import (
    CSRC_DIR,
    Board,
    CanBus,
    Clock,
    FirmwareSim,
    Frame,
    SimNode,
    pinmap,
    runner,
)
from circuit_synth.fwsim.parts import mcp2518fd as mcp
from circuit_synth.fwsim.parts import w5500 as wiz
from circuit_synth.tools.cs_cli import cli

HERE = Path(__file__).parent

NETLIST = textwrap.dedent(
    """\
    (export (version "E")
      (components
        (comp (ref "U1") (value "RP2354A") (property (name "address") (value "mcu")))
        (comp (ref "U2") (value "MCP2518FDT-H/SL") (property (name "address") (value "can")))
        (comp (ref "R1") (value "10k")))
      (nets
        (net (code "1") (name "CAN_CS_N")
          (node (ref "U1") (pin "43") (pinfunction "GPIO29_ADC3"))
          (node (ref "U2") (pin "13") (pinfunction "nCS"))
          (node (ref "R1") (pin "1")))
        (net (code "2") (name "V3V3") (node (ref "R1") (pin "2")))
        (net (code "3") (name "CAN_SCK")
          (node (ref "U1") (pin "40") (pinfunction "GPIO26_ADC0"))
          (node (ref "U2") (pin "10") (pinfunction "SCK")))
        (net (code "4") (name "unconnected-(U1-GPIO5-Pad8)")
          (node (ref "U1") (pin "8") (pinfunction "GPIO5")))))
    """
)


# --- CAN bus ---------------------------------------------------------------------
def test_arbitration_ack_and_format_rules():
    clock = Clock()
    bus = CanBus(clock, "bus", external_terminations=2)
    a, b = bus.attach(SimNode("a")), bus.attach(SimNode("b"))
    a.send(Frame(0x200, b"low priority"[:8]))
    b.send(Frame(0x100, b"first"))
    clock.advance(1000)
    assert [f.can_id for f in a.received] == [0x100]
    assert [f.can_id for f in b.received] == [0x200]
    assert bus.stats.arbitration_losses == 1

    # a classic-only node destroys CAN FD frames; a listen-only one does not ACK
    lonely = CanBus(clock, "lonely", external_terminations=2)
    tx = lonely.attach(SimNode("tx"))
    lonely.attach(SimNode("monitor", listen_only=True))
    tx.give_up_after_errors = 2
    tx.send(Frame(0x1, b"x"))
    clock.advance(5000)
    assert tx.tx_errors == ["ack", "ack"]
    old = lonely.attach(SimNode("classic", fd=False))
    tx.send(Frame(0x2, bytes(16), fd=True, brs=True))
    clock.advance(5000)
    assert tx.tx_errors[-1] == "bit" and old.received == []


def test_unterminated_bus_carries_nothing():
    clock = Clock()
    bus = CanBus(clock, "bus")
    a, b = bus.attach(SimNode("a")), bus.attach(SimNode("b"))
    a.give_up_after_errors = 1
    a.send(Frame(0x1, b""))
    clock.advance(1000)
    assert b.received == [] and bus.terminations() == 0
    relay = [True]
    bus.term_sources.append(lambda: relay[0])
    a.send(Frame(0x1, b""))
    clock.advance(1000)
    assert len(b.received) == 1


def test_frame_duration_follows_bit_rate_switching():
    classic = Frame(0x123, bytes(8)).duration_us(1e6, 5e6, 0)
    fd = Frame(0x123, bytes(64), fd=True, brs=True).duration_us(1e6, 5e6, 0)
    slow = Frame(0x123, bytes(64), fd=True).duration_us(1e6, 5e6, 0)
    assert classic == pytest.approx(111)  # 111 bits at 1Mbit/s
    assert fd < 160 < slow


# --- MCP2518FD ---------------------------------------------------------------------
def rd(chip, addr, n=4):
    return int.from_bytes(
        chip.spi(bytes([0x30 | addr >> 8, addr & 0xFF]) + bytes(n))[2:], "little"
    )


def wr(chip, addr, data: bytes):
    chip.spi(bytes([0x20 | addr >> 8, addr & 0xFF]) + data)


def test_mcp2518fd_reset_state_and_rules():
    chip = mcp.Mcp2518fd("U1", Clock())
    assert rd(chip, mcp.C1CON) == 0x04980760
    assert rd(chip, mcp.C1NBTCFG) == 0x003E0F0F
    assert rd(chip, mcp.OSC) & (1 << 10)  # OSCRDY
    assert chip.gpio0() is None and chip.int_n == 1  # GPIO0 an input, no interrupt
    wr(chip, mcp.IOCON + 1, b"\x01")
    wr(chip, mcp.IOCON, b"\x00")
    assert chip.gpio0() == 1 and chip.violations == []
    wr(chip, mcp.IOCON, b"\x00\x00")  # IOCON takes single-byte writes only
    assert "IOCON" in chip.violations[-1]
    wr(chip, mcp.C1CON + 3, b"\x03")  # listen-only, off the bus model: at once
    wr(
        chip, mcp.C1NBTCFG, bytes(4)
    )  # configuration register outside Configuration mode
    assert "outside Configuration mode" in chip.violations[-1]


def test_mcp2518fd_receives_into_its_fifo():
    clock = Clock()
    bus = CanBus(clock, "bus", external_terminations=2)
    chip = bus.attach(mcp.Mcp2518fd("U1", clock))
    node = bus.attach(
        SimNode("node", nominal_bps=500_000, data_bps=2_000_000)
    )  # the chip's reset bit timing
    wr(chip, mcp.C1CON + 2, b"\x00")  # no TXQ, no TEF
    wr(
        chip, mcp.C1FIFOCON1, (0x00 | 1 | 7 << 24).to_bytes(4, "little")
    )  # FIFO1: receive, 8 deep, not-empty irq
    wr(chip, mcp.C1FLTCON0, b"\x81")  # filter 0 -> FIFO1, accept all
    wr(chip, mcp.C1INT + 2, b"\x02")  # RXIE
    wr(chip, mcp.C1CON + 3, b"\x00")  # normal CAN FD mode
    clock.advance(100)
    node.send(Frame(0x321, b"\xde\xad", fd=True, brs=True))
    clock.advance(1000)
    assert node.sent and chip.int_n == 0
    obj = chip.spi(bytes([0x34, 0x00]) + bytes(16))[2:]
    assert int.from_bytes(obj[0:4], "little") == 0x321
    assert obj[4] == 0xC2 and obj[8:10] == b"\xde\xad"  # FDF | BRS | DLC 2
    wr(chip, mcp.C1FIFOCON1 + 1, b"\x01")  # UINC
    assert chip.int_n == 1 and chip.violations == []


# --- W5500 ---------------------------------------------------------------------------
def test_w5500_udp_round_trip_and_arp_timeout():
    clock = Clock()
    net = wiz.Network()
    chip = wiz.W5500(clock, net)
    host = net.add_host(wiz.SimHost("10.0.0.1"))
    clock.advance(100_000)  # link up

    def w(block, addr, data):
        chip.spi(
            bytes([addr >> 8, addr & 0xFF, block << 3 | 4]), bytes(data), len(data)
        )

    def r(block, addr, n):
        return chip.spi(bytes([addr >> 8, addr & 0xFF, block << 3]), None, n)

    assert r(0, wiz.VERSIONR, 1) == b"\x04"
    w(0, wiz.SUBR, [255, 255, 255, 0])
    w(0, wiz.SIPR, [10, 0, 0, 2])
    w(1, wiz.SN_MR, [0x02])
    w(1, wiz.SN_PORT, [0x13, 0x88])
    w(1, wiz.SN_CR, [wiz.CMD_OPEN])
    assert r(1, wiz.SN_SR, 1) == bytes([wiz.SOCK_UDP])
    host.sendto(b"ping", "10.0.0.2", 5000, 6000)
    assert chip.int_n == 1  # SIMR is 0 after reset: no interrupt pin without it
    assert r(1, wiz.SN_RX_RSR, 2) == b"\x00\x0c"
    assert r(3, 0, 12) == bytes([10, 0, 0, 1, 0x17, 0x70, 0, 4]) + b"ping"
    w(1, wiz.SN_DIPR, [10, 0, 0, 1, 0x17, 0x70])
    w(2, 0, b"pong")
    w(1, wiz.SN_TX_WR, [0, 4])
    w(1, wiz.SN_CR, [wiz.CMD_SEND])
    clock.advance(1000)
    assert host.take(6000) == [(wiz.ip_parse("10.0.0.2"), 5000, 6000, b"pong")]
    w(1, wiz.SN_IR, [0xFF])
    w(1, wiz.SN_DIPR, [10, 0, 0, 99, 0x17, 0x70])  # nobody there
    w(2, 4, b"lost")
    w(1, wiz.SN_TX_WR, [0, 8])
    w(1, wiz.SN_CR, [wiz.CMD_SEND])
    clock.advance(2_000_000)
    assert r(1, wiz.SN_IR, 1)[0] & wiz.IR_TIMEOUT and chip.violations == []


def test_w5500_reset_pin():
    clock = Clock()
    chip = wiz.W5500(clock, wiz.Network())

    def version():
        return chip.spi(bytes([0, wiz.VERSIONR, 0]), None, 1)

    assert version() == b"\x04"
    chip.hang()
    assert version() == b"\x00"
    chip.spi(
        bytes([0, wiz.MR, 4]), b"\x80", 1
    )  # a software reset cannot reach a deaf chip
    assert version() == b"\x00"
    chip.set_rstn(0)
    clock.advance(600)
    chip.set_rstn(1)
    assert version() == b"\x00" and "PLL" in chip.violations.pop()  # too early
    clock.advance(1000)
    assert version() == b"\x04" and chip.hardware_resets == 1 and chip.violations == []
    chip.set_rstn(0)
    clock.advance(100)
    chip.set_rstn(1)
    assert "500us" in chip.violations[-1]


# --- netlist and pin map ----------------------------------------------------------------
@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.net"
    path.write_text(NETLIST)
    return Board(path)


def test_board_tracing(board):
    mcu = board.one("RP2354")
    assert board.gpio_nets(mcu) == {
        29: "CAN_CS_N",
        26: "CAN_SCK",
        5: "unconnected-(U1-GPIO5-Pad8)",
    }
    assert board.chip_on("CAN_CS_N", "nCS", "MCP2518").ref == "U2"
    assert board.has_pull("CAN_CS_N", "V3V3") and not board.has_pull("CAN_SCK", "V3V3")
    assert board.net.by_address("can").net(10) == "CAN_SCK"


def test_pin_macros(board, tmp_path):
    header = tmp_path / "board.h"
    header.write_text(
        "#define PIN_CAN_CS_N 29\n#define PIN_CAN_SCK 0x1A  // SPI1\n#define UNUSED { 5 }\n#define OTHER 7u\n"
    )
    defines = pinmap.header_defines(header)
    _, gpio = pinmap.mcu_gpio_nets(board, "RP2354")
    assert (
        pinmap.check_pin_macros(
            defines, gpio, unused=pinmap.header_int_list(header, "UNUSED")
        )
        == []
    )
    defines["PIN_CAN_CS_N"] = 25
    defines["PIN_GHOST"] = 3
    del defines["PIN_CAN_SCK"]
    problems = pinmap.check_pin_macros(defines, gpio, unused={5, 6})
    assert len(problems) == 4
    assert pinmap.check_rp2_spi("CAN", 1, sck=26, mosi=27, miso=28) == []
    assert "SPI0 SCK" in pinmap.check_rp2_spi("ETH", 1, sck=6, mosi=11, miso=12)[0]


# --- harness: a real (tiny) firmware in simulated time --------------------------------------
@pytest.fixture(scope="module")
def example_lib(tmp_path_factory):
    out = tmp_path_factory.mktemp("fw") / "libexample.so"
    subprocess.run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-O1",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{CSRC_DIR}",
            "-o",
            str(out),
            str(HERE / "example_fw" / "fw.c"),
            str(CSRC_DIR / "cs_sim.c"),
        ],
        check=True,
    )
    return out


class ExampleSim(FirmwareSim):
    SPI_FN = ctypes.CFUNCTYPE(
        None,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_size_t,
        ctypes.c_uint64,
    )

    def __init__(self, lib):
        super().__init__(lib)
        self.sensor = 0
        self.calls = []
        self._spi = self.SPI_FN(self._on_spi)
        self.lib.sim_set_spi(self._spi)
        self.lib.fw_init()

    def _on_spi(self, tx, rx, n, now_ns):
        self.enter(now_ns)
        self.calls.append((now_ns, bytes(tx[:n])))
        rx[1] = self.sensor
        self.sync()


def test_firmware_runs_in_simulated_time(example_lib):
    sim = ExampleSim(example_lib)
    sim.run(10)
    assert sim.now_ms == pytest.approx(10)
    assert 9 <= len(sim.calls) <= 11 and sim.calls[0][1] == b"\x9f\x00"
    assert sim.lib_var("sim_led").value == 0
    # a model event in the future: the firmware sees its effect at the right time
    sim.clock.at(sim.clock.now + 4200, lambda: setattr(sim, "sensor", 1))
    sim.run(4)
    assert sim.lib_var("sim_led").value == 0
    sim.run(2)
    assert sim.lib_var("sim_led").value == 1
    # the SPI transfer took simulated time: 16 bits at 1MHz
    assert sim.lib.cs_sim_busy_total_ns(0) == len(sim.calls) * 16_000
    assert sim.run_until(
        lambda: sim.lib_var("fw_polls", ctypes.c_uint32).value >= 30, timeout_ms=100
    )


# --- runner and CLI ------------------------------------------------------------------------------
def test_runner_finds_and_runs_suites(tmp_path):
    fw = tmp_path / "design" / "firmware"
    (fw / "tests").mkdir(parents=True)
    (fw / "tests" / "__init__.py").write_text("")
    (fw / "tests" / "test_ok.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_built(self):\n"
        "        import pathlib\n        self.assertTrue(pathlib.Path('built').exists())\n"
    )
    (fw / "Makefile").write_text("sim:\n\ttouch built\n")
    (tmp_path / "design" / "layouts").mkdir()
    assert runner.find_suites(tmp_path / "design") == [fw]
    assert runner.run_suite(fw) == 0
    (fw / "tests" / "test_bad.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_no(self):\n        self.fail()\n"
    )
    assert runner.run_suite(fw) != 0
    assert runner.run_suite(fw, pattern="test_built") == 0
    result = CliRunner().invoke(
        cli, ["sim", "run", str(tmp_path / "design" / "layouts")]
    )
    assert result.exit_code != 0 and "no simulation suite" in result.output


def test_cli(board, tmp_path):
    cli_runner = CliRunner()
    assert "mcp2518fd" in cli_runner.invoke(cli, ["sim", "parts"]).output
    assert Path(
        cli_runner.invoke(cli, ["sim", "csrc"]).output.strip(), "cs_sim.c"
    ).exists()
    net = tmp_path / "board.net"
    header = tmp_path / "pins.h"
    header.write_text("#define PIN_CAN_CS_N 29\n#define PIN_CAN_SCK 27\n")
    result = cli_runner.invoke(
        cli, ["sim", "pinmap", str(header), str(net), "--mcu", "RP2354"]
    )
    assert (
        result.exit_code == 1
        and "PIN_CAN_SCK = 27 but net CAN_SCK is on GPIO26" in result.output
    )
    header.write_text("#define PIN_CAN_CS_N 29\n#define PIN_CAN_SCK 26\n")
    result = cli_runner.invoke(
        cli, ["sim", "pinmap", str(header), str(net), "--mcu", "RP2354"]
    )
    assert result.exit_code == 0 and "0 problem(s)" in result.output
