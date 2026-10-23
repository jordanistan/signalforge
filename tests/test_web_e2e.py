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
    assert h["referrer-policy"] == "no-referrer"
    assert re.search(r'src="/assets/pages\.js\?v=[0-9a-f]{12}"', page.content())  # updates bypass stale caches
    r = page.goto(f"{srv.url}/assets/pages.js")
    assert r.headers["cache-control"] == "no-cache"  # an updated UI is never hidden behind a stale cached copy


def test_osm_tiles_send_only_the_origin_as_referer(srv, ctx):
    seen = []
    page = ctx.new_page()
    page.on("request", lambda r: seen.append(r.headers.get("referer")) if "tile.openstreetmap.org" in r.url else None)
    page.add_init_script("try{localStorage.setItem('mesh.base','Streets')}catch(e){}")  # satellite is the default
    page.goto(f"{srv.url}/?token={TOKEN}")
    page.click("#view-map")
    page.wait_for_function("document.querySelectorAll('#map .leaflet-tile').length > 0")
    assert seen and all(ref == f"{srv.url}/" for ref in seen), seen  # never the ?token= URL, never missing


def test_debug_ui_reports_clicks_pages_and_errors_without_the_token(tmp_path, ctx):
    with LiveServer(tmp_path) as srv:
        page = ctx.new_page()
        page.goto(f"{srv.url}/?token={TOKEN}")
        expect(page.locator("#status")).to_have_text("connected")
        page.click("#nav-nodes")
        time.sleep(0.5)
        assert not [line for line in srv.out if line.startswith("UIDEBUG")]  # off unless --debug-ui

        srv.hub.debug_ui_path = str(tmp_path / "ui-debug.jsonl")
        page.reload()
        expect(page.locator("#status")).to_have_text("connected")
        page.click("#nav-nodes")
        page.evaluate("setTimeout(() => { throw new Error('boom <x>') })")
        lines = lambda: [line for line in srv.out if line.startswith("UIDEBUG")]
        deadline = time.time() + 8
        while time.time() < deadline and not any('"ev":"page"' in x and '"page":"nodes"' in x for x in lines()):
            time.sleep(0.2)
        text = "\n".join(lines())
        assert '"ev":"click"' in text and '"id":"nav-nodes"' in text
        assert '"ev":"js-error"' in text and "boom" in text
        assert '"newNodesPage":true' in text and '"nodesMap":{"present":true' in text
        assert TOKEN not in text and TOKEN not in (tmp_path / "ui-debug.jsonl").read_text()


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


def test_nodes_page_radar_map_shows_rings_reach_and_side_list(srv, page):
    srv.radio._nodes["!66666666"] = node("Near", "Near Direct", NOW - 120, 30.28, -97.75, hops=0, snr=3.0)  # ~1.2 km north
    open_app(srv, page)
    page.click("#nav-nodes")
    expect(page.locator("#np-map .leaflet-tile-pane img").first).to_be_attached()
    assert "arcgisonline" in page.locator("#np-map .leaflet-tile-pane img").first.get_attribute("src")  # satellite by default
    expect(page.locator("#np-map .nm-ring").first).to_be_attached()
    expect(page.locator("#np-map .nm-reach")).to_have_count(1)
    expect(page.locator("#np-map .nm-link.good")).to_have_count(1)
    expect(page.locator("#np-map .nm-radar")).not_to_have_class(re.compile("hidden"))
    expect(page.locator("#np-legend")).to_contain_text("Direct reach 1.2 km")
    expect(page.locator("#np-map .node-label", has_text=EVIL_SHORT)).to_have_count(1)  # hostile names stay text
    first = page.locator("#np-body tr.row").first
    expect(first).to_have_attribute("data-id", "!66666666")  # sorted nearest first
    first.click()
    expect(page.locator("#detail")).to_contain_text("Near Direct")
    assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_every_map_carries_the_radar_overlay_and_the_command_center_has_map_widgets(srv, page):
    open_app(srv, page, view="command")
    page.click("#nav-dashboard")
    page.click("#view-cmd")
    for card in ("#card-meshmap", "#card-air"):
        expect(page.locator(f"{card} .nm-radar")).not_to_have_class(re.compile("hidden"))
        expect(page.locator(f"{card} .nm-ring").first).to_be_attached()
    expect(page.locator("#card-meshmap .leaflet-interactive.node-marker").first).to_be_attached()
    page.click("#view-map")
    expect(page.locator("#map .nm-radar")).not_to_have_class(re.compile("hidden"))
    for name, sel in (("nodes", "#np-map"), ("air", "#page-air"), ("sky", "#sky-map")):
        page.click(f"#nav-{name}")
        expect(page.locator(f"{sel} .nm-radar")).not_to_have_class(re.compile("hidden"))
        expect(page.locator(f"{sel} .nm-ring").first).to_be_attached()
    assert not [m for m in page.console_msgs if m[0] == "pageerror"]


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
        assert page.locator("#command img:not(.leaflet-tile)").count() == 0  # map-widget tiles are the only images
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
        expect(page.locator("#card-air .plane")).to_have_count(1, timeout=6000)  # plane marker on the radar map
        expect(page.locator("#card-air .nm-radar")).not_to_have_class(re.compile("hidden"))
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
    page.click("#nav-sensors")
    expect(page.locator("#sensors-toggle")).to_be_disabled()
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


