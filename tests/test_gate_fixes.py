"""作者填的准入口限速（0.10.13 功能一的计算部分：fixes.json 的 gates 段、档案读数按远景坐标认口、准入口表 gates_view.json）。
在临时目录里造小据点，直接调 design.analyze / run_base，不读本机实测数据。

小据点的坐标都按「蓝图里」写：底账远景坐标就是它，logistics.json 里准入口的 p_bp 也是它；off = 重新拼合后拼合图整体挪了多少
（transform 的 F0 = -off：远景坐标不变，拼合图上的设备、物流件、准入口跟着挪），用来测「拼合图坐标变了、键不变」。
都是 2 秒一份的配方：一条满速每种原料 30/分。
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
POOL2 = "池产物：芽针溶液(50)/锦草溶液(50)"
R = {
    "碎芽针": "芽针×1 → 芽针粉末×2（2秒）",
    "芽针瓶": "赤铜瓶×1 + 芽针溶液×1 → 赤铜瓶(芽针溶液)×1（2秒）",
    "锦草瓶": "赤铜瓶×1 + 锦草溶液×1 → 赤铜瓶(锦草溶液)×1（2秒）",
}
# 一条线：输入端 1 号（芽针 30）→ 物品准入口 L → 粉碎机（要芽针 30）
LINE = {"devices": [("粉碎机", (400, 300), R["碎芽针"])],
        "nodes": {"L": ("准入口", (200, 300), "物品")},
        "gates": [(1, (40, 300), "芽针", 30, "L")],
        "links": [("L", 0, "belt")]}
KEY_L = D.gate_key(BP, (200, 300))
TOP_KEYS = {"devices", "gates", "ports", "recipe_fixes", "tunnels", "notes", "issues", "inferred_links", "reread", "pending", "iters",
            "summary"}  # 0.10.12 的 design.json 顶层键：准入口表另存，不能多出键


def _scene(td: Path, sc: dict, off=(0, 0), limits=None, gates_fix=None, K=1.0):
    """→ (据点目录, 底账, 配方表, 档案准入口限速, {节点键: 物流节点号})。
    sc：devices [(名字, 坐标, 配方)]、nodes {键: (类型, 坐标, 物品 / 管道[, 蓝图名, 蓝图里的坐标])}（准入口默认在 BP、p_bp = 坐标）、
    gates [(编号, 坐标, 物品, 流速, 去向)]（输入端）、links [(a, b, 层[, 只走的物品])]（整数 = 第几台设备，字符串 = 节点键 / g<编号>）。
    limits {节点键: {档案行的字段}}：档案 gate_limits 的行，默认 p = 现在的拼合图坐标、far = 远景坐标，给 None 就去掉这个字段。
    gates_fix：fixes.json 这个据点的 gates 段（None = 不写 fixes.json）。
    K：transform 的比例（拼合图 = (远景 - F0) / K）。默认 1（拼合图坐标 = 远景坐标 + off）；给真实据点那样的 0.5787 时远景和拼合图坐标
    差得远，按 far 认口、导出时 far = F0 + K·c 写反了（乘成除）测得出来。"""
    proj = td / "项目"
    bd = proj / "base" / "测试"
    bd.mkdir(parents=True)
    if K == 1:  # 蓝图里的坐标 → 现在的拼合图坐标
        cv = lambda p: [p[0] + off[0], p[1] + off[1]]  # noqa: E731
        F0 = [-off[0], -off[1]]
    else:
        cv = lambda p: [round(p[0] / K + off[0]), round(p[1] / K + off[1])]  # noqa: E731  拼合图坐标是整数（logistics 按它裁图标）
        F0 = [-off[0] * K, -off[1] * K]
    devs = [{"name": n, "x": p[0], "y": p[1]} for n, p, _ in sc["devices"]]
    recs = [{"name": n, "x": p[0], "y": p[1], "recipe": rc} for n, p, rc in sc["devices"] if rc]
    nodes, links, marks, lg, nid, bps = [], [], [], [], {}, {BP}

    def add(key, tp, p, kind, bp=BP, p_bp=None):
        nid[key] = len(nodes)
        nodes.append({"id": len(nodes), "type": tp, "p": cv(p), "c": cv(p), "kind": kind})
        if tp == "准入口":
            lg.append({"type": "准入口", "p": cv(p), "kind": kind, "bp": bp, "p_bp": list(p_bp or p)})
            bps.add(bp)
    for key, (tp, p, kind, *where) in sc.get("nodes", {}).items():
        add(key, tp, p, kind, *where)
    glinks = []
    for no, p, item, rate, dst in sc["gates"]:
        layer = "pipe" if D._is_fluid(item) else "belt"
        add(f"g{no}", "准入口", p, "管道" if layer == "pipe" else "物品")
        marks.append({"no": no, "p": cv(p), "item": item, "rate": rate, "kind": "管道" if layer == "pipe" else "物品"})
        glinks.append((f"g{no}", dst, layer))

    def pos(k):
        return cv(sc["devices"][k][1]) if isinstance(k, int) else list(nodes[nid[k]]["p"])
    for a, b, layer, *its in glinks + list(sc["links"]):
        lk = {"layer": layer, "path": [pos(a), pos(b)], "basis": ["合成"]}
        if its:
            lk["items"] = its[0]
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
    (bd / "flow.json").write_text(json.dumps({"nodes": nodes, "links": links, "issues": [], "structures": []}, ensure_ascii=False),
                                  encoding="utf-8")
    (bd / "logistics.json").write_text(json.dumps(lg, ensure_ascii=False), encoding="utf-8")
    (bd / "transform.json").write_text(json.dumps({"K": K, "F0": F0, "bps": {b: {"t": [0, 0], "rot": 0} for b in sorted(bps)}},
                                                  ensure_ascii=False), encoding="utf-8")
    side = 1000 if K == 1 else int(1000 / K) + 1  # 拼合图要盖住所有设备
    V.wr(bd / "composite.png", np.full((side, side, 3), 232, np.uint8))
    (proj / "inputs.json").write_text(json.dumps({"测试": {"marks": marks}}, ensure_ascii=False), encoding="utf-8")
    if gates_fix is not None:
        (proj / "fixes.json").write_text(json.dumps({"测试": {"gates": gates_fix}}, ensure_ascii=False), encoding="utf-8")
    rows = []
    for k, extra in (limits or {}).items():
        base = sc["nodes"][k][1] if k in sc.get("nodes", {}) else next(p for no, p, *_ in sc["gates"] if f"g{no}" == k)
        row = {"p": cv(base), "far": list(base), "kind": "物品", "bp": BP, "limit_on": True}
        row.update(extra)
        rows.append({f: v for f, v in row.items() if v is not None})
    gl = {"area": "测试", "time": "20261109-011253", "rows": rows} if limits else None
    return bd, {"devices": devs}, recs, gl, nid


def _fix(p_bp, limit, count=None, bp=BP, **kw):
    return dict({"bp": bp, "p_bp": list(p_bp), "kind": "物品", "item": "芽针", "limit": limit, "count": count, "at": "2026-11-21 20:00:00"}, **kw)


def _an(sc=LINE, **kw):
    """analyze 主遍 → (结果, {节点键: 准入口表那一行}, {"x,y": 设备行})。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl, nid = _scene(Path(td), sc, **kw)
        res = D.analyze(bd, led, rec, gl)
    gv = {r["gate"]: r for r in res["gate_view"]["gates"]}
    return res, {k: gv[i] for k, i in nid.items() if i in gv}, {f"{r['x']},{r['y']}": r for r in res["devices"]}


