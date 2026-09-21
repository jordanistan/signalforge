"""End-to-end tests: a real meshweb server (fake radio) driven by headless Chromium through Playwright."""
import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import meshweb  # noqa: E402
from aiohttp import web  # noqa: E402

playwright_sync = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402

CHROMIUM = "/usr/bin/chromium"
if not Path(CHROMIUM).exists():
    pytest.skip("system Chromium not found", allow_module_level=True)

NOW = time.time()
TOKEN = "e2e-token"
BROADCAST = "^all"
HOME = [30.2695, -97.75]  # downtown Austin
PLACES = [["Austin", "TX", 30.267, -97.743, 974447]]
BLANK_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082")
EVIL_SHORT = "<b>x</b>"
EVIL_LONG = "<img src=x onerror=window.__pwned=1>"


ISS_TLE = ("ISS (ZARYA)\n1 25544U 98067A   26263.78762384  .00007766  00000+0  14793-3 0  9996\n"
           "2 25544  51.6308 186.9475 0004819 163.3027 196.8121 15.49200337586572\n")


def node(short, long, heard, lat=None, lon=None, hops=None, snr=None, batt=None):
    n = {"user": {"id": "", "shortName": short, "longName": long, "hwModel": "HELTEC_V3"}, "lastHeard": heard}
    if lat is not None:
        n["position"] = {"latitude": lat, "longitude": lon}
    if hops is not None:
        n["hopsAway"] = hops
    if snr is not None:
        n["snr"] = snr
    if batt is not None:
        n["deviceMetrics"] = {"batteryLevel": batt, "voltage": 4.0, "channelUtilization": 5.0, "airUtilTx": 1.0}
    return n


class E2ERadio:
    my_id = "!00000aaa"

    def __init__(self, on_packet):
        self.on_packet = on_packet
        self.sent = []
        self._nodes = {
            "!00000aaa": node("me", "Me Node", NOW, 30.2695, -97.75, batt=88),
            "!02ecdcdc": node("dcdc", "Meshtastic dcdc", NOW - 30, hops=0, snr=6.5),  # live, no position
            "!11111111": node("Ridge", "Ridge Router", NOW - 600, 30.29, -97.79, hops=2, snr=-3.0),  # live
            "!22222222": node("Day", "Day Node", NOW - 5 * 3600, 30.25, -97.72, hops=1, snr=2.0),
            "!33333333": node("Old", "Old Node", NOW - 40 * 86400, 30.30, -97.70, hops=3, snr=-9.0),
            "!44444444": node(EVIL_SHORT, EVIL_LONG, NOW - 60, 30.26, -97.76, hops=1, snr=4.0),
        }

    def nodes(self):
        return {k: json.loads(json.dumps(v)) for k, v in self._nodes.items()}

    def send_text(self, text, dest, on_ack=None):
        self.sent.append((text, dest))
        if on_ack and dest != BROADCAST:
            on_ack({"decoded": {"routing": {"errorReason": "NONE"}}})

    def close(self):
        pass


def packet(sender, text, to="!00000aaa"):
    return {"fromId": sender, "toId": to, "rxTime": time.time(), "decoded": {"text": text}}


class LiveServer:
    def __init__(self, tmp_path, factory=E2ERadio, sdr=None, **hub_kw):
        self.out = []
        hub_kw.setdefault("home", HOME)
        self.hub = meshweb.Hub(factory, log_path=str(tmp_path / "messages.jsonl"), outbox=str(tmp_path / "outbox"),
                               nodes_json=str(tmp_path / "nodes.json"), places=PLACES, out=self.out.append,
                               follows_path=str(tmp_path / "follows.json"), **hub_kw)
        self.hub.poll_secs, self.hub.chunk_delay, self.hub.follow_check_secs = 0.2, 0, 0.2
        self.adsb = None
        if sdr:  # a RadioWatch wired to fake receivers, so the SDR panels can be tested without hardware
            import radiowatch as rw
            import test_radiowatch as trw
            self.adsb = trw.FakeAdsbProc()

            async def spawn_adsb():
                return self.adsb

            async def survey(band, seconds):
                return trw.csv_with({162_400_000: -30.0}), ""

            self.job = None
            self.job_cmds = []

            async def spawn_job(cmd):
                self.job_cmds.append(cmd)
                self.job = trw.FakeAdsbProc()
                return self.job

            self.hub.radio = rw.RadioWatch(self.hub.broadcast, bands=[rw.BANDS[1]], available=True, settle=0,
                                           snapshot=str(tmp_path / "radio.json"),
                                           kill=trw.kill_fake, spawn_adsb=spawn_adsb, survey=survey,
                                           survey_seconds=0, pause=0.05, reference=(52.0, 4.0),
                                           spawn_job=spawn_job, job_check=lambda cls: "")
            import nws_fixture
            import satellites
            import weather
            self.nws = nws_fixture.FakeNws()
            self.hub.weather = weather.WeatherService(self.hub.broadcast, self.hub.location, fetch_json=self.nws,
                                                      on_alert=self.hub.on_weather_alert, refresh_secs=3600)

            async def tle_text(url):
                return ISS_TLE

            self.hub.sats = satellites.SatTracker(self.hub.location, tle_dir=str(tmp_path / "tle"), fetch_text=tle_text)
        self.loop = asyncio.new_event_loop()
        self.port = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.ready.set()
        self.loop.run_forever()

    async def _start(self):
        self.runner = web.AppRunner(meshweb.make_app(self.hub, TOKEN))
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def radio(self):
        return self.hub.backend


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture
def ctx(browser):
    c = browser.new_context(viewport={"width": 1500, "height": 900})
    c.route(re.compile(r"^(?!http://127\.0\.0\.1).*"),  # no real tile traffic in tests: answer with a blank tile
            lambda r: r.fulfill(status=200, content_type="image/png", body=BLANK_PNG))
    yield c
    c.close()


