"""设计分析计算器（任务计划第 3 项；docs/蓝图分析器-设计.md 4.7）。

从第 2 节的输入端往下游推，给每台设备一条记录：设计开工率、预期状态、依据。
输入（都在据点目录 base/<据点>/ 或蓝图档案里，离线）：
- flow.json：物流件之间、物流件 ↔ 设备的有向连接（第 4 项），局部结构（单进单出汇流器、分流回环…）；
- 输入端（pipenet.load_inputs：作者填的优先，清水 60/口）；准入口限速、限制通过数量（作者在「准入口限速」表里填的、存在项目的
  fixes.json，优先；没填的用档案 ledger/gate_limits/<据点>.json 里游戏读到的）；
- 底账设备 + 配方（ledger/<据点>.json、<据点>_recipes.json，pipenet.recipe_table 补推断）。

模型（第一版，只按规则，不做逐帧模拟）：
- 来源：输入端准入口（作者流速；开了限速取 min；开了限制通过数量、数量知道 → 0，见下面准入口）；仓库取货口（每条带 30/分）；
  生产设备的产物（产量 × 它自己的开工率，迭代几轮）。暗管出口的量来自配对的暗管入口，第一版记「不知道」；
  没配对的外接暗管出口按作者标在它上面的输入端（物品、每分钟多少）算，没填按满管 120 估（0.10.13，见 tunnel_outlets）。
- 准入口：放行 min(来量, 限速)，物品不对的不放行（analyze 里的 gate_pass，推料、算下游吃得下多少都走它）。
  开了限制通过数量、数量 N 知道的一律不放行：它是一次性的，放满 N 个就停（用户 2026-11-09 实测），设计分析算的是长期开工率；
  线内的口、输入端，作者填的、游戏里读的都一样（0.10.13 A2）。N 不知道的不挡。
- 分流器：在接着的出口之间平分；某路下游吃不下（满了）多出来的分给其他路（水位分配）。
  经过「单进单出汇流器」的那一路是备路：主路都吃饱了才分给它（4.7 优先级）。绕回自己上游的那一路（分流回环）照样分走一份、不回到设备（4.7：设备拿到 L/n）。
- 汇流器：各路相加，封顶带子 30 / 管道 120。
- 设备：每种原料拿到的 ÷ 满速用量，取最小 = 设计开工率；燃料最低需求（散布机、固气/液气转化机 6/分）不够 → 罢工。
"""
from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import atomic
from . import pool_model as PM
from . import vision as V

BELT_CAP = 30.0
PIPE_CAP = 120.0
FUEL_MIN = {"气体散布机": 6.0, "固气转化机": 6.0, "液气转化机": 6.0}
MERGE_T = ("汇流器", "管道汇流器")
# RATE_LIMITED_INTAKE、MAIN_BLOCKED 只是 analyze 的默认值，运行中不再改它们，要换模式就给 analyze 传参数（2026-11-19）。
# 原来 run_base 靠改这两个模块变量切换三遍计算的模式；界面每次接口调用都在单独的线程里跑，两次设计分析同时跑（拼合任务和待确认的
# 重算撞上、「保存并重算」双击，同一个据点或两个不同据点都算）会互相改掉对方的开关。审计在副本上实测 21 轮并发 12 轮算错：
# 别人的武陵城 3 台封装机 (2080,1071)、(612,1652)、(2113,2029) 的 available / limit_by 错，皇帝应龙关 5 台的 got 错，
# 其中气体反应炉 (878,373) 在没料和溢流位之间翻转，错的结果还写进了 design.json
RATE_LIMITED_INTAKE = True  # 设备只按「另一种原料够跑多快」吃这种原料（False：每种原料各吃各的，用来算「能送到多少」）
BACKPRESSURE = True  # 2026-11-10 背压：产物全部去向都是确定的设备、吃不完时，生产者跟着减速（主路顶住 → 多出来的走备路）
MAIN_BLOCKED = False  # 第三遍（情景）：设备出料口的主路全堵死，看备路上的设备能拿到多少（塑形机出料堵住时精炼炉溢流到配件机）
# 出料堵死的设备复核那两遍 probe（见 analyze 内层后面）：probe 1「出料堵死的设备当它吃得下」、probe 2「整条线都不算出口堵塞」，
# 两遍一起开关：False = 都不跑。只给测试比对用（probe 不能改主分析的任何数字），运行中不改它
PROBE = True
BUFFERS = ("协议储存箱", "储气罐")  # 进来什么就往外出什么（缓冲中转）：景玉谷研磨机 → 储存箱 → 中容封装机（2026-11-09）
SOURCE_PORTS = ("仓库取货口", "暗管出口", "暗管入口", "多口暗管出口", "多口暗管入口")  # 往外出带子/管子的：仓库、暗管来料（物品和量看不到，按满带、到设备按缺口分）
# 反应池 → 格数（审计第 3 项，2026-11-19）：池子能同时跑几条配方（官方），所有在跑配方的物品种类不超过格数。
# 规则和纯函数在 pool_model；这里只认机器名
POOLS = PM.SLOTS
EPS = 1e-6
# 开工率迭代的轮数上限（2026-11-19 审计第 3 项）。INNER_MAX：出结果那一趟（原来就是 20）。
# 有并行池子时外层先定出料上限（最多 OUTER_MAX 轮，每轮一趟内层）：皇帝产线武陵城第一趟不带上限要 17 轮，离 20 只差 3 轮，
# 这几趟放宽到 CAP_INNER_MAX。合成场景里外层最多 6 轮（共用清水反向耦合）、三层池子串联 4 轮
INNER_MAX, CAP_INNER_MAX, OUTER_MAX = 20, 40, 8
# 作者说池子出口送原料时（0.10.13 功能三），外层定「到手的原料转给下游几成」最多几轮（每轮一趟内层，和出料上限同一个外层循环）。
# 作者武陵城扩容反应池 (1798,454) 5 轮（0.5 → 1 → 0.667 → 0.833 → 0.714）；跑满写说明、计入 hit_cap
PT_OUTER_MAX = 16


_RECIPES = None
_INFER_DEBUG = None  # 调试：lambda d, c: bool


def _pool_recipe(rc: str, machine: str, nth: int = 0) -> str:
    """「池产物：液化息壤(50)/重息壤(0)/…」→ 「清水×1 + 息壤×1 → 液化息壤×1（2秒）」（data/recipes.json 里这种池子、数量 >0 的第一个产物）。
    2026-11-09 应龙关扩容反应池：原来当成什么都不产，液气转化机的液化息壤一路是 0（应为 6÷2 = 3/分）。"""
    global _RECIPES
    if _RECIPES is None:
        from paths import ROOT
        rec = json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))
        names = json.loads((ROOT / "data" / "item_id_names.json").read_text(encoding="utf-8"))
        _RECIPES = [(r.get("machine_name"), {names.get(k, k): v for k, v in r["inputs"].items()},
                     {names.get(k, k): v for k, v in r["outputs"].items()}, r.get("seconds"), r.get("env", 0)) for r in rec]
    if not rc:
        return ""
    # 池子能同时跑几条配方，面板列的物品不全是产物（息壤、重息壤是原料）；首尾相接的几条并成一条净配方（pool_channels）
    # 2026-11-10 武陵城扩容反应池 (1489,1055)：息壤 / 壤晶废液 / 惰性壤晶废液 → 清水 + 息壤 + 污水 → 两种废液（原来只认出不用清水的那条）
    import pool_channels as PC
    found = [PC.recipe_text(r) for r in PC.from_text(machine, rc)]
    return found[nth] if len(found) > nth else ""


def _machine_recipes(machine: str) -> list[str]:
    """data/recipes.json 里这种机器的全部配方（写成「A×1 + B×1 → C×1（2秒）」）。"""
    _pool_recipe("", machine)  # 载入 _RECIPES
    out = []
    for m, ins, outs, sec, _env in _RECIPES:
        if m == machine and sec:
            out.append(" + ".join(f"{k}×{v:g}" for k, v in ins.items()) + " → " +
                       " + ".join(f"{k}×{v:g}" for k, v in outs.items()) + f"（{sec:g}秒）")
    return out


def _recipes_of(rec, i, extra=None) -> list[str]:
    """第 i 台在跑的全部配方：主配方 + extra[i]（交替配方、并行池子的其余几条；analyze 里由 recipes_of 生成）。
    2026-11-19 审计第 3 项：池子要能同时跑几条配方，原来这些地方只认主配方、最多再加一条交替配方
    （武陵城反应池 (1357,1332) 芽针溶液 / 锦草溶液两条，第二条的原料产物在判连线方向、仓库口取存时看不到）。"""
    return [rec.get(i, ("", False))[0]] + list((extra or {}).get(i, ()))


SPLIT_T = ("分流器", "管道分流器")
ICON_GUARD_SCORE, ICON_GUARD_MARGIN = 0.55, 0.25  # 机身图标认准了产物（= recipe_icons.SURE_SCORE / SURE_MARGIN）时，按邻居推配方不换成产物不同的配方
# （见 _infer_by_neighbors）。2026-11-21 评审：原来领先 0.10 就护，正好落在 recipe_icons「没把握、标推断、交给邻居重挑」的区间里
# （低容 / 中容武陵电池 0.81 对 0.63 那种认错），等于把邻居推断在这个区间里关掉了；改成只护认准了、只是同产物几条配方还没挑定的。


def _junction_role(n: dict, side) -> str | None:
    """物流件 n 的 side 那一边是进口还是出口（'in' / 'out'）：分流器一进多出、汇流器多进一出（flow 认出的 in / outs、out / ins）。
    别的物流件（桥、准入口）、方向没认出来的 → None。模板和明暗说法不一致（covered）、又没被小三角改过（fixed_by_arrows）的
    也当没认准 → None，不分兄弟（2026-11-21 评审：朝向猜反时真正送料的那台会被当成兄弟，它的产物就不算了）。"""
    if n.get("covered") and not n.get("fixed_by_arrows"):
        return None
    if n.get("type") in SPLIT_T and n.get("in"):
        return "in" if side == n["in"] else "out" if side in (n.get("outs") or ()) else None
    if n.get("type") in MERGE_T and n.get("out"):
        return "out" if side == n["out"] else "in" if side in (n.get("ins") or ()) else None
    return None


def _infer_by_neighbors(flow, devices, dev_canvas, rec, marks, depth=4, alt=None, par=None, icon=None) -> list[dict]:
    """配方是推断出来的设备（配方表里没有：没读到 / 池子只记了池产物）：按物流图上相邻的、配方可信的设备重挑。
    从这台设备顺着连线（不管方向；穿过物流件和储存箱，不穿过别的生产设备）找邻居：邻居产的东西可能是原料，邻居吃的东西可能是产物；
    输入端准入口的物品（作者填的清水等）也算原料。每个候选配方：原料有人产 +1，产物有人吃 +1；唯一最高分且高过现在的才换。
    2026-11-10 武陵城：粉碎机 (1572,1292) 配方表里没有，按本据点最常见的猜成砂叶 → 砂叶粉末；它下面接着芽针种植机，
    其实是芽针 → 芽针粉末（→ 储存箱 → 反应池做芽针溶液，作者说那台反应池游戏里在转）。
    兄弟设备的产物不算「有人产」（2026-11-21 issue #1 第五轮续）：挂在同一个分流器的几个出口、同一个汇流器的几个进口、
    同一个只进不出的储存箱的几个进口上的设备是兄弟，路上在这几处「拐到另一个出口 / 进口」走到的设备和输入端都是兄弟那一路，
    它们产的东西送不到这台（兄弟吃的照旧算：只管「有人产」这一边）。同一台设备另有不拐弯的路走得到的，照常算。
    别人的武陵城天有洪炉 (698,582) 做息壤（机身图标息壤 0.69），它和隔壁天有洪炉 (538,562) 共用一个清水分流器（拼合图 405,507），
    息壤又都送进同一个只进不出的协议储存箱（底账 613,563）；两条路都把隔壁产的息壤算成了它的原料，它被改成「壤晶废液 + 息壤 → 重息壤」，
    成了没料。只按层（管子只算流体）或只排除储存箱都不够：两条路同一深度，堵掉一条另一条照样到。
    皇帝产线景玉谷天有洪炉 (855,896) 是同一个毛病（实测做息壤、在转），改完按息壤算。
    icon：{设备序号: (机身图标认的产物, 分, 领先第二像多少)}（只有按机身图标推配方的据点才有，recipe_icons.derive 的 product / score / margin）。
    分 ≥ICON_GUARD_SCORE 且领先 ≥ICON_GUARD_MARGIN（认准了，和 recipe_icons 同一个门槛）时，邻居推断不换成产物里没有它的配方
    （同一种产物换原料的那一步不受影响）。上面那台天有洪炉图标领先只有 0.17，没认准，这道保险不管它，靠兄弟规则留住息壤
    （第一轮兄弟路上没人产东西，不换；第二轮精炼炉 (448,622) 的配方改好了，碳块有人产，现在的配方得分，不换）。"""
    def dev_of(dv):
        best, bd_ = None, 1e9
        for i, (d, c) in enumerate(zip(devices, dev_canvas)):
            if d["name"] == dv["name"]:
                dist = float(np.hypot(c[0] - dv["c"][0], c[1] - dv["c"][1]))
                if dist < bd_:
                    best, bd_ = i, dist
        return ("d", best) if best is not None and bd_ < 3 else None

    def nb_recipes(j):
        """邻居 j 在跑的配方：主配方 + 交替配方 / 并行池子的其余几条（这一趟里 alt 会被改，现查）。"""
        more = [c["recipe"] for c in (par or {}).get(j, [])[1:]] + ([alt[j][0]] if alt and j in alt else [])
        return _recipes_of(rec, j, {j: more})
    nodes = {n["id"]: n for n in flow["nodes"]}
    ends, into_box = [], defaultdict(list)
    for lk in flow["links"]:  # 只看物流图的连线，不管方向（在建图之前做：方向没定的带子要靠改好的配方定方向）
        a = dev_of(lk["a_dev"]) if lk.get("a_dev") else (("n", lk["a"]) if lk.get("a") is not None else None)
        b = dev_of(lk["device"]) if lk.get("device") else (("n", lk["b"]) if lk.get("b") is not None else None)
        ends.append((lk, a, b))
        for x, inward in ((a, lk.get("dir") == "b->a"), (b, lk.get("dir") == "a->b")):
            if x and x[0] == "d" and devices[x[1]]["name"] in BUFFERS:
                into_box[x[1]].append(inward)
    # 只进不出的储存箱 / 储气罐：连着它的线方向全定了、全往里送（有一根往外或方向没定的都不算）
    sink = {j for j, v in into_box.items() if v and all(v)}

    def role(x, lk, side_key, node_key):
        """这根线在 x 这头接的是进口还是出口：分流器 / 汇流器按边认，只进不出的储存箱都是进口，别的 None。"""
        if x[0] == "n":
            return _junction_role(nodes.get(lk.get(node_key), {}), lk.get(side_key))
        return "in" if x[1] in sink else None

    def turned(x, r1, r2):
        """从 r1 那根线进到 x、从 r2 那根线出去，是不是拐到兄弟那边：分流器出口 → 出口，汇流器 / 只进不出的储存箱进口 → 进口。"""
        if r1 is None or r1 != r2:
            return False
        if x[0] == "d":
            return x[1] in sink
        t = nodes.get(x[1], {}).get("type")
        return t in SPLIT_T if r1 == "out" else t in MERGE_T
    adj = defaultdict(set)  # 顶点 → {(邻居, 这根线在自己这头的口, 在邻居那头的口)}
    for lk, a, b in ends:
        if a and b and a != b:
            ra, rb = role(a, lk, "a_side", "a"), role(b, lk, "b_side", "b")
            adj[a].add((b, ra, rb)); adj[b].add((a, rb, ra))
    gate_item = {}
    for m in marks or []:
        if m.get("item"):
            for n in flow["nodes"]:
                if abs(n["c"][0] - m["p"][0]) <= 20 and abs(n["c"][1] - m["p"][1]) <= 20:
                    gate_item[n["id"]] = m["item"]
    fixes = []
    fixed = set()
    unfed = {}  # 配方是推断的、原料邻居里没人送的设备 → 它现在的配方（留给最后「跟着同类取」那一步）
    for _round in range(2):
        changed = False
        for i, d in enumerate(devices):
            rc, inferred = rec.get(i, ("", False))
            if not inferred or d["name"] in BUFFERS or d["name"] in PORT_NAMES:
                continue
            if par and i in par:
                # 池产物已经读出几条并行配方的池子整台不动（2026-11-19）：下面「唯一最高」那条会把它换成一条，并行的另几条就丢了
                continue
            cands = _machine_recipes(d["name"])
            if len(cands) < 2:
                continue
            made, eaten = set(), set()
            makers = defaultdict(list)  # 物品 → 产它的邻居的配方（输入端准入口记空串）
            # 走到过的状态：(顶点, 进来那根线在它这头的口（只有分流器 / 汇流器 / 只进不出的储存箱要记）, 是不是兄弟那一路)。
            # 兄弟那一路先走到的顶点，后来又有不拐弯的路走到，再算一遍（这回它产的算数）；反过来不用再走
            start = ("d", i)
            seen, frontier = set(), [(start, None, False)]
            for _ in range(depth):
                nxt = []
                for u, r_in, sib in frontier:
                    for v, r_u, r_v in adj[u]:
                        if v == start:
                            continue
                        s = sib or turned(u, r_in, r_u)
                        rk = r_v if v[0] == "n" or v[1] in sink else None
                        if (v, rk, False) in seen or (v, rk, s) in seen:
                            continue
                        seen.add((v, rk, s))
                        if v[0] == "n":
                            if v[1] in gate_item and not s:
                                made.add(gate_item[v[1]])
                                makers[gate_item[v[1]]].append("")
                            nxt.append((v, rk, s))
                        elif devices[v[1]]["name"] in BUFFERS:
                            nxt.append((v, rk, s))
                        elif not rec.get(v[1], ("", True))[1] or v[1] in fixed:  # 配方可信的邻居（含上一轮按邻居改好的）
                            for r in nb_recipes(v[1]):  # 邻居同时跑 / 交替跑的几条配方都算
                                eaten |= set(inputs_of(r))
                                if s:  # 兄弟产的送不到这台
                                    continue
                                made |= set(outputs_of(r))
                                for k in outputs_of(r):
                                    makers[k].append(r)
                frontier = nxt

            def score(r):
                return len(set(inputs_of(r)) & made) + len(set(outputs_of(r)) & eaten)
            sc = sorted(((score(r), r) for r in cands), reverse=True)
            best, cur = sc[0], score(rc) if rc else 0
            ic = (icon or {}).get(i)
            sure = ic[0] if ic and ic[1] >= ICON_GUARD_SCORE and ic[2] >= ICON_GUARD_MARGIN else None

            def icon_says_no(rs):
                """机身图标认准了产物，换过去的几条配方都不产它 → 不换。"""
                return sure is not None and not any(sure in outputs_of(r) for r in rs)
            if _INFER_DEBUG and _INFER_DEBUG(d, dev_canvas[i]):
                print("INFER", d["name"], dev_canvas[i], "cur", cur, rc, "made", made, "eaten", eaten, sc[:4], "icon", ic)
            # 只换「现在这个推断配方一点邻居支持都没有」的：有支持的不动（反向配方——气→固、粉末→块——原料产物邻居里都有，分数会更高）
            if alt is not None and i not in alt and cur == 0 and len(sc) >= 2 and sc[0][0] == sc[1][0] >= 1                     and (len(sc) < 3 or sc[2][0] < sc[0][0]):
                r1, r2 = sc[0][1], sc[1][1]
                own1, own2 = set(inputs_of(r1)) - set(inputs_of(r2)), set(inputs_of(r2)) - set(inputs_of(r1))
                if own1 & made and own2 & made and not icon_says_no((r1, r2)):  # 两种配方各自的原料都有人送进来：池子两种都做（扩容反应池吃芽针粉末和锦草粉末）
                    if par is not None and d["name"] in POOLS:
                        # 池子两条同时跑（官方：池子能同时跑几条配方；2026-11-19 审计第 3 项，原来按轮流做算）。
                        # 皇帝产线武陵城扩容反应池 (1313,1114)：芽针粉末、锦草粉末两边都有粉碎机送
                        fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rc, "to": f"{r1} + 并行 {r2}",
                                      "how": "按相邻设备推断：两种原料都有人送进来，两条配方同时跑（并行，推断）"})
                        par[i] = [{"recipe": r, "members": [r], "src": "邻居推断"} for r in (r1, r2)]
                    else:
                        fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rc, "to": f"{r1} / 交替 {r2}",
                                      "how": "按相邻设备推断：两种原料都有人送进来，两种配方轮流做"})
                        alt[i] = (r2, "相邻设备送进来两种原料，按两种配方轮流做算（推断）")
                    rec[i] = (r1, True)
                    fixed.add(i)
                    changed = True
                    continue
            reverse = rc and set(inputs_of(best[1])) <= set(outputs_of(rc)) and set(outputs_of(best[1])) <= set(inputs_of(rc))
            if best[0] >= 1 and (len(sc) < 2 or sc[1][0] < best[0]) and cur == 0 and best[1] != rc and not reverse \
                    and not icon_says_no((best[1],)):  # 正反两个方向的配方（固气转化机）靠邻居分不出
                fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rc, "to": best[1],
                              "how": f"按相邻设备推断（原料有人产/产物有人吃 {best[0]} 项）"})
                rec[i] = (best[1], True)
                fixed.add(i)
                changed = True
                continue
            # 同一种产物的几条配方（精炼炉做碳块：芽针 / 砂叶 / 锦草 / 荞花各一条）：现在这条「有支持」只是因为产物有人吃，
            # 上面那条规则就不动它了；可它的原料邻居里没人送，另一条的原料有人送 → 换成有人送的那条。
            # 产物一样，碰不上正反两个方向的配方（2026-11-14 issue #1：武陵城 6 台做碳块的精炼炉 5 台按砂叶算成没料，作者说用的是芽针）。
            # 原料只由「吃这台产物」的邻居产的不算有人送：皇帝产线武陵城精炼炉 (1870,699) 做蓝铁块，旁边的粉碎机拿蓝铁块磨蓝铁粉末，
            # 绕一圈，原料其实是蓝铁矿（和 recipe_icons.pick_recipe 同一条规矩）
            if rc and i not in fixed:
                same = [r for r in cands if set(outputs_of(r)) == set(outputs_of(rc))]

                def sent(r):
                    return [k for k in inputs_of(r) if k in made and not all(set(outputs_of(r)) & set(inputs_of(q)) for q in makers[k])]
                fed = sorted(((len(sent(r)), r) for r in same), reverse=True)
                if len(fed) >= 2 and fed[0][0] > fed[1][0] and fed[0][1] != rc:
                    fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rc, "to": fed[0][1],
                                  "how": "按相邻设备推断：做" + "、".join(outputs_of(rc)) + f"的配方有 {len(same)} 条，相邻设备送来的是"
                                         + "、".join(sent(fed[0][1]))})
                    rec[i] = (fed[0][1], True)
                    fixed.add(i)
                    unfed.pop(i, None)
                    changed = True
                elif len(same) >= 2 and fed[0][0] == 0:
                    unfed[i] = rc
        if not changed:
            break
    # 还是定不下来的（物流图里没接上、邻居里没人送原料）：同据点同种机器做同一种产物的，按邻居定下来的都是同一条 → 跟着取。
    # 原来的配方是 recipe_icons.pick_recipe「按原料分不出来，先取一条」，本来就是随便取的
    for i, rc in unfed.items():
        if i in fixed:
            continue
        peers = {rec[j][0] for j in fixed if devices[j]["name"] == devices[i]["name"] and set(outputs_of(rec[j][0])) == set(outputs_of(rc))}
        if len(peers) == 1 and rc not in peers:
            to = peers.pop()
            n = sum(1 for j in fixed if rec[j][0] == to and devices[j]["name"] == devices[i]["name"])
            fixes.append({"device": devices[i]["name"], "x": devices[i]["x"], "y": devices[i]["y"], "from": rc, "to": to,
                          "how": f"这台的原料没看到有谁送；同据点做同一种产物的 {n} 台{devices[i]['name']}按相邻设备定的是这条，跟着取"})
            rec[i] = (to, True)
    return fixes


UNK_BELT, UNK_PIPE = "*", "*管"  # 物品没看到的来料：带子来的（仓库取货口）/ 管子来的（暗管出口）。到设备再按缺口分
UNKNOWN = (UNK_BELT, UNK_PIPE)
GATE_WRONG = "物品不对"  # analyze 里 gate_pass 的「为什么」：准入口设的物品和来的料不一样，一点不过


def _is_fluid(item: str) -> bool:
    """走管子的东西（液体、气体）。固体只走带子。规则在 pool_model.is_fluid（池子格数、选择器也按它分流体 / 固体）。"""
    return PM.is_fluid(item)


def _unknown_fits(unk: str, item: str) -> bool:
    """物品没看到的来料 unk 能不能当作 item：管子里来的只能是流体。
    2026-11-14 别人的武陵城：暗管出口 → 管道分流器 → 天有洪炉（碳块 + 清水 → 息壤），管子里的 120/分被「当作碳块」，清水算成没人送。
    带子来的照旧不限制：按理只能是固体，但皇帝产线武陵城塑形机 (844,423)（赤铜块 + 惰气）两条取货口带子里有一条一直被「当作惰气」，
    它的惰气管在物流图里没连上；限制以后它从间歇 50% 变成没料。先不动，等惰气那条连线查清再收紧。"""
    return unk != UNK_PIPE or _is_fluid(item)


def _is_fuel(item: str, recipe: str) -> bool:
    """转化机/散布机的燃料：送进来的气，不是配方原料也不是产物。"""
    return "气" in item and item not in inputs_of(recipe) and item not in outputs_of(recipe)


def _fuel_got(sup_i: dict, recipe: str) -> float:
    return sum(v for k, v in sup_i.items() if _is_fuel(k, recipe))


def env_required(machine: str, recipe: str) -> bool | None:
    """这个配方要不要稳定环境（AKEData FactoryMachineCraftTable.gasEnv = 1）。天有洪炉：碳块×1 + 清水 → 息壤 要稳定环境，
    普通状态下是稳定碳块×2 + 清水 → 息壤、壤晶废液×5 + 息壤×10 → 重息壤（作者 2026-11-10）。认不出是哪条配方 → None。"""
    if not recipe or "→" not in recipe:
        return None
    _pool_recipe("", machine)
    ins, outs = inputs_of(recipe), outputs_of(recipe)
    for m, a, b, sec, env in _RECIPES:
        if m != machine or not sec:
            continue
        if {k: v * 60.0 / sec for k, v in a.items()} == ins and {k: v * 60.0 / sec for k, v in b.items()} == outs:
            return bool(env)
    return None


def per_min(recipe: str, item: str) -> float:
    """配方文字里 item 作为原料的每分钟用量（「碳块×1 + 清水×1 → 息壤×1（2秒）」）。"""
    if not recipe or "→" not in recipe:
        return 0.0
    left = recipe.split("→")[0]
    m = re.search(rf"(?:^|[\s+]){re.escape(item)}×(\d+(?:\.\d+)?)", left)
    t = re.search(r"（(\d+(?:\.\d+)?)秒）", recipe)
    return float(m.group(1)) * 60.0 / float(t.group(1)) if m and t else 0.0


def inputs_of(recipe: str) -> dict:
    if not recipe or "→" not in recipe:
        return {}
    t = re.search(r"（(\d+(?:\.\d+)?)秒）", recipe)
    if not t:
        return {}
    sec = float(t.group(1))
    return {nm.strip(): float(n) * 60.0 / sec for nm, n in re.findall(r"([^\s+×→]+)×(\d+(?:\.\d+)?)", recipe.split("→")[0])}


def outputs_of(recipe: str) -> dict:
    if not recipe or "→" not in recipe:
        return {}
    t = re.search(r"（(\d+(?:\.\d+)?)秒）", recipe)
    if not t:
        return {}
    sec = float(t.group(1))
    right = re.sub(r"（.*?）", "", recipe.split("→")[1])
    return {nm.strip(): float(n) * 60.0 / sec for nm, n in re.findall(r"([^\s+×→]+)×(\d+(?:\.\d+)?)", right)}


# ------------------------------------------------------------------ 图
class Graph:
    """节点：('n', 物流件 id) / ('d', 底账设备序号)。边：有向，带层（belt/pipe）和连接序号。"""

    def __init__(self, flow: dict, devices: list[dict], dev_canvas: list, rec: dict | None = None):
        self.flow = flow
        self.nodes = flow["nodes"]
        self.devices = devices
        self.out = defaultdict(list)   # u → [(v, link)]
        self.inn = defaultdict(list)
        self.guessed = []
        for lk in flow["links"]:
            if lk.get("free"):
                a = self._dev(lk["a_dev"], dev_canvas) if lk.get("a_dev") else None
                b = self._dev(lk["device"], dev_canvas) if lk.get("device") else None
                if a is None or b is None or a == b:
                    continue
                if devices[a[1]]["name"] == devices[b[1]]["name"] == "仓库取货口":
                    # 取货口只往外出：两个取货口之间不会有带子。追线把取货口上面那一格短带追歪了、两头都落在取货口上
                    # （2026-11-14 别人的武陵城 (1292,1650)：占着取货口的出边，后面「取货口紧贴设备」就不补了，上面的粉碎机没接上）
                    continue
                d = lk.get("dir") or _guess_dir(devices[a[1]]["name"], devices[b[1]]["name"], rec.get(a[1], ("", 0))[0] if rec else "",
                                                 rec.get(b[1], ("", 0))[0] if rec else "")
                na, nb = devices[a[1]]["name"], devices[b[1]]["name"]
                forced = ("a->b" if na == "仓库取货口" or nb == "仓库存货口" else "b->a" if nb == "仓库取货口" or na == "仓库存货口" else None)
                if forced and not (na.startswith("仓库") and nb.startswith("仓库")) and d != forced:
                    # 取货口只往外出、存货口只往里进：比小三角可靠（武陵城配件机 (767,1396) 那条被判成「配件机 → 取货口」，2026-11-10）
                    lk = dict(lk, dir=forced, basis=lk.get("basis", []) + [f"接{'仓库取货口：只出不进' if 'a->b' == forced and na == '仓库取货口' or forced == 'b->a' and nb == '仓库取货口' else '仓库存货口：只进不出'}"])
                    d = forced
                if d is None and lk["layer"] == "pipe":  # 储气罐/储存箱 ↔ 设备的管子没票：设备吃流体 → 中转往设备送；设备产流体 → 往中转里送
                    for x, y, dd in ((a, b, "a->b"), (b, a, "b->a")):
                        if devices[x[1]]["name"] in BUFFERS and devices[y[1]]["name"] not in BUFFERS and rec:
                            rc = rec.get(y[1], ("", 0))[0]
                            if any("气" in k or "液" in k or k in ("清水", "污水", "沉积酸") for k in inputs_of(rc)):
                                d = dd
                            elif any("气" in k or "液" in k for k in outputs_of(rc)):
                                d = "b->a" if dd == "a->b" else "a->b"
                if d is None:
                    continue
                if not lk.get("dir"):
                    self.guessed.append(lk)
                    lk["basis"] = lk.get("basis", []) + ["按两头设备的配方/端口推断"]
                u, v = (a, b) if d == "a->b" else (b, a)
                self.out[u].append((v, lk)); self.inn[v].append((u, lk))
                continue
            if not lk.get("dir") and lk["end"] == "device" and lk.get("device"):
                nm = lk["device"]["name"]  # 暗管：只会往入口里进、从出口往外出（小三角票不够时按这个定方向）
                if "暗管入口" in nm or nm == "仓库存货口":
                    lk["dir"] = "a->b"; lk["basis"] = lk.get("basis", []) + [f"接{nm}：只进不出"]
                elif "暗管出口" in nm or nm == "仓库取货口":
                    lk["dir"] = "b->a"; lk["basis"] = lk.get("basis", []) + [f"接{nm}：只出不进"]
            if not lk.get("dir"):
                continue
            a = ("n", lk["a"])
            if lk.get("b") is not None:
                b = ("n", lk["b"])
            elif lk["end"] == "device" and lk.get("device"):
                b = self._dev(lk["device"], dev_canvas)
                if b is None:
                    continue
            else:
                continue
            u, v = (a, b) if lk["dir"] == "a->b" else (b, a)
            self.out[u].append((v, lk))
            self.inn[v].append((u, lk))

    def _dev(self, dv, dev_canvas):
        best, bd = None, 1e9
        for i, (d, c) in enumerate(zip(self.devices, dev_canvas)):
            if d["name"] != dv["name"]:
                continue
            dist = float(np.hypot(c[0] - dv["c"][0], c[1] - dv["c"][1]))
            if dist < bd:
                best, bd = i, dist
        return ("d", best) if best is not None and bd < 3 else None

    def kind(self, u):
        return self.nodes[u[1]]["type"] if u[0] == "n" else self.devices[u[1]]["name"]


