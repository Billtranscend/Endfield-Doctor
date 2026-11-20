"""拼合图上的图标要按蓝图旋转角转回来再认（2026-11-14 issue #1 第四轮）。

据点拼合图是把每张蓝图的预览按旋转角转过再贴的，图标跟着一起转了。机身上的产物图标 0.10.9 已经转回来再比
（recipe_icons），仓库口、准入口这几处漏了。他的 5 张蓝图都转 90° 放：18 个源矿取货口认成致密源石粉末
（不转 0.38–0.45 输给 0.50–0.59，转回来 0.86–0.93），源矿没人供，下游 30 台设备算成「没料」。
图标库（作者标过的图标，按预览里的朝向存的）同理：转着放的据点（皇帝产线应龙关 180°、武陵城 90°）原来一个都配不上。

样本 tests/data/port_icons/：他的武陵城拼合图上的取货口图标（23×23，文件名 = 实际物品，rot90 = 拼合时转了 90°）。
和 AKEData 比的几条要有物品图标缓存（.cache，不进仓库），没有就跳过。
"""
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import design as D  # noqa: E402
from bpa import logistics as LG  # noqa: E402
from bpa import pipenet as PN  # noqa: E402
from bpa import vision as V  # noqa: E402

from _testkit import run, skip  # noqa: E402

DATA = ROOT / "tests" / "data" / "port_icons"
CAND = {"源矿", "致密源石粉末", "源石粉末", "赤铜矿", "赤铜块", "砂叶粉末", "砂叶", "蓝铁粉末", "息壤", "中容武陵电池"}  # 他那个据点配方里的固体（一部分）


def has_icons() -> bool:
    try:
        return len(LG._ake_templates()) > 50
    except Exception:
        return False


def icon(name: str) -> np.ndarray:
    return V.rd(DATA / f"{name}_rot90.png")


def ranked(cs):
    return [(c["name"], c["score"]) for c in cs if c["name"] in CAND]


# ---------------------------------------------------------------- 转回来

def test_upright_undoes_composite_rotation():
    a = np.arange(23 * 23 * 3, dtype=np.uint8).reshape(23, 23, 3)
    for rot, code in ((90, cv2.ROTATE_90_CLOCKWISE), (180, cv2.ROTATE_180), (270, cv2.ROTATE_90_COUNTERCLOCKWISE)):
        assert np.array_equal(LG.upright(cv2.rotate(a, code), rot), a), rot
    assert LG.upright(a, 0) is a and LG.upright(a, 360) is a


def test_ore_port_is_read_right_only_when_turned_back():
    if not has_icons():
        skip("没有 AKEData 物品图标缓存（.cache）")
    for s in ("源矿_a", "源矿_b", "源矿_c"):
        raw = ranked(LG.ake_candidates(icon(s), top=999))
        assert raw[0][0] == "致密源石粉末", (s, raw[:3])  # 不转：认错（这就是他那 18 个口）
        up = ranked(LG.ake_candidates_upright(icon(s), [90], top=999))
        assert up[0][0] == "源矿" and up[0][1] >= 0.8 and up[0][1] - up[1][1] >= 0.25, (s, up[:3])


def test_other_ports_keep_their_item():
    """反例：转回来以后别的物品不能被带成源矿。真的致密源石粉末还是致密源石粉末，赤铜矿、砂叶粉末也一样，而且分数更高。"""
    if not has_icons():
        skip("没有 AKEData 物品图标缓存（.cache）")
    for s in ("致密源石粉末", "赤铜矿", "砂叶粉末"):
        raw = dict(ranked(LG.ake_candidates(icon(s), top=999)))
        up = ranked(LG.ake_candidates_upright(icon(s), [90], top=999))
        assert up[0][0] == s and up[0][1] >= 0.9 and up[0][1] - up[1][1] >= 0.25, (s, up[:3])
        assert up[0][1] > raw[s] + 0.15, (s, up[0], raw[s])


def test_several_rotations_take_the_best_per_item():
    """分不清在哪张蓝图里（给了几个角度）：每种物品取最高分；不给角度 = 不转。"""
    if not has_icons():
        skip("没有 AKEData 物品图标缓存（.cache）")
    ic = icon("源矿_a")
    both = ranked(LG.ake_candidates_upright(ic, [0, 90], top=999))
    assert both[0][0] == "源矿" and both[0][1] >= 0.8
    plain = LG.ake_candidates(ic, top=5)
    assert LG.ake_candidates_upright(ic, [], top=5) == plain and LG.ake_candidates_upright(ic, [0], top=5) == plain


# ---------------------------------------------------------------- 这个位置转了多少度

def test_rotation_at_canvas_position():
    with tempfile.TemporaryDirectory() as td:
        bd = Path(td) / "proj" / "base" / "某据点"
        bd.mkdir(parents=True)
        assert D._rots_fn(bd)(100, 100) == [0]  # 反例：没有 transform.json，按没转算
        tf = {"K": V.K, "F0": [0.0, 0.0], "bps": {"甲": {"rot": 90, "t": [0, 0], "offset": [0, 0]}, "乙": {"rot": 90, "t": [500, 0], "offset": [900, 0]}}}
        (bd / "transform.json").write_text(json.dumps(tf), encoding="utf-8")
        assert D._rots_fn(bd)(100, 100) == [90] and D._rots_fn(bd)(5000, 5000) == [90]
        tf["bps"]["乙"]["rot"] = 0  # 两张蓝图转得不一样、又读不到蓝图尺寸：两个角度都给，比的时候取高的
        (bd / "transform.json").write_text(json.dumps(tf), encoding="utf-8")
        assert D._rots_fn(bd)(100, 100) == [0, 90]