@pytest.fixture
def page(ctx):
    pg = ctx.new_page()
    pg.console_msgs = []
    pg.on("console", lambda m: pg.console_msgs.append((m.type, m.text)))
    pg.on("pageerror", lambda e: pg.console_msgs.append(("pageerror", str(e))))
    return pg


@pytest.fixture
def srv(tmp_path):
    with LiveServer(tmp_path) as s:
        yield s


def open_app(srv, page, wait=True, view="map"):
    if view:  # most tests drive the map/lists: start them there (the default landing view is the Command Center)
        page.add_init_script(f"try{{localStorage.setItem('mesh.view','{view}')}}catch(e){{}}")
    page.goto(f"{srv.url}/?token={TOKEN}")
    if wait:
        expect(page.locator("#status")).to_have_text("connected")
        expect(page.locator("#node-list li")).not_to_have_count(0)
    return page


def marker(page, nid):
    return page.locator(f'.node-marker[data-id="{nid}"]')


# ---- basics ---------------------------------------------------------------------------------------
def test_loads_with_state_counts_and_no_console_errors(srv, page):
    open_app(srv, page)
    expect(page.locator("#me")).to_contain_text("me")
    expect(page.locator("#me")).to_contain_text("88%")
    expect(page.locator("#counts")).to_have_text("3 live · 5 known")  # dcdc, Ridge, evil are live; Day/Old are not
    expect(page.locator("#node-list li")).to_have_count(5)  # every known node except this radio
    expect(page.locator("#nodes-count")).to_have_text("(5)")
    expect(page.locator(".node-marker")).to_have_count(5)  # me + the four with positions
    expect(page.locator("#mapnote")).to_contain_text("dcdc")  # live but no position: listed under the map
    page.wait_for_timeout(300)
    assert page.console_msgs == [], page.console_msgs  # includes CSP violations


def test_layout_fills_the_window_on_every_tab(srv, page):
    open_app(srv, page)
    for tab in ("#tab-nodes", "#tab-chats"):
        page.click(tab)
        h = page.evaluate("document.querySelector('#layout').getBoundingClientRect().height")
        assert h == 900 - 48, (tab, h)  # regression: a hidden banner used to collapse the layout


def test_node_list_is_sorted_and_styled_by_freshness(srv, page):
    open_app(srv, page)
    names = page.locator("#node-list li .name").all_inner_texts()
    assert names == ["dcdc", EVIL_SHORT, "Ridge", "Day", "Old"]  # most recently heard first
    expect(page.locator("#node-list li.stale")).to_have_count(1)  # only the 40-day-old node is dimmed
    expect(page.locator('#node-list li[data-id="!33333333"]')).to_have_class(re.compile("stale"))
    expect(page.locator('#node-list li[data-id="!11111111"] .sub')).to_contain_text("5 km from Austin, TX")
    expect(page.locator('#node-list li[data-id="!22222222"] .sub')).to_contain_text("in Austin, TX")


def test_filters_and_search(srv, page):
    open_app(srv, page)
    page.click('.chip[data-filter="live"]')
    expect(page.locator("#node-list li")).to_have_count(3)
    page.click('.chip[data-filter="direct"]')
    expect(page.locator("#node-list li")).to_have_count(1)  # only dcdc has hops == 0
    page.click('.chip[data-filter="mapped"]')
    expect(page.locator("#node-list li")).to_have_count(4)  # dcdc has no position
    page.click('.chip[data-filter="all"]')
    page.fill("#search", "ridge")
    expect(page.locator("#node-list li")).to_have_count(1)
    page.fill("#search", "!2222")
    expect(page.locator("#node-list li .name")).to_have_text(["Day"])
    page.fill("#search", "zzz")
    expect(page.locator("#node-list")).to_contain_text("No nodes match")


# ---- selection & details ------------------------------------------------------------------------
def test_click_row_selects_node_and_shows_details(srv, page):
    open_app(srv, page)
    page.click('#node-list li[data-id="!11111111"]')
    d = page.locator("#detail")
    expect(d).to_contain_text("Ridge Router")
    expect(d).to_contain_text("!11111111")
    expect(d).to_contain_text("-3.0 dB")
    expect(d).to_contain_text("2 hops away")
    expect(d).to_contain_text("5 km from Austin, TX")
    expect(d).to_contain_text("From you")
    expect(d).to_contain_text("live")
    expect(marker(page, "!11111111")).to_have_class(re.compile("selected"))
    expect(page.locator('#node-list li[data-id="!11111111"]')).to_have_class(re.compile("sel"))
    page.click('#node-list li[data-id="!33333333"]')
    expect(page.locator("#detail")).to_contain_text("remembered")
    expect(marker(page, "!11111111")).not_to_have_class(re.compile("selected"))


def test_node_without_position_has_no_centre_and_says_so(srv, page):
    open_app(srv, page)
    page.click('#node-list li[data-id="!02ecdcdc"]')
    expect(page.locator("#detail")).to_contain_text("not reported")
    expect(page.locator("#btn-center")).to_be_disabled()
    page.click('#mapnote a[data-id="!02ecdcdc"]')  # the "heard, no position" note is clickable too
    expect(page.locator("#detail")).to_contain_text("Meshtastic dcdc")


def test_click_map_marker_selects_and_popup_message_button_opens_dm(srv, page):
    open_app(srv, page)
    marker(page, "!11111111").click()
    expect(page.locator("#detail")).to_contain_text("Ridge Router")
    pop = page.locator(".leaflet-popup .pop")
    expect(pop).to_contain_text("Ridge Router")
    pop.locator("button").click()
    expect(page.locator("#chat-head")).to_contain_text("Ridge")
    expect(page.locator("#chat-head")).to_contain_text("!11111111")
    expect(page.locator("#msg-input")).to_be_enabled()
    expect(page.locator("#msg-input")).to_be_focused()


