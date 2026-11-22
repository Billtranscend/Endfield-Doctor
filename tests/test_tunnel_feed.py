"""外接暗管出口的来料（0.10.13 功能二的计算部分：design.tunnel_outlets、标在暗管出口上的输入端标记、sources() 的暗管分支、
「暗管来料」待确认卡）。在临时目录里造小据点，直接调 design.analyze，不读本机实测数据。

小据点：transform 的 K = 1、F0 = 0，拼合图坐标 = 底账远景坐标；蓝图「测试图」t = 0、不转，锚点在预览里的坐标 = 拼合图坐标 / vision.K。
暗管出口的「外接」靠 bp/<蓝图>/anchors.json 里同名锚点带 open。都是 2 秒一份的配方：一条满速每种原料 30/分（3 秒 20、10 秒 6）。
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import vision as V  # noqa: E402

BP = "测试图"
R = {
    "两样": "壤晶废液×1 + 清水×1 → 测试品×1（2秒）",
    "废液30": "壤晶废液×1 → 测试品×1（2秒）",
    "废液6": "壤晶废液×1 → 测试品×1（10秒）",
    "污水30": "污水×1 → 测试品×1（2秒）",
    "污水20": "污水×1 → 测试品×1（3秒）",
    "污水150": "污水×5 → 测试品×1（2秒）",
    "做废液": "清水×1 → 壤晶废液×1（2秒）",
}
T = (300, 300)  # 暗管出口的位置（各场景都用它）


def _to_bp(t, rot, c):
    """拼合图坐标 → 这张蓝图预览里的坐标（vision.preview_to_canvas 反过来；K = 1、F0 = 0）。"""
    x, y = (c[0] - t[0]) / V.K, (c[1] - t[1]) / V.K
    return [round(v, 1) for v in {0: (x, y), 90: (y, -x), 180: (-x, -y), 270: (-y, x)}[rot]]


def _scene(td: Path, sc: dict):
    """→ (据点目录, 底账, 配方表)。sc：
    devices [(名字, 坐标, 配方)]、nodes {键: (类型, 坐标[, 物品])}、gates [(编号, 坐标, 物品, 流速, 去向)]（准入口输入端，标记和口同一点）、
    links [(a, b, 层)]（整数 = 第几台设备，字符串 = 节点键 / g<编号>）、
    bps {蓝图: (t, rot)}（默认只有「测试图」）、anchors [(蓝图, 名字, 拼合图坐标, open)]、
    links_json {蓝图: [(i, j)…]}（这张蓝图不带 open 的暗管按 anchors 里的顺序编号；有这一项才写 links.json 和 mosaic.png）、
    marks [标记]（不在准入口上的输入端，比如标在暗管出口上的）。"""
    proj = td / "项目"
    bd = proj / "base" / "测试"
    bd.mkdir(parents=True)
    devs = [{"name": n, "x": p[0], "y": p[1]} for n, p, _ in sc["devices"]]
    recs = [{"name": n, "x": p[0], "y": p[1], "recipe": rc} for n, p, rc in sc["devices"] if rc]
    nodes, links, marks, nid = [], [], [], {}

    def add(key, tp, p, item=None):
        nid[key] = len(nodes)
        nodes.append({"id": len(nodes), "type": tp, "p": list(p), "c": list(p), "kind": "管道"})
        if item:
            nodes[-1]["item"] = item
    for key, (tp, p, *item) in sc.get("nodes", {}).items():
        add(key, tp, p, *item)
    glinks = []
    for no, p, item, rate, dst in sc.get("gates", []):
        add(f"g{no}", "准入口", p)
        marks.append({"no": no, "p": list(p), "item": item, "rate": rate, "kind": "管道"})
        glinks.append((f"g{no}", dst, "pipe"))
    marks += [dict(m) for m in sc.get("marks", [])]

    def pos(k):
        return list(sc["devices"][k][1]) if isinstance(k, int) else list(nodes[nid[k]]["p"])
    for a, b, layer in glinks + list(sc.get("links", [])):
        lk = {"layer": layer, "path": [pos(a), pos(b)], "basis": ["合成"]}
        if isinstance(a, int) and isinstance(b, int):
            lk.update(free=True, a=None, end="device", a_dev={"name": devs[a]["name"], "c": pos(a)},
                      device={"name": devs[b]["name"], "c": pos(b)}, dir="a->b")
        elif isinstance(b, int):
            lk.update(a=nid[a], a_side="E", end="device", device={"name": devs[b]["name"], "c": pos(b)}, dir="a->b")
        elif isinstance(a, int):
            lk.update(a=nid[b], a_side="W", end="device", device={"name": devs[a]["name"], "c": pos(a)}, dir="b->a")
        else:
            lk.update(a=nid[a], a_side="E", b=nid[b], end="node", dir="a->b")
        links.append(lk)
    bps = sc.get("bps", {BP: ((0, 0), 0)})
    (bd / "flow.json").write_text(json.dumps({"nodes": nodes, "links": links, "issues": [], "structures": []}, ensure_ascii=False),
                                  encoding="utf-8")
    (bd / "transform.json").write_text(json.dumps({"K": 1.0, "F0": [0, 0], "bps": {b: {"t": list(t), "rot": r} for b, (t, r) in bps.items()}},
                                                  ensure_ascii=False), encoding="utf-8")
    V.wr(bd / "composite.png", np.full((1300, 1900, 3), 232, np.uint8))
    for b, (t, r) in bps.items():
        anc = [{"name": nm, "p": _to_bp(t, r, c), "score": 0.99, **({"open": True} if op else {})}
               for bb, nm, c, op in sc.get("anchors", []) if bb == b]
        if not anc and b not in sc.get("links_json", {}):
            continue
        d = proj / "bp" / b
        d.mkdir(parents=True)
        (d / "anchors.json").write_text(json.dumps({"anchors": anc}, ensure_ascii=False), encoding="utf-8")
        if b in sc.get("links_json", {}):
            pipes = [a for a in anc if not a.get("open")]
            part = {str(k): None for k in range(len(pipes))}
            for x, y in sc["links_json"][b]:
                part[str(x)], part[str(y)] = y, x
            (d / "links.json").write_text(json.dumps({"pipes": pipes, "partner": part}, ensure_ascii=False), encoding="utf-8")
            V.wr(d / "mosaic.png", np.full((20, 20, 3), 232, np.uint8))
    (proj / "inputs.json").write_text(json.dumps({"测试": {"marks": marks}}, ensure_ascii=False), encoding="utf-8")
    return bd, {"devices": devs}, recs


def _an(sc, **kw):
    """analyze 主遍 → (结果, {"x,y": 设备行}, [暗管来料卡])。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec = _scene(Path(td), sc)
        res = D.analyze(bd, led, rec, None, **kw)
    return res, {f"{r['x']},{r['y']}": r for r in res["devices"]}, [p for p in res["pending"] if p["kind"] == "暗管来料"]