INFER_WORDS = ("推断", "按格子补", "贴着", "紧贴")
INFER_TAG = "〔推断连线〕"


def _is_inferred(lk) -> bool:
    """这条连线不是沿带子/管子追出来、两头方向都看到的：按配方/端口推方向、紧贴着直接对接、按格子补的短带…"""
    return any(w in b for b in (lk.get("basis") or []) for w in INFER_WORDS)


def _tag(path, lk):
    return path + [INFER_TAG] if lk.get("inferred") and INFER_TAG not in path else path


def _guess_dir(na, nb, ra, rb):
    """两台设备之间没读到小三角的短带：仓库取货口只出、仓库存货口/协议储存箱只进；否则 A 的产物是 B 的原料 → A→B。"""
    src = ("仓库取货口", "暗管出口", "多口暗管出口")
    snk = ("仓库存货口", "暗管入口", "多口暗管入口")  # 协议储存箱是中转，不是只进
    if na in src or nb in snk:
        return "a->b"
    if nb in src or na in snk:
        return "b->a"
    ab = set(outputs_of(ra)) & set(inputs_of(rb))
    ba = set(outputs_of(rb)) & set(inputs_of(ra))
    if ab and not ba:
        return "a->b"
    if ba and not ab:
        return "b->a"
    return None


def _chevrons(gray, x0, y0, x1, y1) -> list:
    """区域里设备口格上的小折角（~9×4 px 的 V）：→ [尖朝向 U/D/L/R]。尖朝哪边，东西就往哪边走。"""
    import cv2 as _cv
    H, W = gray.shape
    x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(W, int(x1)), min(H, int(y1))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return []
    sub = gray[y0:y1, x0:x1]
    n, lab, st, _ = _cv.connectedComponentsWithStats((sub < 175).astype(np.uint8), connectivity=8)
    out = []
    for k in range(1, n):
        x, y, w, h, a = st[k]
        if a < 6 or a > 30:
            continue
        ys, xs = np.nonzero(lab == k)
        if 6 <= w <= 13 and 3 <= h <= 6:
            c = x + (w - 1) / 2
            mid, edge = np.abs(xs - c) <= 1.5, np.abs(xs - c) >= (w - 1) / 2 - 1.5
            if mid.any() and edge.any() and abs(ys[mid].mean() - ys[edge].mean()) >= 0.6:
                out.append("U" if ys[mid].mean() < ys[edge].mean() else "D")
        elif 6 <= h <= 13 and 3 <= w <= 6:
            c = y + (h - 1) / 2
            mid, edge = np.abs(ys - c) <= 1.5, np.abs(ys - c) >= (h - 1) / 2 - 1.5
            if mid.any() and edge.any() and abs(xs[mid].mean() - xs[edge].mean()) >= 0.6:
                out.append("L" if xs[mid].mean() < xs[edge].mean() else "R")
    return out


def glyph_sides(gray, box) -> dict:
    """设备外框四条边各是物品进口还是出口：口格里的小折角朝外 = 出、朝里 = 进（2026-11-09 看图确认：
    精炼炉上进下出、转过的塑形机右进左出、武陵城左列种植机/采种机都是上出下进）。
    只认粗黑边框那几条边（物品口），或折角 ≥3 个；管口半圆里的 > 不算。→ {"N"/"S"/"W"/"E": "in"/"out"}"""
    x, y, w, h = box
    bands = {"N": (x + 4, y + 2, x + w - 4, y + 24), "S": (x + 4, y + h - 24, x + w - 4, y + h - 2),
             "W": (x + 2, y + 4, x + 24, y + h - 4), "E": (x + w - 24, y + 4, x + w - 2, y + h - 4)}
    H, W = gray.shape

    def thick(sd):
        if sd in "NS":
            ys = range(y - 3, y + 2) if sd == "N" else range(y + h - 2, y + h + 3)
            return any(0 <= yy < H and (gray[yy, max(0, x + 4):min(W, x + w - 4)] < 70).mean() >= 0.7 for yy in ys)
        xs = range(x - 3, x + 2) if sd == "W" else range(x + w - 2, x + w + 3)
        return any(0 <= xx < W and (gray[max(0, y + 4):min(H, y + h - 4), xx] < 70).mean() >= 0.7 for xx in xs)
    res = {}
    for sd, (a, b, c, d) in bands.items():
        cs = [k for k in _chevrons(gray, a, b, c, d) if k in (("U", "D") if sd in "NS" else ("L", "R"))]
        if len(cs) < 2 or (len(cs) < 3 and not thick(sd)):
            continue
        top = max(set(cs), key=cs.count)
        if cs.count(top) >= 0.8 * len(cs):
            res[sd] = "out" if top == {"N": "U", "S": "D", "W": "L", "E": "R"}[sd] else "in"
    return res


PLANTERS = ("种植机", "采种机")


def _plant_of(machine: str, recipe: str) -> str | None:
    """种植机配方 → 种的植物（产物）；采种机配方 → 采的植物（原料）。"""
    if machine == "种植机":
        outs = [k for k in outputs_of(recipe) if k not in ("清水",)]
        return outs[0] if len(outs) == 1 else None
    if machine == "采种机":
        ins = [k for k in inputs_of(recipe) if k not in ("清水",)]
        return ins[0] if len(ins) == 1 else None
    return None


def _plant_recipe(machine: str, plant: str) -> str | None:
    """这种机器种 / 采 plant 的配方。"""
    for r in _machine_recipes(machine):
        if _plant_of(machine, r) == plant:
            return r
    return None


def _plant_alternation(flow, devices, dev_canvas, rec, alt) -> list[str]:
    """任务计划第 13 项：种植机 / 采种机可以在两种植物之间轮换（作者 2026-11-09：种植机在种锦草、旁边的采种机在采芽针）。
    配方表里读到的只是某一刻的那一种。直接相连（带子直连或外框紧贴）的一对种植机和采种机，种的和采的对不上、
    两种植物两台机器都有配方 → 两台都按「两种植物轮换」交替配方算（推断），不当成配方错或断料。
    只在这一对是对方唯一对不上的伙伴时才改。"""
    C = V.CELL
    idx = [i for i, d in enumerate(devices) if d["name"] in PLANTERS and rec.get(i, ("", 0))[0] and i not in alt]
    pairs = set()
    for lk in flow.get("links", []):
        if not lk.get("free") or not lk.get("a_dev") or not lk.get("device"):
            continue
        ends = []
        for dv in (lk["a_dev"], lk["device"]):
            hit = [i for i in idx if devices[i]["name"] == dv["name"] and abs(dev_canvas[i][0] - dv["c"][0]) < 3 and abs(dev_canvas[i][1] - dv["c"][1]) < 3]
            ends.append(hit[0] if hit else None)
        if None not in ends and ends[0] != ends[1]:
            pairs.add(tuple(sorted(ends)))
    for a_i, a in enumerate(idx):  # 外框紧贴的一对（5×5 并排：中心相距约 5 格、另一方向对齐）
        for b in idx[a_i + 1:]:
            dx, dy = abs(dev_canvas[a][0] - dev_canvas[b][0]), abs(dev_canvas[a][1] - dev_canvas[b][1])
            if (4.4 * C <= dx <= 5.6 * C and dy <= 1.2 * C) or (4.4 * C <= dy <= 5.6 * C and dx <= 1.2 * C):
                pairs.add((a, b))
    cand = defaultdict(list)
    for a, b in pairs:
        na, nb = devices[a]["name"], devices[b]["name"]
        if na == nb:
            continue
        pa, pb = _plant_of(na, rec[a][0]), _plant_of(nb, rec[b][0])
        if pa and pb and pa != pb and _plant_recipe(na, pb) and _plant_recipe(nb, pa):
            cand[a].append((b, pb)); cand[b].append((a, pa))
    notes = []
    for a, lst in cand.items():
        if len(lst) != 1 or a in alt:
            continue
        b, other = lst[0]
        if len(cand.get(b, [])) != 1:
            continue
        na = devices[a]["name"]
        mine = _plant_of(na, rec[a][0])
        alt[a] = (_plant_recipe(na, other), f"{na}{'种' if na == '种植机' else '采'}{mine}，相连的{devices[b]['name']}却是{other}：按两种植物轮换算（推断）")
        notes.append(f"{na} ({dev_canvas[a][0]:.0f},{dev_canvas[a][1]:.0f})：{mine} / {other} 轮换（推断）")
    return notes


def adjacent_links(G, bd, devices, dev_canvas, rec, gap=14, extra=None):
    """紧挨着的两台设备端口直接对接、中间没有带子（种植机 ↔ 采种机等，2026-11-09 景玉谷）：
    两台设备外框相贴（缝 ≤ gap px，外框线各自往里缩几 px，实测相贴的缝 11 px；隔一格约 35 px、相贴边重叠 ≥ 1 格），且一台的产物是另一台的原料 → 加一条推断连接。
    extra = {设备序号: [主配方以外在跑的配方…]}（交替配方、并行池子的其余几条），产物 / 原料都算。"""
    import cv2 as _cv
    from .pipenet import _rects, device_rect

    def outs_of(i):
        return set().union(*(set(outputs_of(r)) for r in _recipes_of(rec, i, extra)))

    def ins_of(i):
        return set().union(*(set(inputs_of(r)) for r in _recipes_of(rec, i, extra)))
    canvas = V.rd(bd / "composite.png")
    hsv = _cv.cvtColor(canvas, _cv.COLOR_BGR2HSV)
    dark_thin = ((_cv.cvtColor(canvas, _cv.COLOR_BGR2GRAY) < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    rects = _rects(canvas)
    box = {}
    for i, c in enumerate(dev_canvas):
        if inputs_of(rec[i][0]) or outputs_of(rec[i][0]):
            r = device_rect(dark_thin, c[0], c[1], rects)
            if r is not None:
                box[i] = r
    have = {(u, v) for u in G.out for v, _ in G.out[u]}
    added = []
    ids = list(box)
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            ax, ay, aw, ah = box[a]; bx, by, bw, bh = box[b]
            touch_v = (abs(ax + aw - bx) <= gap or abs(bx + bw - ax) <= gap) and min(ay + ah, by + bh) - max(ay, by) >= V.CELL
            touch_h = (abs(ay + ah - by) <= gap or abs(by + bh - ay) <= gap) and min(ax + aw, bx + bw) - max(ax, bx) >= V.CELL
            if not (touch_v or touch_h):
                continue
            gray_ = _cv.cvtColor(canvas, _cv.COLOR_BGR2GRAY) if "gray_" not in locals() else gray_
            if touch_v:
                ea = "E" if abs(ax + aw - bx) <= gap else "W"
            else:
                ea = "S" if abs(ay + ah - by) <= gap else "N"
            eb = {"N": "S", "S": "N", "E": "W", "W": "E"}[ea]
            ga, gb = glyph_sides(gray_, box[a]), glyph_sides(gray_, box[b])
            for u, v in ((a, b), (b, a)):
                items = outs_of(u) & ins_of(v)  # 交替配方的产物 / 原料也算（两种植物轮换的种植机 ↔ 采种机）
                eu, ev = (ea, eb) if u == a else (eb, ea)
                gu, gv = (ga, gb) if u == a else (gb, ga)
                if gu.get(eu) == "in" or gv.get(ev) == "out":
                    continue  # 口格折角说这边是进口 / 那边是出口：这个方向不通（武陵城左列种植机 ↔ 采种机原来两个方向都加，成了假循环）
                if items and (("d", u), ("d", v)) not in have:
                    lk = {"layer": "belt", "adjacent": True, "dir": "a->b", "items": sorted(items),
                          "basis": [f"{devices[u]['name']}和{devices[v]['name']}紧挨着、端口直接对接（按配方推断）"]}
                    G.out[("d", u)].append((("d", v), lk)); G.inn[("d", v)].append((("d", u), lk))
                    added.append(lk)
    # 两台设备隔一格、中间只有一条直带（一格长），一台的产物是另一台的原料，这条带子又没追出来 → 接上。
    # 2026-11-14 别人的武陵城：精炼炉 (252,1054) → 天有洪炉 (411,952) 的碳块带只有一格；精炼炉的端口朝向认反了（当成上进下出），
    # 按格子补短带的那段找不到它的出口。只认「横跨这道缝、宽度是一条带子」的黄带：顺着缝走的带子（很长）不算。
    # 黄带上面压着一根顺着缝走的管子时，带子中间一段是管子的深色：深色也算（没有带子的地方管子只占这道缝的 1/4，不到下面的 0.6）
    yel = ((hsv[..., 0] >= 15) & (hsv[..., 0] <= 40) & (hsv[..., 1] > 60) & (hsv[..., 2] > 180)) | dark_thin.astype(bool)
    have = {(u, v) for u in G.out for v, _ in G.out[u]}
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            ax, ay, aw, ah = box[a]; bx, by, bw, bh = box[b]
            strip = None  # (缝的那一块黄带掩码, 沿边的轴)
            for g0, g1, o0, o1, vert in ((ax + aw, bx, max(ay, by), min(ay + ah, by + bh), True), (bx + bw, ax, max(ay, by), min(ay + ah, by + bh), True),
                                         (ay + ah, by, max(ax, bx), min(ax + aw, bx + bw), False), (by + bh, ay, max(ax, bx), min(ax + aw, bx + bw), False)):
                if gap < g1 - g0 <= V.CELL + gap and o1 - o0 >= V.CELL:
                    strip = (yel[o0:o1, g0:g1], 1) if vert else (yel[g0:g1, o0:o1], 0)
            if strip is None:
                continue
            m, axis = strip
            full = m.mean(axis=axis) >= 0.6  # 沿边每个位置：这道缝是不是大半都是黄带
            runs, n = [], 0
            for t in list(full) + [False]:
                if t:
                    n += 1
                elif n:
                    runs.append(n); n = 0
            if len(runs) != 1 or not (0.35 * V.CELL <= runs[0] <= 0.9 * V.CELL):
                continue
            for u, v in ((a, b), (b, a)):
                items = outs_of(u) & ins_of(v)
                if not items or (("d", u), ("d", v)) in have or (("d", v), ("d", u)) in have:
                    continue
                if any(set(lk.get("items") or outs_of(w[1]) if w[0] == "d" else ()) & items for w, lk in G.inn[("d", v)]):
                    continue  # 这种原料已经有设备在送了
                lk = {"layer": "belt", "adjacent": True, "dir": "a->b", "items": sorted(items),
                      "basis": [f"{devices[u]['name']}和{devices[v]['name']}隔一格、中间一条直带（按配方推断）"]}
                G.out[("d", u)].append((("d", v), lk)); G.inn[("d", v)].append((("d", u), lk))
                added.append(lk)
                break
    # 仓库取货口直接贴着一台一条进料都没有的设备（中间没有带子）：取货口往它送
    # （2026-11-10 武陵城精炼炉 (2308,644)：正上方就是蓝铁矿取货口，原来「没接上」）
    for p, d in enumerate(devices):
        if d["name"] != "仓库取货口" or G.out[("d", p)]:
            continue
        px, py = dev_canvas[p]
        best = None
        for v, (bx, by, bw, bh) in box.items():
            if v == p or not inputs_of(rec[v][0]) or G.inn[("d", v)]:
                continue
            m = V.CELL / 2
            gaps = []
            if bx - m <= px <= bx + bw + m:
                gaps += [by - py, py - (by + bh)]
            if by - m <= py <= by + bh + m:
                gaps += [bx - px, px - (bx + bw)]
            g = min((x for x in gaps if x >= 0), default=None)
            # 2.25 格：底账标签不在图标正中，离设备外框会多出半格（2026-11-14 别人的武陵城 (1291,1820) → 精炼炉 (1311,1944)，隔 73 px）
            if g is not None and g <= 2.25 * V.CELL and (best is None or g < best[0]):
                best = (g, v)
        if best is not None:
            v = best[1]
            lk = {"layer": "belt", "adjacent": True, "dir": "a->b",
                  "basis": [f"仓库取货口紧贴{devices[v]['name']}、直接对接（推断）"]}
            G.out[("d", p)].append((("d", v), lk)); G.inn[("d", v)].append((("d", p), lk))
            added.append(lk)
    G.adjacent = added
    return added


PORT_NAMES = ("仓库取货口", "仓库存货口", "仓库口")
PORT_ICON_CLEAR, PORT_ICON_LEAD = 0.7, 0.15  # 仓库口图标「看得很清楚」：分数、领先都到这个数，才敢和连着的设备配方唱反调


def _port_side(port_name: str, rc: str) -> set:
    """仓库口旁边那台设备的配方 rc 里，这个口可能存/取的物品：取货口往外出料，只可能是那台设备的原料；存货口只可能是产物。
    原来原料、产物都算：图标被管子压住时，「赤铜矿 → 赤铜块」的精炼炉旁边的取货口，赤铜矿、赤铜块都是 0.2 上下，
    谁高全凭运气（2026-11-14 皇帝产线武陵城 6 个口，图标转回来以后从赤铜矿变成了赤铜块）。"""
    s = set()
    if port_name != "仓库存货口":
        s |= set(inputs_of(rc))
    if port_name != "仓库取货口":
        s |= set(outputs_of(rc))
    return s


def _port_icon_clear(cs: list[dict], local: set) -> bool:
    """仓库口图标的比对结果 cs（本据点配方里的物品，分数高的在前）能不能直接信。local = 连着的设备可能存/取的物品（没连设备是空的）。
    分数 ≥0.5 且领先 ≥0.05 算看得清；但认出来的东西连着的设备用不上、图标又不算很清楚（不到 PORT_ICON_CLEAR / PORT_ICON_LEAD）时不信，
    改按连着的设备认：图标上压着管子时转回来也只有 0.5 上下、领先 0.1。
    2026-11-14 issue #1 武陵城 7 个被管子压住的赤铜矿口比成源石粉末 0.57（领先 0.10–0.14），旁边是吃赤铜矿的精炼炉；
    皇帝产线景玉谷 (613,692) 整个图标压在管子下面，比成源石粉末 0.65，旁边的设备吃致密源石粉末（图标的蓝圈也是致密的那种）。"""
    if not cs or cs[0]["score"] < 0.5:
        return False
    lead = cs[0]["score"] - (cs[1]["score"] if len(cs) > 1 else 0.0)
    if lead < 0.05:
        return False
    if local and cs[0]["name"] not in local and (cs[0]["score"] < PORT_ICON_CLEAR or lead < PORT_ICON_LEAD):
        return False
    return True


def _icon23(img, disk):
    import cv2 as _cv
    x, y, r = int(disk[0]), int(disk[1]), disk[2]
    h = int(round(r * 0.85))  # 0.72 会切掉图标边上的形状：景玉谷砂叶口 0.46 → 0.77、砂叶粉末口 0.63 → 0.96
    patch = img[max(0, y - h):y + h + 1, max(0, x - h):x + h + 1]
    if patch.shape[0] < 2 * h or patch.shape[1] < 2 * h:
        return None
    return _cv.resize(patch, (23, 23), interpolation=_cv.INTER_AREA)


def _rots_fn(bd: Path):
    """→ 函数 (拼合图 x, y) → 这个位置所在蓝图的旋转角 [度]。拼合图上的图标（仓库口、准入口、机身产物）跟着蓝图一起转了，
    和 AKEData 比之前要转回来（LG.ake_candidates_upright）。读不到 transform.json：按没转算。"""
    from . import recipe_icons as RI
    try:
        tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
        F0, k = np.array(tf["F0"], float), float(tf["K"])
    except (OSError, ValueError, KeyError):
        return lambda cx, cy: [0]
    bp_dir = bd.parent.parent / "bp"
    bp_dir = bp_dir if bp_dir.is_dir() else None

    def at(cx, cy):
        fx, fy = F0 + k * np.array([cx, cy], float)
        return RI._rots_at(tf, bp_dir, float(fx), float(fy))
    return at


def _recipes_by_icon(bd: Path, devices: list[dict], dev_canvas: list, rec: dict, recipes: list[dict], skip=()) -> list[dict]:
    """设备机身中间的圆图标画着它的产物（2026-11-09）。同类设备在本据点有几种配方时，按图标认产物定配方：
    历史配方是按同名最近配对的（±30 px），挨着的同类设备会配反（景玉谷 (507,581)/(507,640) 两台粉碎机）。
    只在图标离设备 ≤30 px、分数 ≥0.45、领先第二名 ≥0.25 时改；改了的标推断。
    skip：并行池子（同时跑几条配方，2026-11-19）。机身图标只画一种产物，按它改成一条会把另几条丢掉。"""
    from . import flow as F
    from . import logistics as LG
    known = defaultdict(set)
    for d in recipes:
        if d.get("recipe") and "→" in d["recipe"]:
            known[d["name"]].add(d["recipe"])
    multi = {k: v for k, v in known.items() if len(v) >= 2}
    if not multi:
        return []
    img = V.rd(bd / "composite.png")
    flow = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
    _, disks = F.icon_disks(img, flow["nodes"])
    rots_at = _rots_fn(bd)
    out = []
    for i, d in enumerate(devices):
        if d["name"] not in multi or not disks or i in skip:
            continue
        cx, cy = dev_canvas[i]
        dk = min(disks, key=lambda k: np.hypot(k[0] - cx, k[1] - cy))
        if np.hypot(dk[0] - cx, dk[1] - cy) > 30:
            continue
        ic = _icon23(img, dk)
        if ic is None:
            continue
        by_out = {}
        for r in multi[d["name"]]:
            o = list(outputs_of(r))
            if o:
                by_out.setdefault(o[0], r)
        cs = [c for c in LG.ake_candidates_upright(ic, rots_at(dk[0], dk[1]), top=999) if c["name"] in by_out]
        if not cs or cs[0]["score"] < 0.45 or (len(cs) > 1 and cs[0]["score"] - cs[1]["score"] < 0.25):  # 低容/中容电池图标很像，0.58 vs 0.38 也会认错
            continue
        new = by_out[cs[0]["name"]]
        old = rec[i][0]
        if new != old:
            rec[i] = (new, True)
            out.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": old, "to": new,
                        "why": f"机身图标是{cs[0]['name']}（{cs[0]['score']:.2f}）"})
    return out


def port_items(bd: Path, devices: list[dict], dev_canvas: list, rec: dict, G=None, extra=None) -> dict:
    """仓库口圆图标里画着它存/取的物品（2026-11-09）：圆图标中间那块缩到 23×23，和 AKEData 物品图标比。
    候选只取这个据点配方里出现过的物品（致密源石粉末 / 致密晶体粉末这种长得像的才分得开）。
    仓库只存物品：液体/气体不算候选。连着的设备（G 里直接相连）配方里有的物品优先：图标上压着管子时比对分数很低，
    但「连着固气转化机（赤铜块 → 气态赤铜）」就只剩赤铜块一个候选（应龙关那排红色图标）。
    同样子的图标归一组，组里有一个认得准就整组用它。→ {设备序号: {item, score, sure, how}}
    extra：主配方以外在跑的配方（见 _recipes_of），候选、连着的设备、下游吃什么都按全部配方算。"""
    import cv2 as _cv
    from . import flow as F
    from . import logistics as LG
    img = V.rd(bd / "composite.png")
    flow = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
    _, disks = F.icon_disks(img, flow["nodes"])
    rots_at = _rots_fn(bd)
    cand = set()
    for j in rec:
        for rc in _recipes_of(rec, j, extra):
            cand |= set(inputs_of(rc)) | set(outputs_of(rc))
    found = {}
    for i, d in enumerate(devices):
        if d["name"] not in PORT_NAMES or not disks:
            continue
        cx, cy = dev_canvas[i]
        dk = min(disks, key=lambda k: np.hypot(k[0] - cx, k[1] - cy))
        if np.hypot(dk[0] - cx, dk[1] - cy) > 40:
            continue
        x, y = int(dk[0]), int(dk[1])
        ic = _icon23(img, dk)
        if ic is None:
            continue
        # 图标跟着蓝图转了，转回来再比（2026-11-14 issue #1：转 90° 放的蓝图，18 个源矿口认成致密源石粉末，下游 30 台设备算成没料）
        allc = [c for c in LG.ake_candidates_upright(ic, rots_at(dk[0], dk[1]), top=999) if c["state"] == "固"]
        local = set()
        if G is not None:
            for v, _ in G.out[("d", i)] + G.inn[("d", i)]:
                if v[0] == "d":
                    for rc in _recipes_of(rec, v[1], extra):
                        local |= _port_side(d["name"], rc)
        cs_local = [c for c in allc if c["name"] in local]
        cs = [c for c in allc if c["name"] in cand] if cand else []
        if _port_icon_clear(cs, {c["name"] for c in cs_local}):  # 图标看得清：只信图标（连着的设备配方可能配错台，会把仓库口也带错）
            found[i] = {"item": cs[0]["name"], "score": cs[0]["score"], "how": "图标比对", "sure": True, "icon": ic, "disk": [x, y]}
        elif cs_local:  # 图标看不清（上面压着管子）：在连着的设备配方里挑
            ll = cs_local[0]["score"] - (cs_local[1]["score"] if len(cs_local) > 1 else -1.0)
            found[i] = {"item": cs_local[0]["name"], "score": cs_local[0]["score"], "how": "连着的设备配方",
                        "sure": cs_local[0]["score"] >= 0.1 and ll >= 0.05, "icon": ic, "disk": [x, y]}
        elif cs:
            found[i] = {"item": cs[0]["name"], "score": cs[0]["score"], "how": "图标比对", "sure": False, "icon": ic, "disk": [x, y]}
        if i in found and found[i]["how"] == "图标比对" and G is not None and d["name"] == "仓库取货口":
            # 图标认出的东西下游没人吃：顺着出料（穿过物流件）找配方可信的设备，它们要的东西在图标比对里也有点分数 → 按下游配方认
            # （2026-11-10 武陵城取货口 (649,1237)：图标是蓝铁瓶，图标库的蓝铁瓶图差别大，比对成蓝铁粉末 0.66 / 蓝铁瓶 0.22；
            #  下游两台灌装机配方从游戏面板读来，只吃蓝铁瓶）
            eat, seen, front = set(), {("d", i)}, [("d", i)]
            for _ in range(4):
                nxt = []
                for u in front:
                    for v, _lk in G.out[u]:
                        if v in seen:
                            continue
                        seen.add(v)
                        if v[0] == "n":
                            nxt.append(v)
                        elif not rec.get(v[1], ("", True))[1]:
                            for rc in _recipes_of(rec, v[1], extra):
                                eat |= set(inputs_of(rc))
                front = nxt
            nobody = not any(found[i]["item"] in inputs_of(rc) for j in rec if not rec[j][1] for rc in _recipes_of(rec, j, extra))  # 全据点配方可信的设备都不吃它
            if eat and found[i]["item"] not in eat and nobody:
                alts = [c for c in allc if c["name"] in eat and c["score"] >= 0.15]
                if alts:
                    found[i].update(item=alts[0]["name"], score=alts[0]["score"], sure=True, how=f"下游配方（图标比对更像{found[i]['item']}）")
    # 同样子的图标一组：组里认得准的投票定名
    ids = list(found)
    grp = LG.group_icons([found[i]["icon"] for i in ids]) if ids else []
    by = defaultdict(list)
    for i, g in zip(ids, grp):
        by[g].append(i)
    for g, mem in by.items():
        sure = [found[i]["item"] for i in mem if found[i]["sure"] and found[i]["how"] == "图标比对"]
        if sure:
            best = max(set(sure), key=sure.count)
            for i in mem:
                if not (found[i]["sure"] and found[i]["how"] == "图标比对") and not found[i]["how"].startswith("下游配方")                         and found[i]["item"] != best:
                    found[i]["item"], found[i]["sure"], found[i]["by_group"], found[i]["how"] = best, True, True, "同样子的图标"
    for v in found.values():
        v.pop("icon", None)
    return found


# ------------------------------------------------------------------ 计算
def _cap(lk):
    return PIPE_CAP if lk["layer"] == "pipe" else BELT_CAP


