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


# ---- production hardening: actionable errors, port conflicts, private state -----------------------
@pytest.mark.parametrize("error,expect", [
    ("Could not exclusively lock port /dev/ttyUSB0: [Errno 11] Resource temporarily unavailable", "Another program"),
    ("[Errno 13] could not open port /dev/ttyUSB0: [Errno 13] Permission denied: '/dev/ttyUSB0'", "usermod -aG uucp"),
    ("/dev/ttyUSB0 is not present and 0 candidate radios were found", "No radio found"),
    ("Timed out waiting for connection completion", "did not answer"),
])
def test_radio_errors_become_actionable_sentences(error, expect):
    text = meshcore.radio_error_hint(OSError(error), "/dev/ttyUSB0")
    assert text.startswith("Could not open radio: ") and expect in text
    assert meshcore.radio_error_hint(RuntimeError("weird")) == "Could not open radio: weird"


async def test_reconnect_reports_a_changed_reason_once(tmp_path):
    errors = iter([OSError("x is not present and 0 candidate radios were found")] * 3
                  + [OSError("Could not exclusively lock port x")] * 5)
    opened = []

    def factory(callback):
        err = next(errors, None)
        if err:
            raise err
        opened.append(FakeRadio(callback))
        return opened[-1]

    async with Rig(tmp_path, factory=factory) as rig:
        for _ in range(200):
            if rig.hub.backend is not None:
                break
            await asyncio.sleep(0.01)
        assert rig.hub.backend is opened[0] and rig.hub.error is None
        lines = [line for line in rig.out if line.startswith("ERROR")]
        assert len(lines) == 2  # "no radio" once, then "port busy" once: not one line per retry
        assert "No radio found" in lines[0] and "Another program" in lines[1]


def test_state_dir_is_made_private(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    (state / "outbox").mkdir(mode=0o755)
    old = os.umask(0o022)
    try:
        meshweb.prepare_state_dir(str(state))
        assert state.stat().st_mode & 0o777 == 0o700
        assert (state / "outbox").stat().st_mode & 0o777 == 0o700
        (state / "new.json").write_text("{}")
        assert (state / "new.json").stat().st_mode & 0o077 == 0
    finally:
        os.umask(old)


async def test_second_server_on_a_busy_port_exits_cleanly_without_touching_state(tmp_path):
    import socket
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(meshweb.__file__)), "--demo", "--state-dir", str(tmp_path / "state"),
            "--http-port", str(port), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode == meshweb.EXIT_PORT_IN_USE == 3
    assert b"Cannot listen on 127.0.0.1" in err and b"already running" in err and b"Traceback" not in err
    assert not (tmp_path / "state" / "demo" / "web-url.txt").exists()


async def test_node_table_errors_neither_abort_reconnect_nor_flood_the_log(tmp_path):
    class Flaky(FakeRadio):
        def nodes(self):
            raise OSError("reader wedged")
    async with Rig(tmp_path, factory=Flaky) as rig:
        assert rig.hub.backend is not None  # startup survived the failing first read
        await asyncio.sleep(0.4)  # ~8 poll periods
        assert len([line for line in rig.out if "node poll" in line]) == 1
        await rig.hub._radio_lost("test")
        for _ in range(100):
            if rig.hub.backend is not None:
                break
            await asyncio.sleep(0.01)
        assert rig.hub.backend is not None and not rig.hub._reconnecting


def _debug_hub(tmp_path, path=True):
    out = []
    hub = meshweb.Hub(FakeRadio, log_path=str(tmp_path / "m.jsonl"), outbox=str(tmp_path / "outbox"),
                      nodes_json=str(tmp_path / "n.json"), follows_path=str(tmp_path / "f.json"), out=out.append)
    if path:
        hub.debug_ui_path = str(tmp_path / "ui-debug.jsonl")
    return hub, out


def test_ui_debug_is_ignored_when_debug_is_off(tmp_path):
    hub, out = _debug_hub(tmp_path, path=False)
    hub.ui_debug({"type": "uidebug", "event": "click"})
    assert out == [] and not (tmp_path / "ui-debug.jsonl").exists()


def test_ui_debug_rate_limit_drops_and_summarises(tmp_path):
    hub, out = _debug_hub(tmp_path)
    for _ in range(500):  # a JS error loop: all in the same instant
        hub.ui_debug({"type": "uidebug", "event": "error"}, now=100.0)
    assert len(out) == hub.UI_DEBUG_BURST
    hub.ui_debug({"type": "uidebug", "event": "click"}, now=101.0)  # a second later the bucket has refilled
    assert out[-2] == f"UIDEBUG dropped {500 - hub.UI_DEBUG_BURST} events (rate limit)"
    assert out[-1].startswith("UIDEBUG {")
    assert len((tmp_path / "ui-debug.jsonl").read_text().splitlines()) == hub.UI_DEBUG_BURST + 1


def test_ui_debug_lines_stay_valid_json_when_huge(tmp_path):
    hub, out = _debug_hub(tmp_path)
    hub.ui_debug({"type": "uidebug", "event": "error", "stack": "x" * 100_000, "nested": [{"a": "y" * 9999}] * 3})
    hub.ui_debug({"type": "uidebug", **{f"k{i}": "z" * 400 for i in range(30)}})
    for line in out:
        json.loads(line[len("UIDEBUG "):])
    rows = [json.loads(r) for r in (tmp_path / "ui-debug.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and len(rows[0]["stack"]) == 500 and rows[1]["truncated"] is True


def test_ui_debug_log_rotates(tmp_path):
    hub, out = _debug_hub(tmp_path)
    hub.UI_DEBUG_MAX_BYTES = 1000
    for i in range(40):
        hub.ui_debug({"type": "uidebug", "event": "click", "i": i, "pad": "p" * 100}, now=float(i))
    assert (tmp_path / "ui-debug.jsonl.1").exists()
    assert (tmp_path / "ui-debug.jsonl").stat().st_size <= 1000 + 300