def test_author_limit_caps_downstream():
    """作者给线内的口填限速 18：下游粉碎机 60%，依据写「准入口限速 18（作者填）」；准入口表这一行
    通过 18、挡下 12、下游 30（从出边算，不含自己的限速：从口本身算会被 18 截住）。"""
    res, gv, dv = _an(gates_fix={KEY_L: _fix((200, 300), 18)})
    r = dv["400,300"]
    assert abs(r["rate"] - 0.6) < 1e-6 and r["got"]["芽针"] == 18, r
    assert any("准入口限速 18（作者填）" in b for b in r["basis"]), r["basis"]
    L = gv["L"]
    assert L["flow"] == {"芽针": 18} and L["held"] == {"芽针": 12}, L
    assert L["downstream"] == 30, L  # 改回 absorb(口本身) 会是 18
    assert L["key"] == KEY_L and L["src"] == "作者" and L["limit"] == 18 and L["count"] is None and L["read"] is None, L
    assert L["bp"] == BP and L["p_bp"] == [200, 300] and L["kind"] == "物品" and L["input_no"] is None, L
    assert [d["name"] for d in L["to"]] == ["粉碎机"] and L["to"][0]["c"] == [400, 300] and L["from"] == [], L
    g1 = gv["g1"]  # 输入端：带编号和作者填的流速，往下穿过 L 找到粉碎机
    assert g1["input_no"] == 1 and g1["input_rate"] == 30 and g1["flow"] == {"芽针": 30} and g1["held"] == {}, g1
    assert g1["src"] is None and g1["key"] == D.gate_key(BP, (40, 300)) and [d["name"] for d in g1["to"]] == ["粉碎机"], g1
    assert res["gate_view"]["stale"] == [] and not any(n["kind"] == "准入口设置对不上" for n in res["notes"]), res["notes"]


