"""按蓝图预览里设备机身中间的产物图标定配方（离线，不进游戏）。

为什么要有：设计分析要知道每台设备的配方。皇帝产线的配方表（档案 ledger/<据点>_recipes.json）是开发期间巡检点面板攒出来的，
别的蓝图没有这张表，设计分析一台都算不了（2026-11-13 issue #1 的作者，武陵城「共 0 台」）。
预览里每台生产设备机身中间画着它的产物（带彩色环的圆图标）；同一种机器能做的东西有限，认出产物多数就定了配方。

做法（derive）：
  1. 底账每台设备换到拼合图上，在它附近找圆（半径 11–18、里面有深色底），取最近的。
  2. 圆里的图标缩到 23×23，和 AKEData 物品图标比（logistics.ake_candidates），候选只取 data/recipes.json 里这种机器能做的产物。
     拼合图是按基地朝向转过的，图标跟着转了：按这台设备所在蓝图的旋转角转回来再比。
  3. 一种产物只有一条配方 → 就是它。有几条（天有洪炉做息壤：碳块 / 稳定碳块；精炼炉做碳块：六种植物）→ 看这个据点里有哪种原料
     （别的设备产的、作者填的输入端）；只差「要不要稳定环境」的两条（提纯机）→ 据点里有气体散布机就取要环境的那条。
  4. 没把握的标「推断」（inferred）：图标分数低或和第二名差得少、同产物的配方挑不出来。设计分析会再按物流图上的邻居重挑（design._infer_by_neighbors）。

2026-11-14 用皇帝产线三个据点的真配方表比（128 台有标准配方的）：产物认对 117 台；认错 9 台里 6 台是灌装机
（装了液体的瓶子在配方数据里和空瓶同名、图标也一样，要靠接的是哪种液体），3 台是长得像的（低容 / 中容武陵电池、
芽针种子 / 琼叶参种子、息壤气 / 重息壤气）。只在皇帝产线用到的物品上验证过。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from . import vision as V

SURE_SCORE = 0.55  # 图标和产物的相似度 ≥ 这个
SURE_MARGIN = 0.25  # 且比第二像的产物高出这么多，才算认准了。0.15 时景玉谷的低容武陵电池认成中容（0.81 对 0.63）还算「认准」；
# design._recipes_by_icon 早就因为这一对用的 0.25。认不准的照样给配方，只是标「推断」
WIN, NEAR = 46, 32  # 拼合图 px：在底账位置周围多大范围找圆；圆心离底账位置多远以内才算这台设备的
ENV_MACHINE = "气体散布机"  # 据点里有它，才可能有「要稳定环境」的配方


_RAW = None


def raw_items() -> set[str]:
    """没有任何配方能做出来的物品（矿石等）：只能从仓库取货口进来，哪个据点都可能有。"""
    global _RAW
    if _RAW is None:
        from . import design as D
        D._pool_recipe("", "")
        made = {k for _, _, outs, _, _ in D._RECIPES for k in outs}
        _RAW = {k for _, ins, _, _, _ in D._RECIPES for k in ins} - made
    return _RAW


def machine_rows(machine: str) -> list[dict]:
    """data/recipes.json 里这种机器的全部配方 → [{text, ins, outs, env}]（text 的写法同 design._machine_recipes）。"""
    from . import design as D
    D._pool_recipe("", machine)  # 载入 D._RECIPES
    out = []
    for m, ins, outs, sec, env in D._RECIPES:
        if m == machine and sec:
            out.append({"text": " + ".join(f"{k}×{v:g}" for k, v in ins.items()) + " → " +
                        " + ".join(f"{k}×{v:g}" for k, v in outs.items()) + f"（{sec:g}秒）",
                        "ins": dict(ins), "outs": dict(outs), "env": int(env or 0)})
    return out


def body_icon(img: np.ndarray, gray: np.ndarray, cx: float, cy: float) -> tuple[np.ndarray | None, float]:
    """设备机身中间的产物图标 → (23×23 图标 或 None, 圆心离 (cx, cy) 多远)。
    flow.icon_disks 是给仓库口用的：气体产物的图标带彩色环、下面有色条，它不认（三个据点 38 台找不到）。"""
    from .design import _icon23
    H, W = gray.shape
    x0, y0 = int(max(0, cx - WIN)), int(max(0, cy - WIN))
    sub = gray[y0:int(min(H, cy + WIN)), x0:int(min(W, cx + WIN))]
    if sub.shape[0] < 30 or sub.shape[1] < 30:
        return None, 999.0
    c = cv2.HoughCircles(cv2.medianBlur(sub, 3), cv2.HOUGH_GRADIENT, dp=1, minDist=18, param1=80, param2=16, minRadius=11, maxRadius=18)
    best = None
    for x, y, r in ([] if c is None else c[0]):
        m = np.zeros(sub.shape, np.uint8)
        cv2.circle(m, (int(x), int(y)), max(1, int(r) - 3), 1, -1)
        inner = sub[m > 0]
        if not inner.size or float((inner < 110).mean()) < 0.15:
            continue  # 里面没有深色底：端口的半圆、供电桩的圈
        dist = float(np.hypot(x + x0 - cx, y + y0 - cy))
        if dist <= NEAR and (best is None or dist < best[0]):
            best = (dist, (float(x + x0), float(y + y0), float(r)))
    if best is None:
        return None, 999.0
    return _icon23(img, best[1]), best[0]


def rank_products(icon: np.ndarray, products: set[str], rots: list[int]) -> list[tuple[str, float]]:
    """图标像哪种产物 → [(产物名, 分数)]，分数高的在前。rots：这台设备所在蓝图的旋转角（拼合时顺时针转了这么多度），
    转回来再比；不确定是哪张蓝图时给几个角度，每种产物取最高的分。"""
    from . import logistics as LG
    best: dict[str, float] = {}
    for rot in rots:
        up = np.ascontiguousarray(np.rot90(icon, (rot // 90) % 4))  # np.rot90 是逆时针：把顺时针转过的转回来
        for c in LG.ake_candidates(up, top=999):
            if c["name"] in products and c["score"] > best.get(c["name"], -9.0):
                best[c["name"]] = float(c["score"])
    return sorted(best.items(), key=lambda kv: -kv[1])


def pick_recipe(rows: list[dict], product: str, available: set[str], has_env: bool,
                producers: dict[str, list[dict]] | None = None) -> tuple[dict | None, bool, str]:
    """这种机器做 product 的配方里挑一条 → (配方行, 挑得准不准, 说明)。
    只有一条：就是它。几条：按原料打分，分最高的那条，并列就算挑不准——
      据点里有设备产的原料 2 分；矿石这类哪条配方都做不出来的原料 1 分（只能从仓库来，哪个据点都可能有）；别的 0 分。
      producers（物品 → 据点里产它的配方）给了的话：原料只由「吃这个产物」的设备产（蓝铁块 → 蓝铁粉末 → 蓝铁块 绕一圈）不算有，0 分。
      武陵城精炼炉做蓝铁块：蓝铁矿（矿石 1 分）对蓝铁粉末（粉碎机拿蓝铁块磨的，绕圈 0 分）→ 蓝铁矿；
      景玉谷精炼炉做碳块：芽针（种植机产的 2 分）对原木（1 分）→ 芽针。
    原料一样、只差要不要稳定环境的（提纯机：分离芯×1 / ×2）：据点里有气体散布机先取要环境的，但标不准——
    应龙关 5 台提纯机离散布机都是 85–106 px，3 台用不要环境的、2 台用要环境的，靠位置分不出来。"""
    rs = [r for r in rows if product in r["outs"]]
    if not rs:
        return None, False, ""
    if len(rs) == 1:
        return rs[0], True, ""
    raw = raw_items()
    have = available | raw

    def worth(k):
        if k in available:
            ps = (producers or {}).get(k)
            if ps and all(product in q["ins"] for q in ps):
                return 0  # 只有吃这个产物的设备在产它：绕圈
            return 2
        return 1 if k in raw else 0

    hit = [sum(worth(k) for k in r["ins"]) for r in rs]
    top = [r for r, h in zip(rs, hit) if h == max(hit)]
    note = f"做{product}的配方有 {len(rs)} 条"
    if len(top) > 1:
        env = [r for r in top if bool(r["env"]) == has_env]
        if len(env) == 1 and len({frozenset(r["ins"]) for r in top}) == 1:
            return env[0], False, note + f"，原料一样、只差要不要稳定环境：据点里{'有' if has_env else '没有'}{ENV_MACHINE}，先取{'要' if has_env else '不要'}环境的"
        if 0 < len(env) < len(top):
            top = env
    if len(top) == 1 and max(hit) > 0:
        return top[0], True, note + "，按据点里有的原料（" + "、".join(k for k in top[0]["ins"] if k in have) + "）挑的"
    return top[0], False, note + "，按原料分不出来，先取一条"


def _rots_at(tf: dict, bp_dir: Path | None, fx: float, fy: float) -> list[int]:
    """这个远景位置落在哪几张蓝图的范围里 → 它们的旋转角。读不到蓝图尺寸、或哪张都不在：全部蓝图的旋转角（去重）。"""
    from .placement import to_preview
    bps = tf.get("bps") or {}
    every = sorted({int(b.get("rot", 0)) for b in bps.values()}) or [0]
    if len(every) == 1 or bp_dir is None:
        return every
    got = set()
    for name, b in bps.items():
        try:
            size = json.loads((bp_dir / name / "size.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return every
        if not size:
            return every
        px, py = to_preview(b, fx, fy)
        if -40 <= px <= size[0] * V.CELL + 40 and -40 <= py <= size[1] * V.CELL + 40:
            got.add(int(b.get("rot", 0)))
    return sorted(got) or every


def derive(bd: Path, ledger: dict, known: list[dict] | None = None, inputs: list[dict] | None = None) -> list[dict]:
    """据点目录 bd（composite.png、transform.json）+ 底账 → 每台认得出产物的设备一条
    {name, x, y, recipe, inferred, how, product, score, margin}。known：已经有配方的设备（档案的配方表），不再认，
    它们的产物算作据点里有的物品。inputs：作者填的输入端（[{item, ...}]），它们的物品也算据点里有的。"""
    from . import design as D
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    img = V.rd(bd / "composite.png")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    bp_dir = bd.parent.parent / "bp"
    bp_dir = bp_dir if bp_dir.is_dir() else None
    have = {(d["name"], d["x"], d["y"]) for d in known or [] if "→" in (d.get("recipe") or "")}
    available = {m["item"] for m in inputs or [] if m.get("item")}
    for d in known or []:
        available |= set(D.outputs_of(d.get("recipe") or ""))
    devices = ledger.get("devices", [])
    has_env = any(d["name"] == ENV_MACHINE for d in devices)
    seen = []
    for d in devices:
        if (d["name"], d["x"], d["y"]) in have:
            continue
        rows = machine_rows(d["name"])
        if not rows:
            continue  # 仓库口、暗管、供电桩、池子以外没有配方的东西
        cx, cy = V.far_to_canvas(tf, d["x"], d["y"])
        if not (0 <= cx < W and 0 <= cy < H):
            continue
        icon, dist = body_icon(img, gray, cx, cy)
        if icon is None:
            continue
        ranked = rank_products(icon, {k for r in rows for k in r["outs"]}, _rots_at(tf, bp_dir, d["x"], d["y"]))
        if not ranked:
            continue
        seen.append((d, rows, ranked))
    producers = defaultdict(list)
    for d, rows, ranked in seen:  # 一种产物只有一条配方的先定下来：它们的产物是别的设备的原料
        one = [r for r in rows if ranked[0][0] in r["outs"]]
        if len(one) == 1:
            available |= set(one[0]["outs"])
            for k in one[0]["outs"]:
                producers[k].append(one[0])
    out = []
    for d, rows, ranked in seen:
        product, score = ranked[0]
        margin = score - (ranked[1][1] if len(ranked) > 1 else -1.0)
        row, picked, note = pick_recipe(rows, product, available, has_env, producers)
        if row is None:
            continue
        sure_icon = score >= SURE_SCORE and margin >= SURE_MARGIN
        how = f"机身图标是{product}（{score:.2f}" + (f"，第二像 {ranked[1][0]} {ranked[1][1]:.2f}" if len(ranked) > 1 else "") + "）"
        out.append({"name": d["name"], "x": d["x"], "y": d["y"], "recipe": row["text"], "inferred": not (sure_icon and picked),
                    "how": how + ("；" + note if note else ""), "product": product, "score": round(score, 3), "margin": round(margin, 3)})
    return out
