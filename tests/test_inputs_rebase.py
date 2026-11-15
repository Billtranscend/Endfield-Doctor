"""作者存的输入端跟着拼合图走、物品名校验、同一种产物几条配方的挑法（2026-11-14 issue #1 第四轮）。

1. 输入端：作者存的标记原来只记拼合图坐标。拼合图原点一变（重扫底账、重读蓝图、增减蓝图），存的坐标指到别处，
   设计分析一个输入端都找不上（他的武陵城：存着 7 个旧标记，现在的 21 个清水候选一个都对不上，清水 445/960）。
   现在每个标记记「哪张蓝图预览里的什么位置」，拼合后重新定位；老版本存的对不上就挪到「对不上的旧标记」，改用候选。
2. 物品名：他在应龙关填的「水」「重积酸」认不出来，原来没有任何提示。
3. 配方：他的 6 台做碳块的精炼炉 5 台按砂叶算成没料，实际是芽针（相邻设备送来的就是芽针）。
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import design as D  # noqa: E402
from bpa import inputs as INP  # noqa: E402
from bpa import pipenet as PN  # noqa: E402
from bpa import project as P  # noqa: E402
from bpa import vision as V  # noqa: E402


def make_tf(f0=(1000.0, 2000.0), rot=90, bps=("甲", "乙")):
    """两张蓝图的据点变换。甲在原点附近，乙在它右边 900 px（远景再乘 K）。"""
    tf = {"K": V.K, "F0": list(f0), "bps": {}}
    for i, bp in enumerate(bps):
        tf["bps"][bp] = {"rot": rot, "t": [f0[0] + V.K * (600 + 900 * i), f0[1] + V.K * 50], "offset": [0, 0]}
    return tf


def gates_on(tf, spec):
    """spec：[(蓝图, 预览坐标)] → 拼合图上的准入口（project._logistics_on_canvas 的写法）。"""
    return [{"type": "准入口", "bp": bp, "p_bp": list(q), "p": INP.from_bp(tf, bp, q), "icon_group": 1} for bp, q in spec]


SPEC = [("甲", (40, 100)), ("甲", (40, 170)), ("甲", (40, 240)), ("乙", (40, 100)), ("乙", (40, 170))]


def auto_of(lg):
    return [dict(p=list(o["p"]), bp=o["bp"], p_bp=list(o["p_bp"]), item="清水", rate=60, via="水泵", source="auto", no=i + 1, merged=False, note="")
            for i, o in enumerate(lg)]


# ---------------------------------------------------------------- 坐标换算

def test_canvas_and_preview_round_trip():
    for rot in (0, 90, 180, 270):
        tf = make_tf(rot=rot)
        for bp in ("甲", "乙"):
            p = INP.from_bp(tf, bp, (123, 456))
            q = INP.to_bp(tf, bp, p)
            assert abs(q[0] - 123) <= 1 and abs(q[1] - 456) <= 1, (rot, bp, q)


def test_anchor_takes_gate_or_nearest_blueprint():
    tf = make_tf()
    lg = gates_on(tf, SPEC)
    on = {"no": 1, "p": [lg[3]["p"][0] + 6, lg[3]["p"][1] - 5], "item": "清水"}  # 离乙的第一个口 6 px：算标在它上面
    off = {"no": 2, "p": [lg[0]["p"][0] + 60, lg[0]["p"][1] + 5], "item": ""}  # 作者随手点的，不在任何口上
    a, b = INP.anchor([on, off], tf, lg)
    assert a["bp"] == "乙" and a["p_bp"] == [40, 100]
    assert b["bp"] == "甲" and INP.from_bp(tf, "甲", b["p_bp"]) == off["p"]
    assert INP.anchor([on], {"error": "没有底账"}, lg) == [on]  # 反例：没有变换，原样返回


# ---------------------------------------------------------------- 拼合图变了

def test_marks_follow_the_composite():
    tf = make_tf()
    lg = gates_on(tf, SPEC)
    saved = INP.anchor([dict(m, rate=120) for m in auto_of(lg)[:4]] + [{"no": 5, "p": [lg[0]["p"][0] + 60, lg[0]["p"][1]], "source": "manual", "item": ""}], tf, lg)
    # 新拼合：原点挪了（多了一张更靠左上的蓝图），两张蓝图各自的位置不变
    tf2 = make_tf()
    tf2["F0"] = [tf["F0"][0] - V.K * 300, tf["F0"][1] - V.K * 120]
    lg2 = gates_on(tf2, SPEC)
    assert lg2[0]["p"] != lg[0]["p"]
    assert not any(INP._gate_at(m["p"], lg2) for m in saved[:4])  # 不处理的话：旧坐标一个口都对不上（这就是他的情况）
    marks, stale, info = INP.rebase(saved, tf2, lg2, auto_of(lg2), INP.seen_of(auto_of(lg)))
    assert not stale and info == {"moved": 5, "stale": 0, "added": 0}, info
    assert [m["p"] for m in marks[:4]] == [o["p"] for o in lg2[:4]] and all(m["rate"] == 120 for m in marks[:4])  # 作者填的流速还在
    assert marks[4]["p"] == [lg2[0]["p"][0] + 60, lg2[0]["p"][1]]  # 不在口上的也跟着挪
    # 反例：拼合图没变，再来一遍什么都不动
    again, stale, info = INP.rebase(marks, tf2, lg2, auto_of(lg2), INP.seen_of(auto_of(lg2)))
    assert again == marks and info == {"moved": 0, "stale": 0, "added": 0}


def test_deleted_candidate_stays_deleted_new_one_is_added():
    tf = make_tf()
    lg = gates_on(tf, SPEC)
    auto = auto_of(lg)
    saved = INP.anchor(auto[:2] + auto[3:], tf, lg)  # 作者删掉了第 3 个候选
    seen = INP.seen_of(auto)
    lg2 = gates_on(tf, SPEC + [("乙", (40, 240))])  # 程序新认出一个口
    marks, stale, info = INP.rebase(saved, tf, lg2, auto_of(lg2), seen)
    assert info == {"moved": 0, "stale": 0, "added": 1}, info
    assert [m["p_bp"] for m in marks] == [[40, 100], [40, 170], [40, 100], [40, 170], [40, 240]]
    assert marks[-1]["no"] == 6 and marks[-1]["bp"] == "乙"  # 接着原来最大的编号往下排，原来的编号不动


def test_removed_blueprint_goes_stale():
    tf = make_tf()
    lg = gates_on(tf, SPEC)
    saved = INP.anchor(auto_of(lg), tf, lg)
    tf2 = make_tf(bps=("甲",))
    lg2 = gates_on(tf2, SPEC[:3])
    marks, stale, info = INP.rebase(saved, tf2, lg2, auto_of(lg2), INP.seen_of(auto_of(lg)))
    assert len(marks) == 3 and len(stale) == 2 and all("乙" in s["why"] for s in stale), info


# ---------------------------------------------------------------- 老版本存的（没有 p_bp）

def legacy(marks):
    return [{k: v for k, v in m.items() if k not in ("p_bp",)} for m in marks]


def test_old_save_on_changed_composite_falls_back_to_candidates():
    """他的情况：老版本存的 7 个标记，位置都不在现在的准入口上 → 全部挪到「对不上」，改用这次的候选。"""
    tf = make_tf()
    lg = gates_on(tf, SPEC)
    old = legacy([dict(m, p=[m["p"][0] + 300, m["p"][1] + 120], rate=120) for m in auto_of(lg)]) + [
        {"no": 6, "p": [50, 60], "source": "manual", "item": ""}, {"no": 7, "p": [80, 60], "source": "manual", "item": ""}]
    marks, stale, info = INP.rebase(old, tf, lg, auto_of(lg), None)
    assert info == {"moved": 0, "stale": 7, "added": 5}, info
    assert [m["p"] for m in marks] == [o["p"] for o in lg] and all(m["rate"] == 60 for m in marks)
    assert all(s["why"] for s in stale) and stale[0]["rate"] == 120  # 当时填的还看得到


def test_old_save_chance_hit_is_not_kept():
    """拼合图变过（落在口上的不到一半）：碰巧落在某个口上的旧标记也不留，那多半是另一个口。"""
    tf = make_tf()
    lg = gates_on(tf, SPEC)
    old = legacy([dict(m, p=[m["p"][0] + 300, m["p"][1] + 120]) for m in auto_of(lg)])
    old[0]["p"] = list(lg[4]["p"])
    old[0]["item"] = "沉积酸"
    marks, stale, info = INP.rebase(old, tf, lg, auto_of(lg), None)
    assert info["stale"] == 5 and info["added"] == 5 and all(m["item"] == "清水" for m in marks), info


def test_old_save_on_same_composite_is_kept_as_is():
    """反例：老版本存的、拼合图没变（本机皇帝产线就是这样）：全部留下、补记位置；作者删过的候选不会被加回来。"""
    tf = make_tf()
    lg = gates_on(tf, SPEC)
    old = legacy(auto_of(lg)[:3]) + [{"no": 4, "p": [lg[0]["p"][0] + 60, lg[0]["p"][1]], "source": "manual", "item": "息壤气"}]
    marks, stale, info = INP.rebase(old, tf, lg, auto_of(lg), None)
    assert info == {"moved": 0, "stale": 0, "added": 0} and len(marks) == 4, info
    assert [m["p"] for m in marks] == [m["p"] for m in old] and all(m.get("p_bp") is not None for m in marks)


# ---------------------------------------------------------------- 项目里：保存、拼合后写回

def test_project_save_then_rebase_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        old_bpa = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            bd = P.pdir("项目") / "base" / "某据点"
            bd.mkdir(parents=True)
            tf = make_tf()
            lg = gates_on(tf, SPEC)
            auto = auto_of(lg)
            for f, v in (("transform.json", tf), ("logistics.json", lg), ("inputs_auto.json", auto)):
                (bd / f).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            bad = P.save_inputs("项目", "某据点", [dict(auto[0], rate=120), dict(auto[1], item="水")])
            assert [(b["no"], b["item"]) for b in bad] == [(2, "水")] and "清水" in bad[0]["suggest"]
            e = json.loads((P.pdir("项目") / "inputs.json").read_text(encoding="utf-8"))["某据点"]
            assert e["marks"][0]["p_bp"] == [40, 100] and len(e["seen"]) == 5 and "stale" not in e
            # 拼合图原点挪了
            tf2 = make_tf()
            tf2["F0"] = [tf["F0"][0] - V.K * 300, tf["F0"][1]]
            lg2 = gates_on(tf2, SPEC)
            logs = []
            P._rebase_inputs("项目", "某据点", np.full((400, 2400, 3), 232, np.uint8), tf2, lg2, auto_of(lg2), logs.append)
            e = json.loads((P.pdir("项目") / "inputs.json").read_text(encoding="utf-8"))["某据点"]
            assert [m["p"] for m in e["marks"]] == [lg2[0]["p"], lg2[1]["p"]] and e["marks"][0]["rate"] == 120
            assert any("跟着挪" in s for s in logs) and any("「水」" in s and "清水" in s for s in logs), logs
            for f, v in (("transform.json", tf2), ("logistics.json", lg2), ("inputs_auto.json", auto_of(lg2))):
                (bd / f).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            got = PN.load_inputs(bd)  # 分析读到的就是挪过的
            assert [m["p"] for m in got] == [lg2[0]["p"], lg2[1]["p"]]
        finally:
            P.BPA = old_bpa


def test_project_old_save_gets_backup_and_stale_list():
    with tempfile.TemporaryDirectory() as td:
        old_bpa = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            bd = P.pdir("项目") / "base" / "某据点"
            bd.mkdir(parents=True)
            tf = make_tf()
            lg = gates_on(tf, SPEC)
            auto = auto_of(lg)
            wrong = legacy([dict(m, p=[m["p"][0] + 300, m["p"][1] + 120]) for m in auto[:3]])
            pj = P.pdir("项目") / "inputs.json"
            pj.write_text(json.dumps({"某据点": {"marks": wrong, "saved": "2026-11-13 20:00:00"}, "别的据点": {"marks": wrong}}, ensure_ascii=False), encoding="utf-8")
            logs = []
            P._rebase_inputs("项目", "某据点", np.full((400, 2400, 3), 232, np.uint8), tf, lg, auto, logs.append)
            allin = json.loads(pj.read_text(encoding="utf-8"))
            e = allin["某据点"]
            assert len(e["stale"]) == 3 and [m["p"] for m in e["marks"]] == [o["p"] for o in lg]
            assert allin["别的据点"] == {"marks": wrong}  # 别的据点不碰
            assert json.loads((pj.parent / "inputs.json.bak").read_text(encoding="utf-8"))["某据点"]["marks"] == wrong
            assert any("对不上" in s and "候选 5 个" in s for s in logs), logs
            for f, v in (("transform.json", tf), ("logistics.json", lg), ("inputs_auto.json", auto)):
                (bd / f).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            # 作者在页面上把旧标记全删了再保存：stale 没了，标记还在
            P.save_inputs("项目", "某据点", e["marks"], [])
            e = json.loads(pj.read_text(encoding="utf-8"))["某据点"]
            assert "stale" not in e and len(e["marks"]) == 5
            # 反例：不传 stale（老界面的调用）= 保留原来存的
            pj.write_text(json.dumps({"某据点": {"marks": e["marks"], "stale": wrong}}, ensure_ascii=False), encoding="utf-8")
            P.save_inputs("项目", "某据点", e["marks"])
            assert len(json.loads(pj.read_text(encoding="utf-8"))["某据点"]["stale"]) == 3
        finally:
            P.BPA = old_bpa


def test_opening_the_page_checks_an_old_save_first():
    """升级后没重新拼合就打开「输入端」页：老版本存的、对不上的标记先挪走，页面上是这次的候选。
    不这样的话作者一点保存，旧位置就被当成现在的位置记下来（带上 p_bp），以后再也认不出它是旧的。"""
    with tempfile.TemporaryDirectory() as td:
        old_bpa = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            bd = P.pdir("项目") / "base" / "某据点"
            bd.mkdir(parents=True)
            tf = make_tf()
            lg = gates_on(tf, SPEC)
            auto = legacy(auto_of(lg))  # 老版本拼合出的候选也没有 p_bp
            for f, v in (("transform.json", tf), ("logistics.json", lg), ("inputs_auto.json", auto), ("inputs_dropped.json", [])):
                (bd / f).write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")
            V.wr(bd / "composite.png", np.full((400, 2400, 3), 232, np.uint8))
            wrong = legacy([dict(m, p=[m["p"][0] + 300, m["p"][1] + 120], rate=120) for m in auto_of(lg)[:3]])
            pj = P.pdir("项目") / "inputs.json"
            pj.write_text(json.dumps({"某据点": {"marks": wrong}}, ensure_ascii=False), encoding="utf-8")
            v = P.inputs_view("项目", "某据点")
            assert [m["p"] for m in v["marks"]] == [o["p"] for o in lg] and len(v["stale"]) == 3 and v["stale"][0]["rate"] == 120
            assert all(m.get("p_bp") is not None for m in v["marks"]) and not v["bad_items"] and "清水" in v["items"]
            before = pj.read_text(encoding="utf-8")
            P.inputs_view("项目", "某据点")  # 再开一次：已经核过，文件不再动
            assert pj.read_text(encoding="utf-8") == before
            # 反例：老版本存的、位置都对：留着，只补记位置
            pj.write_text(json.dumps({"某据点": {"marks": legacy([dict(m, rate=120) for m in auto_of(lg)[:3]])}}, ensure_ascii=False), encoding="utf-8")
            v = P.inputs_view("项目", "某据点")
            assert len(v["marks"]) == 3 and not v["stale"] and all(m["rate"] == 120 and m.get("p_bp") is not None for m in v["marks"])
        finally:
            P.BPA = old_bpa


# ---------------------------------------------------------------- 物品名

def test_item_names_are_checked():
    names = INP.item_names()
    assert {"清水", "沉积酸", "息壤气", "芽针"} <= set(names)
    marks = [{"no": 1, "item": "水"}, {"no": 2, "item": "重积酸"}, {"no": 3, "item": "清水"}, {"no": 4, "item": ""}, {"no": 5, "item": " 沉积酸 "},
             {"no": 6, "item": "完全不是物品"}]
    bad = {b["no"]: b for b in INP.check_items(marks)}
    assert sorted(bad) == [1, 2, 6], bad
    assert "清水" in bad[1]["suggest"] and bad[2]["suggest"][0] == "沉积酸" and len(bad[1]["suggest"]) <= 3


# ---------------------------------------------------------------- 同一种产物的几条配方

SHAYE, YAZHEN = "砂叶×1 → 碳块×1（2秒）", "芽针×1 → 碳块×2（2秒）"
PLANT, FURNACE = "清水×1 + 芽针种子×1 → 芽针×2（2秒）", "碳块×1 + 清水×1 → 息壤×1（2秒）"


def chain(devs, links):
    """devs：[(设备名, 配方, 是不是推断的)]；links：[(i, j)] 直接相连。→ (flow, devices, dev_canvas, rec)"""
    devices = [{"name": n, "x": 100 * i, "y": 0} for i, (n, _, _) in enumerate(devs)]
    canvas = [(100.0 * i, 0.0) for i in range(len(devs))]
    flow = {"nodes": [], "links": [{"a_dev": {"name": devs[a][0], "c": list(canvas[a])}, "device": {"name": devs[b][0], "c": list(canvas[b])}} for a, b in links]}
    return flow, devices, canvas, {i: (r, inf) for i, (_, r, inf) in enumerate(devs)}


def test_same_product_recipe_follows_what_neighbors_send():
    assert SHAYE in D._machine_recipes("精炼炉") and YAZHEN in D._machine_recipes("精炼炉")
    flow, devices, canvas, rec = chain([("种植机", PLANT, False), ("精炼炉", SHAYE, True), ("天有洪炉", FURNACE, False)], [(0, 1), (1, 2)])
    fixes = D._infer_by_neighbors(flow, devices, canvas, rec, [], alt={})
    assert rec[1] == (YAZHEN, True) and len(fixes) == 1 and "芽针" in fixes[0]["how"], fixes
    # 反例：配方是配方表里读来的（不是推断的）不动
    flow, devices, canvas, rec = chain([("种植机", PLANT, False), ("精炼炉", SHAYE, False), ("天有洪炉", FURNACE, False)], [(0, 1), (1, 2)])
    assert D._infer_by_neighbors(flow, devices, canvas, rec, [], alt={}) == [] and rec[1] == (SHAYE, False)
    # 反例：邻居没人送任何一种原料（只有吃碳块的天有洪炉）→ 分不出来，不动
    flow, devices, canvas, rec = chain([("精炼炉", SHAYE, True), ("天有洪炉", FURNACE, False)], [(0, 1)])
    assert D._infer_by_neighbors(flow, devices, canvas, rec, [], alt={}) == [] and rec[0] == (SHAYE, True)


def test_same_product_loop_feed_does_not_count():
    """皇帝产线武陵城精炼炉 (1870,699)：做蓝铁块，旁边的粉碎机拿蓝铁块磨蓝铁粉末。粉末是它自己的产物磨的，不算有人送，还是蓝铁矿。"""
    ore, powder, grind = "蓝铁矿×1 → 蓝铁块×1（2秒）", "蓝铁粉末×1 → 蓝铁块×1（2秒）", "蓝铁块×1 → 蓝铁粉末×1（2秒）"
    assert ore in D._machine_recipes("精炼炉") and powder in D._machine_recipes("精炼炉") and grind in D._machine_recipes("粉碎机")
    flow, devices, canvas, rec = chain([("精炼炉", ore, True), ("粉碎机", grind, False)], [(0, 1)])
    assert D._infer_by_neighbors(flow, devices, canvas, rec, [], alt={}) == [] and rec[0] == (ore, True)


def test_unconnected_one_follows_its_peers():
    """他的第 6 台：物流图里没接上，原料没看到有谁送；同据点另外几台做碳块的精炼炉按邻居定的都是芽针 → 跟着取。"""
    devs = [("种植机", PLANT, False), ("精炼炉", SHAYE, True), ("天有洪炉", FURNACE, False), ("精炼炉", SHAYE, True), ("天有洪炉", FURNACE, False)]
    flow, devices, canvas, rec = chain(devs, [(0, 1), (1, 2), (3, 4)])
    fixes = D._infer_by_neighbors(flow, devices, canvas, rec, [], alt={})
    assert rec[1][0] == YAZHEN and rec[3][0] == YAZHEN and "跟着取" in fixes[-1]["how"], fixes
    # 反例：没有按邻居定下来的同类 → 不动
    flow, devices, canvas, rec = chain(devs[3:], [(0, 1)])
    assert D._infer_by_neighbors(flow, devices, canvas, rec, [], alt={}) == [] and rec[0][0] == SHAYE


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
