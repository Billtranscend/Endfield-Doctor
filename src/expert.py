"""排查树：按基建维修群的人工思路，只用简报逐级判断「先看什么、低了去哪、查哪类设备」。

用户 2026-11-07 的顺序（错题本编号见 playbook.json）：
  0. 电量（只有巡检读到顶部电量框时才有）：用电顶到上限 → #3 核心旁放热能池。
  1. 息壤：天有洪炉上限 12 台 × 30 = 360/分。没爆仓又明显低 → 先看惰气（散布机要 6 只给 5 → 5/6 降速），
     再分缺碳块（#3 应龙关芽针-碳块产线）还是缺水（水泵/水管）。
  2. 灼铜块：3 台固气转化机 × 30 = 90。低了按 #8 顺序：8.1 息壤气没全部接入（7.1 两项核查）→ 8.2 气态赫铜不够
     （左侧气体反应炉的水和酸：一个准入口一个泵）→ 8.3 左上角提纯机有杂质。
  3. 灼铜零件：3 台配件机 × 6 = 18，吃 90 灼铜块。灼铜块正常它却低 → 配件机这段。
  4. 重息壤：固气转化机 × 12。低了按 #7 顺序：7.1 息壤气 → 7.2 分离芯库存见底 → 惰气 → 7.3 右侧提纯机有杂质。
  5. 中容武陵电池：目标 14.7（传送带优先级版）。按 #3：库存 0 = 大停电，先启动产线 → 蓝矿/致密源石粉末理论 0 → 矿机电线
     → 壤晶不够 = 息壤有问题（回第 1 步）→ 都正常 → 景玉谷封装机。
  6. 其余 playbook.targets 的产物按目标比。
每一项「确认不是爆仓再自查」（错题本 #7/#8）：仓库满了产量由下游决定，不看产量；但气的缺口是真问题，满仓也报。
阈值不手填：每台的产率从配方表算（产出 × 60 / 秒），台数 = 基准理论 / 产率（基准 = 参考简报，多次合并取最大）。
流体没有库存、当前产 = 当前耗（按需），所以流体「不够」要同时满足：当前低于理论，且低于基准当前值；或理论比基准少。

每一步返回 {key, product, status: ok|bad|skip|info, title, facts[], cause, where: {areas, machines, checks}, advice}；
status=bad 的步骤可以直接发起定向巡检（只去 where.areas、只点 where.machines）。子检查命中时 where 收窄到那一段。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, asdict

import blueprint as bp
from diagnose import FULL_RATIO, NAME_IDS, RECIPES

UNDER = 0.95  # 当前 / 理论 低于这个比例算降速
THEO_UNDER = 0.95  # 理论 / 基准理论 低于这个比例算有设备没在算
FLUID_REF = 0.9  # 流体当前值低于基准当前值的这个比例才算真少（流体按需，理论常比当前高）
LOW_STOCK = 2000  # 原料库存低于这个数算「快见底」：洪炉一分钟吃 360 碳块，几千的库存撑不了几分钟
POWER_FULL = 0.95  # 用电 / 上限 达到这个比例算顶满（错题本 #3：超过 9800 → 放热能池）


@dataclass
class Step:
    key: str
    product: str
    title: str
    status: str = "ok"  # ok / bad / skip / info
    facts: list[str] = field(default_factory=list)
    cause: str = ""
    where: dict = field(default_factory=dict)  # {"areas": [...], "machines": [...], "checks": bool}
    advice: str = ""


# 分支定义：产物 → 主机型、默认要去的分区和要点开的设备类型、是否跑错题本定点核查。
# needs：上一步没 ✓ 时这一步先跳过（上游的问题会连带拖低它）。子检查（refine_*）命中时会把 where 收窄。
BRANCHES = [
    {"product": "息壤", "machine": "天有洪炉", "areas": ["景玉谷", "武陵城"], "machines": ["天有洪炉", "气体散布机", "水泵"], "checks": False, "env_gas": "惰气"},
    {"product": "灼铜块", "machine": "固气转化机", "areas": ["应龙关"], "machines": ["固气转化机", "气体反应炉"], "checks": True, "needs": "息壤"},
    {"product": "灼铜零件", "machine": "配件机", "areas": ["应龙关"], "machines": ["配件机"], "checks": False, "needs": "灼铜块"},
    {"product": "重息壤", "machine": "固气转化机", "areas": ["应龙关"], "machines": ["固气转化机", "提纯机"], "checks": True, "needs": "息壤"},
    {"product": "中容武陵电池", "machine": "封装机", "areas": ["景玉谷"], "machines": ["封装机"], "checks": False},
]


def rate_of(product: str, machine: str) -> float | None:
    """配方表里 machine 做 product 的产率（个/分）。多条配方取最大。"""
    ids = set(NAME_IDS.get(product, []))
    best = None
    for r in RECIPES:
        if r["machine_name"] != machine:
            continue
        for o, q in r["outputs"].items():
            if o in ids and r.get("seconds"):
                v = q * 60 / r["seconds"]
                best = v if best is None or v > best else best
    return best


def env_gas_issue(cur: dict, ref: dict, gas: str) -> tuple[list[str], list[str]]:
    """环境气（惰气等）的缺口，分两种返回 (断气, 少用)：
      断气：理论消耗比基准少 = 有散布机的准入口被关/断开（每个准入口 6）。这是确定的故障信号（故障 C：150→144）。
      少用：当前消耗低于理论。可能是散布机没吃饱（6 只给 5 → 用它的设备按 5/6 降速），也可能是用气的洪炉在停等
           （息壤满仓、下游爆仓消耗降了，洪炉不转就不用气）——这是结果不是原因，只能在息壤没满仓时当证据。
    用户 2026-11-07 纠正：14:41–17:32 息壤 ~275/360、惰气当前 136/理论 150，是下游爆仓带下来的，旧蓝图没断过惰气。"""
    g, gr = cur.get(gas), ref.get(gas)
    if not g:
        return [], []
    cut, low = [], []
    if gr and (gr.get("theo_cons") or 0) > (g.get("theo_cons") or 0):
        cut.append(f"{gas} 理论消耗 {gr['theo_cons']}→{g['theo_cons']}：差 {gr['theo_cons'] - g['theo_cons']} = 有散布机的准入口被关/断开（每个准入口 6）")
    if (g.get("theo_cons") or 0) and (g.get("cur_cons") or 0) < g["theo_cons"] * UNDER:
        ratio = g["cur_cons"] * 100 // g["theo_cons"]
        low.append(f"{gas} 当前消耗 {g['cur_cons']} 只有理论 {g['theo_cons']} 的 {ratio}%：散布机没吃饱，用它的设备按比例降速")
    return cut, low


def fluid_short(cur: dict, ref: dict, name: str) -> str | None:
    """流体是否真的不够，返回一句依据；够或简报里没有返回 None。
    流体没有库存、按需生产：只有「理论比基准少」（有生产它的设备没在算）或「当前低于理论且低于基准当前值」才算不够，
    单看当前 < 理论会误报（2026-11-07 正常状态下息壤气 286/370）。"""
    f, fr = cur.get(name), ref.get(name)
    if not f:
        return None
    theo, curp = f.get("theo_prod") or 0, f.get("cur_prod") or 0
    rt, rc = (fr or {}).get("theo_prod") or 0, (fr or {}).get("cur_prod") or 0
    if rt and theo < rt * THEO_UNDER:
        return f"{name} 理论 {theo} 低于基准 {rt}：生产它的设备有的没在算"
    if theo and curp < theo * UNDER and rc and curp < rc * FLUID_REF:
        return f"{name} 当前 {curp}，理论 {theo}、基准当前 {rc}：供不上"
    return None


def rows_by_name(rows: list[dict]) -> dict[str, dict]:
    """固体行按名称索引（灌装瓶另算）；流体行 stock 为 None。"""
    out = {}
    for r in rows or []:
        if r.get("fluid"):
            continue
        out.setdefault(r["name"], r)
    return out


def cap_of(rows: list[dict]) -> int:
    stocks = sorted((r["stock"] for r in rows or [] if r.get("stock") is not None), reverse=True)
    if len(stocks) >= 2 and stocks[0] > 2 * stocks[1]:
        stocks = stocks[1:]
    return max(10000, math.ceil(max(stocks, default=0) / 10000) * 10000)


def fmt(r: dict | None) -> str:
    if not r:
        return "简报里没有"
    stock = "流体" if r.get("stock") is None else f"库存 {r['stock']}"
    return f"当前 产{r['cur_prod']}/耗{r['cur_cons']}，理论 产{r['theo_prod']}/耗{r['theo_cons']}，{stock}"


def parse_power(power: str | None) -> tuple[int, int] | None:
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)", power or "")
    if not m or not int(m.group(2)):
        return None
    return int(m.group(1)), int(m.group(2))


# ---- 子检查：一项 ✗ 之后按错题本顺序往下找是哪一段。返回 (原因, where, 建议) ----

def refine_xirang(cur: dict, ref: dict, full) -> tuple[str, dict, str]:
    """息壤降了、惰气没问题：缺碳块还是缺水。清水在简报里不可靠（野外水泵、闲置水泵都算进理论），所以看碳块：
    碳块快见底 → 缺碳块（#3 应龙关芽针-碳块产线）；碳块充足 → 缺水。"""
    c = cur.get("碳块")
    if c and c.get("stock") is not None and not full(c) and c["stock"] < LOW_STOCK:
        return (f"碳块库存只剩 {c['stock']}（{fmt(c)}）：洪炉缺碳块",
                {"areas": ["应龙关"], "machines": ["精炼炉", "种植机", "水泵"], "checks": False},
                "#3 缺碳块 → 应龙关芽针-碳块产线：误删传送带/管道、管道没接好、缺水（2 台水泵）")
    stock = f"碳块库存 {c['stock']}" if c and c.get("stock") is not None else "碳块简报里没有"
    return (f"惰气正常、{stock} 不缺 → 洪炉缺的是水",
            {"areas": ["景玉谷", "武陵城"], "machines": ["天有洪炉", "水泵"], "checks": False},
            "#6 景玉谷 6 水泵：没接电 / 水管分流 / 水管中断；#3 武陵城缺水 → 水管重连，连好了还缺 → 武陵城全删重摆。"
            "简报里的清水含野外水泵、闲置水泵，不能拿来判断，要点开洪炉看缺水")


