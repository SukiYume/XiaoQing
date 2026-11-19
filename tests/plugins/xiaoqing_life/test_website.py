"""观测站用的作息、日期和只读数据边界。"""

import json
import socket
import sys
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientConnectorError, ClientSession
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from plugins.xiaoqing_life import website
from plugins.xiaoqing_life.travel import generate_travel_day, publish
from plugins.xiaoqing_life.website import SITE, archive, create_app, live_status
from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.settings_snapshot import settings_snapshot

ZONE = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("options", "args", "expected"),
    [
        ({}, [], 12002),
        ({"web_port": 13002}, [], 13002),
        ({"web_port": 13002}, ["--port", "14002"], 14002),
        ({"web_port": True}, [], None),
        ({"web_port": 65536}, [], None),
    ],
)
def test_station_port_at_startup(tmp_path, monkeypatch, options, args, expected):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "pendo"        : {"web_port": 12001},
                    "xiaoqing_life": options,
                }
            }
        ),
        encoding="utf8",
    )
    monkeypatch.setattr(sys, "argv", ["website", "--root", str(tmp_path), *args])
    bindings = []
    monkeypatch.setattr(
        website.web, "run_app", lambda app, *, host, port: bindings.append((host, port))
    )
    if expected is None:
        with pytest.raises(SystemExit) as error:
            website.main()
        assert error.value.code == 2 and not bindings
    else:
        website.main()
        assert bindings == [("127.0.0.1", expected)]


@pytest.mark.asyncio
async def test_plugin_owns_web_listener_and_recovers_after_port_conflict(tmp_path, monkeypatch):
    from plugins.xiaoqing_life import main as life

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port   = occupied.getsockname()[1]
        config = {"timezone": "UTC", "plugins": {"xiaoqing_life": {"web_port": port}}}
        (tmp_path / "config").mkdir()
        (tmp_path / "config/config.json").write_text(json.dumps(config))
        ctx = SimpleNamespace(
            runtime_root = tmp_path,
            data_dir     = tmp_path / "data/xiaoqing_life",
            state        = {},
            get_settings_snapshot=lambda: settings_snapshot(config=config),
        )
        await life.init(ctx)
        assert life.get_runtime(ctx).web_runner is None

    url = f"http://127.0.0.1:{port}/api/live"
    async with ClientSession() as client:
        for _ in range(2):
            try:
                await life.init(ctx)
                runner = life.get_runtime(ctx).web_runner
                await life.init(ctx)
                assert runner is not None and life.get_runtime(ctx).web_runner is runner
                async with client.get(url) as response:
                    assert response.status == 200 and (await response.json())["zone"] == "UTC"
                with monkeypatch.context() as missing_chat:
                    missing_chat.setitem(sys.modules, "plugins.xiaoqing_chat.life.schedule", None)
                    async with client.get(url) as response:
                        assert response.status == 503
                    async with client.get(f"http://127.0.0.1:{port}/api/archive") as response:
                        assert response.status == 200
            finally:
                await life.shutdown(ctx)
            with pytest.raises(ClientConnectorError):
                await client.get(url)


def test_scene_switch_keeps_display_until_latest_selection_is_ready():
    script = """
        let finishPose;
        const slowPose = new Promise(resolve => { finishPose = resolve; });
        globalThis.Image = class {
            decode() {
                if (this.src.endsWith("desk-poses.webp")) return slowPose;
                if (this.src.endsWith("tea-room.webp")) return Promise.reject(new Error("image unavailable"));
                return Promise.resolve();
            }
        };
        globalThis.matchMedia = () => ({matches: true});
        const painted = [];
        const slot = {prepend: img => painted.push(img.src), append: img => painted.push(img.src)};
        const host = {
            firstElementChild: {querySelector: () => slot},
            setAttribute() {}, removeAttribute() {},
        };
        const show = client.createStagePlayer(host);
        await show("club", "club");
        const original = host.innerHTML;
        const pending = show("desk", "desk");
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(host.innerHTML, original);
        assert.deepEqual(painted, ["assets/live/club-room.webp", "assets/live/club-poses.webp"]);
        await show("tea", "tea");
        assert.match(host.innerHTML, /data-scene="tea"/);
        assert.equal(painted.at(-1), "assets/tea.webp");
        const visible = host.innerHTML;
        finishPose();
        await pending;
        assert.equal(host.innerHTML, visible);
        assert.equal(painted.length, 3);
        await show("desk", "desk");
        assert.match(host.innerHTML, /data-scene="desk"/);
        assert.deepEqual(painted.slice(-2), ["assets/live/desk-room.webp", "assets/live/desk-poses.webp"]);
    """
    assert_node_esm_contract(
        (SITE / "assets/stage.js").read_text(encoding="utf8"), script, cwd=SITE
    )