def analyze(bd: Path, ledger: dict, recipes: list[dict], gate_limits: dict | None = None, inputs: list[dict] | None = None,
            rate_limited_intake: bool = RATE_LIMITED_INTAKE, main_blocked: bool = MAIN_BLOCKED, pass_split: dict | None = None) -> dict:
    """rate_limited_intake=False：每种原料各算「能送到多少」（run_base 第二遍）；main_blocked=True：出料口的主路全堵死的情景（第三遍）。
    pass_split：池子出口转送原料的比例 {"设备键|物品": 转给下游几成}（主遍结果顶层的 pass_split）。给了就直接用、不再定比例
    （run_base 第二、三遍用主遍定下的）；不给、又有作者说出口送原料的池子时，外层自己定（0.10.13 功能三）。"""
    from .pipenet import load_inputs, recipe_table
    flow = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    devices = ledger["devices"]
    dev_canvas = [V.far_to_canvas(tf, d["x"], d["y"]) for d in devices]
    table = recipe_table(bd, ledger, recipes)
    rec = {i: table.get((d["name"], d["x"], d["y"]), ("", False)) for i, d in enumerate(devices)}
    alt = {}  # 交替配方（液气转化机：两路液体轮流做，2026-11-09 补；配方表 alt 字段）
    # 审计第 3 项（2026-11-19）：池子同时跑的几条配方。par[i] = [{recipe: 净配方, members: [原始配方…], src}…]，
    # 只放并出来 ≥2 组的池子，rec[i][0] 是第一组；和 alt 不重叠（alt 只留液气转化机、种植机 / 采种机轮换这类真的轮流做的）。
    # par_note：并行的说明；pool_src：池子配方从哪来；unknown：没配方的生产设备；pool_unused：作者说没在用的池子
    par, par_note, pool_src = {}, {}, {}
    unknown, pool_unused = set(), set()

    def recipes_of(i):
        """第 i 台在跑的全部配方（并行池子的每一组、交替配方的两条、其它设备只有主配方）。"""
        if i in par:
            return [c["recipe"] for c in par[i]]
        if i in alt:
            return [rec[i][0], alt[i][0]]
        return [rec[i][0]]

    def extra():
        """给模块级的连线 / 仓库口函数：{设备: [主配方以外在跑的配方…]}。alt、par 一路在补，每次用时现算。"""
        return {i: recipes_of(i)[1:] for i in set(alt) | set(par)}
    for i, d in enumerate(devices):  # 反应池只记「池产物：液化息壤(50)/…」：换成配方表里这种池子做这个产物的配方
        rc = rec[i][0]
        if rc.startswith("池产物"):
            rec[i] = (_pool_recipe(rc, d["name"]) or rc, True)
            # 池产物同时记了几种有量、互不相干的产物（武陵城反应池 (1357,1332)：芽针溶液 50 / 锦草溶液 50）：几条配方同时跑。
            # 2026-11-19 审计第 3 项：原来按两种配方轮流做（alt）算，可池子能同时跑几条配方（官方），轮流会把它少算一半。
            # 首尾相接的几条（壤晶废液 → 壤晶）并成一组净配方，只有一组的照旧只写 rec（应龙关扩容反应池 (949,731) 结果不变）
            gs = PM.groups(d["name"], PM.panel_recipes(d["name"], rc)) if d["name"] in POOLS else []
            if len(gs) >= 2:
                par[i] = [dict(g, src="面板") for g in gs]
                par_note[i] = "池产物里记了几种互不相干的产物：几条配方同时跑（并行）"
                rec[i] = (gs[0]["recipe"], True)
    for q in recipes:
        if q.get("alt"):
            for i, d in enumerate(devices):
                if i in par:  # 池产物已经读出几条并行的配方：配方表的交替配方不再盖上去（一台池子不会既并行又交替）
                    continue
                if d["name"] == q["name"] and abs(d["x"] - q["x"]) <= 30 and abs(d["y"] - q["y"]) <= 30:
                    alt[i] = (q["alt"], q.get("note", ""))
    icon_fixes = _recipes_by_icon(bd, devices, dev_canvas, rec, recipes, skip=par)
    fx = load_fixes(bd)
    # 作者在「池子配方」待确认里勾的几条原始配方（fixes.json 的 pool_recipes，2026-11-19 审计第 3 项）：池子机身没有产物图标，
    # 别人的武陵城 7 台池子只能靠作者说。[] = 这台没在用；格数不够等放不下的忽略（写一条说明），照旧按没指定算。
    # 比老的 recipes[键]（一台只能一条）优先：两段都有时以 pool_recipes 为准
    pool_fix, pool_notes = {}, []
    for i, d in enumerate(devices):
        want = (fx.get("pool_recipes") or {}).get(f"{d['name']}@{d['x']},{d['y']}")
        if d["name"] not in POOLS or not isinstance(want, list):
            continue
        ck = PM.check(d["name"], want) if want else {"ok": True}
        if ck["ok"]:
            pool_fix[i] = list(want)
        else:
            pool_notes.append({"kind": "池子配方放不下", "msg": f"{d['name']} ({d['x']},{d['y']}) 作者指定的池子配方没用上："
                                                           + "；".join(ck["why"])})
    by_author = set()  # 配方是作者指定的设备（老的 recipes 和 pool_recipes 都算）
    for i, d in enumerate(devices):  # 作者指定的配方：盖过图标认的、按邻居推的，算可信（别的设备可以拿它当依据）
        want = (fx.get("recipes") or {}).get(f"{d['name']}@{d['x']},{d['y']}")
        if i in pool_fix:
            if want:  # 手改的 fixes.json、旧版存下的：不静默丢掉，说一句以池子配方为准（2026-11-19；界面已经把池子的单条入口改成存 pool_recipes）
                pool_notes.append({"kind": "老的配方指定没用上", "msg": f"{d['name']} ({d['x']},{d['y']}) 老的单条配方指定「{want}」没用上："
                                                                   "这台另有作者勾的池子配方，以池子配方为准"})
            continue
        if want:
            by_author.add(i)
        if want and (want != rec[i][0] or i in par):
            # 并行池子被老的单条指定清成一条：选的正好是现在第一条时原来什么都不记，另几条配方的下游悄悄变成没料（2026-11-19，
            # 合成例子：面板读出芽针 + 锦草两条的反应池，recipes[键] = 芽针那条 → 锦草灌装机没料、recipe_fixes 空的）
            icon_fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rec[i][0], "to": want,
                               "how": "作者指定（只留这一条，原来并行的另几条已去掉）" if i in par else "作者指定"})
        if want:
            rec[i] = (want, False)
            # 作者只给了一条：这台就只跑这一条，池产物读出的并行配方、配方表的交替配方都不留（2026-11-19，原来交替配方会残留）
            alt.pop(i, None)
            par.pop(i, None)
            par_note.pop(i, None)
    for i, want in pool_fix.items():
        d = devices[i]
        by_author.add(i)
        alt.pop(i, None)
        par.pop(i, None)
        par_note.pop(i, None)
        if not want:  # 没在用：不出产物、不吃原料，单出一行「没在用（作者说）」（不借「溢流位」：那个前缀会让 run_base 多跑一遍堵死情景）
            pool_unused.add(i)
            rec[i] = ("", False)
            continue
        # 首尾相接的几条先并成一条净配方再算：液化息壤 + 壤晶废液 + 壤晶三条原样当三条并行，中间产物液化息壤会被当成要从外面送的原料，
        # 后两条就成了没料（别人的武陵城扩容反应池 (1570,1196)）。格数在上面按原始配方查过（中间产物也占一格）
        gs = PM.groups(d["name"], want)
        to = " + 并行 ".join(g["recipe"] for g in gs)
        if to != rec[i][0]:
            icon_fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rec[i][0], "to": to, "how": "作者指定（池子配方）"})
        rec[i] = (gs[0]["recipe"], False)
        if len(gs) >= 2:
            par[i] = [dict(g, src="作者") for g in gs]
            par_note[i] = "作者指定了几条互不相干的配方：几条同时跑（并行）"
    # 按机身图标推的配方（recipe_icons.derive，只有档案里没有配方表的据点才有）认出的产物和把握，给邻居推断把关（2026-11-21）
    at = {(d["name"], d["x"], d["y"]): i for i, d in enumerate(devices)}
    body_icon = {}
    for q in recipes:
        k = (q.get("name"), q.get("x"), q.get("y"))
        if q.get("product") and "score" in q and "margin" in q and k in at:
            body_icon[at[k]] = (q["product"], q["score"], q["margin"])
    icon_fixes += _infer_by_neighbors(flow, devices, dev_canvas, rec, inputs if inputs is not None else load_inputs(bd), alt=alt, par=par,
                                      icon=body_icon)
    for i in par:
        par_note.setdefault(i, "相邻设备送进来几种原料：几条配方同时跑（并行，推断）")
    plant_notes = _plant_alternation(flow, devices, dev_canvas, rec, alt)
    G = Graph(flow, devices, dev_canvas, rec)
    adjacent_links(G, bd, devices, dev_canvas, rec, extra=extra())
    tpairs = tunnel_pairs(bd, devices, dev_canvas)
    tun_out = tunnel_outlets(bd, devices, dev_canvas, tpairs)  # 没配对的暗管出口：外接 / 漏配 / 缺文件 / 蓝图外（0.10.13 功能二）
    buf_ids = {i for i, d in enumerate(devices) if d["name"] in BUFFERS}
    for a, b in tpairs:  # 暗管入口 → 出口：入口进什么、出口就出什么
        lk = {"layer": "pipe", "tunnel": True, "dir": "a->b", "basis": ["暗管配对（读蓝图时点开确认）"]}
        G.out[("d", a)].append((("d", b), lk)); G.inn[("d", b)].append((("d", a), lk))
        buf_ids |= {a, b}
    import cv2
    gray = cv2.cvtColor(V.rd(bd / "composite.png"), cv2.COLOR_BGR2GRAY)
    _outlet_touch(G, flow, devices, dev_canvas, rec, gray, extra=extra())
    _feed_touch(G, flow, devices, dev_canvas, rec)
    _glyph_dirs(G, flow, devices, dev_canvas, bd)
    _buffer_dirs(G, flow, devices, dev_canvas, rec, extra())
    _orphan_feeds(G, flow, devices, dev_canvas, rec, extra=extra())
    reread, pending = _starved_by_reading(G, flow, devices, dev_canvas, rec, bd, feeds=fx.get("feeds"), extra=extra())
    for u in list(G.out):  # 推断出来的连线打标，推料时经过它的路径带上〔推断连线〕
        for v, lk in G.out[u]:
            if _is_inferred(lk):
                lk["inferred"] = True
    pitem = port_items(bd, devices, dev_canvas, rec, G, extra=extra())
    # 作者用 pool_recipes 指定的池子（一条也好、几条也好）不再按取货口物品改：作者说的算
    fixes = icon_fixes + _fix_recipes_by_ports(G, devices, rec, recipes, pitem, skip=set(par) | set(pool_fix))
    # 改配方的步骤到这里都做完了：并行池子的主配方就是第一组，一台池子不会既并行又交替
    assert not set(par) & set(alt), [devices[i]["name"] for i in set(par) & set(alt)]
    assert all(rec[i][0] == par[i][0]["recipe"] for i in par), [devices[i]["name"] for i in par if rec[i][0] != par[i][0]["recipe"]]
    # 审计第 3 项（2026-11-19）：配方库里有配方、但这台没读到配方（或配方文字认不出）的生产设备。原来出记录时 `if not need: continue`
    # 整台消失：别人的武陵城 7 台池子（扩容反应池 (1798,454) 等）不在结果里，下游 6 台缺料也看不出是它们的缘故。
    # 配方库里 0 条配方的机器（废水处理机、散布机、热能池、水泵…）不算，照旧不出行。作者说没在用的池子另出一行，不算配方未知
    unknown |= {i for i, d in enumerate(devices) if i not in buf_ids and d["name"] not in PORT_NAMES and i not in pool_unused
                and "→" not in rec[i][0] and _machine_recipes(d["name"])}
    # 作者说池子出口送原料（0.10.13 功能三）：pt[池子] = {转送的原料: 层}；说明带 key，放在 inner() 外面
    pt, pt_notes = _outlet_answers(devices, fx, rec, pool_fix, pool_unused, unknown, recipes_of)

    def port_role(i):
        """仓库口是往外出（取货）还是往里收（存货）：物品是连着的设备的原料 → 取；是产物 → 存；都不是看底账名字。"""
        it = pitem.get(i, {})
        item = it.get("item") if it.get("sure") else None
        if item:
            nb = [v for v, _ in G.out[("d", i)] + G.inn[("d", i)] if v[0] == "d"]
            if any(item in inputs_of(r) for v in nb for r in recipes_of(v[1])):  # 连着的是池子：哪一组配方吃 / 产它都算
                return "取", item
            if any(item in outputs_of(r) for v in nb for r in recipes_of(v[1])):
                return "存", item
        nm = devices[i]["name"]
        return ("取" if nm in SOURCE_PORTS else "存" if nm == "仓库存货口" else None), item
    inputs = inputs if inputs is not None else load_inputs(bd)
    # 档案里游戏读到的准入口限速：按行上的远景坐标 far 换到现在的拼合图上再找口（拼合图坐标 p 在重新拼合后会变；
    # 0.10.13 以前按 p 找）。没有 far 的老文件照旧按 p
    gl = {tuple(V.far_to_canvas(tf, *r["far"]) if r.get("far") else r["p"]): r for r in (gate_limits or {}).get("rows", [])}
    nodes = flow["nodes"]
    struct = flow.get("structures", [])
    backup_merge = {s["node"] for s in struct if s["kind"] == "单进单出汇流器"}
    # 1 进 1 出的汇流器（降优先级用）按补完连接后的图再认一遍：贴着出口、flow 里没连上的进料边（应龙关 H2）flow 的 structures 漏掉
    for n in nodes:
        if n["type"] in MERGE_T and len(G.inn[("n", n["id"])]) == 1 and len(G.out[("n", n["id"])]) == 1:
            backup_merge.add(n["id"])
    loop_nodes = set()
    for s in struct:
        if s["kind"] == "分流回环":
            loop_nodes |= set(s["nodes"])

    inputs = inputs if inputs is not None else load_inputs(bd)
    gate_guess = _gate_items(bd, nodes, rec, inputs)
    ag, ag_stale = author_gates(bd, nodes, fx)
    # 作者填的准入口设置有对不上的：说一句（放在 inner() 外面，inner() 每轮开头清空 notes）。说明里不写内部的键，写是哪种口、什么料、为什么
    gate_notes = [{"kind": "准入口设置对不上", "keys": [s["key"] for s in ag_stale],
                   "msg": f"作者填的 {len(ag_stale)} 个准入口设置在现在的拼合图上找不到对应的口，没参加计算："
                          + "；".join(f"{s.get('kind') or ''}准入口" + (f"（{s['item']}）" if s.get("item") else "") + f"：{s['why']}"
                                     for s in ag_stale[:3]) + (" 等" if len(ag_stale) > 3 else "")}] if ag_stale else []
    # 每个准入口：物品、限速、是不是输入端。限速、限制通过数量的优先级：作者填的 > 档案里游戏读的 > 没说（按不限速）；src 记从哪来
    gate = {}
    for n in nodes:
        if n["type"] != "准入口":
            continue
        g = {"item": n.get("item") or gate_guess.get(n["id"], {}).get("item", ""), "limit": None, "count": None, "input": None,
             "item_how": "作者标" if n.get("item") else gate_guess.get(n["id"], {}).get("how"), "src": None}
        m = next((m for m in inputs if abs(m["p"][0] - n["p"][0]) <= 15 and abs(m["p"][1] - n["p"][1]) <= 15), None)
        if m:
            g["input"] = m
            g["item"] = m.get("item") or g["item"]
        # 读失败的行（read_area 写的带 error：画面里没找到图标、点开的不是准入口…）不当读数：不按它算、不标「游戏里读的」，只记下没读出来
        near = [r for p, r in gl.items() if abs(p[0] - n["p"][0]) <= 15 and abs(p[1] - n["p"][1]) <= 15]
        r = next((r for r in near if not r.get("error")), None)
        if r is None and near:
            g["read_error"] = near[0]["error"]
        if r:
            g["read"] = r
            g["src"] = "游戏"
            if r.get("limit_on"):
                g["limit"] = r.get("limit")
            if r.get("count_on"):
                g["count"] = {"limit": r.get("count_limit"), "passed": r.get("passed")}
        a = ag.get(n["id"])
        if a is not None:  # 作者填的盖过游戏里读的（两项都是 null = 作者说都没开）
            rc = g.get("count")
            g["src"] = "作者"
            g["author"] = a
            g["limit"] = a.get("limit")
            # 作者填的限制通过数量没有累计数；和游戏里读到的同一个数时沿用读到的累计数（只给准入口段写「累计 N」用：
            # 0.10.13 A2 起开着、N 知道就按不放行算，累计到没到上限不影响结果）
            g["count"] = ({"limit": a["count"], "passed": rc.get("passed") if rc and rc.get("limit") == a["count"] else None}
                          if a.get("count") is not None else None)
        gate[n["id"]] = g
    tun_mark, tun_here, tun_notes = _bind_tunnel_marks(inputs, nodes, devices, dev_canvas, tun_out, tpairs)

    # 输入端准入口直接贴着设备（应龙关 5、6 号息壤气贴着多口暗管入口，中间没有管子）：物流图里这一头没连上 → 接到最近的设备
    for nid, g in gate.items():
        if g.get("input") and not G.out[("n", nid)]:
            c = nodes[nid]["c"]
            cand = [i for i in range(len(devices)) if devices[i]["name"] not in ("息壤供电桩",)]
            if cand:
                i = min(cand, key=lambda i: np.hypot(dev_canvas[i][0] - c[0], dev_canvas[i][1] - c[1]))
                if np.hypot(dev_canvas[i][0] - c[0], dev_canvas[i][1] - c[1]) <= 2.5 * V.CELL:
                    lk = {"layer": "pipe" if g["input"].get("kind") == "管道" else "belt", "dir": "a->b", "touch": True, "inferred": True,
                          "basis": [f"输入端紧贴{devices[i]['name']}"]}
                    G.out[("n", nid)].append((("d", i), lk)); G.inn[("d", i)].append((("n", nid), lk))

    # 并行池子每条配方满速的原料 / 产物（每分钟）、几条都要的原料（共用的清水）
    cneed = {i: [inputs_of(c["recipe"]) for c in cs] for i, cs in par.items()}
    cout = {i: [outputs_of(c["recipe"]) for c in cs] for i, cs in par.items()}
    shared_of = {i: {k for k in set().union(*nds) if sum(k in nd for nd in nds) >= 2} for i, nds in cneed.items()}
    cout_all = {}  # 并行池子 → 产物 → 几条合计满速产量（按配方顺序，依据里的先后不随进程变）
    for i, ods in cout.items():
        cout_all[i] = {}
        for od in ods:
            for k, v in od.items():
                cout_all[i][k] = cout_all[i].get(k, 0.0) + v
    par_products = {k for agg in cout_all.values() for k in agg}

    def dev_need(i):
        rc, _ = rec[i]
        need = inputs_of(rc)
        if i in par:  # 几条同时跑：用量相加（武陵城反应池 (1357,1332) 两条各要清水 30 → 60）
            need = {}
            for nd in cneed[i]:
                for k, v in nd.items():
                    need[k] = need.get(k, 0.0) + v
        nm = devices[i]["name"]
        return need, FUEL_MIN.get(nm, 0.0)

    resid = [False]  # True：推料阶段，设备按剩余缺口算
    # 这一趟内层按不按「别的原料够跑多快」限吃量（rl），是不是在给并行池子定出料上限（capping）。只有并行池子的外层会临时改它们
    rl = [rate_limited_intake]
    capping = [False]
    last_sup = {}     # 上一轮每台设备到手的料（按「另一种原料够跑多快」限这种原料的吃量）
    prev_sup = {}     # 再上一轮：两种原料互相限吃量会来回震荡（塑形机赤铜块 50/惰气 30 ↔ 60/25），取两轮里多的那轮，收敛到上面那个自洽解

    def once(g):
        """准入口开了限制通过数量、数量 N 知道：一次性的，放满 N 个就不再放行（用户 2026-11-09 实测），设计分析长期按不放行算
        （0.10.13 A2：线内的口、输入端，作者填的、游戏里读的都一样，累计到没到上限不管）。N 不知道（游戏里读到开着、数量没读出来）的不挡。"""
        c = g.get("count")
        return bool(c) and c.get("limit") is not None

    def gate_pass(nid, item, q, moving=False):
        """准入口 nid 放 q/分的 item 过去，能过多少 → (过口的量, 为什么少了)。为什么：None = 照原样过；GATE_WRONG = 物品不对、一点不过；
        其它是被挡下的原因（「准入口限速 18」「准入口限制通过数量 20：…」，作者填的加「（作者填）」）。item 是空的或没看到的物品时不按物品挡。
        absorb / drain / eat / push、池子出口的「下游吃得下多少」（_take_at）的准入口都走这里，准入口怎么放行只改这一处。
        开了限制通过数量（once）→ 0：推料时挡下的量照样记进 gate_held（准入口表的「被挡下」）；
        moving 时另外把 q > 0、看得到的物品记进 gate_shut，不管量：上游是生产设备、池子时背压先把它压成 0，
        推料一点都没有、gate_held 是空的，下游那句依据、准入口表靠它知道挡的是什么料（审查 2026-11-21）。
        moving 只在真有料在动时给：推料（push，q = 真推到口上的量，单路推料不看下游吃不吃得下，和 gate_held 一样）；分料、几口合计、
        池子出口转送按各路下游吃得下的量问（split、pools、_pt_ask 置 ask）。背压试算（drain）、去向过滤、eat 只是问一问，不记（复审 2026-11-22）。
        drain、eat 经 gate_ask 调它（probe 2 把堵死型的口当放行）。"""
        g = gate.get(nid, {})
        if g.get("item") and item and item not in UNKNOWN and g["item"] != item:
            return 0.0, GATE_WRONG
        if once(g):
            if moving and q > EPS and item and item not in UNKNOWN:
                gate_shut[nid].add(item)
            return 0.0, f"准入口限制通过数量 {g['count']['limit']:g}" + ("（作者填）" if g.get("src") == "作者" else "") + "：一次性，放满就不再放行"
        if g.get("limit") is not None and q > g["limit"]:
            return g["limit"], f"准入口限速 {g['limit']:g}" + ("（作者填）" if g.get("src") == "作者" else "")
        return q, None

    def gate_ask(nid, item, q):
        """算背压（drain）、并行池子的下游需求上限（eat）时过准入口 → 能过多少：下游出不去，上游就减速、停下。平时就是 gate_pass。
        堵死型的口（开了限制通过数量、限速 0、物品不对：一点都不放行）挡掉 > 0 的量时：主分析里记 dead_cut（决定跑不跑 probe 2）；
        probe 2（见内层后面）当它放行，只留正的限速。料怎么分（push、absorb、池子出口转送的 _take_at）不走这里，probe 里照样挡
        （复审 2026-11-22 第四轮）"""
        p = gate_pass(nid, item, q)[0]
        g = gate.get(nid, {})
        lim = g.get("limit")
        wrong = bool(g.get("item") and item and item not in UNKNOWN and g["item"] != item)
        if q > EPS and (wrong or once(g) or (lim is not None and lim <= EPS)):
            if probe_mode[0] == 2:
                return min(q, lim) if lim is not None and lim > EPS else q
            if not probe_mode[0]:
                dead_cut[0] = True
        return p

    def pt_outs(i, item):
        """作者说出口送原料 item 的池子 i：item 能从哪几条出边出去（层对：固体走带、液体走管；连线没限定别的物品；
        两根线通到同一台设备按一个去向算，和 split 一样）。0.10.13 功能三"""
        fl, dv, res = PM.is_fluid(item), set(), []
        for v, lk in G.out[("d", i)]:
            if lk.get("items") and item not in lk["items"] or lk.get("layer") and (lk["layer"] == "pipe") != fl:
                continue
            if v[0] == "d":
                if v in dv:
                    continue
                dv.add(v)
            res.append((v, lk))
        return res

    def absorb(u, item, seen=()):
        """能吸收多少（见 _absorb）。作者说出口送原料的池子：吃得下的 = 自己用的 + 出口下游吃得下的，从自己的出口绕回自己算 0
        （不算「不知道」）。别的设备、别的物品直接是 _absorb（0.10.13 功能三）。
        出口下游那段只是问一问（ask 关着）：转不转、转多少是外层定的比例，出口后面的口记不记 shut 只看 _pt_ask（按收敛后的状态问）。
        原来分料问到这里时照记，记不记还看出口下游另一路来料先推还是后推（复审 2026-11-22 第四轮）"""
        if pt and u[0] == "d" and item in pt.get(u[1], ()):
            if u in seen:
                return 0.0
            a, ask[0] = ask[0], False
            down = sum(min(_cap(lk), absorb(v, item, seen + (u,))) for v, lk in pt_outs(u[1], item))
            ask[0] = a
            return _absorb(u, item, seen) + down
        return _absorb(u, item, seen)

    # 能吸收多少（某物品，从节点 u 往下）：设备 = 用量；分流 = 各路之和；汇流/准入口 = 下游；带子/管道封顶
    def _absorb(u, item, seen=()):
        if u in seen:
            return float("inf")  # 绕回上游：回环那一路照样分走一份
        if u[0] == "d" and u[1] not in buf_ids:
            need, fuel = dev_need(u[1])
            got_i = sup.get(u[1], {}) if resid[0] else {}  # 推料时按「还差多少」分：多个水口都往同一台送时，灌满的不再多拿（武陵城精炼炉 40/30 vs 20/30）
            if u[1] in par and item and item not in UNKNOWN:
                return max(0.0, sum(nd[item] * _ch_intake(u[1], c, item) for c, nd in enumerate(cneed[u[1]]) if item in nd)
                           - got_i.get(item, 0.0))
            if u[1] in alt and item in inputs_of(alt[u[1]][0]):
                return max(0.0, inputs_of(alt[u[1]][0])[item] - got_i.get(item, 0.0))
            if fuel and item and _is_fuel(item, rec[u[1]][0]):
                return max(0.0, fuel - (_fuel_got(got_i, rec[u[1]][0]) if got_i else 0.0))  # 转化机/散布机烧的气：每台最低 6/分（4.7）
            if item and item not in UNKNOWN:
                k_rate = 1.0
                if resid[0] and rl[0] and item in need:  # 另一种原料只够跑 X%，这种也只吃 X%（灌装机缺赤铜耐压罐时不吃惰气，气改流向塑形机；用户 2026-11-09）
                    # 定池子出料上限 / 出口转送比例的那几趟：并行池子的产物、从池子出口转来的原料不算（不然自己压自己，见外层循环的说明）
                    others = [min(1.0, max(last_sup.get(u[1], {}).get(k, 0.0), prev_sup.get(u[1], {}).get(k, 0.0)) / v)
                              for k, v in need.items() if k != item and v > 0 and not (capping[0] and k in par_products)
                              and not (pt_capping[0] and k in pt_rx.get(u[1], ()))]
                    if others and last_sup:
                        k_rate = min(others)
                if resid[0]:
                    k_rate = min(k_rate, out_cap.get(u[1], 1.0))  # 产物出不去的设备也吃不了那么多
                return max(0.0, need.get(item, 0.0) * k_rate - got_i.get(item, 0.0))
            fit = [k for k in need if _unknown_fits(item, k)] if item in UNKNOWN else list(need)
            return max(0.0, sum(need[k] for k in fit) - sum(got_i.get(k, 0.0) for k in fit))
        kd = G.kind(u)
        tot = 0.0
        for v, lk in G.out[u]:
            tot += min(_cap(lk), absorb(v, item, seen + (u,)))
        if kd == "准入口":
            tot = gate_pass(u[1], item, tot, ask[0])[0]
        return tot

    # 来量：从来源往下推（每轮按拓扑顺序；设备产物下一轮才有）
    sup = defaultdict(lambda: defaultdict(float))     # 设备 → 物品 → 每分钟到手
    gate_flow = defaultdict(lambda: defaultdict(float))  # 准入口 → 物品 → 推料时过口的量（每轮和 sup 一起清空，留下的是最后一轮）
    gate_held = defaultdict(lambda: defaultdict(float))  # 准入口 → 物品 → 被限速 / 限制通过数量挡下的量
    # 开了限制通过数量的准入口 → 真有料在动时送到它上面的物品（gate_pass 记，只记物品不记量；每轮内层和 gate_flow 一起清空，留下的是最后一轮，
    # 第 0 轮全部满速起步推的不算数。复审 2026-11-21：原来整次分析不清空，上游自己没料的口也记上了）
    gate_shut = defaultdict(set)
    ask = [False]   # True：分料 / 几口合计 / 池子出口转送正在按下游吃得下的量问，absorb、_take_at 经过的口记 gate_shut（见 gate_pass）
    # 正在跑哪一遍（见内层后面的 probe）：0 主分析；1 probe 1（背压是 0 的不算）；2 probe 2（另外算背压、下游需求上限时堵死型的口当放行）
    probe_mode = [0]
    dead_cut = [False]  # 主分析里 drain / eat 有没有被堵死型的口挡掉过 > 0 的量（gate_ask 记）：没有的话 probe 2 和 probe 1 一样
    # 到手里来自并行池子的、按缺口分来的未知来料（定池子出料上限时，下游「从别处已经拿到的」要扣掉这两种以外的）
    not_exo = defaultdict(lambda: defaultdict(float))
    pushing_par = [False]  # 正在推的是并行池子的产物
    via = defaultdict(lambda: defaultdict(list))      # 设备 → 物品 → 依据
    # 从「全部满速」开始往下迭代（取最大的自洽解）：种植机 ↔ 采种机这种互相供料的环从 0 开始永远起不来
    dev_rate = {i: 1.0 for i in range(len(devices))}
    out_cap = {}  # 设备 → 产物能出去的比例（背压，上一轮算的）
    alt_split = {i: (0.5, 0.5) for i in alt}  # 交替配方两种各占的开工时间（迭代里按来料更新）
    notes = []
    # 并行池子（审计第 3 项，2026-11-19）。每条配方各有开工率；出料上限有两种：
    # struct_cap = 结构背压（和别的设备同一套，按下游满速用量，每轮内层算）；dem_cap = 下游「实际」吃得下多少（看下游别的原料够不够），
    # 外层算、一趟内层里不变。有效上限 ecap 取两者小的，用在推料吸收量和每条的开工率上；给别的设备算背压的 drain 只用 struct_cap
    # （别的设备的背压口径不动，那是另一个审计项）。
    # 为什么要 dem_cap：皇帝产线武陵城反应池 (1357,1332) 两条溶液下游的灌装机各只有 15 蓝铁瓶，只按结构算两条都满速、白占 60 清水，
    # 和它共用分流器的种植机 (1573,1391) 就被算成缺水（作者 2026-11-10 说那组设备游戏里都在转，巡检看到的是下游堵）
    chan_rate = {i: [1.0] * len(cs) for i, cs in par.items()}
    struct_cap = {}  # 池子 → [每条的结构背压上限]（有一条 < 1 才记）
    dem_cap = {}     # 池子 → [每条的下游需求上限]（有一条 < 1 才记）
    dem_why = {}     # 池子 → 产物 → (下游吃得下, 满速产, [点名的下游设备说明], [点名的设备键])
    last_scale = {}  # 池子 → 共用原料 → 上一轮「到手 / 按各条开工率要的」（不够时按各条用量比例分）
    # 池子出口转送原料（0.10.13 功能三）。phi[(池子, 原料)] = 到手的原料从出口转给下游几成：外层按「下游实际吃得下多少」定（_pt_step），
    # 给了 pass_split 就直接用。不在内层里直接「下游先拿」：作者那组每一轮是 p ← 75 − 2.5p（斜率 −2.5），在 0 和 30 之间来回跳、发散；
    # 内层里比例固定不会震荡，外层是有界的一维求根
    key_of = {f"{d['name']}@{d['x']},{d['y']}": i for i, d in enumerate(devices)}
    phi = {(i, x): 0.5 for i in pt for x in pt[i]}
    for k, v in (pass_split or {}).items():
        dk, _, x = k.rpartition("|")
        if (key_of.get(dk), x) in phi:
            phi[(key_of[dk], x)] = float(v)
    phi0 = dict(phi)  # 起步的比例（probe 那遍从这里重新定）
    pt_free = bool(phi) and not pass_split  # 这次调用自己定比例
    fwd = {}       # (池子, 原料) → 上一轮从出口转给下游的量（下一轮 sources 推出去，和产物一样晚一轮）
    arrive = {}    # (池子, 原料) → 这一轮到手的总量（扣掉转送之前）
    # 设备 → (转出的池子, 物品) → 到手里从池子出口转来的。按池子分账：两台池子的出口送同一种原料给同一台下游时，
    # 给一台定比例，另一台转来的算「别处来的」（不分账的话两台都按下游的全部吃量转，一起转多了）；绕回池子自己的也从这里看
    ptgot = defaultdict(lambda: defaultdict(float))
    pushing_pt = [None]  # 正在推的是哪台池子出口转送的原料（池子编号；None = 不是）
    pt_capping = [False]  # 正在定转送比例（外层那几趟）
    pt_rx = {}            # 设备 → 上一轮从池子出口转来的物品

    def ecap(i, c):
        return min(struct_cap[i][c] if i in struct_cap else 1.0, dem_cap[i][c] if i in dem_cap else 1.0)

    def _ch_intake(i, c, item):
        """并行池子第 c 条配方在推料时按满速用量的几成吃 item：这条别的独有原料上一轮只够跑多少（限吃量那一遍）、
        共用原料上一轮按比例分到几成、这条的出料上限，取最小。不是推料阶段（算准入口下游需求）时都按满速。"""
        nd, k = cneed[i][c], 1.0
        if resid[0] and rl[0] and last_sup:
            own = [kk for kk in nd if kk != item and kk not in shared_of[i] and nd[kk] > 0 and not (capping[0] and kk in par_products)
                   and not (pt_capping[0] and kk in pt_rx.get(i, ()))]
            if own:
                k = min(1.0, min(max(last_sup.get(i, {}).get(kk, 0.0), prev_sup.get(i, {}).get(kk, 0.0)) / nd[kk] for kk in own))
            k = min([k] + [last_scale.get(i, {}).get(kk, 1.0) for kk in nd if kk != item and kk in shared_of[i]])
        if resid[0]:
            k = min(k, ecap(i, c))
        return k

    def push(u, item, q, path, seen):
        """q/分的 item 到了 u，往下分。"""
        if q <= EPS:
            return
        if u[0] == "d" and u[1] not in buf_ids:
            if item in UNKNOWN:  # 仓库取货口 / 暗管出口来的、物品没看到：到设备再按缺口分
                star[u[1]].append((q, path, item))
                return
            sup[u[1]][item] += q
            if pushing_par[0]:
                not_exo[u[1]][item] += q
            if pushing_pt[0] is not None:
                ptgot[u[1]][(pushing_pt[0], item)] += q
            via[u[1]][item].append(path + [f"{q:g}"])
            return
        if u in seen:
            return  # 回环回到上游：这一份不再回到设备
        seen = seen | {u}
        kd = G.kind(u)
        if kd == "准入口" and u[1] in gate:
            g = gate[u[1]]
            if item in UNKNOWN and g.get("item"):
                item = g["item"]
            q2, why = gate_pass(u[1], item, q, True)
            if why == GATE_WRONG:
                return
            if why:  # 被挡下的量记账（准入口表的「被挡下」）
                gate_held[u[1]][item] += q - q2
                path = path + [why]
                q = q2
            gate_flow[u[1]][item] += q
        outs = G.out[u]
        if not outs:
            return
        if len(outs) == 1:
            v, lk = outs[0]
            push(v, item, min(q, _cap(lk)), _tag(path, lk), seen)
            return
        split(outs, item, q, path, seen, kd)

    def split(outs, item, q, path, seen, kd):
        """一个出口分到几路：主路平分（水位：吃饱的不再多给），备路（经过单进单出汇流器）只拿主路吃剩的（4.7 优先级）。
        分流器、多口暗管出口、设备自己的几个出料口都按这个分（作者 11-03：精炼炉两条出料带，没汇流器那条先走，堵了才走有汇流器那条）。"""
        uniq, keep = set(), []
        for v, lk in outs:  # 两个出口通到同一台设备：按一个缺口算（武陵城 #75 两路都进精炼炉 (1385,748)，原来灌了 40/30，隔壁只剩 20）；
            if v[0] == "d" and v in uniq:  # 通到同一个物流件的几路是真的几条路（武陵城重息壤回环：分流器两路都回汇流器，各分一份）
                continue
            uniq.add(v)
            keep.append((v, lk))
        outs = keep
        if len(outs) == 1:
            v, lk = outs[0]
            push(v, item, min(q, _cap(lk)), _tag(path, lk), seen)
            return
        main = [(v, lk) for v, lk in outs if not (v[0] == "n" and v[1] in backup_merge)]
        back = [(v, lk) for v, lk in outs if (v[0] == "n" and v[1] in backup_merge)]
        left = q
        for grp, tag in ((main, "主路"), (back, "备路")):
            if not grp or left <= EPS:
                continue
            if main_blocked and tag == "主路" and back and kd == "出料口":
                continue  # 情景：主路堵死，全部走备路
            ask[0] = True  # 真有料在分：按各路下游吃得下的量问，经过开了限制通过数量的口的记 gate_shut
            caps = [min(_cap(lk), absorb(v, item, tuple(seen))) for v, lk in grp]
            ask[0] = False
            alloc = [0.0] * len(grp)
            rest, open_ = left, list(range(len(grp)))
            while rest > EPS and open_:
                share = rest / len(open_)
                nxt = []
                for k in open_:
                    give = min(share, caps[k] - alloc[k])
                    alloc[k] += give
                    rest -= give
                    if caps[k] - alloc[k] > EPS:
                        nxt.append(k)
                if len(nxt) == len(open_):
                    break
                open_ = nxt
            left = rest
            for (v, lk), a in zip(grp, alloc):
                push(v, item, a, _tag(path + [f"{kd}{tag} 1/{len(grp)}"], lk), seen)

    def pools():
        """几个输入端合计一个总量（作者：应龙关 5、6 号息壤气合计 150，各进多少不限）：按各口下游吃得下的量水位分。
        开了限制通过数量的口（once）长期不放行：不参加分，合计全分给别的口（0.10.13 A2）→ {口: (分到, 合计名, 合计, [不参加分的口])}"""
        grp = defaultdict(list)
        for nid, g in gate.items():
            m = g.get("input") or {}
            if m.get("pool") and m["pool"].get("total") is not None:
                grp[m["pool"]["name"]].append(nid)
        out = {}
        for name, all_ids in grp.items():
            total = float(gate[all_ids[0]]["input"]["pool"]["total"])
            shut = [nid for nid in all_ids if once(gate[nid])]
            nids = [nid for nid in all_ids if nid not in shut]
            for nid in shut:
                out[nid] = (0.0, name, total, shut)
            caps = []
            ask[0] = total > EPS  # 合计有料才算在分（合计是 0 = 平时不来料：下游开了限制通过数量的口不记 shut）
            for nid in nids:
                c = absorb(("n", nid), gate[nid].get("item") or None)
                caps.append(PIPE_CAP if c == float("inf") else min(c, PIPE_CAP))
            ask[0] = False
            alloc, rest, open_ = [0.0] * len(nids), total, list(range(len(nids)))
            while rest > EPS and open_:
                share = rest / len(open_)
                nxt = []
                for k in open_:
                    give = min(share, caps[k] - alloc[k]); alloc[k] += give; rest -= give
                    if caps[k] - alloc[k] > EPS:
                        nxt.append(k)
                if len(nxt) == len(open_):
                    break
                open_ = nxt
            for nid, a in zip(nids, alloc):
                out[nid] = (a, name, total, shut)
        return out

    makers = {i for i in range(len(devices)) if inputs_of(rec[i][0])}  # 生产设备：找带子上的物品准入口时不穿过它们

    def tun_src(i):
        """外接 / 漏配的暗管出口、标了输入端的没配对暗管出口当来源（0.10.13 功能二）→ sources() 的几条来源。
        - 作者填了每分钟多少：按填的物品和量（多口最多 240），只有一路出边直接推；几路的记进 prod_out / src_kd，推料时按下游各路
          吃得下的分、排在设备产物前面（和准入口输入端一样当外来量）；填 0 = 这台平时不来料，不当来源；
        - 没填量（只填了物品也算；清水没填量时读进来补的 60 也算没填）：照旧每条出边按满管 120 估，物品取作者填的 → 下游第一个
          准入口的 → 都没有就按设备缺口分。
        作者填的物品只认液体、气体（_mark_item），填了固体的按没填物品算（卡上提醒）。"""
        d, u = devices[i], ("d", i)
        m = tun_mark.get(i)
        outs = list(G.out[u])
        rate = _mark_rate(m) if m else None
        item = _mark_item(m)
        cls = tun_out[i]["cls"]
        name = {"外接": "外接", "漏配": "没配上对的"}.get(cls, "") + d["name"]
        if rate is None:
            res = []
            for v, lk in outs:
                it = item or _first_gate_item(G, v, gate, stop=makers) or (UNK_PIPE if lk.get("layer") == "pipe" else UNK_BELT)
                what = it if it not in UNKNOWN else "物品没看到，按设备缺口分"
                # 标记没填物品（或填了固体，按没填算）：物品是按下游准入口认的，不能写成像作者填的
                txt = what if not m else f"输入端{m.get('no')}号 {it}" if item else f"输入端{m.get('no')}号，物品没填，" + (
                    f"按下游准入口认作{it}" if it not in UNKNOWN else "按设备缺口分")
                res.append((v, it, _cap(lk), [f"{name}（{txt}，没填量、按满管估）{_cap(lk):g}/分"]))
            return res
        if rate <= EPS or not outs:
            return []
        top = PIPE_CAP * tunnel_ports(d["name"])
        gi = "" if item else next((x for v, _lk in outs if (x := _first_gate_item(G, v, gate, stop=makers))), "")
        it = item or gi or UNK_PIPE
        how = "" if item else "，物品没填、按下游准入口认" if gi else "，物品没填、按设备缺口分"
        over = f"，{tunnel_ports(d['name'])} 个口最多 {top:g}" if rate > top else ""
        path = [f"输入端{m.get('no')}号{'' if it in UNKNOWN else ' ' + it}（{name}，作者填 {rate:g}/分{over}{how}）"]
        if len(outs) == 1:
            v, lk = outs[0]
            return [(v, it, min(rate, top, _cap(lk)), _tag(path, lk))]
        prod_out[(i, it)] = outs  # 几路一起分：推料时按下游各路吃得下的分（split），依据里写这台暗管出口的名字
        src_kd[(i, it)] = d["name"]
        return [(("out", i), it, min(rate, top), path)]

    def sources():
        out = []
        par_srcs.clear()
        pool = pools()
        for nid, g in gate.items():
            m = g.get("input")
            if not m:
                continue
            if nid in pool:
                a, name, total, shut = pool[nid]
                no_share = f"，{'、'.join(str(gate[s]['input'].get('no')) for s in shut)} 号开了限制通过数量、不参加分" if shut else ""
                out.append((("n", nid), g["item"], a, [f"输入端{m.get('no')}号 {g['item']}（{name} 合计 {total:g}{no_share}，这口分到 {a:g}）"]))
                continue
            rate = m.get("rate")
            try:
                rate = float(rate) if rate not in (None, "") else None
            except ValueError:
                rate = None
            if rate is None and g.get("limit") is not None:
                rate = g["limit"]
            # 开了限制通过数量（N 知道）：一次性的，长期按不来料算，没填流速也不再提醒（0.10.13 A2；以前只认游戏里读到已经放满的）
            if once(g):
                rate = 0.0
            if rate is None:
                notes.append({"kind": "输入端量不知道", "gate": nid, "item": g["item"], "msg": f"输入端 {m.get('no')} 号（{g['item']}）没填流速、也没开限速"})
                continue
            out.append((("n", nid), g["item"], rate, [f"输入端{m.get('no')}号 {g['item']} {rate:g}/分"]))
        for i, d in enumerate(devices):
            u = ("d", i)
            if d["name"] in PORT_NAMES:
                role, pit = port_role(i)
                if role != "取":
                    continue
                how = "图标" if pit else ""
                outs = list(G.out[u])
                if pit:  # 底账名字/小三角把方向弄反了（连成 设备 → 仓库口）：这台设备要这种原料，就按仓库口往它出
                    outs += [(v, lk) for v, lk in G.inn[u] if v[0] == "d" and any(pit in inputs_of(r) for r in recipes_of(v[1]))]
                for v, lk in outs:
                    item = pit or _first_gate_item(G, v, gate, stop=makers) or (UNK_PIPE if lk.get("layer") == "pipe" else UNK_BELT)
                    tag = f"{item}，认{how}" if pit else (item if item not in UNKNOWN else "物品没看到，按设备缺口分")
                    out.append((v, item, _cap(lk), [f"{d['name']}（{tag}）{_cap(lk):g}/分"]))
            elif i in tun_mark or tun_out.get(i, {}).get("cls") in TUN_CARD:  # 外接 / 漏配、作者标了来料的暗管出口（0.10.13 功能二）
                out += tun_src(i)
            elif d["name"] in SOURCE_PORTS and i not in buf_ids:  # 配上对的暗管出口是中转，不当来源
                for v, lk in G.out[u]:
                    item = _first_gate_item(G, v, gate, stop=makers) or (UNK_PIPE if lk.get("layer") == "pipe" else UNK_BELT)
                    out.append((v, item, _cap(lk), [f"{d['name']}（{item if item not in UNKNOWN else '物品没看到，按设备缺口分'}）{_cap(lk):g}/分"]))
            elif dev_rate.get(i, 0) > EPS:
                rc, _ = rec[i]
                if i in par:  # 并行池子：每条按自己的开工率出产物，几条产同一种的相加
                    agg = {}
                    for c, od in enumerate(cout[i]):
                        for it_, q_ in od.items():
                            agg[it_] = agg.get(it_, 0.0) + q_ * chan_rate[i][c]
                    prods = [(it_, q_) for it_, q_ in agg.items() if q_ > EPS]
                elif i in alt and i in alt_split:  # 交替配方：主配方和交替配方各按自己的开工时间出产物（应龙关液气转化机 (950,879) 轮流做两路液体）
                    t1, t2 = alt_split[i]
                    prods = [(it, r * t1) for it, r in outputs_of(rc).items()] + [(it, r * t2) for it, r in outputs_of(alt[i][0]).items()]
                else:
                    prods = [(it, r * dev_rate[i]) for it, r in outputs_of(rc).items()]
                for item, q in prods:
                    # 产物只往吃得下这种物品的方向走（直接对接的设备要用它 / 去仓库 / 去物流件）
                    fluid = "气" in item or "液" in item or item in ("清水", "污水", "沉积酸")
                    targets = [(v, lk) for v, lk in G.out[u]
                               if (not lk.get("items") or item in lk["items"])
                               and (not lk.get("layer") or (lk["layer"] == "pipe") == fluid)  # 固体只走带子、流体只走管子：
                               # 精炼炉的赤铜块不往污水管里分（武陵城 (1112,748) 两条带 + 一根污水管，原来按 3 份分成 10/10）
                               and (v[0] == "n" or devices[v[1]]["name"] in ("仓库存货口",) or v[1] in buf_ids or absorb(v, item) > EPS)]
                    if len(targets) == 1:
                        v, lk = targets[0]
                        out.append((v, item, min(q, _cap(lk)), _tag([f"{d['name']}产 {item} {q:.2g}/分"], lk)))
                    elif targets:  # 几个出料口：推料时按主路/备路分（那时才知道下游各吃得下多少）
                        prod_out[(i, item)] = targets
                        out.append((("out", i), item, q, [f"{d['name']}产 {item} {q:.2g}/分"]))
                    else:
                        continue
                    if i in par:
                        par_srcs.add(len(out) - 1)
        pt_srcs.clear()
        for (i, x), q in fwd.items():  # 池子出口转送的原料（0.10.13 功能三）：和产物一样晚一轮、往同形态的出口推，只给吃它的下游
            if q <= EPS:
                continue
            targets = [(v, lk) for v, lk in pt_outs(i, x)
                       if v[0] == "n" or devices[v[1]]["name"] in ("仓库存货口",) or v[1] in buf_ids or absorb(v, x) > EPS]
            tag = f"{devices[i]['name']}从{'管' if PM.is_fluid(x) else '带'}出口转送 {x} {q:.3g}/分"
            if len(targets) == 1:
                v, lk = targets[0]
                out.append((v, x, min(q, _cap(lk)), _tag([tag], lk)))
            elif targets:  # 几个出口：和产物一样按主路 / 备路分
                prod_out[(i, x)] = targets
                out.append((("out", i), x, q, [tag]))
            else:
                continue
            pt_srcs[len(out) - 1] = i
        return out

    par_srcs = set()  # sources() 这一趟返回的来源里，哪几条是并行池子的产物（下标）
    pt_srcs = {}      # 哪几条是池子出口转送的原料：下标 → 转出的池子

    prod_out = {}
    # 几路一起分的来源里不是设备出料口的 → split 依据里写的名字（作者填了量、接出几路的暗管出口，0.10.13 功能二）。
    # 推料时它们先分（作者给的外来量，和准入口输入端一样），「主路堵死」情景只认设备出料口，套不到它们上
    src_kd = {}

    def drain(u, item, rate, seen=()):
        """最多能出去多少（见 _drain）。作者说出口送原料的池子：自己用的 + 出口下游能出去的，从自己的出口绕回自己算 0
        （不算「不知道」）。别的设备、别的物品直接是 _drain（0.10.13 功能三）"""
        if pt and u[0] == "d" and item in pt.get(u[1], ()):
            if u in seen:
                return 0.0
            own = _drain(u, item, rate, seen)
            if own == float("inf"):
                return own
            return own + sum(min(_cap(lk), drain(v, item, rate, seen + (u,))) for v, lk in pt_outs(u[1], item))
        return _drain(u, item, rate, seen)

    def _drain(u, item, rate, seen=()):
        """item 从 u 往下最多能出去多少（按下游设备这一轮的开工潜力）。去向里有仓库、没出口的物流件/储存箱、绕回的环 → 不知道，返回 inf。"""
        if u in seen:
            return float("inf")
        if u[0] == "d" and u[1] not in buf_ids:
            nm = devices[u[1]]["name"]
            need, _ = dev_need(u[1])
            if nm in ("仓库存货口",) or not need:
                return float("inf")
            if item not in need:
                return float("inf")  # 连到不吃这种东西的设备：多半是认错了连线/配方，不拿它当「堵」
            # 只算结构上的堵：满速用量 × 它自己的产物出不出得去。不看它别的原料够不够
            # （粉碎机 → 研磨机：两台粉碎机分别供源石粉末和砂叶粉末，按「别的原料够不够」算会互相压成 0）
            # 并行池子：各条按自己的结构背压吃（不用下游需求上限 dem_cap，见 chan_rate 上面的说明）。注意 _backpressure 前
            # struct_cap、out_cap 都先清空（先清后算，和原来一样），所以这里和下面普通设备的系数实际总是 1：结构背压只往上传一层
            # （2026-11-19 核实：池子串池子、下游池子那条被准入口限到 50% 时，上游池子的 struct_cap 仍是空的，靠 dem_cap 限住）
            if u[1] in par:
                return sum(nd.get(item, 0.0) * (struct_cap[u[1]][c] if u[1] in struct_cap else 1.0) for c, nd in enumerate(cneed[u[1]]))
            return need[item] * out_cap.get(u[1], 1.0)
        outs = G.out[u]
        if not outs:
            return float("inf")  # 没出口的储存箱、没追到下游的物流件：满不满看玩家，不当堵
        tot = 0.0
        for v, lk in outs:
            if lk.get("items") and item not in lk["items"]:
                continue
            tot += min(_cap(lk), drain(v, item, rate, seen + (u,)))
        if G.kind(u) == "准入口":
            tot = gate_ask(u[1], item, tot)
        return tot

    def _backpressure(rate) -> tuple[dict, dict]:
        """每台生产设备：每种产物，所有去向都是确定的设备时，下游这一轮最多吃多少 ÷ 满速产量 → 开工率上限。
        几台生产者往同一台下游送时，每台都按下游全部用量算（只会少算堵，不会多算）。交替配方的设备不算。
        并行池子按条算（第二个返回值，池子 → [每条的上限]）：某种产物没有去向 → 含它的那条不限；下游有「不知道」的（inf）→
        这种产物不参加取小，同一条的别的产物照样限。"""
        caps, ch_caps = {}, {}
        for i, d in enumerate(devices):
            rc, _ = rec[i]
            if i in par:
                icap = {}
                for item, full in cout_all[i].items():
                    if full <= EPS:
                        continue
                    targets = [(v, lk) for v, lk in G.out[("d", i)] if (not lk.get("items") or item in lk["items"])
                               and (not lk.get("layer") or (lk["layer"] == "pipe") == _is_fluid(item))]
                    if not targets:
                        icap[item] = None
                        continue
                    tot = sum(min(_cap(lk), drain(v, item, rate, (("d", i),))) for v, lk in targets)
                    if tot != float("inf"):
                        icap[item] = max(0.0, min(1.0, tot / full))
                chc = [1.0 if any(k in icap and icap[k] is None for k in od) else min([icap[k] for k in od if k in icap] or [1.0])
                       for od in cout[i]]
                if any(x < 1.0 - 1e-3 for x in chc):
                    ch_caps[i] = chc
                continue
            if i in alt or i in buf_ids or not outputs_of(rc) or not inputs_of(rc):
                continue
            u = ("d", i)
            lim = 1.0
            for item, full in outputs_of(rc).items():
                if full <= EPS:
                    continue
                fluid = "气" in item or "液" in item or item in ("清水", "污水", "沉积酸")
                targets = [(v, lk) for v, lk in G.out[u] if (not lk.get("items") or item in lk["items"])
                           and (not lk.get("layer") or (lk["layer"] == "pipe") == fluid)]
                if not targets:
                    lim = None  # 有产物没找到去向：不知道，不限
                    break
                tot = sum(min(_cap(lk), drain(v, item, rate, (u,))) for v, lk in targets)
                if tot == float("inf"):
                    continue
                lim = min(lim, tot / full)
            if lim is not None and lim < 1.0 - 1e-3:
                caps[i] = max(0.0, lim)
        return caps, ch_caps

    def eat(u, item, origin, acc, seen=()):
        """item 从 u 往下「实际」能被吃掉多少（给并行池子 origin 定出料上限用）。和 drain 不同，这里看下游别的原料够不够：
        - 普通设备：满速用量 × min(1, 它自己的结构背压, 每种别的原料 到手 / 用量)，再减去它从别处（不是并行池子、也不是按缺口分的
          未知来料）已经拿到的这种料。别的原料到手是 0 的不按它截（多半是物流图漏连：按它把池子截成 0，省下的共用原料会送给别人、
          藏住别处的缺料）；只有「有、但不够」才截。别的原料是 origin 自己的产物时，按那条配方没被共用原料比例压之前的量算
          （池内反向耦合：下游吃 芽针溶液×1 + 锦草溶液×2、清水不够时两条一起被压，不能拿压过的量再去压自己）；
        - 下游也是并行池子：逐条配方算，相加；共用原料的分母 = 这条的用量 + 别的条按 chan_rates 口径（独有原料、出料上限）
          实际要的量；吃不满时和普通设备一样点名（写进 why / keys）；
        - 储存箱 / 储气罐 / 暗管 / 物流件：穿过去，各路相加，每路受连线上限封顶；准入口物品不对算 0、有限速按限速封顶；
          通到同一台设备的几路只算一次（和 split 同口径）；
        - 不知道的去处 → inf：仓库存货口、没配方的设备、交替配方设备、没出口的储存箱或物流件、绕回的环；
        - 不吃这种料的设备 → 0（推料时也不往那边分）。
        acc：found（追到过吃它的设备没有）、why / keys（点名限住它的下游设备）。"""
        if u in seen:
            return float("inf")
        if u[0] == "d" and u[1] not in buf_ids:
            j = u[1]
            need, _ = dev_need(j)
            if devices[j]["name"] == "仓库存货口" or not need or j in alt:
                return float("inf")
            if item not in need:
                return 0.0
            acc["found"] = True
            exo = max(0.0, sup[j].get(item, 0.0) - not_exo[j].get(item, 0.0))

            def got(kk):
                g = sup[j].get(kk, 0.0)
                if kk in cout_all[origin]:
                    s = max((min([last_scale.get(origin, {}).get(k2, 1.0) for k2 in cneed[origin][c] if k2 in shared_of[origin]] or [1.0])
                             for c, od in enumerate(cout[origin]) if od.get(kk, 0.0) > 0), default=1.0)
                    if s > EPS:
                        g /= s
                return g
            if j in par:
                # 共用原料按别的条「实际能跑多少」分，和 PM.chan_rates 一个口径（2026-11-19）。原来分母是各条满速用量相加，
                # 把不跑的那条也算进去：合成例子 P2 两条共用蓝铁粉末 15、赫铜溶液为 0，chan_rates 里第二条跑 0、蓝铁全归第一条
                # （能跑 50%、吃 30 壤晶废液），这里却按 15/(30+30) 只算吃 15，上游池子废液那条被压到 50%，两台一起低估还自洽收敛。
                # 别的条独有原料到手为 0 就按 0 算，不套「到手为 0 不截」：那条是防止把 origin 截低，这里让别的条按 0 是把共用原料让给
                # 这条、把 origin 放宽，方向本来就对。
                def _f_other(c2):
                    n2 = cneed[j][c2]
                    own = [got(k2) / x for k2, x in n2.items() if k2 != item and k2 not in shared_of[j] and x > 0]
                    return max(0.0, min([1.0, ecap(j, c2)] + own))
                tot, full, worst = 0.0, 0.0, None
                for c, nd in enumerate(cneed[j]):
                    if item not in nd:
                        continue
                    k, lim_k = ecap(j, c), None
                    for kk, v in nd.items():
                        if kk == item or v <= 0 or sup[j].get(kk, 0.0) <= EPS:
                            continue
                        dn = v + sum(n2[kk] * _f_other(c2) for c2, n2 in enumerate(cneed[j]) if c2 != c and kk in n2) \
                            if kk in shared_of[j] else v
                        if got(kk) / dn < k:
                            k, lim_k = got(kk) / dn, (kk, sup[j].get(kk, 0.0), dn)
                    k = max(0.0, min(1.0, k))
                    tot += nd[item] * k
                    full += nd[item]
                    if k < 1.0 - 1e-3 and (worst is None or k < worst[0]):
                        worst = (k, lim_k)
                if tot < full - 1e-3 and worst:
                    # 和普通设备一样点名（2026-11-19）：池子串池子（P1 液化息壤 → P2，P2 的污水只有 15）时 P1 的「出料受限」原来不提 P2，
                    # channels[].named 空着，run_base 也就挡不住 P2 反过来把液化息壤列进「卡在」，两台互相指认。取压得最狠的那条的原因。
                    d, (k, lim_k) = devices[j], worst
                    acc["why"].append(f"{d['name']}({d['x']},{d['y']}) " + (f"缺{lim_k[0]}（能送到 {lim_k[1]:g}/{round(lim_k[2], 2):g}）" if lim_k
                                                                             else f"自己的产物出不去（背压 {k:.0%}）")
                                      + f"，只吃得下 {round(tot, 2):g}/{full:g}")
                    acc["keys"].append(f"{d['name']}@{d['x']},{d['y']}")
                return max(0.0, tot - exo)
            k, lim_k = out_cap.get(j, 1.0), None
            for kk, v in need.items():
                if kk == item or v <= 0 or sup[j].get(kk, 0.0) <= EPS:
                    continue
                if got(kk) / v < k:
                    k, lim_k = got(kk) / v, (kk, sup[j].get(kk, 0.0), v)
            k = max(0.0, min(1.0, k))
            if k < 1.0 - 1e-3:
                d = devices[j]
                acc["why"].append(f"{d['name']}({d['x']},{d['y']}) " + (f"缺{lim_k[0]}（能送到 {lim_k[1]:g}/{lim_k[2]:g}）" if lim_k
                                                                         else f"自己的产物出不去（背压 {k:.0%}）")
                                  + f"，只吃得下 {need[item] * k:g}/{need[item]:g}")
                acc["keys"].append(f"{d['name']}@{d['x']},{d['y']}")
            return max(0.0, need[item] * k - exo)
        outs = G.out[u]
        if not outs:
            return float("inf")
        tot, dv = 0.0, set()
        for v, lk in outs:
            if lk.get("items") and item not in lk["items"]:
                continue
            if v[0] == "d":  # 通到同一台设备的几路按一个去向算（split 推料时也去重）
                if v in dv:
                    continue
                dv.add(v)
            tot += min(_cap(lk), eat(v, item, origin, acc, seen + (u,)))
        if G.kind(u) == "准入口":
            tot = gate_ask(u[1], item, tot)
        return tot

    def _demand_caps() -> tuple[dict, dict]:
        """按刚收敛的「定上限」那趟，算每台并行池子每条配方的下游需求上限 = 它的产物下游实际吃得下 ÷ 满速产量（取各产物里最小的）。
        产物一条去向都没有、或一路追下去没有一台吃它的设备 → 含它的那条不限（物流图漏连时不把池子压成 0）；
        下游合计是 inf（不知道）→ 这种产物不参加取小。→ ({池子: [每条上限]}, {池子: {产物: (吃得下, 满速产, 点名说明, 点名设备键)}})"""
        caps, whys = {}, {}
        for i in par:
            u = ("d", i)
            icap, iw = {}, {}
            for item, full in cout_all[i].items():
                if full <= EPS:
                    continue
                targets, seen_d = [], set()
                for v, lk in G.out[u]:
                    if lk.get("items") and item not in lk["items"] or lk.get("layer") and (lk["layer"] == "pipe") != _is_fluid(item):
                        continue
                    if v[0] == "d":
                        if v[1] not in buf_ids and devices[v[1]]["name"] != "仓库存货口" and dev_need(v[1])[0] \
                                and not any(item in inputs_of(r) for r in recipes_of(v[1])):
                            continue  # 直接相连、但哪条配方都不吃它的设备（交替配方的原料也看）：推料时也不往那边分
                        if v in seen_d:
                            continue  # 两根管子通到同一台：按一个去向算
                        seen_d.add(v)
                    targets.append((v, lk))
                if not targets:
                    icap[item] = None
                    continue
                acc = {"found": False, "why": [], "keys": []}
                tot = sum(min(_cap(lk), eat(v, item, i, acc, (u,))) for v, lk in targets)
                if tot == float("inf"):
                    continue
                if tot <= EPS and not acc["found"]:
                    icap[item] = None
                    continue
                icap[item] = min(1.0, tot / full)
                iw[item] = (tot, full, acc["why"], acc["keys"])
            caps[i] = [1.0 if any(k in icap and icap[k] is None for k in od) else min([icap[k] for k in od if k in icap] or [1.0])
                       for od in cout[i]]
            whys[i] = iw
        return caps, whys

    star = defaultdict(list)

    def _reset():
        """每一趟内层都从「全部满速」开始：种植机 ↔ 采种机这种互相供料的环从 0 开始永远起不来，
        外层定池子上限的那几趟也得这样，否则上一趟池子多占清水时环被压低的值会留下来（棘轮）。"""
        dev_rate.clear()
        dev_rate.update({i: 1.0 for i in range(len(devices))})
        out_cap.clear(); struct_cap.clear(); last_scale.clear(); last_sup.clear(); prev_sup.clear()
        alt_split.clear()
        alt_split.update({i: (0.5, 0.5) for i in alt})
        for i in par:
            chan_rate[i] = [1.0] * len(par[i])
        # 池子出口转送的原料也从「池子满速」开始：第一轮就按池子满速用量的 φ 成往下游推（和全部设备从满速开始一个道理）。
        # 从 0 开始时转送的料第二轮才到下游，下游第二轮按「别的原料够跑多快」限吃量用的还是第一轮的 0，别的设备开工率又正好没动，
        # 内层会在下游 0 上假收敛（合成场景：池子壤晶废液经分流器分给天有洪炉和灌装机，比例 0.5 时洪炉算成 0，外层 16 轮跑满）
        fwd.clear(); ptgot.clear(); pt_rx.clear()
        fwd.update({(i, x): ph * dev_need(i)[0].get(x, 0.0) for (i, x), ph in phi.items()})

    def _take_at(u, item, seen):
        """item 到了 u，往下「实际」吃得下多少（给池子出口转送定比例，0.10.13 功能三）。和 eat 一个口径：设备 = 用量 ×
        min(1, 背压, 每种别的原料 到手 / 用量) − 它从别处拿到的这种料；但别的原料到手是 0 就是 0（和 eat 不同：按 0 算才不会把料
        白白转走）。不知道的去处（仓库存货口、没配方、交替配方、没出口的物流件、绕回别处的环）→ inf：出口按满带 / 满管送；
        绕回这台池子自己 → 0；作者说没在用的池子 → 0。准入口走 gate_pass（限速、物品不对对转送一样起作用）。
        它和 absorb 一样是「料怎么分」：probe 那遍堵死型的口照样挡（池子出口后面的口堵死，游戏里池子把料留给自己用）。"""
        if u in seen:
            return 0.0 if u == seen[0] else float("inf")
        if u[0] == "d" and u[1] not in buf_ids:
            j = u[1]
            if j in pool_unused:
                return 0.0
            need, _ = dev_need(j)
            if devices[j]["name"] == "仓库存货口" or not need or j in alt:
                return float("inf")
            if item not in need:
                return 0.0
            # 它从别处拿到的这种料：只扣这台池子（seen[0]）自己转来的；别的池子出口转来的算别处来的
            exo = max(0.0, sup[j].get(item, 0.0) - ptgot[j].get((seen[0][1], item), 0.0))
            if j in par:
                tot = 0.0
                for c, nd in enumerate(cneed[j]):
                    if item in nd:
                        k = min([ecap(j, c)] + [sup[j].get(kk, 0.0) / x for kk, x in nd.items() if kk != item and x > 0])
                        tot += nd[item] * max(0.0, min(1.0, k))
                return max(0.0, tot - exo)
            k = min([out_cap.get(j, 1.0)] + [sup[j].get(kk, 0.0) / x for kk, x in need.items() if kk != item and x > 0])
            return max(0.0, need[item] * max(0.0, min(1.0, k)) - exo)
        outs = G.out[u]
        if not outs:
            return float("inf")
        tot, dv = 0.0, set()
        for v, lk in outs:
            if lk.get("items") and item not in lk["items"]:
                continue
            if v[0] == "d":  # 通到同一台设备的几路按一个去向算（和 split、eat 一样）
                if v in dv:
                    continue
                dv.add(v)
            tot += min(_cap(lk), _take_at(v, item, seen + (u,)))
        if G.kind(u) == "准入口":
            tot = gate_pass(u[1], item, tot, ask[0])[0]
        return tot

    pt_fresh = {"lo": 0.0, "hi": 1.0, "hlo": None, "hhi": None, "side": 0}
    pt_state = {k: dict(pt_fresh) for k in phi}  # 每个转送项的夹逼区间

    def _pt_step() -> list:
        """按刚收敛的那趟内层，每个转送项算 h = 实际交给下游的 − 下游实际吃得下的，夹逼找 h = 0 的比例。
        - 实际交给下游的 = 转出去的 − 绕回这台池子自己的（出口经分流器绕回自己时，绕回的那份池子又自己用了，不算给了下游；
          只按转出去的比，下游吃不满一条带时会定在「转出 = 下游吃量」，分流器对半分，下游实际只拿到一半）；
          转出去的超过出口连线的上限也算多（h 取两者大的）。
        - 下游吃得下的封顶在「到手 − 绕回的」，所以 0 处 h ≤ 0、1 处 h ≥ 0，根一定在 [0, 1]；转得越多池子越慢、下游越吃不下，
          h 一般随比例单调。下游一点都吃不下时根就是 0（「下游吃得下 / 到手」= 0 落在区间端点上也直接用，不对半逼近）。
        - 下一步：两端都有了用割线（Illinois）：下游吃量随比例线性变（下游要的原料比到手的少、斜率在 −1 到 0 之间）时一步到根；
          只先试「下游吃得下 / 到手」的话这种情形是线性慢收，16 轮跑满。只有一端时先试「下游吃得下 / 到手」（这时它一定落在
          区间里），区间收没了 h 还大（别的东西变了，比如并行池子的出料上限、别的池子的比例），重新放开。→ 还在动的转送项"""
        mv = []
        for k in phi:
            i, x = k
            s, ph, st = arrive.get(k, 0.0), phi[k], pt_state[k]
            outs = pt_outs(i, x)
            top = sum(_cap(lk) for _v, lk in outs)       # 出口连线最多送多少
            back = ptgot[i].get((i, x), 0.0)             # 转出去又绕回自己的
            want = min(sum(min(_cap(lk), _take_at(v, x, (("d", i),))) for v, lk in outs), max(0.0, s - back))
            h = max(ph * s - back - want, ph * s - top)
            if s <= EPS or abs(h) <= max(0.01, 1e-3 * s):
                continue
            if h > 0:
                if st["side"] == 1 and st["hlo"] is not None:
                    st["hlo"] /= 2
                st.update(hi=ph, hhi=h, side=1)
            else:
                if st["side"] == -1 and st["hhi"] is not None:
                    st["hhi"] /= 2
                st.update(lo=ph, hlo=h, side=-1)
            pic = min(1.0, min(want + back, top) / s)
            if st["hlo"] is not None and st["hhi"] is not None:
                nph = st["lo"] + (st["hi"] - st["lo"]) * (-st["hlo"]) / (st["hhi"] - st["hlo"])
            elif st["lo"] <= pic <= st["hi"]:
                nph = pic
            else:
                nph = (st["lo"] + st["hi"]) / 2
            if st["hi"] - st["lo"] < 1e-6:
                st.update(lo=0.0, hi=1.0, hlo=None, hhi=None, side=0)
                nph = pic
            phi[k] = min(1.0, max(0.0, nph))
            mv.append(k)
        return mv

    def _pt_ask():
        """池子出口转送的原料（0.10.13 功能三）经过开了限制通过数量的口：转送比例定成 0 以后推料一点都没有，定比例的 _take_at
        又是在内层之外调的（每轮内层清 gate_shut）。按收敛后的状态再问一遍出口下游吃得下多少（记 shut），池子确实到手了这种原料才问"""
        ask[0] = True
        for (i, x) in phi:
            if arrive.get((i, x), 0.0) > EPS:
                for v, _lk in pt_outs(i, x):
                    _take_at(v, x, (("d", i),))
        ask[0] = False

    def inner(limit) -> tuple[int, bool]:
        """推料 → 每台的开工率 → 背压，迭代到不再变。→ (轮数, 收敛了没有)"""
        _reset()
        for it in range(limit):
            if pt:  # 上一轮谁从池子出口拿到了转送的原料（定比例那几趟，算「别的原料够跑多快」时不算它们）
                pt_rx.clear()
                pt_rx.update({j: {x for (_p, x), q in v.items() if q > EPS} for j, v in ptgot.items()})
                ptgot.clear()
            sup.clear(); via.clear(); star.clear(); notes.clear(); not_exo.clear(); gate_flow.clear(); gate_held.clear()
            gate_shut.clear()  # 和 gate_flow 一样留最后一轮的（第 0 轮全部满速起步推的不算，复审 2026-11-21）
            resid[0] = True
            prod_out.clear()
            src_kd.clear()
            srcs = sources()
            for k, (u, item, q, path) in enumerate(srcs):  # 先推只有一个去向的，再分几个出料口的：分的时候已经吃饱的下游不再多给
                if u[0] != "out":          # （应龙关采种机 (349,1161) 原来先各给 15，另一台又单独给 (349,1332) 30，一台 45、一台 15）
                    pushing_par[0] = k in par_srcs
                    pushing_pt[0] = pt_srcs.get(k)
                    push(u, item, q, path, frozenset())
            # 几个出料口要分的：出口少的先分（背压：出口少的没别处可去，先把自己那几台下游填满；出口多的再分剩下的缺口）。
            # 2026-11-10 武陵城左列：采种机 (326,1463) 两个去向、(361,1123) 三个去向，都通种植机 (361,1294)。
            # 先分三个去向的那台时三台各 20，两台种植机只有 2/3；游戏里 (361,1294) 吃饱后那条带子堵住、种子只流向另两台，理论满速（作者确认）
            # 作者填了量的外接暗管出口排在所有设备产物前面（0.10.13 功能二）：别人的武陵城扩容反应池 (1570,1196) 暗管来 12、隔壁池子补 18；
            # 产物先分时隔壁池子先把它灌满 30，作者填的 12 在依据里看不到。没有这种来源的据点（src_kd 空的）顺序和原来一样
            for k, (u, item, q, path) in sorted(((k, s) for k, s in enumerate(srcs) if s[0][0] == "out"),
                                                key=lambda ks: ((ks[1][0][1], ks[1][1]) not in src_kd, len(prod_out[(ks[1][0][1], ks[1][1])]))):
                pushing_par[0] = k in par_srcs
                pushing_pt[0] = pt_srcs.get(k)
                split(prod_out[(u[1], item)], item, q, path, frozenset(), src_kd.get((u[1], item), "出料口"))
            pushing_par[0], pushing_pt[0] = False, None
            resid[0] = False
            for i, lst in star.items():  # 仓库带 / 暗管：一条带（管）一种物品，先给还没有来源的原料，再给缺得最多的
                need, _ = dev_need(i)
                for q, path, unk in sorted(lst, key=lambda z: -z[0]):
                    fit = [k for k in need if _unknown_fits(unk, k)]  # 管子来的只能是流体原料，带子来的只能是固体原料
                    if not fit:
                        continue
                    k = min(fit, key=lambda k: (sup[i].get(k, 0.0) > EPS, sup[i].get(k, 0.0) / need[k]))
                    sup[i][k] += q
                    not_exo[i][k] += q  # 按缺口分来的：不当成下游「从别处已经拿到的」（它可能本来就是池子的产物）
                    via[i][k].append(path + [f"当作{k}", f"{q:g}"])
            pt_moved = False
            if phi:  # 池子出口转送的原料：到手的 φ 成从出口出去（下一轮推），池子自己（开工率、并行各条）只用剩下的
                new_fwd = {}
                for (i, x), ph in phi.items():
                    a = sup[i].get(x, 0.0)
                    arrive[(i, x)] = a
                    new_fwd[(i, x)] = ph * a
                    sup[i][x] = a - ph * a
                pt_moved = any(abs(q - fwd.get(k, 0.0)) >= 1e-3 for k, q in new_fwd.items())
                fwd.clear()
                fwd.update(new_fwd)
            prev_sup.clear()
            prev_sup.update(last_sup)
            last_sup.clear()
            last_sup.update({i: dict(v) for i, v in sup.items()})
            new, new_ch = {}, {}
            for i in par:  # 并行池子：每条按自己的原料和出料上限，共用原料不够时按各条用量比例分（推断）
                new_ch[i], last_scale[i] = PM.chan_rates(cneed[i], sup[i], [ecap(i, c) for c in range(len(par[i]))])
            for i, d in enumerate(devices):
                need, fuel = dev_need(i)
                if not need:
                    new[i] = 0.0
                    continue
                rs = [min(1.0, sup[i].get(k, 0.0) / v) for k, v in need.items() if v > 0]
                new[i] = min(rs) if rs else 0.0
                if i in par:
                    new[i] = max(new_ch[i])  # 只当「出不出产物」的开关；出多少按每条算（sources）
                if i in alt:  # 交替配方：两种配方各自的原料够跑多少，按比例分时间（共用的原料只算一次）
                    aneed = {k: per_min(alt[i][0], k) for k in inputs_of(alt[i][0])}
                    shared = [k for k in need if k in aneed]
                    f1 = min((sup[i].get(k, 0.0) / v for k, v in need.items() if k not in shared and v > 0), default=1.0)
                    f2 = min((sup[i].get(k, 0.0) / v for k, v in aneed.items() if k not in shared and v > 0), default=1.0)
                    fs = min((sup[i].get(k, 0.0) / need[k] for k in shared if need[k] > 0), default=1.0)
                    tot = min(1.0, f1 + f2, fs)
                    alt_split[i] = (tot * f1 / (f1 + f2), tot * f2 / (f1 + f2)) if f1 + f2 > EPS else (0.0, 0.0)
                    new[i] = tot
            if BACKPRESSURE:
                for i in new:
                    new[i] = min(new[i], out_cap.get(i, 1.0))
                out_cap.clear()  # 先清再算：drain 按下游满速用量算，不叠上一轮的背压（和原来一样）
                struct_cap.clear()
                caps, ch_caps = _backpressure(new)
                if probe_mode[0]:  # probe：背压是 0 的（出料堵死）不算，正的照算（出料带子装不下、下游只吃一半）
                    caps = {k: v for k, v in caps.items() if v > EPS}
                    ch_caps = {k: [1.0 if x <= EPS else x for x in v] for k, v in ch_caps.items()}
                    ch_caps = {k: v for k, v in ch_caps.items() if any(x < 1.0 - 1e-3 for x in v)}
                out_cap.update(caps)
                struct_cap.update(ch_caps)
            ch_moved = any(abs(a - b) >= 1e-3 for i in par for a, b in zip(new_ch[i], chan_rate[i]))
            chan_rate.update(new_ch)
            if all(abs(new[i] - dev_rate[i]) < 1e-3 for i in new) and not ch_moved and not pt_moved:
                return it + 1, True
            dev_rate.clear()
            dev_rate.update(new)
        return limit, False

    # 外层（只有据点里有并行池子时才跑）：定每条配方的下游需求上限 dem_cap。每一轮都从全部满速按「定上限」口径把内层跑到收敛，
    # 再按收敛后的到手量算新上限（_demand_caps），上限不再变就停；停下后按这次调用自己的口径（主遍 / 能送到多少 / 主路堵死）
    # 带着上限从满速再跑一趟出结果。「定上限」口径 = 按实际限吃量跑，但任何设备算「别的原料够跑多快」时不把并行池子的产物算进去
    # （不然下游因为缺池子的产物少吃别的原料，再反过来把池子限住，自己压自己）。合成场景见 tests/test_pool_parallel.py。
    # 没有并行池子的据点只跑最后那一趟，和原来一模一样。
    # capping 现在是防护性的（2026-11-19 核实）：四个据点和已收敛的合成场景里关掉它结果都不变（absorb 限吃量滞后两轮，
    # 内层第 3 轮就停了，轮不到它起作用）；只在内层跑满的场景里决定外层收不收敛（池子 + 种植机环共用 55 清水、芽针粉末 10、
    # 下游瓶子来自分流器：关掉后外层 8 轮不收敛），测试 test_capping_keeps_outer_converging
    # 池子出口转送原料的比例（0.10.13 功能三，pt_free）也在这个外层定，和出料上限一起，两个都不动了才停（最多 PT_OUTER_MAX 轮）。
    # 定比例那几趟（pt_capping）任何设备算「别的原料够跑多快」时不把从池子出口转来的原料算进去，和 capping 同一个道理：
    # 不加的话作者那组收敛到「池子 1/3、天有洪炉 1/3」这个错的自洽解（洪炉因为息壤少不收提纯机多出来的壤晶废液，息壤又因此给得少），
    # 加上以后是 2/7、5/14（作者实测重息壤 2.14/分）。没有出口回答的据点循环和原来一模一样
    def settle(it) -> tuple[bool, list]:
        """跑上面说的外层 → (出料上限最后一轮还在动没有, 还在动的转送项)。只写 dem_cap、dem_why、phi、pt_state（外层的结果）；
        it 记轮数（{"outer", "inner", "hit_cap"}，probe 那遍给一份扔掉的）"""
        moved, pt_mv = False, []
        if not (par or pt_free):
            return moved, pt_mv
        rl[0], capping[0], pt_capping[0] = True, bool(par), pt_free
        prev_mv = {}
        for rounds in range(1, (PT_OUTER_MAX if pt_free else OUTER_MAX) + 1):
            n, ok = inner(CAP_INNER_MAX)
            it["inner"].append(n)
            it["hit_cap"] |= not ok
            if pt_free:
                pt_mv = _pt_step()
            if par:
                nc, nw = _demand_caps()
                for i, caps in nc.items():
                    old = dem_cap.get(i, [1.0] * len(caps))  # 「不限」按 1.0 比
                    for c in range(len(caps)):
                        mv = caps[c] - old[c]
                        if mv * prev_mv.get((i, c), 0.0) < 0:
                            # 这一轮的变化方向和上一轮相反（共用清水让给下游别的原料的生产者：上限越松，下游越吃不下）：取中点防来回跳。
                            # 方向一致不阻尼：三层池子串联时上限一层层单调往上传，每轮取平均会拖成 8 轮还收不拢
                            caps[c] = (caps[c] + old[c]) / 2
                            mv = caps[c] - old[c]
                        if abs(mv) >= 1e-9:
                            prev_mv[(i, c)] = mv
                moved = any(abs(caps[c] - dem_cap.get(i, [1.0] * len(caps))[c]) >= 1e-3 for i, caps in nc.items() for c in range(len(caps)))
                dem_cap.clear()
                dem_cap.update({i: caps for i, caps in nc.items() if any(x < 1.0 - 1e-3 for x in caps)})
                dem_why.clear()
                dem_why.update(nw)
            it["outer"] = rounds
            if not moved and not pt_mv:
                break
        rl[0], capping[0], pt_capping[0] = rate_limited_intake, False, False
        return moved, pt_mv

    iters = {"outer": 0, "inner": [], "hit_cap": False}
    moved, pt_mv = settle(iters)
    n, ok = inner(INNER_MAX)
    # probe（复审 2026-11-22，第四轮改）：把外层、内层照这次调用的设置（主路堵死情景、限吃量）从头再跑，只拿来
    # （甲）判出料堵死的设备是原料有、只是出不去（间歇）还是原料真没有（没料）；
    # （乙）那一遍最后一轮真有料在动时送到开了限制通过数量的口上的物品，并进 shut（上游有料、只是被背压压成 0 的口）。两遍：
    # - probe 1「出料堵死的设备当它吃得下」：背压是 0 的不算，别的照主分析（堵死型的口照样挡、正常的背压照算）。游戏里口放满以后
    #   别处照常转，主路上的兄弟只吃它出得去的那么多（出料带子装不下、下游只吃一半、另一路出料过放满的口），多的照样走备路到
    #   堵死的设备。第三轮把背压整个去掉，主路兄弟吃满，备路上原料其实有的设备反而分不到。
    # - probe 2「整条线都不算出口堵塞」：另外在算背压（drain）、并行池子的下游需求上限（eat）时把堵死型的口（开了限制通过数量、
    #   限速 0、物品不对）当放行（gate_ask），正的背压照算：上游只因为口堵死才减速、停下的那部分料（备路上的限量口、
    #   并行池子只能往限量口出的那条）算得出来。
    # 两遍里料怎么分（推料、分料、池子出口转送）都照样在堵死型的口挡：游戏里口堵死了，料就分给别处、池子留给自己用；
    # 池子的下游需求上限、出口转送比例按这一遍重新定。（甲）每种原料哪一遍到手 > 0 都算有，只判普通设备：
    # 并行池子每条都被截到 0 的一律写间歇（见 _par_record）；（乙）主分析、两遍的 shut 并起来。
    # 这两遍是估算，已知会判错（设计文档「第三轮核实、撤回的」）。2026-11-22 试过、都比不改差、当天撤回的，别再直接加回来：
    # 要在同一遍里凑齐每种原料（两遍会各放开一台抢料的，有料的也判成没料）；放宽轮数、没收敛就整遍不用（内层是整个据点一起判收敛的，
    # 不相干的产线会被一起拖到漏干、或者退成没料）；fed_blocked 的料不点名准入口（唯一缺料的口也被藏住）。
    # probe 1 只在有出料堵死、开工率 0 的设备时跑；probe 2 只在主分析算背压、下游需求上限时有堵死型的口挡掉过 > 0 的量
    # （dead_cut）时跑，不然和 probe 1 一样。都没有的据点（皇帝产线三个据点、景玉谷2号）都不跑。
    # 跑完把外层的结果（dem_cap、dem_why、phi、pt_state）放回，主分析的内层从头再跑一遍：内层写的状态全部按主分析重建，
    # 不用逐个放回（第三轮逐个放回，漏放一个测试锁不住）

    def probe(mode) -> tuple[dict, dict]:
        """跑一遍 probe（mode 1 / 2）→ (每台到手, 开了限制通过数量的口 → 送到它上面的物品)。外层的结果会被改掉，调用方负责放回。
        轮数上限和主分析一样，跑满照用最后一轮：断料链 20 台起 shut 多记、21 台起链尾判成间歇（已知）"""
        probe_mode[0] = mode
        dem_cap.clear()
        dem_why.clear()
        phi.update(phi0)
        pt_state.update({k: dict(pt_fresh) for k in pt_state})
        settle({"outer": 0, "inner": [], "hit_cap": False})
        inner(INNER_MAX)
        _pt_ask()
        probe_mode[0] = 0
        return {i: dict(v) for i, v in sup.items()}, {k: set(v) for k, v in gate_shut.items()}

    modes = [m for m, on in ((1, any(c <= EPS and dev_rate[i] <= EPS for i, c in out_cap.items())), (2, dead_cut[0])) if on] if PROBE else []
    probe_sups, probe_shut = [], defaultdict(set)
    if modes:
        kept = ({i: list(v) for i, v in dem_cap.items()}, dict(dem_why), dict(phi), {k: dict(v) for k, v in pt_state.items()})
        for m in modes:
            ps, sh = probe(m)
            probe_sups.append(ps)
            for k, v in sh.items():
                probe_shut[k] |= v
        for c, s in zip((dem_cap, dem_why, phi, pt_state), kept):
            c.clear()
            c.update(s)
        n, ok = inner(INNER_MAX)
    iters["inner"].append(n)
    if iters["hit_cap"] or not ok:
        notes.append({"kind": "迭代跑满", "msg": f"开工率迭代跑满还在变（各趟轮数 {iters['inner']}），结果取的是最后一轮"})
    if moved:
        notes.append({"kind": "池子出料上限没收敛", "msg": f"并行池子按下游需求定出料上限跑了 {iters['outer']} 轮还在变，用的是最后一轮的上限"})
    if pt_mv:
        ks = [f"{devices[i]['name']}@{devices[i]['x']},{devices[i]['y']}" for i, _x in pt_mv]
        notes.append({"kind": "出口转送没收敛", "keys": sorted(set(ks)),
                      "msg": "、".join(f"{devices[i]['name']} ({devices[i]['x']},{devices[i]['y']}) 的{x}" for i, x in pt_mv)
                             + f"从池子出口转给下游几成，定了 {iters['outer']} 轮还在变，用的是最后一轮"})
    iters["hit_cap"] |= not ok or moved or bool(pt_mv)

    def _par_record(i, need, state, lim, feeds, on_backup, basis):
        """并行池子的记录：开工率 = 各条按满速用量加权平均；依据前两条「出料受限：」「并行配方：」（插在 basis 前面）。
        → (rate, state, limit_by, channels, need_capped)。need_capped = 按每条出料上限算的用量，run_base 拿它判「卡在」：
        武陵城反应池 (1357,1332) 两条都被下游灌装机缺蓝铁瓶限到 50%，清水到手 30 正好够，不能报「卡在清水（30/60）」。"""
        ts, n = chan_rate[i], len(par[i])
        caps = [ecap(i, c) for c in range(n)]
        w = [sum(nd.values()) for nd in cneed[i]]
        rate = sum(t * x for t, x in zip(ts, w)) / sum(w) if sum(w) > EPS else 0.0
        capped = [c for c in range(n) if caps[c] < 1.0 - 1e-3 and ts[c] >= caps[c] - 1e-3]
        # 每条都贴着自己的出料上限：是出料受限，不是缺原料（limit_by 不写原料；全被截到 0 也写间歇，不写没料）。
        # 已知限制：全被截到 0、原料其实也没有的池子也写间歇。不要拿 probe 改它的状态：池子吸多少料跟着它自己的出料上限走，
        # probe 里分不清「没来料」和「出不去所以没分给它」（有料的会判成没料、列假「卡在」），状态一变 run_base 还会多跑主路堵死
        # 那一遍、多出开工率（最后一轮复审 2026-11-22 试过，当天撤回）。要分清得先改池子怎么吸料
        all_capped = bool(capped) and all(ts[c] >= caps[c] - 1e-3 for c in range(n))
        if feeds:
            if min(ts) >= 0.995:
                state = "满速"
            elif max(ts) <= EPS and not all_capped:
                state = "溢流位/平时空闲" if on_backup else "没料"
            else:
                state = "间歇"
        if all_capped:
            lim = None
        need_capped = {k: round(sum(nd.get(k, 0.0) * caps[c] for c, nd in enumerate(cneed[i])), 3) for k in need}
        sh = [k for k in need if k in shared_of[i]]
        short_sh = [k for k in sh if sup[i].get(k, 0.0) + 1e-3 < need_capped[k]]
        chk = PM.check(devices[i]["name"], [m for c in par[i] for m in c["members"]])
        basis.insert(0, "并行配方（官方：池子能同时跑几条配方，各自 2 秒一份）："
                     + "；".join(f"{c['recipe']} 跑 {ts[k]:.0%}" + (f"（{'、'.join(c['members'])} 首尾相接，按一条净配方算）"
                                                                    if len(c["members"]) > 1 else "") for k, c in enumerate(par[i]))
                     + ("；共用的" + "、".join(f"{k} 到手 {sup[i].get(k, 0.0):g}/满速要 {need[k]:g}"
                                             + (f"（按出料上限只要 {need_capped[k]:g}）" if need_capped[k] < need[k] - 1e-3 else "") for k in sh)
                        + ("，不够时按各条用量比例分（推断）" if short_sh else "") if sh else "")
                     + f"（{par_note.get(i, '')}）" + "".join(f"；{x}" for x in chk["warn"]))
        named = [set() for _ in range(n)]  # 每条点了名的下游设备（run_base 判它们「卡在」时不再反过来列这条的产物）
        if capped:
            ws = []
            for item, (tot, full, why, keys) in dem_why.get(i, {}).items():
                if tot >= full - 1e-3:
                    continue
                more = f" 等 {len(why)} 台" if len(why) > 3 else ""
                ws.append(f"{item} 下游实际吃得下 {tot:g}/满速产 {full:g}" + (f"（{'；'.join(why[:3])}{more}）" if why else ""))
                for c in capped:
                    if item in cout[i][c]:
                        named[c] |= set(keys)
            basis.insert(0, "出料受限：" + "；".join(f"{par[i][c]['recipe']} 最多跑 {caps[c]:.0%}" for c in capped)
                         + ("。" + "；".join(ws) if ws else "（背压，主路顶住）")
                         + "（推断：游戏里下游堵 → 池内这种产物攒满 → 那条配方停，按稳态折成只跑下游吃得下的那几成）")
        chans = [{"recipe": c["recipe"], "members": c["members"], "src": c["src"], "rate": round(ts[k], 3), "out_cap": round(caps[k], 3),
                  "struct_cap": round(struct_cap[i][k], 3) if i in struct_cap else None,
                  "demand_cap": round(dem_cap[i][k], 3) if i in dem_cap else None, "named": sorted(named[k])}
                 for k, c in enumerate(par[i])]
        return rate, state, lim, chans, need_capped

    # 池子配方从哪来（行上的 pool_src）、候选组合（行上的 candidates）、「池子配方」待确认（审计第 3 项，2026-11-19）
    panel_keys = {(q["name"], q["x"], q["y"]) for q in recipes if "→" in (q.get("recipe") or "") or (q.get("recipe") or "").startswith("池产物")}
    guessed = {(f["device"], f["x"], f["y"]) for f in fixes if "相邻" in f.get("how", "") or "why" in f}

    def pool_src_of(i):
        """作者 / 面板 / 邻居推断 / 照抄同类（配方表里没有这台，按同类借的：皇帝产线武陵城扩容反应池 (1768,1174)）/ 不明（配方未知）。"""
        k = (devices[i]["name"], devices[i]["x"], devices[i]["y"])
        if i in by_author:
            return "作者"
        if i in par:
            return par[i][0]["src"]
        if i in unknown:
            return "不明"
        return "邻居推断" if k in guessed else "面板" if k in panel_keys else "照抄同类"
    raw_pool_fix = fx.get("pool_recipes") or {}
    known = {j for j in range(len(devices)) if "→" in rec[j][0] and j not in unknown and j not in pool_unused}
    pool_cand = {}
    for i, d in enumerate(devices):
        if d["name"] not in POOLS:
            continue
        key = f"{d['name']}@{d['x']},{d['y']}"
        card = i in unknown or key in raw_pool_fix  # 作者回答过的卡片留着（标 applied），能撤销、能改
        if not (card or pool_src_of(i) in ("邻居推断", "照抄同类")):
            continue
        links = _pool_links(i, G, devices, dev_canvas, recipes_of, known, pitem, gate)
        pool_cand[i] = _pool_candidates(d["name"], links)
        if not card:
            continue

        def side(dirn):
            return [{"item": L["sup" if dirn == "in" else "dem"][0] if len(L["sup" if dirn == "in" else "dem"]) == 1 else None,
                     "items": L["sup" if dirn == "in" else "dem"], "layer": L["layer"], "unclear": L["wild_in"] or L["wild_out"],
                     "from" if dirn == "in" else "to": "、".join(L["ends"][:3]) or "（没追到设备）", "at": L["at"]}
                    for L in links if L["dir"] == dirn]
        ans = raw_pool_fix.get(key)
        # 坐标和设计分析表、依据里的点名一样四舍五入（2026-11-20 核对安装包：卡片写 [2586, 217]、点名写 (2587,218)，作者按坐标对不上）
        cc = [round(v) for v in dev_canvas[i]]
        pending.append({"kind": "池子配方", "key": key, "device": f"{d['name']}[{cc[0]}, {cc[1]}]",
                        "c": cc, "recipe": rec[i][0], "slots": PM.SLOTS[d["name"]],
                        "msg": (f"{d['name']}机身没有产物图标，读不出它在跑哪几条配方。池子能同时跑几条（官方），"
                                f"按进出的连线勾上这台在跑的配方（物品种类不超过 {PM.SLOTS[d['name']]} 格），或者说这台没在用。")
                        if i in unknown else f"{d['name']}的配方是你在这里指定的；要改可以重新勾，或者撤销。",
                        "ins": side("in"), "outs": side("out"), "combos": pool_cand[i], "answer": ans,
                        "applied": i in pool_fix})
    # 「暗管来料」待确认（0.10.13 功能二）：外接、漏配的暗管出口，有出边或作者已经标了输入端的，每台一张。
    # 回答存成标在这台暗管上的输入端标记（项目 inputs.json，和「输入端」页同一份），不加 design.json 顶层键
    for i, t in sorted(tun_out.items()):
        if t["cls"] in TUN_CARD and (G.out[("d", i)] or i in tun_mark):
            links = _pool_links(i, G, devices, dev_canvas, recipes_of, known, pitem, gate)
            pending.append(_tunnel_card(i, t, tun_mark.get(i), devices, dev_canvas, G, nodes, gate, links, tun_here.get(i, [])))
    # 绑标记时的说明（一台标了两个、几口合计、标在配上对的暗管上）：这台有卡的挂到卡上给作者看；没有卡的由 summary 放进提醒
    tun_cards = {p["key"]: p for p in pending if p.get("kind") == "暗管来料"}
    for n in tun_notes:
        if n.get("outlet") in tun_cards:
            tun_cards[n["outlet"]]["extra"].append(n["msg"])
            n["on_card"] = True

    _pt_ask()
    # 开了限制通过数量的口挡的料：主分析、probe 两遍的并起来，分析跑完先取下来（后面准入口段、准入口表还会调 absorb，ask 是关的，不记）
    shut = {k: sorted(v) for k in set(gate_shut) | set(probe_shut) if (v := gate_shut.get(k, set()) | probe_shut.get(k, set()))}

    # 每台设备的记录
    rows = []
    for i, d in enumerate(devices):
        rc, inferred = rec[i]
        need, fuel = dev_need(i)
        if not need:
            if i in pool_unused or i in unknown:  # 先判「没在用」：作者说没在用的池子配方也是空的，不能算成配方未知
                rows.append(_blank_row(i, d, rc, dev_canvas, G, unused=i in pool_unused))
            continue
        u = ("d", i)
        feeds = G.inn[u]
        got = {k: round(sup[i].get(k, 0.0), 3) for k in need}
        r = dev_rate[i]
        lim = min(need, key=lambda k: sup[i].get(k, 0.0) / need[k]) if need else None
        basis = []
        for k in need:
            for pth in via[i].get(k, [])[:3]:
                basis.append(" → ".join(pth))
        on_backup = any(any("备路" in s for s in p) for k in via[i] for p in via[i][k]) and \
            not any(not any("备路" in s for s in p) for k in via[i] for p in via[i][k])
        if not on_backup and feeds and all(v[0] == "n" and v[1] in backup_merge for v, _ in feeds):
            on_backup = True  # 进料全来自 1 进 1 出汇流器（备路）：主路吃得完时一点都拿不到（武陵城精炼炉正下方的配件机）
        if not feeds:
            state = "没接上（物流图里没有进料连接）"
        elif r >= 0.995:
            state = "满速"
        elif r <= EPS:
            state = "溢流位/平时空闲" if on_backup else "没料"
        else:
            state = "间歇"
        zero = [k for k in need if abs(sup[i].get(k, 0.0) - need[k]) < 1e-3 and need[k] > 0]
        chans = need_capped = None
        if i in par:
            r, state, lim, chans, need_capped = _par_record(i, need, state, lim, feeds, on_backup, basis)
        if i in alt:
            ar, anote = alt[i]
            agot = {k: round(sup[i].get(k, 0.0), 3) for k in inputs_of(ar) if k not in need}
            aneed = {k: per_min(ar, k) for k in inputs_of(ar)}
            shared = [k for k in need if k in aneed]  # 两种配方共用的原料（池子的清水）只算一次

            def _frac(nd):
                own = [k for k in nd if k not in shared and nd[k] > 0]
                return min((sup[i].get(k, 0.0) / nd[k] for k in own), default=1.0)
            f1, f2 = _frac(need), _frac(aneed)
            fs = min((sup[i].get(k, 0.0) / need[k] for k in shared if need[k] > 0), default=1.0)
            share = min(1.0, f1 + f2, fs)
            tot = sum(got.values()) + sum(agot.values())
            cap = sum(need.values())
            basis.insert(0, f"交替配方：{rc} / {ar}；各自的原料够跑 {f1:.0%} + {f2:.0%}"
                            + (f"，共用的{'、'.join(shared)}够跑 {fs:.0%}" if shared else "")
                            + f" → 约 {share:.0%} 时间在转（{anote}）")
            for k in agot:
                for pth in via[i].get(k, [])[:2]:
                    basis.append("交替 " + " → ".join(pth))
            r = share
            state = "满速" if r >= 0.995 else ("间歇" if r > EPS else state)
        fed_blk = []  # 到手 0 只是因为出不去的原料（见下面）：run_base 的「卡在：」不再列它们
        if i in out_cap and r >= out_cap[i] - 1e-3:  # 开工率正好卡在背压上限上
            basis.insert(0, f"出料受限：产物下游最多吃得下满速的 {out_cap[i]:.0%}（背压，主路顶住）")
            # 产物一点都出不去（下游准入口开了限制通过数量、限速 0、物品不对），每种原料又都在 probe 的哪一遍里到手 > 0：
            # 是出料堵住，不是缺料，和并行池子全被截到 0 一样写间歇（审查 2026-11-21：只能往限制通过数量口出料的粉碎机原料吃饱了，
            # 原来写「没料」、summary 也算进没料）。有一种原料不堵也到不了的照旧写没料：游戏里没原料一件都做不出来，显示的是缺料
            # （复审 2026-11-21：原来不看原料，上游真缺料的也写成间歇）
            # 每种原料各看任一遍：A 只在 probe 1 有、B 只在 probe 2 有也算有，会有假间歇（已知）。试过要同一遍都有，
            # 把两种料本来就到手的也判成没料，撤回（第三轮核实 2026-11-22）
            if state == "满速" or (state == "没料" and out_cap[i] <= EPS
                                  and all(any(ps.get(i, {}).get(k, 0.0) > EPS for ps in probe_sups) for k, v in need.items() if v > 0)):
                if state == "没料":
                    held_k = [k for k, v in need.items() if v > 0 and sup[i].get(k, 0.0) <= EPS]
                    if held_k:  # 到手是 0、只是因为出不去没分给它的原料：说一句，不然「能送到 0」和间歇对不上
                        basis.insert(1, f"{'、'.join(held_k)}有来料，只是这台产物出不去，没分给它（出料堵住，不算缺料）")
                        fed_blk = held_k
                state = "间歇"
        fuel_got = _fuel_got(sup[i], rc) if fuel else None
        if fuel and fuel_got + EPS < fuel:
            state += f"（送进来的非配方气 {round(fuel_got, 2):g}/分，低于燃料最低 {fuel:g}：可能罢工，燃料种类未确认）"  # 2.33333 → 2.33
        if fuel:
            for k, v in sup[i].items():
                if _is_fuel(k, rc):
                    for pth in via[i].get(k, [])[:2]:
                        basis.append("燃料 " + " → ".join(pth))
        via_inf = any(INFER_TAG in s_ for k in via[i] for p_ in via[i][k] for s_ in p_)
        if via_inf:
            basis.append("来料路径里有推断出来的连线（标〔推断连线〕的那段），以实际为准")
        envr = env_required(d["name"], rc)
        if envr:
            basis.append("这条配方要稳定环境（环境气体覆盖）；普通状态下是另一条配方")
        rows.append({"device": d["name"], "x": d["x"], "y": d["y"], "c": [round(v) for v in dev_canvas[i]], "recipe": rc, "env_required": envr, "via_inferred": via_inf,
                     "recipe_inferred": inferred, "need": need, "got": got, "rate": round(r, 3), "limit_by": lim,
                     "state": state, "zero_margin": zero, "feeds": len(feeds), "basis": basis[:8],
                     "fuel": {"need": fuel, "got": round(fuel_got, 3)} if fuel else None,
                     "loop": any(v[0] == "n" and v[1] in loop_nodes for v, _ in feeds)})
        if chans:  # 只有并行池子有这两个键：别的设备行的字段不变
            rows[-1].update(channels=chans, need_capped=need_capped)
        if fed_blk:  # 只有出料堵死、原料有来料的设备有这个键（0.10.13 A2）
            rows[-1]["fed_blocked"] = fed_blk
        if i in pt:  # 只有作者说了出口送原料的池子有 pass_out（0.10.13 功能三）；依据会进导入方的巡检说明，写「作者说的」
            po = []
            for x, layer in pt[i].items():
                a, f_ = arrive.get((i, x), 0.0), fwd.get((i, x), 0.0)
                bk = ptgot[i].get((i, x), 0.0)  # 出口经分流器绕回自己的（到手里含这份）：只有这种连法才有 back
                po.append({"item": x, "layer": layer, "arrive": round(a, 3), "out": round(f_, 3), "own": round(a - f_, 3),
                           "share": round(phi[(i, x)], 4)})
                moved = f"从出口转给下游 {f_:.3g}"
                if bk > 1e-3:
                    po[-1]["back"] = round(bk, 3)
                    moved = f"从出口转出 {f_:.3g}（绕回这台自己 {bk:.3g}、给下游 {f_ - bk:.3g}）"
                rows[-1]["basis"].insert(0, f"{'带' if layer == 'belt' else '管'}出口送{x}（作者说的）：{x}到手 {a:.3g}/分，"
                                            f"{moved}，池子自己用 {a - f_:.3g}（{PT_RULE}）")
            rows[-1]["pass_out"] = po
    dev_at = {(d["name"], d["x"], d["y"]): i for i, d in enumerate(devices)}
    for r in rows:  # 只有池子行有 pool_src；算了候选的池子行带 candidates
        i = dev_at[(r["device"], r["x"], r["y"])]
        if r["device"] in POOLS:
            r["pool_src"] = pool_src_of(i)
            if i in pool_cand:
                r["candidates"] = pool_cand[i]
    _name_unknown_upstream(rows, G, devices, dev_canvas, unknown, buf_ids)
    _name_count_gates(rows, G, devices, nodes, gate, gate_held, shut, once, buf_ids)

    # 准入口 ↔ 下游需求
    gates = []
    for nid, g in gate.items():
        if g.get("limit") is None and not g.get("count") and not g.get("input"):
            continue
        dem = absorb(("n", nid), g.get("item") or None)
        dem = None if dem == float("inf") else round(dem, 3)
        ent = {"gate": nid, "c": nodes[nid]["c"], "item": g["item"], "item_how": g.get("item_how"), "limit": g.get("limit"), "count": g.get("count"),
               "input_no": (g.get("input") or {}).get("no"), "author_rate": (g.get("input") or {}).get("rate"), "downstream": dem}
        msg = []
        if g.get("count"):
            c = g["count"]
            if c.get("limit") is None:  # 游戏里读到开着、数量没读出来：gate_pass 不挡，照实写
                msg.append("限制通过数量开着，但没读到数量：设计分析没按它挡，照常放行")
            else:
                # 作者填的和游戏里读的一样按一次性算（0.10.13 A2）：放满就不再放行，长期按不送料算（gate_pass 的 once）
                tags = (["作者填"] if g.get("src") == "作者" else []) + ([f"累计 {c['passed']:g}"] if c.get("passed") is not None else [])
                msg.append(f"限制通过数量 {c['limit']:g}" + (f"（{'，'.join(tags)}）" if tags else "") + "：放满后不再放行，按一次性备料算")
        if g.get("limit") is not None and once(g):
            # 限速和限制通过数量都开着：按限制通过数量不放行，限速管不着；dem 从口本身算已经是 0，下面那几句会写反（审查 2026-11-21）
            msg.append(f"限速 {g['limit']:g} 不起作用：限制通过数量已经按不放行算")
        elif g.get("limit") is not None and dem is not None:
            if dem <= EPS:
                msg.append("下游没有用这种物品的设备（或连接没追到）")
            elif g["limit"] + EPS >= dem:
                msg.append(f"限速 {g['limit']:g} ≥ 下游满速需求 {dem:g}：不卡" + ("（正好相等，设计如此）" if abs(g["limit"] - dem) < 1e-3 else ""))
            else:
                msg.append(f"限速 {g['limit']:g} < 下游满速需求 {dem:g}：限量生产，下游只能跑 {g['limit'] / dem:.0%}")
        ar = ent["author_rate"]
        try:
            ar = float(ar) if ar not in (None, "") else None
        except ValueError:
            ar = None
        if ar is not None and g.get("limit") is not None and g["limit"] + EPS < ar:
            msg.append(f"作者填的流速 {ar:g}/分，但限速只有 {g['limit']:g}：准入口可能被改过")
        ent["msg"] = msg
        gates.append(ent)
    ports = [{"device": devices[i]["name"], "c": [round(v) for v in dev_canvas[i]], "disk": it.get("disk"), "item": it["item"],
              "sure": it["sure"], "how": it.get("how"), "score": it["score"], "role": port_role(i)[0]} for i, it in pitem.items()]
    inferred = []
    for u in list(G.out):
        for v, lk in G.out[u]:
            if lk.get("inferred"):
                nm = lambda x: (devices[x[1]]["name"] + str([round(t) for t in dev_canvas[x[1]]])) if x[0] == "d" else f"{nodes[x[1]]['type']}{nodes[x[1]]['c']}"  # noqa: E731
                inferred.append({"from": nm(u), "to": nm(v), "layer": lk.get("layer"), "basis": [b for b in lk.get("basis") or []][-2:],
                                 "path": lk.get("path", [])[:1] + lk.get("path", [])[-1:]})
    n_tun = sum(1 for d in devices if "暗管入口" in d["name"] or "暗管出口" in d["name"])
    miss = tunnel_files_missing(bd) if n_tun else []
    if miss:
        # 2026-11-19：别人的武陵城反馈包里没有 bp/<蓝图>/links.json，本地重算暗管配对是 0 对，暗管出口全按「物品不明、按缺口分」算，
        # 结论可能偏乐观，界面上却看不出来。只加一条说明，不改任何设备的结果（皇帝产线三个据点有暗管的蓝图文件都齐、配对非空，不会出这条）。
        # 按蓝图逐张看、点名是哪张：只看整个据点有没有配对，一张蓝图配上了、另一张缺 links.json 就提示不到
        notes.append({"kind": "暗管配对缺失", "msg": f"{'、'.join(miss)} 没读到暗管配对（这个据点底账里有 {n_tun} 个暗管入口 / 出口）——"
                                                  "请重读这几张蓝图（如果这份数据是从反馈包导入的，可能是包里没带蓝图数据）；经过暗管的来料现在是按缺口估的"})
    elif n_tun and not tpairs:
        # 文件都齐但一对都没有：暗管全是外接的（read_blueprints 不点、不写 links.json，issue #1 武陵城那种外接暗管入口），
        # 或者是玩家在蓝图外自己摆的。重读也配不出来，不叫用户重读、不进 summary 的 warnings（2026-11-19）
        notes.append({"kind": "暗管未配对", "msg": f"这个据点底账里的 {n_tun} 个暗管入口 / 出口没有配对（外接的、蓝图外自己摆的暗管不配对），"
                                                "经过它们的来料按缺口估"})
    out = {"devices": rows, "gates": gates, "ports": ports, "recipe_fixes": fixes, "tunnels": len(tpairs),
           "notes": notes + plant_notes + pool_notes + gate_notes + tun_notes + pt_notes, "issues": flow.get("issues", []),
           "inferred_links": inferred, "reread": reread, "pending": pending, "iters": iters,
           "gate_view": _gate_view(bd, G, devices, dev_canvas, nodes, gate, gate_flow, gate_held, absorb, ag_stale, gate_limits, buf_ids,
                                   shut)}
    if phi:  # 池子出口转送原料的比例（0.10.13 功能三）：run_base 第二、三遍用；只有作者说了出口送原料时才有这个键
        out["pass_split"] = {f"{devices[i]['name']}@{devices[i]['x']},{devices[i]['y']}|{x}": round(v, 6) for (i, x), v in phi.items()}
    analyze.last_graph = (G, devices, nodes, dict(sup), buf_ids)  # 调试用：最近一次的图
    return out