def test_author_overrides_archive():
    """档案读到限速 12；作者填 18 → 按 18；作者存两项都是 null（说没开）→ 不限速；撤销（删掉记录）→ 回到 12。
    档案读到的依据措辞不变（没有「作者填」）。"""
    lim = {"L": {"limit": 12, "count_on": False}}
    _, gv, dv = _an(limits=lim)
    assert abs(dv["400,300"]["rate"] - 0.4) < 1e-6, dv["400,300"]
    assert any(b.endswith("准入口限速 12 → 12") for b in dv["400,300"]["basis"]), dv["400,300"]["basis"]
    assert not any("作者填" in b for b in dv["400,300"]["basis"])
    assert gv["L"]["src"] == "游戏" and gv["L"]["limit"] == 12 and gv["L"]["read"]["limit"] == 12 and gv["L"]["read"]["limit_on"], gv["L"]
    res, gv, dv = _an(limits=lim, gates_fix={KEY_L: _fix((200, 300), 18)})
    assert abs(dv["400,300"]["rate"] - 0.6) < 1e-6 and any("准入口限速 18（作者填）" in b for b in dv["400,300"]["basis"]), dv["400,300"]
    assert gv["L"]["src"] == "作者" and gv["L"]["limit"] == 18 and gv["L"]["read"]["limit"] == 12, gv["L"]  # 读数照样给，界面写「游戏里读到 12」
    assert res["gate_view"]["read_time"] == "20261109-011253"
    _, gv, dv = _an(limits=lim, gates_fix={KEY_L: _fix((200, 300), None, None)})
    assert dv["400,300"]["rate"] == 1.0 and not any("准入口限速" in b for b in dv["400,300"]["basis"]), dv["400,300"]
    assert gv["L"]["src"] == "作者" and gv["L"]["limit"] is None and gv["L"]["count"] is None and gv["L"]["downstream"] == 30, gv["L"]
    _, gv, dv = _an(limits=lim, gates_fix={})  # 撤销 = 删掉这条记录
    assert abs(dv["400,300"]["rate"] - 0.4) < 1e-6 and gv["L"]["src"] == "游戏", (dv["400,300"], gv["L"])
    # 作者填的限制通过数量（0.10.13 A2）：线内的口也按一次性算，放满就不再放行，下游长期按不送料算（0.0）；
    # 准入口段和游戏里读的一样写「按一次性备料算」，标「作者填」，不再写「暂时没按它算」
    res, gv, dv = _an(limits=lim, gates_fix={KEY_L: _fix((200, 300), None, 20)})
    assert gv["L"]["count"] == 20 and gv["L"]["limit"] is None and dv["400,300"]["rate"] == 0.0, (gv["L"], dv["400,300"])
    ent = next(e for e in res["gates"] if e["gate"] == gv["L"]["gate"])
    assert ent["count"] == {"limit": 20, "passed": None} and ent["msg"][0] == "限制通过数量 20（作者填）：放满后不再放行，按一次性备料算", ent
    assert "暂时没按它算" not in "".join(ent["msg"]) and ent["downstream"] == 0.0, ent