def _mark(no, item, rate, p=T, **kw):
    return dict({"no": no, "p": list(p), "item": item, "rate": rate, "via": "暗管", "merged": False, "source": "manual",
                 "note": "外接暗管出口（设计分析「待确认」里填的）"}, **kw)


def _basic(marks=(), open_=True, links_json=None, anchor=True, fed="清水"):
    """暗管出口 T（外接）→ 管道准入口（没标物品）→ 灌装机 C（要壤晶废液 30、清水 30）；输入端 1 号送 fed（默认清水）30 给 C。"""
    sc = {"devices": [("暗管出口", T, None), ("灌装机", (700, 300), R["两样"])],
          "nodes": {"A": ("准入口", (420, 300))},
          "gates": [(1, (40, 600), fed, 30, 1)],
          "links": [(0, "A", "pipe"), ("A", 1, "pipe")],
          "anchors": [(BP, "暗管出口", (T[0] + 5, T[1] - 4), open_)] if anchor else [],
          "marks": list(marks)}
    if links_json is not None:
        sc["links_json"] = {BP: links_json}
    return sc


def test_classify_outlets():
    """带 open 的 → 外接；links 里没配上的、配上了却没对到底账设备的 → 漏配；配上对的 → 不出现；
    蓝图没有 links.json → 缺文件；附近没有锚点的 → 蓝图外；多口按名字对；配上对的设备占着的锚点不归旁边那台；
    bp、p_bp 是锚点的（转了 90° 的蓝图也对）。"""
    J = (8, -6)  # 底账标签离锚点（暗管正中）偏一点

    def at(c):
        return (c[0] + J[0], c[1] + J[1])
    A, B, TI, TO, G, GI, F2, F, D2, A2, E = ((300, 300), (300, 600), (600, 300), (600, 600), (900, 600), (900, 300), (1200, 300),
                                             (1200, 600), (1700, 300), (1700, 600), (300, 1000))
    H = (TO[0] + 35, TO[1])
    devs = [("暗管出口", at(A), None), ("暗管出口", at(B), None), ("暗管入口", at(TI), None), ("暗管出口", at(TO), None),
            ("暗管出口", at(G), None), ("多口暗管出口", at(F2), None), ("多口暗管出口", at(F), None), ("暗管出口", at(D2), None),
            ("暗管出口", at(A2), None), ("暗管出口", E, None), ("暗管出口", H, None)]
    sc = {"devices": devs,
          "bps": {BP: ((0, 0), 0), "乙图": ((1500, 0), 90)},
          "anchors": [(BP, "暗管出口", A, True), (BP, "暗管出口", B, False), (BP, "暗管入口", TI, False), (BP, "暗管出口", TO, False),
                      (BP, "暗管出口", G, False), (BP, "暗管入口", GI, False), (BP, "多口暗管出口", F2, True), (BP, "暗管出口", F, True),
                      ("乙图", "暗管出口", D2, False), ("乙图", "暗管出口", A2, True)],
          # 测试图不带 open 的：0 = B、1 = TI、2 = TO、3 = G、4 = GI（GI 底账里没有这台）
          "links_json": {BP: [(1, 2), (3, 4)]}}
    with tempfile.TemporaryDirectory() as td:
        bd, led, _rec = _scene(Path(td), sc)
        tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
        canvas = [V.far_to_canvas(tf, d["x"], d["y"]) for d in led["devices"]]
        tp = D.tunnel_pairs(bd, led["devices"], canvas)
        got = D.tunnel_outlets(bd, led["devices"], canvas, tp)
        anc = {(b, a["name"], tuple(round(v) for v in V.preview_to_canvas(tf, b, None, *a["p"]))): a["p"]
               for b in (BP, "乙图") for a in json.loads((bd.parent.parent / "bp" / b / "anchors.json").read_text(encoding="utf-8"))["anchors"]}
    assert tp == [(2, 3)], tp
    want = {0: ("外接", BP, A, "暗管出口"), 1: ("漏配", BP, B, "暗管出口"), 4: ("漏配", BP, G, "暗管出口"),
            5: ("外接", BP, F2, "多口暗管出口"), 6: ("蓝图外", None, None, None), 7: ("缺文件", "乙图", D2, "暗管出口"),
            8: ("外接", "乙图", A2, "暗管出口"), 9: ("蓝图外", None, None, None), 10: ("蓝图外", None, None, None)}
    assert set(got) == set(want), (sorted(got), sorted(want))  # 配上对的 TO（3 号）、暗管入口不出现
    for i, (cls, bp, c, nm) in want.items():
        g = got[i]
        assert g["cls"] == cls and g["bp"] == bp, (i, g, cls, bp)
        assert g["p_bp"] == (anc[(bp, nm, c)] if c else None), (i, g)
    assert D.tunnel_ports("暗管出口") == 1 and D.tunnel_ports("多口暗管出口") == 2