def test_there_is_no_pager_page(srv, page):
    open_app(srv, page)
    assert page.locator("#nav-pager").count() == 0
    page.goto(f"{srv.url}/#/pager")
    expect(page.locator("#layout")).to_be_visible()  # unknown pages fall back to the Command Center / map


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


def test_sensors_default_to_auto_scan_and_the_command_center_summarises_them(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        open_app(srv, page, view="command")
        page.click("#nav-dashboard")
        page.click("#view-cmd")
        expect(page.locator("#card-sensors")).to_contain_text("Sensor scan is off")
        page.click("#cmd-sensors-btn")
        expect(page.locator("#card-sensors")).to_contain_text("433.92 / 315 / 915 MHz (auto)")
        srv.loop.call_soon_threadsafe(srv.job.push, json.dumps({"model": "Acurite-Tower", "id": 9, "temperature_C": 22, "humidity": 61}))
        expect(page.locator("#card-sensors .crow")).to_contain_text("Acurite-Tower")
        expect(page.locator("#card-sensors")).to_contain_text("72°F")
        page.click("#nav-sensors")
        expect(page.locator("#sn-status")).to_contain_text("running on 433.92 / 315 / 915 MHz (auto)")
        assert page.locator("#sensors-mhz").input_value() == "auto"
        page.click("#sensors-toggle")
        expect(page.locator("#sn-status")).to_contain_text("idle")
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
    for name in ("nodes", "chats", "air", "radio", "weather", "sensors", "sky"):
        page.click(f"#nav-{name}")
        expect(page.locator(f"#page-{name}")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
        if name in ("air", "radio", "sky"):
            selector = f"#{'page-' + name} .{'air' if name == 'air' else 'radio' if name == 'radio' else 'sky'}-page"
            assert page.locator(selector).evaluate("e => e.scrollWidth <= e.clientWidth + 1"), name
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
                        for name in ("survey", "audio", "aircraft", "sensors")}
        srv.hub.radio.capabilities = lambda: capabilities
        open_app(srv, page)
        page.click("#nav-sensors")
        expect(page.locator("#sensors-toggle")).to_be_enabled()
        page.click("#nav-radio")
        expect(page.locator("#rp-toggle")).to_be_disabled()
        expect(page.locator("#rp-listen")).to_be_disabled()
        expect(page.locator("#rp-now")).to_contain_text("Missing audio tools")
        page.click("#nav-air")
        expect(page.locator("#ap-toggle")).to_be_disabled()
        expect(page.locator("#ap-status")).to_contain_text("Missing aircraft tools")


# ---- UI polish: layout at every width, radar labels, satellite maps, radio rows, weather extras ------------
def test_no_page_is_wider_than_the_window_at_tablet_widths(srv, page):
    for width in (1100, 800):
        page.set_viewport_size({"width": width, "height": 900})
        open_app(srv, page)
        for name in ("dashboard", "nodes", "chats", "air", "radio", "weather", "sensors", "sky"):
            page.click(f"#nav-{name}")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, name)
            assert page.evaluate("document.documentElement.scrollLeft === 0 && document.body.scrollLeft === 0"), (width, name)
        page.click("#nav-dashboard")
        page.click("#view-map")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, "map")
        box = page.locator("#view-cmd").bounding_box()
        assert box["height"] < 40, box  # the view switch never wraps onto two lines
    assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_every_map_defaults_to_satellite_and_dark_mode_never_inverts_imagery(srv, page):
    page.add_init_script("try{localStorage.setItem('mesh.theme','dark')}catch(e){}")
    open_app(srv, page)
    expect(page.locator("#map .leaflet-tile-pane img").first).to_be_attached()
    assert "arcgisonline" in page.locator("#map .leaflet-tile-pane img").first.get_attribute("src")
    for sel in ("#map", ):
        filt = page.locator(f"{sel} .leaflet-tile-pane .leaflet-layer").first.evaluate("e => getComputedStyle(e).filter + getComputedStyle(e.parentElement).filter")
        assert "invert" not in filt, filt
    page.hover("#map .leaflet-control-layers")
    page.locator("#map .leaflet-control-layers-base label", has_text="Streets").click()
    expect(page.locator("#map .tiles-street")).to_have_count(1)
    assert "invert" in page.locator("#map .tiles-street").evaluate("e => getComputedStyle(e).filter")  # streets get the dark look


