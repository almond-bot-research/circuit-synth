#include "cs_sim.h"

uint64_t cs_sim_now_ns;
uint64_t cs_sim_next_event_ns = UINT64_MAX;
uint32_t cs_sim_quantum_ns = 10000;

static cs_sim_advance_fn advance_cb;
static uint64_t busy_until[CS_SIM_RESOURCES];
static uint64_t busy_total[CS_SIM_RESOURCES];
static bool stopped;

void cs_sim_set_advance(cs_sim_advance_fn fn) {
    advance_cb = fn;
}

void cs_sim_occupy(unsigned resource, uint64_t duration_ns) {
    if (busy_until[resource] < cs_sim_now_ns) busy_until[resource] = cs_sim_now_ns;
    busy_until[resource] += duration_ns;
    busy_total[resource] += duration_ns;
}

bool cs_sim_busy(unsigned resource) {
    return cs_sim_now_ns < busy_until[resource];
}

uint64_t cs_sim_busy_until(unsigned resource) {
    return busy_until[resource];
}

uint64_t cs_sim_busy_total_ns(unsigned resource) {
    return busy_total[resource];
}

void cs_sim_stop(void) {
    stopped = true;
}

bool cs_sim_stopped(void) {
    return stopped;
}

void cs_sim_reset(void) {
    cs_sim_now_ns = 0;
    cs_sim_next_event_ns = UINT64_MAX;
    cs_sim_quantum_ns = 10000;
    stopped = false;
    for (unsigned i = 0; i < CS_SIM_RESOURCES; i++) busy_until[i] = busy_total[i] = 0;
}

void cs_sim_run(uint64_t until_us) {
    uint64_t until = until_us * 1000u;
    while (cs_sim_now_ns < until && !stopped) {
        cs_sim_poll();
        uint64_t next = cs_sim_now_ns + cs_sim_quantum_ns;
        for (unsigned i = 0; i < CS_SIM_RESOURCES; i++) {
            if (busy_until[i] > cs_sim_now_ns && busy_until[i] < next) next = busy_until[i];
        }
        if (next > until) next = until;
        if (cs_sim_next_event_ns <= next) {
            if (cs_sim_next_event_ns > cs_sim_now_ns) cs_sim_now_ns = cs_sim_next_event_ns;
            if (advance_cb) advance_cb(cs_sim_now_ns);
        } else {
            cs_sim_now_ns = next;
        }
    }
    if (!stopped && advance_cb) advance_cb(cs_sim_now_ns);
}
