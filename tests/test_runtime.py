"""Server lifecycle, durable bridge queues, isolated demos, and shared input validation."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import meshcore
import meshweb
from test_meshweb import FakeRadio, Rig, recv_type


@pytest.mark.parametrize("text", ["é" * 200, "🛰️ " * 100, "weather 晴朗 " * 80, "a" * 400])
def test_bridge_chunks_preserve_unicode_and_obey_bytes(text):
    chunks = meshcore.split_for_radio(text)
    assert all(0 < len(p.encode()) <= 180 for p in chunks)
    assert "".join("".join(chunks).split()) == "".join(text.split())


@pytest.mark.parametrize("value", ["30", "30,40,50", "nan,10", "20,inf", "91,0", "0,-181", "abc,10"])
def test_location_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        meshcore.parse_home(value)


@pytest.mark.parametrize("lat,lon", [(None, 0), (True, 0), (float("nan"), 0), (0, float("inf")), (91, 0)])
def test_invalid_radio_positions_do_not_reach_maps(lat, lon):
    assert meshcore.position({"position": {"latitude": lat, "longitude": lon}}) is None


async def test_outbox_preserves_only_unsent_chunks_after_failure(tmp_path):
    async with Rig(tmp_path, allow=["!02ecdcdc"]) as rig:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        queued = outbox / "100"
        queued.write_text("a" * 180 + " " + "é" * 90 + " final")
        original = rig.hub.backend.send_text
        calls = 0
        def fail_second(text, dest, on_ack=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("link lost")
            original(text, dest, on_ack)
        rig.hub.backend.send_text = fail_second
        await rig.hub._drain_outbox_once()
        assert queued.read_text() == "é" * 90 + "\nfinal"
        assert rig.hub.backend.sent == [("a" * 180, "!02ecdcdc")]
        rig.hub.backend.send_text = original
        await rig.hub._drain_outbox_once()
        assert not queued.exists()
        assert rig.hub.backend.sent == [("a" * 180, "!02ecdcdc"), ("é" * 90, "!02ecdcdc"), ("final", "!02ecdcdc")]


async def test_outbox_waits_for_connection_and_destination(tmp_path):
    async with Rig(tmp_path, allow=[]) as rig:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        queued = outbox / "100"
        queued.write_text("retained")
        await rig.hub._drain_outbox_once()
        assert queued.exists() and not rig.hub.backend.sent
        rig.hub.allow = ["!02ecdcdc"]
        backend, rig.hub.backend = rig.hub.backend, None
        await rig.hub._drain_outbox_once()
        assert queued.exists()
        rig.hub.backend = backend
        await rig.hub._drain_outbox_once()
        assert backend.sent == [("retained", "!02ecdcdc")]


async def test_receipts_and_history_recover_across_restart(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _ = await rig.ws()
        rec = await rig.hub.send("delivered", "!02ecdcdc")
        await recv_type(ws, "ack")
        def fail(*args):
            raise OSError("unplugged")
        rig.hub.backend.send_text = fail
        with pytest.raises(meshweb.HubError):
            await rig.hub.send("failed", "!02ecdcdc")
        await ws.close()
    with (tmp_path / "messages.jsonl").open("a") as f:
        f.write('{broken\n[]\n{"type":"delivery","id":"x"}\n')
        f.write(json.dumps({"ts": 1, "from": "!00000001", "to": "^all", "text": "after corruption", "dir": "in"}) + "\n")
    async with Rig(tmp_path) as rig:
        msgs = rig.hub.state()["messages"]
        assert msgs[0]["id"] == rec["id"] and msgs[0]["delivery"] == "delivered"
        assert msgs[1]["delivery"] == "failed"
        assert msgs[2]["text"] == "after corruption"


async def test_demo_starts_without_snapshot_and_never_sends_real_ack(tmp_path):
    args = meshweb.argument_parser().parse_args(["--demo", "--bridge", "--state-dir", str(tmp_path)])
    hub, directory = meshweb.build_hub(args)
    assert directory == str(tmp_path / "demo")
    assert hub.outbox == str(tmp_path / "demo/outbox")
    await hub.start()
    try:
        assert hub.demo and len(hub.nodes) == 3 and hub.error is None
        assert hub.state()["capabilities"] == {"weather": False, "sats": False}
        sent = await hub.send("hello demo", "!00000002")
        assert sent["delivery"] == "simulated" and sent["demo"] is True
        assert not (tmp_path / "messages.jsonl").exists()
        assert not (tmp_path / "follows.json").exists()
    finally:
        await hub.stop()


async def test_disabled_services_respond_and_socket_remains_usable(tmp_path):
    async with Rig(tmp_path) as rig:
        ws, _ = await rig.ws()
        for kind in ("weather", "sats", "radio"):
            await ws.send_json({"type": kind})
            reply = await recv_type(ws, "error" if kind == "radio" else kind)
            assert "disabled" in (reply.get("error") or reply.get("message"))
        await ws.send_json({"type": "send", "text": "still connected", "to": "^all"})
        assert (await recv_type(ws, "message"))["msg"]["text"] == "still connected"
        await ws.close()


def test_reply_script_validates_bytes_and_queues_atomically(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools/mesh-reply.sh"
    env = {**os.environ, "MESH_OUTBOX": str(tmp_path / "queue")}
    for text in ("é" * 91, "", "  "):
        result = subprocess.run(["bash", str(script), text], env=env, capture_output=True, text=True)
        assert result.returncode == 2
        assert not (tmp_path / "queue").exists()
    result = subprocess.run(["bash", str(script), "é" * 90], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    queued = list((tmp_path / "queue").iterdir())
    assert len(queued) == 1 and not queued[0].name.endswith(".tmp")
    assert queued[0].read_text() == "é" * 90 and queued[0].stat().st_mode & 0o777 == 0o600


def test_core_server_imports_without_optional_science_packages():
    code = """import sys
sys.path.insert(0, 'tools')
sys.modules['skyfield'] = None
sys.modules['pyModeS'] = None
import meshweb
assert meshweb.DemoBackend
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_failed_radio_handshake_releases_port_before_reconnect(tmp_path):
    opened = []
    class BadHandshake(FakeRadio):
        @property
        def my_id(self):
            raise OSError("node info unavailable")
    def factory(callback):
        if opened:
            assert opened[-1].closed
        backend = BadHandshake(callback) if len(opened) < 2 else FakeRadio(callback)
        opened.append(backend)
        return backend
    async with Rig(tmp_path, factory=factory) as rig:
        assert rig.hub.backend is None and rig.hub.error
        for _ in range(100):
            if rig.hub.backend is not None:
                break
            await asyncio.sleep(0.01)
        assert len(opened) == 3 and all(b.closed for b in opened[:2])
        assert rig.hub.backend is opened[-1] and rig.hub.error is None