def test_double_click_marker_and_row_open_dm(srv, page):
    open_app(srv, page)
    marker(page, "!22222222").dblclick()
    expect(page.locator("#chat-head")).to_contain_text("!22222222")
    page.dblclick('#node-list li[data-id="!02ecdcdc"]')
    expect(page.locator("#chat-head")).to_contain_text("!02ecdcdc")
    page.click('#node-list li[data-id="!11111111"]')
    page.click("#btn-message")
    expect(page.locator("#chat-head")).to_contain_text("!11111111")


def test_map_buttons_fit_and_home(srv, page):
    open_app(srv, page)
    page.evaluate("map.setView([40, -100], 4)")
    page.click("#btn-home")
    page.wait_for_timeout(800)  # Leaflet animates the move
    c = page.evaluate("[map.getCenter().lat, map.getCenter().lng, map.getZoom()]")
    assert abs(c[0] - HOME[0]) < 0.01 and abs(c[1] - HOME[1]) < 0.01 and c[2] >= 12
    page.evaluate("map.setView([40, -100], 4)")
    page.click("#btn-fit")
    page.wait_for_timeout(800)
    b = page.evaluate("[map.getBounds().getSouth(), map.getBounds().getNorth(), map.getBounds().getWest(), map.getBounds().getEast()]")
    assert b[0] < 30.26 < b[1] and b[2] < -97.76 < b[3]  # the live positioned nodes are in view
    page.click('#node-list li[data-id="!11111111"]')
    page.click("#btn-center")
    page.wait_for_timeout(800)
    c = page.evaluate("[map.getCenter().lat, map.getCenter().lng]")
    assert abs(c[0] - 30.29) < 0.01 and abs(c[1] + 97.79) < 0.01


# ---- messaging -------------------------------------------------------------------------------------
def test_send_dm_shows_bubble_ack_and_reaches_the_radio(srv, page):
    open_app(srv, page)
    page.dblclick('#node-list li[data-id="!02ecdcdc"]')
    page.fill("#msg-input", "hello dcdc")
    page.keyboard.press("Enter")
    out = page.locator("#thread .msg.out")
    expect(out).to_have_count(1)
    expect(out.locator(".text")).to_have_text("hello dcdc")
    expect(out.locator(".foot .good")).to_have_text("✓ delivered")
    expect(page.locator("#msg-input")).to_have_value("")
    assert srv.radio.sent == [("hello dcdc", "!02ecdcdc")]


def test_broadcast_goes_to_channel_thread_only(srv, page):
    open_app(srv, page)
    page.click("#tab-chats")
    page.click('#chat-list li[data-conv="^all"]')
    expect(page.locator("#chat-head")).to_contain_text("# Channel")
    page.fill("#msg-input", "hi everyone")
    page.click("#btn-send")
    expect(page.locator("#thread .msg.out .text")).to_have_text("hi everyone")
    expect(page.locator("#thread .msg.out .foot .good")).to_have_count(0)  # broadcasts are not ACKed
    assert srv.radio.sent == [("hi everyone", BROADCAST)]
    page.dblclick('#node-list li[data-id="!02ecdcdc"]') if page.locator("#pane-nodes").is_visible() else None


def test_conversations_stay_separate_and_dm_targets_follow_the_open_thread(srv, page):
    open_app(srv, page)
    for nid, text in (("!02ecdcdc", "alpha"), ("!11111111", "bravo")):
        page.dblclick(f'#node-list li[data-id="{nid}"]')
        page.fill("#msg-input", text)
        page.keyboard.press("Enter")
        expect(page.locator("#thread .msg.out .text")).to_have_text(text)
    page.click("#tab-chats")
    page.click('#chat-list li[data-conv="^all"]')
    page.fill("#msg-input", "gamma")
    page.keyboard.press("Enter")
    expect(page.locator("#thread .msg.out .text")).to_have_text("gamma")
    assert srv.radio.sent == [("alpha", "!02ecdcdc"), ("bravo", "!11111111"), ("gamma", BROADCAST)]
    page.click('#chat-list li[data-conv="!02ecdcdc"]')
    expect(page.locator("#thread .msg .text")).to_have_text(["alpha"])
    page.click('#chat-list li[data-conv="!11111111"]')
    expect(page.locator("#thread .msg .text")).to_have_text(["bravo"])
    expect(page.locator("#chat-list li")).to_have_count(3)


def test_byte_limit_disables_send(srv, page):
    open_app(srv, page)
    page.dblclick('#node-list li[data-id="!02ecdcdc"]')
    page.fill("#msg-input", "x" * 200)
    expect(page.locator("#btn-send")).to_be_enabled()
    expect(page.locator("#byte-count")).to_have_text("200/200 bytes")
    page.fill("#msg-input", "x" * 201)
    expect(page.locator("#btn-send")).to_be_disabled()
    expect(page.locator("#byte-count")).to_have_class("over")
    page.fill("#msg-input", "é" * 101)  # 202 bytes, only 101 characters
    expect(page.locator("#btn-send")).to_be_disabled()
    page.fill("#msg-input", "   ")
    expect(page.locator("#btn-send")).to_be_disabled()
    page.keyboard.press("Enter")
    assert srv.radio.sent == []


def test_shift_enter_makes_a_newline_instead_of_sending(srv, page):
    open_app(srv, page)
    page.dblclick('#node-list li[data-id="!02ecdcdc"]')
    page.fill("#msg-input", "line1")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("line2")
    assert srv.radio.sent == []
    page.keyboard.press("Enter")
    expect(page.locator("#thread .msg.out .text")).to_have_text("line1\nline2")


