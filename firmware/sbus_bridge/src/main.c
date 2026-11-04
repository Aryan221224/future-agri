/* S-BUS bridge firmware for the RP2040 (Raspberry Pi Pico / Pico W / any RP2040 board).
 *
 * Wiring (see docs/hardware/k3a-autonomy-kit.md):
 *   GP1  <- pilot receiver S-BUS out (also wired to the fail-over mux input A)
 *   GP0  -> fail-over mux input B (bridge S-BUS out)
 *   GP2  -> fail-over mux SELECT (pull-down: 0 = pilot receiver straight to the K3-A Pro)
 *   GP4  -> companion RX (Raspberry Pi UART, 3.3 V)   GP5 <- companion TX
 *   USB  -> companion alternative link (CDC serial), same frames
 *   GP25    LED (Pico): slow blink = pilot, fast = companion, double = hold, solid = RTH
 * The mux output goes to the K3-A Pro RC port. S-BUS is inverted 100000 baud 8E2; the
 * RP2040 inverts its UART pins in hardware, so no external inverter is needed.
 *
 * Fail-safe by construction: SELECT is driven high only while the core chooses a
 * companion-side source (companion, hold, RTH). A hang trips the watchdog (50 ms); during
 * reset the pin floats and its pull-down hands the K3-A Pro straight back to the pilot.
 */
#include <stdio.h>
#include <string.h>

#include "bridge_core.h"
#include "hardware/gpio.h"
#include "hardware/uart.h"
#include "hardware/watchdog.h"
#include "pico/stdio_usb.h"
#include "pico/stdlib.h"

#define PIN_SBUS_TX 0
#define PIN_SBUS_RX 1
#define PIN_SELECT 2
#define PIN_COMP_TX 4
#define PIN_COMP_RX 5
#define PIN_LED 25
#define SBUS_UART uart0
#define COMP_UART uart1
#define FRAME_PERIOD_US 14000
#define STATUS_PERIOD_US 100000
#define SBUS_GAP_US 3000   /* S-BUS frames are separated by an idle gap: resync on it */

static bridge_t bridge;
static sbus_parser_t sbus_in;
static link_parser_t link_uart, link_usb;
static uint32_t link_errors;

static inline uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

static void send_status(uint32_t t) {
    uint8_t st[18], wire[32];
    size_t n = bridge_status(&bridge, t, st, sizeof st);
    uint32_t errs = link_uart.bad + link_usb.bad + link_errors;
    st[8] = (uint8_t)errs;
    st[9] = (uint8_t)(errs >> 8);
    memcpy(st + 10, &sbus_in.good, 4);
    memcpy(st + 14, &sbus_in.bad, 4);
    size_t w = link_encode(LINK_STATUS, st, (uint8_t)n, wire, sizeof wire);
    uart_write_blocking(COMP_UART, wire, w);
    if (stdio_usb_connected()) {
        fwrite(wire, 1, w, stdout);
        fflush(stdout);
    }
}

static void led(bridge_source_t src, uint32_t t) {
    bool on;
    switch (src) {
        case SRC_COMPANION: on = (t / 100) % 2; break;
        case SRC_HOLD: on = (t % 1000) < 100 || ((t % 1000) > 200 && (t % 1000) < 300); break;
        case SRC_RTH: on = true; break;
        case SRC_PILOT: on = (t / 500) % 2; break;
        default: on = false;
    }
    gpio_put(PIN_LED, on);
}

int main(void) {
    stdio_usb_init();
    gpio_init(PIN_SELECT);
    gpio_set_dir(PIN_SELECT, GPIO_OUT);
    gpio_pull_down(PIN_SELECT);
    gpio_put(PIN_SELECT, 0);                 /* pilot first, always */
    gpio_init(PIN_LED);
    gpio_set_dir(PIN_LED, GPIO_OUT);

    uart_init(SBUS_UART, 100000);
    uart_set_format(SBUS_UART, 8, 2, UART_PARITY_EVEN);
    uart_set_hw_flow(SBUS_UART, false, false);
    gpio_set_function(PIN_SBUS_TX, GPIO_FUNC_UART);
    gpio_set_function(PIN_SBUS_RX, GPIO_FUNC_UART);
    gpio_set_outover(PIN_SBUS_TX, GPIO_OVERRIDE_INVERT);
    gpio_set_inover(PIN_SBUS_RX, GPIO_OVERRIDE_INVERT);

    uart_init(COMP_UART, 115200);
    gpio_set_function(PIN_COMP_TX, GPIO_FUNC_UART);
    gpio_set_function(PIN_COMP_RX, GPIO_FUNC_UART);

    bridge_config_t cfg;
    bridge_config_default(&cfg);
    bridge_init(&bridge, &cfg);
    sbus_parser_init(&sbus_in);
    link_parser_init(&link_uart);
    link_parser_init(&link_usb);

    watchdog_enable(50, true);
    uint64_t next_frame = time_us_64(), next_status = next_frame, last_byte = 0;
    sbus_frame_t f, out;
    link_msg_t m;
    for (;;) {
        watchdog_update();
        uint64_t t_us = time_us_64();
        while (uart_is_readable(SBUS_UART)) {
            uint8_t c = (uint8_t)uart_getc(SBUS_UART);
            uint64_t t = time_us_64();
            if (t - last_byte > SBUS_GAP_US) sbus_in.n = 0;
            last_byte = t;
            if (sbus_parser_feed(&sbus_in, c, &f)) bridge_on_pilot(&bridge, &f, now_ms());
        }
        while (uart_is_readable(COMP_UART))
            if (link_parser_feed(&link_uart, (uint8_t)uart_getc(COMP_UART), &m) &&
                !bridge_on_link(&bridge, &m, now_ms()))
                link_errors++;
        int ch;
        while ((ch = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT)
            if (link_parser_feed(&link_usb, (uint8_t)ch, &m) && !bridge_on_link(&bridge, &m, now_ms()))
                link_errors++;
        if (t_us >= next_frame) {
            next_frame += FRAME_PERIOD_US;
            bool emit;
            uint32_t t = now_ms();
            bridge_source_t src = bridge_output(&bridge, t, &out, &emit);
            bool ours = emit && (src == SRC_COMPANION || src == SRC_HOLD || src == SRC_RTH);
            if (ours) {
                uint8_t wire[SBUS_FRAME_LEN];
                sbus_encode(&out, wire);
                uart_write_blocking(SBUS_UART, wire, SBUS_FRAME_LEN);
            }
            gpio_put(PIN_SELECT, ours);      /* pilot / none: the receiver drives the K3-A Pro */
            led(src, t);
        }
        if (t_us >= next_status) {
            next_status += STATUS_PERIOD_US;
            send_status(now_ms());
        }
    }
}