def test_key_survives_restitch():
    """重新拼合：拼合图整体挪了 (+100,+50)、p_bp 不变 → 照样认得出；p_bp 漂 10 px → 认得出，表上的键还是那条记录的；
    漂 20 px、换了蓝图名 → 对不上，不参加计算，notes 里有「准入口设置对不上」；两条记录对到同一个口 → 近的那条算数。"""
    for off in ((0, 0), (100, 50)):
        _, gv, dv = _an(off=off, gates_fix={KEY_L: _fix((200, 300), 18)})
        r = dv["400,300"]
        assert abs(r["rate"] - 0.6) < 1e-6 and r["c"] == [400 + off[0], 300 + off[1]], (off, r)
        assert gv["L"]["c"] == [200 + off[0], 300 + off[1]] and gv["L"]["key"] == KEY_L, gv["L"]
    drift = D.gate_key(BP, (210, 290))
    _, gv, dv = _an(off=(100, 50), gates_fix={drift: _fix((210, 290), 18)})
    assert abs(dv["400,300"]["rate"] - 0.6) < 1e-6 and gv["L"]["key"] == drift, gv["L"]  # 改了存回同一条，不会多出一条
    for name, rec, why in (("漂 20", _fix((220, 300), 18), "附近没有准入口"), ("换蓝图名", _fix((200, 300), 18, bp="别的图"), "不在这个据点")):
        res, gv, dv = _an(off=(100, 50), gates_fix={"旧键": rec})
        assert dv["400,300"]["rate"] == 1.0 and gv["L"]["src"] is None and gv["L"]["key"] == KEY_L, (name, dv["400,300"], gv["L"])
        st = res["gate_view"]["stale"]
        assert len(st) == 1 and st[0]["key"] == "旧键" and why in st[0]["why"] and st[0]["limit"] == 18 and st[0]["bp"] == rec["bp"], (name, st)
        note = [n for n in res["notes"] if n["kind"] == "准入口设置对不上"]
        assert len(note) == 1 and note[0]["keys"] == ["旧键"] and "1 个" in note[0]["msg"], (name, res["notes"])
    res, gv, dv = _an(gates_fix={"远的": _fix((205, 300), 6), KEY_L: _fix((200, 300), 18)})
    assert abs(dv["400,300"]["rate"] - 0.6) < 1e-6 and gv["L"]["key"] == KEY_L, (dv["400,300"], gv["L"])
    st = res["gate_view"]["stale"]
    assert [s["key"] for s in st] == ["远的"] and "对到同一个准入口" in st[0]["why"], st
    res, _, dv = _an(gates_fix={KEY_L: _fix((200, 300), "18")})  # 手改坏的记录：不当真，列进对不上
    assert dv["400,300"]["rate"] == 1.0 and "不是数字" in res["gate_view"]["stale"][0]["why"], res["gate_view"]["stale"]
    # 两张蓝图：各自的预览坐标是各自的，L（测试图）和 M（图乙）的 p_bp 一样。作者给图乙那个口填 18，只认图乙里的 M，
    # 不认节点号更小、坐标一样的 L（对位置时不看蓝图名就会认错）
    sc = {"devices": [("粉碎机", (400, 300), R["碎芽针"]), ("粉碎机", (400, 500), R["碎芽针"])],
          "nodes": {"L": ("准入口", (200, 300), "物品"), "M": ("准入口", (200, 500), "物品", "图乙", (200, 300))},
          "gates": [(1, (40, 300), "芽针", 30, "L"), (2, (40, 500), "芽针", 30, "M")],
          "links": [("L", 0, "belt"), ("M", 1, "belt")]}
    res, gv, dv = _an(sc, gates_fix={D.gate_key("图乙", (200, 300)): _fix((200, 300), 18, bp="图乙")})
    assert dv["400,300"]["rate"] == 1.0 and abs(dv["400,500"]["rate"] - 0.6) < 1e-6, (dv["400,300"], dv["400,500"])
    assert gv["M"]["src"] == "作者" and gv["L"]["src"] is None and gv["L"]["key"] == KEY_L and res["gate_view"]["stale"] == [], gv