def refine_zhuotong(cur: dict, ref: dict, full) -> tuple[str, dict, str]:
    """灼铜块降了：错题本 #8 顺序。气体反应炉 = 气态赫铜×2 + 息壤气 → 气态灼铜，气态赫铜来自提纯机。"""
    why = fluid_short(cur, ref, "息壤气")
    if why:
        return (f"8.1 {why}：气体反应炉的息壤气没全部接入",
                {"areas": ["应龙关"], "machines": ["气体反应炉", "固气转化机"], "checks": True},
                "#8.1 同 #7.1：禁区和雪松林的高纯/低纯全部接到应龙关左下角两个息壤气准入口；右下角单口暗管准入口 30/分跑满、上方暗管恒定 2/秒")
    why = fluid_short(cur, ref, "气态赫铜")
    if why:
        return (f"8.2 {why}：左侧气体反应炉吃不饱",
                {"areas": ["应龙关"], "machines": ["气体反应炉", "提纯机", "水泵"], "checks": False},
                "#8.2 左侧气体反应炉不工作 → 水和酸是否接入（一个准入口一个泵）")
    return ("8.1 息壤气、8.2 气态赫铜都够 → 8.3 左上角提纯机可能进了杂质",
            {"areas": ["应龙关"], "machines": ["提纯机"], "checks": False},
            "#8.3 点开左上角提纯机看原料格有没有配方外的物品（巡检会标「入口堆着…」）")


