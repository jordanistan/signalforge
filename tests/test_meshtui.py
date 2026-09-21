import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import meshtui  # noqa: E402
BROADCAST = meshtui.BROADCAST
from textual.widgets import Button, DataTable, Input, RichLog, TabbedContent  # noqa: E402

NOW = time.time()


class FakeBackend:
    my_id = "!me"

    def __init__(self, on_text):
        self.on_text = on_text
        self.sent = []
        self._nodes = {
            "!me": {"user": {"id": "!me", "shortName": "me"}, "position": {"latitude": 30.27, "longitude": -97.77}},
            "!near": {"user": {"id": "!near", "shortName": "near"}, "lastHeard": NOW - 30, "hopsAway": 0, "snr": 7.0,
                      "deviceMetrics": {"batteryLevel": 62}, "position": {"latitude": 30.28, "longitude": -97.77}},
            "!far": {"user": {"id": "!far", "shortName": "far"}, "lastHeard": NOW - 600, "hopsAway": 3},
            "!old": {"user": {"id": "!old", "shortName": "old"}, "lastHeard": NOW - 86400, "hopsAway": 1},
        }

    def nodes(self):
        return dict(self._nodes)

    def send_text(self, text, dest, on_ack=None):
        self.sent.append((text, dest))
        if on_ack and dest != meshtui.BROADCAST:  # like SerialBackend: only DMs request an ACK
            on_ack({"decoded": {"routing": {"errorReason": "NONE"}}})

    def close(self):
        pass


def make_app(tmp_path):
    holder = {}

    def factory(on_text):
        holder["b"] = FakeBackend(on_text)
        return holder["b"]

    return meshtui.MeshTUI(factory, log_path=str(tmp_path / "messages.jsonl"), basemap=None), holder


def board_text(app):
    return "\n".join(line.text for line in app.query_one("#thread", RichLog).lines)