def test_incoming_messages_raise_unread_toasts_and_clear_when_read(srv, page):
    open_app(srv, page)
    expect(page.locator("#btn-unread")).to_be_hidden()
    page.dblclick('#node-list li[data-id="!22222222"]')  # the open conversation is Day's, not dcdc's or the channel
    srv.radio.on_packet(packet("!02ecdcdc", "Where are you parked?"))
    srv.radio.on_packet(packet("!11111111", "channel chatter", to=BROADCAST))
    srv.radio.on_packet(packet("!02ecdcdc", "hello?"))
    expect(page.locator("#btn-unread")).to_have_text("✉ 3 unread")
    expect(page).to_have_title("(3) SignalForge")
    expect(page.locator("#toasts .toast")).to_have_count(3)
    expect(page.locator("#toasts .toast").first).to_contain_text("hello?")
    expect(page.locator("#chats-badge")).to_have_text("3")
    page.click("#tab-chats")
    expect(page.locator('#chat-list li[data-conv="!02ecdcdc"] .badge')).to_have_text("2")
    expect(page.locator('#chat-list li[data-conv="^all"] .badge')).to_have_text("1")
    page.click('#chat-list li[data-conv="!02ecdcdc"]')
    expect(page.locator("#thread .msg .text")).to_have_text(["Where are you parked?", "hello?"])
    expect(page.locator("#btn-unread")).to_have_text("✉ 1 unread")  # only the channel one is left
    expect(page.locator('#chat-list li[data-conv="!02ecdcdc"] .badge')).to_have_count(0)
    page.locator("#btn-unread").click()  # jumps to the newest unread conversation
    expect(page.locator("#chat-head")).to_contain_text("# Channel")
    expect(page.locator("#btn-unread")).to_be_hidden()
    expect(page).to_have_title("SignalForge")
    # clicking a toast opens its conversation
    srv.radio.on_packet(packet("!22222222", "ping from Day"))
    page.locator("#toasts .toast", has_text="ping from Day").click()
    expect(page.locator("#chat-head")).to_contain_text("!22222222")
    expect(page.locator("#thread .msg .text")).to_have_text(["ping from Day"])


def test_channel_message_in_the_open_channel_thread_is_read_at_once(srv, page):
    open_app(srv, page)  # the channel is the default conversation
    srv.radio.on_packet(packet("!11111111", "channel chatter", to=BROADCAST))
    expect(page.locator("#thread .msg.in .text")).to_have_text("channel chatter")
    expect(page.locator("#thread .who")).to_have_text("Ridge")
    expect(page.locator("#btn-unread")).to_be_hidden()


def test_incoming_to_the_open_thread_is_read_immediately(srv, page):
    open_app(srv, page)
    page.dblclick('#node-list li[data-id="!02ecdcdc"]')
    srv.radio.on_packet(packet("!02ecdcdc", "live reply"))
    expect(page.locator("#thread .msg.in .text")).to_have_text("live reply")
    expect(page.locator("#btn-unread")).to_be_hidden()
    expect(page.locator("#toasts .toast")).to_have_count(0)


def test_unread_state_survives_reload_and_old_history_starts_read(tmp_path, ctx):
    (tmp_path / "messages.jsonl").write_text(json.dumps(
        {"ts": NOW - 3600, "from": "!02ecdcdc", "to": "!00000aaa", "text": "from last week", "dir": "in", "id": "a1"}) + "\n")
    with LiveServer(tmp_path) as srv:
        page = ctx.new_page()
        open_app(srv, page)
        expect(page.locator("#btn-unread")).to_be_hidden()  # first visit: history counts as read
        page.goto("about:blank")  # close the app, then a message arrives while it is not open
        srv.radio.on_packet(packet("!02ecdcdc", "while you were away"))
        time.sleep(0.3)
        page.goto(f"{srv.url}/")
        expect(page.locator("#status")).to_have_text("connected")
        expect(page.locator("#btn-unread")).to_have_text("✉ 1 unread")


def test_claude_bridge_replies_are_labelled(tmp_path, ctx):
    with LiveServer(tmp_path, bridge=True, allow=["!02ecdcdc"]) as srv:
        page = ctx.new_page()
        open_app(srv, page)
        page.dblclick('#node-list li[data-id="!02ecdcdc"]')
        srv.radio.on_packet(packet("!02ecdcdc", "how far is Zilker?"))
        (tmp_path / "outbox").mkdir(exist_ok=True)
        (tmp_path / "outbox" / "1").write_text("About 1.1 miles.")
        claude = page.locator("#thread .msg.claude")
        expect(claude).to_have_count(1, timeout=5000)
        expect(claude.locator(".who")).to_have_text("Claude")
        expect(claude.locator(".text")).to_have_text("About 1.1 miles.")
        assert "MSG from !02ecdcdc: how far is Zilker?" in srv.out


# ---- security ---------------------------------------------------------------------------------------
def test_hostile_node_names_and_messages_are_shown_as_text_not_run(srv, page):
    open_app(srv, page)
    srv.radio.on_packet(packet("!44444444", "<script>window.__pwned=2</script><img src=x onerror=window.__pwned=3>", to=BROADCAST))
    page.click('#node-list li[data-id="!44444444"]')
    expect(page.locator('#node-list li[data-id="!44444444"] .name')).to_have_text(EVIL_SHORT)
    expect(page.locator("#detail h2")).to_contain_text(EVIL_LONG)
    marker(page, "!44444444").click()
    expect(page.locator(".leaflet-popup .pop h3")).to_have_text(EVIL_LONG)
    page.click("#tab-chats")
    page.click('#chat-list li[data-conv="^all"]')
    expect(page.locator("#thread .msg .text")).to_contain_text("<script>window.__pwned=2</script>")
    expect(page.locator("#thread .who")).to_have_text(EVIL_SHORT)
    page.wait_for_timeout(300)
    assert page.evaluate("window.__pwned") is None
    for sel in ("#node-list img", "#detail img", "#thread img", "#thread script", ".leaflet-popup img", ".node-label img", ".node-label b"):
        assert page.locator(sel).count() == 0, sel
    assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_page_requires_the_token(srv, ctx):
    anon = ctx.new_page()
    r = anon.goto(f"{srv.url}/")
    assert r.status == 401 and "token" in anon.inner_text("body").lower()
    r = anon.goto(f"{srv.url}/?token=wrong")
    assert r.status == 401
    r = anon.goto(f"{srv.url}/api/state")
    assert r.status == 401 and "nodes" not in anon.inner_text("body")


