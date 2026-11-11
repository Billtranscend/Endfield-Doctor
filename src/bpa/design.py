"""设计分析计算器（任务计划第 3 项；docs/蓝图分析器-设计.md 4.7）。

从第 2 节的输入端往下游推，给每台设备一条记录：设计开工率、预期状态、依据。
输入（都在据点目录 base/<据点>/ 或蓝图档案里，离线）：
- flow.json：物流件之间、物流件 ↔ 设备的有向连接（第 4 项），局部结构（单进单出汇流器、分流回环…）；
- 输入端（pipenet.load_inputs：作者填的优先，清水 60/口）；准入口限速、限制通过数量（档案 ledger/gate_limits/<据点>.json）；
- 底账设备 + 配方（ledger/<据点>.json、<据点>_recipes.json，pipenet.recipe_table 补推断）。

模型（第一版，只按规则，不做逐帧模拟）：
- 来源：输入端准入口（作者流速；开了限速取 min；开了限制通过数量且已放满 → 0）；仓库取货口（每条带 30/分）；
  生产设备的产物（产量 × 它自己的开工率，迭代几轮）。暗管出口的量来自配对的暗管入口，第一版记「不知道」。
- 准入口：放行 min(来量, 限速)，物品不对的不放行。
- 分流器：在接着的出口之间平分；某路下游吃不下（满了）多出来的分给其他路（水位分配）。
  经过「单进单出汇流器」的那一路是备路：主路都吃饱了才分给它（4.7 优先级）。绕回自己上游的那一路（分流回环）照样分走一份、不回到设备（4.7：设备拿到 L/n）。
- 汇流器：各路相加，封顶带子 30 / 管道 120。
- 设备：每种原料拿到的 ÷ 满速用量，取最小 = 设计开工率；燃料最低需求（散布机、固气/液气转化机 6/分）不够 → 罢工。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import vision as V

BELT_CAP = 30.0
PIPE_CAP = 120.0
FUEL_MIN = {"气体散布机": 6.0, "固气转化机": 6.0, "液气转化机": 6.0}
MERGE_T = ("汇流器", "管道汇流器")
RATE_LIMITED_INTAKE = True  # 设备只按「另一种原料够跑多快」吃这种原料（False：每种原料各吃各的，用来算「能送到多少」）
BACKPRESSURE = True  # 2026-11-10 背压：产物全部去向都是确定的设备、吃不完时，生产者跟着减速（主路顶住 → 多出来的走备路）
MAIN_BLOCKED = False  # 第三遍（情景）：设备出料口的主路全堵死，看备路上的设备能拿到多少（塑形机出料堵住时精炼炉溢流到配件机）
BUFFERS = ("协议储存箱", "储气罐")  # 进来什么就往外出什么（缓冲中转）：景玉谷研磨机 → 储存箱 → 中容封装机（2026-11-09）
SOURCE_PORTS = ("仓库取货口", "暗管出口", "暗管入口", "多口暗管出口", "多口暗管入口")  # 往外出带子/管子的：仓库、暗管来料（物品和量看不到，按满带、到设备按缺口分）
EPS = 1e-6


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


def _infer_by_neighbors(flow, devices, dev_canvas, rec, marks, depth=4, alt=None) -> list[dict]:
    """配方是推断出来的设备（配方表里没有：没读到 / 池子只记了池产物）：按物流图上相邻的、配方可信的设备重挑。
    从这台设备顺着连线（不管方向；穿过物流件和储存箱，不穿过别的生产设备）找邻居：邻居产的东西可能是原料，邻居吃的东西可能是产物；
    输入端准入口的物品（作者填的清水等）也算原料。每个候选配方：原料有人产 +1，产物有人吃 +1；唯一最高分且高过现在的才换。
    2026-11-10 武陵城：粉碎机 (1572,1292) 配方表里没有，按本据点最常见的猜成砂叶 → 砂叶粉末；它下面接着芽针种植机，
    其实是芽针 → 芽针粉末（→ 储存箱 → 反应池做芽针溶液，作者说那台反应池游戏里在转）。"""
    def dev_of(dv):
        best, bd_ = None, 1e9
        for i, (d, c) in enumerate(zip(devices, dev_canvas)):
            if d["name"] == dv["name"]:
                dist = float(np.hypot(c[0] - dv["c"][0], c[1] - dv["c"][1]))
                if dist < bd_:
                    best, bd_ = i, dist
        return ("d", best) if best is not None and bd_ < 3 else None
    adj = defaultdict(set)
    for lk in flow["links"]:  # 只看物流图的连线，不管方向（在建图之前做：方向没定的带子要靠改好的配方定方向）
        a = dev_of(lk["a_dev"]) if lk.get("a_dev") else (("n", lk["a"]) if lk.get("a") is not None else None)
        b = dev_of(lk["device"]) if lk.get("device") else (("n", lk["b"]) if lk.get("b") is not None else None)
        if a and b and a != b:
            adj[a].add(b); adj[b].add(a)
    gate_item = {}
    for m in marks or []:
        if m.get("item"):
            for n in flow["nodes"]:
                if abs(n["c"][0] - m["p"][0]) <= 20 and abs(n["c"][1] - m["p"][1]) <= 20:
                    gate_item[n["id"]] = m["item"]
    fixes = []
    fixed = set()
    for _round in range(2):
        changed = False
        for i, d in enumerate(devices):
            rc, inferred = rec.get(i, ("", False))
            if not inferred or d["name"] in BUFFERS or d["name"] in PORT_NAMES:
                continue
            cands = _machine_recipes(d["name"])
            if len(cands) < 2:
                continue
            made, eaten = set(), set()
            seen, frontier = {("d", i)}, [("d", i)]
            for _ in range(depth):
                nxt = []
                for u in frontier:
                    for v in adj[u]:
                        if v in seen:
                            continue
                        seen.add(v)
                        if v[0] == "n":
                            if v[1] in gate_item:
                                made.add(gate_item[v[1]])
                            nxt.append(v)
                        elif devices[v[1]]["name"] in BUFFERS:
                            nxt.append(v)
                        elif not rec.get(v[1], ("", True))[1] or v[1] in fixed:  # 配方可信的邻居（含上一轮按邻居改好的）
                            made |= set(outputs_of(rec[v[1]][0]))
                            eaten |= set(inputs_of(rec[v[1]][0]))
                frontier = nxt

            def score(r):
                return len(set(inputs_of(r)) & made) + len(set(outputs_of(r)) & eaten)
            sc = sorted(((score(r), r) for r in cands), reverse=True)
            best, cur = sc[0], score(rc) if rc else 0
            if _INFER_DEBUG and _INFER_DEBUG(d, dev_canvas[i]):
                print("INFER", d["name"], dev_canvas[i], "cur", cur, rc, "made", made, "eaten", eaten, sc[:4])
            # 只换「现在这个推断配方一点邻居支持都没有」的：有支持的不动（反向配方——气→固、粉末→块——原料产物邻居里都有，分数会更高）
            if alt is not None and i not in alt and cur == 0 and len(sc) >= 2 and sc[0][0] == sc[1][0] >= 1                     and (len(sc) < 3 or sc[2][0] < sc[0][0]):
                r1, r2 = sc[0][1], sc[1][1]
                own1, own2 = set(inputs_of(r1)) - set(inputs_of(r2)), set(inputs_of(r2)) - set(inputs_of(r1))
                if own1 & made and own2 & made:  # 两种配方各自的原料都有人送进来：池子两种都做（扩容反应池吃芽针粉末和锦草粉末）
                    fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rc, "to": f"{r1} / 交替 {r2}",
                                  "how": "按相邻设备推断：两种原料都有人送进来，两种配方轮流做"})
                    rec[i] = (r1, True)
                    alt[i] = (r2, "相邻设备送进来两种原料，按两种配方轮流做算（推断）")
                    fixed.add(i)
                    changed = True
                    continue
            reverse = rc and set(inputs_of(best[1])) <= set(outputs_of(rc)) and set(outputs_of(best[1])) <= set(inputs_of(rc))
            if best[0] >= 1 and (len(sc) < 2 or sc[1][0] < best[0]) and cur == 0 and best[1] != rc and not reverse:  # 正反两个方向的配方（固气转化机）靠邻居分不出
                fixes.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": rc, "to": best[1],
                              "how": f"按相邻设备推断（原料有人产/产物有人吃 {best[0]} 项）"})
                rec[i] = (best[1], True)
                fixed.add(i)
                changed = True
        if not changed:
            break
    return fixes


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


def adjacent_links(G, bd, devices, dev_canvas, rec, gap=14, alt=None):
    """紧挨着的两台设备端口直接对接、中间没有带子（种植机 ↔ 采种机等，2026-11-09 景玉谷）：
    两台设备外框相贴（缝 ≤ gap px，外框线各自往里缩几 px，实测相贴的缝 11 px；隔一格约 35 px、相贴边重叠 ≥ 1 格），且一台的产物是另一台的原料 → 加一条推断连接。"""
    import cv2 as _cv
    from .pipenet import _rects, device_rect
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
                items = set(outputs_of(rec[u][0])) & set(inputs_of(rec[v][0]))
                if alt:  # 交替配方的产物 / 原料也算（两种植物轮换的种植机 ↔ 采种机）
                    items |= (set(outputs_of(alt[u][0])) if u in alt else set()) & (set(inputs_of(rec[v][0])) | (set(inputs_of(alt[v][0])) if v in alt else set()))
                    items |= set(outputs_of(rec[u][0])) & (set(inputs_of(alt[v][0])) if v in alt else set())
                eu, ev = (ea, eb) if u == a else (eb, ea)
                gu, gv = (ga, gb) if u == a else (gb, ga)
                if gu.get(eu) == "in" or gv.get(ev) == "out":
                    continue  # 口格折角说这边是进口 / 那边是出口：这个方向不通（武陵城左列种植机 ↔ 采种机原来两个方向都加，成了假循环）
                if items and (("d", u), ("d", v)) not in have:
                    lk = {"layer": "belt", "adjacent": True, "dir": "a->b", "items": sorted(items),
                          "basis": [f"{devices[u]['name']}和{devices[v]['name']}紧挨着、端口直接对接（按配方推断）"]}
                    G.out[("d", u)].append((("d", v), lk)); G.inn[("d", v)].append((("d", u), lk))
                    added.append(lk)
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
            if g is not None and g <= 2.0 * V.CELL and (best is None or g < best[0]):
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


def _icon23(img, disk):
    import cv2 as _cv
    x, y, r = int(disk[0]), int(disk[1]), disk[2]
    h = int(round(r * 0.85))  # 0.72 会切掉图标边上的形状：景玉谷砂叶口 0.46 → 0.77、砂叶粉末口 0.63 → 0.96
    patch = img[max(0, y - h):y + h + 1, max(0, x - h):x + h + 1]
    if patch.shape[0] < 2 * h or patch.shape[1] < 2 * h:
        return None
    return _cv.resize(patch, (23, 23), interpolation=_cv.INTER_AREA)


def _recipes_by_icon(bd: Path, devices: list[dict], dev_canvas: list, rec: dict, recipes: list[dict]) -> list[dict]:
    """设备机身中间的圆图标画着它的产物（2026-11-09）。同类设备在本据点有几种配方时，按图标认产物定配方：
    历史配方是按同名最近配对的（±30 px），挨着的同类设备会配反（景玉谷 (507,581)/(507,640) 两台粉碎机）。
    只在图标离设备 ≤30 px、分数 ≥0.45、领先第二名 ≥0.25 时改；改了的标推断。"""
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
    out = []
    for i, d in enumerate(devices):
        if d["name"] not in multi or not disks:
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
        cs = [c for c in LG.ake_candidates(ic, top=999) if c["name"] in by_out]
        if not cs or cs[0]["score"] < 0.45 or (len(cs) > 1 and cs[0]["score"] - cs[1]["score"] < 0.25):  # 低容/中容电池图标很像，0.58 vs 0.38 也会认错
            continue
        new = by_out[cs[0]["name"]]
        old = rec[i][0]
        if new != old:
            rec[i] = (new, True)
            out.append({"device": d["name"], "x": d["x"], "y": d["y"], "from": old, "to": new,
                        "why": f"机身图标是{cs[0]['name']}（{cs[0]['score']:.2f}）"})
    return out


def port_items(bd: Path, devices: list[dict], dev_canvas: list, rec: dict, G=None) -> dict:
    """仓库口圆图标里画着它存/取的物品（2026-11-09）：圆图标中间那块缩到 23×23，和 AKEData 物品图标比。
    候选只取这个据点配方里出现过的物品（致密源石粉末 / 致密晶体粉末这种长得像的才分得开）。
    仓库只存物品：液体/气体不算候选。连着的设备（G 里直接相连）配方里有的物品优先：图标上压着管子时比对分数很低，
    但「连着固气转化机（赤铜块 → 气态赤铜）」就只剩赤铜块一个候选（应龙关那排红色图标）。
    同样子的图标归一组，组里有一个认得准就整组用它。→ {设备序号: {item, score, sure, how}}"""
    import cv2 as _cv
    from . import flow as F
    from . import logistics as LG
    img = V.rd(bd / "composite.png")
    flow = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
    _, disks = F.icon_disks(img, flow["nodes"])
    cand = set()
    for rc, _ in rec.values():
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
        allc = [c for c in LG.ake_candidates(ic, top=999) if c["state"] == "固"]
        local = set()
        if G is not None:
            for v, _ in G.out[("d", i)] + G.inn[("d", i)]:
                if v[0] == "d":
                    rc = rec[v[1]][0]
                    local |= set(inputs_of(rc)) | set(outputs_of(rc))
        cs_local = [c for c in allc if c["name"] in local]
        cs = [c for c in allc if c["name"] in cand] if cand else []
        lead = (cs[0]["score"] - (cs[1]["score"] if len(cs) > 1 else 0.0)) if cs else 0.0
        if cs and cs[0]["score"] >= 0.5 and lead >= 0.05:  # 图标看得清：只信图标（连着的设备配方可能配错台，会把仓库口也带错）
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
                            eat |= set(inputs_of(rec[v[1]][0]))
                front = nxt
            nobody = not any(found[i]["item"] in inputs_of(rec[j][0]) for j in rec if not rec[j][1])  # 全据点配方可信的设备都不吃它
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


def analyze(bd: Path, ledger: dict, recipes: list[dict], gate_limits: dict | None = None, inputs: list[dict] | None = None) -> dict:
    from .pipenet import load_inputs, recipe_table
    flow = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    devices = ledger["devices"]
    dev_canvas = [V.far_to_canvas(tf, d["x"], d["y"]) for d in devices]
    table = recipe_table(bd, ledger, recipes)
    rec = {i: table.get((d["name"], d["x"], d["y"]), ("", False)) for i, d in enumerate(devices)}
    alt = {}  # 交替配方（液气转化机：两路液体轮流做，2026-11-09 补；配方表 alt 字段）
    for i, d in enumerate(devices):  # 反应池只记「池产物：液化息壤(50)/…」：换成配方表里这种池子做这个产物的配方
        rc = rec[i][0]
        if rc.startswith("池产物"):
            rec[i] = (_pool_recipe(rc, d["name"]) or rc, True)
            second = _pool_recipe(rc, d["name"], 1)
            first = rec[i][0]
            chain = second and (set(outputs_of(first)) & set(inputs_of(second)) or set(outputs_of(second)) & set(inputs_of(first)))
            if second and not chain:  # 池产物同时记了两种有量、互不相干的产物（武陵城反应池：芽针溶液 50 / 锦草溶液 50）：两种配方轮流做；
                # 一种的产物是另一种的原料（壤晶废液 → 壤晶）是两步串联，不算
                alt[i] = (second, "池产物里记了两种产物，按两种配方轮流做算（推断）")
    for q in recipes:
        if q.get("alt"):
            for i, d in enumerate(devices):
                if d["name"] == q["name"] and abs(d["x"] - q["x"]) <= 30 and abs(d["y"] - q["y"]) <= 30:
                    alt[i] = (q["alt"], q.get("note", ""))
    icon_fixes = _recipes_by_icon(bd, devices, dev_canvas, rec, recipes)
    icon_fixes += _infer_by_neighbors(flow, devices, dev_canvas, rec, inputs if inputs is not None else load_inputs(bd), alt=alt)
    plant_notes = _plant_alternation(flow, devices, dev_canvas, rec, alt)
    G = Graph(flow, devices, dev_canvas, rec)
    adjacent_links(G, bd, devices, dev_canvas, rec, alt=alt)
    tpairs = tunnel_pairs(bd, devices, dev_canvas)
    buf_ids = {i for i, d in enumerate(devices) if d["name"] in BUFFERS}
    for a, b in tpairs:  # 暗管入口 → 出口：入口进什么、出口就出什么
        lk = {"layer": "pipe", "tunnel": True, "dir": "a->b", "basis": ["暗管配对（读蓝图时点开确认）"]}
        G.out[("d", a)].append((("d", b), lk)); G.inn[("d", b)].append((("d", a), lk))
        buf_ids |= {a, b}
    import cv2
    gray = cv2.cvtColor(V.rd(bd / "composite.png"), cv2.COLOR_BGR2GRAY)
    _outlet_touch(G, flow, devices, dev_canvas, rec, gray)
    _feed_touch(G, flow, devices, dev_canvas, rec)
    _glyph_dirs(G, flow, devices, dev_canvas, bd)
    _buffer_dirs(G, flow, devices, dev_canvas, rec, alt)
    _orphan_feeds(G, flow, devices, dev_canvas, rec)
    for u in list(G.out):  # 推断出来的连线打标，推料时经过它的路径带上〔推断连线〕
        for v, lk in G.out[u]:
            if _is_inferred(lk):
                lk["inferred"] = True
    pitem = port_items(bd, devices, dev_canvas, rec, G)
    fixes = icon_fixes + _fix_recipes_by_ports(G, devices, rec, recipes, pitem)

    def port_role(i):
        """仓库口是往外出（取货）还是往里收（存货）：物品是连着的设备的原料 → 取；是产物 → 存；都不是看底账名字。"""
        it = pitem.get(i, {})
        item = it.get("item") if it.get("sure") else None
        if item:
            nb = [v for v, _ in G.out[("d", i)] + G.inn[("d", i)] if v[0] == "d"]
            if any(item in inputs_of(rec[v[1]][0]) for v in nb):
                return "取", item
            if any(item in outputs_of(rec[v[1]][0]) for v in nb):
                return "存", item
        nm = devices[i]["name"]
        return ("取" if nm in SOURCE_PORTS else "存" if nm == "仓库存货口" else None), item
    inputs = inputs if inputs is not None else load_inputs(bd)
    gl = {tuple(r["p"]): r for r in (gate_limits or {}).get("rows", [])}
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
    # 每个准入口：物品、限速、是不是输入端
    gate = {}
    for n in nodes:
        if n["type"] != "准入口":
            continue
        g = {"item": n.get("item") or gate_guess.get(n["id"], {}).get("item", ""), "limit": None, "count": None, "input": None,
             "item_how": "作者标" if n.get("item") else gate_guess.get(n["id"], {}).get("how")}
        m = next((m for m in inputs if abs(m["p"][0] - n["p"][0]) <= 15 and abs(m["p"][1] - n["p"][1]) <= 15), None)
        if m:
            g["input"] = m
            g["item"] = m.get("item") or g["item"]
        r = next((r for p, r in gl.items() if abs(p[0] - n["p"][0]) <= 15 and abs(p[1] - n["p"][1]) <= 15), None)
        if r:
            g["read"] = r
            if r.get("limit_on"):
                g["limit"] = r.get("limit")
            if r.get("count_on"):
                g["count"] = {"limit": r.get("count_limit"), "passed": r.get("passed")}
        gate[n["id"]] = g

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

    def dev_need(i):
        rc, _ = rec[i]
        need = inputs_of(rc)
        nm = devices[i]["name"]
        return need, FUEL_MIN.get(nm, 0.0)

    resid = [False]  # True：推料阶段，设备按剩余缺口算
    last_sup = {}     # 上一轮每台设备到手的料（按「另一种原料够跑多快」限这种原料的吃量）
    prev_sup = {}     # 再上一轮：两种原料互相限吃量会来回震荡（塑形机赤铜块 50/惰气 30 ↔ 60/25），取两轮里多的那轮，收敛到上面那个自洽解

    # 能吸收多少（某物品，从节点 u 往下）：设备 = 用量；分流 = 各路之和；汇流/准入口 = 下游；带子/管道封顶
    def absorb(u, item, seen=()):
        if u in seen:
            return float("inf")  # 绕回上游：回环那一路照样分走一份
        if u[0] == "d" and u[1] not in buf_ids:
            need, fuel = dev_need(u[1])
            got_i = sup.get(u[1], {}) if resid[0] else {}  # 推料时按「还差多少」分：多个水口都往同一台送时，灌满的不再多拿（武陵城精炼炉 40/30 vs 20/30）
            if u[1] in alt and item in inputs_of(alt[u[1]][0]):
                return max(0.0, inputs_of(alt[u[1]][0])[item] - got_i.get(item, 0.0))
            if fuel and item and _is_fuel(item, rec[u[1]][0]):
                return max(0.0, fuel - (_fuel_got(got_i, rec[u[1]][0]) if got_i else 0.0))  # 转化机/散布机烧的气：每台最低 6/分（4.7）
            if item and item != "*":
                k_rate = 1.0
                if resid[0] and RATE_LIMITED_INTAKE and item in need:  # 另一种原料只够跑 X%，这种也只吃 X%（灌装机缺赤铜耐压罐时不吃惰气，气改流向塑形机；用户 2026-11-09）
                    others = [min(1.0, max(last_sup.get(u[1], {}).get(k, 0.0), prev_sup.get(u[1], {}).get(k, 0.0)) / v)
                              for k, v in need.items() if k != item and v > 0]
                    if others and last_sup:
                        k_rate = min(others)
                if resid[0]:
                    k_rate = min(k_rate, out_cap.get(u[1], 1.0))  # 产物出不去的设备也吃不了那么多
                return max(0.0, need.get(item, 0.0) * k_rate - got_i.get(item, 0.0))
            return max(0.0, sum(need.values()) - sum(got_i.get(k, 0.0) for k in need))
        kd = G.kind(u)
        tot = 0.0
        for v, lk in G.out[u]:
            tot += min(_cap(lk), absorb(v, item, seen + (u,)))
        if kd == "准入口":
            g = gate.get(u[1], {})
            if g.get("item") and item and item != "*" and g["item"] != item:
                return 0.0
            if g.get("limit") is not None:
                tot = min(tot, g["limit"])
        return tot

    # 来量：从来源往下推（每轮按拓扑顺序；设备产物下一轮才有）
    sup = defaultdict(lambda: defaultdict(float))     # 设备 → 物品 → 每分钟到手
    via = defaultdict(lambda: defaultdict(list))      # 设备 → 物品 → 依据
    # 从「全部满速」开始往下迭代（取最大的自洽解）：种植机 ↔ 采种机这种互相供料的环从 0 开始永远起不来
    dev_rate = {i: 1.0 for i in range(len(devices))}
    out_cap = {}  # 设备 → 产物能出去的比例（背压，上一轮算的）
    alt_split = {i: (0.5, 0.5) for i in alt}  # 交替配方两种各占的开工时间（迭代里按来料更新）
    notes = []

    def push(u, item, q, path, seen):
        """q/分的 item 到了 u，往下分。"""
        if q <= EPS:
            return
        if u[0] == "d" and u[1] not in buf_ids:
            if item == "*":  # 仓库取货口来的、物品没看到：到设备再按缺口分
                star[u[1]].append((q, path))
                return
            sup[u[1]][item] += q
            via[u[1]][item].append(path + [f"{q:g}"])
            return
        if u in seen:
            return  # 回环回到上游：这一份不再回到设备
        seen = seen | {u}
        kd = G.kind(u)
        if kd == "准入口" and u[1] in gate:
            g = gate[u[1]]
            if item == "*" and g.get("item"):
                item = g["item"]
            if g.get("item") and item and g["item"] != item:
                return
            if g.get("limit") is not None and q > g["limit"]:
                path = path + [f"准入口限速 {g['limit']:g}"]
                q = g["limit"]
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
            if MAIN_BLOCKED and tag == "主路" and back and kd == "出料口":
                continue  # 情景：主路堵死，全部走备路
            caps = [min(_cap(lk), absorb(v, item, tuple(seen))) for v, lk in grp]
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
        """几个输入端合计一个总量（作者：应龙关 5、6 号息壤气合计 150，各进多少不限）：按各口下游吃得下的量水位分。"""
        grp = defaultdict(list)
        for nid, g in gate.items():
            m = g.get("input") or {}
            if m.get("pool") and m["pool"].get("total") is not None:
                grp[m["pool"]["name"]].append(nid)
        out = {}
        for name, nids in grp.items():
            total = float(gate[nids[0]]["input"]["pool"]["total"])
            caps = []
            for nid in nids:
                c = absorb(("n", nid), gate[nid].get("item") or None)
                caps.append(PIPE_CAP if c == float("inf") else min(c, PIPE_CAP))
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
                out[nid] = (a, name, total)
        return out

    def sources():
        out = []
        pool = pools()
        for nid, g in gate.items():
            m = g.get("input")
            if not m:
                continue
            if nid in pool:
                a, name, total = pool[nid]
                out.append((("n", nid), g["item"], a, [f"输入端{m.get('no')}号 {g['item']}（{name} 合计 {total:g}，这口分到 {a:g}）"]))
                continue
            rate = m.get("rate")
            try:
                rate = float(rate) if rate not in (None, "") else None
            except ValueError:
                rate = None
            if rate is None and g.get("limit") is not None:
                rate = g["limit"]
            if g.get("count") and g["count"].get("passed") is not None and g["count"]["limit"] is not None \
                    and g["count"]["passed"] >= g["count"]["limit"]:
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
                    outs += [(v, lk) for v, lk in G.inn[u] if v[0] == "d" and pit in inputs_of(rec[v[1]][0])]
                for v, lk in outs:
                    item = pit or _first_gate_item(G, v, gate) or "*"
                    tag = f"{item}，认{how}" if pit else (item if item != "*" else "物品没看到，按设备缺口分")
                    out.append((v, item, _cap(lk), [f"{d['name']}（{tag}）{_cap(lk):g}/分"]))
            elif d["name"] in SOURCE_PORTS and i not in buf_ids:  # 配上对的暗管出口是中转，不当来源
                for v, lk in G.out[u]:
                    item = _first_gate_item(G, v, gate) or "*"
                    out.append((v, item, _cap(lk), [f"{d['name']}（{item if item != '*' else '物品没看到，按设备缺口分'}）{_cap(lk):g}/分"]))
            elif dev_rate.get(i, 0) > EPS:
                rc, _ = rec[i]
                if i in alt and i in alt_split:  # 交替配方：主配方和交替配方各按自己的开工时间出产物（扩容反应池轮流做锦草/芽针溶液）
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
        return out

    prod_out = {}

    def drain(u, item, rate, seen=()):
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
            g = gate.get(u[1], {})
            if g.get("item") and g["item"] != item:
                return 0.0
            if g.get("limit") is not None:
                tot = min(tot, g["limit"])
        return tot

    def _backpressure(rate) -> dict:
        """每台生产设备：每种产物，所有去向都是确定的设备时，下游这一轮最多吃多少 ÷ 满速产量 → 开工率上限。
        几台生产者往同一台下游送时，每台都按下游全部用量算（只会少算堵，不会多算）。交替配方的设备不算。"""
        caps = {}
        for i, d in enumerate(devices):
            rc, _ = rec[i]
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
        return caps

    star = defaultdict(list)
    for it in range(20):
        sup.clear(); via.clear(); star.clear(); notes.clear()
        resid[0] = True
        prod_out.clear()
        srcs = sources()
        for u, item, q, path in srcs:  # 先推只有一个去向的，再分几个出料口的：分的时候已经吃饱的下游不再多给
            if u[0] != "out":          # （应龙关采种机 (349,1161) 原来先各给 15，另一台又单独给 (349,1332) 30，一台 45、一台 15）
                push(u, item, q, path, frozenset())
        # 几个出料口要分的：出口少的先分（背压：出口少的没别处可去，先把自己那几台下游填满；出口多的再分剩下的缺口）。
        # 2026-11-10 武陵城左列：采种机 (326,1463) 两个去向、(361,1123) 三个去向，都通种植机 (361,1294)。
        # 先分三个去向的那台时三台各 20，两台种植机只有 2/3；游戏里 (361,1294) 吃饱后那条带子堵住、种子只流向另两台，理论满速（作者确认）
        for u, item, q, path in sorted((s for s in srcs if s[0][0] == "out"), key=lambda s: len(prod_out[(s[0][1], s[1])])):
            split(prod_out[(u[1], item)], item, q, path, frozenset(), "出料口")
        resid[0] = False
        for i, lst in star.items():  # 仓库带：一条带一种物品，先给还没有来源的原料，再给缺得最多的
            need, _ = dev_need(i)
            for q, path in sorted(lst, key=lambda z: -z[0]):
                if not need:
                    break
                k = min(need, key=lambda k: (sup[i].get(k, 0.0) > EPS, sup[i].get(k, 0.0) / need[k]))
                sup[i][k] += q
                via[i][k].append(path + [f"当作{k}", f"{q:g}"])
        prev_sup.clear()
        prev_sup.update(last_sup)
        last_sup.clear()
        last_sup.update({i: dict(v) for i, v in sup.items()})
        new = {}
        for i, d in enumerate(devices):
            need, fuel = dev_need(i)
            if not need:
                new[i] = 0.0
                continue
            rs = [min(1.0, sup[i].get(k, 0.0) / v) for k, v in need.items() if v > 0]
            new[i] = min(rs) if rs else 0.0
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
            out_cap.clear()
            out_cap.update(_backpressure(new))
        if all(abs(new[i] - dev_rate[i]) < 1e-3 for i in new):
            break
        dev_rate = new

    # 每台设备的记录
    rows = []
    for i, d in enumerate(devices):
        rc, inferred = rec[i]
        need, fuel = dev_need(i)
        if not need:
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
        if i in out_cap and r >= out_cap[i] - 1e-3:  # 开工率正好卡在背压上限上
            basis.insert(0, f"出料受限：产物下游最多吃得下满速的 {out_cap[i]:.0%}（背压，主路顶住）")
            if state == "满速":
                state = "间歇"
        fuel_got = _fuel_got(sup[i], rc) if fuel else None
        if fuel and fuel_got + EPS < fuel:
            state += f"（送进来的非配方气 {fuel_got:g}/分，低于燃料最低 {fuel:g}：可能罢工，燃料种类未确认）"
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
            msg.append(f"限制通过数量 {c['limit']:g}（累计 {c['passed']:g}）：放满后不再放行，按一次性备料算" if c.get("limit") is not None else "限制通过数量")
        if g.get("limit") is not None and dem is not None:
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
    out = {"devices": rows, "gates": gates, "ports": ports, "recipe_fixes": fixes, "tunnels": len(tpairs), "notes": notes + plant_notes, "issues": flow.get("issues", []),
           "inferred_links": inferred}
    analyze.last_graph = (G, devices, nodes, dict(sup), buf_ids)  # 调试用：最近一次的图
    return out


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


def _orphan_feeds(G, flow, devices, dev_canvas, rec) -> list:
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
            items = set(outputs_of(rec[u[1]][0])) & set(inputs_of(rec[v[1]][0]))
            if items and not G.inn[v]:
                nl = dict(lk, dir="a->b", basis=[f"{devices[v[1]]['name']}没有别的进料，按配方推断 {devices[u[1]]['name']} 往它送{'、'.join(sorted(items))}"])
                G.out[u].append((v, nl)); G.inn[v].append((u, nl))
                added.append((devices[u[1]]["name"], devices[v[1]]["name"]))
                break
    return added


def _buffer_dirs(G, flow, devices, dev_canvas, rec, alt, depth=6) -> list:
    """设备 ↔ 储存箱（中转）的带子没票、方向没定（Graph 里没收）：看储存箱往下送给谁 / 从谁那拿。
    储存箱下游有设备吃这台设备的产物 → 设备 → 储存箱；储存箱上游有设备产这台设备的原料 → 储存箱 → 设备；两样都有或都没有就不定。
    2026-11-10 武陵城：芽针粉碎机 (1793,1669) → 储存箱 (1636,1669) → 汇流器 → 反应池（作者说那台反应池游戏里在转）。"""
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
                        rc = rec.get(v[1], ("", False))[0]
                        rcs = [rc] + ([alt[v[1]][0]] if v[1] in alt else [])
                        for r in rcs:
                            items |= set(inputs_of(r)) if out else set(outputs_of(r))
            front = nxt
        return items
    added = []
    def recipes_of(i):
        return [rec.get(i, ("", False))[0]] + ([alt[i][0]] if i in alt else [])
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


def _outlet_touch(G, flow, devices, dev_canvas, rec=None, gray=None) -> list:
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
    outs = [i for i, d in enumerate(devices) if d["name"] in OUTLETS or (TOUCH_PRODUCERS and rec and outputs_of(rec[i][0]))]
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
                fl_out = [k for k in outputs_of(rec[i][0]) if "气" in k or "液" in k or k in ("清水", "污水", "沉积酸")]
                if (pipe_node and not fl_out) or (not pipe_node and len(fl_out) == len(outputs_of(rec[i][0]))):
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


def _gate_items(bd: Path, nodes: list[dict], rec: dict, inputs: list[dict] | None = None) -> dict:
    """准入口没标物品的（作者还没在界面里标）→ {节点 id: {item, how}}。
    1. 准入口图标早按长相分了组（icon_group）：作者确认过物品的输入端在哪组，整组都是那种物品（应龙关 5/6 号息壤气那组）。
    2. 再查图标库（作者标过的同样子图标）。
    3. 剩下的组和 AKEData 比：管道口只看液/气、物品口只看固体、只看本据点配方里的物品；组内取分最高的，≥0.5 且领先 ≥0.08 才用
       （0.45 左右会把息壤气口认成气态赫铜，把气挡在准入口外，2026-11-09 应龙关）。"""
    from . import logistics as LG
    img = V.rd(bd / "composite.png")
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
        st, nm = LG.lookup_item(ic, bd.parent.parent / "items" if (bd.parent.parent / "items").exists() else None)
        if nm:
            out[n["id"]] = {"item": nm, "how": "图标库"}
            continue
        k = n.get("kind")
        ok_state = ("液", "气") if k == "管道" else ("固",) if k == "物品" else ("液", "气", "固")
        cs = [c for c in LG.ake_candidates(ic, top=999) if c["state"] in ok_state and (not cand or c["name"] in cand)]
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


def _fix_recipes_by_ports(G, devices, rec, recipes, pitem) -> list[dict]:
    """配方来自历史巡检面板、按同名最近配对（±30 px），同类设备挨着时会配错台（2026-11-09 景玉谷：
    「砂叶 → 砂叶粉末」配到了一台由源矿取货口喂料的粉碎机上）。仓库取货口认出了物品后反过来核对：
    喂进来的物品一个都不在配方原料里，就换成同类设备在本据点出现过的、原料包含这些物品的配方（标推断）。"""
    known = defaultdict(set)
    for d in recipes:
        if d.get("recipe") and "→" in d["recipe"]:
            known[d["name"]].add(d["recipe"])
    out = []
    for i, d in enumerate(devices):
        rc, inf = rec[i]
        if not rc or d["name"] in PORT_NAMES:
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


def _first_gate_item(G, u, gate, depth=0):
    if depth > 12:
        return ""
    if u[0] == "n" and u[1] in gate and gate[u[1]].get("item"):
        return gate[u[1]]["item"]
    for v, _ in G.out[u]:
        it = _first_gate_item(G, v, gate, depth + 1)
        if it:
            return it
    return ""


def run_base(bd: Path, archive: Path | None, area: str, ledger: Path | None = None) -> dict:
    """bd = 项目 base/<据点>/；archive = 蓝图档案目录（data/blueprints/<档案>，配方表、准入口限速从这里读，可以没有）；
    ledger = 底账（不给就用档案里的）。写 design.json、design.jpg。"""
    led = json.loads((ledger or archive / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
    rp = archive / "ledger" / f"{area}_recipes.json" if archive else None
    recipes = json.loads(rp.read_text(encoding="utf-8"))["devices"] if rp and rp.exists() else []
    glp = archive / "ledger" / "gate_limits" / f"{area}.json" if archive else None
    gl = json.loads(glp.read_text(encoding="utf-8")) if glp and glp.exists() else None
    global RATE_LIMITED_INTAKE, MAIN_BLOCKED
    RATE_LIMITED_INTAKE = False  # 第二遍：不管别的原料够不够，每种原料「能送到多少」→ 报真正卡住的是哪种
    pot = analyze(bd, led, recipes, gl)
    RATE_LIMITED_INTAKE = True
    res = analyze(bd, led, recipes, gl)
    if any(r["state"].startswith("溢流位") for r in res["devices"]):
        # 第三遍（情景）：设备出料口的主路全堵死（下游爆仓、塑形机出料堵住…），备路上的设备能拿到多少
        MAIN_BLOCKED = True
        try:
            blk = analyze(bd, led, recipes, gl)
        finally:
            MAIN_BLOCKED = False
        bm = {(r["device"], tuple(r["c"])): r for r in blk["devices"]}
        for r in res["devices"]:
            b = bm.get((r["device"], tuple(r["c"])))
            if r["state"].startswith("溢流位") and b is not None and b["rate"] > EPS:
                r["if_main_blocked"] = {"rate": b["rate"], "got": b["got"]}
                r["basis"].insert(0, f"主路堵住时（下游爆仓）能拿到 " + "、".join(f"{k} {v:g}/分" for k, v in b["got"].items())
                                  + f"，开工率 {b['rate']:.0%}")
    potm = {(r["device"], tuple(r["c"])): r["got"] for r in pot["devices"]}
    for r in res["devices"]:
        pg = potm.get((r["device"], tuple(r["c"])))
        if pg is None:
            continue
        r["available"] = pg
        if r["rate"] < 0.995 and r["need"]:
            short = sorted((k for k in r["need"] if pg.get(k, 0.0) + 1e-6 < r["need"][k]), key=lambda k: pg.get(k, 0.0) / r["need"][k])
            r["limit_by"] = short[0] if short else r.get("limit_by")
            if short:
                r["basis"].insert(0, "卡在：" + "、".join(f"{k}（能送到 {pg.get(k, 0):g}/{r['need'][k]:g}）" for k in short))
    res["summary"] = summary(res)
    (bd / "design.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
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


STATE_COLORS = [("满速", (40, 170, 60)), ("间歇", (240, 150, 0)), ("溢流位", (140, 140, 140)), ("没接上", (200, 0, 200)), ("没料", (220, 30, 30))]


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
            "env_required": sum(1 for d in res["devices"] if d.get("env_required")), "recipe_fixes": len(res.get("recipe_fixes", []))}


def image(canvas, res):
    """设计分析图：每台设备一个圈，颜色 = 状态（绿满速 / 橙间歇 / 红没料 / 紫没接上 / 灰溢流位），旁边写开工率；
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
        txt = f"{d['rate']:.0%}" + ("*" if d.get("recipe_inferred") else "")
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
