"""设计分析计算器：用 4.7 的已知实例核对（皇帝产线实测数据，debug/bpa/实测-皇帝产线）。

- 景玉谷中容封装机 (240,1615)：只有 1 条致密源石粉末带（研磨机 → 储存箱 → 封装机）→ 开工率 25% = 1.5/分。
- 武陵城装备原件机：重息壤准入口限速 12 → 分 3 路 → 回环分走一半 → 2/分。
- 仓库口物品按图标认（景玉谷 36 个都认得准）；配方按机身图标纠正（(507,581)/(507,640) 两台粉碎机历史配对配反）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import design as D  # noqa: E402

BASE = ROOT / "debug/bpa/实测-皇帝产线/base"
ARCH = ROOT / "data/blueprints/1.6皇帝产线"


def _run(area):
    return D.run_base(BASE / area, ARCH, area)


def test_jingyugu():
    r = _run("景玉谷")
    zr = [d for d in r["devices"] if "中容" in d["recipe"] and d["c"] == [240, 1615]]
    assert zr and abs(zr[0]["rate"] - 0.25) < 1e-6, zr
    ports = r["ports"]
    assert len(ports) >= 30 and all(p["sure"] for p in ports), [p for p in ports if not p["sure"]]
    fx = {(f["x"], f["y"]): f["to"] for f in r["recipe_fixes"]}
    assert fx.get((507, 581), "").startswith("砂叶") and fx.get((507, 640), "").startswith("源矿"), fx


def test_wulingcheng_loop():
    r = _run("武陵城")
    yj = [d for d in r["devices"] if "装备原件" in d["recipe"]]
    assert yj and abs(yj[0].get("available", {}).get("重息壤", 0) - 2.0) < 1e-6, yj  # 它缺灼铜零件转不起来，但重息壤这一路送得到 2/分
    # 塑形机的赤铜块来自精炼炉（作者确认，2026-11-09）：每台精炼炉底边两个出料口，一个直连塑形机（主路）、
    # 一个落进正下方 1 进 1 出的汇流器 → 配件机（备路：主路堵了才走，作者 11-03 讲的传送带优先级）。
    # B (1247,815) 吃两台精炼炉主路 → 60 满速；A (972,850) 吃一台 → 30（那段带子大半压在管子底下）；A 下面那台配件机拿不到溢流
    sx = {tuple(d["c"]): d for d in r["devices"] if d["device"] == "塑形机"}
    assert abs(sx[(1247, 815)]["got"]["赤铜块"] - 60) < 1e-6 and abs(sx[(972, 850)]["got"]["赤铜块"] - 30) < 1e-6, \
        (sx[(1247, 815)]["basis"], sx[(972, 850)]["basis"])
    pj = {tuple(d["c"]): d["got"].get("赤铜块") for d in r["devices"] if d["device"] == "配件机"}
    assert pj[(1078, 954)] < 1e-6, pj  # 汇流器接的是正上方那台精炼炉，且是备路
    # 情景（2026-11-10）：主路堵死（塑形机出料爆仓）时，精炼炉正下方的三台配件机各拿到 30 赤铜块
    sc = {tuple(d["c"]): d.get("if_main_blocked") for d in r["devices"] if d["device"] == "配件机"}
    for c in [(1078, 954), (1176, 954), (1384, 954)]:
        assert sc[c] and abs(sc[c]["got"]["赤铜块"] - 30) < 1e-6, (c, sc[c])
    # 左列砂叶循环（作者 2026-11-10：游戏里理论满速）：6 台种植机 + 3 台采种机 + 粉碎机 (88,440) 全满速。
    # 靠端口布局补上的两条一格弯带（种植机 (155,1158) → 采种机 (120,987)、采种机 (326,1463) → 种植机 (122,1431)）
    # 和「出口少的先分」（采种机 (326,1463) 两个去向先填满，(361,1123) 三个去向再分缺口）
    left = [d for d in r["devices"] if d["c"][0] < 520 and d["device"] in ("种植机", "采种机") or d["c"] == [88, 440]]
    assert len(left) == 10 and all(d["rate"] > 0.995 for d in left), [(d["device"], d["c"], d["rate"]) for d in left]
    # 清水（作者 2026-11-10：这些设备游戏里都在转）：每 2 个水口 120 → 4 台精炼炉；天有洪炉组 120 → 精炼炉 30 + 沿基段 90 → 3 台天有洪炉；
    # 采种机组 120 → 暗管 → 2 种植机 + 反应池 + 扩容反应池
    av = {tuple(d["c"]): d.get("available", {}).get("清水", 0) for d in r["devices"] if "清水" in d["need"]}
    for c in [(1489, 679), (1385, 748), (1714, 714), (1918, 714), (1679, 919), (908, 304), (1795, 1840), (1009, 1840), (1422, 1738)]:
        assert abs(av[c] - 30) < 1e-6, (c, av[c])


def test_yinglongguan_gas_priority():
    """4.7：外部 150 息壤气进多口暗管出口 M2，主路（直连暗管入口）先拿 120 去 M1，备路经 1 进 1 出汇流器 H2 → G5（限 30）只拿剩下 30，
    再经暗管 → 中间多口出口 → 储气罐 → 气体反应炉。作者给的量：5+6 号合计 150（inputs.json pool）。"""
    r = _run("应龙关")
    g5 = [g for g in r["gates"] if g["c"] == [60, 1570]]
    assert g5 and g5[0]["limit"] == 30 and abs((g5[0]["downstream"] or 0) - 30) < 1e-6, g5
    assert r["tunnels"] >= 16, r["tunnels"]
    fy = [d for d in r["devices"] if d["device"] == "气体反应炉" and d["c"] == [878, 373]]
    assert fy and fy[0].get("available", {}).get("息壤气", 0) > 0, fy  # 它缺气态赫铜平时不转（灼铜链溢流设计），但息壤气是送得到的
    # 液气转化机：液化息壤经「汇流器 → 准入口限 6 → 分流器（一路绕回汇流器）」只剩 6÷2 = 3/分（4.7）；配方补自游戏内确认（结构.json）
    yq = [d for d in r["devices"] if d["device"] == "液气转化机"]
    assert yq and any(b.endswith("→ 3") and "液化息壤" in b for b in yq[0]["basis"]), yq


def test_plant_alternation():
    """任务计划第 13 项：紧挨着的种植机（种锦草）和采种机（采芽针）对不上 → 两台都按「锦草 / 芽针轮换」交替配方算；对得上的不动。"""
    from bpa import vision as V
    C = V.CELL
    devices = [{"name": "种植机"}, {"name": "采种机"}, {"name": "种植机"}, {"name": "采种机"}]
    canvas = [(100, 100), (100 + 5 * C, 100), (100, 400), (100 + 5 * C, 400)]
    rec = {0: ("清水×1 + 锦草种子×1 → 锦草×2（2秒）", False), 1: ("芽针×1 → 芽针种子×1（2秒）", False),
           2: ("清水×1 + 芽针种子×1 → 芽针×2（2秒）", False), 3: ("芽针×1 → 芽针种子×1（2秒）", False)}
    alt = {}
    notes = D._plant_alternation({"links": []}, devices, canvas, rec, alt)
    assert set(alt) == {0, 1} and "芽针" in alt[0][0] and "锦草" in alt[1][0], alt
    assert len(notes) == 2 and all("轮换" in n for n in notes), notes
    # 天有洪炉：碳块 + 清水 → 息壤 要稳定环境（gasEnv=1），做重息壤那条是普通状态（作者 2026-11-10）
    assert D.env_required("天有洪炉", "碳块×1 + 清水×1 → 息壤×1（2秒）") is True
    assert D.env_required("天有洪炉", "壤晶废液×5 + 息壤×10 → 重息壤×1（10秒）") is False


if __name__ == "__main__":
    ok = 0
    for f in (test_jingyugu, test_wulingcheng_loop, test_yinglongguan_gas_priority, test_plant_alternation):
        try:
            f(); ok += 1
        except AssertionError as e:
            print("FAIL", f.__name__, str(e)[:300])
    print(f"{ok}/4 通过")