async def test_filters(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        table = app.query_one("#nodes", DataTable)
        assert {k.value for k in table.rows} == {"!near", "!far", "!old"}  # default shows every known node
        await pilot.press("f2")  # recent
        assert {k.value for k in table.rows} == {"!near", "!far"}  # self and 1-day-old node hidden
        await pilot.press("f2")  # direct
        assert {k.value for k in table.rows} == {"!near"}
        await pilot.press("f2")  # back to all
        assert {k.value for k in table.rows} == {"!near", "!far", "!old"}


def test_haversine_and_age():
    assert abs(meshtui.haversine_km((30.0, -97.0), (31.0, -97.0)) - 111.2) < 0.5
    assert meshtui.age(NOW - 90, NOW) == "1m" and meshtui.age(None) == "-"


def _rows(be):
    return [(k, v) for k, v in be.nodes().items() if k != "!me"]


def test_radar_places_positioned_node_by_bearing():
    be = FakeBackend(lambda p: None)
    text = meshtui.render_radar(60, 21, "!me", be.nodes(), _rows(be), sweep=3.0).plain
    lines = text.split("\n")
    assert len(lines) == 21 and all(len(line) == 60 for line in lines)
    center_row = next(i for i, line in enumerate(lines) if "✚" in line)
    near_row = next(i for i, line in enumerate(lines) if "near" in line)
    assert near_row < center_row  # 'near' is due north of 'me'
    assert "far" in text  # heard 10 min ago: labelled, plotted on a signal ring
    assert "old" not in text and "•" in text  # remembered for a day+: dim unlabelled dot
    assert lines[0].count("N") == 1


def test_radar_selected_marker_and_small_window():
    be = FakeBackend(lambda p: None)
    text = meshtui.render_radar(60, 21, "!me", be.nodes(), _rows(be), 0.0, selected="!near").plain
    assert "◉" in text
    assert "bigger window" in meshtui.render_radar(10, 5, "!me", {}, [], 0.0).plain


def test_bearing_cardinals():
    assert abs(meshtui.bearing((30.0, -97.0), (31.0, -97.0))) < 1e-6  # north
    assert abs(meshtui.bearing((30.0, -97.0), (30.0, -96.0)) - 1.5708) < 0.01  # east


def test_signal_bars_and_gauge():
    assert meshtui.signal_bars(7.0).plain == "▂▄▆█"
    assert meshtui.signal_bars(-3.0).plain == "▂▄··"
    assert meshtui.signal_bars(-20.0).plain == "····"
    assert meshtui.signal_bars(None).plain == "····"
    assert meshtui.gauge(50, 10).plain.startswith("█████░░░░░ 50%")
    assert meshtui.gauge(150, 4).plain == "████ 100%"  # clamped


def test_signal_radius_orders_by_strength():
    strong = meshtui.signal_radius({"snr": 10, "hopsAway": 0})
    weak = meshtui.signal_radius({"snr": -20, "hopsAway": 0})
    relayed = meshtui.signal_radius({"hopsAway": 3})
    assert strong < weak < relayed <= 0.95


def test_render_me_and_detail():
    me = {"user": {"id": "!me", "longName": "Me Node", "hwModel": "HELTEC_V3"},
          "deviceMetrics": {"batteryLevel": 62, "voltage": 3.82, "channelUtilization": 12.0,
                            "airUtilTx": 1.0, "uptimeSeconds": 482}}
    t = meshtui.render_me(me, total=3, direct=1, n_in=2, n_out=1).plain
    for want in ("Me Node", "62%", "3.82 V", "8m", "Heard 3 nodes (1 direct)", "↓2", "↑1"):
        assert want in t
    assert "USB powered" in meshtui.render_me({"deviceMetrics": {"batteryLevel": 101}}, 0, 0, 0, 0).plain
    be = FakeBackend(lambda p: None)
    d = meshtui.render_detail("!near", be.nodes()["!near"], (30.27, -97.77)).plain
    assert "near" in d and "7.0 dB" in d and "km" in d
    assert "Select a node" in meshtui.render_detail(None, None, None).plain


async def test_theme_cycle_and_snr_history(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        assert app.theme == meshtui.THEMES[0]
        await pilot.press("f4")
        assert app.theme == meshtui.THEMES[1]
        app.refresh_nodes()
        assert list(app.history["!near"]) == [7.0, 7.0]  # once on mount, once now
        assert all(t in app.available_themes for t in meshtui.THEMES)


async def test_selection_updates_detail_and_radar(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.click("#nodes")
        await pilot.press("down")
        await pilot.pause()
        assert app.selected == "!far"
        assert app.query_one("#radar").selected == "!far"



async def thread_of(app, pilot, key):
    """Plain text of one conversation, as the thread pane shows it once the tab has been laid out."""
    app.open_conv(key)
    await pilot.pause()
    return board_text(app)


async def test_incoming_dm_goes_to_its_own_thread_with_unread(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.add_message({"ts": NOW, "from": "!near", "to": "!me", "text": "hello [red]x[/red]", "dir": "in"})
        app.add_message({"ts": NOW, "from": "!far", "to": "^all", "text": "to everyone", "dir": "in"})
        await pilot.pause()
        assert app.unread == {"!near": 1, BROADCAST: 1}
        assert app.query_one(TabbedContent).get_tab("messages").label.plain == "Messages (2)"
        assert app.query_one("#radar").unread == {"!near"}  # channel unread has no node to mark
        assert "*near" in meshtui.render_radar(60, 21, "!me", app.backend.nodes(), app.visible_nodes(app.backend.nodes()),
                                               0.0, unread={"!near"}).plain
        dm = await thread_of(app, pilot, "!near")
        assert "hello [red]x[/red]" in dm and "to everyone" not in dm  # markup escaped, threads separate
        assert app.unread == {BROADCAST: 1}  # opening a thread marks only that one read
        chan = await thread_of(app, pilot, BROADCAST)
        assert "to everyone" in chan and "far" in chan and "hello" not in chan
        assert app.unread == {}
        assert app.query_one(TabbedContent).get_tab("messages").label.plain == "Messages"
    saved = [json.loads(line) for line in (tmp_path / "messages.jsonl").read_text().splitlines()]
    assert [r["text"] for r in saved] == ["hello [red]x[/red]", "to everyone"]


async def test_incoming_to_open_thread_is_not_unread(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_conv("!near")
        await pilot.pause()
        app.add_message({"ts": NOW, "from": "!near", "to": "!me", "text": "live", "dir": "in"})
        await pilot.pause()
        assert app.unread == {}
        assert "live" in board_text(app)


async def test_history_rebuilds_separate_conversations(tmp_path):
    lines = [
        {"ts": NOW, "from": "!near", "to": "!me", "text": "dm from near", "dir": "in"},
        {"ts": NOW, "from": "!me", "to": "!far", "text": "dm to far", "dir": "out"},
        {"ts": NOW, "from": "!far", "to": "^all", "text": "channel chatter", "dir": "in"},
        {"ts": NOW, "from": "!me", "to": "^all", "text": "my broadcast", "dir": "out"},
    ]
    (tmp_path / "messages.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        assert set(app.convs) == {BROADCAST, "!near", "!far"}
        assert app.unread == {}  # history never counts as unread
        assert "dm from near" in await thread_of(app, pilot, "!near") and "dm to far" not in board_text(app)
        assert "dm to far" in await thread_of(app, pilot, "!far")
        chan = await thread_of(app, pilot, BROADCAST)
        assert "channel chatter" in chan and "my broadcast" in chan and "dm" not in chan


async def test_dm_two_nodes_and_channel_stay_separate(tmp_path):
    app, holder = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.click("#nodes")  # cursor on first row: !near (most recently heard)
        await pilot.press("enter")
        await pilot.pause()
        assert app.current == "!near" and app.query_one(TabbedContent).active == "messages"
        await pilot.press(*"alpha", "enter")
        await pilot.pause(0.2)
        app.open_conv("!far")
        await pilot.pause()
        await pilot.press(*"bravo", "enter")
        await pilot.pause(0.2)
        await pilot.press("f3")
        await pilot.pause()
        assert app.current == BROADCAST
        await pilot.press(*"gamma", "enter")
        await pilot.pause(0.2)
        assert holder["b"].sent == [("alpha", "!near"), ("bravo", "!far"), ("gamma", BROADCAST)]
        near, far, chan = await thread_of(app, pilot, "!near"), await thread_of(app, pilot, "!far"), await thread_of(app, pilot, BROADCAST)
        assert "alpha" in near and "bravo" not in near and "gamma" not in near and "delivered to near" in near
        assert "bravo" in far and "alpha" not in far and "delivered to far" in far
        assert "gamma" in chan and "alpha" not in chan and "delivered" not in chan  # broadcasts are not ACKed


async def test_inbox_lists_conversations_and_opens_on_enter(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.add_message({"ts": NOW, "from": "!far", "to": "!me", "text": "ping", "dir": "in"})
        await pilot.pause()
        inbox = app.query_one("#inbox", DataTable)
        assert {k.value for k in inbox.rows} == {BROADCAST, "!far"}
        assert inbox.get_row("!far")[1].plain == "● 1" and "ping" in inbox.get_row("!far")[2]
        inbox.focus()
        inbox.move_cursor(row=[r.key.value for r in inbox.ordered_rows].index("!far"))
        await pilot.press("enter")
        await pilot.pause()
        assert app.current == "!far" and app.query_one(TabbedContent).active == "messages" and app.unread == {}


async def test_switching_to_messages_tab_reads_current_thread(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.add_message({"ts": NOW, "from": "!far", "to": "^all", "text": "c1", "dir": "in"})
        await pilot.pause()
        assert app.unread == {BROADCAST: 1}
        await pilot.press("f5")  # current conversation is the channel
        await pilot.pause()
        assert app.unread == {} and "c1" in board_text(app)
        await pilot.press("f1")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "dash"


async def test_too_long_not_sent(tmp_path):
    app, holder = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_conv(BROADCAST)
        await pilot.pause()
        app.query_one("#send", Input).value = "x" * 201
        await pilot.press("enter")
        assert holder["b"].sent == []
        assert "Too long" in board_text(app)


async def test_radio_unavailable_shows_error(tmp_path):
    def boom(on_text):
        raise OSError("Device or resource busy")

    app = meshtui.MeshTUI(boom, log_path=str(tmp_path / "m.jsonl"))
    async with app.run_test() as pilot:
        assert "Could not open radio" in await thread_of(app, pilot, BROADCAST)
        assert any("No radio" in n.title for n in app._notifications)  # visible on the dashboard too


def test_conv_of():
    assert meshtui.conv_of({"dir": "in", "from": "!a", "to": "!me"}) == "!a"
    assert meshtui.conv_of({"dir": "in", "from": "!a", "to": "^all"}) == meshtui.BROADCAST
    assert meshtui.conv_of({"dir": "in", "from": "!a", "to": "!ffffffff"}) == meshtui.BROADCAST
    assert meshtui.conv_of({"dir": "out", "from": "!me", "to": "!b"}) == "!b"
    assert meshtui.conv_of({"dir": "out", "from": "!me", "to": meshtui.BROADCAST}) == meshtui.BROADCAST
    assert meshtui.conv_of({"dir": "note", "from": None, "to": "!b"}) == "!b"



# ---- map, staleness and clicking ---------------------------------------------------------------
ME_POS = {"latitude": 30.27, "longitude": -97.77}


def geo_nodes():
    """me at 30.27,-97.77; N is ~1.1 km north, E ~1 km east, W is 0.5 km west; S has no position."""
    def node(short, lat=None, lon=None, heard=NOW - 30):
        n = {"user": {"id": "!" + short, "shortName": short}, "lastHeard": heard, "hopsAway": 0}
        if lat is not None:
            n["position"] = {"latitude": lat, "longitude": lon}
        return n
    nodes = {"!me": {"user": {"id": "!me", "shortName": "me"}, "position": ME_POS},
             "!N": node("Nrth", 30.28, -97.77), "!E": node("East", 30.27, -97.76),
             "!W": node("West", 30.27, -97.775, heard=NOW - 90000), "!S": node("Nopos")}
    return nodes, [(k, v) for k, v in nodes.items() if k != "!me"]


def test_node_class_thresholds():
    assert meshtui.node_class({"lastHeard": NOW - 60}, NOW) == "live"
    assert meshtui.node_class({"lastHeard": NOW - 3 * 3600}, NOW) == "day"
    assert meshtui.node_class({"lastHeard": NOW - 3 * 86400}, NOW) == "old"
    assert meshtui.node_class({}, NOW) == "old"


def test_nice():
    assert [meshtui.nice(x) for x in (0.7, 1.4, 3, 7, 12, 250)] == [0.5, 1, 2, 5, 10, 200]


def test_geo_places_nodes_by_real_direction_and_reports_stats():
    nodes, rows = geo_nodes()
    hits, stats = [], {}
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, hits=hits, stats=stats).plain
    lines = text.split("\n")
    assert len(lines) == 31 and all(len(line) == 100 for line in lines)
    pos = {nid: (x0, y) for nid, x0, _, y in hits}
    me = next((line.index("✚"), i) for i, line in enumerate(lines) if "✚" in line)
    assert pos["!N"][1] < me[1] and abs(pos["!N"][0] - me[0]) <= 2  # north: above, same column
    assert pos["!E"][0] > me[0] and abs(pos["!E"][1] - me[1]) <= 1  # east: to the right, same row
    assert pos["!W"][0] < me[0]  # west: to the left
    assert pos["!S"][1] == 29 and "no position:" in lines[29] and "Nopos" in lines[29]  # tray, second-last row
    assert {k: stats[k] for k in ("on_map", "off", "no_pos", "center")} == {
        "on_map": 3, "off": 0, "no_pos": 1, "center": "you"}  # !S is not plotted, but counted
    assert "km" in lines[-1] or " m" in lines[-1]  # scale bar
    assert "Nrth" in text and "East" in text
    assert "West" not in text  # 25h old: remembered, drawn as an unlabelled dim dot


def test_geo_zoom_pushes_far_nodes_off_screen_and_focus_recentres():
    nodes, rows = geo_nodes()
    stats = {}
    meshtui.render_geo(100, 31, "!me", nodes, rows, zoom=8.0, stats=stats)
    assert stats["off"] >= 1 and stats["on_map"] + stats["off"] == 3  # zoomed in: the outer nodes leave the view
    stats = {}
    meshtui.render_geo(100, 31, "!me", nodes, rows, zoom=8.0, focus=(30.28, -97.77), stats=stats)
    assert stats["center"] == "focus" and stats["on_map"] >= 1


def test_geo_centres_on_median_without_own_position_and_handles_empty():
    nodes, rows = geo_nodes()
    del nodes["!me"]["position"]
    stats = {}
    meshtui.render_geo(100, 31, "!me", nodes, rows, stats=stats)
    assert stats["center"] == "median" and stats["on_map"] == 3
    empty = meshtui.render_geo(100, 31, "!me", {"!me": {}}, [("!S", {"user": {"shortName": "S"}})], stats=(s := {}))
    assert "No node has a position" in empty.plain and s["center"] is None
    assert "bigger window" in meshtui.render_geo(10, 5, "!me", {}, []).plain


def test_remembered_nodes_are_dim_unlabelled_dots_until_selected_or_unread():
    nodes, rows = geo_nodes()
    plain = meshtui.render_geo(100, 31, "!me", nodes, rows).plain
    assert "West" not in plain and "•" in plain
    assert "West" in meshtui.render_geo(100, 31, "!me", nodes, rows, selected="!W").plain
    assert "*West" in meshtui.render_geo(100, 31, "!me", nodes, rows, unread={"!W"}).plain


def test_draw_nodes_never_hides_a_colliding_node():
    nodes, rows = geo_nodes()
    for k in ("!N", "!E", "!W"):  # stack everyone on the same spot
        nodes[k]["position"] = {"latitude": 30.28, "longitude": -97.77}
        nodes[k]["lastHeard"] = NOW - 30
    hits = []
    meshtui.render_geo(100, 31, "!me", nodes, rows, hits=hits)
    stacked = [h for h in hits if h[0] in {"!N", "!E", "!W"}]  # (the tray also lists live no-position !S)
    assert {h[0] for h in stacked} == {"!N", "!E", "!W"}  # none of them was hidden
    assert len({h[3] for h in stacked}) == 3  # each nudged onto its own row


async def open_map(app, pilot):
    await pilot.press("f6")
    await pilot.pause(0.2)
    return app.query_one("#bigmap")


async def test_node_table_dims_remembered_nodes_and_counts_live(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test():
        table = app.query_one("#nodes", DataTable)
        assert str(table.get_row("!old")[0].style) == "dim"
        assert "dim" not in str(table.get_row("!near")[0].style)
        assert "3 known" in app.query_one("#nodes").border_title and "2 live" in app.query_one("#nodes").border_title


async def test_click_node_on_map_selects_it_and_shows_details(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        assert app.query_one(TabbedContent).active == "map" and bigmap.mode == "geo"
        x0, _, y = next((h[1], h[2], h[3]) for h in bigmap.hits if h[0] == "!near")
        await pilot.click("#bigmap", offset=(x0 + 1 + 1, y + 1))  # +1 border, +1 onto the label
        await pilot.pause()
        assert app.selected == "!near" and app.query_one(TabbedContent).active == "map"  # single click: no jump
        assert "near" in app.query_one("#mapdetail").render().plain
        assert not app.query_one("#dm", Button).disabled
        assert bigmap.selected == "!near" and "◉" in bigmap.render().plain


async def test_double_click_node_opens_its_dm(tmp_path):
    app, holder = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        x0, _, y = next((h[1], h[2], h[3]) for h in bigmap.hits if h[0] == "!near")
        await pilot.click("#bigmap", offset=(x0 + 1, y + 1), times=2)
        await pilot.pause(0.2)
        assert app.current == "!near" and app.query_one(TabbedContent).active == "messages"
        await pilot.press(*"hello", "enter")
        await pilot.pause(0.2)
        assert holder["b"].sent == [("hello", "!near")]


async def test_message_button_and_enter_key_open_dm(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        app.select_node("!far")
        await pilot.pause()
        await pilot.click("#dm")
        await pilot.pause(0.2)
        assert app.current == "!far" and app.query_one(TabbedContent).active == "messages"
        await pilot.press("f6")
        await pilot.pause(0.2)
        app.select_node("!near")
        bigmap.focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.current == "!near"


async def test_click_on_empty_map_space_does_nothing(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        await open_map(app, pilot)
        await pilot.click("#bigmap", offset=(3, 25))
        await pilot.pause()
        assert app.selected is None or app.selected == "!near"  # never changes to a node that wasn't clicked


async def test_map_keys_zoom_reset_mode_and_centre(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        bigmap.focus()
        await pilot.press("plus")
        assert bigmap.zoom == 1.5
        await pilot.press("minus", "minus")
        assert abs(bigmap.zoom - 1.5 / 1.5 / 1.5) < 1e-9
        app.select_node("!near")
        await pilot.press("c")
        assert bigmap.focus_pos == (30.28, -97.77)
        await pilot.press("0")
        assert bigmap.zoom == 1.0 and bigmap.focus_pos is None
        await pilot.press("g")
        assert bigmap.mode == "radar"
        await pilot.press("g")
        assert bigmap.mode == "geo"


async def test_map_subtitle_reports_counts(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        bigmap.refresh()
        await pilot.pause(0.3)
        sub = bigmap.border_subtitle
        assert "on map" in sub and "no position" in sub and "centred on you" in sub


def test_no_position_tray_lists_only_recent_nodes_and_truncates():
    nodes, rows = geo_nodes()
    nodes["!S"]["lastHeard"] = NOW - 90000 * 5  # remembered: not in the tray
    hits = []
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, hits=hits).plain
    assert "no position:" not in text and "!S" not in {h[0] for h in hits}
    for i in range(30):  # a crowd of live nodes without positions
        nodes[f"!p{i}"] = {"user": {"shortName": f"p{i:02}"}, "lastHeard": NOW - 10}
    rows = [(k, v) for k, v in nodes.items() if k != "!me"]
    hits = []
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, hits=hits).plain.split("\n")[29]
    assert "more" in text and len(text) == 100  # overflow summarised, never runs off the edge
    assert all(h[2] < 100 for h in hits)


async def test_click_tray_entry_selects_node_without_position(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        x0, _, y = next((h[1], h[2], h[3]) for h in bigmap.hits if h[0] == "!far")  # far: live, no position
        await pilot.click("#bigmap", offset=(x0 + 2, y + 1))
        await pilot.pause()
        assert app.selected == "!far"



# ---- basemap: coastlines, highways, cities -----------------------------------------------------
# A tiny synthetic world inside the ~2 km view around 30.27,-97.77: a coast (north-south, lon -97.775), a highway
# (east-west, lat 30.262) and two cities. Polylines are flat [lat, lon, lat, lon, ...] like data/basemap.json.
TINY = {
    "coast": [[30.24, -97.775, 30.30, -97.775]],
    "roads": [[30.262, -97.80, 30.262, -97.74]],
    "borders": [], "rivers": [],
    "places": [["Bigtown", "TX", 30.265, -97.755, 900000], ["Smallville", "TX", 30.263, -97.785, 20000]],
}


def braille_cells(text):
    return [(x, y) for y, line in enumerate(text.split("\n")) for x, ch in enumerate(line) if "\u2800" < ch <= "\u28ff"]


def test_basemap_draws_lines_as_braille_and_labels_cities():
    nodes, rows = geo_nodes()
    bm = meshtui.Basemap(TINY)
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, basemap=bm).plain
    cells = braille_cells(text)
    assert len(cells) > 40  # coast + highway rasterised
    lines = text.split("\n")
    coast_cols = {x for x, y in cells if y in range(5, 20)}
    assert coast_cols  # something drawn mid-map
    assert "Bigtown" in text and "◆" in text  # big city: filled marker
    assert "Smallville" in text and "◇" in text
    plain = meshtui.render_geo(100, 31, "!me", nodes, rows).plain
    assert not braille_cells(plain)  # no basemap -> no braille lines


def test_basemap_line_geometry_matches_projection():
    """The highway runs due east-west through lat 30.262, i.e. below (south of) the map centre."""
    nodes, rows = geo_nodes()
    bm = meshtui.Basemap({"coast": [], "roads": [[30.262, -97.90, 30.262, -97.64]], "borders": [], "rivers": [],
                          "places": []})
    stats = {}
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, basemap=bm, stats=stats).plain
    rows_with_line = {y for _, y in braille_cells(text)}
    cx, cy, kpr = stats["cx"], stats["cy"], stats["km_per_row"]
    expected_row = round(cy - (30.262 - 30.27) * 110.57 / kpr)  # south of centre -> below it
    assert expected_row > cy and rows_with_line <= {expected_row - 1, expected_row, expected_row + 1}
    xs = sorted(x for x, _ in braille_cells(text))
    assert xs[0] < 15 and xs[-1] > 60  # spans most of the width


def test_basemap_raster_is_cached_and_clips_offscreen():
    bm = meshtui.Basemap(TINY)
    center = (30.27, -97.77)
    to_cell = lambda lat, lon: (50 + (lon + 97.77) * 111.32 * 0.86 / 0.1 * 2, 15 - (lat - 30.27) * 110.57 / 0.1)
    a = bm.raster(100, 31, center, 0.1, to_cell)
    assert bm.raster(100, 31, center, 0.1, to_cell) is a  # cache hit
    far = meshtui.Basemap({"coast": [[45.0, -100.0, 46.0, -100.0]], "places": []})  # nowhere near the view
    assert far.raster(100, 31, center, 0.1, to_cell) == {}
    assert all(0 <= c < 100 and 0 <= r < 31 for c, r in a)


def test_city_labels_never_cover_nodes():
    nodes, rows = geo_nodes()
    bm = meshtui.Basemap({**TINY, "places": [["Nrth", "TX", 30.28, -97.77, 900000]]})  # right on top of node N
    hits = []
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, basemap=bm, hits=hits).plain
    n_hit = next(h for h in hits if h[0] == "!N")
    line = text.split("\n")[n_hit[3]]
    assert "Nrth" in line[n_hit[1]:n_hit[2] + 1]  # the node label survived; the city was skipped
    assert text.count("◆") == 0


def test_nearest_place_and_place_text():
    places = [["Bigtown", "TX", 30.30, -97.72, 900000], ["Smallville", "TX", 30.22, -97.74, 20000],
              ["Farville", "TX", 40.0, -100.0, 5000000]]
    near = meshtui.nearest_place(30.29, -97.73, places)
    assert near[0] == "Bigtown" and near[2] < 5
    assert meshtui.place_text(near) == "in Bigtown, TX"
    assert meshtui.place_text(("Bigtown", "TX", 42.4)) == "42 km from Bigtown, TX"
    assert meshtui.place_text(None) == ""
    assert meshtui.nearest_place(0.0, 0.0, places) is None  # nothing within range
    d = meshtui.render_detail("!near", {"user": {"shortName": "n"}, "position": {"latitude": 30.29, "longitude": -97.73}},
                              (30.27, -97.77), places).plain
    assert "in Bigtown, TX" in d


def test_home_centres_the_map_when_the_radio_has_no_position():
    nodes, rows = geo_nodes()
    del nodes["!me"]["position"]
    stats = {}
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, home=(30.27, -97.77), stats=stats).plain
    assert stats["center"] == "home" and stats["center_pos"] == (30.27, -97.77) and "✚" in text  # home marker


def test_load_basemap_handles_missing_and_real_file(tmp_path):
    assert meshtui.Basemap.load(str(tmp_path / "nope.json")) is None
    (tmp_path / "bad.json").write_text("{not json")
    assert meshtui.Basemap.load(str(tmp_path / "bad.json")) is None
    real = meshtui.Basemap.load()  # the shipped data/basemap.json
    assert real is not None and all(real.layers[k] for k in ("coast", "borders", "roads", "rivers"))
    austin = next(p for p in real.places if p[0] == "Austin")
    assert austin[1] == "TX" and abs(austin[2] - 30.27) < 0.05 and abs(austin[3] + 97.74) < 0.05
    assert meshtui.nearest_place(30.2672, -97.7431, real.places)[0] == "Austin"  # downtown Austin


async def test_hover_shows_coordinates_and_nearest_city(tmp_path):
    app, _ = make_app(tmp_path)
    app.basemap = meshtui.Basemap(TINY)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        bigmap.basemap = app.basemap
        bigmap.refresh()
        await pilot.pause(0.2)
        s = bigmap.stats
        # hover exactly on the map centre (you at 30.27,-97.77): title shows those coordinates
        await pilot.hover("#bigmap", offset=(round(s["cx"]) + 1, round(s["cy"]) + 1))
        await pilot.pause(0.2)
        assert "30.27" in bigmap.border_title and "-97.7" in bigmap.border_title
        assert "Bigtown" in bigmap.border_title or "Smallville" in bigmap.border_title


async def test_centre_on_node_without_position_warns(tmp_path):
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(170, 46)) as pilot:
        bigmap = await open_map(app, pilot)
        app.select_node("!far")  # live but has no position
        bigmap.focus()
        await pilot.press("c")
        await pilot.pause(0.2)
        assert bigmap.focus_pos is None
        assert any("no position" in n.message for n in app._notifications)



def test_city_label_beats_a_remembered_dot():
    nodes, rows = geo_nodes()
    nodes["!W"]["position"] = {"latitude": 30.265, "longitude": -97.755}  # 25h-old (remembered) node ON Bigtown
    hits = []
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, basemap=meshtui.Basemap(TINY), hits=hits).plain
    assert "Bigtown" in text and "◆" in text  # the city stays readable in a dense area
    w = next(h for h in hits if h[0] == "!W")  # the dim dot was nudged clear of the label, not drawn over it
    assert text.split("\n")[w[3]][w[1]] == "•"


def test_live_node_beats_a_city_label():
    nodes, rows = geo_nodes()
    nodes["!E"]["position"] = {"latitude": 30.265, "longitude": -97.755}  # live node ON Bigtown
    hits = []
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, basemap=meshtui.Basemap(TINY), hits=hits).plain
    assert "!E" in {h[0] for h in hits} and "East" in text  # live node placed first and kept
    assert "Bigtown" not in text  # the city label yielded instead of covering it


def test_places_off_screen_are_skipped_cheaply():
    nodes, rows = geo_nodes()
    many = [[f"Far{i}", "TX", 45.0 + i * 0.01, -100.0, 1000] for i in range(5000)] + TINY["places"]
    bm = meshtui.Basemap({**TINY, "places": many})
    t0 = time.time()
    text = meshtui.render_geo(100, 31, "!me", nodes, rows, basemap=bm).plain
    assert "Far" not in text and "Bigtown" in text
    assert time.time() - t0 < 0.5  # 5000 off-view places must not slow the render