@pytest.mark.parametrize(
    ("time", "scene"),
    [
        ("2026-11-13 20:00", "club"),
        ("2026-11-16 08:00", "classroom"),
        ("2026-11-16 11:50", "meal"),
        ("2026-11-16 12:50", "desk"),
        ("2026-11-17 02:00", "rest"),
        ("2026-11-16 23:15", "anime"),
        ("2026-11-16 07:45", "campus"),
        ("2026-11-12 16:15", "tea"),
        ("2026-11-14 15:30", "game"),
        ("2026-11-15 15:30", "library"),
    ],
)
def test_scene_follows_persona_schedule(tmp_path, time, scene):
    live = live_status(tmp_path, datetime.fromisoformat(time).replace(tzinfo=ZONE))
    assert (live["scene"], live["source"]) == (scene, "日常作息")
    assert list(tmp_path.iterdir()) == []


def test_live_session_expires_and_keeps_query_private(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.json").write_text(
        '{"plugins":{"xiaoqing_life":{"enabled":true}}}', encoding="utf8"
    )
    directory = tmp_path / "data/xiaoqing_life"
    directory.mkdir(parents=True)
    moment = datetime(2026, 11, 13, 20, tzinfo=ZONE)
    at      = moment.timestamp()
    current = {"start": at, "end": at + 60, "choice": "interest_dive", "query": "private-query"}
    (directory / "state.json").write_text(json.dumps({"current": current}), encoding="utf8")
    live = live_status(tmp_path, moment)
    assert (live["scene"], live["source"]) == ("desk", "生活记录")
    assert "private-query" not in str(live)
    assert live_status(tmp_path, datetime.fromtimestamp(at + 60, ZONE))["scene"] == "club"


def test_archive_filters_future_and_private_fields_and_reads_backup_without_writing(tmp_path):
    directory = tmp_path / "data/xiaoqing_life"
    directory.mkdir(parents=True)
    now = datetime(2026, 11, 16, 2, tzinfo=ZONE)
    at      = now.timestamp()
    episode = {"title": "观测笔记", "at": at, "share": True, "chat_topic": "private-topic"}
    rows    = [episode, {**episode, "at": at + 1}, {**episode, "share": False}]
    (directory / "episodes.json").write_text(json.dumps(rows), encoding="utf8")
    (directory / "journal.json").write_bytes(b"broken")
    (directory / "journal.json.bak").write_text(
        json.dumps([{"kind": "weekly", "at": at, "week": "2026-W46", "text": "周末整理"}]),
        encoding="utf8",
    )
    (directory / "state.json").write_text(
        json.dumps(
            {
                "stats": {
                    "2026-11-14": {},
                    "2026-11-15": {
                        "session.hot_scroll"      : 2,
                        "session.thread_continue" : 1,
                        "episodes"                : 4,
                        "chat.turns"              : 48,
                        "thread_continue.rounds"  : 3,
                        "thread_continue.findings": 2,
                        "terms_seen"              : 4,
                        "term_lookup.hits"        : 2,
                        "term_lookup.misses"      : 1,
                    },
                },
                "bili_login": "private-login",
            }
        ),
        encoding="utf8",
    )
    data = archive(tmp_path, now)
    assert len(data["episodes"]) == 1
    assert data["journal"] == [{"date": "2026-W46", "kind": "weekly", "text": "周末整理"}]
    assert data["today"] == "2026-11-15"
    assert data["week"][-1] == {
        "day"       : data["today"],
        "recorded"  : True,
        "episodes"  : 4,
        "sessions"  : 3,
        "activities": {
            "hot_scroll"     : 2,
            "interest_dive"  : 0,
            "thread_continue": 1,
            "work_on"        : 0,
            "term_lookup"    : 0,
        },
        "rounds"  : 3,
        "findings": 2,
        "terms"   : 4,
        "hits"    : 2,
        "misses"  : 1,
    }
    assert data["week"][-2]["recorded"] and data["week"][-2]["sessions"] == 0
    assert data["week"][0]["recorded"] is False
    assert "private-" not in str(data) and "chat.turns" not in str(data)
    assert (directory / "journal.json").read_bytes() == b"broken"
    (directory / "state.json").write_text(
        json.dumps({"stats": {data["today"]: {"episodes": "broken"}}})
    )
    with pytest.raises(ValueError, match="invalid daily count"):
        archive(tmp_path, now)


