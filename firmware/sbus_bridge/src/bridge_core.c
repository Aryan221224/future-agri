/* S-BUS bridge core (see bridge_core.h). Portable C11, no allocation, no hardware. */
#include "bridge_core.h"

#include <string.h>

/* ---------------------------------------------------------------- little endian helpers */
static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, (uint16_t)v); put16(p + 2, (uint16_t)(v >> 16)); }
static uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

/* ---------------------------------------------------------------- S-BUS codec */
uint16_t sbus_us_to_raw(uint16_t us) {
    /* standard mapping: us = raw * 0.625 + 880  ->  raw = (us - 880) * 1.6 */
    if (us <= 880) return 0;
    uint32_t raw = ((uint32_t)(us - 880) * 16 + 5) / 10;
    return raw > 2047 ? 2047 : (uint16_t)raw;
}

uint16_t sbus_raw_to_us(uint16_t raw) {
    return (uint16_t)(((uint32_t)(raw & 0x7FF) * 5 + 4) / 8 + 880);
}

void sbus_encode(const sbus_frame_t *f, uint8_t out[SBUS_FRAME_LEN]) {
    memset(out, 0, SBUS_FRAME_LEN);
    out[0] = SBUS_HEADER;
    uint32_t acc = 0;
    int bits = 0, k = 1;
    for (int i = 0; i < SBUS_NCH; i++) {
        acc |= (uint32_t)sbus_us_to_raw(f->us[i]) << bits;
        bits += 11;
        while (bits >= 8) {
            out[k++] = (uint8_t)acc;
            acc >>= 8;
            bits -= 8;
        }
    }
    out[23] = (uint8_t)((f->ch17 ? 0x01 : 0) | (f->ch18 ? 0x02 : 0) | (f->frame_lost ? 0x04 : 0) |
                        (f->failsafe ? 0x08 : 0));
    out[24] = 0x00;
}

static bool footer_ok(uint8_t b) { return b == 0x00 || (b & 0x0F) == 0x04; /* S-BUS2 footers */ }

bool sbus_decode(const uint8_t in[SBUS_FRAME_LEN], sbus_frame_t *f) {
    if (in[0] != SBUS_HEADER || !footer_ok(in[24])) return false;
    uint32_t acc = 0;
    int bits = 0, k = 1;
    for (int i = 0; i < SBUS_NCH; i++) {
        while (bits < 11) {
            acc |= (uint32_t)in[k++] << bits;
            bits += 8;
        }
        f->us[i] = sbus_raw_to_us((uint16_t)(acc & 0x7FF));
        acc >>= 11;
        bits -= 11;
    }
    f->ch17 = in[23] & 0x01;
    f->ch18 = in[23] & 0x02;
    f->frame_lost = in[23] & 0x04;
    f->failsafe = in[23] & 0x08;
    return true;
}

void sbus_parser_init(sbus_parser_t *p) { memset(p, 0, sizeof *p); }

bool sbus_parser_feed(sbus_parser_t *p, uint8_t byte, sbus_frame_t *out) {
    if (p->n == 0 && byte != SBUS_HEADER) return false;
    p->buf[p->n++] = byte;
    if (p->n < SBUS_FRAME_LEN) return false;
    if (sbus_decode(p->buf, out)) {
        p->n = 0;
        p->good++;
        return true;
    }
    /* not a frame: resynchronise on the next header byte inside the buffer */
    p->bad++;
    uint8_t i = 1;
    while (i < SBUS_FRAME_LEN && p->buf[i] != SBUS_HEADER) i++;
    p->n = (uint8_t)(SBUS_FRAME_LEN - i);
    memmove(p->buf, p->buf + i, p->n);
    return false;
}

/* ---------------------------------------------------------------- companion frames */
uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; b++) crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}

size_t link_encode(uint8_t type, const uint8_t *payload, uint8_t len, uint8_t *out, size_t cap) {
    if (len > LINK_MAX_PAYLOAD || cap < (size_t)len + 5) return 0;
    out[0] = LINK_SYNC;
    out[1] = type;
    out[2] = len;
    if (len) memcpy(out + 3, payload, len);
    put16(out + 3 + len, crc16_ccitt(out + 1, (size_t)len + 2));
    return (size_t)len + 5;
}

void link_parser_init(link_parser_t *p) { memset(p, 0, sizeof *p); }

