"""反应池 / 扩容反应池同时跑几条配方：格数、配方组合、并组、开工率分配、候选打分（纯函数，审计第 3 项，2026-11-19）。

规则来源：
- 官方：池子能同时跑几条配方，各自 2 秒一份；所有在跑配方涉及的不同物品种类 ≤ 5 格（反应池）/ 8 格（扩容反应池），
  同一条配方不能跑两份（官方教程：液化息壤 + 壤晶废液 + 壤晶三条正好占满 8 格）。
- 推断（只当警告、不当硬条件）：池内没有别的配方吃的产物得送出去，管出口 2 个各选一种液体、带出口 1 个选择器只出一种固体；
  要从外面进的流体原料 ≤ 2 种（2 个管入口，一根管一种流体）。
- 推断：几条配方共用一种原料（清水）、送来的不够时，按各条满速用量的比例分（chan_rates）。

配方都用文字（「清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）」，和 pool_channels.recipe_text、design._machine_recipes 一样）。
首尾相接的几条（一条的产物是另一条的原料）在池内自产自用，按 pool_channels 并成一条净配方（groups）。
这里只依赖 pool_channels 和配方库，不 import design（design 反过来用这里）。
"""
from __future__ import annotations

import itertools
from functools import lru_cache

import pool_channels as PC

SLOTS = {"反应池": 5, "扩容反应池": 8}  # 格数（官方）
MAX_RECIPES = {"反应池": 2, "扩容反应池": 3}  # 枚举候选时最多几条：反应池 3 条、扩容池 4 条按格数一种都放不下（combos.txt）
OUT_FLUID, OUT_SOLID, IN_FLUID = 2, 1, 2  # 管出口选择器 2 个、带出口选择器 1 个、管入口 2 个（推断）


def is_fluid(item: str) -> bool:
    """走管子的东西（液体、气体）。固体只走带子。design._is_fluid 用的就是这条。"""
    return "气" in item or "液" in item or item in ("清水", "污水", "沉积酸")


@lru_cache(maxsize=None)
def _by_text(machine: str) -> dict:
    """这种池子的配方：文字 → recipes.json 的 dict（按配方表顺序）。"""
    recipes, _ = PC._db()
    return {PC.recipe_text(r): r for r in recipes if r["machine_name"] == machine and r.get("seconds")}


def recipes(machine: str) -> list[str]:
    """这种池子能跑的全部配方（文字）。不是池子 → []。"""
    return list(_by_text(machine)) if machine in SLOTS else []


def _io(raw) -> tuple[set, set]:
    """几条原始配方的原料名、产物名（并集）。认不出的文字跳过。"""
    _, names = PC._db()
    db = {}
    for m in SLOTS:
        db.update(_by_text(m))
    ins, outs = set(), set()
    for t in dict.fromkeys(raw):
        r = db.get(t)
        if r:
            ins |= {names.get(k, k) for k in r["inputs"]}
            outs |= {names.get(k, k) for k in r["outputs"]}
    return ins, outs


def items_of(raw) -> set:
    """几条原始配方用到的物品种类（占的格数）：按原始配方去重，中间产物只算一种。
    液化息壤 + 壤晶废液两条是 6 种（中间产物液化息壤占一格），不是净配方的 5 种。"""
    ins, outs = _io(raw)
    return ins | outs


def terminal_io(raw) -> dict:
    """外部原料（池内不产、要从外面进的）和终端产物（池内没有别的配方吃、得送出去的），按流体 / 固体分开。"""
    ins, outs = _io(raw)
    ext, term = ins - outs, outs - ins
    return {"ext_fluid": {k for k in ext if is_fluid(k)}, "ext_solid": {k for k in ext if not is_fluid(k)},
            "term_fluid": {k for k in term if is_fluid(k)}, "term_solid": {k for k in term if not is_fluid(k)}}


def check(machine: str, raw) -> dict:
    """几条原始配方能不能同时放进这台池子 → {ok, items（种数）, names（物品）, slots, why[], warn[]}。
    ok=False 的硬条件：不是池子、配方不是这种池子的、同一条写了两遍、物品种类超格。警告（推断，不挡）：终端液体 > 2、
    终端固体 > 1、外部流体原料 > 2（菜籽油 + 饱食饲料两种固体产物只有一个带出口选择器）。"""
    raw = list(raw)
    slots = SLOTS.get(machine)
    why, warn = [], []
    if slots is None:
        return {"ok": False, "items": 0, "names": [], "slots": None, "why": [f"{machine}不是反应池"], "warn": []}
    mine = _by_text(machine)
    bad = [t for t in raw if t not in mine]
    if bad:
        why.append(f"不是{machine}的配方：{'、'.join(bad)}")
    if len(set(raw)) < len(raw):
        why.append("同一条配方写了两遍")
    if not raw:
        why.append("一条配方都没有")
    names = items_of(raw)
    if len(names) > slots:
        why.append(f"物品 {len(names)} 种，超过 {slots} 格")
    io = terminal_io(raw)
    if len(io["term_fluid"]) > OUT_FLUID:
        warn.append(f"要送出去的液体 {len(io['term_fluid'])} 种（{'、'.join(sorted(io['term_fluid']))}），管出口只有 {OUT_FLUID} 个（推断）")
    if len(io["term_solid"]) > OUT_SOLID:
        warn.append(f"要送出去的固体 {len(io['term_solid'])} 种（{'、'.join(sorted(io['term_solid']))}），带出口只能选 {OUT_SOLID} 种（推断）")
    if len(io["ext_fluid"]) > IN_FLUID:
        warn.append(f"要从外面进的流体 {len(io['ext_fluid'])} 种（{'、'.join(sorted(io['ext_fluid']))}），管入口只有 {IN_FLUID} 个（推断）")
    return {"ok": not why, "items": len(names), "names": sorted(names), "slots": slots, "why": why, "warn": warn}