def test_archive_far_matching():
    """档案行按远景坐标 far 认口：p 故意写错、far 对 → 认上（重新拼合后拼合图坐标变了的老读数）；没有 far 的老行照旧按 p 认。
    两条线：L1 限速 12（p 错、far 对）、L2 限速 6（没有 far、p 对），拼合图挪过 (+100,+50)，所以 p 和 far 不一样。
    再按真实据点的比例 K = 0.5787 算一遍（far 换拼合图要除以 K，写成乘就认不上）。"""
    sc = {"devices": [("粉碎机", (400, 300), R["碎芽针"]), ("粉碎机", (400, 500), R["碎芽针"])],
          "nodes": {"L1": ("准入口", (200, 300), "物品"), "L2": ("准入口", (200, 500), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, "L1"), (2, (40, 500), "芽针", 30, "L2")],
          "links": [("L1", 0, "belt"), ("L2", 1, "belt")]}
    for k in (1.0, 0.5787):
        _, gv, dv = _an(sc, off=(100, 50), K=k, limits={"L1": {"limit": 12, "p": [5, 5]}, "L2": {"limit": 6, "far": None}})
        assert abs(dv["400,300"]["rate"] - 0.4) < 1e-6 and gv["L1"]["src"] == "游戏", (k, dv["400,300"], gv["L1"])
        assert abs(dv["400,500"]["rate"] - 0.2) < 1e-6 and gv["L2"]["src"] == "游戏", (k, dv["400,500"], gv["L2"])