bool link_parser_feed(link_parser_t *p, uint8_t byte, link_msg_t *out) {
    if (p->n == 0) {
        if (byte == LINK_SYNC) p->buf[p->n++] = byte;
        return false;
    }
    p->buf[p->n++] = byte;
    if (p->n == 3) {
        if (p->buf[2] > LINK_MAX_PAYLOAD) {
            p->bad++;
            p->n = 0;
            return false;
        }
        p->want = (uint8_t)(p->buf[2] + 5);
    }
    if (p->n < 3 || p->n < p->want) return false;
    uint8_t len = p->buf[2];
    bool ok = crc16_ccitt(p->buf + 1, (size_t)len + 2) == get16(p->buf + 3 + len);
    p->n = 0;
    if (!ok) {
        p->bad++;
        return false;
    }
    p->good++;
    out->type = p->buf[1];
    out->len = len;
    memcpy(out->payload, p->buf + 3, len);
    return true;
}

/* ---------------------------------------------------------------- bridge */
void bridge_config_default(bridge_config_t *c) {
    c->auto_ch = 7;            /* CH8: a 2-position switch on the pilot's transmitter     */
    c->auto_on_us = 1700;
    c->pilot_timeout_ms = 200; /* S-BUS frames come every 7-14 ms                          */
    c->max_ttl_ms = 250;
    c->hold_ms = 2000;
    c->mode_ch = 4;            /* K3-A Pro CH5                                             */
    c->mode_gps_us = 1500;     /* verified on the bench (Assistant2 shows the mode)        */
    c->rth_ch = 5;             /* K3-A Pro CH6: one-key return home                        */
    c->rth_on_us = 2000;
    c->rth_off_us = 1000;
    c->throttle_ch = 2;        /* CH3                                                      */
    c->pump_ch = 6;            /* CH7: pump channel in Assistant2 manual pump mode         */
    c->pump_off_us = 1000;
}

size_t bridge_config_pack(const bridge_config_t *c, uint8_t *o) {
    o[0] = c->auto_ch;
    put16(o + 1, c->auto_on_us);
    put16(o + 3, c->pilot_timeout_ms);
    put16(o + 5, c->max_ttl_ms);
    put16(o + 7, c->hold_ms);
    o[9] = c->mode_ch;
    put16(o + 10, c->mode_gps_us);
    o[12] = c->rth_ch;
    put16(o + 13, c->rth_on_us);
    put16(o + 15, c->rth_off_us);
    o[17] = c->throttle_ch;
    o[18] = c->pump_ch;
    put16(o + 19, c->pump_off_us);
    return 21;
}

static bool us_ok(uint16_t us) { return us >= 800 && us <= 2200; }

bool bridge_config_unpack(const uint8_t *i, size_t len, bridge_config_t *c) {
    if (len != 21) return false;
    bridge_config_t t = {
        .auto_ch = i[0], .auto_on_us = get16(i + 1), .pilot_timeout_ms = get16(i + 3),
        .max_ttl_ms = get16(i + 5), .hold_ms = get16(i + 7), .mode_ch = i[9], .mode_gps_us = get16(i + 10),
        .rth_ch = i[12], .rth_on_us = get16(i + 13), .rth_off_us = get16(i + 15), .throttle_ch = i[17],
        .pump_ch = i[18], .pump_off_us = get16(i + 19)};
    if (t.auto_ch >= SBUS_NCH || t.mode_ch >= SBUS_NCH || t.rth_ch >= SBUS_NCH || t.throttle_ch >= SBUS_NCH ||
        (t.pump_ch >= SBUS_NCH && t.pump_ch != 0xFF))
        return false;
    if (!us_ok(t.auto_on_us) || !us_ok(t.mode_gps_us) || !us_ok(t.rth_on_us) || !us_ok(t.rth_off_us) ||
        !us_ok(t.pump_off_us))
        return false;
    if (t.pilot_timeout_ms < 20 || t.pilot_timeout_ms > 2000 || t.max_ttl_ms < 20 || t.max_ttl_ms > 1000 ||
        t.hold_ms > 10000)
        return false;
    *c = t;
    return true;
}

void bridge_init(bridge_t *b, const bridge_config_t *c) {
    memset(b, 0, sizeof *b);
    b->cfg = *c;
    for (int i = 0; i < SBUS_NCH; i++) b->comp_us[i] = 1500;
}