def refine_zhongxirang(cur: dict, ref: dict, full) -> tuple[str, dict, str]:
    """重息壤降了：错题本 #7 顺序。重息壤气来自提纯机（分离芯 + 息壤气）。"""
    why = fluid_short(cur, ref, "息壤气")
    if why:
        return (f"7.1 {why}",
                {"areas": ["应龙关"], "machines": ["固气转化机", "提纯机"], "checks": True},
                "#7.1 禁区和雪松林的 7 高纯 1 低纯全部接到应龙关左下角的两个息壤气准入口（20 级及以下 5 高纯 3 低纯）；"
                "右下角单口暗管准入口 30/分跑满、上方暗管恒定 2/秒")
    s = cur.get("分离芯")
    if s and s.get("stock") is not None and s["stock"] < LOW_STOCK and not full(s):
        return (f"7.2 分离芯库存只剩 {s['stock']}：提纯机缺分离芯",
                {"areas": ["武陵城", "景玉谷"], "machines": ["气体散布机", "封装机"], "checks": False},
                "#7.2 检查武陵城和景玉谷的惰气是否接对且充足")
    return ("7.1 息壤气、7.2 分离芯都没问题 → 7.3 右侧提纯机可能进了杂质",
            {"areas": ["应龙关"], "machines": ["提纯机"], "checks": False},
            "#7.3 点开右侧提纯机看原料格有没有配方外的物品（巡检会标「入口堆着…」）")