def test_security_headers_are_sent(srv, page):
    r = page.goto(f"{srv.url}/?token={TOKEN}")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"


# ---- resilience ---------------------------------------------------------------------------------------
def test_radio_failure_shows_a_banner_and_disables_sending(tmp_path, ctx):
    def boom(on_packet):
        raise OSError("Device or resource busy")

    with LiveServer(tmp_path, factory=boom) as srv:
        page = ctx.new_page()
        page.goto(f"{srv.url}/?token={TOKEN}")
        expect(page.locator("#banner")).to_contain_text("Device or resource busy")
        expect(page.locator("#status")).to_have_text("radio offline")
        expect(page.locator("#msg-input")).to_be_disabled()
        expect(page.locator("#btn-send")).to_be_disabled()


def test_reconnects_after_the_socket_drops(srv, page):
    open_app(srv, page)
    page.evaluate("ws.close()")
    expect(page.locator("#status")).to_have_text("reconnecting…")
    expect(page.locator("#status")).to_have_text("connected", timeout=6000)
    srv.radio.on_packet(packet("!02ecdcdc", "after reconnect"))
    expect(page.locator("#btn-unread")).to_have_text("✉ 1 unread")


def test_node_updates_are_pushed_live(srv, page):
    open_app(srv, page)
    srv.radio._nodes["!55555555"] = node("Newbie", "Just Arrived", time.time(), 30.28, -97.74, hops=1, snr=5.0)
    expect(page.locator('#node-list li[data-id="!55555555"]')).to_be_visible(timeout=5000)
    expect(page.locator("#counts")).to_have_text("4 live · 6 known")
    expect(marker(page, "!55555555")).to_have_count(1)
    del srv.radio._nodes["!55555555"]
    expect(marker(page, "!55555555")).to_have_count(0, timeout=5000)


# ---- map layers, theme ------------------------------------------------------------------------------
def test_offline_vector_layer_draws_lines_and_city_labels(srv, page):
    open_app(srv, page)
    page.hover(".leaflet-control-layers")
    page.locator(".leaflet-control-layers-base label", has_text="Offline vector").click()
    expect(page.locator("#map")).to_have_class(re.compile("offline"))
    expect(page.locator(".city span", has_text="Austin")).to_be_visible(timeout=10000)
    assert page.locator(".leaflet-basemap-pane canvas").count() >= 1  # roads/rivers/borders on a canvas
    expect(page.locator(".leaflet-tile")).to_have_count(0)  # no tiles needed
    assert marker(page, "!11111111").count() == 1  # nodes stay on top
    page.reload()
    expect(page.locator("#map")).to_have_class(re.compile("offline"))  # the choice is remembered


def test_theme_toggle_persists(srv, page):
    open_app(srv, page)
    start = page.get_attribute("html", "data-theme")
    page.click("#btn-theme")
    flipped = page.get_attribute("html", "data-theme")
    assert flipped != start
    page.reload()
    expect(page.locator("#status")).to_have_text("connected")
    assert page.get_attribute("html", "data-theme") == flipped


def test_follow_mode_indicator_and_labels(tmp_path, ctx):
    with LiveServer(tmp_path, bridge=True, allow=["!02ecdcdc"]) as srv:
        page = ctx.new_page()
        open_app(srv, page)
        expect(page.locator("#follows")).to_be_hidden()
        page.dblclick('#node-list li[data-id="!02ecdcdc"]')
        srv.radio.on_packet(packet("!02ecdcdc", "follow austin traffic"))
        expect(page.locator("#follows")).to_have_text("◉ following 1")
        expect(page.locator("#follows")).to_have_attribute("title", re.compile("f1: austin traffic"))
        expect(page.locator("#thread .msg.claude .who")).to_have_text("Follow mode")
        expect(page.locator("#thread .msg.claude .text")).to_contain_text("Following 'austin traffic' (f1)")
        assert not any(line.startswith("MSG from") for line in srv.out)  # the command is not passed on as a question
        srv.radio.on_packet(packet("!02ecdcdc", "unfollow all"))
        expect(page.locator("#follows")).to_be_hidden()
        page.close()



# ---- command center, aircraft ---------------------------------------------------------------------
FRAMES = ["*8D4840D6202CC371C32CE0576098;", "*8D40621D58C386435CC412692AD6;", "*8D40621D58C382D690C8AC2863A7;",
          "*8D485020994409940838175B284F;"]


def test_command_center_is_the_default_landing_view_and_summarises_everything(tmp_path, ctx):
    with LiveServer(tmp_path, bridge=True, allow=["!02ecdcdc"], sdr=True) as srv:
        page = ctx.new_page()
        open_app(srv, page, view=None)  # no override: the real default
        cmd = page.locator("#command")
        expect(cmd).to_be_visible()
        expect(page.locator("#view-cmd")).to_have_class(re.compile("active"))
        expect(cmd.locator(".stat").first).to_contain_text("me")
        expect(cmd.locator(".cmd-clock")).to_contain_text(":")
        for card in ("card-news", "card-msgs", "card-mesh", "card-air", "card-radio", "card-feed"):
            expect(page.locator(f"#{card}")).to_be_visible()
        expect(page.locator("#card-mesh")).to_contain_text("live now")
        expect(page.locator("#card-mesh .crow")).to_contain_text(["dcdc"])
        expect(page.locator("#card-news")).to_contain_text("Nothing is being followed")
        page.close()


def test_view_switch_between_command_center_and_map_is_remembered(tmp_path, ctx):
    with LiveServer(tmp_path) as srv:
        page = ctx.new_page()
        open_app(srv, page, view=None)
        expect(page.locator("#command")).to_be_visible()
        page.click("#view-map")
        expect(page.locator("#command")).to_be_hidden()
        expect(page.locator("#map")).to_be_visible()
        assert page.evaluate("map.getSize().y") > 300  # the map was resized when it came back
        page.reload()
        expect(page.locator("#status")).to_have_text("connected")
        expect(page.locator("#command")).to_be_hidden()  # remembered across a reload
        page.click("#view-cmd")
        expect(page.locator("#command")).to_be_visible()
        page.close()


