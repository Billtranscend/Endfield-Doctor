"""外接暗管出口的来料：存储和接口（0.10.13 功能二）。project.save_tunnel_feed（存成 inputs.json 里标在暗管出口上的输入端标记）、
check_fixes 的 tunnel_feeds 校验、api.bpa_fix / bpa_fix_batch 当场报错不开任务、修正任务的日志和撤「对」、_pending_view 的暗管来料卡，
近景巡检 gate_targets 跳过不在准入口上的标记；存、撤销只动设计分析认到这台上的标记（旁边有没卡的暗管出口时不改错）。

小据点照 tests/test_tunnel_feed.py 的 _scene 造（暗管出口 T 在 (300,300)、外接，锚点在 (305,296)；→ 管道准入口 A (420,300) → 灌装机
(700,300)，要壤晶废液 30、清水 30；输入端 1 号清水 30），再补 logistics.json（准入口的蓝图位置）、project.json、蓝图档案（临时目录里的
data/blueprints/档案：底账、配方表），project.BPA、project.ROOT 都指到临时目录，先跑一遍 rerun_design 写出 design.json。不读本机实测数据。
"""
import base64
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app"), str(ROOT / "tests")]

import api as A  # noqa: E402
import test_tunnel_feed as TF  # noqa: E402
from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import inputs as INP  # noqa: E402
from bpa import project as P  # noqa: E402
from bpa import vision as V  # noqa: E402

KEY = "暗管出口@300,300"
DEV = "灌装机@700,300"
ANC = [305, 296]  # 暗管出口锚点在拼合图上的位置（TF._basic：底账标签离锚点偏一点）