def test_unfilled_keeps_full_pipe():
    """没填：下游到手和原来一样（满管 120，按缺口分成壤晶废液），依据写「没填量、按满管估」，出一张没答的卡；
    蓝图外的暗管出口照旧是原来的写法、不出卡；漏配的出卡，叫重读。"""
    res, dv, cards = _an(_basic())
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 120.0, "清水": 30.0} and c["rate"] == 1.0, c
    assert any(b.startswith("外接暗管出口（物品没看到，按设备缺口分，没填量、按满管估）120/分") for b in c["basis"]), c["basis"]
    assert len(cards) == 1, res["pending"]
    k = cards[0]
    assert k["key"] == "暗管出口@300,300" and k["device"] == "暗管出口[300, 300]" and k["c"] == [300, 300] and k["cls"] == "外接", k
    assert k["bp"] == BP and k["p_bp"] == _to_bp((0, 0), 0, (305, 296)) and k["ports"] == 1, k
    assert k["answer"] is None and k["applied"] is False and k["mark"] is None, k
    assert k["outs"] == [{"to": "灌装机", "items": ["壤晶废液", "清水"], "unclear": False, "gate": "", "at": [420, 300]}], k["outs"]
    assert k["suggest"] == ["壤晶废液", "清水"] and "满送 120/分" in k["msg"] and "请填上" in k["msg"], k
    # 同一个场景，暗管附近没有锚点（蓝图外）：数字一样，依据是原来的写法，不出卡
    _res, dv0, cards0 = _an(_basic(anchor=False))
    assert dv0["700,300"]["got"] == c["got"] and dv0["700,300"]["rate"] == c["rate"], dv0["700,300"]
    assert "暗管出口（物品没看到，按设备缺口分）120/分 → 当作壤晶废液 → 120" in dv0["700,300"]["basis"], dv0["700,300"]["basis"]
    assert cards0 == []
    # 漏配：锚点不带 open、links.json 里没配上
    _res, dv1, cards1 = _an(_basic(open_=False, links_json=[]))
    assert dv1["700,300"]["got"] == c["got"], dv1["700,300"]
    assert any(b.startswith("没配上对的暗管出口（物品没看到，按设备缺口分，没填量、按满管估）") for b in dv1["700,300"]["basis"])
    assert len(cards1) == 1 and cards1[0]["cls"] == "漏配" and "重读" in cards1[0]["msg"] and cards1[0]["answer"] is None, cards1