@lru_cache(maxsize=None)
def combos(machine: str, strict: bool = False, n: int | None = None) -> tuple:
    """这种池子放得下的配方组合（每个是原始配方文字的元组，按配方表顺序）。n=None：1 条到 MAX_RECIPES 条；给 n 只枚举 n 条。
    strict=True 时警告也当放不下。对照 combos.txt：反应池 1 条 11、2 条 16（strict 13）；扩容池 2 条 55（38）、3 条 63（34）。"""
    rs = recipes(machine)
    sizes = [n] if n is not None else range(1, MAX_RECIPES.get(machine, 0) + 1)
    out = []
    for k in sizes:
        for c in itertools.combinations(rs, k):
            ck = check(machine, c)
            if ck["ok"] and not (strict and ck["warn"]):
                out.append(c)
    return tuple(out)


def panel_recipes(machine: str, text: str) -> list[str]:
    """面板「池产物：芽针溶液(50)/锦草溶液(50)」→ 这台池子配置的原始配方（文字，按面板顺序；只看数量 > 0 的格，
    和 pool_channels.from_text 的 positive_only 一样：应龙关扩容反应池 (949,731) 的液化重息壤(0) 不算在跑）。
    from_text 返回的是并好的净配方，这里要的是并之前的那几条（groups 再按首尾相接并组，members 才有内容）。"""
    name_id = PC._name_id()
    ids = [name_id[a] for a, _ in PC.slot_items(text, True) if a in name_id]
    return [PC.recipe_text(r) for r in PC.channels(machine, ids)]


def groups(machine: str, raw) -> list[dict]:
    """几条原始配方 → 并行的几组 [{recipe: 这组的净配方文字, members: [组成它的原始配方…]}]。
    首尾相接的并成一组（pool_channels.chains / composite：液化息壤 + 壤晶废液 + 壤晶三条并成一条净配方，members 3 条），
    互不相干的各自一组（芽针溶液、锦草溶液两组）。文字认不出 → ValueError（先过 check）。
    组内按配方表顺序并：净配方的文字和给的先后无关（壤晶废液、液化息壤两条先给哪条，并出来都是「清水×1 + 息壤×1 + 污水×1 → …」，
    和面板池产物那条路 pool_channels.from_text 一致），作者存的配方和面板读到的才比得上。
    组和组之间按给的先后排：第一组是设计分析的主配方（武陵城反应池 (1357,1332) 面板先列芽针溶液，主配方就还是芽针那条）。"""
    mine = _by_text(machine)
    bad = [t for t in raw if t not in mine]
    if bad:
        raise ValueError(f"不是{machine}的配方：{'、'.join(bad)}")
    first = {}
    for k, t in enumerate(raw):
        first.setdefault(t, k)
    pos = {id(r): k for k, r in enumerate(mine.values())}
    gs = [sorted(g, key=lambda r: pos[id(r)]) for g in PC.chains([mine[t] for t in first])]
    gs.sort(key=lambda g: min(first[PC.recipe_text(r)] for r in g))
    return [{"recipe": PC.recipe_text(g[0] if len(g) == 1 else PC.composite(g)), "members": [PC.recipe_text(r) for r in g]} for g in gs]


@lru_cache(maxsize=None)
def members_of(machine: str, net: str) -> tuple:
    """一条净配方由哪几条原始配方首尾相接并成（界面配方列悬停显示组成用，2026-11-19）。本身就是原始配方 → (它,)；对不上 → ()。
    单条池子的行上只存净配方、不存组成（并行池子的 channels 才带 members）：别人的武陵城扩容反应池 (1570,1196) 作者勾了
    液化息壤、壤晶废液、壤晶三条，行上只有并好的「清水×1 + 息壤×1 + 污水×1 + … → …」，作者看不出是哪三条。
    在放得下的组合里找并出来正好一组、净配方文字一样的那个（groups 组内按配方表顺序并，文字和勾的先后无关）。"""
    if net in _by_text(machine):
        return (net,)
    for c in combos(machine):
        if len(c) > 1:
            g = groups(machine, c)
            if len(g) == 1 and g[0]["recipe"] == net:
                return tuple(g[0]["members"])
    return ()