def _first_devs(G, u, side, devices, dev_canvas, hops=8, most=3) -> list[dict]:
    """从节点 u 顺着连线往上游（side="in"）/ 下游（"out"）找最近的设备，穿过物流件最多 hops 跳 → [{name, c, x, y}]（最多 most 台）。
    准入口表的「从哪来 → 到哪去」、小图上的「进」「出」用。"""
    found, seen, front = [], {u}, [u]
    for _ in range(hops):
        nxt = []
        for x in front:
            for v, _lk in (G.inn[x] if side == "in" else G.out[x]):
                if v in seen:
                    continue
                seen.add(v)
                if v[0] == "d":
                    d = devices[v[1]]
                    found.append({"name": d["name"], "c": [round(t) for t in dev_canvas[v[1]]], "x": d["x"], "y": d["y"]})
                else:
                    nxt.append(v)
        if len(found) >= most or not nxt:
            break
        front = nxt
    return found[:most]


UNK_NAMES = {UNK_BELT: "没看到的物品", UNK_PIPE: "没看到的液体"}  # 准入口表里过口的物品没看到时的写法
OPEN_ENDS = ("仓库存货口", "暗管入口", "多口暗管入口")  # 料送进去就不知道用多少的地方（仓库、没配对的暗管送到基地外面）