def refine_zhongrong(cur: dict, ref: dict, full, xirang_ok: bool, areas_all: list[str]) -> tuple[str, dict, str]:
    """中容降了：错题本 #3 顺序。封装机 = 致密源石粉末×20 + 壤晶×5 → 中容（10 秒）。"""
    for ore in ("蓝铁矿", "致密源石粉末"):
        o = cur.get(ore)
        if o and (o.get("theo_prod") or 0) == 0 and ((ref.get(ore) or {}).get("theo_prod") or 0) > 0:
            return (f"3.2.2.1 {ore} 理论生产 0（基准 {ref[ore]['theo_prod']}）：矿机没在工作",
                    {"areas": areas_all, "machines": ["电驱矿机", "二型电驱矿机"], "checks": False},
                    "#3.2.2.1 矿机不工作 → 电线重连")
    rj = cur.get("壤晶")
    if rj and not full(rj) and (rj.get("stock") or 0) < LOW_STOCK:
        if not xirang_ok:
            return (f"壤晶库存只剩 {rj['stock']}，上面息壤也没 ✓：中容低是息壤带下来的",
                    {"areas": ["景玉谷", "武陵城"], "machines": ["天有洪炉", "气体散布机", "水泵"], "checks": False},
                    "#3.2.2.2 先按息壤那一步修，息壤好了中容会跟着回来")
        return (f"壤晶库存只剩 {rj['stock']}：封装机缺壤晶",
                {"areas": ["景玉谷"], "machines": ["封装机"], "checks": False},
                "看壤晶的来路和封装机进料")
    return ("矿、壤晶都不缺 → 景玉谷封装机这段",
            {"areas": ["景玉谷"], "machines": ["封装机"], "checks": False},
            "点开景玉谷的封装机看是否在做中容、进料是否到位（传送带优先级版：中容走优先带）")


REFINE = {"息壤": refine_xirang, "灼铜块": refine_zhuotong, "重息壤": refine_zhongxirang}

CONS_DROP_FLUIDS = ("清水",)  # 源头流体：理论消耗比基准少 = 有用它的设备停了（不在理论值里）


def _ledger_users(plan: bp.Blueprint, areas: list[str], fid: str, outs: set[str]) -> dict[str, list[str]]:
    """各分区底账 + 配方表里，配方吃 fid、且（直接或池内串联）做 outs 里某样东西的设备：分区 → ["扩容反应池 (1489,1055)", …]。
    反应池按面板列的全部通道算（pool_channels）。配方表没有的分区返回空。"""
    import pool_channels as PC
    out: dict[str, list[str]] = {}
    for a in areas:
        f = plan.dir / "ledger" / f"{a}_recipes.json"
        if not f.exists():
            continue
        try:
            devs = json.loads(f.read_text(encoding="utf-8")).get("devices", [])
        except ValueError:
            continue
        for d in devs:
            rc = d.get("recipe") or ""
            if "池产物" in rc:
                parts = PC.channels(d["name"], [PC._name_id()[x] for x, _ in PC.slot_items(rc) if x in PC._name_id()])
                hit = any(fid in r["inputs"] for r in parts) and any(set(r["outputs"]) & outs for r in parts)
            else:
                hit = any(r["machine_name"] == d["name"] and fid in r["inputs"] and set(r["outputs"]) & outs
                          and f"{PC._db()[1].get(next(iter(r['outputs'])), '')}×" in rc for r in RECIPES)
            if hit:
                out.setdefault(a, []).append(f"{d['name']} ({d['x']},{d['y']})")
    return out


