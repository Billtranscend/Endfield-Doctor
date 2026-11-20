"""池子并行的纯函数（src/bpa/pool_model.py，审计第 3 项，2026-11-19）。不读任何本机实测数据，只用 data/recipes.json。

- combos：按格数枚举放得下的配方组合，数目对照 scratchpad 试验 combos.txt（反应池 2 条 16 种、扩容池 3 条 63 种…）；
- check：格数按原始配方去重、中间产物只算一种（液化息壤 + 壤晶废液是 6 种）；出口 / 入口不够只警告；
- groups：首尾相接的并成一条净配方（液化息壤 + 壤晶废液 + 壤晶 → 一条，members 3 条），互不相干的各自一组；
- chan_rates：共用的清水不够时按各条用量比例分；一条缺独有原料时不白占清水（清水 45 只用掉 36）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import pool_channels as PC  # noqa: E402
from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import pool_model as PM  # noqa: E402

YA = "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"
JIN = "清水×1 + 锦草粉末×1 → 锦草溶液×1（2秒）"
A = "清水×1 + 息壤×1 → 液化息壤×1（2秒）"
B = "污水×1 + 液化息壤×1 → 惰性壤晶废液×1 + 壤晶废液×1（2秒）"
C = "蓝铁粉末×1 + 壤晶废液×2 → 污水×1 + 壤晶×1（2秒）"
OIL = "籽油花×1 + 清水×1 → 菜籽油×1（2秒）"
FODDER = "平薯叶粉末×1 + 清水×1 → 饱食饲料×1（2秒）"


def test_recipes_same_as_design():
    # 配方文字和设计分析用的一字不差（groups 靠文字反查 recipes.json 的配方）
    for m in ("反应池", "扩容反应池"):
        assert PM.recipes(m) == D._machine_recipes(m) and len(PM.recipes(m)) == 11, m
    assert PM.recipes("灌装机") == []
    assert PM.SLOTS == D.POOLS == {"反应池": 5, "扩容反应池": 8}


def test_is_fluid_matches_ids():
    # 名字规则和物品 id 的 item_liquid / item_gas 前缀对得上（池子配方里的全部物品）
    recipes, names = PC._db()
    for r in recipes:
        if r["machine_name"] in PM.SLOTS:
            for k in list(r["inputs"]) + list(r["outputs"]):
                assert PM.is_fluid(names[k]) == k.startswith(("item_liquid", "item_gas")), (k, names[k])
    assert D._is_fluid("清水") and D._is_fluid("气态赤铜") and not D._is_fluid("菜籽油")


def test_combos_count():
    n = lambda m, k, strict=False: len(PM.combos(m, strict, k))  # noqa: E731
    assert (n("反应池", 1), n("反应池", 2), n("反应池", 2, True), n("反应池", 3)) == (11, 16, 13, 0)
    assert (n("扩容反应池", 2), n("扩容反应池", 2, True), n("扩容反应池", 3), n("扩容反应池", 3, True), n("扩容反应池", 4)) == (55, 38, 63, 34, 0)
    assert len(PM.combos("反应池")) == 11 + 16 and len(PM.combos("扩容反应池")) == 11 + 55 + 63
    assert (YA, JIN) in PM.combos("反应池") or (JIN, YA) in PM.combos("反应池")


def test_check():
    ck = PM.check("反应池", [JIN, YA])
    assert ck["ok"] and ck["items"] == 5 and ck["slots"] == 5 and not ck["warn"], ck
    ck = PM.check("扩容反应池", [A, B, C])  # 官方教程：三条正好占满 8 格
    assert ck["ok"] and ck["items"] == 8, ck
    assert PM.check("扩容反应池", [A, B])["items"] == 6  # 中间产物液化息壤占一格，不是净配方的 5 种
    assert not PM.check("反应池", [A, B])["ok"]  # 6 种放不进 5 格
    ck = PM.check("反应池", [OIL, FODDER])  # 两种固体产物，带出口只有一个选择器：只警告
    assert ck["ok"] and ck["warn"] and "固体" in ck["warn"][0], ck
    assert not PM.check("反应池", [YA, YA])["ok"]  # 重复
    assert not PM.check("反应池", ["清水×1 → 芽针×1（2秒）"])["ok"]  # 不是这种池子的配方
    assert not PM.check("灌装机", [YA])["ok"]
    assert PM.terminal_io([A, B, C]) == {"ext_fluid": {"清水"}, "ext_solid": {"息壤", "蓝铁粉末"},
                                         "term_fluid": {"惰性壤晶废液"}, "term_solid": {"壤晶"}}


def test_groups():
    g = PM.groups("扩容反应池", [A, B, C])
    assert len(g) == 1 and g[0]["members"] == [A, B, C], g
    assert g[0]["recipe"] == "清水×1 + 息壤×1 + 蓝铁粉末×1 + 壤晶废液×1 → 惰性壤晶废液×1 + 壤晶×1（2秒）", g
    g = PM.groups("反应池", [YA, JIN])
    assert g == [{"recipe": YA, "members": [YA]}, {"recipe": JIN, "members": [JIN]}], g
    assert [x["recipe"] for x in PM.groups("反应池", [JIN, YA])] == [JIN, YA]  # 组按给的先后排：第一组是主配方
    assert PM.groups("扩容反应池", [C, B, A]) == PM.groups("扩容反应池", [A, B, C])  # 组内按配方表顺序并，文字和先后无关
    # 和面板「池产物」那条路（pool_channels.from_text）并出来的一样：武陵城扩容反应池 (1489,1055)
    wl = PC.from_text("扩容反应池", "池产物：惰性壤晶废液(0)/息壤(50)/壤晶废液(50)")
    assert [x["recipe"] for x in PM.groups("扩容反应池", [B, A])] == [PC.recipe_text(r) for r in wl]
    try:
        PM.groups("反应池", ["清水×1 → 芽针×1（2秒）"])
        raise AssertionError("认不出的配方文字应该报错")
    except ValueError:
        pass


def test_chan_rates():
    two = [{"清水": 30.0, "芽针粉末": 30.0}, {"清水": 30.0, "锦草粉末": 30.0}]
    full = {"芽针粉末": 30.0, "锦草粉末": 30.0}
    t, sc = PM.chan_rates(two, dict(full, 清水=60.0))
    assert t == [1.0, 1.0] and sc == {"清水": 1.0}, (t, sc)
    t, sc = PM.chan_rates(two, dict(full, 清水=30.0))
    assert t == [0.5, 0.5] and sc == {"清水": 0.5}, (t, sc)
    t, _ = PM.chan_rates(two, {"芽针粉末": 15.0, "锦草粉末": 30.0, "清水": 60.0})
    assert t == [0.5, 1.0], t
    # 一条的独有原料只够 0.2、清水 45：清水用掉 0.2×30 + 30 = 36（不是 45，也不是按 0.5 平分的 28.5）
    t, sc = PM.chan_rates(two, {"芽针粉末": 6.0, "锦草粉末": 30.0, "清水": 45.0})
    assert abs(t[0] - 0.2) < 1e-9 and t[1] == 1.0 and sc["清水"] == 1.0, (t, sc)
    assert abs(sum(nd["清水"] * x for nd, x in zip(two, t)) - 36.0) < 1e-9
    # 出料上限只压那一条：清水够时另一条照样满速
    t, _ = PM.chan_rates(two, dict(full, 清水=60.0), [0.25, None])
    assert t == [0.25, 1.0], t
    # 清水也不够：先按上限算各条要多少（7.5 + 30 = 37.5），再按比例分 30 → 0.2、0.8
    t, _ = PM.chan_rates(two, dict(full, 清水=30.0), [0.25, None])
    assert abs(t[0] - 0.2) < 1e-9 and abs(t[1] - 0.8) < 1e-9, t
    # 三条共用清水：按用量比例
    three = two + [{"清水": 30.0, "息壤": 30.0}]
    t, sc = PM.chan_rates(three, dict(full, 息壤=30.0, 清水=45.0))
    assert all(abs(x - 0.5) < 1e-9 for x in t) and abs(sc["清水"] - 0.5) < 1e-9, (t, sc)
    t, _ = PM.chan_rates(three, {"芽针粉末": 30.0, "锦草粉末": 30.0, "息壤": 0.0, "清水": 30.0})
    assert t[2] == 0.0 and all(abs(x - 0.5) < 1e-9 for x in t[:2]), t  # 没息壤的那条不分清水


def test_score_decide():
    # 管子进清水、带子进芽针粉末和锦草粉末、两根管子各收一种溶液：芽针 + 锦草两条最好
    links = [{"layer": "pipe", "dir": "in", "sup": {"清水"}, "dem": set()},
             {"layer": "belt", "dir": "in", "sup": {"芽针粉末", "锦草粉末"}, "dem": set()},
             {"layer": "pipe", "dir": "out", "sup": set(), "dem": {"芽针溶液"}},
             {"layer": "pipe", "dir": "out", "sup": set(), "dem": {"锦草溶液"}}]
    scored = [(PM.score(c, links)[0], c) for c in PM.combos("反应池")]
    best = PM.decide(scored)
    assert best is not None and set(best) == {YA, JIN}, sorted(scored, key=lambda t: -t[0])[:3]
    s, why = PM.score((YA,), links)
    assert why == [] and abs(s - (2 + 2 + 2 - 1 - 0.1)) < 1e-9, (s, why)  # 收锦草溶液的那根管子解释不了，扣 1
    s, why = PM.score((A,), links)
    assert "缺息壤" in why and "液化息壤没去处" in why, why
    assert PM.decide([(2.0, (YA,)), (1.5, (JIN,))]) is None  # 领先不到 1
    assert PM.decide([(-1.0, (YA,))]) is None


if __name__ == "__main__":
    sys.exit(run([test_recipes_same_as_design, test_is_fluid_matches_ids, test_combos_count, test_check, test_groups, test_chan_rates,
                  test_score_decide]))