def test_ring_labels_are_thinned_when_rings_are_small(srv, page):
    open_app(srv, page)
    page.click("#nav-nodes")
    expect(page.locator("#np-map .nm-ring-label").first).to_be_attached()
    page.evaluate("NM.map.setView(myPos(), 11, { animate: false })")  # km-sized rings are only a few px apart here
    page.wait_for_timeout(200)
    visible = page.evaluate("""() => [...document.querySelectorAll('#np-map .nm-ring-label:not(.reach)')]
        .filter(e => getComputedStyle(e).opacity !== '0').map(e => e.getBoundingClientRect())""")
    for a, b in zip(visible, visible[1:]):
        assert abs(a["y"] - b["y"]) >= 20 or abs(a["x"] - b["x"]) >= 60, visible  # shown labels never stack
    page.evaluate("NM.map.setView(myPos(), 16, { animate: false })")
    page.wait_for_timeout(200)
    shown = page.evaluate("[...document.querySelectorAll('#np-map .nm-ring-label:not(.reach)')].filter(e => getComputedStyle(e).opacity !== '0').length")
    assert shown >= 2  # zoomed in, the rings are far apart and keep their labels


def test_satellites_page_uses_the_shared_bases(srv, page):
    open_app(srv, page)
    page.click("#nav-sky")
    expect(page.locator("#sky-map .leaflet-tile-pane img").first).to_be_attached()
    assert "arcgisonline" in page.locator("#sky-map .leaflet-tile-pane img").first.get_attribute("src")
    page.hover("#sky-map .leaflet-control-layers")
    labels = page.locator("#sky-map .leaflet-control-layers-base label").all_inner_texts()
    assert [t.strip() for t in labels] == ["Satellite", "Streets", "Offline vector"], labels
    page.locator("#sky-map .leaflet-control-layers-base label", has_text="Streets").click()
    assert page.evaluate("localStorage.getItem('mesh.sky.base')") == "Streets"
    expect(page.locator("#sky-map .nm-ring").first).to_be_attached()  # the radar rings stay
    expect(page.locator("#sk-plot")).to_contain_text("orbital data")  # empty boxes explain themselves
    expect(page.locator("#sk-pbody")).to_contain_text("orbital data")


