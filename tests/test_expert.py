"""排查树（src/expert.py）各分支的离线用例：拿蓝图基准简报当「正常」，按错题本改几行数造故障，看排查树指到哪。

用法：.venv\\Scripts\\python.exe tests\\test_expert.py
"""

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import blueprint as bp  # noqa: E402
import expert  # noqa: E402

PLAN = bp.load("1.6皇帝产线")
REGION = "武陵"


def base() -> dict:
    """基准简报，关键产物库存改成没满（不然全按爆仓 ✓，测不到下面的分支）。"""
    b = copy.deepcopy(PLAN.reference_brief)
    for r in b[REGION]:
        if r["name"] in ("息壤", "灼铜块", "灼铜零件", "重息壤", "中容武陵电池") and not r.get("fluid"):
            r["stock"] = 40000
    return b


def setrow(b: dict, name: str, **kw):
    for r in b[REGION]:
        if r["name"] == name and not r.get("fluid"):
            r.update(kw)
            return
    raise KeyError(name)


def step(tree, product):
    return next(s for s in tree if s["product"] == product)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def normal_all_ok():
    tree = expert.run(base(), PLAN)
    bad = [s["product"] for s in tree if s["status"] == "bad"]
    assert not bad, bad


@case
def xirang_short_water():
    b = base()
    setrow(b, "息壤", cur_prod=240, theo_prod=240)  # 4 台洪炉没在算，惰气正常，碳块充足
    s = step(expert.run(b, PLAN), "息壤")
    assert s["status"] == "bad" and "缺的是水" in s["facts"][-1], s
    assert s["where"]["machines"] == ["天有洪炉", "水泵"], s["where"]


@case
def xirang_short_carbon():
    b = base()
    setrow(b, "息壤", cur_prod=240, theo_prod=240)
    setrow(b, "碳块", stock=300)
    s = step(expert.run(b, PLAN), "息壤")
    assert s["status"] == "bad" and "缺碳块" in s["facts"][-1], s
    assert s["where"]["areas"] == ["应龙关"] and "精炼炉" in s["where"]["machines"], s["where"]


@case
def xirang_gas_cut_even_if_full():
    """故障 C：惰气准入口被关，理论消耗 150→144。满仓也报（仓满只是把掉速盖住了）。"""
    b = base()
    setrow(b, "息壤", cur_prod=180, theo_prod=360, stock=89975)
    setrow(b, "惰气", cur_cons=131, theo_cons=144)
    s = step(expert.run(b, PLAN), "息壤")
    assert s["status"] == "bad" and "惰气" in s["cause"] and "仓库虽满" in s["cause"], s


@case
def xirang_full_low_gas_use_is_ok():
    """用户 2026-11-07 纠正：14:41–17:32 息壤 ~275/360 满仓、惰气当前 136/理论 150（理论没少）= 下游爆仓、洪炉停等用气少，不是断气。"""
    b = base()
    setrow(b, "息壤", cur_prod=274, theo_prod=360, stock=89994)
    setrow(b, "惰气", cur_cons=135, theo_cons=150)
    s = step(expert.run(b, PLAN), "息壤")
    assert s["status"] == "ok" and "不是断气" in s["facts"][-1], s


@case
def xirang_not_full_water_or_gas():
    """没满仓、降速、惰气当前用量低：缺水和缺气简报分不开（11-06 实际是缺水），报 ✗ 但不下缺气结论，指去洪炉面板分。"""
    b = base()
    setrow(b, "息壤", cur_prod=300, theo_prod=360, stock=55462)
    setrow(b, "惰气", cur_cons=108, theo_cons=150)
    s = step(expert.run(b, PLAN), "息壤")
    assert s["status"] == "bad" and "分不开" in s["facts"][-1] and "无环境" in s["advice"], s
    assert "气体散布机" in s["where"]["machines"] and "水泵" in s["where"]["machines"], s["where"]


@case
def zhuotong_81_xirang_gas():
    b = base()
    setrow(b, "灼铜块", cur_prod=60, theo_prod=60)
    setrow(b, "息壤气", cur_prod=250, theo_prod=260)
    s = step(expert.run(b, PLAN), "灼铜块")
    assert s["status"] == "bad" and s["facts"][-1].startswith("8.1") and s["where"]["checks"], s