def test_filled_item_and_rate():
    """标记填壤晶废液 12/分：下游要 30 → 开工 40%；依据写「输入端22号」；卡上 answer、applied 都对。"""
    res, dv, cards = _an(_basic([_mark(22, "壤晶废液", 12)]))
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 12.0, "清水": 30.0} and abs(c["rate"] - 0.4) < 1e-6 and c["state"] == "间歇", c
    assert "输入端22号 壤晶废液（外接暗管出口，作者填 12/分） → 12" in c["basis"], c["basis"]
    assert not any("满管" in b for b in c["basis"]), c["basis"]
    k = cards[0]
    assert k["answer"] == {"no": 22, "item": "壤晶废液", "rate": 12.0} and k["applied"] is True and k["mark"]["no"] == 22, k
    assert "按你填的 壤晶废液 12/分 算（输入端 22 号）" in k["msg"], k["msg"]
    assert not [n for n in res["notes"] if n["kind"].startswith("暗管出口")], res["notes"]


def test_filled_without_item():
    """只填量 12、没填物品：按缺口分（壤晶废液），依据写物品没填。"""
    _res, dv, cards = _an(_basic([_mark(22, "", 12)]))
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 12.0, "清水": 30.0} and abs(c["rate"] - 0.4) < 1e-6, c
    assert any(b.startswith("输入端22号（外接暗管出口，作者填 12/分，物品没填、按设备缺口分）") and "当作壤晶废液 → 12" in b
               for b in c["basis"]), c["basis"]
    assert cards[0]["answer"] == {"no": 22, "item": "", "rate": 12.0}, cards[0]


