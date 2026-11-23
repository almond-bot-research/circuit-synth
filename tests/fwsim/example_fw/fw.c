// The smallest firmware that can be simulated: it polls a "sensor" over SPI
// every millisecond and mirrors bit 0 of the answer onto an LED.
//
// A real project keeps this file's two halves apart: the firmware proper
// (above a HAL header) and a simulation HAL like the one at the bottom.
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cs_sim.h"

// --- HAL ---------------------------------------------------------------------
uint64_t hal_time_us(void);
void hal_spi_start(const uint8_t *tx, uint8_t *rx, size_t len);
bool hal_spi_busy(void);
void hal_led(bool on);

// --- firmware ------------------------------------------------------------------
static uint8_t tx[2] = {0x9F, 0x00}, rx[2];
static uint64_t next_poll;
static bool waiting;
uint32_t fw_polls;

void fw_poll(void) {
    if (waiting) {
        if (hal_spi_busy()) return;
        waiting = false;
        fw_polls++;
        hal_led(rx[1] & 1);
    }
    if (hal_time_us() >= next_poll) {
        next_poll = hal_time_us() + 1000;
        hal_spi_start(tx, rx, sizeof(tx));
        waiting = true;
    }
}

void fw_init(void) {
    next_poll = 0;
    waiting = false;
    fw_polls = 0;
}

// --- simulation HAL -------------------------------------------------------------
#define RES_SPI 0
#define SPI_HZ 1000000u

typedef void (*spi_fn)(const uint8_t *tx, uint8_t *rx, size_t len, uint64_t now_ns);
static spi_fn spi_cb;
uint8_t sim_led;

void sim_set_spi(spi_fn fn) { spi_cb = fn; }

uint64_t hal_time_us(void) { return cs_sim_now_ns / 1000u; }

void hal_spi_start(const uint8_t *t, uint8_t *r, size_t len) {
    spi_cb(t, r, len, cs_sim_now_ns);                         // the model answers now ...
    cs_sim_occupy(RES_SPI, (uint64_t)len * 8u * 1000000000u / SPI_HZ);  // ... the firmware sees it later
}

bool hal_spi_busy(void) { return cs_sim_busy(RES_SPI); }

void hal_led(bool on) { sim_led = on; }

void cs_sim_poll(void) { fw_poll(); }