def _dropped(cur: dict, ref: dict, iid: str) -> bool:
    """物品 iid 的理论产量比基准少（简报里没有这一行的不管，算「没反证」）。"""
    from diagnose import ID_NAME
    name = ID_NAME.get(iid, iid)
    r, rr = cur.get(name), ref.get(name)
    if not r or not rr:
        return True
    return (rr.get("theo_prod") or 0) > (r.get("theo_prod") or 0)


def fid_of(fluid: str) -> str:
    fids = NAME_IDS.get(fluid, [])
    return next((i for i in fids if i.startswith("item_")), next(iter(fids), ""))


def _pools_stalled_by_full(plan: bp.Blueprint, areas: list[str], fid: str, full_name) -> list[str]:
    """底账配方表里吃 fid 的反应池，池里有产物已爆仓的：「应龙关 扩容反应池 (949,731)：壤晶 爆仓」。
    多配方池子一个产物满了整池停（[[endfield-pool-channels]]，群友 H 2026-11-11：壤晶爆仓，液化息壤理论 150→120）。"""
    import pool_channels as PC
    out = []
    for a in areas:
        f = plan.dir / "ledger" / f"{a}_recipes.json"
        if not f.exists():
            continue
        try:
            devs = json.loads(f.read_text(encoding="utf-8")).get("devices", [])
        except ValueError:
            continue
        for d in devs:
            rc = d.get("recipe") or ""
            if "池产物" not in rc:
                continue
            ids = [PC._name_id()[x] for x, _ in PC.slot_items(rc) if x in PC._name_id()]
            parts = PC.channels(d["name"], ids)
            if not any(fid in r["inputs"] for r in parts):
                continue
            names = sorted({PC._db()[1].get(o, o) for r in parts for o in r["outputs"]})
            full = [n for n in names if full_name(n)]
            if full:
                out.append(f"{a} {d['name']} ({d['x']},{d['y']})：{'、'.join(full)} 爆仓")
    return out


def fluid_cons_drop(cur: dict, ref: dict, fluid: str, plan: bp.Blueprint, areas_all: list[str], full=None) -> Step | None:
    """源头流体（清水）的理论消耗比基准少：游戏里缺任一原料（或被关、断电）的设备不计入理论值，所以少多少 ≈ 停了几台用它的设备。
    再看同时「理论产量比基准少」、配方吃这种流体的产物，把范围收窄到做它的设备和所在分区。
    （2026-11-10 断水实测：断了武陵城输入端 13 往扩容反应池那段，清水理论消耗 3210→3120、液化息壤理论产量 150→60，
    排查树原来全是 ✓，靠人工比简报才找到反应池）"""
    r, rr = cur.get(fluid), ref.get(fluid)
    if not r or not rr:
        return None
    if full is None:
        cap = cap_of(list(cur.values()))
        full = lambda n: bool(cur.get(n)) and cur[n].get("stock") is not None and cur[n]["stock"] >= cap * FULL_RATIO  # noqa: E731
    drop = (rr.get("theo_cons") or 0) - (r.get("theo_cons") or 0)
    fids = set(NAME_IDS.get(fluid, []))
    per = sorted({q * 60 / x["seconds"] for x in RECIPES if x.get("seconds") for i, q in x["inputs"].items() if i in fids})
    unit = 30.0 if 30.0 in per else (per[0] if per else 30.0)
    st = Step(key=f"tree:{fluid}理论消耗", product=fluid, title=f"{fluid}理论消耗", facts=[fmt(r), f"基准理论消耗 {rr.get('theo_cons')}"],
              where={"areas": [], "machines": [], "checks": False})
    if drop < unit * 0.9:
        st.cause = f"理论消耗 {r.get('theo_cons')}，和基准 {rr.get('theo_cons')} 差不多：用{fluid}的设备都在算"
        return st
    st.status = "bad"
    st.cause = (f"理论消耗比基准少 {drop}：约 {drop / unit:.0f} 台用{fluid}的设备（{unit:g}/分一台）停了、没在算"
                f"——产物爆仓整池停、缺别的原料、被关或断电，或者缺{fluid}（水管断了/准入口被关），它们都不出现在理论值里")
    stalled = _pools_stalled_by_full(plan, areas_all, fid_of(fluid), full)
    if stalled:
        st.facts.append("这些反应池有产物已爆仓，多配方池子一个产物满了整池停、吃" + fluid + "的那条也跟着停（不是缺" + fluid + "）："
                        + "；".join(stalled))
    machines, outs, names = set(), set(), []
    for name, row in cur.items():
        rrow = ref.get(name) or {}
        d = (rrow.get("theo_prod") or 0) - (row.get("theo_prod") or 0)
        if d <= 0 or name == fluid:
            continue
        pids = set(NAME_IDS.get(name, []))
        # 配方的其他产物理论产量没少的不算（精炼炉 清水 → 赤铜块 + 污水：污水少了是反应池那头，赤铜块没少说明精炼炉都在算）
        rs = [x for x in RECIPES if pids & set(x["outputs"]) and fids & set(x["inputs"]) and all(_dropped(cur, ref, o) for o in x["outputs"])]
        if rs:
            machines |= {x["machine_name"] for x in rs}
            outs |= pids
            names.append(f"{name} 理论产量 {rrow.get('theo_prod')}→{row.get('theo_prod')}（少 {d}）")
    fid = next((i for i in fids if i.startswith("item_")), next(iter(fids), ""))
    if names:
        st.facts.append("同时少了、配方吃" + fluid + "的产物：" + "；".join(names))
        users = _ledger_users(plan, areas_all, fid, outs)
        st.where = {"areas": [a for a in areas_all if a in users] or areas_all, "machines": sorted(machines), "checks": False}
        if users:
            st.facts.append("底账里做它们、吃" + fluid + "的设备：" + "；".join(f"{a} {'、'.join(v)}" for a, v in users.items()))
        st.advice = (f"定向巡检 {'、'.join(st.where['areas'])} 的 {'、'.join(st.where['machines'])}（会一起看这些分区的{fluid}输入端），"
                     f"看哪台空着、为什么空着。{fluid}输入端显示「阻塞」= 水是满的、不缺，是设备自己停了（产物爆仓、被关）；"
                     f"设备空闲、原料格缺{fluid}才是水管断了")
    else:
        st.where = {"areas": areas_all, "machines": [], "checks": False}
        st.advice = f"简报看不出是哪种产物的设备停了：定向巡检各分区的{fluid}输入端和用{fluid}的设备"
    return st