def test_rate_none_and_zero():
    """只填物品、没填量：照没填算（满管 120），但物品用作者填的（壤晶废液；输入端已经送了壤晶废液，按缺口分会当成清水），
    卡仍算没答（answer 为 None、mark 带物品），不出「输入端量不知道」。填 0：这台平时不来料，不当来源、不报说明，卡算答了。
    只填了清水：pipenet.load_inputs 按一台水泵补成 60/分（输入端的老规矩），暗管出口上的照样当没填（满管 120、卡没答，不写「作者填 60」）。
    填了固体（「输入端」页能填任何物品）：暗管送不了，按没填物品算，卡上提醒。"""
    _res, dv, _cards = _an(_basic(fed="壤晶废液"))
    assert dv["700,300"]["got"] == {"壤晶废液": 30.0, "清水": 120.0}, dv["700,300"]  # 没填：按缺口分成清水
    res, dv, cards = _an(_basic([_mark(22, "壤晶废液", None)], fed="壤晶废液"))
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 150.0, "清水": 0.0} and c["rate"] == 0.0 and c["state"] == "没料", c
    assert any(b.startswith("外接暗管出口（输入端22号 壤晶废液，没填量、按满管估）120/分") for b in c["basis"]), c["basis"]
    k = cards[0]
    assert k["answer"] is None and k["applied"] is False and k["mark"]["item"] == "壤晶废液", k
    assert "按一根管子满送 120/分、物品按你填的壤晶废液来估" in k["msg"] and "还差每分钟多少" not in k["msg"], k["msg"]  # 缺量那句界面另起一行说
    assert not [n for n in res["notes"] if n["kind"] == "输入端量不知道"], res["notes"]
    _res, dv, cards = _an(_basic([_mark(22, "清水", None)], fed="壤晶废液"))
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 30.0, "清水": 120.0} and c["rate"] == 1.0, c
    assert any(b.startswith("外接暗管出口（输入端22号 清水，没填量、按满管估）120/分") for b in c["basis"]), c["basis"]
    assert not any("作者填" in b for b in c["basis"]), c["basis"]
    k = cards[0]
    assert k["answer"] is None and k["applied"] is False and k["mark"]["item"] == "清水" and k["mark"]["rate"] is None, k
    assert "rate_auto" not in k["mark"] and "物品按你填的清水" in k["msg"], k
    _res, dv, cards = _an(_basic([_mark(22, "芽针", 12)]))
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 12.0, "清水": 30.0}, c                         # 按缺口当成壤晶废液，不是一管子芽针
    assert cards[0]["answer"] == {"no": 22, "item": "", "rate": 12.0} and "「芽针」不是液体或气体" in cards[0]["extra"][0], cards[0]
    # 填了固体、没填量（主会话界面实测 2026-11-21）：物品是按缺口当的，依据不能写成「输入端22号 壤晶废液」像作者填的
    _res, dv, cards = _an(_basic([_mark(22, "芽针", None)]))
    c = dv["700,300"]
    assert any(b.startswith("外接暗管出口（输入端22号，物品没填，按设备缺口分，没填量、按满管估）120/分") for b in c["basis"]), c["basis"]
    assert cards[0]["answer"] is None and "「芽针」不是液体或气体" in cards[0]["extra"][0], cards[0]
    res, dv, cards = _an(_basic([_mark(22, "", 0)]))
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 0.0, "清水": 30.0} and c["rate"] == 0.0, c
    assert not any("暗管" in b for b in c["basis"]), c["basis"]
    assert cards[0]["answer"] == {"no": 22, "item": "", "rate": 0.0} and cards[0]["applied"] is True, cards[0]
    assert cards[0]["msg"].startswith("你说过这台平时不来料（输入端 22 号）") and "按你填的" not in cards[0]["msg"], cards[0]["msg"]
    assert not [n for n in res["notes"] if n["kind"] == "输入端量不知道" or n["kind"].startswith("暗管出口")], res["notes"]