def test_news_card_shows_follows_with_their_last_report(tmp_path, ctx):
    with LiveServer(tmp_path, bridge=True, allow=["!02ecdcdc"]) as srv:
        page = ctx.new_page()
        open_app(srv, page, view=None)
        srv.radio.on_packet(packet("!02ecdcdc", "follow Austin traffic"))
        news = page.locator("#card-news")
        expect(news).to_contain_text("f1 · Austin traffic", timeout=5000)
        expect(news).to_contain_text("first check is pending")
        srv.hub.follows.checked("f1", report="I-35 southbound closed at 183 per KXAN, 11:20pm CT", sent=1)
        expect(news).to_contain_text("I-35 southbound closed at 183", timeout=6000)  # pushed live from the follow file
        expect(news).to_contain_text("checked")
        expect(page.locator("#card-feed")).to_contain_text("Update recorded for")
        page.close()


def test_messages_card_and_activity_feed_track_traffic_and_open_the_chat(tmp_path, ctx):
    with LiveServer(tmp_path) as srv:
        page = ctx.new_page()
        open_app(srv, page, view=None)
        srv.radio.on_packet(packet("!02ecdcdc", "Where are you parked?"))
        expect(page.locator("#card-msgs")).to_contain_text("Where are you parked?")
        expect(page.locator("#card-msgs .badge")).to_have_text("1")
        expect(page.locator("#card-feed")).to_contain_text("dcdc: Where are you parked?")
        expect(page.locator(".stat", has_text="unread")).to_contain_text("1")
        page.locator("#card-msgs .crow").first.click()
        expect(page.locator("#chat-head")).to_contain_text("dcdc")
        page.close()


def test_command_center_renders_hostile_text_as_text(tmp_path, ctx):
    with LiveServer(tmp_path) as srv:
        page = ctx.new_page()
        open_app(srv, page, view=None)
        srv.radio.on_packet(packet("!44444444", "<img src=x onerror=window.__pwned=9>", to=BROADCAST))
        expect(page.locator("#card-msgs")).to_contain_text("<img src=x onerror=window.__pwned=9>")
        expect(page.locator("#card-feed")).to_contain_text(EVIL_SHORT)
        page.wait_for_timeout(300)
        assert page.evaluate("window.__pwned") is None
        assert page.locator("#command img").count() == 0
        page.close()


def test_air_tab_tracks_aircraft_and_puts_labelled_planes_on_the_map(tmp_path, ctx):
    with LiveServer(tmp_path, sdr=True) as srv:
        page = ctx.new_page()
        open_app(srv, page)
        page.click("#tab-air")
        expect(page.locator("#air-status")).to_contain_text("idle")
        page.click("#btn-air")
        expect(page.locator("#btn-air")).to_have_text("Stop tracking")
        for f in FRAMES:
            srv.adsb.push(f)
        rows = page.locator("#air-list li.air-row")
        expect(rows).to_have_count(3, timeout=6000)  # KLM1023, 40621D (with position), 485020
        expect(page.locator("#air-list")).to_contain_text("KLM1023")
        expect(page.locator("#air-list")).to_contain_text("FL380")
        expect(page.locator("#air-list li", has_text="40621D").locator(".meta")).to_contain_text("NM")
        expect(page.locator("#air-status")).to_contain_text("3 aircraft")
        expect(page.locator("#air-count")).to_have_text("3")
        plane = page.locator(".plane[data-icao]")
        expect(plane).to_have_count(1)  # only the aircraft with a decoded position is on the map
        expect(page.locator(".plane-label")).to_contain_text("FL380")
        page.click("#btn-air")
        expect(page.locator("#air-status")).to_contain_text("idle", timeout=5000)
        assert srv.adsb.killed  # the receiver was stopped: the dongle is free
        page.close()


def test_command_center_airspace_radar_shows_aircraft(tmp_path, ctx):
    with LiveServer(tmp_path, sdr=True) as srv:
        page = ctx.new_page()
        open_app(srv, page, view=None)
        expect(page.locator("#card-air")).to_contain_text("Aircraft tracking is off")
        page.click("#cmd-air-btn")
        for f in FRAMES:
            srv.adsb.push(f)
        expect(page.locator("#card-air .radar .dotp")).to_have_count(1, timeout=6000)
        expect(page.locator("#card-air .radar .sweep")).to_have_count(1)
        expect(page.locator("#card-air .crow").first).to_contain_text("40621D")
        expect(page.locator(".stat", has_text=re.compile(r"^\s*3\s+aircraft"))).to_have_count(1)
        expect(page.locator("#card-feed")).to_contain_text("40621D")  # the newly seen aircraft is announced in the feed
        page.close()


def test_dashboard_navigation_always_returns_to_command_center(srv, page):
    open_app(srv, page, view='map')
    expect(page.locator("#map")).to_be_visible()
    page.click("#nav-dashboard")
    expect(page.locator("#command")).to_be_visible()
    expect(page.locator("#card-mesh")).to_be_visible()
    expect(page.locator("#card-msgs")).to_be_visible()
    expect(page.locator("#card-air")).to_be_visible()
    expect(page.locator("#card-radio")).to_be_visible()
    expect(page.locator("#card-wx")).to_be_visible()
    expect(page.locator("#card-feed")).to_be_visible()
    expect(page.locator("body")).to_have_class(re.compile(r"view-command"))
    page.click("#view-map")
    expect(page.locator("#map")).to_be_visible()
    page.click("#nav-dashboard")
    expect(page.locator("#command")).to_be_visible()
    page.close()