def _down_open(G, u, devices, buf_ids) -> bool:
    """从节点 u 往下游有没有一路「看不出要多少」：绕回上游（回环）、进仓库存货口或没配对的暗管入口、走到没有出口的物流件。
    absorb 在这几种地方给的是 0 或被带子 / 管子上限截住的 inf，看着像个正常的数（准入口表的「下游满速要」照它就写成 0 / 30）。
    缓冲（储存箱、储气罐）、配上对的暗管照 absorb 当中转往下走。走过、往下都看得出的节点记下来不再走（回环从哪进来都能在它下面找到）。"""
    known = set()

    def walk(x, path):
        for v, _lk in G.out[x]:
            if v in path:
                return True
            if v in known:
                continue
            if v[0] == "d" and v[1] not in buf_ids:
                if devices[v[1]]["name"] in OPEN_ENDS:
                    return True
                continue
            if not G.out[v] or walk(v, path | {v}):
                return True
            known.add(v)
        return False
    return walk(u, {u})


def _gate_view(bd, G, devices, dev_canvas, nodes, gate, gate_flow, gate_held, absorb, stale, gate_limits, buf_ids=frozenset(),
               shut=None) -> dict:
    """设计分析的副产物「准入口表」：run_base 从结果里拿出来写成 base/<据点>/gates_view.json，不进 design.json。
    gates 每个准入口一行：
      key        作者存设置用的键：作者填过的是那条记录的键（p_bp 漂了几 px 也还是它，改了不会多出一条），
                 没填过的按 logistics.json 里这个口的 bp、p_bp 现算（gate_key）；没有位置数据时 None
      gate、c    物流节点号、拼合图坐标；bp、p_bp、kind（物品 / 管道 / 空）来自 logistics.json
      item、item_how
      flow、held {物品: 每分钟}：推料最后一轮过口的量、被限速 / 限制通过数量挡下的量（只放 > 0 的）
      shut       [物品]：开了限制通过数量的口，不算出料堵死时（正的背压照算）真有料在动着送到它上面的物品（不管量）；别的口 []。
                 是主分析最后一轮的 gate_shut 和两遍 probe 的并集（probe 1「出料堵死的设备当它吃得下」、probe 2「整条线都不算出口堵塞」；
                 有出料堵死的设备才跑 probe 1，堵死型的口压住过上游才跑 probe 2）。
                 推料推到口上的都算（单路推料不看下游吃不吃得下，和 held 一样）；分料、几口合计、池子出口转送按下游吃得下的记；
                 背压试算、去向过滤只是问一问，不记；上游自己没料的没有。
                 上游是生产设备、池子时背压先把它压成 0，flow、held 都是空的，靠它知道这个口挡的是什么料（0.10.13 A2 审查、复审）
      downstream 下游满速要多少：从这个口的出边往下算，不含它自己的限速；有一路看不出（_down_open：绕回上游、去仓库、
                 没配对的暗管入口、没有出口的物流件）写 None；没有出边 0。
                 按口上的物品算；口上没认出物品（item 是 None）、推料时只过了（或被挡下、shut 里）一种料的按那种料算，
                 几种或一种都没有的按设备全部原料算
      limit、count  现在生效的限制速度 / 限制通过数量（None = 没开）
      src        "作者" / "游戏"（档案里读到过这个口，开没开都算；读失败的行不算）/ None（没说，按不限速）
      read       档案里游戏读到的原样 {limit_on, limit, count_on, count_limit, passed}；没读过 None（作者改过也照样给）
      read_error 档案里这个口读失败了（read_area 记的原因，比如画面里没找到图标）；没有 None。读失败的不当读数
      input_no、input_rate  输入端编号、作者在输入端页填的流速（不是输入端都是 None）
      from、to   上游 / 下游最近的设备（穿过物流件最多 8 跳，最多 3 台）[{name, c, x, y}]
    stale：author_gates 对不上的记录；read_time：档案读数的时间（没有档案读数 None）。"""
    spots = _gate_spots(bd, nodes)
    rows = []
    for n in nodes:
        if n["type"] != "准入口":
            continue
        nid, g = n["id"], gate[n["id"]]
        o, u, m, rd = spots.get(nid, {}), ("n", n["id"]), g.get("input") or {}, g.get("read")
        key = (g.get("author") or {}).get("key") or (gate_key(o["bp"], o["p_bp"]) if o.get("bp") and o.get("p_bp") is not None else None)
        it = g.get("item") or None
        sh = (shut or {}).get(nid, [])
        seen_it = {k for d in (gate_flow.get(nid, {}), gate_held.get(nid, {})) for k, v in d.items() if v > EPS and k not in UNKNOWN} | set(sh)
        dit = it or (next(iter(seen_it)) if len(seen_it) == 1 else None)  # 口上没认出物品、只过了一种料：按这种料算下游要多少
        dem = None if _down_open(G, u, devices, buf_ids) else sum(min(_cap(lk), absorb(v, dit, (u,))) for v, lk in G.out[u])
        rows.append({"key": key, "gate": nid, "c": n["c"], "bp": o.get("bp"), "p_bp": o.get("p_bp"), "kind": n.get("kind") or o.get("kind"),
                     "item": it, "item_how": g.get("item_how"),
                     "flow": {UNK_NAMES.get(k, k): round(v, 3) for k, v in gate_flow.get(nid, {}).items() if v > EPS},
                     "held": {UNK_NAMES.get(k, k): round(v, 3) for k, v in gate_held.get(nid, {}).items() if v > EPS},
                     "shut": list(sh),
                     "downstream": None if dem is None or dem == float("inf") else round(dem, 3),
                     "limit": g.get("limit"), "count": (g.get("count") or {}).get("limit"), "src": g.get("src"),
                     "read": {k: rd.get(k) for k in ("limit_on", "limit", "count_on", "count_limit", "passed")} if rd else None,
                     "read_error": g.get("read_error"), "input_no": m.get("no"), "input_rate": m.get("rate"),
                     "from": _first_devs(G, u, "in", devices, dev_canvas), "to": _first_devs(G, u, "out", devices, dev_canvas)})
    return {"gates": rows, "stale": stale, "read_time": (gate_limits or {}).get("time")}