def test_downstream_open_and_read_errors():
    """准入口表的「下游满速要」：下游有一路去仓库、绕回上游（回环）时看不出要多少，写 None（不写 absorb 给的 0 或被带子截住的 30）；
    只到设备的照常算。档案里读失败的行（read_area 带 error 的）不当读数：不标「游戏」、不按它限速，只记下 read_error。"""
    sc = {"devices": [("粉碎机", (400, 300), R["碎芽针"]), ("仓库存货口", (400, 500), None), ("粉碎机", (700, 700), R["碎芽针"]),
                      ("仓库存货口", (700, 800), None), ("粉碎机", (700, 900), R["碎芽针"])],
          "nodes": {"A": ("准入口", (200, 500), "物品"), "M": ("汇流器", (300, 700), None), "B": ("准入口", (400, 700), "物品"),
                    "S": ("分流器", (500, 700), None), "C": ("准入口", (300, 900), "物品"), "T": ("分流器", (500, 900), None)},
          "gates": [(1, (40, 300), "芽针", 30, 0), (2, (40, 500), "芽针", 30, "A"), (3, (40, 700), "芽针", 30, "M"),
                    (4, (40, 900), "芽针", 30, "C")],
          "links": [("A", 1, "belt"),                                                      # A → 仓库存货口
                    ("M", "B", "belt"), ("B", "S", "belt"), ("S", 2, "belt"), ("S", "M", "belt"),  # 汇流器 → B → 分流器 → 粉碎机 + 绕回汇流器
                    ("C", "T", "belt"), ("T", 4, "belt"), ("T", 3, "belt")]}              # C → 分流器 → 粉碎机 + 仓库存货口
    err = {"limit_on": None, "limit": None, "error": "画面里没找到准入口图标（可能被挡住、或基地里这里不是准入口）"}
    res, gv, dv = _an(sc, limits={"A": {"limit": 12}, "C": dict(err, limit_on=True, limit=6)})
    assert gv["A"]["downstream"] is None and gv["A"]["flow"], gv["A"]
    assert gv["B"]["downstream"] is None and gv["C"]["downstream"] is None, (gv["B"], gv["C"])
    assert gv["g1"]["downstream"] == 30, gv["g1"]                                          # 只到设备：照常算
    assert gv["A"]["src"] == "游戏" and gv["A"]["read_error"] is None, gv["A"]
    c = gv["C"]
    assert c["src"] is None and c["read"] is None and c["limit"] is None and "没找到" in c["read_error"], c  # 读失败的不按它限速
    assert not any("准入口限速 6" in b for b in dv["700,900"]["basis"]), dv["700,900"]["basis"]


def test_author_keeps_read_passed():
    """输入端游戏里读到限制通过数量 20、已经放满（累计 20）→ 不来料。作者只改限速（限制通过数量照读到的 20 一起存）→ 照样不来料，
    读到的累计数沿用（准入口段写「累计 20」）；作者把限制通过数量改成别的数 → 没有累计数，0.10.13 A2 起照样按一次性算、不来料
    （A2 以前照限速 24 来料，开工率 0.8）；作者说限制通过数量没开、只开限速 24 → 照限速来料。"""
    lim = {"g1": {"limit_on": False, "count_on": True, "count_limit": 20, "passed": 20}}
    _, gv, dv = _an(limits=lim)
    assert dv["400,300"]["rate"] == 0.0, dv["400,300"]
    key = D.gate_key(BP, (40, 300))
    res, gv, dv = _an(limits=lim, gates_fix={key: _fix((40, 300), 24, 20)})
    assert dv["400,300"]["rate"] == 0.0 and gv["g1"]["src"] == "作者" and gv["g1"]["count"] == 20, (dv["400,300"], gv["g1"])
    ent = next(e for e in res["gates"] if e["gate"] == gv["g1"]["gate"])
    assert ent["msg"][0] == "限制通过数量 20（作者填，累计 20）：放满后不再放行，按一次性备料算", ent["msg"]
    _, gv, dv = _an(limits=lim, gates_fix={key: _fix((40, 300), 24, 30)})
    assert dv["400,300"]["rate"] == 0.0 and gv["g1"]["count"] == 30, (dv["400,300"], gv["g1"])
    _, gv, dv = _an(limits=lim, gates_fix={key: _fix((40, 300), 24, None)})
    assert abs(dv["400,300"]["rate"] - 0.8) < 1e-6, dv["400,300"]


