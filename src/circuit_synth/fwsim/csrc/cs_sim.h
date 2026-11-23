// circuit-synth firmware simulation: simulated time for firmware built on
// Linux (see docs/FIRMWARE_SIM.md).
//
// The firmware is compiled as a shared library together with this file and
// a HAL written for the simulation.  Python models of the board's parts
// (circuit_synth.fwsim) stand behind the HAL.  Time only moves in
// cs_sim_run(): it calls the firmware's main-loop body, cs_sim_poll(), and
// advances the clock to whatever comes next - a transfer finishing, a model
// event, or one quantum.
#ifndef CS_SIM_H
#define CS_SIM_H

#include <stdbool.h>
#include <stdint.h>

#define CS_SIM_RESOURCES 16

extern uint64_t cs_sim_now_ns;         // simulated time
extern uint64_t cs_sim_next_event_ns;  // next model event; written by the Python side
extern uint32_t cs_sim_quantum_ns;     // time step when nothing else is due (default 10us)

// Provided by the firmware's simulation HAL: one pass of its main loop(s).
void cs_sim_poll(void);

// Called when simulated time reaches a model event (and at the end of a
// run): the models catch up and refresh pin states.
typedef void (*cs_sim_advance_fn)(uint64_t now_ns);
void cs_sim_set_advance(cs_sim_advance_fn fn);

// Things that take time: an SPI bus, a CPU context that blocks.  A HAL
// calls cs_sim_occupy() when a transfer starts; cs_sim_busy() says whether
// it is still going on.  Occupying a busy resource queues behind it.
void cs_sim_occupy(unsigned resource, uint64_t duration_ns);
bool cs_sim_busy(unsigned resource);
uint64_t cs_sim_busy_until(unsigned resource);
uint64_t cs_sim_busy_total_ns(unsigned resource);

// Run until simulated time `until_us`, or until cs_sim_stop().
void cs_sim_run(uint64_t until_us);
void cs_sim_stop(void);
bool cs_sim_stopped(void);
// Back to time zero, nothing busy (a new simulation in the same process).
void cs_sim_reset(void);

#endif
