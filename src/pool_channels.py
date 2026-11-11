"""反应池多通道配方。

游戏里反应池能同时跑几条配方（IndustrialPlanner：反应池 mix_pool_1 4 条通道、扩容反应池 mix_pool_2 8 条，
同一条配方不能占两条通道）。面板右栏「池产物」列的是这几条配方用到的物品，不全是产物：
- 应龙关扩容反应池 (949,731)：液化息壤 / 重息壤 / 液化重息壤，其中重息壤是「沉积酸 + 重息壤 → 液化重息壤」的原料；
- 武陵城扩容反应池 (1489,1055)：惰性壤晶废液 / 息壤 / 壤晶废液，其中息壤是「清水 + 息壤 → 液化息壤」的原料，
  这台池子自产自用：一条做液化息壤，另一条马上拿液化息壤 + 污水做壤晶废液 + 惰性壤晶废液。
  （2026-11-10 断水实测：断了输入端 13，简报清水理论消耗少 90、液化息壤理论产量少 90；原来只认一条配方，算成「不用清水」）

channels() 把面板列的物品还原成配置的那几条配方；chains() 把首尾相接（一条的产物是另一条的原料）的通道归成一组，
composite() 把一组并成一条「净」配方（中间产物在池内自产自用、抵掉）。
"""
from __future__ import annotations

import re


def _db():
    from diagnose import ID_NAME, RECIPES
    return RECIPES, ID_NAME


def _name_id() -> dict[str, str]:
    """显示名 → 物品 id；同名的（系统蓝图 sysbp_* 也叫「息壤」）取 item_ 开头的。"""
    _, ID_NAME = _db()
    out: dict[str, str] = {}
    for k, v in ID_NAME.items():
        if v not in out or (k.startswith("item_") and not out[v].startswith("item_")):
            out[v] = k
    return out


def slot_items(text: str, positive_only: bool = False) -> list[tuple[str, int]]:
    """「池产物：A(0)/B(50)/C(50)；池内：…」→ [(显示名, 数量)]（只看池产物那段）。"""
    seg = (text or "").split("；")[0].split("：", 1)[-1]
    out = [(a.strip(), int(n)) for a, n in re.findall(r"([^：/()（；]+)\((\d+)", seg)]
    return [(a, n) for a, n in out if n > 0] if positive_only else out


def channels(machine: str, item_ids: list[str]) -> list[dict]:
    """池子面板列的物品 id → 这台池子配置的配方（配方表 dict，按物品出现顺序、去重）。
    1. 物品是这种池子某条配方的产物：只有一条就是它；几条（污水既是壤晶那条、也是赫铜那条的产物）时取另一个产物也在面板上的那条；
    2. 物品不是任何配方的产物、只是一条配方的原料（息壤、重息壤）：就是那条。认不出的物品跳过。"""
    RECIPES, _ = _db()
    mine = [r for r in RECIPES if r["machine_name"] == machine and r.get("seconds")]
    have = set(item_ids)
    out: list[dict] = []
    for iid in item_ids:
        prod = [r for r in mine if iid in r["outputs"]]
        if len(prod) > 1:
            prod = [r for r in prod if have & (set(r["outputs"]) - {iid})] or [r for r in prod if have & set(r["inputs"])]
        if not prod:
            prod = [r for r in mine if iid in r["inputs"]]
        if len(prod) == 1 and prod[0] not in out:
            out.append(prod[0])
    return out


def chains(recs: list[dict]) -> list[list[dict]]:
    """首尾相接的通道归成一组（一条的产物是另一条的原料）；互不相干的各自一组。"""
    groups = [[r] for r in recs]
    merged = True
    while merged:
        merged = False
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                ga, gb = groups[a], groups[b]
                oa = {o for r in ga for o in r["outputs"]}
                ia = {i for r in ga for i in r["inputs"]}
                ob = {o for r in gb for o in r["outputs"]}
                ib = {i for r in gb for i in r["inputs"]}
                if oa & ib or ob & ia:
                    groups[a] = ga + gb
                    del groups[b]
                    merged = True
                    break
            if merged:
                break
    return groups


def composite(group: list[dict]) -> dict:
    """几条同时跑的通道并成一条净配方：每分钟原料/产物相加，中间产物（既产又耗）抵掉，只剩净缺口或净余量。
    所有通道周期相同时（池子配方都是 2 秒）按每周期写，否则按 60 秒写每分钟的量。"""
    secs = {r["seconds"] for r in group}
    sec = secs.pop() if len(secs) == 1 else 60
    k = {id(r): sec / r["seconds"] for r in group}
    ins: dict[str, float] = {}
    outs: dict[str, float] = {}
    for r in group:
        for i, n in r["inputs"].items():
            ins[i] = ins.get(i, 0) + n * k[id(r)]
        for o, n in r["outputs"].items():
            outs[o] = outs.get(o, 0) + n * k[id(r)]
    for x in set(ins) & set(outs):
        m = min(ins[x], outs[x])
        ins[x] -= m
        outs[x] -= m
    ins = {i: n for i, n in ins.items() if n > 1e-9}
    outs = {o: n for o, n in outs.items() if n > 1e-9}
    return {"id": "pool+" + "+".join(r["id"] for r in group), "machine_name": group[0]["machine_name"],
            "inputs": ins, "outputs": outs, "seconds": sec, "parts": [r["id"] for r in group]}


def recipe_text(r: dict) -> str:
    _, ID_NAME = _db()

    def side(d):
        return " + ".join(f"{ID_NAME.get(i, i)}×{n:g}" for i, n in d.items())
    return f"{side(r['inputs'])} → {side(r['outputs'])}（{r['seconds']:g}秒）"


def from_text(machine: str, text: str, positive_only: bool = True) -> list[dict]:
    """「池产物：…」文字 → 配置的配方组：[净配方]（每组一条；只有一条通道的组就是那条配方本身）。
    positive_only：只看数量 > 0 的格（设计分析一直这样：数量 0 的格常是没在跑的通道，如应龙关的液化重息壤）。"""
    name_id = _name_id()
    ids = [name_id[a] for a, _ in slot_items(text, positive_only) if a in name_id]
    groups = chains(channels(machine, ids))
    return [g[0] if len(g) == 1 else composite(g) for g in groups]