@case
def zhuotong_82_hetong_gas():
    b = base()
    setrow(b, "灼铜块", cur_prod=60, theo_prod=60)
    setrow(b, "气态赫铜", cur_prod=120, theo_prod=120)
    s = step(expert.run(b, PLAN), "灼铜块")
    assert s["status"] == "bad" and s["facts"][-1].startswith("8.2"), s


@case
def zhuotong_83_impurity():
    b = base()
    setrow(b, "灼铜块", cur_prod=60, theo_prod=90)
    s = step(expert.run(b, PLAN), "灼铜块")
    assert s["status"] == "bad" and "8.3" in s["facts"][-1] and s["where"]["machines"] == ["提纯机"], s


@case
def zhuotong_downstream_full_is_ok():
    """灼铜零件爆仓 → 灼铜块在线上回堵 0/90（库存不动）：设计内（2026-11-08 晚实测）；灼铜零件没满时同样数值仍是 ✗。"""
    b = base()
    setrow(b, "灼铜块", cur_prod=0, theo_prod=90)
    setrow(b, "灼铜零件", cur_prod=0, stock=90000)
    s = step(expert.run(b, PLAN), "灼铜块")
    assert s["status"] == "ok" and "灼铜零件" in s["cause"], s
    setrow(b, "灼铜零件", stock=40000)
    assert step(expert.run(b, PLAN), "灼铜块")["status"] == "bad"


@case
def zhuotong_full_is_ok():
    b = base()
    setrow(b, "灼铜块", cur_prod=60, theo_prod=90, stock=89990)
    s = step(expert.run(b, PLAN), "灼铜块")
    assert s["status"] == "ok", s


@case
def zhongxirang_72_separator():
    b = base()
    setrow(b, "重息壤", cur_prod=12, theo_prod=12)
    setrow(b, "分离芯", stock=0)
    s = step(expert.run(b, PLAN), "重息壤")
    assert s["status"] == "bad" and s["facts"][-1].startswith("7.2"), s
    assert set(s["where"]["areas"]) == {"武陵城", "景玉谷"}, s["where"]


@case
def zhongxirang_73_impurity():
    b = base()
    setrow(b, "重息壤", cur_prod=12, theo_prod=24)
    s = step(expert.run(b, PLAN), "重息壤")
    assert s["status"] == "bad" and "7.3" in s["facts"][-1], s


@case
def downstream_skipped_when_xirang_bad():
    b = base()
    setrow(b, "息壤", cur_prod=240, theo_prod=240)
    setrow(b, "重息壤", cur_prod=12, theo_prod=24)
    s = step(expert.run(b, PLAN), "重息壤")
    assert s["status"] == "skip", s


@case
def zhongrong_blackout():
    b = base()
    setrow(b, "中容武陵电池", cur_prod=0, theo_prod=0, stock=0)
    s = step(expert.run(b, PLAN), "中容武陵电池")
    assert s["status"] == "bad" and "大停电" in s["cause"], s


@case
def zhongrong_ore_zero():
    b = base()
    setrow(b, "中容武陵电池", cur_prod=6, theo_prod=12)
    setrow(b, "蓝铁矿", theo_prod=0, cur_prod=0)
    s = step(expert.run(b, PLAN), "中容武陵电池")
    assert s["status"] == "bad" and "矿机" in s["facts"][-1], s


@case
def zhongrong_packer():
    b = base()
    setrow(b, "中容武陵电池", cur_prod=6, theo_prod=12)
    s = step(expert.run(b, PLAN), "中容武陵电池")
    assert s["status"] == "bad" and "封装机" in s["facts"][-1] and s["where"]["areas"] == ["景玉谷"], s


@case
def power_full():
    tree = expert.run(base(), PLAN, power="9700/9800")
    assert tree[0]["product"] == "电量" and tree[0]["status"] == "bad", tree[0]
    tree = expert.run(base(), PLAN, power="7215/9800")
    assert tree[0]["status"] == "ok", tree[0]


def main() -> int:
    fails = 0
    for fn in CASES:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  ✗ {fn.__name__}: {e}")
    print(f"{len(CASES) - fails}/{len(CASES)} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