@pytest.mark.asyncio
async def test_site_serves_pages_and_recovers_after_read_failure(tmp_path):
    directory = tmp_path / "data/xiaoqing_life"
    directory.mkdir(parents=True)
    path = directory / "journal.json"
    path.write_text("broken", encoding="utf8")
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        landing = await (await client.get("/")).text()
        assert '<h1>小青的<br><em>校园日常</em></h1>' in landing
        assert '<style>' in landing and '.hero{' in landing.split('</head>', 1)[0]
        assert 'rel="stylesheet"' not in landing and 'as="font"' not in landing
        assert (await client.get("/live")).status == 200
        assert (await client.get("/assets/club.webp")).content_type == "image/webp"
        assert (await client.get("/data/xiaoqing_life/state.json")).status == 404
        assert (await client.post("/api/archive")).status == 405
        assert (await client.get("/api/archive")).status == 503
        path.write_text("[]", encoding="utf8")
        response = await client.get("/api/archive")
        assert response.status == 200 and (await response.json())["journal"] == []


@pytest.mark.asyncio
async def test_travel_matches_captions_to_both_archived_images_by_name(tmp_path):
    day   = date(2026, 12, 1)
    files = {}
    for n, color in ((2, "blue"), (1, "red")):
        path = tmp_path / f"job-0101-0{3 - n}.png"
        Image.new("RGB", (30 * n, 20), color).save(path)
        files[f"{day}-{n}.png"] = str(path)
    response = {"status": "done", "images_by_name": files}

    async def draw(prompt, references):
        return response

    ctx = SimpleNamespace(data_dir=tmp_path, capabilities=SimpleNamespace(travel_postcards=draw))
    folder = tmp_path / "travel" / day.isoformat()
    for captions in (["缺第二篇"], ["晨间徒步", "入夜观星"]):
        response["final_text"] = json.dumps(
            {"country": "智利", "city": "圣佩德罗", "captions": captions}
        )
        await generate_travel_day(ctx, day)
        assert folder.is_dir() == (len(captions) == 2)
    cards = json.loads((folder / "entry.json").read_bytes())["photos"]
    assert [(c["width"], c["caption"]) for c in cards] == [(30, "晨间徒步"), (60, "入夜观星")]


@pytest.mark.asyncio
async def test_custom_data_root_serves_life_records_and_skips_broken_travel_days(
    tmp_path, monkeypatch
):
    from plugins.xiaoqing_life import travel

    data_root = tmp_path / "custom"
    directory = data_root / "xiaoqing_life"
    directory.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/config.json").write_text(
        json.dumps(
            {
                "data_root": str(data_root),
                "plugins"  : {"xiaoqing_life": {"enabled": True}},
            }
        )
    )
    (directory / "state.json").write_text(
        json.dumps(
            {
                "current": {"choice": "term_lookup", "start": 0, "end": 4102444800},
            }
        )
    )
    (directory / "episodes.json").write_text(
        json.dumps([{"at": 1, "share": True, "title": "shared"}])
    )
    broken = directory / "travel/2026-11-16"
    broken.mkdir(parents=True)
    (broken / "entry.json").write_text('{"photos": {}}')
    picture = tmp_path / "photo.png"
    Image.new("RGB", (16, 9)).save(picture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "travel",
            "--root",
            str(tmp_path),
            "--date",
            "2026-11-18",
            "--country",
            "Japan",
            "--city",
            "Kyoto",
            "--photo",
            str(picture),
            "Maple",
        ],
    )
    travel.main()
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        assert (await (await client.get("/api/live")).json())["source"] == "生活记录"
        assert (await (await client.get("/api/archive")).json())["episodes"][0]["title"] == "shared"
        stops = await (await client.get("/api/travel")).json()
        assert [stop["city"] for stop in stops] == ["Kyoto", "里斯本"]
        assert (await client.get(stops[0]["photos"][0]["src"])).content_type == "image/webp"