def test_gate_mark_wins():
    """标记在准入口 15 px 内（准入口离暗管 40 px，标记离暗管 36 px，也在 45 px 内）：照旧是准入口输入端，不绑暗管，暗管卡没答。"""
    sc = _basic()
    sc["nodes"] = {"A": ("准入口", (340, 300))}
    sc["marks"] = [_mark(22, "壤晶废液", 12, p=(336, 303))]
    assert np.hypot(336 - T[0], 303 - T[1]) < D.TUN_MARK_TOL  # 不看准入口的话会绑到暗管上
    res, dv, cards = _an(sc)
    c = dv["700,300"]
    assert "输入端22号 壤晶废液 12/分 → 12" in c["basis"], c["basis"]
    # 暗管出口没绑标记：照旧满管 120，物品取下游第一个准入口的（输入端标的壤晶废液）。输入端和暗管重复算是已知的老问题，这里不管
    assert any(b.startswith("外接暗管出口（壤晶废液，没填量、按满管估）120/分") for b in c["basis"]), c["basis"]
    assert len(cards) == 1 and cards[0]["answer"] is None and cards[0]["mark"] is None, cards
    assert not [n for n in res["notes"] if n["kind"].startswith("暗管出口")], res["notes"]


def test_paired_outlet_mark_ignored():
    """标在配上对的暗管出口上：不用，出说明（写输入端编号、不写坐标，没有卡就进汇总的提醒）；下游照旧拿对面暗管入口送来的清水 30，不出卡。"""
    TI, TO = (200, 600), (600, 600)
    sc = {"devices": [("暗管入口", TI, None), ("暗管出口", TO, None), ("灌装机", (900, 600), "清水×1 → 测试品×1（2秒）")],
          "gates": [(1, (40, 900), "清水", 30, 0)],
          "links": [(1, 2, "pipe")],
          "anchors": [(BP, "暗管入口", TI, False), (BP, "暗管出口", TO, False)],
          "links_json": {BP: [(0, 1)]},
          "marks": [_mark(22, "清水", 5, p=(TO[0] + 6, TO[1] + 4))]}
    res, dv, cards = _an(sc)
    c = dv["900,600"]
    assert c["got"] == {"清水": 30.0} and c["rate"] == 1.0, c
    assert res["tunnels"] == 1 and cards == [], (res["tunnels"], cards)
    ns = [n for n in res["notes"] if n["kind"] == "暗管出口标记没用上"]
    assert len(ns) == 1 and "输入端 22 号" in ns[0]["msg"] and "对面的暗管入口" in ns[0]["msg"] and "600" not in ns[0]["msg"], ns
    assert ns[0]["msg"] in D.summary(res)["warnings"], D.summary(res)  # 没有卡：放进设计分析上面的提醒


def test_two_marks_one_outlet():
    """一台暗管出口标了两个：按先标的（22 号 12/分）算，说一句（只一条，不随迭代轮数重复）；带几口合计的说一句、按单口算。
    这两种说明挂在这台的卡上（extra），不进汇总的提醒；卡上 marks 按先后列出两个编号。"""
    res, dv, cards = _an(_basic([_mark(22, "壤晶废液", 12), _mark(23, "壤晶废液", 20, p=(T[0] + 12, T[1] - 9))]))
    assert dv["700,300"]["got"]["壤晶废液"] == 12.0, dv["700,300"]
    ns = [n for n in res["notes"] if n["kind"] == "暗管出口标了两个输入端"]
    assert len(ns) == 1 and "22 号、23 号" in ns[0]["msg"] and "按 22 号算" in ns[0]["msg"], ns
    assert cards[0]["answer"]["no"] == 22 and cards[0]["marks"] == [22, 23] and cards[0]["extra"] == [ns[0]["msg"]], cards[0]
    assert "300" not in ns[0]["msg"] and D.summary(res)["warnings"] == [], D.summary(res)
    res, dv, cards = _an(_basic([_mark(22, "壤晶废液", 12, pool={"name": "甲", "total": 50})]))
    assert dv["700,300"]["got"]["壤晶废液"] == 12.0, dv["700,300"]
    ns = [n for n in res["notes"] if n["kind"] == "暗管出口不支持几口合计"]
    assert len(ns) == 1 and "输入端 22 号" in ns[0]["msg"] and cards[0]["extra"] == [ns[0]["msg"]], res["notes"]


