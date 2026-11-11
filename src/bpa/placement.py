"""第 4.9 节：放置完整性检查（离线、只读）。

作者自己在游戏里摆蓝图，程序只核对、不改基地。输入：
  - 每张蓝图的预览拼图（mosaic.png）、设备一览（devices.json）、锚点（anchors.json：暗管、息壤供电桩）、物流件（logistics.json）；
  - 据点拼合的变换（base/<据点>/transform.json：每张蓝图在基地远景坐标里的旋转 + 平移）；
  - 据点远景底账（项目自己扫的优先，否则档案里的）。

做法：
  1. 归属：底账每台设备换到各张蓝图的预览坐标，看哪张蓝图的预览在那里「有东西」（墨迹）；都没有 = 不属于任何蓝图。
     蓝图是 L 形的也没关系（矩形重叠处只有一张有墨迹）。
  2. 数量：每张蓝图按类型比「设备一览」和「归到它名下的底账设备」。没有标签的类型（存取线基段/源桩、物流件）不比。
     底账里分不清取货/存货的「仓库口」先补取货口、再补存货口的缺口。
  3. 定位：预览里的设备图标（产物图标圆盘、暗管、息壤供电桩）换到基地坐标，和底账逐个配对（≤ 18 远景 px ≈ 0.9 格）。
     某类型缺了，就把这张蓝图里没配上的图标标成「疑似缺失」的位置；多了，就把这类型没配上的底账设备标成「多出」。
远景标签可能被挡住，「疑似缺失」以后用近景定向看一眼确认（4.9），这里只列出来。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

from . import vision as V

UNLABELED = {"仓库存取线基段", "仓库存取线源桩"}  # 远景没有标签，底账里数不全
MATCH_TOL = 18.0  # 远景 px：底账（标签图标中心）和预览图标的配对距离
# 预览 px：看设备位置附近 ±INK_WIN 的墨迹。设备内部大片是白的，±14 时武陵城几台只有 0.02–0.05（会判成不属于任何蓝图）；
# ±30 时所属蓝图 0.12–0.17、别的蓝图 0
INK_WIN = 30
INK_MIN = 0.04  # 墨迹比例 ≥ 这个才算这张蓝图在那里有东西
GENERIC = {"仓库口": ("仓库取货口", "仓库存货口")}


def to_preview(b: dict, fx: float, fy: float) -> tuple[float, float]:
    """远景 → 这张蓝图的预览拼图坐标（far = t + K·R(p) 的逆）。"""
    qx, qy = (fx - b["t"][0]) / V.K, (fy - b["t"][1]) / V.K
    return {0: (qx, qy), 90: (qy, -qx), 180: (-qx, -qy), 270: (-qy, qx)}[b["rot"]]


def to_far(b: dict, px: float, py: float) -> tuple[float, float]:
    rx, ry = V.Rp(b["rot"], px, py)
    return b["t"][0] + V.K * rx, b["t"][1] + V.K * ry


def ink_map(mosaic: np.ndarray) -> np.ndarray:
    """预览里「有东西」的地方：深色线条或有颜色（传送带、图标）；网格线和底色不算。"""
    g = cv2.cvtColor(mosaic, cv2.COLOR_BGR2GRAY).astype(np.float32)
    hsv = cv2.cvtColor(mosaic, cv2.COLOR_BGR2HSV)
    ink = ((g < cv2.blur(g, (61, 61)) - 28) | (hsv[..., 1] > 60)).astype(np.uint8)
    ink[:34, :170] = 0  # 左上「蓝图预览」标题
    return ink


def ink_at(ink: np.ndarray, px: float, py: float, r: int = INK_WIN) -> float:
    H, W = ink.shape
    x0, x1 = int(max(0, px - r)), int(min(W, px + r + 1))
    y0, y1 = int(max(0, py - r)), int(min(H, py + r + 1))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(ink[y0:y1, x0:x1].mean())


def icon_circles(mosaic: np.ndarray) -> list[tuple[float, float]]:
    """产物图标圆盘（设备、仓库取货/存货口中心的深色圆）。"""
    g = cv2.medianBlur(cv2.cvtColor(mosaic, cv2.COLOR_BGR2GRAY), 3)
    c = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1, minDist=22, param1=80, param2=22, minRadius=12, maxRadius=20)
    out = []
    if c is None:
        return out
    for x, y, r in c[0]:
        m = np.zeros(g.shape, np.uint8)
        cv2.circle(m, (int(x), int(y)), max(1, int(r) - 3), 1, -1)
        inner = g[m > 0]
        if inner.size and float((inner < 110).mean()) >= 0.3:
            out.append((float(x), float(y)))
    return out


def preview_marks(mosaic: np.ndarray, anchors: list[dict], logistics: list[dict]) -> list[dict]:
    """预览里的设备位置：锚点（带名字）+ 图标圆（不带名字）。物流件（分流/汇流/桥/准入口）旁边的圆去掉。"""
    marks = [{"name": a["name"], "p": [float(a["p"][0]), float(a["p"][1])]} for a in anchors]
    lg = [o["p"] for o in logistics]
    for x, y in icon_circles(mosaic):
        if any(abs(x - a["p"][0]) <= 16 and abs(y - a["p"][1]) <= 16 for a in marks):
            continue
        if any(abs(x - q[0]) <= 20 and abs(y - q[1]) <= 20 for q in lg):
            continue
        marks.append({"name": None, "p": [x, y]})
    return marks


PATCH = 44  # 拼合图 px：模板半宽（约 2.5 格见方），设备外壳 + 端口的样子
CAND_NCC = 0.72  # 和同类设备外壳的相似度 ≥ 这个才算候选


def type_candidates(canvas: np.ndarray, tf: dict, examples: list[tuple[float, float]], exclude: list[tuple[float, float]],
                    keep=lambda fx, fy: True, max_ex: int = 8, count: int = 1) -> list[dict]:
    """缺了某类设备时找它该在哪：用底账里同类设备在拼合图上的外观（以底账位置为中心裁的块）做模板，在拼合图上找相似的地方，
    去掉已有同类设备（exclude）的位置。拼合图是按基地朝向合成的，各张蓝图的同类设备朝向一致（旋转过的蓝图也已转正）。
    examples / exclude：远景坐标。keep(fx, fy)：候选要满足的条件（例如归属这张蓝图）。返回 [{far, score}]，分数高的在前。"""
    g = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    H, W = g.shape
    best = None
    allpts = [V.far_to_canvas(tf, *e) for e in examples]
    allpts = [(x, y) for x, y in allpts if PATCH <= x < W - PATCH and PATCH <= y < H - PATCH]
    pts = allpts[:: max(1, len(allpts) // max_ex)][:max_ex]
    cross = []  # 同类设备彼此的相似度：这类设备「长得多像」，用来定门槛（采种机/种植机这种外壳相近的，固定门槛会招来一堆别的设备）
    for x, y in pts:
        t = g[int(y) - PATCH:int(y) + PATCH, int(x) - PATCH:int(x) + PATCH]
        if t.std() < 8:
            continue
        r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
        for bx, by in allpts:
            if (bx, by) != (x, y):
                cross.append(float(r[max(0, int(by) - PATCH - 3):int(by) - PATCH + 4, max(0, int(bx) - PATCH - 3):int(bx) - PATCH + 4].max()))
        best = r if best is None else np.maximum(best, r)
    if best is None:
        return []
    thr = float(np.clip(np.percentile(cross, 25) - 0.05, CAND_NCC, 0.92)) if len(cross) >= 3 else CAND_NCC
    ex = [V.far_to_canvas(tf, *e) for e in exclude]
    out = []
    r = best.copy()
    while True:
        _, v, _, (x, y) = cv2.minMaxLoc(r)
        if v < thr or len(out) >= count + 2:
            break
        r[max(0, y - PATCH):y + PATCH, max(0, x - PATCH):x + PATCH] = -1
        cx, cy = x + PATCH, y + PATCH
        if any(abs(cx - a) <= PATCH * 0.6 and abs(cy - b) <= PATCH * 0.6 for a, b in ex):
            continue
        fx, fy = tf["F0"][0] + cx * tf["K"], tf["F0"][1] + cy * tf["K"]
        if keep(fx, fy):
            out.append({"far": [round(fx, 1), round(fy, 1)], "score": round(float(v), 3)})
    return out


def _expected(devices: dict) -> dict[str, int]:
    return {k: int(v) for k, v in devices.items() if isinstance(v, (int, float)) and v is not None and k not in UNLABELED}


def check_base(bps: dict[str, dict], tf: dict, ledger: dict, canvas: np.ndarray | None = None) -> dict:
    """bps：{蓝图名: {mosaic, devices, anchors, logistics}}；tf：transform.json；ledger：底账 json；canvas：据点拼合图（找缺失位置用）。
    返回 {"bps": {蓝图: {expected, found, diff, missing:[...], extra:[...]}}, "orphans": [...], "summary": str}"""
    devs = [dict(d, where="线内") for d in ledger.get("devices", [])] + [dict(d, where="线外") for d in ledger.get("outside", [])]
    names = [bp for bp in bps if bp in tf.get("bps", {})]
    inks = {bp: ink_map(bps[bp]["mosaic"]) for bp in names}

    def owner_of(fx, fy):
        best, bv = None, INK_MIN
        for bp in names:
            px, py = to_preview(tf["bps"][bp], fx, fy)
            H, W = inks[bp].shape
            if not (-INK_WIN <= px <= W + INK_WIN and -INK_WIN <= py <= H + INK_WIN):
                continue
            v = ink_at(inks[bp], px, py)
            if v > bv:
                best, bv = bp, v
        return best

    # 旧底账里同一个仓库口常被认成两条（一条具体的取货/存货口 + 一条分不清的「仓库口」）：紧挨着具体口的「仓库口」是重复，去掉，
    # 不然真少摆了一个口时会被它补上（武陵城模拟删取货口时漏报）
    dup = [i for i, d in enumerate(devs) if d["name"] in GENERIC and any(
        o["name"] in GENERIC[d["name"]] and abs(o["x"] - d["x"]) <= 30 and abs(o["y"] - d["y"]) <= 12 for o in devs)]
    devs = [d for i, d in enumerate(devs) if i not in set(dup)]

    # 1. 归属
    owner = {i: owner_of(d["x"], d["y"]) for i, d in enumerate(devs)}

    # 3. 预览图标 → 远景，和底账配对（同一张蓝图里；带名字的锚点只配同名设备）
    marks = {}
    for bp in names:
        ms = preview_marks(bps[bp]["mosaic"], bps[bp].get("anchors", []), bps[bp].get("logistics", []))
        for m in ms:
            m["far"] = list(to_far(tf["bps"][bp], *m["p"]))
        marks[bp] = ms
    # 底账记的是标签图标中心，离设备中心有个偏移；标签总是水平画在屏幕上，所以同一种设备的偏移在远景坐标里固定（和蓝图旋转无关）。
    # 第一遍宽松配对学每种设备的偏移（中位），第二遍扣掉偏移再按紧的容差配（仓库取货口名字长，偏 20–38 px，宽松一遍会漏）
    def match(offsets, tols, tol_other):
        pairs = []
        for bp in names:
            for j, m in enumerate(marks[bp]):
                for i, d in enumerate(devs):
                    if owner[i] != bp or (m["name"] and m["name"] != d["name"]):
                        continue
                    o = offsets.get(d["name"])
                    dx, dy = d["x"] - m["far"][0], d["y"] - m["far"][1]
                    if o is not None:
                        dx, dy = dx - o[0], dy - o[1]
                    dist = float(np.hypot(dx, dy))
                    if dist <= tols.get(d["name"], tol_other):
                        pairs.append((dist, bp, j, i))
        md, mm, got = set(), set(), []
        for dist, bp, j, i in sorted(pairs):
            if i in md or (bp, j) in mm:
                continue
            md.add(i); mm.add((bp, j)); got.append((bp, j, i))
        return md, mm, got

    _, _, got = match({}, {}, MATCH_TOL * 2.2)
    by_name = defaultdict(list)
    for bp, j, i in got:
        m, d = marks[bp][j], devs[i]
        by_name[d["name"]].append((d["x"] - m["far"][0], d["y"] - m["far"][1]))
    offsets, tols = {}, {}
    for k, v in by_name.items():
        if len(v) < 3:
            continue
        a = np.array(v)
        med = np.median(a, axis=0)
        res = np.hypot(*(a - med).T)
        offsets[k] = tuple(med)
        # 容差跟着这种设备自己的离散程度走（仓库取货口朝向不同，图标在口里的位置不同，离散大）
        tols[k] = float(np.clip(1.5 * np.percentile(res, 90) + 4, 10, 26))
    matched_dev, matched_mark, _ = match(offsets, tols, MATCH_TOL * 1.3)

    # 2. 数量 + 缺/多 的位置
    out = {}
    for bp in names:
        exp = _expected(bps[bp]["devices"])
        mine = [i for i in owner if owner[i] == bp]
        found = Counter(devs[i]["name"] for i in mine)
        # 「仓库口」（分不清取货/存货）按缺口补
        notes = []
        for g, opts in GENERIC.items():
            n = found.pop(g, 0)
            for o in opts:
                k = min(n, max(0, exp.get(o, 0) - found.get(o, 0)))
                found[o] += k; n -= k
            if n:  # 取货/存货口都不缺，还剩的「仓库口」多半是底账把同一个口认了两次（旧底账常见），不算多出
                notes.append(f"底账里还有 {n} 个分不清取货/存货的「仓库口」没用上（多半是同一个口认了两次）")
        diff ={k: found.get(k, 0) - exp.get(k, 0) for k in set(exp) | set(found) if found.get(k, 0) != exp.get(k, 0)}
        free_marks = [m for j, m in enumerate(marks[bp]) if (bp, j) not in matched_mark]
        missing = []
        for k, dv in sorted(diff.items()):
            if dv >= 0:
                continue
            # 候选位置：① 名字对得上的锚点（暗管、息壤供电桩：预览里认得出名字）；
            # ② 拼合图上和同类设备外壳相似、又没有同类底账设备的地方（协议储存箱、存货口这种没有圆形图标的也能找）；
            # ③ 都没有时，退回这张蓝图里没配上底账的图标圆（类型不确定）
            how = "锚点"
            cands = [{"far": [round(v, 1) for v in m["far"]], "score": 1.0} for m in free_marks if m["name"] == k]
            if not cands and canvas is not None:
                same = [(d["x"], d["y"]) for d in devs if d["name"] == k]
                cands = type_candidates(canvas, tf, same, same, keep=lambda fx, fy, bp=bp: owner_of(fx, fy) == bp, count=-dv)
                if len(cands) > -dv:  # 备选只留和前几名差不多像的（模拟里真位置 0.98–1.0，别的设备 0.80–0.86）
                    cut = cands[-dv - 1]["score"] - 0.06
                    cands = cands[:-dv] + [c for c in cands[-dv:] if c["score"] >= cut]
                how = "外观相似"
            if not cands:
                cands = [{"far": [round(v, 1) for v in m["far"]], "score": 0.0} for m in free_marks if m["name"] is None]
                how = "未配上的图标（类型不确定）"
            missing.append({"name": k, "count": -dv, "how": how, "candidates": cands})
        extra = []
        for k, dv in sorted(diff.items()):
            if dv <= 0:
                continue
            ds = [i for i in mine if devs[i]["name"] == k]
            un = [i for i in ds if i not in matched_dev] or ds
            extra.append({"name": k, "count": dv, "in_blueprint": k in exp,
                          "devices": [{"far": [devs[i]["x"], devs[i]["y"]], "where": devs[i]["where"]} for i in un]})
        out[bp] = {"expected": exp, "found": dict(found), "diff": diff, "missing": missing, "extra": extra, "notes": notes,
                   "marks": len(marks[bp]), "marks_matched": sum(1 for j in range(len(marks[bp])) if (bp, j) in matched_mark)}
    orphans = [{"name": d["name"], "far": [d["x"], d["y"]], "where": d["where"]} for i, d in enumerate(devs) if owner[i] is None]
    n_miss = sum(m["count"] for r in out.values() for m in r["missing"])
    n_extra = sum(e["count"] for r in out.values() for e in r["extra"] if e["in_blueprint"])
    n_other = sum(e["count"] for r in out.values() for e in r["extra"] if not e["in_blueprint"])
    summary = ("全部到位" if not (n_miss or n_extra or n_other) else
               "；".join(x for x in (f"疑似缺 {n_miss} 台" if n_miss else "", f"多出 {n_extra} 台" if n_extra else "",
                                     f"蓝图范围里另有 {n_other} 台蓝图外的设备" if n_other else "") if x))
    return {"bps": out, "orphans": orphans, "summary": summary, "marks": {bp: marks[bp] for bp in names},
            "label_offsets": {k: [round(v[0], 1), round(v[1], 1)] for k, v in offsets.items()}, "ledger_dups_dropped": len(dup)}


# ------------------------------------------------------------------ 标注图
def draw(canvas: np.ndarray, tf: dict, res: dict, font_path: str = r"C:\Windows\Fonts\msyhbd.ttc"):
    """据点拼合图上圈出：红圈 = 疑似缺失候选位置，橙圈 = 多出的底账设备；左上列每张蓝图的核对结果。"""
    from PIL import Image, ImageDraw, ImageFont
    pil = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    F, FS = ImageFont.truetype(font_path, 22), ImageFont.truetype(font_path, 18)
    n = 0
    for bp, r in res["bps"].items():
        for m in r["missing"]:
            for j, c in enumerate(m["candidates"]):
                x, y = V.far_to_canvas(tf, *c["far"])
                n += 1
                top = j < m["count"]  # 前 count 个是最像的；后面的是备选，细圈
                dr.ellipse([x - 30, y - 30, x + 30, y + 30], outline=(230, 0, 0) if top else (240, 120, 120), width=5 if top else 2)
                dr.text((x + 32, y - 14), f"{n} 缺{m['name']}" + ("" if top else "（备选）"), fill=(230, 0, 0) if top else (220, 110, 110), font=F if top else FS)
        for e in r["extra"]:
            for d in e["devices"]:
                x, y = V.far_to_canvas(tf, *d["far"])
                n += 1
                dr.ellipse([x - 30, y - 30, x + 30, y + 30], outline=(240, 140, 0), width=5)
                dr.text((x + 32, y - 14), f"{n} 多{e['name']}", fill=(220, 120, 0), font=F)
    lines = [f"放置完整性：{res['summary']}"]
    for bp, r in res["bps"].items():
        if not r["diff"]:
            lines.append(f"{bp}：{sum(r['expected'].values())} 台全到")
        else:
            lines.append(f"{bp}：" + "、".join(f"{k}{'+' if v > 0 else ''}{v}" for k, v in sorted(r["diff"].items())))
    w = max(dr.textlength(t, font=FS) for t in lines) + 20
    dr.rectangle([0, 0, w, 10 + 26 * len(lines)], fill=(255, 255, 255))
    for k, t in enumerate(lines):
        dr.text((10, 5 + 26 * k), t, fill=(0, 0, 0) if k else (160, 0, 0), font=FS)
    return pil