void bridge_on_pilot(bridge_t *b, const sbus_frame_t *f, uint32_t now_ms) {
    b->pilot = *f;
    b->pilot_seen = true;
    b->pilot_ms = now_ms;
    if (!f->failsafe) b->auto_latched = f->us[b->cfg.auto_ch] >= b->cfg.auto_on_us;
}

void bridge_on_companion(bridge_t *b, const uint16_t us[SBUS_NCH], uint16_t ttl_ms, uint16_t seq,
                         uint32_t now_ms) {
    memcpy(b->comp_us, us, sizeof b->comp_us);
    b->comp_seen = true;
    b->comp_ms = now_ms;
    b->comp_ttl = ttl_ms > b->cfg.max_ttl_ms ? b->cfg.max_ttl_ms : ttl_ms;
    b->comp_seq = seq;
}

bool bridge_on_link(bridge_t *b, const link_msg_t *m, uint32_t now_ms) {
    if (m->type == LINK_CHANNELS && m->len == 36) {
        uint16_t us[SBUS_NCH];
        for (int i = 0; i < SBUS_NCH; i++) us[i] = get16(m->payload + 4 + 2 * i);
        bridge_on_companion(b, us, get16(m->payload + 2), get16(m->payload), now_ms);
        return true;
    }
    if (m->type == LINK_CONFIG) {
        /* never re-configure while the companion (or a hold / return home) is flying */
        if (b->last == SRC_COMPANION || b->last == SRC_HOLD || b->last == SRC_RTH) return false;
        bridge_config_t c;
        if (!bridge_config_unpack(m->payload, m->len, &c)) return false;
        b->cfg = c;
        return true;
    }
    return m->type == LINK_PING;
}

static bool pilot_fresh(const bridge_t *b, uint32_t now) {
    return b->pilot_seen && (uint32_t)(now - b->pilot_ms) <= b->cfg.pilot_timeout_ms;
}

static bool comp_fresh(const bridge_t *b, uint32_t now) {
    return b->comp_seen && (uint32_t)(now - b->comp_ms) <= b->comp_ttl;
}

bridge_source_t bridge_output(bridge_t *b, uint32_t now, sbus_frame_t *out, bool *emit) {
    bridge_source_t src;
    *emit = true;
    if (!b->auto_latched || !b->comp_seen) {
        if (pilot_fresh(b, now)) {
            *out = b->pilot;                     /* exact pass-through, flags included */
            src = SRC_PILOT;
        } else {
            *emit = false;                       /* silence: the K3-A Pro's own RC-loss RTH */
            src = SRC_NONE;
        }
    } else if (comp_fresh(b, now)) {
        memset(out, 0, sizeof *out);
        memcpy(out->us, b->comp_us, sizeof out->us);
        src = SRC_COMPANION;
    } else {
        uint32_t expired = b->comp_ms + b->comp_ttl;
        memset(out, 0, sizeof *out);
        memcpy(out->us, b->comp_us, sizeof out->us);
        out->us[0] = out->us[1] = out->us[3] = 1500;   /* roll, pitch, yaw: centred = hover */
        out->us[b->cfg.throttle_ch] = 1500;            /* centred = hold altitude */
        out->us[b->cfg.mode_ch] = b->cfg.mode_gps_us;
        if (b->cfg.pump_ch < SBUS_NCH) out->us[b->cfg.pump_ch] = b->cfg.pump_off_us;
        bool rth = (uint32_t)(now - expired) > b->cfg.hold_ms;
        out->us[b->cfg.rth_ch] = rth ? b->cfg.rth_on_us : b->cfg.rth_off_us;
        src = rth ? SRC_RTH : SRC_HOLD;
    }
    if (*emit) b->frames_out++;
    b->last = src;
    return src;
}

size_t bridge_status(const bridge_t *b, uint32_t now, uint8_t *o, size_t cap) {
    if (cap < 18) return 0;
    memset(o, 0, 18);
    o[0] = (uint8_t)b->last;
    o[1] = (uint8_t)((pilot_fresh(b, now) ? 0x01 : 0) | (b->pilot.failsafe ? 0x02 : 0) |
                     (b->auto_latched ? 0x04 : 0) | (comp_fresh(b, now) ? 0x08 : 0));
    put16(o + 2, b->comp_seq);
    put32(o + 4, b->frames_out);
    /* bytes 8..17 (link errors, pilot parser counters) are filled in by the firmware */
    return 18;
}