def test_starting_the_survey_switches_the_dongle_away_from_aircraft(tmp_path, ctx):
    with LiveServer(tmp_path, sdr=True) as srv:
        page = ctx.new_page()
        open_app(srv, page)
        page.click("#tab-air")
        page.click("#btn-air")
        expect(page.locator("#btn-air")).to_have_text("Stop tracking")
        page.click("#tab-radio")
        page.click("#btn-radio")
        expect(page.locator("#btn-radio")).to_have_text("Stop survey")
        page.click("#tab-air")
        expect(page.locator("#btn-air")).to_have_text("Start tracking", timeout=5000)  # one program on the dongle at a time
        page.click("#tab-radio")
        expect(page.locator("#radio-list .radio-row", has_text="162.4000")).to_be_visible(timeout=6000)
        page.close()


def test_air_and_radio_report_a_busy_dongle(tmp_path, ctx):
    with LiveServer(tmp_path, sdr=True) as srv:
        page = ctx.new_page()
        open_app(srv, page)
        page.click("#tab-air")
        srv.adsb.stderr_data = b"usb_claim_interface error -6"
        page.click("#btn-air")
        srv.adsb.end()
        expect(page.locator("#air-error")).to_contain_text("in use by another program", timeout=6000)
        expect(page.locator("#btn-air")).to_have_text("Start tracking")
        page.close()


def test_aircraft_layer_can_be_toggled_from_the_layer_control(tmp_path, ctx):
    with LiveServer(tmp_path, sdr=True) as srv:
        page = ctx.new_page()
        open_app(srv, page)
        page.click("#tab-air")
        page.click("#btn-air")
        for f in FRAMES:
            srv.adsb.push(f)
        expect(page.locator(".plane[data-icao]")).to_have_count(1, timeout=6000)
        page.hover(".leaflet-control-layers")
        page.locator(".leaflet-control-layers-overlays label", has_text="Aircraft").locator("input").uncheck()
        expect(page.locator(".plane[data-icao]")).to_have_count(0)
        page.close()