def run(brief: dict, plan: bp.Blueprint, region: str | None = None, power: str | None = None) -> list[dict]:
    """对一个地区（默认蓝图 areas 的第一个地区）跑排查树。power = 巡检读到的顶部电量框「用电/上限」（快速诊断没有）。"""
    region = region or next(iter(plan.areas), None) or next(iter(brief), None)
    rows = brief.get(region) or []
    cur = rows_by_name(rows)
    ref = rows_by_name((plan.reference_brief or {}).get(region) or [])
    cap = cap_of(rows)
    areas_all = [a if isinstance(a, str) else a.get("name") for a in plan.areas.get(region, [])]
    steps: list[Step] = []
    ok_products: set[str] = set()
    targets = {t["product"]: t["per_min"] for t in plan.playbook.get("targets", [])}

    def full(r: dict | None) -> bool:
        return bool(r) and r.get("stock") is not None and r["stock"] >= cap * FULL_RATIO

    # 0. 电量（#3 大停电）
    pw = parse_power(power)
    if pw:
        used, top = pw
        st = Step(key="tree:电量", product="电量", title="电量", facts=[f"用电/上限 {used}/{top}"], where={"areas": [], "machines": [], "checks": False})
        if used >= top * POWER_FULL:
            st.status, st.cause = "bad", f"用电 {used} 顶到上限 {top} 的 {used * 100 // top}%：再加设备就会大停电"
            st.advice = "#3 核心旁放热能池；已经停电了先启动产线（中容电池库存 0 时）"
        else:
            st.cause = f"用电 {used * 100 // top}%，没顶满"
        steps.append(st)

    for b in BRANCHES:
        p = b["product"]
        r, rr = cur.get(p), ref.get(p)
        rate = rate_of(p, b["machine"]) or 0
        st = Step(key=f"tree:{p}", product=p, title=p, where={"areas": b["areas"], "machines": b["machines"], "checks": b["checks"]})
        if r is None:
            st.status, st.cause = "info", "简报里没有这一行"
            steps.append(st)
            continue
        st.facts.append(fmt(r))
        ref_theo = (rr or {}).get("theo_prod") or 0
        n_ref = round(ref_theo / rate, 1) if rate and ref_theo else None
        if n_ref:
            st.facts.append(f"基准理论 {ref_theo}/分 = {n_ref:g} 台{b['machine']} × {rate:g}/分")
        gas_cut, gas_low = env_gas_issue(cur, ref, b["env_gas"]) if b.get("env_gas") else ([], [])
        # 只认「断气」（理论消耗比基准少）。「当前用气低」分不清因果：洪炉满仓停等、缺水、缺碳块停了都会让用气变少
        # （11-06 息壤 300/360、惰气 108/150，根因是景玉谷水泵后暗管断了缺水；11-07 下午 136/150 是下游爆仓），
        # 只作为降速时的旁证交给子检查，由洪炉面板（顶部「无环境」还是缺清水）来分
        gas_issue = gas_cut
        if gas_issue and (r["cur_prod"] or 0) < (r["theo_prod"] or 0) * UNDER:
            # 准入口断了、产量也确实掉了：就算仓库满着也是故障（仓满只是把掉速盖住了）
            st.status = "bad"
            st.cause = f"当前 {r['cur_prod']} 只有理论 {r['theo_prod']} 的 {(r['cur_prod'] or 0) * 100 // max(r['theo_prod'] or 1, 1)}%，且 {b['env_gas']} 有缺口" + ("（仓库虽满，但气的问题是真的）" if full(r) else "")
            st.facts += gas_issue
            st.where = {"areas": b["areas"], "machines": ["气体散布机", b["machine"]], "checks": False}
            st.advice = f"去 {'、'.join(b['areas'])} 看气体散布机的{b['env_gas']}准入口/管道（#6 惰气管中断/接少），再看用它的{b['machine']}"
            steps.append(st)
            continue
        if full(r):
            st.status = "ok"
            st.cause = "已爆仓，产量由下游消耗决定，不看产量"
            if gas_low:
                st.facts.append(f"{b['env_gas']} 当前消耗低于理论、理论消耗和基准一样：是{b['machine']}满仓停等用气少了，不是断气")
            ok_products.add(p)
            steps.append(st)
            continue
        if p == "中容武陵电池" and (r.get("stock") or 0) == 0 and (r.get("cur_prod") or 0) == 0:
            st.status = "bad"
            st.cause = "中容电池库存 0、也没在产：大停电，先启动产线"
            st.where = {"areas": areas_all, "machines": [], "checks": False}
            st.advice = "#3 没电池了先启动产线；用电超过上限 → 核心旁放热能池"
            steps.append(st)
            continue
        if b.get("needs") and b["needs"] not in ok_products:
            st.status, st.cause = "skip", f"先把上一步 {b['needs']} 的问题解决，再看这一项"
            steps.append(st)
            continue
        theo, curp = r["theo_prod"] or 0, r["cur_prod"] or 0
        # 有错题本目标的按 min(理论, 目标) 比：中容理论算 4 台封装机 24，但传送带优先级版设计就只有 14.7
        goal = min(theo, targets[p]) if p in targets and theo else theo
        bad = False
        if ref_theo and theo < ref_theo * THEO_UNDER:
            missing = (ref_theo - theo) / rate if rate else 0
            bad = True
            st.cause = f"理论 {theo} 低于基准 {ref_theo}：约 {missing:.0f} 台{b['machine']}没在算——没原料（缺水/缺料）、断电或被关了，它们不出现在理论值里"
        elif goal and curp < goal * UNDER:
            down = next((b2["product"] for b2 in BRANCHES if b2.get("needs") == p and full(cur.get(b2["product"]))), None)
            if down:
                # 下游产物已爆仓：这一步的产物在线上直接送下游（不进仓库，库存不动），下游停了它就回堵停下，是设计内的
                # （2026-11-08 灼铜零件 21:12 爆仓 → 21:34 起灼铜块/气态灼铜 0/90，灼铜块库存一直 61251；合并诊断判「下游爆仓回堵」）
                st.status = "ok"
                st.cause = f"当前 {curp}/理论 {theo}：下游 {down} 已爆仓，{p} 在线上回堵停等，产量由下游消耗决定"
                ok_products.add(p)
                steps.append(st)
                continue
            bad = True
            st.cause = f"当前 {curp} 只有{'目标' if goal != theo else '理论'} {goal:g} 的 {int(curp * 100 // goal)}%：设备在算但降速"
        if not bad:
            st.status = "ok"
            st.cause = f"当前 {curp} / 理论 {theo}，满速"
            ok_products.add(p)
            steps.append(st)
            continue
        st.status = "bad"
        if p == "中容武陵电池":
            sub = refine_zhongrong(cur, ref, full, "息壤" in ok_products, areas_all)
        elif p in REFINE:
            sub = REFINE[p](cur, ref, full)
        else:
            sub = None
        if sub:
            why, where, advice = sub
            if gas_low and p == "息壤" and "缺碳块" not in why:
                st.facts += gas_low
                why = f"碳块不缺；{b['env_gas']}当前用量也低——缺水或缺气都会这样（洪炉停了用气就少），简报分不开"
                where = {"areas": b["areas"], "machines": ["天有洪炉", "气体散布机", "水泵"], "checks": False}
                advice = ("点开天有洪炉：面板顶部「无环境」= 气体散布机缺惰气（#6 惰气管中断/接少）；原料格缺清水 = 缺水（#6 景玉谷 6 水泵："
                          "没接电/水管分流/水管中断；#3 武陵城水管重连，还缺就全删重摆）。定向巡检会读这两样")
            st.facts.append(why)
            st.where, st.advice = where, advice
        else:
            st.advice = f"去 {'、'.join(b['areas'])} 点开 {'、'.join(b['machines'])} 的面板" + ("，并跑错题本 7.1 两项核查" if b["checks"] else "")
        steps.append(st)

    # 源头流体理论消耗下降（断水）：产物都爆仓/满速时前面各步全是 ✓，断水只在理论消耗上露出来
    for fl in CONS_DROP_FLUIDS:
        st = fluid_cons_drop(cur, ref, fl, plan, areas_all, lambda n: full(cur.get(n)))
        if st is not None:
            steps.append(st)

    # 其余 playbook 目标
    done = {s.product for s in steps}
    hints = {h["product"]: h["text"] for h in plan.playbook.get("hints", [])}
    for t in plan.playbook.get("targets", []):
        p = t["product"]
        if p in done:
            continue
        r = cur.get(p.split("(")[0])
        st = Step(key=f"tree:{p}", product=p, title=p, where={"areas": areas_all, "machines": [], "checks": False})
        if r is None:
            st.status, st.cause = "info", "简报里没有这一行"
        else:
            st.facts.append(fmt(r))
            goal = t["per_min"]
            if full(r):
                st.status, st.cause = "ok", "已爆仓，按设计停等"
            elif (r["cur_prod"] or 0) >= goal * 0.9:
                st.status, st.cause = "ok", f"当前 {r['cur_prod']} ≥ 目标 {goal}"
            else:
                st.status, st.cause = "bad", f"当前 {r['cur_prod']} < 目标 {goal}（理论 {r['theo_prod']}）"
                st.advice = hints.get(p, "") or "按错题本对应条目排查"
        steps.append(st)
    return [asdict(s) for s in steps]


