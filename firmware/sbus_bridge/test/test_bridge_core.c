/* Host unit tests for the bridge core: gcc -std=c11 ... && ./test_bridge_core */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../src/bridge_core.h"

static int failures = 0, checks = 0;
#define CHECK(cond)                                                                    \
    do {                                                                               \
        checks++;                                                                      \
        if (!(cond)) {                                                                 \
            failures++;                                                                \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);   \
        }                                                                              \
    } while (0)

static void frame_all(sbus_frame_t *f, uint16_t us) {
    memset(f, 0, sizeof *f);
    for (int i = 0; i < SBUS_NCH; i++) f->us[i] = us;
}

static void test_sbus_roundtrip(void) {
    sbus_frame_t f, g;
    frame_all(&f, 1500);
    for (int i = 0; i < SBUS_NCH; i++) f.us[i] = (uint16_t)(1000 + 60 * i);
    f.ch17 = true;
    f.failsafe = true;
    uint8_t b[SBUS_FRAME_LEN];
    sbus_encode(&f, b);
    CHECK(b[0] == SBUS_HEADER && b[24] == 0x00);
    CHECK(sbus_decode(b, &g));
    for (int i = 0; i < SBUS_NCH; i++) CHECK(abs((int)g.us[i] - (int)f.us[i]) <= 1);
    CHECK(g.ch17 && !g.ch18 && g.failsafe && !g.frame_lost);
    b[0] = 0x0E;
    CHECK(!sbus_decode(b, &g));
}

static void test_sbus_scaling(void) {
    CHECK(sbus_us_to_raw(1500) == 992);
    CHECK(sbus_raw_to_us(992) == 1500);
    CHECK(sbus_us_to_raw(500) == 0);                 /* clamped */
    CHECK(sbus_us_to_raw(3000) == 2047);
}

static void test_sbus_parser_resyncs(void) {
    sbus_parser_t p;
    sbus_parser_init(&p);
    sbus_frame_t f, out;
    frame_all(&f, 1600);
    uint8_t b[SBUS_FRAME_LEN];
    sbus_encode(&f, b);
    int got = 0;
    uint8_t junk[] = {0x0F, 0x12, 0x00, 0x33};       /* a false header in the noise */
    for (size_t i = 0; i < sizeof junk; i++) got += sbus_parser_feed(&p, junk[i], &out);
    for (int k = 0; k < 3; k++)
        for (int i = 0; i < SBUS_FRAME_LEN; i++) got += sbus_parser_feed(&p, b[i], &out);
    CHECK(got == 3);
    CHECK(out.us[3] == 1600);
}

static void test_link_roundtrip_and_crc(void) {
    CHECK(crc16_ccitt((const uint8_t *)"123456789", 9) == 0x29B1);
    uint8_t pl[36] = {0};
    for (int i = 0; i < 36; i++) pl[i] = (uint8_t)i;
    uint8_t wire[80];
    size_t n = link_encode(LINK_CHANNELS, pl, 36, wire, sizeof wire);
    CHECK(n == 36 + 5);
    link_parser_t p;
    link_parser_init(&p);
    link_msg_t m;
    int got = 0;
    for (size_t i = 0; i < n; i++) got += link_parser_feed(&p, wire[i], &m);
    CHECK(got == 1 && m.type == LINK_CHANNELS && m.len == 36 && m.payload[35] == 35);
    wire[10] ^= 0xFF;                                /* corrupt: rejected, counted */
    got = 0;
    for (size_t i = 0; i < n; i++) got += link_parser_feed(&p, wire[i], &m);
    CHECK(got == 0 && p.bad == 1);
}

static bridge_t B;

static void setup(void) {
    bridge_config_t c;
    bridge_config_default(&c);
    bridge_init(&B, &c);
}

static void pilot(uint32_t t, uint16_t auto_us, bool failsafe) {
    sbus_frame_t f;
    frame_all(&f, 1500);
    f.us[B.cfg.auto_ch] = auto_us;
    f.us[0] = 1234;                                  /* recognisable pilot roll */
    f.failsafe = failsafe;
    bridge_on_pilot(&B, &f, t);
}

static void companion(uint32_t t, uint16_t ttl) {
    uint16_t us[SBUS_NCH];
    for (int i = 0; i < SBUS_NCH; i++) us[i] = 1500;
    us[0] = 1700;                                    /* recognisable companion roll */
    us[B.cfg.pump_ch] = 1900;                        /* pump on */
    bridge_on_companion(&B, us, ttl, 7, t);
}

static void test_no_pilot_ever_means_no_output(void) {
    setup();
    companion(0, 200);
    sbus_frame_t o;
    bool emit = true;
    CHECK(bridge_output(&B, 10, &o, &emit) == SRC_NONE && !emit);
}

static void test_manual_switch_passes_pilot_through(void) {
    setup();
    pilot(0, 1000, false);
    companion(0, 200);
    sbus_frame_t o;
    bool emit;
    CHECK(bridge_output(&B, 5, &o, &emit) == SRC_PILOT && emit && o.us[0] == 1234);
}

static void test_auto_switch_with_fresh_companion_flies_companion(void) {
    setup();
    pilot(0, 2000, false);
    companion(0, 200);
    sbus_frame_t o;
    bool emit;
    CHECK(bridge_output(&B, 50, &o, &emit) == SRC_COMPANION && emit && o.us[0] == 1700 && !o.failsafe);
}

