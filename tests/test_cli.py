"""Smoke the real entrypoint with isolated demo state, not a fake server factory."""
import asyncio
import json
from pathlib import Path
import signal
import socket
import sys

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("options", [["--home", "91,0"], ["--home", "nan,1"], ["--http-port", "0"], ["--allow", "bad"]])
async def test_cli_rejects_invalid_settings_without_writing_state(tmp_path, options):
    state = tmp_path / "state"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "tools/meshweb.py"), "--demo", "--state-dir", str(state), *options,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
    assert proc.returncode == 2 and b"error:" in stderr
    assert not state.exists() and not stdout


async def test_real_cli_serves_demo_with_private_reused_token_and_stops(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    demo = tmp_path / "demo"
    demo.mkdir()
    token_file = demo / "web-url.txt"
    token = "smoke-test-token-that-is-long-enough"
    token_file.write_text(f"http://127.0.0.1:{port}/?token={token}\n")
    token_file.chmod(0o644)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "tools/meshweb.py"), "--demo", "--state-dir", str(tmp_path), "--http-port", str(port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            for _ in range(100):
                if proc.returncode is not None:
                    _, err = await proc.communicate()
                    pytest.fail(f"demo startup exited: {err.decode()}")
                try:
                    async with client.get(base + "/api/state") as r:
                        assert r.status == 401
                    break
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("demo server did not bind")
            async with client.get(base + "/?token=" + token) as r:
                assert r.status == 200 and "SignalForge" in await r.text()
                assert r.headers["Referrer-Policy"] == "no-referrer"
            async with client.get(base + "/api/state") as r:
                state = await r.json()
                assert state["demo"] is True and len(state["nodes"]) == 3 and state["error"] is None
            async with client.ws_connect(base + "/ws") as ws:
                await ws.receive_json()
                await ws.send_json({"type": "send", "to": "!00000002", "text": "hello simulated mesh"})
                while True:
                    event = await asyncio.wait_for(ws.receive_json(), 2)
                    if event["type"] == "message":
                        assert event["msg"]["delivery"] == "simulated"
                        break
            assert token_file.stat().st_mode & 0o777 == 0o600
            assert token_file.read_text().strip().endswith(token)
            assert not (tmp_path / "nodes.json").exists()
            assert not (tmp_path / "messages.jsonl").exists()
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGINT)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            pytest.fail("demo server failed to release its resources on shutdown")
    assert proc.returncode == 0, err.decode()
    rows = [json.loads(line) for line in (demo / "messages.jsonl").read_text().splitlines()]
    assert rows[0]["demo"] is True