@pytest.mark.asyncio
async def test_travel_publishes_whole_days_and_serves_new_photos_without_restart(tmp_path):
    source = tmp_path / "image.png"
    Image.new("RGB", (64, 36), "green").save(source)
    folder = tmp_path / "data/xiaoqing_life/travel"
    day    = date(2026, 11, 18)
    photos = [(source, "午后"), (source, "入夜")]
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        assert (await client.get("/travel")).status == 200
        seed = (await (await client.get("/api/travel")).json())[0]
        assert seed["city"] == "里斯本"
        assert (await client.get(seed["photos"][0]["src"])).content_type == "image/webp"
        assert not publish(folder, date.fromisoformat(seed["date"]), "地区", "重复首站", photos)
        with pytest.raises(FileNotFoundError):
            publish(folder, day, "地区", "城市", [photos[0], (tmp_path / "missing", "第二幅")])
        assert list(folder.iterdir()) == []
        assert await (await client.get("/api/travel")).json() == [seed]
        assert publish(folder, day, "地区", "城市", photos)
        assert not publish(folder, day, "其他地区", "重复当天", photos[:1])
        first, second = await (await client.get("/api/travel")).json()
        assert (first["date"], first["city"], second) == (day.isoformat(), "城市", seed)
        assert [p["caption"] for p in first["photos"]] == ["午后", "入夜"]
        for photo in first["photos"]:
            response = await client.get(photo["src"])
            assert response.content_type == "image/webp"
            with Image.open(BytesIO(await response.read())) as decoded:
                assert decoded.size == (photo["width"], photo["height"]) == (64, 36)
        assert (await client.get(f"/travel-media/{day}/entry.json")).status == 404


@pytest.mark.asyncio
async def test_stage_serves_distinct_transparent_cells_for_all_scenes(tmp_path):
    scenes = (
        "club",
        "desk",
        "classroom",
        "meal",
        "rest",
        "anime",
        "campus",
        "tea",
        "game",
        "library",
    )
    atlases = [(f"{scene}-poses", 2, 2) for scene in scenes]
    atlases.extend([("items", 3, 2), ("faces", 4, 6), ("field-items", 2, 2), ("field-faces", 4, 4)])
    async with TestClient(TestServer(create_app(tmp_path))) as client:
        for scene in scenes:
            room = await client.get(f"/assets/live/{scene}-room.webp")
            assert room.status == 200
            with Image.open(BytesIO(await room.read())) as image:
                assert image.format == "WEBP" and image.width > image.height
                image.verify()
        for name, columns, rows in atlases:
            response = await client.get(f"/assets/live/{name}.webp")
            assert response.status == 200
            with Image.open(BytesIO(await response.read())) as sheet:
                assert sheet.format == "WEBP" and sheet.mode == "RGBA"
                tile, remainder = divmod(sheet.width, columns)
                assert remainder == 0 and sheet.height == tile * rows
                cells = set()
                for y in range(0, sheet.height, tile):
                    for x in range(0, sheet.width, tile):
                        cell = sheet.crop((x, y, x + tile, y + tile))
                        assert cell.getchannel("A").getbbox(), (name, x, y)
                        if name.endswith("-poses"):
                            assert cell.getchannel("A").getextrema() == (0, 255)
                        cells.add(cell.tobytes())
                assert len(cells) == columns * rows, name