static void test_stale_companion_holds_then_returns_home(void) {
    setup();
    pilot(0, 2000, false);
    companion(0, 200);
    sbus_frame_t o;
    bool emit;
    uint32_t t = 300;                                /* ttl 200 ms passed */
    pilot(t, 2000, false);
    CHECK(bridge_output(&B, t, &o, &emit) == SRC_HOLD);
    CHECK(o.us[0] == 1500 && o.us[1] == 1500 && o.us[3] == 1500 && o.us[B.cfg.throttle_ch] == 1500);
    CHECK(o.us[B.cfg.mode_ch] == B.cfg.mode_gps_us && o.us[B.cfg.rth_ch] == B.cfg.rth_off_us);
    CHECK(o.us[B.cfg.pump_ch] == B.cfg.pump_off_us);   /* never spray while holding */
    t += B.cfg.hold_ms + 10;
    pilot(t, 2000, false);
    CHECK(bridge_output(&B, t, &o, &emit) == SRC_RTH && o.us[B.cfg.rth_ch] == B.cfg.rth_on_us);
    companion(t + 5, 200);                           /* the agent recovers: it decides again */
    CHECK(bridge_output(&B, t + 6, &o, &emit) == SRC_COMPANION);
}

static void test_ttl_is_capped(void) {
    setup();
    pilot(0, 2000, false);
    companion(0, 60000);                             /* a buggy agent asks for a minute */
    sbus_frame_t o;
    bool emit;
    pilot(400, 2000, false);
    CHECK(bridge_output(&B, 400, &o, &emit) == SRC_HOLD);
}

static void test_switch_to_manual_takes_over_at_once(void) {
    setup();
    pilot(0, 2000, false);
    companion(0, 200);
    sbus_frame_t o;
    bool emit;
    CHECK(bridge_output(&B, 10, &o, &emit) == SRC_COMPANION);
    pilot(20, 1000, false);
    companion(20, 200);
    CHECK(bridge_output(&B, 21, &o, &emit) == SRC_PILOT && o.us[0] == 1234);
}

static void test_pilot_rx_lost_at_auto_keeps_the_job_going(void) {
    setup();
    pilot(0, 2000, false);
    companion(1000, 200);                            /* pilot frames now 1 s old: RX lost */
    sbus_frame_t o;
    bool emit;
    CHECK(bridge_output(&B, 1001, &o, &emit) == SRC_COMPANION);
}

static void test_pilot_failsafe_flag_does_not_allow_autonomy(void) {
    setup();
    pilot(0, 2000, true);                            /* receiver in failsafe: switch value is fake */
    companion(0, 200);
    sbus_frame_t o;
    bool emit;
    CHECK(bridge_output(&B, 5, &o, &emit) == SRC_PILOT && o.failsafe);
}

static void test_pilot_lost_at_manual_stops_output(void) {
    setup();
    pilot(0, 1000, false);
    sbus_frame_t o;
    bool emit = true;
    CHECK(bridge_output(&B, 500, &o, &emit) == SRC_NONE && !emit);
}

static void test_config_pack_roundtrip_and_link_dispatch(void) {
    bridge_config_t c, d;
    bridge_config_default(&c);
    c.hold_ms = 1234;
    c.auto_ch = 9;
    uint8_t buf[64];
    size_t n = bridge_config_pack(&c, buf);
    CHECK(bridge_config_unpack(buf, n, &d) && d.hold_ms == 1234 && d.auto_ch == 9);
    CHECK(!bridge_config_unpack(buf, n - 1, &d));
    d.auto_ch = 16;                                  /* out of range: refused */
    n = bridge_config_pack(&d, buf);
    CHECK(!bridge_config_unpack(buf, n, &c));

    setup();
    pilot(0, 2000, false);
    uint8_t pl[36];
    uint16_t seq = 42, ttl = 200;
    memcpy(pl, &seq, 2);
    memcpy(pl + 2, &ttl, 2);
    for (int i = 0; i < SBUS_NCH; i++) {
        uint16_t v = (uint16_t)(i == 0 ? 1650 : 1500);
        memcpy(pl + 4 + 2 * i, &v, 2);
    }
    link_msg_t m = {LINK_CHANNELS, 36, {0}};
    memcpy(m.payload, pl, 36);
    CHECK(bridge_on_link(&B, &m, 1));
    sbus_frame_t o;
    bool emit;
    CHECK(bridge_output(&B, 2, &o, &emit) == SRC_COMPANION && o.us[0] == 1650 && B.comp_seq == 42);
    uint8_t st[32];
    CHECK(bridge_status(&B, 3, st, sizeof st) == 18 && st[0] == SRC_COMPANION);
}

int main(void) {
    test_sbus_roundtrip();
    test_sbus_scaling();
    test_sbus_parser_resyncs();
    test_link_roundtrip_and_crc();
    test_no_pilot_ever_means_no_output();
    test_manual_switch_passes_pilot_through();
    test_auto_switch_with_fresh_companion_flies_companion();
    test_stale_companion_holds_then_returns_home();
    test_ttl_is_capped();
    test_switch_to_manual_takes_over_at_once();
    test_pilot_rx_lost_at_auto_keeps_the_job_going();
    test_pilot_failsafe_flag_does_not_allow_autonomy();
    test_pilot_lost_at_manual_stops_output();
    test_config_pack_roundtrip_and_link_dispatch();
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