# ---------------------------------------------------------------- 图标库

def test_item_library_matches_only_upright():
    """图标库按预览里的朝向存。拼合图上转过 90° 的同一个图标：直接查配不上，转回来能配上。"""
    with tempfile.TemporaryDirectory() as td:
        lib = Path(td) / "items"
        on_canvas = icon("源矿_a")
        LG.save_item(LG.upright(on_canvas, 90), "固", "源矿", lib)
        assert LG.lookup_item(LG.upright(on_canvas, 90), lib) == ("固", "源矿")
        assert LG.lookup_item(on_canvas, lib)[1] != "源矿"
        assert LG.lookup_item(LG.upright(icon("赤铜矿"), 90), lib)[1] != "源矿"  # 反例：别的物品转回来也不会配成它


def test_local_gates_found_in_library_after_turning_back():
    """本机皇帝产线武陵城（转 90° 放，有数据才跑）：四个物品准入口直接查图标库一个都配不上，转回来是重息壤、赤铜耐压罐、芽针、锦草。"""
    bd = ROOT / "debug" / "bpa" / "实测-皇帝产线" / "base" / "武陵城"
    if not (bd / "composite.png").exists() or not (bd / "flow.json").exists() or not LG._lib(LG.ITEM_DIR):
        skip("本机没有实测项目武陵城的拼合结果，或者图标库是空的")
    img = V.rd(bd / "composite.png")
    nodes = {n["id"]: n for n in json.loads((bd / "flow.json").read_text(encoding="utf-8"))["nodes"]}
    rots = D._rots_fn(bd)
    for nid, want in ((49, "重息壤"), (53, "赤铜耐压罐"), (85, "芽针"), (86, "锦草")):
        n = nodes.get(nid)
        if n is None or n["type"] != "准入口":
            skip(f"实测项目武陵城重新拼合过，物流件编号 {nid} 不是准入口了")
        ic = LG.gate_icon(img, n["p"])
        assert rots(*n["p"]) == [90]
        assert LG.lookup_item(ic, None)[1] == "", nid
        assert LG.lookup_item(LG.upright(ic, 90), None)[1] == want, (nid, LG.lookup_item(LG.upright(ic, 90), None))


# ---------------------------------------------------------------- 取货口旁边的设备

def test_port_side_of_neighbor_recipe():
    rc = "赤铜矿×1 + 清水×1 → 赤铜块×1 + 污水×1（2秒）"
    assert D._port_side("仓库取货口", rc) == {"赤铜矿", "清水"}  # 取货口只可能是原料：被管子压住的图标不会再猜成赤铜块
    assert D._port_side("仓库存货口", rc) == {"赤铜块", "污水"}
    assert D._port_side("仓库口", rc) == {"赤铜矿", "清水", "赤铜块", "污水"}  # 分不清存还是取：都算


def test_covered_icon_yields_to_neighbor():
    c = lambda *rows: [{"name": n, "score": s} for n, s in rows]  # noqa: E731
    # 被管子压住的赤铜矿口：图标比成源石粉末 0.57（领先 0.10），旁边的精炼炉吃赤铜矿 → 不信图标
    assert not D._port_icon_clear(c(("源石粉末", 0.57), ("致密源石粉末", 0.47)), {"赤铜矿"})
    # 反例：图标很清楚（0.90，领先 0.37），就算旁边的设备用不上也信图标（连着的设备可能配错台）
    assert D._port_icon_clear(c(("源矿", 0.90), ("致密源石粉末", 0.53)), {"赤铜矿"})
    # 反例：分数高但领先不够（长得像的一对），旁边的设备又用不上 → 不信
    assert not D._port_icon_clear(c(("中容武陵电池", 0.81), ("低容武陵电池", 0.72)), {"赤铜矿"})
    # 图标一般清楚，认出来的正是旁边设备要的 → 信
    assert D._port_icon_clear(c(("赤铜矿", 0.56), ("源石粉末", 0.48)), {"赤铜矿"})
    # 没连着设备：还是原来的门槛（0.5 / 领先 0.05）
    assert D._port_icon_clear(c(("源石粉末", 0.57), ("致密源石粉末", 0.47)), set())
    assert not D._port_icon_clear(c(("源石粉末", 0.45), ("致密源石粉末", 0.20)), set())
    assert not D._port_icon_clear(c(("源石粉末", 0.57), ("致密源石粉末", 0.54)), set())
    assert not D._port_icon_clear([], {"赤铜矿"})


# ---------------------------------------------------------------- 输入端候选还没生成

def test_load_inputs_without_candidates_is_empty():
    """新项目第一次拼合、输入端候选还没写：当作没有输入端，不抛 FileNotFoundError（原来设计分析直接出错）。"""
    with tempfile.TemporaryDirectory() as td:
        bd = Path(td) / "proj" / "base" / "某据点"
        bd.mkdir(parents=True)
        assert PN.load_inputs(bd) == []
        (bd / "inputs_auto.json").write_text(json.dumps([{"no": 1, "item": "清水", "p": [10, 10]}]), encoding="utf-8")
        assert PN.load_inputs(bd)[0]["item"] == "清水"
        (bd.parent.parent / "inputs.json").write_text(json.dumps({"某据点": {"marks": [{"no": 1, "item": "清水", "rate": None, "p": [10, 10]}]}}), encoding="utf-8")
        assert PN.load_inputs(bd)[0]["rate"] == 60  # 作者填的优先，清水没填流速按 60


if __name__ == "__main__":
    sys.exit(run([v for k, v in list(globals().items()) if k.startswith("test_")]))
