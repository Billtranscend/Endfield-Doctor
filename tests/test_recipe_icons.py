"""按机身上的产物图标定配方（src/bpa/recipe_icons.py，2026-11-14）。

起因：issue #1 的作者拿自己的蓝图走到「设计分析」是「共 0 台」——配方只从档案的配方表读，那张表是皇帝产线开发期间
巡检点面板攒的，别的蓝图没有。预览里每台设备机身中间画着产物，同一种机器能做的东西有限，认出产物多数就定了配方。

样本 tests/data/recipe_icons/：皇帝产线三个据点拼合图上的机身图标（23×23）。文件名里的 rot = 那张蓝图拼合时转了多少度
（拼合图按基地朝向转过，图标跟着转了）。认图标的几条要有 AKEData 物品图标缓存（.cache，不进仓库），没有就跳过。
最后一条用本机的皇帝产线实测项目跑整个 derive（有数据才跑）：三个据点 128 台有标准配方的设备里配方一致 108 台、
没标「推断」却认错的 0 台（景玉谷两台粉碎机是真配方表自己配反了，见 design._recipes_by_icon）。
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import pipenet as PN  # noqa: E402
from bpa import recipe_icons as RI  # noqa: E402

DATA = ROOT / "tests" / "data" / "recipe_icons"


def row(ins, outs, env=0):
    return {"text": " + ".join(f"{k}×1" for k in ins) + " → " + " + ".join(f"{k}×1" for k in outs) + "（2秒）",
            "ins": {k: 1 for k in ins}, "outs": {k: 1 for k in outs}, "env": env}


def has_icons() -> bool:
    try:
        from bpa import logistics as LG
        return len(LG._ake_templates()) > 50
    except Exception:
        return False


def icon(name: str) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(DATA / name), np.uint8), cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- 同一种产物有几条配方时挑哪条

def test_single_recipe_is_taken():
    r, sure, _ = RI.pick_recipe([row(["赤铜块"], ["赤铜零件"]), row(["灼铜块"], ["灼铜零件"])], "赤铜零件", set(), False)
    assert sure and list(r["ins"]) == ["赤铜块"]


def test_input_made_in_base_wins():
    """天有洪炉做息壤：碳块（据点里精炼炉产的）对稳定碳块（没人产）。"""
    rows = [row(["稳定碳块", "清水"], ["息壤"]), row(["碳块", "清水"], ["息壤"], env=1)]
    r, sure, note = RI.pick_recipe(rows, "息壤", {"碳块", "清水"}, True)
    assert sure and "碳块" in r["ins"] and "稳定碳块" not in r["ins"], note


def test_made_in_base_beats_raw_but_not_through_a_cycle():
    """精炼炉做碳块：芽针（种植机产的）对原木（做不出来、只能从仓库来）→ 芽针。
    精炼炉做蓝铁块：蓝铁矿（做不出来的）对蓝铁粉末（据点里的粉碎机拿蓝铁块磨的，绕一圈）→ 蓝铁矿。"""
    raw0 = RI._RAW
    RI._RAW = {"原木", "蓝铁矿"}
    try:
        rows = [row(["原木"], ["碳块"]), row(["芽针"], ["碳块"])]
        r, sure, _ = RI.pick_recipe(rows, "碳块", {"芽针"}, False, {"芽针": [row(["芽针种子", "清水"], ["芽针"])]})
        assert sure and list(r["ins"]) == ["芽针"]
        rows = [row(["蓝铁粉末"], ["蓝铁块"]), row(["蓝铁矿"], ["蓝铁块"])]
        r, sure, _ = RI.pick_recipe(rows, "蓝铁块", {"蓝铁粉末"}, False, {"蓝铁粉末": [row(["蓝铁块"], ["蓝铁粉末"])]})
        assert sure and list(r["ins"]) == ["蓝铁矿"]
    finally:
        RI._RAW = raw0


def test_tie_is_reported_as_unsure():
    """反例：两条配方的原料据点里都有 / 都没有，分不出来：给一条，但说不准。"""
    raw0 = RI._RAW
    RI._RAW = set()
    try:
        rows = [row(["锦草溶液", "赤铜瓶"], ["赤铜瓶(装)"]), row(["芽针溶液", "赤铜瓶"], ["赤铜瓶(装)"])]
        r, sure, _ = RI.pick_recipe(rows, "赤铜瓶(装)", {"赤铜瓶", "锦草溶液", "芽针溶液"}, False)
        assert r is not None and not sure
        r, sure, _ = RI.pick_recipe(rows, "赤铜瓶(装)", set(), False)
        assert r is not None and not sure
    finally:
        RI._RAW = raw0


def test_env_only_difference_is_unsure():
    """反例：提纯机做重息壤气，分离芯×2（不要稳定环境）/ 分离芯×1（要）：原料一样，据点里有散布机先取要环境的，但说不准
    （应龙关 5 台提纯机离散布机都是 85–106 px，3 台不要、2 台要）。"""
    rows = [row(["分离芯", "息壤气"], ["重息壤气"], env=0), row(["分离芯", "息壤气"], ["重息壤气"], env=1)]
    r, sure, _ = RI.pick_recipe(rows, "重息壤气", {"分离芯", "息壤气"}, True)
    assert r["env"] == 1 and not sure
    r, sure, _ = RI.pick_recipe(rows, "重息壤气", {"分离芯", "息壤气"}, False)
    assert r["env"] == 0 and not sure


def test_unknown_product_gives_nothing():
    assert RI.pick_recipe([row(["赤铜块"], ["赤铜零件"])], "息壤", set(), False)[0] is None


# ---------------------------------------------------------------- 配方表：没把握的标「推断」

def test_recipe_table_keeps_the_unsure_flag():
    led = {"devices": [{"name": "精炼炉", "x": 10, "y": 10}, {"name": "精炼炉", "x": 50, "y": 10}]}
    recs = [{"name": "精炼炉", "x": 10, "y": 10, "recipe": "蓝铁矿×1 → 蓝铁块×1（2秒）", "inferred": True},
            {"name": "精炼炉", "x": 50, "y": 10, "recipe": "蓝铁矿×1 → 蓝铁块×1（2秒）"}]
    t = PN.recipe_table(Path("."), led, recs)
    assert t[("精炼炉", 10, 10)] == ("蓝铁矿×1 → 蓝铁块×1（2秒）", True)
    assert t[("精炼炉", 50, 10)] == ("蓝铁矿×1 → 蓝铁块×1（2秒）", False)  # 反例：档案配方表里的照旧不算推断


# ---------------------------------------------------------------- 认图标（要 .cache 里的物品图标）

def products(machine: str) -> set:
    return {k for r in RI.machine_rows(machine) for k in r["outs"]}


def test_upright_icon_is_recognised():
    if not has_icons():
        return
    top = RI.rank_products(icon("rot0_天有洪炉_息壤.png"), products("天有洪炉"), [0])
    assert top[0][0] == "息壤" and top[0][1] >= RI.SURE_SCORE and top[0][1] - top[1][1] >= RI.SURE_MARGIN, top[:2]


def test_rotated_icons_need_turning_back():
    """应龙关转了 180°、武陵城转了 90°：按蓝图的旋转角转回来才认得对；不转回来两张都认成别的。"""
    if not has_icons():
        return
    for fn, machine, rot, want, wrong in (("rot180_固气转化机_气态赤铜.png", "固气转化机", 180, "气态赤铜", "气态灼铜"),
                                         ("rot90_配件机_赤铜零件.png", "配件机", 90, "赤铜零件", "灼铜零件")):
        ic = icon(fn)
        top = RI.rank_products(ic, products(machine), [rot])
        assert top[0][0] == want and top[0][1] >= 0.75, (fn, top[:2])
        assert RI.rank_products(ic, products(machine), [0])[0][0] == wrong, fn  # 反例：不转回来就是错的


def test_lookalike_pair_is_not_called_sure():
    """反例：景玉谷那台封装机做的是低容武陵电池，图标认成中容（0.81 对 0.63）。两种电池长得像，这种差距不能算认准。"""
    if not has_icons():
        return
    top = RI.rank_products(icon("rot0_封装机_低容武陵电池.png"), products("封装机"), [0])
    assert {top[0][0], top[1][0]} == {"中容武陵电池", "低容武陵电池"}, top[:2]
    assert top[0][1] - top[1][1] < RI.SURE_MARGIN, top[:2]


# ---------------------------------------------------------------- 整个 derive（有本机实测项目才跑）

def test_derive_on_emperor_line():
    arch = ROOT / "data" / "blueprints" / "1.6皇帝产线" / "ledger"
    bd = ROOT / "debug" / "bpa" / "实测-皇帝产线" / "base" / "景玉谷"
    if not has_icons() or not (bd / "composite.png").exists() or not (arch / "景玉谷_recipes.json").exists():
        return
    from bpa import design as D
    truth = {(d["name"], d["x"], d["y"]): d.get("recipe") or "" for d in json.loads((arch / "景玉谷_recipes.json").read_text(encoding="utf-8"))["devices"]}
    led = json.loads((arch / "景玉谷.json").read_text(encoding="utf-8"))
    got = {(r["name"], r["x"], r["y"]): r for r in RI.derive(bd, led, inputs=PN.load_inputs(bd))}
    same = sum(1 for k, rc in truth.items() if "→" in rc and k in got and got[k]["recipe"] == rc)
    assert same >= 33, same  # 39 台里 35 台一致（另有 2 台粉碎机是真配方表配反了）
    swapped = {("粉碎机", 507, 581), ("粉碎机", 507, 640)}
    silent = [k for k, rc in truth.items() if "→" in rc and k in got and k not in swapped and not got[k]["inferred"]
              and set(D.outputs_of(got[k]["recipe"])) != set(D.outputs_of(rc))]
    assert not silent, silent  # 没标「推断」的不能认错产物


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过" + ("" if has_icons() else "（没有物品图标缓存，认图标的几条跳过了）"))