def to_text(steps: list[dict]) -> list[str]:
    mark = {"ok": "✓", "bad": "✗", "skip": "→", "info": "?"}
    out = ["## 排查树（按人工检修顺序，只用简报）"]
    for s in steps:
        out.append(f"- {mark.get(s['status'], '?')} **{s['title']}**：{s['cause']}")
        for f in s["facts"]:
            out.append(f"  - {f}")
        if s["status"] == "bad":
            w = s["where"]
            out.append(f"  - 去哪：{'、'.join(w.get('areas', [])) or '—'}；看什么：{'、'.join(w.get('machines', [])) or '按错题本'}" + ("；跑 7.1 核查" if w.get("checks") else ""))
            if s.get("advice"):
                out.append(f"  - 建议：{s['advice']}")
    return out


if __name__ == "__main__":  # 开发期：python src\expert.py debug\brief_xxx.json [电量 7215/9800]
    import sys
    from pathlib import Path
    from diagnose import DEBUG
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else max(DEBUG.glob("brief_*.json"), key=lambda p: p.stat().st_mtime)
    brief = json.loads(path.read_text(encoding="utf-8"))
    plan = bp.load(None)
    print("\n".join(to_text(run(brief, plan, power=sys.argv[2] if len(sys.argv) > 2 else None))))