OUTLETS = ("暗管出口", "多口暗管出口", "仓库取货口")
TOUCH_PRODUCERS = True


def _port_border(gray, c, d) -> bool:
    """物流件 c 的 d 边外面紧挨着（2–14 px）一条设备的粗黑边框：设备放物品口的边（精炼炉上下两边、转过的塑形机左右两边）
    画成 2–3 px 的深黑线，其余边只是细灰线（中间可能横穿一根管子）。横向 5 条线至少 4 条在同一距离碰到才算。"""
    vx, vy = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}[d]
    ux, uy = -vy, vx
    H, W = gray.shape
    hits = []
    for lo in (-8, -4, 0, 4, 8):
        prof = []
        for t in range(-2, 18):
            x, y = int(round(c[0] + vx * (V.CELL / 2 + t) + ux * lo)), int(round(c[1] + vy * (V.CELL / 2 + t) + uy * lo))
            prof.append(int(gray[y, x]) if 0 <= x < W and 0 <= y < H else 255)
        dk = [v < 90 for v in prof]
        i = 0
        while i < len(prof) and (dk[i] or i < 2):  # 跳过物流件自己的外框
            i += 1
        while i < len(prof) and not dk[i]:
            i += 1
        if i >= len(prof):
            continue
        j = i
        while j + 1 < len(prof) and dk[j + 1]:
            j += 1
        if j - i + 1 >= 2 and min(prof[i:j + 1]) <= 60 and i - 2 <= 14:
            hits.append(i)
    return len(hits) >= 4 and max(hits) - min(hits) <= 2