def chan_rates(needs: list[dict], sup: dict, caps: list | None = None) -> tuple[list[float], dict]:
    """并行的几条配方各跑多少（0–1）。needs[c] = 第 c 条满速每分钟要的原料，sup = 到手的料，caps[c] = 这条的出料上限（None 不限）。
    1. f_c = min(1, cap_c, 这条独有原料的 到手/用量)；
    2. 共用原料 k（≥2 条要）：dem_k = Σ need_c[k]·f_c，scale_k = min(1, 到手/dem_k)（dem_k≈0 时 1）——不够时按各条用量比例分（推断）；
    3. t_c = f_c × 这条用到的共用原料里最小的 scale。
    → (t 列表, {共用原料: scale})。例：芽针、锦草两条各要清水 30，清水到手 30 → (0.5, 0.5)；
    一条独有原料只够 0.2、清水到手 45 → (0.2, 1)，清水只用掉 0.2×30 + 30 = 36（另一条不会因为前一条缺料白占清水）。"""
    caps = caps or [None] * len(needs)
    cnt = {}
    for nd in needs:
        for k in nd:
            cnt[k] = cnt.get(k, 0) + 1
    shared = {k for k, n in cnt.items() if n >= 2}
    f = []
    for nd, cap in zip(needs, caps):
        own = [sup.get(k, 0.0) / v for k, v in nd.items() if k not in shared and v > 0]
        f.append(min([1.0, 1.0 if cap is None else cap] + own))
    scale = {}
    for k in shared:
        dem = sum(nd.get(k, 0.0) * fc for nd, fc in zip(needs, f))
        scale[k] = 1.0 if dem <= 1e-9 else min(1.0, sup.get(k, 0.0) / dem)
    t = [fc * min([scale[k] for k in nd if k in scale] or [1.0]) for nd, fc in zip(needs, f)]
    return t, scale


def score(raw, links: list[dict]) -> tuple[float, list[str]]:
    """按物流图上池子每根连线通到的东西给一个配方组合打分（分值照 scratchpad 试验 score_pools.py，推断）。
    links 每项：{layer: "pipe"/"belt", dir: "in"/"out"/"?", sup: 这根线能送来的物品, dem: 会吃掉的物品,
                 wild_in: 来源里有物品不明的（暗管、没认准的取货口）, wild_out: 去处里有物品不明的（没认准的存货口）}。
    每根线：进来的是组合的外部原料 / 出去的是终端产物 +2；只对得上中间产物 +1；只能按物品不明解释 +0.5；解释不了 −1。
    组合要的外部原料没有线送、终端产物没有线收（同一层也没有物品不明的线）各 −1.5；每多一条配方 −0.1。→ (分, 缺什么)"""
    ins, outs = _io(raw)
    term, ext = outs - ins, ins - outs
    s, why = -0.1 * len(list(raw)), []
    fed, taken = set(), set()
    for L in links:
        fl = L["layer"] == "pipe"
        e = {x for x in ext if is_fluid(x) == fl}
        t = {x for x in term if is_fluid(x) == fl}
        allin = {x for x in ins if is_fluid(x) == fl}
        allout = {x for x in outs if is_fluid(x) == fl}
        hit_in = (e & set(L["sup"])) if L["dir"] in ("in", "?") else set()
        hit_out = (t & set(L["dem"])) if L["dir"] in ("out", "?") else set()
        if hit_in or hit_out:
            s += 2; fed |= hit_in; taken |= hit_out
        elif L["dir"] in ("in", "?") and allin & set(L["sup"]):
            s += 1; fed |= allin & set(L["sup"])
        elif L["dir"] in ("out", "?") and allout & set(L["dem"]):
            s += 1; taken |= allout & set(L["dem"])
        elif (L.get("wild_in") and L["dir"] in ("in", "?") and e) or (L.get("wild_out") and L["dir"] in ("out", "?") and t):
            s += 0.5
        else:
            s -= 1
    wild_in = {L["layer"] for L in links if L.get("wild_in") and L["dir"] in ("in", "?")}
    wild_out = {L["layer"] for L in links if L.get("wild_out") and L["dir"] in ("out", "?")}
    for x in sorted(ext - fed):
        if ("pipe" if is_fluid(x) else "belt") not in wild_in:
            s -= 1.5; why.append(f"缺{x}")
    for x in sorted(term - taken):
        if ("pipe" if is_fluid(x) else "belt") not in wild_out:
            s -= 1.5; why.append(f"{x}没去处")
    return s, why


def decide(scored: list[tuple[float, tuple]]):
    """[(分, 组合)…] → 定得下来的那个组合：第一名 > 0 且领先第二名 ≥ 1；定不了 → None。"""
    if not scored:
        return None
    lst = sorted(scored, key=lambda t: -t[0])
    s2 = lst[1][0] if len(lst) > 1 else float("-inf")
    return lst[0][1] if lst[0][0] > 0 and lst[0][0] - s2 >= 1 else None