def test_optional_pages_report_missing_services_and_disable_actions(srv, page):
    open_app(srv, page)
    page.click("#nav-weather")
    expect(page.locator("#wp-place")).to_have_text("Unavailable")
    expect(page.locator("#wp-refresh")).to_be_disabled()
    expect(page.locator("#wp-nwr")).to_be_disabled()
    page.click("#nav-sky")
    expect(page.locator("#sk-list")).to_contain_text("not enabled")
    expect(page.locator("#sk-refresh")).to_be_disabled()
    for name in ("pager", "sensors"):
        page.click(f"#nav-{name}")
        expect(page.locator(f"#{name}-toggle")).to_be_disabled()
    page.click("#nav-radio")
    expect(page.locator("#rp-listen")).to_be_disabled()
    expect(page.locator("#rp-toggle")).to_be_disabled()
    assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_weather_page_displays_forecasts_and_stale_alerts_honestly(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        open_app(srv, page)
        page.click("#nav-weather")
        expect(page.locator("#wp-place")).to_contain_text("Austin")
        expect(page.locator("#wp-now")).to_contain_text("80")
        expect(page.locator("#wp-days .day-card")).to_have_count(3)
        expect(page.locator("#wp-hourly > div")).to_have_count(24)
        expect(page.locator("#wp-alerts")).to_contain_text("No active weather alerts")
        srv.nws.fail = True
        asyncio.run_coroutine_threadsafe(srv.hub.weather.refresh(), srv.loop).result(5)
        expect(page.locator('#wp-alerts [data-section="alerts"]')).to_contain_text("stale")
        expect(page.locator("#wp-alerts")).not_to_contain_text("No active weather alerts")
        expect(page.locator("#wp-days .day-card")).to_have_count(3)
        srv.nws.fail = False
        import nws_fixture
        srv.nws.alerts.append(nws_fixture.ALERT)
        asyncio.run_coroutine_threadsafe(srv.hub.weather.refresh(), srv.loop).result(5)
        expect(page.locator("#wp-alerts .alert")).to_contain_text("Flash Flood Warning")
        page.locator("#wp-alerts .alert").click()
        expect(page.locator("#wp-alerts .alert")).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#wp-alerts .txt")).to_contain_text("higher ground")
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_pager_decoder_validation_filters_pause_and_safe_text(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        open_app(srv, page)
        page.click("#nav-pager")
        page.fill("#pager-mhz", "929.6125oops")
        page.click("#pager-toggle")
        assert not srv.job_cmds
        assert not page.locator("#pager-mhz").evaluate("e => e.checkValidity()")
        page.fill("#pager-mhz", "929.6125")
        page.click("#pager-toggle")
        expect(page.locator("#pg-status")).to_contain_text("running on 929.6125")
        srv.loop.call_soon_threadsafe(srv.job.push, "POCSAG1200: Address: 123456 Function: 3 Alpha: <img src=x onerror=window.__pwned=1>")
        expect(page.locator("#pg-feed .pmsg")).to_have_count(1)
        expect(page.locator("#pg-feed")).to_contain_text("<img src=x")
        assert page.locator("#pg-feed img").count() == 0
        assert page.evaluate("window.__pwned") is None
        page.click("#pg-pause")
        srv.loop.call_soon_threadsafe(srv.job.push, "POCSAG1200: Address: 654321 Function: 3 Alpha: Test dispatch")
        expect(page.locator("#pg-count")).to_contain_text("2 decoded")
        expect(page.locator("#pg-feed .pmsg")).to_have_count(1)
        page.click("#pg-pause")
        expect(page.locator("#pg-feed .pmsg")).to_have_count(2)
        page.locator("#pg-feed .addr").first.click()
        expect(page.locator("#pg-feed .pmsg")).to_have_count(1)
        page.click("#pg-clear")
        expect(page.locator("#pg-feed .pmsg")).to_have_count(2)
        page.fill("#pg-q", "no matches")
        expect(page.locator("#pg-feed")).to_contain_text("No messages match")
        page.click("#pager-toggle")
        expect(page.locator("#pg-status")).to_contain_text("stopped")
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_sensors_page_shows_real_readings_and_custom_frequency(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        open_app(srv, page)
        page.click("#nav-sensors")
        page.fill("#sensors-mhz", "915")
        page.click("#sensors-toggle")
        expect(page.locator("#sn-status")).to_contain_text("running on 915")
        expect(page.locator("#sn-grid")).to_contain_text("Listening on 915 MHz")
        srv.loop.call_soon_threadsafe(srv.job.push, json.dumps({"model": "Garden thermometer", "id": 7, "temperature_C": 0, "humidity": 48, "battery_ok": 0}))
        expect(page.locator("#sn-grid .dev")).to_have_count(1)
        expect(page.locator("#sn-grid .temp")).to_contain_text("32")
        expect(page.locator("#sn-grid")).to_contain_text("low battery")
        srv.loop.call_soon_threadsafe(srv.job.push, json.dumps({"model": "TPMS", "id": "private", "temperature_C": 20}))
        expect(page.locator("#sn-grid")).to_contain_text("1 transmissions")
        expect(page.locator("#sn-grid")).not_to_contain_text("private")
        page.click("#nav-weather")
        expect(page.locator("#wp-433")).to_contain_text("Garden thermometer")
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_satellite_page_loads_on_direct_navigation_and_stops_polling(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        page.goto(f"{srv.url}/?token={TOKEN}#/sky")
        expect(page.locator("#sk-list")).to_contain_text("ISS", timeout=15000)
        expect(page.locator("#sk-loc")).to_contain_text("30.270")
        expect(page.locator("#sk-plot svg")).to_have_count(1)
        timer = page.evaluate("SK.timer")
        page.click("#nav-sky")
        assert page.evaluate("SK.timer") == timer
        page.click("#nav-weather")
        assert page.evaluate("SK.timer") == 0
        page.go_back()
        expect(page.locator("#page-sky")).to_be_visible()
        assert page.evaluate("SK.timer") != 0
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_messages_stay_unread_when_the_conversation_is_on_a_hidden_page(srv, page):
    open_app(srv, page)
    page.click('#node-list li[data-id="!02ecdcdc"]')
    page.click("#btn-message")
    page.click("#nav-weather")
    srv.radio.on_packet(packet("!02ecdcdc", "Remember to read this"))
    expect(page.locator("#nav-badge-chats")).to_have_text("1")
    page.click("#nav-chats")
    expect(page.locator("#thread")).to_contain_text("Remember to read this")
    expect(page.locator("#nav-badge-chats")).to_be_hidden()


def test_page_navigation_and_new_pages_fit_mobile(srv, page):
    page.set_viewport_size({"width": 390, "height": 844})
    open_app(srv, page)
    for name in ("nodes", "chats", "air", "radio", "weather", "pager", "sensors", "sky"):
        page.click(f"#nav-{name}")
        expect(page.locator(f"#page-{name}")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
        if name in ("air", "radio", "sky"):
            selector = f"#{'page-' + name} .{'air' if name == 'air' else 'radio' if name == 'radio' else 'sky'}-page"
            assert page.locator(selector).evaluate("e => e.scrollWidth <= e.clientWidth + 1"), name
        if name == "pager":
            assert page.locator(".decoder-page").evaluate("e => getComputedStyle(e).gridTemplateColumns.split(' ').length") == 1
    page.locator("#nav-sky").focus()
    page.keyboard.press("Home")
    expect(page.locator("#nav-dashboard")).to_be_focused()
    page.keyboard.press("End")
    expect(page.locator("#nav-sky")).to_be_focused()
    assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_delivery_status_survives_reload(srv, page):
    open_app(srv, page)
    page.click('#node-list li[data-id="!02ecdcdc"]')
    page.click("#btn-message")
    page.fill("#msg-input", "Delivery should persist")
    page.click("#btn-send")
    expect(page.locator("#thread .msg.out")).to_contain_text("delivered")
    page.reload()
    expect(page.locator("#status")).to_have_text("connected")
    page.click('#node-list li[data-id="!02ecdcdc"]')
    page.click("#btn-message")
    expect(page.locator("#thread .msg.out")).to_contain_text("delivered")


def test_demo_banner_and_messages_never_claim_transmission(tmp_path, page):
    with LiveServer(tmp_path, demo=True) as srv:
        open_app(srv, page, wait=False)
        expect(page.locator("#status")).to_have_text("demo")
        expect(page.locator("#banner")).to_contain_text("messages are simulated")
        page.click('#node-list li[data-id="!02ecdcdc"]')
        page.click("#btn-message")
        page.fill("#msg-input", "Demo message")
        page.click("#btn-send")
        expect(page.locator("#thread .msg.out")).to_contain_text("simulated · not transmitted")
        expect(page.locator("#thread .msg.out")).not_to_contain_text("delivered")


def test_each_sdr_action_uses_its_own_capability(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        capabilities = {name: {"available": name == "sensors", "error": None if name == "sensors" else f"Missing {name} tools"}
                        for name in ("survey", "audio", "aircraft", "pager", "sensors")}
        srv.hub.radio.capabilities = lambda: capabilities
        open_app(srv, page)
        page.click("#nav-sensors")
        expect(page.locator("#sensors-toggle")).to_be_enabled()
        page.click("#nav-pager")
        expect(page.locator("#pager-toggle")).to_be_disabled()
        expect(page.locator("#pg-status")).to_contain_text("Missing pager tools")
        page.click("#nav-radio")
        expect(page.locator("#rp-toggle")).to_be_disabled()
        expect(page.locator("#rp-listen")).to_be_disabled()
        expect(page.locator("#rp-now")).to_contain_text("Missing audio tools")
        page.click("#nav-air")
        expect(page.locator("#ap-toggle")).to_be_disabled()
        expect(page.locator("#ap-status")).to_contain_text("Missing aircraft tools")
