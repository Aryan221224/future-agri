"""The agent's console link: store-and-forward telemetry, commands, config checking."""
import pytest

from fa_agent.main import AgentConfig
from fa_agent.route import Local
from fa_agent.sim import Fix
from fa_agent.uplink import ConsoleLink, live_batch


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Net:
    def __init__(self):
        self.up, self.posts = False, []

    def post(self, url, json=None, headers=None, timeout=None):
        if not self.up:
            raise ConnectionError("no route to host")
        self.posts.append((url, json, headers))
        return _Resp(200)

    def get(self, url, params=None, headers=None, timeout=None):
        if not self.up:
            raise ConnectionError("no route to host")
        return _Resp(200, {"commands": [{"id": "c1", "kind": "abort"}]})


def test_batches_wait_for_the_link_and_survive_a_restart(tmp_path):
    net = _Net()
    spool = tmp_path / "spool.jsonl"
    link = ConsoleLink("http://console:8099/", "tok", spool, session=net)
    fix = Fix(1.0, 10.0, 20.0, 2.5, 1.0, 0.0, 0.0, 90.0, True, "fixed")
    for i in range(3):
        link.enqueue(live_batch("run_1", i + 1, fix, Local(28.47, 77.5), 0, True))
    assert link.flush() == 0 and not link.online and len(link.queue) == 3
    again = ConsoleLink("http://console:8099", "tok", spool, session=net)     # agent restarted
    assert len(again.queue) == 3
    net.up = True
    assert again.flush() == 3 and not again.queue and again.online
    url, body, h = net.posts[0]
    assert url.endswith("/api/live-run/ingest") and h["X-Device-Token"] == "tok"
    assert body["schema"] == "fa.live/1" and body["telemetry"][0]["spraying"] is True
    assert body["telemetry"][0]["lat"] == pytest.approx(28.47 + 20.0 / 111_319.49, abs=1e-6)
    assert again.poll(1) == [{"id": "c1", "kind": "abort"}]


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text("token: x\nbrige_port: /dev/serial0\n")
    with pytest.raises(ValueError, match="brige_port"):
        AgentConfig.load(str(p))
    p.write_text("token: x\nbridge_port: /dev/ttyAMA0\n")
    assert AgentConfig.load(str(p)).bridge_port == "/dev/ttyAMA0"