def test_clicking_a_survey_row_tunes_it_and_explains_when_it_cannot(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        caps = {name: {"available": True, "error": None} for name in ("survey", "audio", "aircraft", "sensors")}
        srv.hub.radio.capabilities = lambda: caps
        heard = []
        page.route(re.compile(r".*/api/radio/audio.*"), lambda r: (heard.append(r.request.url), r.fulfill(status=204)))
        open_app(srv, page)
        page.click("#nav-radio")
        page.click("#rp-toggle")
        row = page.locator("#rp-body tr.row", has_text="162.4000")
        expect(row).to_be_visible(timeout=6000)
        expect(row).to_have_css("cursor", "pointer")
        row.locator("td").first.click()  # the frequency text, not the button
        expect(page.locator("#rp-mhz")).to_have_value("162.4")
        page.wait_for_timeout(300)
        assert heard and "mhz=162.4" in heard[-1], heard
        n = len(heard)
        page.locator("#rp-mhz").fill("")
        row.focus()
        page.keyboard.press("Enter")  # keyboard works too
        expect(page.locator("#rp-mhz")).to_have_value("162.4")
        page.wait_for_timeout(300)
        assert len(heard) > n
        caps["audio"] = {"available": False, "error": "Missing audio tools"}
        page.reload()
        page.click("#nav-radio")
        expect(row).to_be_visible(timeout=6000)
        page.locator("#rp-mhz").fill("")
        row.locator("td").nth(1).click()
        expect(page.locator("#rp-mhz")).to_have_value("162.4")
        expect(page.locator(".toast.error")).to_contain_text("Missing audio tools")
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_weather_radar_pops_out_and_closes_with_focus_returned(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        open_app(srv, page)
        page.click("#nav-weather")
        expect(page.locator("#wp-radar-cap")).to_contain_text("KGRK")
        page.click("#wp-radar")
        modal = page.locator("#wp-radar-modal")
        expect(modal).to_be_visible()
        expect(modal.locator("[role=dialog]")).to_have_attribute("aria-modal", "true")
        expect(modal).to_contain_text("NWS radar KGRK")
        expect(modal).to_contain_text("Loop of the last hour")
        assert page.locator("#wp-radar-big").get_attribute("src") == page.locator("#wp-radar").get_attribute("src")
        big = page.locator("#wp-radar-big").bounding_box()
        assert big["width"] > 900, big  # enlarged, near the window size
        expect(page.locator("#wp-radar-close")).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.locator("#wp-radar-close")).to_be_focused()  # focus stays in the dialog
        page.keyboard.press("Escape")
        expect(modal).to_have_count(0)
        expect(page.locator("#wp-radar")).to_be_focused()  # focus goes back to what opened it
        page.keyboard.press("Enter")  # the image is a keyboard control too
        expect(modal).to_be_visible()
        page.mouse.click(5, 5)  # the backdrop closes it
        expect(modal).to_have_count(0)
        page.click("#wp-radar-open")
        expect(modal).to_be_visible()
        page.click("#wp-radar-close")
        expect(modal).to_have_count(0)
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_weather_handheld_box_shows_live_bridge_and_follow_status(tmp_path, page):
    with LiveServer(tmp_path, bridge=True, allow=["!02ecdcdc"]) as srv:
        open_app(srv, page)
        page.click("#nav-weather")
        expect(page.locator("#wp-bridge")).to_have_text("Bridge: on · handheld dcdc can use it")
        box = page.locator("#wp-handheld")
        expect(box).to_contain_text("follow weather austin")
        expect(box).to_contain_text("every 15 minutes")
        expect(box).to_contain_text("6 hours")
        expect(box).to_contain_text("does not text them")
        srv.radio.on_packet(packet("!02ecdcdc", "follow weather austin"))
        expect(box.locator(".hh-follow")).to_contain_text("following “weather austin”")
    with LiveServer(tmp_path / "off") as srv:
        open_app(srv, page)
        page.click("#nav-weather")
        expect(page.locator("#wp-bridge")).to_have_text("Bridge: off")
        expect(page.locator("#wp-err .note")).to_be_visible()  # a switched-off service is a note, not a red error
        expect(page.locator("#wp-err .err-note")).to_have_count(0)


def test_air_page_names_the_receiver_and_flags_noise_only(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        srv.hub.radio.capabilities = lambda: {n: {"available": True, "error": None} for n in ("survey", "audio", "aircraft", "sensors")}
        open_app(srv, page)
        page.click("#nav-air")
        page.evaluate("() => { S.air.receiver = 'readsb'; S.air.stats = { frames: 400, accepted: 0 }; airPage.render(); }")
        expect(page.locator("#ap-status")).to_contain_text("via readsb")
        expect(page.locator("#ap-noise")).to_contain_text("Hearing only noise so far")
        page.evaluate("() => { S.air.stats = { frames: 400, accepted: 12 }; airPage.render(); }")
        expect(page.locator("#ap-noise")).to_have_count(0)
        expect(page.locator("#ap-kpis")).not_to_contain_text("null")
        page.evaluate("() => { delete S.air.receiver; delete S.air.stats; airPage.render(); }")  # older servers
        expect(page.locator("#ap-status")).not_to_contain_text("via")
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_reduced_motion_stops_the_sweep_but_keeps_targets_visible(srv, browser):
    c = browser.new_context(viewport={"width": 1500, "height": 900}, reduced_motion="reduce")
    c.route(re.compile(r"^(?!http://127\.0\.0\.1).*"), lambda r: r.fulfill(status=200, content_type="image/png", body=BLANK_PNG))
    page = c.new_page()
    open_app(srv, page)
    page.click("#nav-nodes")
    expect(page.locator("#np-map .nm-blip i").first).to_be_attached()
    assert page.locator("#np-map .nm-sweep").evaluate("e => getComputedStyle(e).animationName") == "none"
    assert float(page.locator("#np-map .nm-blip i").first.evaluate("e => getComputedStyle(e).opacity")) > 0.5
    c.close()


def test_radar_animations_stay_phase_locked_across_rerenders_and_page_switches(srv, page):
    open_app(srv, page, view="command")
    sweep = page.locator("#card-meshmap .nm-sweep")
    expect(sweep).to_be_attached()
    page.wait_for_timeout(400)
    sweep.evaluate("e => { e.__tag = 1; }")
    page.wait_for_timeout(2300)  # the Command Center re-renders every second
    assert sweep.evaluate("e => e.__tag") == 1  # the map card was not rebuilt
    lock = "e => e.getAnimations().map(a => a.startTime)"
    assert sweep.evaluate(lock) == [0]
    page.click("#nav-nodes")
    blip = page.locator("#np-map .nm-blip i").first
    expect(blip).to_be_attached()
    page.wait_for_timeout(300)
    assert blip.evaluate(lock) == [0] and page.locator("#np-map .nm-sweep").evaluate(lock) == [0]
    delay = float(blip.evaluate("e => parseFloat(e.style.animationDelay)"))
    assert 0 <= delay < 6  # the time the beam takes to reach the node's bearing
    page.click("#nav-dashboard")  # hidden and shown again: CSS restarts the animation, the lock re-pins it
    page.wait_for_timeout(300)
    assert sweep.evaluate(lock) == [0]
    assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_one_far_node_does_not_set_the_ring_scale(srv, page):
    srv.radio._nodes["!77777777"] = node("Far", "Far Away", NOW - 60, 39.0, -97.75, hops=3, snr=-12.0)  # ~970 km north
    open_app(srv, page)
    expect(marker(page, "!77777777")).to_have_count(1)
    outer = page.evaluate("meshRadarModel().outer")
    assert outer <= 20, outer  # rings fit the local nodes (a few km), not the outlier
    page.click("#nav-nodes")
    expect(page.locator("#np-body tr.row[data-id='!77777777']")).to_have_count(1)  # still listed


def test_satellite_ring_labels_show_at_the_default_view_and_the_beam_is_faint(srv, page):
    page.set_viewport_size({"width": 1890, "height": 1300})
    page.add_init_script("try{localStorage.setItem('mesh.view','map')}catch(e){}")
    page.goto(f"{srv.url}/?token={TOKEN}#/sky")  # before the state (and your position) has arrived
    labels = page.locator("#sky-map .nm-ring-label")
    expect(labels).to_have_count(3)
    page.wait_for_timeout(500)
    assert all(v == "1" for v in labels.evaluate_all("els => els.map(e => getComputedStyle(e).opacity)"))
    expect(page.locator("#sky-map .nm-ring-label span", has_text="ISS above horizon")).to_be_visible()
    expect(page.locator("#sky-map .nm-radar")).to_have_class(re.compile("wide"))
    page.click("#nav-nodes")
    expect(page.locator("#np-map .nm-radar")).not_to_have_class(re.compile("wide"))


def test_a_program_that_loses_the_dongle_says_so_and_can_resume(tmp_path, page):
    with LiveServer(tmp_path, sdr=True) as srv:
        srv.hub.radio.capabilities = lambda: {n: {"available": True, "error": None} for n in ("survey", "audio", "aircraft", "sensors")}
        open_app(srv, page)
        page.click("#nav-sensors")
        page.click("#sensors-toggle")
        expect(page.locator("#sensors-toggle")).to_have_text("■ Stop", timeout=5000)
        page.click("#nav-air")
        expect(page.locator("#ap-toggle")).to_have_attribute("title", "Starting this stops sensor reading.")
        page.click("#ap-toggle")
        expect(page.locator("#ap-toggle")).to_have_text("■ Stop tracking", timeout=5000)
        expect(page.locator("#sdr-lost-air")).to_have_count(0)
        page.click("#nav-sensors")
        note = page.locator("#sdr-lost-sensors")
        expect(note).to_contain_text("Stopped: the dongle was switched to aircraft tracking at")
        expect(page.locator("#sensors-toggle")).to_have_attribute("title", "Starting this stops aircraft tracking.")
        note.get_by_role("button", name="▶ Resume").click()
        expect(page.locator("#sensors-toggle")).to_have_text("■ Stop", timeout=5000)
        expect(note).to_have_count(0)
        page.click("#nav-air")
        expect(page.locator("#sdr-lost-air")).to_contain_text("switched to sensor reading")
        page.locator("#sdr-lost-air").get_by_role("button", name="Dismiss").click()
        expect(page.locator("#sdr-lost-air")).to_have_count(0)
        assert not [m for m in page.console_msgs if m[0] == "pageerror"]


def test_radar_rings_use_the_radar_colour_not_the_leaflet_default(srv, page):
    open_app(srv, page)
    ring = page.locator("#map .nm-ring").first
    expect(ring).to_be_attached()
    assert ring.evaluate("e => getComputedStyle(e).stroke") == "rgba(90, 255, 150, 0.75)"