def _multi(rate, name="多口暗管出口", need=("污水30", "污水20")):
    """暗管出口 T（外接）接出两路：→ 准入口 A → C1、→ 准入口 B → C2。"""
    return {"devices": [(name, T, None), ("灌装机", (700, 200), R[need[0]]), ("灌装机", (700, 400), R[need[1]])],
            "nodes": {"A": ("准入口", (420, 200)), "B": ("准入口", (420, 400))},
            "links": [(0, "A", "pipe"), ("A", 1, "pipe"), (0, "B", "pipe"), ("B", 2, "pipe")],
            "anchors": [(BP, name, T, True)],
            "marks": [_mark(22, "污水", rate, via="多口暗管")]}


def test_multi_outlet_split():
    """多口暗管出口两路，下游各要 30、20：填 40 → 20 / 20；填 50 → 30 / 20（按下游各路吃得下的水位分）。
    单口暗管出口追线挂了两路：合计最多 120（填 200、下游各要 150 → 60 / 60）。卡上写多口最多 240。
    多口暗管出口只接了一路：填 200 → 这一路一根管子最多 120。没填时卡上照实写每一路按满管 120：两路合计 240，单口的说实际最多 120。"""
    for rate, want in ((40, (20.0, 20.0)), (50, (30.0, 20.0))):
        _res, dv, cards = _an(_multi(rate))
        got = (dv["700,200"]["got"]["污水"], dv["700,400"]["got"]["污水"])
        assert all(abs(a - b) < 1e-6 for a, b in zip(got, want)), (rate, got, want)
        assert any("多口暗管出口主路 1/2" in b for b in dv["700,200"]["basis"]), dv["700,200"]["basis"]
    k = cards[0]
    assert k["ports"] == 2 and k["answer"]["rate"] == 50.0 and [o["at"] for o in k["outs"]] == [[420, 200], [420, 400]], k
    _res, _dv, cards = _an(dict(_multi(None), marks=[]))
    assert "有 2 个口" in cards[0]["msg"] and "最多 240" in cards[0]["msg"], cards[0]["msg"]
    assert "接出去的 2 路每路按满管 120/分算（合计 240/分）" in cards[0]["msg"], cards[0]["msg"]
    _res, _dv, cards = _an(dict(_multi(None, name="暗管出口"), marks=[]))
    assert "接出去的 2 路每路按满管 120/分算（合计 240/分，可一根管子实际最多 120）" in cards[0]["msg"], cards[0]["msg"]
    one = {"devices": [("多口暗管出口", T, None), ("灌装机", (700, 300), R["污水150"])], "nodes": {"A": ("准入口", (420, 300))},
           "links": [(0, "A", "pipe"), ("A", 1, "pipe")], "anchors": [(BP, "多口暗管出口", T, True)],
           "marks": [_mark(22, "污水", 200, via="多口暗管")]}
    _res, dv, _cards = _an(one)
    assert abs(dv["700,300"]["got"]["污水"] - 120.0) < 1e-6, dv["700,300"]
    _res, dv, _cards = _an(_multi(200, name="暗管出口", need=("污水150", "污水150")))
    got = (dv["700,200"]["got"]["污水"], dv["700,400"]["got"]["污水"])
    assert all(abs(a - 60.0) < 1e-6 for a in got), got
    assert any("作者填 200/分，1 个口最多 120" in b for b in dv["700,200"]["basis"]), dv["700,200"]["basis"]


