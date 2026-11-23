# Firmware simulation (`cs sim`)

Run a board's real firmware on Linux, against models of the board's parts,
wired the way the netlist says. The point is to find out what the firmware
cannot do with a board while the board can still change.

(This is not `circuit_synth.simulation`, which is SPICE simulation of the
circuit itself.)

```bash
cs sim run designs/almond_can_hub              # every suite under a design
cs sim run designs/almond_can_hub/firmware_rev2 -k termination -v
cs sim pinmap src/board.h layouts/default/default.net --mcu RP2354 --unused BOARD_UNUSED_GPIOS
cs sim parts                                   # device models that ship with circuit-synth
cs sim csrc                                    # where cs_sim.h / cs_sim.c live
```

## What it proves and what it does not

- It catches logic and sequencing errors, protocol errors, and pin-map
  errors (the firmware's pins against the netlist).
- It does not prove analog behaviour, timing margins, or anything a model
  gets wrong. A model and a driver written by the same person from the same
  datasheet share their misreadings.
- Code below the HAL (the real SPI driver, interrupts, the USB stack) is not
  run at all.

Say so wherever you report results.

## How it fits together

```
firmware (C)        everything above a small HAL header
  |  HAL             SPI transfer, pin read, time, ...
sim HAL (C)          each HAL call becomes a callback into Python;
  |                  cs_sim.c supplies simulated time
harness (Python)     FirmwareSim subclass: owns the models, answers the callbacks
  |
models (Python)      circuit_synth.fwsim.parts, wired from the netlist (Board)
```

- **Time is simulated.** `cs_sim_run()` calls the firmware's loop body
  (`cs_sim_poll()`, which you provide) and moves the clock to whatever is
  next: the end of a transfer, a model event, or one quantum (10us).
- **Transfers take time.** The sim HAL calls `cs_sim_occupy(resource, ns)`
  when a transfer starts and `cs_sim_busy(resource)` to ask whether it is
  over. FIFO depths, service latency and overflow then behave as they would
  at the real bus clock. A blocking transfer occupies a "CPU" resource
  instead, and the HAL skips that loop while it is busy.
- **Wiring comes from the netlist.** Use `Board` to find which chip is on
  which chip select, what a pin drives, where the pull resistors are. Then a
  wrong pin in the firmware changes what the simulated firmware talks to.

## Adding a simulation to a design

1. **Split the firmware at a HAL header.** Everything above it must compile
   on Linux (no SDK headers). Keep the HAL thin: logic below it is logic the
   tests cannot see.
2. **Name the pins after the nets.** `#define PIN_<net> <gpio>` for every
   MCU pin the firmware uses, in one header. `cs sim pinmap` (or
   `pinmap.check_pin_macros` in a test) compares them with the netlist.
3. **Write the sim HAL** (`sim/hal_sim.c`): every HAL function calls a
   function pointer set from Python, and uses `cs_sim.h` for time. See
   `tests/fwsim/example_fw/fw.c` for the smallest complete example.
4. **Build a shared library** from the firmware, the sim HAL and
   `cs_sim.c`. A `Makefile` with a `sim` target is what `cs sim run` builds:

   ```make
   CSRC := $(shell uv run cs sim csrc)
   sim: build/sim/libfw.so
   build/sim/libfw.so: $(wildcard src/*.c sim/*.c)
   	@mkdir -p $(dir $@)
   	$(CC) -shared -fPIC -O1 -Wall -Wextra -Werror -Isrc -Isim -I$(CSRC) -o $@ $^ $(CSRC)/cs_sim.c
   ```

5. **Write the harness**: a `FirmwareSim` subclass that creates the models,
   registers the callbacks and copies model outputs (interrupt lines) into
   the HAL in `sync_pins()`. Call `self.enter(now_ns)` first and
   `self.sync()` last in every callback.
6. **Write tests** in `tests/test_*.py` (unittest). `cs sim run <dir>` finds
   every directory with such a `tests/` package, runs `make sim` if there is
   one, then the tests.

`designs/almond_can_hub/firmware_rev2` in circuits-py is the worked example:
four CAN controllers on two SPI buses, an Ethernet controller, two host
links, 120 tests.

## Device models

`circuit_synth.fwsim.parts` (list them with `cs sim parts`):

| Model | Part |
| --- | --- |
| `mcp2518fd` | Microchip MCP2518FD CAN FD controller |
| `w5500` | WIZnet W5500 Ethernet controller (UDP sockets) |

Plus `circuit_synth.fwsim.canbus`: a frame-level CAN / CAN FD bus
(arbitration, ACK, error frames, bit-rate switching, termination count) and
`SimNode`, a simple node to put on it.

Rules for a new model:

- Write it from the datasheet, not from a driver. Put section and register
  numbers in the comments.
- Share nothing with the firmware: no generated headers, no common tables.
- Record everything the driver does that the datasheet forbids or does not
  define in `violations`; tests require it to stay empty.
- Where the datasheet is silent, take the reading that is hardest on the
  firmware, say so in the docstring, and make it switchable if drivers
  should cope with both.
- Say in the docstring what is not modelled.
