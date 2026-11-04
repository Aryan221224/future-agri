"""RTK receiver parsing (u-blox UBX) and the NTRIP corrections client."""
import socket
import struct
import threading

import pytest

from fa_agent.gnss import NAV_PVT, NAV_RELPOSNED, FixBuilder, NtripClient, UbxParser, gga, ubx_frame
from fa_agent.route import Local


def _pvt(lat, lon, h_m, carr=2, fix=3, vn=0.5, ve=-0.25, vd=0.1, sats=28):
    p = bytearray(92)
    p[20], p[21], p[23] = fix, 0x01 | (carr << 6), sats
    struct.pack_into("<iiiiIIiii", p, 24, round(lon * 1e7), round(lat * 1e7), round(h_m * 1000), 0, 14, 20,
                     round(vn * 1000), round(ve * 1000), round(vd * 1000))
    return ubx_frame(*NAV_PVT, bytes(p))


def _relpos(heading_deg, length_m=0.8, carr=2, valid=True):
    p = bytearray(64)
    struct.pack_into("<ii", p, 20, round(length_m * 100), round(heading_deg * 1e5))
    flags = (0x01 | 0x04 | 0x100 if valid else 0x01) | (carr << 3)
    struct.pack_into("<I", p, 60, flags)
    return ubx_frame(*NAV_RELPOSNED, bytes(p))


def test_ubx_frames_parse_through_noise_and_bad_checksums():
    good = _pvt(28.47, 77.5, 200.0)
    bad = bytearray(_relpos(10.0))
    bad[-1] ^= 0xFF
    p = UbxParser()
    msgs = p.feed(b"\x00\xb5junk" + bytes(bad) + good[:20]) + p.feed(good[20:])
    assert [(c, m) for c, m, _ in msgs] == [NAV_PVT] and p.bad == 1


def test_fix_in_local_metres_with_rtk_heading():
    fb = FixBuilder(28.47, 77.50, heading_offset_deg=-90.0, baseline_m=0.8)
    loc = Local(28.47, 77.50)
    lon, lat = loc.to_lonlat(12.0, -5.0)
    for c, m, pl in UbxParser().feed(_pvt(28.47, 77.50, 200.0) + _pvt(lat, lon, 203.5) + _relpos(100.0)):
        fb.on_ubx(c, m, pl, t=1.0)
    f = fb.fix(1.1)
    assert f.kind == "fixed" and f.heading_ok
    assert f.e == pytest.approx(12.0, abs=0.02) and f.n == pytest.approx(-5.0, abs=0.02)
    assert f.h == pytest.approx(3.5, abs=0.002) and f.heading == pytest.approx(10.0)   # offset applied
    assert f.v_e == pytest.approx(-0.25) and f.v_u == pytest.approx(-0.1)


def test_heading_is_refused_when_the_baseline_length_is_wrong_or_float():
    fb = FixBuilder(28.47, 77.50, baseline_m=0.8)
    for c, m, pl in UbxParser().feed(_pvt(28.47, 77.50, 200.0) + _relpos(45.0, length_m=0.95)):
        fb.on_ubx(c, m, pl, t=1.0)
    assert not fb.fix(1.0).heading_ok                   # wrong length = wrong integer fix
    for c, m, pl in UbxParser().feed(_relpos(45.0, carr=1)):
        fb.on_ubx(c, m, pl, t=1.0)
    assert not fb.fix(1.0).heading_ok


def test_stale_or_no_fix_is_none():
    fb = FixBuilder(28.47, 77.50)
    assert fb.fix(0.0) is None
    for c, m, pl in UbxParser().feed(_pvt(28.47, 77.50, 200.0, carr=1)):
        fb.on_ubx(c, m, pl, t=1.0)
    assert fb.fix(1.0).kind == "float" and fb.fix(2.0) is None


def test_gga_checksum():
    s = gga(28.47, 77.5, 210.0, utc=0).decode()
    body, cs = s[1:].strip().split("*")
    x = 0
    for ch in body:
        x ^= ord(ch)
    assert f"{x:02X}" == cs and ",N," in s and ",E," in s


def _caster(reply: bytes, payload: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = {}

    def serve():
        c, _ = srv.accept()
        seen["req"] = c.recv(1024)
        c.sendall(reply + payload)
        c.close()
        srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], seen


def test_ntrip_streams_rtcm_to_the_receiver():
    port, seen = _caster(b"ICY 200 OK\r\n\r\n", b"\xd3\x00\x13rtcm-bytes")
    got = []
    cl = NtripClient("127.0.0.1", port, "MOUNT", "user", "pw", sink=got.append, timeout_s=2)
    with pytest.raises(ConnectionError):
        cl.run_once()
    assert b"".join(got) == b"\xd3\x00\x13rtcm-bytes"
    assert b"GET /MOUNT" in seen["req"] and b"Authorization: Basic dXNlcjpwdw==" in seen["req"]


def test_ntrip_refusal_is_reported():
    port, _ = _caster(b"HTTP/1.0 401 Unauthorized\r\n\r\n", b"")
    with pytest.raises(PermissionError, match="401"):
        NtripClient("127.0.0.1", port, "M", timeout_s=2).run_once()