def _feed_first():
    """别人的武陵城扩容反应池 (1570,1196) 的样子：暗管出口 T（作者填壤晶废液 12）接两路：→ 准入口 A → C（要 30）、
    → 污水准入口 W → 灌装机 S（这一路物品不对，一点不过）；隔壁的生产设备 P 产壤晶废液 30，两路：直连 C、经管道分流器 N 到 C2（只要 6）。
    P 排在 T 前面、两台都是两路：不按「作者填的先分」，P 先分会把 C 灌到 24、作者填的 12 只用上 6。"""
    return {"devices": [("灌装机", (300, 700), R["做废液"]), ("暗管出口", T, None), ("灌装机", (700, 300), R["废液30"]),
                        ("灌装机", (300, 100), R["污水30"]), ("灌装机", (700, 900), R["废液6"])],
            "nodes": {"A": ("准入口", (420, 300)), "W": ("准入口", (300, 180), "污水"), "N": ("管道分流器", (500, 900))},
            "gates": [(1, (40, 700), "清水", 30, 0)],
            "links": [(1, "A", "pipe"), ("A", 2, "pipe"), (1, "W", "pipe"), ("W", 3, "pipe"), (0, 2, "pipe"), (0, "N", "pipe"),
                      ("N", 4, "pipe")],
            "anchors": [(BP, "暗管出口", T, True)],
            "marks": [_mark(22, "壤晶废液", 12)]}


def test_author_feed_first():
    """作者填的 12 和隔壁设备产物 30 同进一台要 30 的设备：依据先写暗管 12、再写产物 18（作者填的外接来料先分）。
    卡上「它接到」照样列出接到污水准入口的那一路，写明是污水准入口。"""
    _res, dv, cards = _an(_feed_first())
    c = dv["700,300"]
    assert c["got"] == {"壤晶废液": 30.0} and c["rate"] == 1.0, c
    tun = [b for b in c["basis"] if b.startswith("输入端22号")]
    prod = [b for b in c["basis"] if b.startswith("灌装机产 壤晶废液")]
    assert tun == ["输入端22号 壤晶废液（外接暗管出口，作者填 12/分） → 暗管出口主路 1/2 → 12"], c["basis"]
    assert len(prod) == 1 and prod[0].endswith("→ 18") and c["basis"].index(tun[0]) < c["basis"].index(prod[0]), c["basis"]
    assert dv["700,900"]["got"] == {"壤晶废液": 6.0}, dv["700,900"]
    outs = cards[0]["outs"]
    assert [o["to"] for o in outs] == ["灌装机", "污水准入口 → 灌装机"] and outs[1]["gate"] == "污水", outs
    assert [o["at"] for o in outs] == [[420, 300], [300, 180]], outs


def test_main_blocked_not_applied():
    """作者填了量的暗管出口接一条主路、一条经 1 进 1 出汇流器的备路：「主路堵死」情景只认设备出料口，暗管照样先给主路。"""
    sc = {"devices": [("暗管出口", T, None), ("灌装机", (700, 300), R["污水30"]), ("灌装机", (700, 600), R["污水30"])],
          "nodes": {"M": ("管道汇流器", (450, 600))},
          "links": [(0, 1, "pipe"), (0, "M", "pipe"), ("M", 2, "pipe")],
          "anchors": [(BP, "暗管出口", T, True)],
          "marks": [_mark(22, "污水", 40)]}
    for blocked in (False, True):
        _res, dv, _cards = _an(sc, main_blocked=blocked)
        got = (dv["700,300"]["got"]["污水"], dv["700,600"]["got"]["污水"])
        assert got == (30.0, 10.0), (blocked, got)
        assert any("暗管出口备路 1/1" in b for b in dv["700,600"]["basis"]), dv["700,600"]["basis"]


if __name__ == "__main__":
    sys.exit(run([v for k, v in sorted(globals().items()) if k.startswith("test_")]))