def _glyph_dirs(G, flow, devices, dev_canvas, bd) -> list:
    """设备 ↔ 设备的直连带方向没定（没小三角票、按配方两边都说得通）：看两头接的那条边，口格折角说哪边是出口就从哪边来。
    只补方向没定的，不改已经定了的（小三角 / 仓库口 / 物流件定的）。"""
    import cv2 as _cv
    from .pipenet import _rects, device_rect
    canvas = V.rd(bd / "composite.png")
    gray = _cv.cvtColor(canvas, _cv.COLOR_BGR2GRAY)
    hsv = _cv.cvtColor(canvas, _cv.COLOR_BGR2HSV)
    dark_thin = ((gray < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    rects = _rects(canvas)
    cache = {}

    def side(i, edge):
        if i not in cache:
            c = dev_canvas[i]
            r = device_rect(dark_thin, c[0], c[1], rects)
            cache[i] = glyph_sides(gray, r) if r is not None else {}
        return cache[i].get(edge)
    back = {"N": "S", "S": "N", "E": "W", "W": "E"}
    added = []
    for lk in flow["links"]:
        if not lk.get("free") or lk.get("dir") or lk["layer"] != "belt" or not lk.get("a_dev") or not lk.get("device"):
            continue
        a, b = G._dev(lk["a_dev"], dev_canvas), G._dev(lk["device"], dev_canvas)
        if a is None or b is None or a == b or any(v == b for v, _ in G.out[a]) or any(v == a for v, _ in G.out[b]):
            continue
        if devices[a[1]]["name"] in PORT_NAMES or devices[b[1]]["name"] in PORT_NAMES:
            continue
        sa = side(a[1], back[lk["a_dev_side"]]) if lk.get("a_dev_side") else None
        sb = side(b[1], back[lk["dev_side"]]) if lk.get("dev_side") else None
        if (sa == "out" or sb == "in") and not (sa == "in" or sb == "out"):
            u, v = a, b
        elif (sa == "in" or sb == "out") and not (sa == "out" or sb == "in"):
            u, v = b, a
        else:
            continue
        nl = dict(lk, dir="a->b", basis=["按两头设备口格的折角定方向"])
        G.out[u].append((v, nl)); G.inn[v].append((u, nl))
        added.append((devices[u[1]]["name"], devices[v[1]]["name"]))
    return added


GATE_SNAP = 15  # 作者存的准入口（蓝图预览里的坐标）离现在的准入口多少 px 以内算同一个（和输入端 inputs.SNAP 一样）


def gate_key(bp: str, p_bp) -> str:
    """作者填准入口限速时记录的键：蓝图名@蓝图预览里的坐标（取整）。p_bp 是读蓝图时定的，重扫底账、增减蓝图、
    重新拼合、蓝图在基地里转方向都不变（拼合图坐标会变）。"""
    return f"{bp}@{round(p_bp[0])},{round(p_bp[1])}"


def _gate_spots(bd: Path, nodes: list[dict]) -> dict:
    """物流图里每个准入口的节点号 → logistics.json 里同一个口那一行（bp、p_bp、kind），按拼合图坐标 p 对。没有这个文件时是空的。"""
    try:
        lg = json.loads((bd / "logistics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    by_p = {tuple(o["p"]): o for o in lg if isinstance(o, dict) and o.get("type") == "准入口" and o.get("p")}
    return {n["id"]: by_p[tuple(n["p"])] for n in nodes if n["type"] == "准入口" and tuple(n["p"]) in by_p}


def _num_or_none(v) -> bool:
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))


def author_gates(bd: Path, nodes: list[dict], fx: dict) -> tuple[dict, list]:
    """作者在「准入口限速」表里填的（fixes.json 这个据点的 gates 段，键见 gate_key）→ ({物流节点号: 记录（多一个 key）}, [对不上的记录])。
    每条记录到 logistics.json 里找：同一张蓝图、p_bp 两个坐标都差 GATE_SNAP 以内、最近的那个准入口。
    对不上的（蓝图不在这个据点了、附近没有准入口、数不对、两条记录对到同一个口时离得远的那条）不参加计算，
    每条是 {key, bp, p_bp, kind, item, limit, count, at, why}，界面「对不上的旧设置」照它列。"""
    want = fx.get("gates") or {}
    if not isinstance(want, dict) or not want:
        return {}, []
    spots = _gate_spots(bd, nodes)
    try:
        bps = set(json.loads((bd / "transform.json").read_text(encoding="utf-8")).get("bps") or {})
    except (OSError, ValueError):
        bps = set()
    bps |= {o.get("bp") for o in spots.values()}
    cand = [(nid, o.get("bp"), o["p_bp"]) for nid, o in spots.items() if o.get("p_bp") is not None]
    tries, stale = [], []

    def lost(k, r, why):
        r = r if isinstance(r, dict) else {}
        stale.append({"key": k, **{f: r.get(f) for f in ("bp", "p_bp", "kind", "item", "limit", "count", "at")}, "why": why})
    for k, r in want.items():
        p = r.get("p_bp") if isinstance(r, dict) else None
        if not isinstance(p, (list, tuple)) or len(p) != 2 or not all(isinstance(t, (int, float)) for t in p) or not r.get("bp"):
            lost(k, r, "记录不完整（没有蓝图名或蓝图里的位置）")
            continue
        if not (_num_or_none(r.get("limit")) and _num_or_none(r.get("count"))):
            lost(k, r, "记录里的限制速度或限制通过数量不是数字")
            continue
        if not spots:
            lost(k, r, "这个据点没有准入口的位置数据，认不出是哪个口")
            continue
        if r.get("bp") not in bps:
            lost(k, r, f"蓝图「{r.get('bp')}」不在这个据点里了")
            continue
        hit = min(((abs(q[0] - p[0]) + abs(q[1] - p[1]), nid) for nid, b, q in cand
                   if b == r.get("bp") and abs(q[0] - p[0]) <= GATE_SNAP and abs(q[1] - p[1]) <= GATE_SNAP), default=None)
        if hit is None:
            lost(k, r, f"蓝图「{r.get('bp')}」里这个位置附近没有准入口（重读蓝图后挪开了？）")
            continue
        tries.append((hit, k, r))
    hits = {}
    for (_d, nid), k, r in sorted(tries, key=lambda t: (t[0][0], t[1])):  # 两条对到同一个口（手改的文件）：近的那条算数
        if nid in hits:
            lost(k, r, f"和蓝图「{r.get('bp')}」里离得更近的另一条设置对到同一个准入口，用的是近的那条")
            continue
        hits[nid] = dict(r, key=k)
    return hits, stale


def load_fixes(base_dir: Path) -> dict:
    """作者在设计分析页「待确认」里做的修正（<项目>/fixes.json 里这个据点的那一段）：
    {"feeds": {设备键: "to:<另一头设备键>" / "none" / "unused"}, "recipes": {设备键: 配方},
     "pool_recipes": {池子的设备键: [原始配方…]（[] = 这台没在用；2026-11-19）},
     "gates": {准入口键（gate_key）: {bp, p_bp, kind, item, limit: 数 | null, count: 整数 | null, at}}（0.10.13：作者填的准入口
     限制速度 / 限制通过数量；两项都是 null = 作者说都没开；没有这条 = 作者没说），
     "pool_outlets": {池子的设备键: {belt: [固体，最多 1 个], pipe: [液体，最多 2 个], at}}（0.10.13 功能三：池子出口送什么；
     没有 belt / pipe 字段 = 那种出口没说，空列表 = 什么都不送；见 _outlet_answers）}。设备键 = 设备名@底账远景坐标（同确认记录）。"""
    pj = base_dir.parent.parent / "fixes.json"
    if not pj.exists():
        return {}
    try:
        return (json.loads(pj.read_text(encoding="utf-8")) or {}).get(base_dir.name) or {}
    except (OSError, ValueError):
        return {}


PT_RULE = "按作者实测的规则：出口和配方从池里同一格取料，下游先拿、池子用剩下的"  # 池子行依据里写规则来源（0.10.13 功能三）


def _outlet_answers(devices: list[dict], fx: dict, rec: dict, pool_fix: dict, pool_unused: set, unknown: set,
                    recipes_of) -> tuple[dict, list]:
    """作者说的池子出口送什么（fixes.json 的 pool_outlets，0.10.13 功能三）→ ({池子: {转送的原料: "belt" / "pipe"}}, 说明)。
    分类在 PM.outlets；这里配上这台在跑的原始配方（作者勾的池子配方；面板、推断来的按净配方拆回原始配方），写带 key 的说明
    （analyze 放在 inner() 外面：inner() 每轮开头清空 notes）。说明会被导入方当巡检提示看，不写「你」，写「作者说」。
    键在现在的底账里找不到池子（底账重扫过，和 pool_recipes、确认记录一起失效）、配方还不知道、作者说没在用：写说明，不静默丢掉。"""
    pt, notes = {}, []
    by_key = {f"{d['name']}@{d['x']},{d['y']}": i for i, d in enumerate(devices) if d["name"] in POOLS}
    for key, ans in (fx.get("pool_outlets") or {}).items():
        if not isinstance(ans, dict):
            continue
        i = by_key.get(key)
        if i is None:
            name, _, xy = key.partition("@")
            notes.append({"kind": "出口回答没用上", "key": key,
                          "msg": f"作者说的{name} ({xy}) 出口送什么，在现在的底账里找不到这台池子（底账重扫过？），没用上"})
            continue
        d = devices[i]
        where = f"{d['name']} ({d['x']},{d['y']})"
        if i in pool_unused or i in unknown or "→" not in rec[i][0]:
            why = "作者说这台没在用" if i in pool_unused else "这台的配方还不知道"
            notes.append({"kind": "出口回答没用上", "key": key, "msg": f"{where} {why}，出口送什么先没用上"})
            continue
        raw = pool_fix.get(i) or [m for r in recipes_of(i) for m in (PM.members_of(d["name"], r) or (r,))]
        oc = PM.outlets(d["name"], raw, ans)
        notes += [{"kind": "出口回答没用上", "key": key, "msg": f"{where} {w}"} for w in oc["why"]]
        notes += [{"kind": "出口送中间产物", "key": key,
                   "msg": f"{where} 出口送的{x}是池子自己做、自己又用的中间产物：程序还不会算这种分法，这台按它没送出去算，以实际为准"}
                  for x in oc["mid"]]
        if oc["stuck"]:
            notes.append({"kind": "产物没出口", "key": key,
                          "msg": f"{where} 的{'、'.join(oc['stuck'])}三个出口都没选：游戏里它在池里攒满以后，做它的那条配方会停；"
                                 "几条配方都停住时，池子会清空缓存区（官方）。程序没按这个算，这台按它能送出去算，以实际为准"})
        if oc["pass"]:
            pt[i] = oc["pass"]
    return pt, notes


def _starved_by_reading(G, flow, devices, dev_canvas, rec, bd, feeds=None, extra=None) -> tuple[list, list]:
    """自相矛盾：一台生产设备连着带子，但没有一条是进料（全被读成出口）。在生产的设备不可能没有进料，一定是哪条带子读错了。
    先看这台设备自己的口格折角（`glyph_sides`：朝里 = 进口）：带子接在它的进口边上、却被读成往外出 → 这条读错了。
      - 另一头的口格没说话，或也说方向反了 → 把方向改过来；
      - 另一头也是进口（两头都是进口）→ 另一头的设备认错了：带子头旁边另有唯一一台设备、朝着带子头的那条边是出口 → 改认成它；
      - 都定不了 → 不硬选，列进待确认（作者在设计分析页选）。
    改过的连线依据里带「推断」，和别的推断连线一样进清单、等作者确认。→ (改了的, 待确认的)
    2026-11-14 issue #1：精炼炉 (1448,850) 三条带子全被读成出口。作者回答：带子 3 是芽针进料——那条带子的另一头夹在
    天有洪炉下边的进口和种植机上边的出口中间，被认成了天有洪炉。用户定的做法：遇到矛盾先看设备自己的进出口箭头，能定的程序自己改、标成推断。"""
    import cv2 as _cv
    from .pipenet import _rects, device_rect
    canvas = V.rd(bd / "composite.png")
    gray = _cv.cvtColor(canvas, _cv.COLOR_BGR2GRAY)
    hsv = _cv.cvtColor(canvas, _cv.COLOR_BGR2HSV)
    dark_thin = ((gray < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    rects = _rects(canvas)
    boxes, glyphs = {}, {}

    def box(i):
        if i not in boxes:
            c = dev_canvas[i]
            boxes[i] = device_rect(dark_thin, c[0], c[1], rects)
        return boxes[i]

    def side(i, edge):
        if i not in glyphs:
            glyphs[i] = glyph_sides(gray, box(i)) if box(i) is not None else {}
        return glyphs[i].get(edge)
    back = {"N": "S", "S": "N", "E": "W", "W": "E"}
    C = V.CELL

    def facing(j, P):
        """点 P 贴着设备 j 的哪条边（在边外 0.9 格以内、沿边方向落在边的范围里）。"""
        b = box(j)
        if b is None:
            return None
        x, y, w, h = b
        if x - 6 <= P[0] <= x + w + 6:
            if -6 <= y - P[1] <= 0.9 * C:
                return "N"
            if -6 <= P[1] - (y + h) <= 0.9 * C:
                return "S"
        if y - 6 <= P[1] <= y + h + 6:
            if -6 <= x - P[0] <= 0.9 * C:
                return "W"
            if -6 <= P[0] - (x + w) <= 0.9 * C:
                return "E"
        return None

    def nm(i):
        return f"{devices[i]['name']}[{int(dev_canvas[i][0])}, {int(dev_canvas[i][1])}]"

    def move(u, v, lk, nu, nv, why):
        G.out[u] = [(x, l) for x, l in G.out[u] if l is not lk]
        G.inn[v] = [(x, l) for x, l in G.inn[v] if l is not lk]
        nl = dict(lk, dir="a->b", basis=list(lk.get("basis") or []) + [why], reread=True)
        G.out[nu].append((nv, nl)); G.inn[nv].append((nu, nl))
        return nl
    makers = [i for i in range(len(devices)) if devices[i]["name"] not in PORT_NAMES and devices[i]["name"] not in BUFFERS
              and any(not _is_fluid(k) for r in _recipes_of(rec, i, extra) for k in inputs_of(r))]  # 哪条配方吃固体都算（extra 见 _recipes_of）
    fixed, ask = [], []

    def dkey(i):
        return f"{devices[i]['name']}@{devices[i]['x']},{devices[i]['y']}"
    for i in makers:
        u = ("d", i)
        if any(lk.get("layer") != "pipe" for _, lk in G.inn[u]):
            continue  # 有带子进料
        outs = [(v, lk) for v, lk in G.out[u] if lk.get("free") and lk.get("layer") == "belt" and v[0] == "d"
                and lk.get("a_dev") and lk.get("device") and not lk.get("adjacent")
                and not lk.get("reread")]  # 这一趟刚改过来的连线不再回头看（不然改完的那一头又成了「只有出口」，会改回去或被列进待确认）
        if not outs:
            continue  # 一条带子都没连上：是「没接上」，不是读反了
        ends = []  # (另一头, 连线, 我这头的口格, 另一头的口格, 另一头的带子头)
        for v, lk in outs:
            a, b = G._dev(lk["a_dev"], dev_canvas), G._dev(lk["device"], dev_canvas)
            if a is None or b is None or {a, b} != {u, v}:
                continue
            mine_side, other_side = (lk.get("a_dev_side"), lk.get("dev_side")) if a == u else (lk.get("dev_side"), lk.get("a_dev_side"))
            r_me = side(i, back[mine_side]) if mine_side else None
            r_other = (None if devices[v[1]]["name"] in PORT_NAMES else side(v[1], back[other_side])) if other_side else None
            ends.append((v, lk, r_me, r_other, lk["path"][-1] if a == u else lk["path"][0], lk["path"][0] if a == u else lk["path"][-1]))
        options = [{"to": nm(v[1]), "key": dkey(v[1]), "at": [int(t) for t in at], "my_edge": r_me, "other_edge": r_other}
                   for v, lk, r_me, r_other, _, at in ends]
        item = {"kind": "没有进料", "key": dkey(i), "device": nm(i), "c": [int(dev_canvas[i][0]), int(dev_canvas[i][1])], "recipe": rec[i][0],
                "msg": f"{devices[i]['name']}连着 {len(outs)} 条带子，但没有一条是进料（都被读成从它出去）。哪一条其实是进料？",
                "links": options}
        ans = (feeds or {}).get(dkey(i))
        if ans == "unused":  # 作者说这台没在用：不用再问
            ask.append(dict(item, answer=ans, applied=True))
            continue
        if ans and ans.startswith("to:"):
            hit = [(v, lk) for v, lk, *_ in ends if dkey(v[1]) == ans[3:]]
            if hit:
                v, lk = hit[0]
                nl = move(u, v, lk, v, u, "作者确认：这条带子是进料")
                fixed.append({"device": nm(i), "kind": "作者确认", "from": nm(v[1]), "basis": nl["basis"][-1]})
                ask.append(dict(item, answer=ans, applied=True))
                continue
        done = False
        for k, (v, lk, r_me, r_other, P_other, _) in enumerate(ends):
            if r_me != "in" or done:
                continue
            if devices[v[1]]["name"] == "仓库存货口":
                continue  # 存货口只进不出，反不过来
            if r_other in (None, "out"):
                nl = move(u, v, lk, v, u, f"{devices[i]['name']}没有别的进料，这条带子接在它的进口边上（口格折角朝里）：方向改过来（推断）")
                fixed.append({"device": nm(i), "kind": "方向改过来", "from": nm(v[1]), "basis": nl["basis"][-1]})
                done = True
                continue
            # 两头都是进口：另一头的设备认错了。带子头旁边找朝着它的那条边是出口的设备（取货口只出不进，也算）
            cands = []
            for j in range(len(devices)):
                if j in (i, v[1]) or devices[j]["name"] in BUFFERS:
                    continue
                if devices[j]["name"] == "仓库取货口":
                    if np.hypot(dev_canvas[j][0] - P_other[0], dev_canvas[j][1] - P_other[1]) <= 1.2 * C:
                        cands.append(j)
                    continue
                e = facing(j, P_other)
                if e is not None and side(j, e) == "out":
                    cands.append(j)
            if len(cands) == 1:
                j = cands[0]
                nl = move(u, v, lk, ("d", j), u,
                          f"两头都是进口：这头本来认的是{nm(v[1])}，改认成紧挨着带子头、那条边是出口的{nm(j)}（推断）")
                fixed.append({"device": nm(i), "kind": "另一头改认设备", "from": nm(j), "was": nm(v[1]), "basis": nl["basis"][-1]})
                done = True
            else:
                options[k]["candidates"] = [nm(j) for j in cands]
        if not done:
            ask.append(dict(item, answer=ans, applied=False))
    return fixed, ask


def _orphan_feeds(G, flow, devices, dev_canvas, rec, extra=None) -> list:
    """方向两边都说得通、没定下来的设备直连带（种植机 ↔ 采种机：种植机产砂叶给采种机，采种机产种子给种植机）：
    一头那台设备一条进料都没有、另一头的产物正好是它的原料 → 按「另一头 → 它」接上（它总得有料进来）。
    2026-11-10 武陵城种植机 (157,781)：唯一的设备邻居是下面的采种机 (120,987)。"""
    added = []
    for lk in flow["links"]:
        if not lk.get("free") or lk.get("dir") or not lk.get("a_dev") or not lk.get("device"):
            continue
        a, b = G._dev(lk["a_dev"], dev_canvas), G._dev(lk["device"], dev_canvas)
        if a is None or b is None or a == b or any(v == b for v, _ in G.out[a]) or any(v == a for v, _ in G.out[b]):
            continue
        for u, v in ((a, b), (b, a)):
            if devices[v[1]]["name"] in BUFFERS + PORT_NAMES or devices[u[1]]["name"] in BUFFERS:
                continue
            items = {k for r in _recipes_of(rec, u[1], extra) for k in outputs_of(r)} & {k for r in _recipes_of(rec, v[1], extra) for k in inputs_of(r)}
            if items and not G.inn[v]:
                nl = dict(lk, dir="a->b", basis=[f"{devices[v[1]]['name']}没有别的进料，按配方推断 {devices[u[1]]['name']} 往它送{'、'.join(sorted(items))}"])
                G.out[u].append((v, nl)); G.inn[v].append((u, nl))
                added.append((devices[u[1]]["name"], devices[v[1]]["name"]))
                break
    return added


def _buffer_dirs(G, flow, devices, dev_canvas, rec, extra=None, depth=6) -> list:
    """设备 ↔ 储存箱（中转）的带子没票、方向没定（Graph 里没收）：看储存箱往下送给谁 / 从谁那拿。
    储存箱下游有设备吃这台设备的产物 → 设备 → 储存箱；储存箱上游有设备产这台设备的原料 → 储存箱 → 设备；两样都有或都没有就不定。
    2026-11-10 武陵城：芽针粉碎机 (1793,1669) → 储存箱 (1636,1669) → 汇流器 → 反应池（作者说那台反应池游戏里在转）。
    extra：主配方以外在跑的配方（交替配方、并行池子的其余几条，见 _recipes_of）。"""
    def recipes_of(i):
        return _recipes_of(rec, i, extra)

    def reach(u, out):
        items, seen, front = set(), {u}, [u]
        for _ in range(depth):
            nxt = []
            for x in front:
                for v, _lk in (G.out[x] if out else G.inn[x]):
                    if v in seen:
                        continue
                    seen.add(v)
                    if v[0] == "n" or devices[v[1]]["name"] in BUFFERS:
                        nxt.append(v)
                    else:
                        for r in recipes_of(v[1]):
                            items |= set(inputs_of(r)) if out else set(outputs_of(r))
            front = nxt
        return items
    added = []
    cand = []
    und = defaultdict(set)  # 储存箱 → 方向没定的那几台邻居设备（两边都没定时互相等，一起看）
    for lk in flow["links"]:
        if not lk.get("free") or lk.get("dir") or lk["layer"] != "belt" or not lk.get("a_dev") or not lk.get("device"):
            continue
        a, b = G._dev(lk["a_dev"], dev_canvas), G._dev(lk["device"], dev_canvas)
        if a is None or b is None or a == b:
            continue
        if any(v == b for v, _ in G.out[a]) or any(v == a for v, _ in G.out[b]):
            continue
        for buf, dev in ((a, b), (b, a)):
            if devices[buf[1]]["name"] in BUFFERS and devices[dev[1]]["name"] not in BUFFERS:
                cand.append((lk, buf, dev))
                und[buf].add(dev)
    for lk, buf, dev in cand:
        if any(v == dev for v, _ in G.out[buf]) or any(v == buf for v, _ in G.out[dev]):
            continue
        if True:
            rcs = recipes_of(dev[1])
            prod = set().union(*(set(outputs_of(r)) for r in rcs))
            need = set().union(*(set(inputs_of(r)) for r in rcs))
            others = und[buf] - {dev}
            o_in = set().union(*(set(inputs_of(r)) for o in others for r in recipes_of(o[1]))) if others else set()
            o_out = set().union(*(set(outputs_of(r)) for o in others for r in recipes_of(o[1]))) if others else set()
            down = prod & (reach(buf, True) | o_in)
            up = need & (reach(buf, False) | o_out)
            if bool(down) == bool(up):
                continue
            u, v = (dev, buf) if down else (buf, dev)
            nl = dict(lk, dir="a->b", basis=[f"储存箱{'下游有人吃' if down else '上游有人产'}{'、'.join(sorted(down or up))}（按配方推断方向）"])
            G.out[u].append((v, nl)); G.inn[v].append((u, nl))
            added.append((devices[u[1]]["name"], devices[v[1]]["name"]))
    return added


def _outlet_touch(G, flow, devices, dev_canvas, rec=None, gray=None, extra=None) -> list:
    """物流件的进料边直接贴着暗管出口这类只出不进的设备（中间没有管子）：暗管图标外框是浅蓝细线，flow 按外框找不到它。
    进料边没接任何东西、正前方 0.5–3.2 格内、横向 ≤1.5 格有这种设备 → 接上（应龙关多口暗管出口 4 号 → 汇流器 6 号，2026-11-09）。"""
    DIRV = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
    used = set()
    for lk in flow["links"]:
        if lk.get("a") is not None:
            used.add((lk["a"], lk["a_side"]))
        if lk.get("b") is not None:
            used.add((lk["b"], lk.get("b_side")))
    # 只出不进的设备 + 生产设备（有配方产物）：武陵城精炼炉顶上的出料口直接贴着汇流器 → 塑形机（2026-11-09）
    def prods(i):  # 这台设备所有在跑配方的产物（extra 见 _recipes_of）
        return {k for r in _recipes_of(rec, i, extra) for k in outputs_of(r)}
    outs = [i for i, d in enumerate(devices) if d["name"] in OUTLETS or (TOUCH_PRODUCERS and rec and prods(i))]
    added = []
    for n in flow["nodes"]:
        if n["type"] in ("物流桥", "管道桥"):
            continue
        ins = n.get("ins") or ([n["in"]] if n.get("in") else list(DIRV) if n["type"] == "准入口" else [])
        if n["type"] == "准入口":  # 准入口只有一进一出两个口，在同一条直线上：已经接了一边，只看对面那边
            sides = [d_ for (nid_, d_) in used if nid_ == n["id"]]
            if len(sides) >= 2:
                continue
            if sides:
                ins = [{"E": "W", "W": "E", "N": "S", "S": "N"}[sides[0]]]
        for d in ins:
            if (n["id"], d) in used:
                continue
            vx, vy = DIRV[d]
            best = None
            pipe_node = n["type"].startswith("管道") or n.get("kind") == "管道"
            # 传送带物流件贴着生产设备：这一边得正好是设备的物品口那条边（粗黑边框）；设备只有管口的侧边是细灰线
            # （武陵城 4 个汇流器在精炼炉底边出料口下面，按距离先接到了旁边的塑形机/隔壁精炼炉的侧边，2026-11-09）
            port_edge = None if pipe_node or gray is None else _port_border(gray, n["c"], d)
            for i in outs:
                if devices[i]["name"] == "仓库取货口" and pipe_node:
                    continue  # 仓库只出物品，不接管道（应龙关液化息壤回环旁边那个取货口图标，2026-11-09 接错过）
                if "暗管" in devices[i]["name"] and not pipe_node and n["type"] != "准入口":
                    continue
                producer = devices[i]["name"] not in OUTLETS
                if producer and port_edge is False:
                    continue
                dx, dy = dev_canvas[i][0] - n["c"][0], dev_canvas[i][1] - n["c"][1]
                along, lat = dx * vx + dy * vy, abs(-dx * vy + dy * vx)
                lat_max = 2.0 * V.CELL if producer and port_edge else 1.5 * V.CELL  # 底账标签不在设备正中（偏 ~0.7 格），边框已确认时放宽
                if 0.5 * V.CELL <= along <= 3.3 * V.CELL and lat <= lat_max and (best is None or along < best[0]):
                    best = (along, i)
            if best is None:
                continue
            i = best[1]
            if devices[i]["name"] not in OUTLETS and G.inn[("n", n["id"])]:
                continue  # 生产设备只补「上游完全没接东西」的物流件：已经有来源的再加一条会把产物分走（景玉谷种植机 ↔ 采种机互供环被分塌）
            if devices[i]["name"] not in OUTLETS:  # 生产设备：产物得是这一层的（管道物流件只接流体产物，传送带物流件只接物品）
                fl_out = [k for k in prods(i) if "气" in k or "液" in k or k in ("清水", "污水", "沉积酸")]
                if (pipe_node and not fl_out) or (not pipe_node and len(fl_out) == len(prods(i))):
                    continue
            if any(v == ("n", n["id"]) for v, _ in G.out[("d", i)]) or any(v == ("d", i) for v, _ in G.out[("n", n["id"])]):
                continue  # 这台设备和这个物流件已经连着（从别的边）：不重复加，否则同一个来源会被当成两份
            lk = {"layer": "pipe" if "暗管" in devices[i]["name"] or n["type"].startswith("管道") else "belt", "dir": "a->b", "touch": True,
                  "basis": [f"{n['type']}贴着{devices[i]['name']}（中间没有管子）"]}
            G.out[("d", i)].append((("n", n["id"]), lk)); G.inn[("n", n["id"])].append((("d", i), lk))
            added.append((devices[i]["name"], n["type"], n["c"], d))
    return added


def _feed_touch(G, flow, devices, dev_canvas, rec) -> list:
    """物流件的出料边没接任何东西、正前方 0.5–3.3 格（3×3 设备中心离边 1.5 格；横向 ≤1.5 格）贴着一台要吃料的设备 → 接上
    （应龙关液化息壤回环：分流器 (1222,951) 往西直接贴着液气转化机右边的进料口，中间半格管子没追出来，2026-11-09）。"""
    DIRV = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
    used = set()
    for lk in flow["links"]:
        if lk.get("a") is not None:
            used.add((lk["a"], lk["a_side"]))
        if lk.get("b") is not None:
            used.add((lk["b"], lk.get("b_side")))
    eaters = [i for i in range(len(devices)) if inputs_of(rec[i][0])]
    added = []
    for n in flow["nodes"]:
        outs = n.get("outs") or ([n["out"]] if n.get("out") else [])
        for d in outs:
            if (n["id"], d) in used:
                continue
            vx, vy = DIRV[d]
            best = None
            for i in eaters:
                dx, dy = dev_canvas[i][0] - n["c"][0], dev_canvas[i][1] - n["c"][1]
                along, lat = dx * vx + dy * vy, abs(-dx * vy + dy * vx)
                if 0.5 * V.CELL <= along <= 3.3 * V.CELL and lat <= 1.5 * V.CELL and (best is None or along < best[0]):
                    best = (along, i)
            if best is None:
                continue
            i = best[1]
            if any(v == ("d", i) for v, _ in G.out[("n", n["id"])]):
                continue
            lk = {"layer": "pipe" if n["type"].startswith("管道") else "belt", "dir": "a->b", "touch": True,
                  "basis": [f"{n['type']}出口贴着{devices[i]['name']}（中间没追出管子/带子）"]}
            G.out[("n", n["id"])].append((("d", i), lk)); G.inn[("d", i)].append((("n", n["id"]), lk))
            added.append((n["type"], n["c"], d, devices[i]["name"]))
    return added


def tunnel_pairs(bd: Path, devices: list[dict], dev_canvas: list) -> list[tuple[int, int]]:
    """读蓝图时在游戏里逐个点开暗管得到的配对（bp/<蓝图>/links.json，pipenet 管网也用它）→ 底账设备序号对 (入口, 出口)。
    2026-11-09 用户：「入3（多口）你在蓝图分析时就画出来了」——计算器原来没读它，把暗管出口都当成物品不明的满管来源。"""
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    proj = bd.parent.parent
    tun = [i for i, d in enumerate(devices) if "暗管" in d["name"]]
    out = []
    for bp_name in tf.get("bps", {}):
        lp, mp = proj / "bp" / bp_name / "links.json", proj / "bp" / bp_name / "mosaic.png"
        if not lp.exists() or not mp.exists():
            continue
        lk = json.loads(lp.read_text(encoding="utf-8"))
        pipes = lk.get("pipes", [])
        shape = V.rd(mp).shape
        pairs = {tuple(sorted((int(a), int(b)))) for a, b in (lk.get("partner") or {}).items() if b is not None}
        for i, j in sorted(pairs):
            if i >= len(pipes) or j >= len(pipes):
                continue
            ends = []
            for k in (i, j):
                x, y = V.preview_to_canvas(tf, bp_name, shape, *pipes[k]["p"])
                cand = [t for t in tun if ("入口" in devices[t]["name"]) == ("入口" in pipes[k]["name"])]
                t = min(cand, key=lambda t: np.hypot(dev_canvas[t][0] - x, dev_canvas[t][1] - y)) if cand else None
                ends.append(t if t is not None and np.hypot(dev_canvas[t][0] - x, dev_canvas[t][1] - y) <= 45 else None)
            a, b = ends
            if a is None or b is None:
                continue
            if "入口" in devices[b]["name"]:
                a, b = b, a
            if "入口" in devices[a]["name"] and "出口" in devices[b]["name"]:
                out.append((a, b))
    return sorted(set(out))


def tunnel_files_missing(bd: Path) -> list[str]:
    """这个据点里本该有暗管配对、却读不到的蓝图名单（tunnel_pairs 读不出东西的那几张）。
    bp/<蓝图>/anchors.json 不在 → 算缺（别人的武陵城反馈包整个没有 bp/）；在的话，有可点的暗管（不带 open）而 links.json 或 mosaic.png
    不在、或者 partner 一对都没有 → 算缺。只有外接暗管（全带 open）或没有暗管的蓝图不算：read_blueprints 本来就不点外接暗管、
    不写 links.json，重读也生不出来，按「缺文件、请重读」提示永远消不掉（2026-11-19 评审：景玉谷两张蓝图的暗管都标成外接后误报）。"""
    tp = bd / "transform.json"
    if not tp.exists():                      # 还没拼合过：不知道据点里有哪几张蓝图，不提示（analyze 不能因为这个抛异常）
        return []
    tf = json.loads(tp.read_text(encoding="utf-8"))
    proj = bd.parent.parent
    out = []
    for bp_name in tf.get("bps", {}):
        d = proj / "bp" / bp_name
        if not (d / "anchors.json").exists():
            out.append(bp_name)
            continue
        anc = json.loads((d / "anchors.json").read_text(encoding="utf-8")) or {}
        if not any(a.get("name") in V.PIPE_NAMES and not a.get("open") for a in anc.get("anchors", [])):
            continue
        lp = d / "links.json"
        if not lp.exists() or not (d / "mosaic.png").exists():
            out.append(bp_name)
            continue
        part = (json.loads(lp.read_text(encoding="utf-8")) or {}).get("partner") or {}
        if not any(v is not None for v in part.values()):
            out.append(bp_name)
    return out


TUN_OUTLETS = ("暗管出口", "多口暗管出口")
# 拼合图 px：底账暗管出口离蓝图锚点、输入端标记离暗管出口多近算同一台（和 tunnel_pairs 认锚点同一个距离）
TUN_MARK_TOL = 45
TUN_CARD = ("外接", "漏配")  # 出「暗管来料」待确认卡的两类（见 tunnel_outlets）


def tunnel_ports(name: str) -> int:
    """暗管出口往外出几个口（游戏数据 building_ports.json：暗管出口 1、多口暗管出口 2）。作者填的来料最多 120 × 口数。"""
    from . import ports as PT
    n = sum(1 for p in (PT.layouts().get(name) or {}).get("ports", []) if p.get("role") == "out")
    return n or (2 if name.startswith("多口") else 1)


def tunnel_outlets(bd: Path, devices: list[dict], dev_canvas: list, tpairs) -> dict:
    """没配对的暗管出口（底账里的暗管出口 / 多口暗管出口，不在 tpairs 里的）→ {设备序号: {cls, bp, p_bp}}（0.10.13 功能二）。
    bp/<蓝图>/anchors.json 里同名的暗管出口锚点换到拼合图上，每个锚点归离它最近的同名底账设备（和 tunnel_pairs 一样，45 px 内；
    配上对的设备占着的锚点不归别人），没配对的设备按归它的那个锚点分：
    - 外接：锚点带 open（预览里有红标、没有放大镜：另一头不在这套蓝图里，读蓝图时不点它）；
    - 漏配：不带 open，这张蓝图有 links.json：这个口不在任何一对里，或者配上了、对面没对到底账设备（所以不在 tpairs 里）；
    - 缺文件：不带 open，这张蓝图没有 links.json 或 mosaic.png（tunnel_pairs 读不了；已有「暗管配对缺失」说明，不逐台出卡）；
    - 蓝图外：没有锚点归它（玩家在蓝图外自己摆的；项目里整个没有 bp/ 时也是这一类）。
    bp、p_bp = 那个锚点的蓝图名和蓝图预览里的坐标，蓝图外的是 None。"""
    paired = {x for pr in tpairs for x in pr}
    out = {i: {"cls": "蓝图外", "bp": None, "p_bp": None} for i, d in enumerate(devices) if d["name"] in TUN_OUTLETS and i not in paired}
    if not out:
        return out
    try:
        tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    proj = bd.parent.parent
    best = {}  # 设备序号 → (离锚点多远, 归类)
    for bp_name, b in (tf.get("bps") or {}).items():
        if not isinstance(b, dict) or "t" not in b or "rot" not in b:  # 换不到拼合图上（手写的 transform）
            continue
        d = proj / "bp" / bp_name
        try:
            anc = (json.loads((d / "anchors.json").read_text(encoding="utf-8")) or {}).get("anchors") or []
        except (OSError, ValueError):
            continue
        has_links = (d / "links.json").exists() and (d / "mosaic.png").exists()
        for a in anc:
            if a.get("name") not in TUN_OUTLETS or not a.get("p"):
                continue
            c = V.preview_to_canvas(tf, bp_name, None, *a["p"])
            cand = [j for j, dv in enumerate(devices) if dv["name"] == a["name"]]
            if not cand:
                continue
            dist, j = min((float(np.hypot(dev_canvas[j][0] - c[0], dev_canvas[j][1] - c[1])), j) for j in cand)
            if dist > TUN_MARK_TOL or j not in out or (j in best and best[j][0] <= dist):
                continue
            cls = "外接" if a.get("open") else "漏配" if has_links else "缺文件"
            best[j] = (dist, {"cls": cls, "bp": bp_name, "p_bp": list(a["p"])})
    out.update({j: t for j, (_dist, t) in best.items()})
    return out


TUN_NOTE_KINDS = ("暗管出口标记没用上", "暗管出口标了两个输入端", "暗管出口不支持几口合计")  # 绑标记时出的说明（_bind_tunnel_marks）


def _bind_tunnel_marks(marks, nodes, devices, dev_canvas, tun_out, tpairs) -> tuple[dict, dict, list]:
    """标在暗管出口上的输入端标记（作者在「输入端」页点的、在「暗管来料」卡上填的，0.10.13 功能二）
    → ({没配对的暗管出口: 用的那个标记}, {没配对的暗管出口: [标在它上面的全部标记，先标的在前]}, [说明])。
    只看位置，不看标记有没有 on 字段（应龙关作者标的 7 个老标记没有）：
    - 15 px 内有准入口的是准入口输入端（analyze 按同一条规则认），不绑暗管：准入口优先；
    - 剩下的离一台暗管出口（底账坐标换到拼合图上）45 px 内，绑最近的那台；
    - 最近的那台配上了对：来料来自对面的暗管入口，标记不用，说一句；一台标了两个：按先标的那个算，说一句；
    - 带几口合计（pool）的：第一版不支持，说一句，按这一口单独算。
    说明由 analyze 放在 inner() 外面（inner() 每轮开头清空 notes），带 outlet = 那台暗管出口的设备键：有「暗管来料」卡的挂到卡上
    （extra），没有卡的进汇总的提醒（summary 的 warnings）。说明是给作者看的，不写坐标、写输入端编号（「输入端」页的编号图上找得到）。
    全部标记的清单放进卡里（marks），存卡上的回答时改的、撤销时删的就是设计分析认到这台上的这几个（project._apply_tunnel_feeds）。
    原来这些标记全被丢掉，应龙关 7 台外接暗管照旧按满管 120 算。"""
    gate_ps = [n["p"] for n in nodes if n["type"] == "准入口"]
    paired_out = {b for _a, b in tpairs}

    def key(i):
        return f"{devices[i]['name']}@{devices[i]['x']},{devices[i]['y']}"
    bound, here, notes = {}, {}, []
    for m in marks:
        p = m.get("p")
        if not p or any(abs(q[0] - p[0]) <= 15 and abs(q[1] - p[1]) <= 15 for q in gate_ps):
            continue
        near = [(float(np.hypot(dev_canvas[i][0] - p[0], dev_canvas[i][1] - p[1])), i) for i in sorted(set(tun_out) | paired_out)]
        near = [t for t in near if t[0] <= TUN_MARK_TOL]
        if not near:
            continue
        i = min(near)[1]
        nm, no = devices[i]["name"], m.get("no")
        if i in paired_out:
            notes.append({"kind": "暗管出口标记没用上", "outlet": key(i),
                          "msg": f"输入端 {no} 号标在一台配上了对的{nm}上，它的来料来自对面的暗管入口：这个标记没用上（不要了可以在「输入端」页删掉）"})
            continue
        here.setdefault(i, []).append(m)
        if i in bound:
            first = bound[i].get("no")
            notes.append({"kind": "暗管出口标了两个输入端", "outlet": key(i),
                          "msg": f"输入端 {first} 号、{no} 号标在同一台{nm}上，按 {first} 号算，{no} 号没用上"})
        else:
            bound[i] = m
            if m.get("pool"):
                notes.append({"kind": "暗管出口不支持几口合计", "outlet": key(i),
                              "msg": f"输入端 {no} 号标在{nm}上、设了几口合计，暗管出口上的标记暂时不支持几口合计：按这一口单独算"})
    return bound, here, notes


def _mark_rate(m: dict) -> float | None:
    """标在暗管出口上的输入端标记填的每分钟多少（没填、不是数 → None）。清水没填量时 pipenet.load_inputs 按一台水泵补的 60
    （rate_auto）不算填了：暗管出口上的照「没填」算，按满管估、卡算没答。"""
    r = m.get("rate")
    if m.get("rate_auto"):
        return None
    try:
        return float(r) if r not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _mark_item(m: dict | None) -> str:
    """标在暗管出口上的输入端标记填的物品：只认液体、气体（暗管只走管子）。没填、填了固体（「输入端」页能填任何物品）→ ""，
    按下游缺什么当什么（填了固体照推的话下游会拿到一管子固体，变成没料）。"""
    it = (m or {}).get("item") or ""
    return it if it and PM.is_fluid(it) else ""


def _tunnel_card(i, t, m, devices, dev_canvas, G, nodes, gate, links, here=()) -> dict:
    """「暗管来料」待确认卡（0.10.13 功能二）。t = tunnel_outlets 给这台的 {cls, bp, p_bp}；m = 设计分析用的那个标记（没有是 None）；
    links = _pool_links 给这台的连线（出去的几路和 G.out 同一个顺序）；here = 标在这台上的全部标记（_bind_tunnel_marks，m 在最前）。
    - answer = {no, item, rate}：作者填了每分钟多少（0 = 这台平时不来料）才算答了，applied 跟着它；只填了物品、没填量照没答（answer 为 None），
      标记放在 mark 里（界面预填物品用；清水没填量时读进来补的 60 去掉，照没填写）；
    - outs 每一路：to 去哪几台（经过标了物品的准入口时写在前面：追线把别的口接到暗管上时，作者看物品就知道不对）、items 下游要什么、
      unclear 物品不明、gate 这一路第一个准入口的物品、at 这一路第一个物流件的位置（小图上标 1、2…；贴着暗管的准入口没有管子，
      管子端点会落在暗管正中）；
    - ports 口数（多口暗管出口 2），suggest 下游要的液体、气体；
    - marks 标在这台上的全部标记编号（存回答时改第一个、撤销时都删，见 project._apply_tunnel_feeds）；extra 给作者看的提醒
      （填的不是液体、气体；analyze 再挂上一台标了两个、几口合计的说明）。
    没填时说明照实写怎么估的：每一路各按满管 120（sources 的算法），接了几路就是几个 120，单口接两路时合计会超过一根管子。"""
    d, u = devices[i], ("d", i)
    cc = [round(v) for v in dev_canvas[i]]
    every = set(range(len(devices)))
    outs = []
    for (v, _lk), L in zip(G.out[u], [L for L in links if L["dir"] == "out"]):
        gi = _first_gate_item(G, v, gate, stop=every)  # 走到设备就停：只看这一路自己的准入口
        to = "、".join(L["ends"][:3]) or "（没追到设备）"
        at = nodes[v[1]]["c"] if v[0] == "n" else dev_canvas[v[1]]
        outs.append({"to": f"{gi}准入口 → {to}" if gi else to, "items": L["dem"], "unclear": L["wild_out"], "gate": gi,
                     "at": [round(at[0]), round(at[1])]})
    n_ports = tunnel_ports(d["name"])
    rate = _mark_rate(m) if m else None
    item = _mark_item(m)
    no = (m or {}).get("no")
    extra = []
    raw = (m or {}).get("item") or ""
    if raw and not item:
        extra.append(f"输入端 {no} 号填的物品「{raw}」不是液体或气体，暗管送不了，没按它算（当成没填物品）；请在下面重新选。")
    who = "是外接的" if t["cls"] == "外接" else f"在蓝图「{t['bp']}」里，读蓝图时没认出它和哪个暗管入口连着"
    top = f"最多 {PIPE_CAP * n_ports:g}"
    if rate is not None and rate <= EPS:
        msg = f"你说过这台平时不来料（输入端 {no} 号），下游不从它拿料；要改就重新填了再点「按这个算」，或者撤销。"
    elif rate is not None:
        what = f"{item or '物品没填（按下游缺什么当什么）'} {rate:g}/分"
        msg = f"这台{d['name']}{who}，按你填的 {what} 算（输入端 {no} 号）；要改就改了再点「按这个算」，或者撤销。"
    else:
        n_out = len(outs)
        if n_out > 1:  # 每一路各按 120：单口接了两路时合计 240，比一根管子多
            over = f"，可{'一根管子' if n_ports == 1 else f'{n_ports} 个口合计'}实际{top}" if n_out > n_ports else ""
            how = f"接出去的 {n_out} 路每路按满管 120/分算（合计 {PIPE_CAP * n_out:g}/分{over}）"
        else:
            how = "按一根管子满送 120/分"
        est = how + "、" + (f"物品按你填的{item}" if item else "下游缺什么就当什么") + "来估，结果可能偏乐观。"
        if t["cls"] == "外接":
            msg = f"这台{d['name']}是外接的：另一头不在这套蓝图里，程序读不到它送进来什么、每分钟多少。现在{est}"
        else:
            msg = f"这台{d['name']}{who}，现在{est}最好到「读蓝图」重读这张蓝图；如果它其实是外接的，也可以直接在这里填。"
        if not item:  # 只填了物品的：缺的是每分钟多少，界面在卡上另起一行说
            msg += (f"这是多口暗管出口，有 {n_ports} 个口：请填几个口合计送进来的物品和每分钟多少（{top}），程序按下游各路吃得下的分。"
                    if n_ports > 1 else f"请填上它送进来的物品和每分钟多少（一根管子{top}）。")
    ans = {"no": no, "item": item, "rate": rate} if rate is not None else None
    mk = None
    if m:
        mk = {k: v for k, v in m.items() if k != "rate_auto"}
        if m.get("rate_auto"):
            mk["rate"] = None
    return {"kind": "暗管来料", "key": f"{d['name']}@{d['x']},{d['y']}", "device": f"{d['name']}[{cc[0]}, {cc[1]}]", "c": cc,
            "cls": t["cls"], "bp": t["bp"], "p_bp": t["p_bp"], "ports": n_ports, "msg": msg, "outs": outs,
            "suggest": sorted({k for o in outs for k in o["items"] if PM.is_fluid(k)}), "answer": ans, "applied": ans is not None,
            "mark": mk, "marks": [x.get("no") for x in here or ([m] if m else [])], "extra": extra}


def _gate_items(bd: Path, nodes: list[dict], rec: dict, inputs: list[dict] | None = None) -> dict:
    """准入口没标物品的（作者还没在界面里标）→ {节点 id: {item, how}}。
    1. 准入口图标早按长相分了组（icon_group）：作者确认过物品的输入端在哪组，整组都是那种物品（应龙关 5/6 号息壤气那组）。
    2. 再查图标库（作者标过的同样子图标）。
    3. 剩下的组和 AKEData 比：管道口只看液/气、物品口只看固体、只看本据点配方里的物品；组内取分最高的，≥0.5 且领先 ≥0.08 才用
       （0.45 左右会把息壤气口认成气态赫铜，把气挡在准入口外，2026-11-09 应龙关）。"""
    from . import logistics as LG
    img = V.rd(bd / "composite.png")
    rots_at = _rots_fn(bd)
    cand = set()
    for rc, _ in rec.values():
        cand |= set(inputs_of(rc)) | set(outputs_of(rc))
    by_group = {}
    for m in inputs or []:
        if m.get("item") and m.get("icon_group") is not None:
            by_group.setdefault(m["icon_group"], m["item"])
    out, best = {}, {}
    for n in nodes:
        if n["type"] != "准入口" or n.get("item"):
            continue
        g = n.get("icon_group")
        if g is not None and g in by_group:
            out[n["id"]] = {"item": by_group[g], "how": "同组输入端（作者标）"}
            continue
        ic = LG.gate_icon(img, n["p"])
        if ic.shape[:2] != (23, 23):
            continue
        # 图标库里存的是蓝图预览里的朝向（读蓝图时按预览拼图存的），拼合图上的要转回来才对得上：
        # 不转的时候，转着放的据点（应龙关 180°、武陵城 90°）图标库一个都配不上，全靠后面和 AKEData 比（2026-11-14）
        st, nm = None, ""
        for r in rots_at(*n["p"]):
            st, nm = LG.lookup_item(LG.upright(ic, r), bd.parent.parent / "items" if (bd.parent.parent / "items").exists() else None)
            if nm:
                break
        if nm:
            out[n["id"]] = {"item": nm, "how": "图标库"}
            continue
        k = n.get("kind")
        ok_state = ("液", "气") if k == "管道" else ("固",) if k == "物品" else ("液", "气", "固")
        cs = [c for c in LG.ake_candidates_upright(ic, rots_at(*n["p"]), top=999) if c["state"] in ok_state and (not cand or c["name"] in cand)]
        if not cs:
            continue
        lead = cs[0]["score"] - (cs[1]["score"] if len(cs) > 1 else 0.0)
        key = g if g is not None else ("id", n["id"])
        if key not in best or cs[0]["score"] > best[key][0]:
            best[key] = (cs[0]["score"], lead, cs[0]["name"])
        out.setdefault(n["id"], None)
    for n in nodes:
        if n["type"] != "准入口" or out.get(n["id"]) is not None or n["id"] not in out:
            continue
        g = n.get("icon_group")
        sc, lead, nm = best.get(g if g is not None else ("id", n["id"]), (0, 0, ""))
        if sc >= 0.5 and lead >= 0.08:
            out[n["id"]] = {"item": nm, "how": f"图标比对 {sc:.2f}"}
    return {k: v for k, v in out.items() if v}


def _fix_recipes_by_ports(G, devices, rec, recipes, pitem, skip=()) -> list[dict]:
    """配方来自历史巡检面板、按同名最近配对（±30 px），同类设备挨着时会配错台（2026-11-09 景玉谷：
    「砂叶 → 砂叶粉末」配到了一台由源矿取货口喂料的粉碎机上）。仓库取货口认出了物品后反过来核对：
    喂进来的物品一个都不在配方原料里，就换成同类设备在本据点出现过的、原料包含这些物品的配方（标推断）。
    skip：并行池子（2026-11-19）。只看主配方的原料，取货口喂的是第二条配方的原料时会把它换成一条。"""
    known = defaultdict(set)
    for d in recipes:
        if d.get("recipe") and "→" in d["recipe"]:
            known[d["name"]].add(d["recipe"])
    out = []
    for i, d in enumerate(devices):
        rc, inf = rec[i]
        if not rc or d["name"] in PORT_NAMES or i in skip:
            continue
        fed = set()
        for v, _ in G.inn[("d", i)] + G.out[("d", i)]:
            if v[0] == "d" and devices[v[1]]["name"] in PORT_NAMES and pitem.get(v[1], {}).get("sure"):
                fed.add(pitem[v[1]]["item"])
        fed -= set(outputs_of(rc))  # 往存货口送产物的不算
        if not fed or fed & set(inputs_of(rc)):
            continue
        alt = [r for r in known.get(d["name"], ()) if fed <= set(inputs_of(r))]
        if len(alt) == 1:
            rec[i] = (alt[0], True)
            out.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rc, "to": alt[0],
                        "why": f"仓库取货口喂的是{'、'.join(sorted(fed))}，原配方不吃它"})
    return out


def _first_gate_item(G, u, gate, depth=0, stop=()):
    """顺着出料找第一个标了物品的准入口（带子上的物品准入口说明这条带子运的是什么）。stop：生产设备，到这里就不往下找了——
    过了设备就是它的产物那条线，再碰到的准入口和这条带子无关（2026-11-14 别人的武陵城：图标没看到的取货口 → 精炼炉，
    顺着精炼炉的出料一路找到一个清水准入口，取货口就被当成出清水的，精炼炉的矿算成没人送）。"""
    if depth > 12:
        return ""
    if u[0] == "n" and u[1] in gate and gate[u[1]].get("item"):
        return gate[u[1]]["item"]
    if u[0] == "d" and u[1] in stop:
        return ""
    for v, _ in G.out[u]:
        it = _first_gate_item(G, v, gate, depth + 1, stop)
        if it:
            return it
    return ""


# 同一进程里的设计分析一次只跑一个（2026-11-19）。计算模式已经改成参数（见 RATE_LIMITED_INTAKE 上面的说明），这把锁是第二道保险：
# 以后再有人加模块级的状态也不会串台；同时两次写同一个据点的 design.json 也排上队。设计分析本来就是重计算，排队没有损失。
# 加锁顺序：api 的任务锁（Api._lock）只在开任务时拿一下、不在计算期间持有；这里的锁在外，flow._BUILD_LOCK 在内。
# design 现在不调 flow.build（拼合是先 flow.run_base、再 design.run_base，前后两次），flow 里也不许反过来调 design.run_base，
# 两把锁不会互相等。用可重入锁：同一个线程里嵌套调用（以后有人在 run_base 里再调 run_base）不会把自己锁死
_RUN_LOCK = threading.RLock()


def _can_make(machine: str) -> list[str]:
    """这种机器按配方库能做的产物（配方未知的设备拿它去对下游缺的料）。"""
    return sorted({k for r in _machine_recipes(machine) for k in outputs_of(r)})


def _blank_row(i, d, rc, dev_canvas, G, unused=False) -> dict:
    """没有配方的生产设备也出一行（审计第 3 项，2026-11-19）：配方未知，或作者说这台池子没在用。
    开工率记 0.0（数字，界面和 summary 都按数字读），但状态单独一类，不算成「没料」：别人的武陵城 7 台池子原来整台不在结果里。"""
    u = ("d", i)
    nin, nout = len(G.inn[u]), len(G.out[u])
    if unused:
        state, basis = "没在用（作者说）", ["作者说这台没在用：不算开工率，也不往下游送料", f"物流图上进料 {nin} 条、出料 {nout} 条"]
    else:
        pool = d["name"] in POOLS
        if rc.startswith("池产物"):
            why = f"池产物没还原出配方（{rc[:40]}）"
        elif rc:
            why = f"配方文字认不出（{rc[:40]}）"
        else:
            why = "没读到配方，机身也认不出产物" + ("（池子机身没有产物图标）" if pool else "")
        state = "配方未知"
        basis = [f"配方未知：{why}。开工率没算；它的产物没往下游送（下游可能偏低）；它吃的原料也没从共用管网里扣（同网别的设备可能偏乐观）",
                 f"这种机器能做：{'、'.join(_can_make(d['name']))}",
                 f"物流图上进料 {nin} 条、出料 {nout} 条" + ("，去待确认里选配方" if pool else "")]  # 池子有「池子配方」待确认卡
    row = {"device": d["name"], "x": d["x"], "y": d["y"], "c": [round(v) for v in dev_canvas[i]], "recipe": "", "env_required": None,
           "via_inferred": False, "recipe_inferred": False, "need": {}, "got": {}, "rate": 0.0, "limit_by": None, "state": state,
           "zero_margin": [], "feeds": nin, "outs": nout, "basis": basis, "fuel": None, "loop": False}
    if not unused:
        row.update(recipe_unknown=True, can_make=_can_make(d["name"]))
    return row


def _name_unknown_upstream(rows, G, devices, dev_canvas, unknown, buf_ids, depth=8, most=3):
    """下游点名（审计第 3 项，2026-11-19）：开工率不满的设备，缺的那种原料沿进料往上游找（穿过物流件、储存箱、暗管，
    不穿过配方已知的生产设备，最多 depth 层），碰到配方未知、而且这种机器能做这种料的设备 → 依据开头点名，写 blocked_by_unknown。
    只改依据，不动数字。例：别人的武陵城封装机 (1505,948) 缺的料该由上游没配方的扩容反应池送，原来只写「没料」，看不出是池子的缘故。"""
    if not unknown:
        return
    idx = {(r["device"], r["x"], r["y"]): r for r in rows}
    can = {}
    for i, d in enumerate(devices):
        r = idx.get((d["name"], d["x"], d["y"]))
        if r is None or not r["need"] or r["rate"] >= 0.995:
            continue
        short = [k for k, v in r["need"].items() if r["got"].get(k, 0.0) + 1e-6 < v]
        if not short:
            continue
        hits = {}
        seen, front = {("d", i)}, [("d", i)]
        for _ in range(depth):
            nxt = []
            for x in front:
                for v, _lk in G.inn[x]:
                    if v in seen:
                        continue
                    seen.add(v)
                    if v[0] == "n" or v[1] in buf_ids:
                        nxt.append(v)
                    elif v[1] in unknown:
                        nm = devices[v[1]]["name"]
                        can.setdefault(nm, set(_can_make(nm)))
                        ks = [k for k in short if k in can[nm]]
                        if ks:
                            hits[v[1]] = ks
            front = nxt
        if not hits:
            continue
        r["blocked_by_unknown"] = [f"{devices[j]['name']}@{devices[j]['x']},{devices[j]['y']}" for j in hits]
        names = "、".join(f"{devices[j]['name']}({round(dev_canvas[j][0])},{round(dev_canvas[j][1])})（能做{'、'.join(ks)}）"
                         for j, ks in list(hits.items())[:most])
        more = f" 等 {len(hits)} 台" if len(hits) > most else ""
        r["basis"].insert(0, f"上游配方未知：{names}{more}。这一路的料没算进来，缺的料可能就是{'它们' if len(hits) > 1 else '它'}的")


def _name_count_gates(rows, G, devices, nodes, gate, gate_held, shut, once, buf_ids, depth=8):
    """开了限制通过数量的准入口（once，0.10.13 A2）：下游第一台设备（穿过物流件、储存箱、配上对的暗管，不穿过别的设备，最多 depth 层）
    缺这个口挡下的那种料时，依据末尾说一句「上游准入口(x,y) 开了限制通过数量 N：一次性的，放满就不再放行，长期按不送X算」。
    吃饱了的不写（皇帝产线武陵城采种机 (1020,1391) 靠种植机供锦草，不提）。只改依据，不动数字。
    挡下的料 = 推料时记在 gate_held 里的，加上口上认出的物品，再加上 shut（analyze 传进来的是主分析最后一轮的 gate_shut 和两遍 probe
    的并集：不算出料堵死时（正的背压照算）真有料在动着送到这个口上的物品，见 _gate_view 的 shut）：分流器按各路吃得下的分，这一路吃量是 0、一点都分不到；
    输入端直接按不来料算（sources）；上游是生产设备、池子时背压先把它压成 0（审查 2026-11-21）——这几种推料账上都是空的。
    都没有（口上没认出、也没追到送什么料）不写。坐标是拼合图坐标。
    行上 fed_blocked 里的料也照写：它只表示 probe 里到手 > 0，别路不一定真有、也不一定够（试过不写，唯一缺料的口也被藏住，
    2026-11-22 撤回）。所以可能和依据里「X有来料…不算缺料」那句看着相反（已知）。"""
    idx = {(r["device"], r["x"], r["y"]): r for r in rows}
    for nid, g in gate.items():
        if not once(g):
            continue
        its = [k for k, v in gate_held.get(nid, {}).items() if v > EPS and k not in UNKNOWN]
        for k in ([g["item"]] if g.get("item") else []) + list(shut.get(nid, [])):
            if k not in its:
                its.append(k)
        if not its:
            continue
        users, seen, front = [], {("n", nid)}, [("n", nid)]
        for _ in range(depth):
            nxt = []
            for x in front:
                for v, _lk in G.out[x]:
                    if v in seen:
                        continue
                    seen.add(v)
                    if v[0] == "d" and v[1] not in buf_ids:
                        users.append(v[1])
                    else:
                        nxt.append(v)
            if not nxt:
                break
            front = nxt
        c = nodes[nid]["c"]
        n_txt = f"{g['count']['limit']:g}" + ("（作者填）" if g.get("src") == "作者" else "")
        for j in users:
            r = idx.get((devices[j]["name"], devices[j]["x"], devices[j]["y"]))
            if r is None:
                continue
            for it in its:
                if it in r["need"] and r["got"].get(it, 0.0) + 1e-6 < r["need"][it]:
                    r["basis"].append(f"上游准入口({round(c[0])},{round(c[1])}) 开了限制通过数量 {n_txt}：一次性的，放满就不再放行，长期按不送{it}算")


def _pool_links(i, G, devices, dev_canvas, recipes_of, known, pitem, gate, depth=12) -> list[dict]:
    """池子 i 的每根进出连线通到什么（给候选配方打分、给待确认卡片画「进来 / 出去」，审计第 3 项，2026-11-19）。
    进料往上游、出料往下游走：穿过物流件和储存箱（最多 depth 层），碰到别的设备就停。配方已知的设备（known）记它的产物 / 原料；
    输入端准入口记作者填的物品；认准了物品的仓库口记那种物品；没认准的仓库口物品不明（wild）。
    暗管：有配对（G 里 tunnel 的边，读蓝图时点开确认的）当中转，接着往对面走；没配对的、对面再走不下去的物品不明（2026-11-21）。
    别人的武陵城扩容反应池 (1798,454) 右上进液口的污水从精炼炉 (1278,424) 经四对暗管过来，原来卡片写「物品不明，来自暗管出口」；
    四对暗管中间夹着两段管子和一个管道分流器，从池子往上游要走 10 层才到精炼炉，原来的 6 层走不到，层数改成 12。
    推料时按缺口分出来的物品不当证据（那是拿配方反推的）。→ [{layer, dir, sup, dem, wild_in, wild_out, ends, at}]
    例：别人的武陵城扩容反应池 (1571,1729) 的进料管通到另一台池子和取货口，出料管通到提纯机（吃惰性壤晶废液）。"""
    u = ("d", i)
    cx, cy = dev_canvas[i]
    out = []
    for dirn, edges, step in (("in", G.inn[u], G.inn), ("out", G.out[u], G.out)):
        for v, lk in edges:
            sup, dem, ends, wild = set(), set(), [], False
            via = []  # 穿过的配对暗管
            seen, front = {u, v}, [v]
            for _ in range(depth):
                nxt = []
                for x in front:
                    if x[0] == "d" and devices[x[1]]["name"] not in BUFFERS:
                        j, nm = x[1], devices[x[1]]["name"]
                        if "暗管" in nm:
                            paired = any(l2.get("tunnel") for _y, l2 in G.out[x] + G.inn[x])
                            if paired and step[x]:  # 配上对的暗管：不算终点，接着走（入口 → 出口那条边也在 step 里）
                                via.append(nm)
                                for y, _lk in step[x]:
                                    if y not in seen:
                                        seen.add(y)
                                        nxt.append(y)
                                continue
                            ends.append(nm)  # 没配对，或者对面那头什么都没接
                            wild = True
                            continue
                        ends.append(nm)
                        if nm in PORT_NAMES:
                            it = pitem.get(j) or {}
                            if it.get("sure") and it.get("item"):
                                (sup if dirn == "in" else dem).add(it["item"])
                            else:
                                wild = True
                        elif j in known:
                            for r in recipes_of(j):
                                sup |= set(outputs_of(r))
                                dem |= set(inputs_of(r))
                        continue
                    if x[0] == "n":
                        g = gate.get(x[1]) or {}
                        if dirn == "in" and g.get("input") and g.get("item"):
                            sup.add(g["item"])
                            ends.append(f"输入端{g['input'].get('no')}号")
                    for y, _lk in step[x]:
                        if y not in seen:
                            seen.add(y)
                            nxt.append(y)
                front = nxt
            if via and (front or not ends):
                # 穿过配对暗管以后层数用完还没走到头，或者一台设备 / 输入端都没碰到：物品不明，记那台暗管
                # （2026-11-21 评审：原来这两种既不记不明也不记终点，卡片显示「没追到设备」，打分按「解释不了」扣分）
                wild = True
                ends.append(via[0])
            fl = lk.get("layer") == "pipe"
            pts = lk.get("path") or []
            at = min(([p[0], p[1]] for p in (pts[:1] + pts[-1:])), key=lambda p: np.hypot(p[0] - cx, p[1] - cy), default=[cx, cy])
            out.append({"layer": "pipe" if fl else "belt", "dir": dirn, "sup": sorted(k for k in sup if PM.is_fluid(k) == fl),
                        "dem": sorted(k for k in dem if PM.is_fluid(k) == fl), "wild_in": wild and dirn == "in",
                        "wild_out": wild and dirn == "out", "ends": list(dict.fromkeys(ends)), "at": [int(at[0]), int(at[1])]})
    return out


def _pool_candidates(machine: str, links: list[dict], most: int = 5) -> list[dict]:
    """这种池子放得下的配方组合按连线打分，取前 most 个 → [{recipes, label, score, items, why}]（推断，只给作者参考）。
    只列连警告都没有的组合（管出口 2 种液体、带出口 1 种固体、管入口 2 种流体），和 scratchpad 试验 score_pools 同一套分值。
    不自动套用（2026-11-19 定）：别人的武陵城物流图至少有 4 处漏连或连错，推出来的配方会把噪声带进下游的结果。"""
    sc = sorted(((PM.score(c, links), k, c) for k, c in enumerate(PM.combos(machine, strict=True))), key=lambda t: (-t[0][0], t[1]))
    return [{"recipes": list(c), "label": " + ".join("、".join(outputs_of(r)) for r in c), "score": round(s, 1),
             "items": PM.check(machine, c)["items"], "why": why} for (s, why), _k, c in sc[:most]]


def run_base(bd: Path, archive: Path | None, area: str, ledger: Path | None = None) -> dict:
    """bd = 项目 base/<据点>/；archive = 蓝图档案目录（data/blueprints/<档案>，配方表、准入口限速从这里读，可以没有）；
    ledger = 底账（不给就用档案里的）。写 design.json、design.jpg 和准入口表 gates_view.json（见 _gate_view）。"""
    with _RUN_LOCK:
        return _run_base(bd, archive, area, ledger)


def _head(r: dict) -> int:
    """run_base 往依据开头补话的位置：点了上游配方未知设备名的那条留在最前面（2026-11-19：别人的武陵城提纯机 (1592,443)
    先看到「上游配方未知」，再看「卡在：」，不然以为是物流图漏连）。"""
    return 1 if r.get("blocked_by_unknown") else 0


def _run_base(bd: Path, archive: Path | None, area: str, ledger: Path | None) -> dict:
    led = json.loads((ledger or archive / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
    rp = archive / "ledger" / f"{area}_recipes.json" if archive else None
    recipes = json.loads(rp.read_text(encoding="utf-8"))["devices"] if rp and rp.exists() else []
    icon_recipes = []
    if not any("→" in (d.get("recipe") or "") for d in recipes):
        # 档案里没有这个据点的配方表（不是皇帝产线：配方表是开发期间巡检点面板攒的）：按预览里机身中间的产物图标推。
        # 2026-11-13 issue #1 的作者拿自己的蓝图走到这一步是「共 0 台」。有配方表的据点不走这里，结果和原来一样
        from . import recipe_icons as RI
        from .pipenet import load_inputs
        icon_recipes = RI.derive(bd, led, known=recipes, inputs=load_inputs(bd))
        recipes = recipes + icon_recipes
        atomic.write_json(bd / "recipes_icon.json", {"area": area, "from": "机身产物图标（recipe_icons）", "devices": icon_recipes})
    glp = archive / "ledger" / "gate_limits" / f"{area}.json" if archive else None
    gl = json.loads(glp.read_text(encoding="utf-8")) if glp and glp.exists() else None
    res = analyze(bd, led, recipes, gl)
    gate_view = res.pop("gate_view", None)  # 准入口表用主遍的（写 gates_view.json，不进 design.json：design.json 顶层键不变）
    # 先跑主遍：池子出口转送原料的比例（0.10.13 功能三，pass_split）按主遍定，第二、三遍直接用，不各自再定一遍（省掉两遍的外层，
    # 三遍的转送量也对得上）。没有出口回答时不带这个参数，三遍和原来一样（调换先后不改结果）
    ps = {"pass_split": res["pass_split"]} if res.get("pass_split") else {}
    pot = analyze(bd, led, recipes, gl, rate_limited_intake=False, **ps)  # 第二遍：不管别的原料够不够，每种原料「能送到多少」→ 报真正卡住的是哪种
    pot.pop("gate_view", None)
    if any(r["state"].startswith("溢流位") for r in res["devices"]):
        # 第三遍（情景）：设备出料口的主路全堵死（下游爆仓、塑形机出料堵住…），备路上的设备能拿到多少
        blk = analyze(bd, led, recipes, gl, main_blocked=True, **ps)
        blk.pop("gate_view", None)
        bm = {(r["device"], tuple(r["c"])): r for r in blk["devices"]}
        for r in res["devices"]:
            b = bm.get((r["device"], tuple(r["c"])))
            if r["state"].startswith("溢流位") and b is not None and b["rate"] > EPS:
                r["if_main_blocked"] = {"rate": b["rate"], "got": b["got"]}
                r["basis"].insert(_head(r), f"主路堵住时（下游爆仓）能拿到 " + "、".join(f"{k} {v:g}/分" for k, v in b["got"].items())
                                  + f"，开工率 {b['rate']:.0%}")
    potm = {(r["device"], tuple(r["c"])): r["got"] for r in pot["devices"]}
    # 出料受限的并行池子点了名的下游设备 → 这几条配方的产物：下游的「卡在」不再列它们（2026-11-19 审计第 3 项）。
    # 武陵城灌装机 (1069,1124) 缺蓝铁瓶，反应池 (1357,1332) 的芽针溶液那条因此只跑 50%，灌装机能送到的芽针溶液也就 15；
    # 再写「卡在：芽针溶液」就成了两台互相指认。只改这句话，available 的数不动
    by_pool = defaultdict(set)
    for r in res["devices"]:
        for ch in r.get("channels") or []:
            for key in ch.get("named") or []:
                by_pool[key] |= set(outputs_of(ch["recipe"]))
    for r in res["devices"]:
        pg = potm.get((r["device"], tuple(r["c"])))
        if pg is None:
            continue
        r["available"] = pg
        if r["rate"] < 0.995 and r["need"]:
            base = r.get("need_capped") or r["need"]  # 并行池子按出料上限要的量比：出料受限的不报「卡在」原料
            # 出料堵死、原料其实有来料的（analyze 的 fed_blocked，依据第二句已经说了「有来料…不算缺料」）「卡在」那句话里也不列：
            # 「能送到 0」是因为这台出不去没分给它，再写「卡在」就和间歇对不上（0.10.13 A2）。limit_by 不跟着变
            skip = by_pool.get(f"{r['device']}@{r['x']},{r['y']}", ())
            short = sorted((k for k in r["need"] if pg.get(k, 0.0) + 1e-6 < base[k] and k not in skip),
                           key=lambda k: pg.get(k, 0.0) / r["need"][k])
            r["limit_by"] = short[0] if short else r.get("limit_by")  # limit_by 照旧按全部原料排（probe 不改它，最后一轮复审）
            fed = set(r.get("fed_blocked") or ())
            short = [k for k in short if k not in fed]
            if short:
                r["basis"].insert(_head(r), "卡在：" +"、".join(f"{k}（能送到 {pg.get(k, 0):g}/{r['need'][k]:g}）" for k in short))
    res["summary"] = summary(res)
    if gate_view is not None:  # 先写准入口表再写 design.json（界面看到新 design.json 时表已经是新的）。打桩的 analyze 没有它
        atomic.write_json(bd / "gates_view.json", gate_view)
    atomic.write_json(bd / "design.json", res)  # 原子写：界面（api.bpa_design）随时会来读，原来先清空再写，压测里 15% 的读拿到空文件
    try:
        image(V.rd(bd / "composite.png"), res).save(bd / "design.jpg", quality=88)
    except Exception as e:  # 出图失败不影响结果
        print(f"设计分析图没画出来：{e}")
    return res


def cut_impact(bd: Path, ledger: dict, recipes: list[dict], gate_limits: dict | None, inputs: list[dict], gate_no: int,
               fluid: str = "清水") -> list[dict]:
    """任务计划第 9 项：输入端 gate_no 断了（流量 0），按设计分析的分配规则（主路先、备路只拿剩的、出口少的先分）重算一遍，
    → 每台少了 fluid 的设备 [{name, x, y, before, after, rate_before, rate_after}]（少得多的在前）。
    整张网按总量算看不出谁先断；按优先级算，备路上的先没水。第一遍要比图标约 40 秒，之后同一据点不到 1 秒。"""
    before = analyze(bd, ledger, recipes, gate_limits, inputs)
    cut = [dict(m, rate=0) if m.get("no") == gate_no else m for m in inputs]
    after = analyze(bd, ledger, recipes, gate_limits, cut)
    am = {(r["device"], r["x"], r["y"]): r for r in after["devices"]}
    out = []
    for r in before["devices"]:
        a = am.get((r["device"], r["x"], r["y"]))
        if a is None or fluid not in r["need"]:
            continue
        b0, b1 = r["got"].get(fluid, 0.0), a["got"].get(fluid, 0.0)
        if b1 + 1e-6 < b0:
            out.append({"name": r["device"], "x": r["x"], "y": r["y"], "before": round(b0, 3), "after": round(b1, 3),
                        "need": r["need"][fluid], "rate_before": r["rate"], "rate_after": a["rate"]})
    out.sort(key=lambda u: u["after"] - u["before"])
    return out


# 「配方未知」「没在用」（审计第 3 项，2026-11-19）排在「没料」前面：_state_key 按前缀找、找不到才归没料，这两种不能被算成没料
STATE_COLORS = [("满速", (40, 170, 60)), ("间歇", (240, 150, 0)), ("溢流位", (140, 140, 140)), ("没接上", (200, 0, 200)),
                ("配方未知", (90, 120, 170)), ("没在用", (190, 190, 190)), ("没料", (220, 30, 30))]
NO_RATE = ("配方未知", "没在用")  # 这两种行的开工率没算（记 0.0 只为了是数字），图上写「?」


def _state_key(state: str) -> str:
    return next((k for k, _ in STATE_COLORS if state.startswith(k)), "没料")


def summary(res: dict) -> dict:
    """按状态数台数，外加推断连线条数、依据含推断的设备、要稳定环境的配方、改过的配方。"""
    cnt = {k: 0 for k, _ in STATE_COLORS}
    for d in res["devices"]:
        cnt[_state_key(d["state"])] += 1
    return {"states": cnt, "devices": len(res["devices"]), "inferred_links": len(res.get("inferred_links", [])),
            "via_inferred": sum(1 for d in res["devices"] if d.get("via_inferred")),
            "recipe_inferred": sum(1 for d in res["devices"] if d.get("recipe_inferred")),
            "env_required": sum(1 for d in res["devices"] if d.get("env_required")), "recipe_fixes": len(res.get("recipe_fixes", [])),
            # 提醒：暗管配对缺失；标在暗管出口上的输入端没用上、标了两个、几口合计（0.10.13 功能二），有「暗管来料」卡的挂在卡上，这里只放没卡的
            "warnings": [n["msg"] for n in res.get("notes", []) if isinstance(n, dict) and (
                n.get("kind") == "暗管配对缺失" or (n.get("kind") in TUN_NOTE_KINDS and not n.get("on_card")))]}


def image(canvas, res):
    """设计分析图：每台设备一个圈，颜色 = 状态（绿满速 / 橙间歇 / 红没料 / 紫没接上 / 灰溢流位 / 灰蓝配方未知 / 浅灰没在用），旁边写开工率
    （配方未知、没在用写「?」）；
    来料路径里有推断连线的画虚线外圈。"""
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    pil = Image.fromarray(cv2.cvtColor((canvas * 0.6 + 100).clip(0, 255).astype(np.uint8), cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    try:
        F = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 15)
    except OSError:
        F = ImageFont.load_default()
    cols = dict(STATE_COLORS)
    for d in res["devices"]:
        x, y = d["c"]
        col = cols[_state_key(d["state"])]
        r = 20
        dr.ellipse([x - r, y - r, x + r, y + r], outline=col, width=5)
        if d.get("via_inferred"):
            for a in range(0, 360, 30):
                dr.arc([x - r - 7, y - r - 7, x + r + 7, y + r + 7], a, a + 15, fill=(30, 90, 220), width=3)
        txt = ("?" if _state_key(d["state"]) in NO_RATE else f"{d['rate']:.0%}") + ("*" if d.get("recipe_inferred") else "")
        dr.text((x + r + 3, y - 9), txt, fill=col, font=F, stroke_width=2, stroke_fill=(255, 255, 255))
    for lk in res.get("inferred_links", []):
        if lk.get("path"):
            for q in lk["path"]:
                dr.rectangle([q[0] - 4, q[1] - 4, q[0] + 4, q[1] + 4], outline=(30, 90, 220), width=2)
    y = 8
    for k, c in STATE_COLORS:
        dr.ellipse([8, y, 24, y + 16], outline=c, width=4)
        dr.text((30, y - 1), k, fill=c, font=F, stroke_width=2, stroke_fill=(255, 255, 255))
        y += 22
    dr.arc([8, y, 24, y + 16], 0, 300, fill=(30, 90, 220), width=3)
    dr.text((30, y - 1), "蓝虚圈：来料经过推断连线；* 配方是推断的", fill=(30, 90, 220), font=F, stroke_width=2, stroke_fill=(255, 255, 255))
    return pil
