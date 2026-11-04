/* S-BUS bridge core: portable logic shared by the RP2040 firmware, the host unit tests and
 * the Python simulator (built as a shared library). No hardware access here.
 *
 *   pilot receiver --S-BUS--> [bridge] --S-BUS--> JIYI K3-A Pro (GPS-SPEED mode)
 *                                ^ companion frames (fa-agent on the Raspberry Pi)
 *
 * Source selection (bridge_output):
 *   1. autonomy not allowed (pilot switch not at AUTO, or never seen) -> pilot pass-through;
 *      no pilot frames at all -> no output (the K3-A Pro sees signal loss: its own return home)
 *   2. autonomy allowed and a fresh companion frame -> companion channels
 *   3. autonomy allowed, companion stale -> HOLD (sticks centred: the K3-A Pro hovers) for
 *      hold_ms, then RTH (sticks centred, return-home channel on)
 * The pilot switch is latched: if the pilot receiver is lost while at AUTO, autonomy stays
 * allowed (the job continues beyond RC range); moving the switch away takes over at once.
 */
#ifndef BRIDGE_CORE_H
#define BRIDGE_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SBUS_NCH 16
#define SBUS_FRAME_LEN 25
#define SBUS_HEADER 0x0F

/* ---------------------------------------------------------------- S-BUS codec */
typedef struct {
    uint16_t us[SBUS_NCH];   /* pulse width, microseconds (about 880..2160) */
    bool ch17, ch18;
    bool frame_lost;         /* receiver flag: a frame was lost */
    bool failsafe;           /* receiver flag: receiver in failsafe */
} sbus_frame_t;

uint16_t sbus_us_to_raw(uint16_t us);
uint16_t sbus_raw_to_us(uint16_t raw);
void sbus_encode(const sbus_frame_t *f, uint8_t out[SBUS_FRAME_LEN]);
bool sbus_decode(const uint8_t in[SBUS_FRAME_LEN], sbus_frame_t *f);

/* Byte-stream parser: feed bytes as they arrive; returns true when a frame is complete. */
typedef struct {
    uint8_t buf[SBUS_FRAME_LEN];
    uint8_t n;
    uint32_t good, bad;
} sbus_parser_t;

void sbus_parser_init(sbus_parser_t *p);
bool sbus_parser_feed(sbus_parser_t *p, uint8_t byte, sbus_frame_t *out);

/* ---------------------------------------------------------------- companion frames
 * 0xA5 | type | len | payload[len] | crc16 (CCITT-FALSE over type,len,payload; little endian)
 */
#define LINK_SYNC 0xA5
#define LINK_MAX_PAYLOAD 64
enum {
    LINK_CHANNELS = 0x01,   /* seq u16, ttl_ms u16, 16 x u16 us                      (36 B) */
    LINK_CONFIG = 0x02,     /* bridge_config_t fields, see bridge_config_pack          */
    LINK_PING = 0x03,       /* seq u16                                                 */
    LINK_STATUS = 0x81,     /* source u8, flags u8, last_seq u16, frames_out u32, link_errors u16,
                               pilot_good u32, pilot_bad u32                         (18 B) */
};

uint16_t crc16_ccitt(const uint8_t *data, size_t len);
size_t link_encode(uint8_t type, const uint8_t *payload, uint8_t len, uint8_t *out, size_t cap);

typedef struct {
    uint8_t buf[LINK_MAX_PAYLOAD + 5];
    uint8_t n, want;
    uint32_t good, bad;
} link_parser_t;

typedef struct {
    uint8_t type, len;
    uint8_t payload[LINK_MAX_PAYLOAD];
} link_msg_t;

void link_parser_init(link_parser_t *p);
bool link_parser_feed(link_parser_t *p, uint8_t byte, link_msg_t *out);

/* ---------------------------------------------------------------- bridge */
typedef struct {
    uint8_t auto_ch;          /* pilot switch channel (0-based) that allows autonomy      */
    uint16_t auto_on_us;      /* switch value at or above this = AUTO                     */
    uint16_t pilot_timeout_ms;/* pilot frames older than this = receiver lost             */
    uint16_t max_ttl_ms;      /* cap on the companion's frame time-to-live               */
    uint16_t hold_ms;         /* hover this long on a stale companion before RTH          */
    uint8_t mode_ch;          /* flight-mode channel (K3-A Pro: CH5 -> 4)                 */
    uint16_t mode_gps_us;     /* value selecting GPS-SPEED                                */
    uint8_t rth_ch;           /* one-key return home (K3-A Pro: CH6 -> 5)                 */
    uint16_t rth_on_us, rth_off_us;
    uint8_t throttle_ch;      /* CH3 -> 2; centre = hold altitude in GPS modes            */
    uint8_t pump_ch;          /* pump channel, forced off in HOLD/RTH (0xFF = none)        */
    uint16_t pump_off_us;
} bridge_config_t;

typedef enum { SRC_NONE = 0, SRC_PILOT = 1, SRC_COMPANION = 2, SRC_HOLD = 3, SRC_RTH = 4 } bridge_source_t;

typedef struct {
    bridge_config_t cfg;
    sbus_frame_t pilot;
    bool pilot_seen;
    uint32_t pilot_ms;
    bool auto_latched;
    uint16_t comp_us[SBUS_NCH];
    bool comp_seen;
    uint32_t comp_ms;
    uint16_t comp_ttl, comp_seq;
    bool stale;
    uint32_t stale_since;
    bridge_source_t last;
    uint32_t frames_out;
} bridge_t;

void bridge_config_default(bridge_config_t *c);
size_t bridge_config_pack(const bridge_config_t *c, uint8_t *out);
bool bridge_config_unpack(const uint8_t *in, size_t len, bridge_config_t *c);
void bridge_init(bridge_t *b, const bridge_config_t *c);
void bridge_on_pilot(bridge_t *b, const sbus_frame_t *f, uint32_t now_ms);
void bridge_on_companion(bridge_t *b, const uint16_t us[SBUS_NCH], uint16_t ttl_ms, uint16_t seq,
                         uint32_t now_ms);
bool bridge_on_link(bridge_t *b, const link_msg_t *m, uint32_t now_ms);
/* Returns the source used; *emit=false means send nothing this tick (signal loss on purpose). */
bridge_source_t bridge_output(bridge_t *b, uint32_t now_ms, sbus_frame_t *out, bool *emit);
size_t bridge_status(const bridge_t *b, uint32_t now_ms, uint8_t *out, size_t cap);

#ifdef __cplusplus
}
#endif
#endif