def _archive(td: Path, led: dict, rec: list, gl: dict | None) -> Path:
    arch = td / "档案"
    (arch / "ledger" / "gate_limits").mkdir(parents=True)
    (arch / "ledger" / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
    (arch / "ledger" / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
    if gl:
        (arch / "ledger" / "gate_limits" / "测试.json").write_text(json.dumps(gl, ensure_ascii=False), encoding="utf-8")
    return arch


def test_gate_view_not_in_design():
    """走 run_base：准入口表写成 base/<据点>/gates_view.json，design.json 里没有它、顶层键和 0.10.12 一样；
    档案读数（限速 12、读的时间）和作者填的（18）都进表，结果按作者填的算。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl, nid = _scene(Path(td), LINE, limits={"L": {"limit": 12}}, gates_fix={KEY_L: _fix((200, 300), 18)})
        res = D.run_base(bd, _archive(Path(td), led, rec, gl), "测试")
        design = json.loads((bd / "design.json").read_text(encoding="utf-8"))
        gv = json.loads((bd / "gates_view.json").read_text(encoding="utf-8"))
    assert "gate_view" not in res and set(design) == TOP_KEYS, sorted(design)
    assert design["devices"][0]["rate"] == 0.6, design["devices"][0]
    assert set(gv) == {"gates", "stale", "read_time"} and gv["stale"] == [] and gv["read_time"] == "20261109-011253", gv.keys()
    rows = {r["gate"]: r for r in gv["gates"]}
    assert len(rows) == 2, rows
    L = rows[nid["L"]]
    assert L["flow"] == {"芽针": 18} and L["held"] == {"芽针": 12} and L["downstream"] == 30 and L["src"] == "作者", L
    assert L["read"] == {"limit_on": True, "limit": 12, "count_on": None, "count_limit": None, "passed": None}, L["read"]
    # shut：开了限制通过数量的口挡的料（0.10.13 A2 审查加的，gates_view.json 自己的字段，design.json 不变）；只开限速的口是空的
    assert set(L) == {"key", "gate", "c", "bp", "p_bp", "kind", "item", "item_how", "flow", "held", "shut", "downstream", "limit", "count",
                      "src", "read", "read_error", "input_no", "input_rate", "from", "to"}, sorted(L)
    assert L["shut"] == [], L


def test_parallel_pool_respects_author_limit():
    """并行池子的一条原料出口过作者填的限速口（test_pool_parallel.test_structural_cap_on_one_recipe 的场景，档案限速换成作者填 18）：
    只芽针那条受限（60%），锦草那条满速；和档案里读到 18 的结果一样。"""
    sc = {"devices": [("反应池", (300, 300), POOL2), ("灌装机", (700, 220), R["芽针瓶"]), ("灌装机", (560, 380), R["锦草瓶"])],
          "nodes": {"L": ("准入口", (500, 220), "管道")},
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                    (4, (700, 60), "赤铜瓶", 30, 1), (5, (600, 540), "赤铜瓶", 30, 2)],
          "links": [(0, "L", "pipe", ["芽针溶液"]), ("L", 1, "pipe"), (0, 2, "pipe", ["锦草溶液"])]}
    got = {}
    for how, kw in (("作者", {"gates_fix": {D.gate_key(BP, (500, 220)): _fix((500, 220), 18, kind="管道", item="")}}),
                    ("档案", {"limits": {"L": {"limit": 18, "kind": "管道"}}})):
        res, gv, dv = _an(sc, **kw)
        pool = dv["300,300"]
        assert [c["rate"] for c in pool["channels"]] == [0.6, 1.0], (how, pool["channels"])
        assert [c["struct_cap"] for c in pool["channels"]] == [0.6, 1.0] and pool["basis"][0].startswith("出料受限："), (how, pool)
        assert abs(dv["700,220"]["rate"] - 0.6) < 1e-6 and dv["560,380"]["rate"] == 1.0, (how, dv["700,220"], dv["560,380"])
        assert gv["L"]["src"] == ("作者" if how == "作者" else "游戏") and gv["L"]["downstream"] == 30, gv["L"]
        assert not res["iters"]["hit_cap"], res["iters"]
        got[how] = {k: (r["rate"], r["got"]) for k, r in dv.items()}
    assert got["作者"] == got["档案"], got


if __name__ == "__main__":
    sys.exit(run([test_author_limit_caps_downstream, test_author_overrides_archive, test_key_survives_restitch, test_archive_far_matching,
                  test_downstream_open_and_read_errors, test_author_keeps_read_passed, test_gate_view_not_in_design,
                  test_parallel_pool_respects_author_limit]))