@contextlib.contextmanager
def project(sc=None, auto=None, confirm=None):
    """临时项目「项目」、据点「测试」，先算一遍 → 据点目录。auto：候选 inputs_auto.json（默认空）。"""
    sc = sc or TF._basic()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bd, led, rec = TF._scene(td, sc)
        proj = bd.parent.parent
        flow = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
        lg = [{"type": n["type"], "p": n["p"], "kind": n.get("kind"), "bp": TF.BP, "p_bp": TF._to_bp((0, 0), 0, n["p"])}
              for n in flow["nodes"]]
        (bd / "logistics.json").write_text(json.dumps(lg, ensure_ascii=False), encoding="utf-8")
        (bd / "inputs_auto.json").write_text(json.dumps(auto or [], ensure_ascii=False), encoding="utf-8")
        (proj / "project.json").write_text(json.dumps({"name": "项目", "groups": {"测试": [TF.BP]}, "archive": "档案"}, ensure_ascii=False),
                                           encoding="utf-8")
        arch = td / "data" / "blueprints" / "档案" / "ledger"
        arch.mkdir(parents=True)
        (arch / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
        (arch / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
        if confirm:
            (proj / "confirm.json").write_text(json.dumps({"测试": {"devices": confirm}}, ensure_ascii=False), encoding="utf-8")
        old = P.BPA, P.ROOT
        P.BPA, P.ROOT = td, td
        try:
            P.rerun_design("项目", "测试")
            yield bd
        finally:
            P.BPA, P.ROOT = old


def _marks(bd) -> list:
    return json.loads((bd.parent.parent / "inputs.json").read_text(encoding="utf-8"))["测试"]["marks"]


def _card(bd, key=KEY) -> dict:
    return next(p for p in json.loads((bd / "design.json").read_text(encoding="utf-8"))["pending"] if p.get("key") == key)


def _dev(bd, key=DEV) -> dict:
    return next(r for r in json.loads((bd / "design.json").read_text(encoding="utf-8"))["devices"] if P.device_key(r) == key)


def _bad(fn, *args):
    try:
        fn(*args)
    except ValueError as e:
        return str(e)
    raise AssertionError(f"应该报错：{args}")


def _bad_any(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        return str(e)
    raise AssertionError(f"应该报错：{args}")


def _tf(key=KEY, value=None):
    return {"kind": "tunnel_feeds", "key": key, "value": value}


def test_save_tunnel_feed_roundtrip():
    """新增、改、撤销：新的标记编号取最大号 + 1，位置、蓝图、锚点从 design.json 那张卡取（p 是暗管锚点，不信界面），重算后卡显示已填、
    下游按填的算；改成 0 = 这台平时不来料；撤销删掉标记、卡回到没填。inputs.json 是空的时从候选起步。
    作者在「输入端」页自己标在暗管上的（没有 on 字段、离暗管十几 px）：改它，不另加。"""
    with project() as bd:
        assert _card(bd)["answer"] is None and _dev(bd)["rate"] == 1.0
        c = P.save_tunnel_feed("项目", "测试", KEY, {"item": " 壤晶废液 ", "rate": 12.0})
        assert c["no"] == 2 and c["added"] and c["value"] == {"item": "壤晶废液", "rate": 12}, c
        ms = _marks(bd)
        assert len(ms) == 2 and ms[0]["bp"] == TF.BP, ms                                # 输入端 1 号补记了位置
        m = ms[1]
        assert {k: m[k] for k in ("no", "item", "rate", "via", "on", "source", "merged")} == {
            "no": 2, "item": "壤晶废液", "rate": 12, "via": "暗管", "on": "暗管出口", "source": "manual", "merged": False}, m
        assert m["p"] == ANC and m["bp"] == TF.BP and m["p_bp"] == _card(bd)["p_bp"] and "外接暗管出口" in m["note"], m
        P.rerun_design("项目", "测试")
        assert _card(bd)["answer"] == {"no": 2, "item": "壤晶废液", "rate": 12.0} and abs(_dev(bd)["rate"] - 0.4) < 1e-6, _card(bd)
        c = P.save_tunnel_feed("项目", "测试", KEY, {"item": "", "rate": 0})             # 这台平时不来料
        assert c["no"] == 2 and not c["added"] and [(m["no"], m["item"], m["rate"]) for m in _marks(bd)][1:] == [(2, "", 0)], _marks(bd)
        P.rerun_design("项目", "测试")
        assert _card(bd)["answer"]["rate"] == 0 and _dev(bd)["rate"] == 0.0, _card(bd)
        c = P.save_tunnel_feed("项目", "测试", KEY, None)                                # 撤销
        assert c["removed"] == [2] and [m["no"] for m in _marks(bd)] == [1], _marks(bd)
        P.rerun_design("项目", "测试")
        assert _card(bd)["answer"] is None and _dev(bd)["rate"] == 1.0
        assert P.save_tunnel_feed("项目", "测试", KEY, None)["removed"] == []             # 本来就没填：照样能撤，什么都不删
        # 编号接着最大号往下排（不补空号）：只剩 5 号时新加的是 6 号
        pj = bd.parent.parent / "inputs.json"
        pj.write_text(json.dumps({"测试": {"marks": [dict(_marks(bd)[0], no=5)]}}, ensure_ascii=False), encoding="utf-8")
        assert P.save_tunnel_feed("项目", "测试", KEY, {"item": "清水", "rate": 30})["no"] == 6
    # inputs.json 是空的：从候选起步（和「输入端」页、设计分析同一个规矩）
    gate1 = {"no": 1, "p": [40, 600], "item": "清水", "rate": 30, "via": "水泵", "source": "auto", "merged": False, "note": ""}
    with project(auto=[gate1]) as bd:
        pj = bd.parent.parent / "inputs.json"
        pj.write_text(json.dumps({"测试": {"marks": []}}, ensure_ascii=False), encoding="utf-8")
        P.save_tunnel_feed("项目", "测试", KEY, {"item": "壤晶废液", "rate": 12})
        assert [(m["no"], m["source"], m["item"]) for m in _marks(bd)] == [(1, "auto", "清水"), (2, "manual", "壤晶废液")], _marks(bd)
    # 作者自己标在暗管上的老标记（只填了物品，没有 on，位置偏了十几 px）：改它
    own = TF._mark(7, "壤晶废液", None, p=(312, 288))
    own.pop("note")
    with project(TF._basic([own])) as bd:
        assert _card(bd)["answer"] is None and _card(bd)["mark"]["no"] == 7
        c = P.save_tunnel_feed("项目", "测试", KEY, {"item": "壤晶废液", "rate": 12})
        assert c["no"] == 7 and not c["added"], c
        ms = _marks(bd)
        assert len(ms) == 2 and ms[1]["no"] == 7 and ms[1]["rate"] == 12 and ms[1]["on"] == "暗管出口" and ms[1]["p"] == ANC, ms
    # 一台标了两个（设计分析按先标的算）：改先标的那个；撤销两个都删，卡回到没填
    two = [TF._mark(22, "壤晶废液", 5), TF._mark(23, "壤晶废液", 20, p=(312, 291))]
    with project(TF._basic(two)) as bd:
        assert P.save_tunnel_feed("项目", "测试", KEY, {"item": "壤晶废液", "rate": 12})["no"] == 22
        assert [(m["no"], m["rate"]) for m in _marks(bd)] == [(1, 30), (22, 12), (23, 20)], _marks(bd)
        assert P.save_tunnel_feed("项目", "测试", KEY, None)["removed"] == [22, 23] and [m["no"] for m in _marks(bd)] == [1]
    # 贴着暗管的准入口上的输入端（准入口离暗管 40 px，标记在口上、也在暗管 45 px 内）：是准入口的，不改它，另加一个
    sc = TF._basic([TF._mark(22, "壤晶废液", 12, p=(336, 303))])
    sc["nodes"] = {"A": ("准入口", (340, 300))}
    with project(sc) as bd:
        c = P.save_tunnel_feed("项目", "测试", KEY, {"item": "清水", "rate": 6})
        assert c["no"] == 23 and c["added"] and [(m["no"], m["item"], m["rate"]) for m in _marks(bd)][1:] == [(22, "壤晶废液", 12), (23, "清水", 6)]


def test_neighbor_outlet_mark_kept():
    """T (300,300) 外接、有卡；U (360,300) 锚点不带 open、蓝图没有 links.json（缺文件，没卡）；作者以前标的 22 号在两台之间、离 U 近
    （0.10.13 以前的存法，带 bp、p_bp），设计分析把它认到 U 上。给 T 填 12：新加一个，不改 22 号；撤销 T 只删新加的，22 号留着。
    另有一台外接的 W 没填：两张卡都没填时不说会记成几号（谁先填谁拿最大号 + 1）。"""
    W = (300, 1000)
    sc = {"devices": [("暗管出口", TF.T, None), ("灌装机", (700, 300), TF.R["两样"]), ("暗管出口", (360, 300), None),
                      ("灌装机", (700, 600), TF.R["废液30"]), ("暗管出口", W, None), ("灌装机", (700, 1000), TF.R["污水30"])],
          "nodes": {"A": ("准入口", (420, 300))},
          "gates": [(1, (40, 900), "清水", 30, 1)],
          "links": [(0, "A", "pipe"), ("A", 1, "pipe"), (2, 3, "pipe"), (4, 5, "pipe")],
          "anchors": [(TF.BP, "暗管出口", TF.T, True), (TF.BP, "暗管出口", (360, 300), False), (TF.BP, "暗管出口", W, True)],
          "marks": [TF._mark(22, "壤晶废液", None, p=(335, 300), bp=TF.BP, p_bp=TF._to_bp((0, 0), 0, (335, 300)))]}
    with project(sc) as bd:
        cards = {p["key"]: p for p in A.Api().bpa_design("项目", "测试")["pending"] if p["kind"] == "暗管来料"}
        assert set(cards) == {KEY, "暗管出口@300,1000"} and cards[KEY]["marks"] == [] and cards[KEY]["mark"] is None, cards
        assert [cards[k]["input_no"] for k in sorted(cards)] == [None, None], cards                # 两张都没填：号码说不准
        c = P.save_tunnel_feed("项目", "测试", KEY, {"item": "壤晶废液", "rate": 12})
        assert c["no"] == 23 and c["added"], c
        m22 = next(m for m in _marks(bd) if m["no"] == 22)
        assert m22["rate"] is None and m22["item"] == "壤晶废液" and "on" not in m22, m22
        m23 = next(m for m in _marks(bd) if m["no"] == 23)
        assert m23["p"] == list(TF.T) and m23["rate"] == 12, m23                                    # 新加的在 T 的锚点上
        P.rerun_design("项目", "测试")
        assert _card(bd)["answer"] == {"no": 23, "item": "壤晶废液", "rate": 12.0} and _card(bd)["marks"] == [23], _card(bd)
        assert _dev(bd)["got"]["壤晶废液"] == 12.0, _dev(bd)
        assert P.save_tunnel_feed("项目", "测试", KEY, None)["removed"] == [23]
        assert [m["no"] for m in _marks(bd)] == [1, 22], _marks(bd)


def test_mark_on_tunnel():
    """标在这台暗管出口上的标记：不在准入口 15 px 内、离它 45 px 内，离别的暗管出口更近的归别的那台；按标记的先后。"""
    lg = [{"type": "准入口", "p": [340, 300]}, {"type": "管道分流器", "p": [300, 330]}]
    marks = [{"p": [338, 302]}, {"p": [310, 290]}, {"p": [300, 344]}, {"p": [300, 346]}, {"p": [262, 300]}, {"p": [300, 300]}]
    assert INP.mark_on_tunnel(marks, [300, 300], lg) == [1, 2, 4, 5]          # 0 在准入口上，3 离 46 px
    assert INP.mark_on_tunnel(marks, [300, 300], lg, [[240, 300]]) == [1, 2, 5]  # 4 离 (240,300) 那台更近


def test_save_tunnel_feed_checks():
    """校验（一项不对整批不存，inputs.json、fixes.json 都不动）：不是 dict / None；物品不是液体、气体，认不出来；没选物品又不是 0；
    rate 不是数字、负数、超过 120（多口暗管出口超过 240）；key 不是 design.json 里的暗管来料卡。多口的 240 收、via 写多口暗管。
    一批里同时有准入口限速和暗管来料：两处都存；第二处（inputs.json）没写成时报错里说清第一处已经存下了。"""
    with project() as bd:
        pj = bd.parent.parent / "inputs.json"
        before = pj.read_text(encoding="utf-8")
        bad = [(KEY, 12), (KEY, {"item": "芽针", "rate": 12}), (KEY, {"item": "水", "rate": 12}), (KEY, {"item": "", "rate": 12}),
               (KEY, {"item": "壤晶废液", "rate": 121}), (KEY, {"item": "壤晶废液", "rate": -1}), (KEY, {"item": "壤晶废液", "rate": "12"}),
               (KEY, {"item": "壤晶废液", "rate": True}), (KEY, {"item": "壤晶废液"}), (KEY, {"item": 3, "rate": 12}),
               ("暗管出口@1,1", {"item": "壤晶废液", "rate": 12}), ("暗管出口@1,1", None), ("灌装机@700,300", None)]
        msgs = [_bad(P.save_fixes, "项目", "测试", [_tf(k, v)]) for k, v in bad]
        assert "固体" in msgs[1] and "认不出来" in msgs[2] and "0–120" in msgs[4] and "121" in msgs[4] and "待确认" in msgs[10], msgs
        gk = next(r["key"] for r in P.gates_view("项目", "测试")["gates"] if r["c"] == [420, 300])
        _bad(P.save_fixes, "项目", "测试", [{"kind": "gates", "key": gk, "value": {"limit": 6}}, _tf(KEY, {"item": "芽针", "rate": 12})])
        _bad(P.save_fixes, "项目", "测试", [_tf(KEY, {"item": "壤晶废液", "rate": 12}), {"kind": "gates", "key": gk, "value": {"limit": 7}}])
        assert pj.read_text(encoding="utf-8") == before and not (bd.parent.parent / "fixes.json").exists()
        real = P._apply_tunnel_feeds                                                 # 混批：fixes.json 存了、inputs.json 没写成
        P._apply_tunnel_feeds = lambda *a: (_ for _ in ()).throw(PermissionError("文件被占用"))
        try:
            msg = _bad_any(P.save_fixes, "项目", "测试", [{"kind": "gates", "key": gk, "value": {"limit": 12}}, _tf(KEY, {"item": "壤晶废液", "rate": 12})])
        finally:
            P._apply_tunnel_feeds = real
        assert "另外 1 项修正已经存下了" in msg and "暗管来料没存上" in msg and "文件被占用" in msg, msg
        assert D.load_fixes(bd)["gates"][gk]["limit"] == 12 and pj.read_text(encoding="utf-8") == before
        for v in ({"item": "壤晶废液", "rate": 0}, {"item": None, "rate": 0}, {"item": "壤晶废液", "rate": 120}, {"item": "息壤气", "rate": 0.5}):
            P.check_fixes("项目", "测试", [_tf(KEY, v)])                               # 边界上的都收
        done = P.save_fixes("项目", "测试", [{"kind": "gates", "key": gk, "value": {"limit": 6}}, _tf(KEY, {"item": "壤晶废液", "rate": 12})])
        assert [c["kind"] for c in done] == ["gates", "tunnel_feeds"] and D.load_fixes(bd)["gates"][gk]["limit"] == 6
        assert "tunnel_feeds" not in D.load_fixes(bd) and _marks(bd)[-1]["rate"] == 12, D.load_fixes(bd)
    with project(dict(TF._multi(None), marks=[])) as bd:
        key = "多口暗管出口@300,300"
        assert "0–240" in _bad(P.check_fixes, "项目", "测试", [_tf(key, {"item": "污水", "rate": 241})])
        P.save_tunnel_feed("项目", "测试", key, {"item": "污水", "rate": 240})
        m = _marks(bd)[-1]
        assert (m["via"], m["on"], m["rate"], m["p"]) == ("多口暗管", "多口暗管出口", 240, [300, 300]), m


def test_api_tunnel_feed():
    """接口：bpa_fix 的 tunnel_feeds 校验不过当场报错、不开任务；对的开一个修正任务。任务里存 inputs.json → 只重算一次 → 结果变了的设备
    原来点的「对」撤掉、日志点名；日志写「暗管出口 (300,300) 的来料 → 壤晶废液 12/分（输入端 2 号，新加的）」（拼合图坐标）。
    待确认卡：open（只填了物品也算没答）、物品下拉（下游要的排前面，只有液体、气体）、上限、接口、会记成输入端几号、小图。"""
    with project(confirm={DEV: {"verdict": "ok"}}) as bd:
        a = A.Api()
        started = []
        real = a._start
        a._start = lambda kind, fn, *args, **kw: started.append((kind, args, kw)) or {"ok": True}
        for k, v in ((KEY, {"item": "芽针", "rate": 12}), (KEY, {"item": "壤晶废液", "rate": 200}), ("暗管出口@1,1", {"item": "壤晶废液", "rate": 1})):
            r = a.bpa_fix("项目", "测试", "tunnel_feeds", k, v)
            assert r["ok"] is False and r["error"], (k, v, r)
        r = a.bpa_fix_batch("项目", "测试", [_tf(KEY, {"item": "壤晶废液", "rate": 12}), {"kind": "feeds", "key": DEV, "value": "随便写"}])
        assert r["ok"] is False and not started, r                                  # 一项不对：整批不开任务
        assert a.bpa_fix("项目", "测试", "tunnel_feeds", KEY, {"item": "壤晶废液", "rate": 12})["ok"]
        assert [(k, kw) for k, _, kw in started] == [("bpa", {"log_kind": A.FIX_LOG_KIND})], started
        a._start = real
        d = a.bpa_design("项目", "测试")
        card = next(p for p in d["pending"] if p["kind"] == "暗管来料")
        assert card["open"] and card["cap"] == 120 and card["via"] == "暗管" and card["input_no"] == 2, card
        assert card["fluids"][:2] == ["壤晶废液", "清水"] and "息壤气" in card["fluids"] and "芽针" not in card["fluids"], card["fluids"]
        assert len(card["fluids"]) == len(set(card["fluids"])), card["fluids"]
        img = cv2.imdecode(np.frombuffer(base64.b64decode(card["image"].split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)
        assert img is not None and img.shape[:2] == (400, 560), img.shape                # 裁图盖住暗管和它接出去那一路的第一个物流件
        job = A.Job(kind="bpa", state="running")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a._bpa_fix_batch(job, "项目", "测试", [_tf(KEY, {"item": "壤晶废液", "rate": 12})])
        log = buf.getvalue()
        assert "暗管出口 (300,300) 的来料 → 壤晶废液 12/分（输入端 2 号，新加的）" in log, log
        assert job.result["changed"] == 1 and job.result["revoked"] == [DEV] and "灌装机 (700,300)" in log.split("已经撤掉")[1], log
        card = next(p for p in a.bpa_design("项目", "测试")["pending"] if p["kind"] == "暗管来料")
        assert not card["open"] and card["answer"]["rate"] == 12 and card["input_no"] == 2, card
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            a._bpa_fix_batch(job, "项目", "测试", [_tf(KEY, None)])
        assert "暗管出口 (300,300) 的来料 → 撤销（删掉输入端 2 号）" in buf.getvalue(), buf.getvalue()
    own = TF._mark(7, "壤晶废液", None, p=(312, 288))                                 # 只填了物品：卡算没答
    with project(TF._basic([own])) as bd:
        card = next(p for p in A.Api().bpa_design("项目", "测试")["pending"] if p["kind"] == "暗管来料")
        assert card["open"] and card["answer"] is None and card["input_no"] == 7 and card["mark"]["item"] == "壤晶废液", card


def test_near_patrol_skips_tunnel_marks():
    """近景巡检的流体输入端：按位置跳过 15 px 内没有准入口的标记（标在外接暗管出口上的，没有 on 字段的老标记也跳过）；
    据点目录里没有 logistics.json 的老拼合结果照旧全看。"""
    import near_patrol as NP
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bd = td / "debug" / "bpa" / "项目" / "base" / "测试"
        bd.mkdir(parents=True)
        (bd.parent.parent / "project.json").write_text(json.dumps({"name": "项目", "archive": "档案"}, ensure_ascii=False), encoding="utf-8")
        V.wr(bd / "composite.png", np.full((800, 800, 3), 232, np.uint8))
        tf = {"K": 1.0, "F0": [0, 0], "bps": {}}
        lg = [{"type": "准入口", "p": [40, 600], "bp": "图", "p_bp": [40, 600]}, {"type": "管道分流器", "p": [300, 330]}]
        marks = [{"no": 1, "p": [44, 596], "item": "清水", "rate": 60}, {"no": 2, "p": [305, 296], "item": "壤晶废液", "rate": 12},
                 {"no": 3, "p": [300, 300], "item": "息壤气", "rate": 30, "on": "暗管出口"}]
        for f, v in (("transform.json", tf), ("logistics.json", lg), ("inputs_auto.json", [])):
            (bd / f).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
        (bd.parent.parent / "inputs.json").write_text(json.dumps({"测试": {"marks": marks}}, ensure_ascii=False), encoding="utf-8")
        F2N = np.array([[1.0, 0, 0], [0, 1.0, 0]])
        old = NP.ROOT
        NP.ROOT = td
        try:
            assert NP.bpa_base_dir("档案", "测试") == bd
            assert [t["gate_no"] for t in NP.gate_targets("档案", "测试", F2N, None)] == [1]
            assert [t["gate_no"] for t in NP.gate_targets("档案", "测试", F2N)] == [1]
            (bd / "logistics.json").unlink()                                           # 老拼合结果：不知道准入口在哪，照旧全看
            assert [t["gate_no"] for t in NP.gate_targets("档案", "测试", F2N, None)] == [1, 2, 3]
        finally:
            NP.ROOT = old


if __name__ == "__main__":
    sys.exit(run([test_save_tunnel_feed_roundtrip, test_neighbor_outlet_mark_kept, test_mark_on_tunnel, test_save_tunnel_feed_checks,
                  test_api_tunnel_feed, test_near_patrol_skips_tunnel_marks]))
