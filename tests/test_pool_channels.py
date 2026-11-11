"""反应池多通道配方 + 断水实测（2026-11-10，任务计划第 7 项）。

- 面板「池产物」列的物品还原成配置的几条配方：息壤 / 重息壤 是原料（只有一条池子配方吃它）。
- 首尾相接的通道并成一条净配方：武陵城扩容反应池 (1489,1055) = 清水 + 息壤 + 污水 → 两种废液，用清水 30/分。
- 应龙关 (949,731) 只看数量 > 0 的格，仍是 清水 + 息壤 → 液化息壤（设计分析 11-09 的校准不变）。
- 合并诊断：面板读到池产物的池子不再拿同类池子的其他配方兜底（原来把它说成「重息壤 送不到」）。
- 排查树：清水理论消耗比基准少 90 + 液化息壤理论产量少 90 → 指到反应池/扩容反应池和武陵城、应龙关；参考简报 ✓。
- 近景断水影响 → 合并诊断里这台设备那条注明是缺清水停的。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app")]

import pool_channels as PC  # noqa: E402

WL_1489 = "池产物：惰性壤晶废液(0)/息壤(50)/壤晶废液(50)；池内：息壤(50)/污水(50，原料)/壤晶废液(50)/惰性壤晶废液(50)"
YLG_949 = "池产物：液化息壤(50)/重息壤(0)/液化重息壤(0)"


def test_channels_and_composite():
    g = PC.from_text("扩容反应池", WL_1489)
    assert len(g) == 1 and PC.recipe_text(g[0]) == "清水×1 + 息壤×1 + 污水×1 → 惰性壤晶废液×1 + 壤晶废液×1（2秒）", [PC.recipe_text(r) for r in g]
    assert [PC.recipe_text(r) for r in PC.from_text("扩容反应池", YLG_949)] == ["清水×1 + 息壤×1 → 液化息壤×1（2秒）"]
    allc = PC.from_text("扩容反应池", YLG_949, positive_only=False)  # 数量 0 的格也看：重息壤是原料 → 液化重息壤那条
    assert len(allc) == 2 and "重息壤" in PC.recipe_text(allc[1])
    # 两种互不相干的产物：两组（设计分析按轮流做算）
    assert len(PC.from_text("反应池", "池产物：芽针溶液(50)/芽针(0)/锦草溶液(50)；池内：?(50)/?(50)")) == 2


def test_water_need():
    from bpa import pipenet as PN
    assert PN.water_per_min(YLG_949, "清水", "扩容反应池") == 30
    assert PN.water_per_min(WL_1489, "清水", "扩容反应池") == 30  # 原来是 0：「息壤」按产物找不到
    assert PN.water_per_min("池产物：壤晶废液(49)/惰性壤晶废液(0)；池内：液化息壤(50，原料)", "清水", "扩容反应池") == 0


def test_combine_no_fallback_for_pool():
    import combine as C
    d = C.Device("扩容反应池", "空闲中", "空闲中", 1489, 1055, WL_1489, "item_liquid_xiranite_lowpoly|item_xiranite_powder|item_liquid_xiranite_poly",
                 None, guessed=True, near=True)
    ins = C.pool_recipes(d)
    assert {r["id"] for r in ins} == {"pool_liquid_xiranite_poly_2", "pool_liquid_liquid_xiranite_2"}, [r["id"] for r in ins]
    assert "item_liquid_water" in C.recipe_inputs(None, d) and "item_xiranite_enr" not in C.recipe_inputs(None, d)


def test_expert_water_drop():
    import blueprint as bp
    import expert as E
    plan = bp.load("1.6皇帝产线")
    brief = json.loads((ROOT / "debug" / "brief_20261110-153748.json").read_text(encoding="utf-8"))
    st = next(s for s in E.run(brief, plan) if s["key"] == "tree:清水理论消耗")
    assert st["status"] == "bad" and "少 90" in st["cause"] and "约 3 台" in st["cause"], st
    assert set(st["where"]["machines"]) == {"反应池", "扩容反应池"} and set(st["where"]["areas"]) == {"应龙关", "武陵城"}, st["where"]
    assert any("扩容反应池 (1489,1055)" in f for f in st["facts"]), st["facts"]
    ok = next(s for s in E.run(plan.reference_brief, plan) if s["key"] == "tree:清水理论消耗")
    assert ok["status"] == "ok", ok


def test_link_cut_victims():
    from app import api
    data = {"verdicts": {"问题": [{"title": "息壤 理论消耗 390→360：有消费者被断开", "reason": "息壤 的理论消耗从 390 降到 360", "advice": "查传送带",
                                   "device": {"name": "扩容反应池", "x": 1489, "y": 1055}, "members": []}], "待确认": []}}
    rows = [{"name": "清水输入端13", "fluid": "清水", "impact": {"fluid": "清水", "cut_lost": [
        {"name": "扩容反应池", "x": 1489, "y": 1055, "before": 30, "after": 0, "rate_before": 1.0, "rate_after": 0.0}]}}]
    assert api.link_cut_victims(data, rows) == 1
    v = data["verdicts"]["问题"][0]
    assert "清水输入端13 下游" in v["reason"] and "缺清水停的" in v["reason"] and "接回 清水输入端13" in v["advice"], v


def test_theo_flip_after_reconnect():
    """接回水后（17:47）：蓝铁粉末 理论消耗 60（参考 90）、壤晶废液 120（参考 180），实际 75 / 150 和参考一样 → 不报「消费者被断开」；
    断水时（15:37）实际 0 / 0 → 照报。"""
    import combine as C
    after = ROOT / "debug" / "brief_20261110-174739.json"
    if not after.exists():
        return
    _, d = C.run(after, ROOT / "debug" / "patrol_20261110-174225.json", "1.6皇帝产线", merge=False)
    assert not [v for v in d["verdicts"].get("问题", []) if "理论消耗" in (v.get("title") or "")], d["verdicts"]["问题"]
    _, d = C.run(ROOT / "debug" / "brief_20261110-153748.json", ROOT / "debug" / "patrol_20261110-153300.json", "1.6皇帝产线", merge=False)
    titles = [v.get("title") or "" for v in d["verdicts"].get("问题", [])]
    assert any(t.startswith("蓝铁粉末 理论消耗") for t in titles) and any(t.startswith("液化息壤 理论消耗") for t in titles), titles


if __name__ == "__main__":
    fs = [test_channels_and_composite, test_water_need, test_combine_no_fallback_for_pool, test_expert_water_drop, test_link_cut_victims,
          test_theo_flip_after_reconnect]
    ok = 0
    for f in fs:
        try:
            f(); ok += 1
        except AssertionError as e:
            print("FAIL", f.__name__, str(e)[:400])
    print(f"{ok}/{len(fs)} 通过")
