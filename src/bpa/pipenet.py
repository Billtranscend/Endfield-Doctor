"""据点拼合图上的管网：哪些输入端（准入口）接在同一张管网上、这张网喂哪些设备、设计用量多少。
用途：近景巡检发现源头设备（水泵）的出水管断开时，算「影响范围」—— 这根管子进了哪个输入端、同一张网还有哪些输入端补水、
下游设备按配方每分钟要多少，断开后供给还够不够（2026-11-08 用户：被断开的水泵不是多余的，要核对它进了哪些下游设备、
这些设备每分钟设计要多少水）。

拼合图上管道是粗的深灰线（机器外框是细线，开运算去掉）；准入口、管道分流/汇流器、管道桥是图标块，补成方块接上。
设备接没接在网上：管道接到设备的端口时会碰到设备外框（≤ PORT_TOL px）；只是从隔壁格经过的管道离外框约 9 px。
设备外框从拼合图的深色细线里找（包住设备中心、面积合适的最小矩形）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import cv2
import numpy as np

from . import vision as V

PORT_TOL = 6  # 管网像素离设备外框 ≤ 这个 px 算接上（含端点；端口处的分流器方块离外框 5 px，隔壁格经过的管道 ≈ 9 px）
GATE_SNAP = 160  # 断点离输入端准入口 ≤ 这个（拼合图 px）才算这根管子进的是它
PIPE_PARTS = ("管道分流器", "管道汇流器", "管道桥")


def pipe_mask(canvas: np.ndarray, logistics: list[dict]) -> np.ndarray:
    hsv = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    dark = ((g < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    m = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))  # 去掉机器外框细线
    # 管道从半透明的浅灰叠层（存取线等）底下穿过时颜色变浅（灰度约 150），上面的阈值认不出、管子在那里断开
    # （2026-11-08 武陵城中间那组精炼炉的进水管）：放宽灰度，但只收横竖方向连续 ≥25 px、粗 ≥5 px 的直条（管道形状）
    loose = ((g < 175) & (hsv[..., 1] < 60)).astype(np.uint8)
    lines = cv2.morphologyEx(loose, cv2.MORPH_OPEN, np.ones((5, 25), np.uint8)) | cv2.morphologyEx(loose, cv2.MORPH_OPEN, np.ones((25, 5), np.uint8))
    m = m | lines
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))  # 管道上的白箭头补上
    m = cv2.dilate(m, np.ones((7, 7), np.uint8))  # 圆角拐弯处管子细：外扩 3 px 接上
    for o in logistics:
        x, y = o["p"]
        if o["type"] == "管道桥":
            # 管道桥是两根管子交叉、互不相通：把桥这一格挖空，连通时再只把「左右」「上下」各自接上（见 bridge_pairs）
            # （2026-11-08 景玉谷：水管从暗管出口过桥跨过惰气管，桥补成方块时水网和惰气网连成一张）
            cv2.rectangle(m, (x - 20, y - 20), (x + 20, y + 20), 0, -1)
        elif o["type"] in PIPE_PARTS or (o["type"] == "准入口" and o.get("kind") == "管道"):
            cv2.rectangle(m, (x - 18, y - 18), (x + 18, y + 18), 1, -1)
    return m


def bridge_pairs(lab: np.ndarray, logistics: list[dict], stubs: list | None = None) -> list[tuple[int, int]]:
    """管道桥两侧要接上的管网编号对：左边 ↔ 右边、上边 ↔ 下边（只接直通，不接交叉）。
    只有一边有管子时（另一边直接是设备端口，2026-11-08 景玉谷天有洪炉 (993,738)），把这边的管子沿直线穿过桥伸出去：
    stubs 里追加 (编号, (x0, y0, x1, y1))，调用方把这一条画进管网，设备外框判接上时就碰得到。"""
    out = []
    H, W = lab.shape
    for o in logistics:
        if o["type"] != "管道桥":
            continue
        x, y = o["p"]

        def side(x0, y0, x1, y1):
            w = lab[max(0, y0):min(H, y1), max(0, x0):min(W, x1)]
            ids, c = np.unique(w[w > 0], return_counts=True)
            return int(ids[c.argmax()]) if len(ids) else None

        horiz = (side(x - 30, y - 6, x - 20, y + 7), side(x + 21, y - 6, x + 31, y + 7), (x - 30, y - 4, x + 31, y + 5))
        vert = (side(x - 6, y - 30, x + 7, y - 20), side(x - 6, y + 21, x + 7, y + 31), (x - 4, y - 30, x + 5, y + 31))
        for a, b, band in (horiz, vert):
            if a is not None and b is not None and a != b:
                out.append((a, b))
            elif stubs is not None and (a is None) != (b is None):
                stubs.append((a if a is not None else b, band))
    return out


def device_rect(dark_thin: np.ndarray, cx: float, cy: float, rects) -> tuple[int, int, int, int] | None:
    """包住 (cx, cy) 的最小机器外框矩形（宽高 ≥ 2 格）。rects：预先从深色细线里找好的候选矩形。
    找不到闭合外框时（天有洪炉只有上下两条深色边，左右是浅色），从中心往上、往下找第一条穿过中心列、长 ≥ 2 格的深色横线，
    取两条横线的范围。"""
    best = None
    for x, y, w, h in rects:
        if x <= cx <= x + w and y <= cy <= y + h and (best is None or w * h < best[2] * best[3]):
            best = (x, y, w, h)
    return (best or _rect_from_lines(dark_thin, int(cx), int(cy), gap=0) or _rect_from_vlines(dark_thin, int(cx), int(cy))
            or _rect_from_lines(dark_thin, int(cx), int(cy), gap=30))


FOOTPRINT = {"扩容反应池": (6, 5)}  # 长方形设备的格数（IndustrialPlanner mix_pool_2 6×5）


def footprint_rect(name: str, rc: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """外框只认出了一部分的长方形设备：长边补到占地格数。
    扩容反应池 6×5：中间放传送带口的 4 格有一圈深色框，常被当成外框（125×156），左右两侧的管道口落在框外 40 多 px，
    管网就落不到它身上（2026-11-10 武陵城 (1489,1055)、(1647,1055)、(1313,1114)、(1490,1174)；认全的是 195×161）。
    认出的长边不超过 5.2 格 → 缺的是 6 格那一边：短边按「长边 ÷ 5 × 6」居中补齐。"""
    fp = FOOTPRINT.get(name)
    if not fp:
        return rc
    x, y, w, h = rc
    big, small = max(fp), min(fp)
    if max(w, h) > (small + 0.2) * V.CELL:
        return rc
    full = int(round((max(w, h) + 5) / small * big))  # 认出的是内框：5 格那边 156，整台是 161（认全的 (1768,1174) 195×161）
    if w < h:
        return (int(x + w / 2 - full / 2), y, full, h)
    return (x, int(y + h / 2 - full / 2), w, full)


def _run(row: np.ndarray, x: int, gap: int = 30) -> tuple[int, int]:
    """包含 x 的深色横线段，中间允许 ≤ gap px 的断开（反应池上下边被端口的半圆气泡截断，2026-11-08 应龙关扩容反应池）。"""
    if not row[x]:
        return x, x
    l = x
    while True:
        k = l - 1
        while k >= 0 and not row[k] and l - k <= gap:
            k -= 1
        if k < 0 or not row[k]:
            break
        l = k
    r = x
    n = len(row)
    while True:
        k = r + 1
        while k < n and not row[k] and k - r <= gap:
            k += 1
        if k >= n or not row[k]:
            break
        r = k
    return l, r


def _rect_from_vlines(dark: np.ndarray, cx: int, cy: int, max_cells: int = 8) -> tuple[int, int, int, int] | None:
    """从中心往左、往右找第一条穿过中心行、长 ≥ 2 格的深色竖线（反应池左右两侧是粗竖条，上下边被端口气泡截断），取两条竖线的范围。"""
    H, W = dark.shape
    lim = int(max_cells * V.CELL / 2)
    found = {}
    col = dark.T
    for sgn, key in ((-1, "l"), (1, "r")):
        for d in range(int(V.CELL * 0.8), lim):
            x = cx + sgn * d
            if not 0 <= x < W:
                break
            ys = [y for y in range(max(0, cy - 6), min(H, cy + 7)) if dark[y, x]]
            if not ys:
                continue
            t, b = _run(col[x], ys[0], 0)
            if b - t >= 2 * V.CELL:
                found[key] = (x, t, b)
                break
    if len(found) < 2:
        return None
    (xl, tl, bl), (xr, tr, br) = found["l"], found["r"]
    y0, y1 = min(tl, tr), max(bl, br)
    # 竖条只占机身中段：再往上、往下找「这一行在两条竖线之间大半是深色」的外框线（端口就在上下边上）
    for y in range(y0 - 1, max(-1, y0 - 70), -1):
        if dark[y, xl:xr + 1].mean() >= 0.3:
            y0 = y
            break
    for y in range(y1 + 1, min(H, y1 + 70)):
        if dark[y, xl:xr + 1].mean() >= 0.3:
            y1 = y
            break
    if not (2 * V.CELL - 6 <= xr - xl <= max_cells * V.CELL and 2 * V.CELL - 6 <= y1 - y0 <= max_cells * V.CELL):
        return None
    return xl, y0, xr - xl, y1 - y0


def _rect_from_lines(dark: np.ndarray, cx: int, cy: int, max_cells: int = 8, gap: int = 0) -> tuple[int, int, int, int] | None:
    H, W = dark.shape
    lim = int(max_cells * V.CELL / 2)
    found = {}
    for sgn, key in ((-1, "top"), (1, "bot")):
        for d in range(int(V.CELL * 0.8), lim):
            y = cy + sgn * d
            if not 0 <= y < H:
                break
            # 中心列附近 ±6 px 里有深色的就看这一行的连续段
            xs = [x for x in range(max(0, cx - 6), min(W, cx + 7)) if dark[y, x]]
            if not xs:
                continue
            l, r = _run(dark[y], xs[0], gap)
            if r - l >= 2 * V.CELL:
                found[key] = (y, l, r)
                break
    if len(found) < 2:
        return None
    (yt, lt, rt), (yb, lb, rb) = found["top"], found["bot"]
    x0, x1 = min(lt, lb), max(rt, rb)
    if not (2 * V.CELL - 6 <= x1 - x0 <= max_cells * V.CELL and 2 * V.CELL - 6 <= yb - yt <= max_cells * V.CELL):
        return None
    return x0, yt, x1 - x0, yb - yt


def _rects(canvas: np.ndarray) -> list[tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    dark = ((g < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    cs, _ = cv2.findContours(dark, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cs:
        x, y, w, h = cv2.boundingRect(c)
        if 2 * V.CELL - 6 <= w <= 9 * V.CELL and 2 * V.CELL - 6 <= h <= 9 * V.CELL:
            # 矩形外框：轮廓面积接近外接矩形（不是一团管道）
            if cv2.contourArea(c) >= 0.85 * w * h or len(cv2.approxPolyDP(c, 4, True)) <= 6:
                out.append((x, y, w, h))
    return out


def water_per_min(recipe: str, fluid: str = "清水", machine: str = "") -> float:
    """配方文字「碳块×1 + 清水×1 → 息壤×1（2秒）」里 fluid 的每分钟用量。
    反应池的配方表只记「池产物：液化息壤(50)/重息壤(0)/…」：数量 > 0 的产物按配方表里「这种池子 + 这个产物 + 用 fluid」的配方算
    （2026-11-08 应龙关扩容反应池：清水 + 息壤粉末 → 液化息壤，30/分）。"""
    if recipe and recipe.startswith("池产物") and machine:
        return _pool_need(recipe, fluid, machine)
    if not recipe or "→" not in recipe:
        return 0.0
    left = recipe.split("→")[0]
    m = re.search(rf"{fluid}×(\d+)", left)
    t = re.search(r"（(\d+(?:\.\d+)?)秒）", recipe)
    if not m or not t:
        return 0.0
    return float(m.group(1)) * 60.0 / float(t.group(1))


def _pool_need(recipe: str, fluid: str, machine: str) -> float:
    """池子配置的配方（pool_channels：面板列的物品还原成几条通道，首尾相接的并成一条净配方）里 fluid 的每分钟用量。
    互不相干的几组（武陵城反应池 (1357,1332) 芽针溶液 / 锦草溶液）按轮流做算，取用得最多的那组（设计分析同样按交替配方算）。
    2026-11-10 武陵城扩容反应池 (1489,1055)：面板「息壤」是「清水 + 息壤 → 液化息壤」的原料，原来按产物找不到、算成不用清水。"""
    try:
        import pool_channels as PC
        fid = PC._name_id().get(fluid)
        groups = PC.from_text(machine, recipe)
    except Exception:  # noqa: BLE001
        return 0.0
    return max((r["inputs"].get(fid, 0) * 60.0 / r["seconds"] for r in groups), default=0.0)


def load_inputs(base_dir: Path) -> list[dict]:
    """据点的输入端：作者在「输入端」页按编号填过（项目 inputs.json，含每个口的物品和流速）就用作者的，没填过用自动识别的候选。
    （2026-11-09 蓝图作者：要能给单个口设流速，如标记 3 的清水准入口 120/分；原来管网和巡检只读自动候选，每个清水口一律 60）"""
    pj = base_dir.parent.parent / "inputs.json"
    if pj.exists():
        marks = ((json.loads(pj.read_text(encoding="utf-8")) or {}).get(base_dir.name) or {}).get("marks")
        if marks:
            # 清水口作者没填流速：按一台水泵 60/分（和自动候选一样）
            return [dict(m, rate=60) if (m.get("item") == "清水" and m.get("rate") in (None, "")) else m for m in marks]
    return json.loads((base_dir / "inputs_auto.json").read_text(encoding="utf-8"))


def impact(base_dir: Path, ledger: dict, recipes: list[dict], break_far: tuple[float, float], inputs: list[dict] | None = None,
           fluid: str = "清水") -> dict | None:
    """断点（远景坐标）→ 它进的输入端、同网输入端、下游设备和设计用量。拼合图/输入端不在时返回 None。"""
    tf = json.loads((base_dir / "transform.json").read_text(encoding="utf-8"))
    if inputs is None:
        inputs = load_inputs(base_dir)
    bx, by = V.far_to_canvas(tf, *break_far)
    cands = [m for m in inputs if (m.get("item") or "") == fluid]
    if not cands:
        return None
    gate = min(cands, key=lambda m: np.hypot(m["p"][0] - bx, m["p"][1] - by))
    gd = float(np.hypot(gate["p"][0] - bx, gate["p"][1] - by))
    if gd > GATE_SNAP:
        return {"gate": None, "gate_dist": round(gd), "note": f"断点离最近的{fluid}输入端还有 {gd:.0f} px，对不上"}
    r = network(base_dir, ledger, recipes, gate["no"], inputs, fluid)
    if r is not None:
        r.update(gate_dist=round(gd), canvas_break=[round(bx), round(by)])
    return r


ICON_MATCH = 30.0  # 产物图标平均色（BGR）离最像的已知配方组不超过这个、且不到第二像的 1/3，才按图标定配方


def _usable(rc: str) -> bool:
    return bool(rc) and ("→" in rc or rc.startswith("池产物"))


def recipe_table(base_dir: Path, ledger: dict, recipes: list[dict]) -> dict:
    """台账每台设备 → (配方, 是否推断)。没读到配方的：同类设备有几种已知配方时，按蓝图上机身中间的产物图标颜色找最像的那组
    （2026-11-08 武陵城：精炼炉 (1870,699) 没读到配方，按「最常见」猜成了赤铜块（用清水），游戏里它的图标是蓝铁块、不用水）；
    认不出或同类只有一种配方时按最常见的。都标「推断」。"""
    from collections import Counter
    rec = {(d["name"], d["x"], d["y"]): d.get("recipe") or "" for d in recipes}
    unsure = {(d["name"], d["x"], d["y"]) for d in recipes if d.get("inferred")}  # 按机身图标推的、没把握的（recipe_icons）
    common = {}
    for (nm, rc), _ in Counter((d["name"], d.get("recipe")) for d in recipes if d.get("recipe") and "→" in d["recipe"]).most_common():
        common.setdefault(nm, rc)
    out = {}
    need_icon = [d for d in ledger.get("devices", []) if not _usable(rec.get((d["name"], d["x"], d["y"]), ""))
                 and len({rec[k] for k in rec if k[0] == d["name"] and _usable(rec[k])}) >= 2]
    icon = {}
    if need_icon:
        tf = json.loads((base_dir / "transform.json").read_text(encoding="utf-8"))
        canvas = V.rd(base_dir / "composite.png")
        H, W = canvas.shape[:2]
        hsv = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
        dark_thin = ((cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY) < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
        rects = _rects(canvas)
        types = {d["name"] for d in need_icon}

        def patch(d):
            cx, cy = V.far_to_canvas(tf, d["x"], d["y"])
            if not (0 <= cx < W and 0 <= cy < H):
                return None
            r = device_rect(dark_thin, cx, cy, rects)
            if r is None:
                return None
            mx, my = r[0] + r[2] // 2, r[1] + r[3] // 2
            return canvas[max(0, my - 8):my + 9, max(0, mx - 8):mx + 9].reshape(-1, 3).astype(float).mean(0)

        for d in ledger.get("devices", []):
            if d["name"] in types:
                icon[(d["name"], d["x"], d["y"])] = patch(d)
    for d in ledger.get("devices", []):
        k = (d["name"], d["x"], d["y"])
        rc = rec.get(k, "")
        if _usable(rc):
            out[k] = (rc, k in unsure)
            continue
        guess = common.get(d["name"], "")
        if icon.get(k) is not None:
            best = {}
            for k2, rc2 in rec.items():
                if k2[0] == d["name"] and _usable(rc2) and icon.get(k2) is not None:
                    dist = float(np.linalg.norm(icon[k] - icon[k2]))
                    best[rc2] = min(best.get(rc2, 1e9), dist)
            if len(best) >= 2:
                (d1, r1), (d2, _) = sorted((v, r) for r, v in best.items())[:2]
                if d1 <= ICON_MATCH and d1 * 3 <= d2:
                    guess = r1
        out[k] = (guess, True) if guess else ("", False)
    return out


def network(base_dir: Path, ledger: dict, recipes: list[dict], gate_no: int | None, inputs: list[dict] | None = None,
            fluid: str = "清水", start_tunnel: tuple[int, int] | None = None) -> dict | None:
    """输入端 gate_no 所在的那张管网：同网输入端（供给）、下游设备和设计用量。
    start_tunnel=(x, y)：从一个没配上对的暗管出口出发（水从别的蓝图/基地外经暗管进来，量未知），gate_no 传 None。"""
    tf = json.loads((base_dir / "transform.json").read_text(encoding="utf-8"))
    canvas = V.rd(base_dir / "composite.png")
    logistics = json.loads((base_dir / "logistics.json").read_text(encoding="utf-8"))
    if inputs is None:
        inputs = load_inputs(base_dir)
    H, W = canvas.shape[:2]
    gate = next((m for m in inputs if m["no"] == gate_no), None) if gate_no is not None else         {"no": None, "p": list(start_tunnel), "item": None, "rate": 0}
    if gate is None:
        return None
    pm = pipe_mask(canvas, logistics)
    _, lab = cv2.connectedComponents(pm)

    def comp_at(p):
        x, y = p
        w = lab[max(0, y - 25):y + 26, max(0, x - 25):x + 26]
        ids, c = np.unique(w[w > 0], return_counts=True)
        return int(ids[c.argmax()]) if len(ids) else None

    def comp_near(p, r):
        x, y = p
        x0, y0 = max(0, x - r), max(0, y - r)
        w = lab[y0:y + r + 1, x0:x + r + 1]
        ys, xs = np.nonzero(w)
        if not len(xs):
            return None
        k = int(np.hypot(xs + x0 - x, ys + y0 - y).argmin())
        return int(w[ys[k], xs[k]])

    def tunnel_comps(p, multi=False, half=58):
        """接在暗管图标上的管子：碰到图标框（±half）的管网块，去掉整块都在框里的（图标中间的深色标志）。
        单口暗管只有一个端口：取伸到离图标中心最近的那块（从隔壁经过的管子只擦到框边；2026-11-08 武陵城取「重叠最多」取错过）；
        多口暗管有几个端口：全部都算（武陵城 4 号的多口暗管出口一头接种植机线、一头接到暗管入口 10）。"""
        x, y = p
        x0, y0, x1, y1 = max(0, x - half), max(0, y - half), min(W, x + half + 1), min(H, y + half + 1)
        cands = []
        for c in np.unique(lab[y0:y1, x0:x1]):
            if not c:
                continue
            ys, xs = np.nonzero(lab == c)
            if xs.min() >= x0 and xs.max() < x1 and ys.min() >= y0 and ys.max() < y1:
                continue  # 整块在图标里：图标标志
            sy, sx = np.nonzero(lab[y0:y1, x0:x1] == c)
            cands.append((float(np.hypot(sx + x0 - x, sy + y0 - y).min()), int(c)))
        if not cands:
            return []
        return [c for _, c in cands] if multi else [min(cands)[1]]

    def tunnel_comp(p, half=58):
        r = tunnel_comps(p, False, half)
        return r[0] if r else None

    cid = comp_at(gate["p"]) if gate_no is not None else tunnel_comp(tuple(start_tunnel))
    if cid is None:
        return {"gate": gate["no"], "note": "输入端旁边没认出管道"}
    # 暗管：入口和出口在拼合图上不相连，按读蓝图时点开配对的结果（bp/<蓝图>/links.json）把两头所在的管网并起来
    # （2026-11-08 景玉谷：输入端 1 的水进右上角暗管入口，从中间的暗管出口出来喂两台天有洪炉）
    parent = {}

    def find(a):
        while parent.get(a, a) != a:
            a = parent[a]
        return a

    # 一张管网只走一种流体：每块管子带上接在它上面的输入端的流体；暗管/管道桥的连接会把两种流体并到一起时不接
    # （2026-11-08 武陵城：原始管子按流体分得开，是暗管配对/桥把惰气网、壤晶废液网接进了清水网）
    fl = {}
    for m in inputs:
        c = comp_at(m["p"])
        if c is not None and m.get("item"):
            fl.setdefault(c, set()).add(m["item"])
    # 没有输入端的管子按游戏近景图里的颜色定流体（near_patrol 注册）：精炼炉排出的污水是深紫色管，
    # 不能并进清水网（2026-11-08 用户：赤铜块配方带污水，左下那几根是去废水处理机的污水管；清水只算蓝色管）
    for c_, f_ in _fluid_labels(base_dir, lab).items():
        fl.setdefault(c_, {f_})
    if fl.get(cid) and not carries(fl[cid], fluid):
        return {"gate": gate["no"], "note": f"这条是{'、'.join(sorted(fl[cid]))}管，不是{fluid}"}

    def union(a, b) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return True
        fa, fb = fl.get(ra, set()), fl.get(rb, set())
        if not fluids_compatible(fa, fb):
            return False
        parent[ra] = rb
        fl[rb] = fluids_merge(fa, fb)
        return True

    stubs = []
    for a_, b_ in bridge_pairs(lab, logistics, stubs):  # 管道桥：只接直通
        union(a_, b_)
    for c_, (x0_, y0_, x1_, y1_) in stubs:  # 只有一边有管子：沿直线穿过桥伸到对面的设备端口
        lab[max(0, y0_):y1_, max(0, x0_):x1_] = np.where(lab[max(0, y0_):y1_, max(0, x0_):x1_] == 0, c_, lab[max(0, y0_):y1_, max(0, x0_):x1_])
    tunnels = 0
    for bp_name, b in tf.get("bps", {}).items():
        lp = base_dir.parent.parent / "bp" / bp_name / "links.json"
        mp = base_dir.parent.parent / "bp" / bp_name / "mosaic.png"
        if not lp.exists() or not mp.exists():
            continue
        lk = json.loads(lp.read_text(encoding="utf-8"))
        shape = V.rd(mp).shape
        pipes = lk.get("pipes", [])
        # 配对两头都记的（双向）和只记一头的（单向）都算；原来只取 i < j 的那一条，单向且 i > j 的整条丢了
        # （2026-11-08 武陵城 2号 5→4、4号 3→0、11→9 三对没接上）
        pairs = {tuple(sorted((int(a_), int(b_)))) for a_, b_ in (lk.get("partner") or {}).items() if b_ is not None}
        for i, j in sorted(pairs):
            if i >= len(pipes) or j >= len(pipes):
                continue
            ends = [V.preview_to_canvas(tf, bp_name, shape, *pipes[k]["p"]) for k in (i, j)]
            cs = [tunnel_comps((int(round(x)), int(round(y))), "多口" in pipes[k]["name"]) for (x, y), k in zip(ends, (i, j))]
            if cs[0] and cs[1]:  # 暗管图标约 3 格见方，管子接在图标边上
                ok_ = all([union(a_, b_) for a_ in cs[0] for b_ in cs[1]])
                for extra in (cs[0][1:], cs[1][1:]):  # 多口暗管的几个端口彼此相通
                    for e_ in extra:
                        union(cs[0][0] if extra is cs[0][1:] else cs[1][0], e_)
                tunnels += ok_
    # 拼合图上断开、游戏近景图里却连着的地方（分流器/桥上下贴着、叠层、蓝图拼接处）：近景核实过的断口接上
    for a_, b_ in _gap_pairs(base_dir, lab):
        union(a_, b_)
    root = find(cid)
    if fl.get(root) and not carries(fl[root], fluid):
        # 起点那段没定流体，并完才看出是别的流体的网（2026-11-08 武陵城：顶上暗管 (821,65) 是精炼炉的污水去废水处理机的路）
        return {"gate": gate["no"], "note": f"这条是{'、'.join(sorted(fl[root]))}管，不是{fluid}"}
    comps = [c for c in np.unique(lab) if c and find(int(c)) == root]
    net = np.isin(lab, comps).astype(np.uint8)
    same = [m for m in inputs if (c := comp_at(m["p"])) is not None and find(c) == root]
    rects = _rects(canvas)
    hsv = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
    dark_thin = ((cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY) < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    table = recipe_table(base_dir, ledger, recipes)
    users = []
    for d in ledger.get("devices", []):
        rc_, guessed = table.get((d["name"], d["x"], d["y"]), ("", False))
        if not rc_:
            continue
        r = {"recipe": rc_}
        need = water_per_min(r["recipe"] if r else "", fluid, d["name"])
        if need <= 0:
            continue
        cx, cy = V.far_to_canvas(tf, d["x"], d["y"])
        if not (0 <= cx < W and 0 <= cy < H):
            continue
        rc = device_rect(dark_thin, cx, cy, rects)
        if rc is None:
            continue
        rc = footprint_rect(d["name"], rc)
        x, y, w, h = rc
        x0, y0, x1, y1 = max(0, x - PORT_TOL), max(0, y - PORT_TOL), min(W, x + w + PORT_TOL + 1), min(H, y + h + PORT_TOL + 1)
        if net[y0:y1, x0:x1].any():
            if "→" in r["recipe"]:
                prod = r["recipe"].split("→")[1].split("×")[0].strip()
            else:  # 池产物：取数量 > 0 的第一个
                prod = next((a.strip() for a, n in re.findall(r"([^：/()]+)\((\d+)\)", r["recipe"]) if int(n) > 0), "")
            users.append({"name": d["name"], "far": [d["x"], d["y"]], "recipe": r["recipe"], "need": need, "product": prod,
                          "rect": [int(v) for v in rc], "recipe_guessed": guessed})
    # 任务计划第 10 项（2026-11-10）：网上还有别的水源
    # 1. 接在这张网上、配方产这种流体的设备（提纯机：惰性壤晶废液×4 → 清水×1 + 壤晶废液×1；拆解机拆瓶子；液气转化机水蒸气 → 清水），
    #    按设计分析算出的开工率折算（没有 design.json 按满速）
    from .design import outputs_of
    rates = {}
    dj = base_dir / "design.json"
    if dj.exists():
        try:
            rates = {(r["device"], r["x"], r["y"]): float(r.get("rate") or 0) for r in json.loads(dj.read_text(encoding="utf-8")).get("devices", [])}
        except (ValueError, KeyError):
            rates = {}
    producers = []
    for d in ledger.get("devices", []):
        rc_, guessed = table.get((d["name"], d["x"], d["y"]), ("", False))
        out = outputs_of(rc_).get(fluid, 0.0) if rc_ and "→" in rc_ else 0.0
        if out <= 0 or water_per_min(rc_, fluid, d["name"]) > 0:
            continue
        cx, cy = V.far_to_canvas(tf, d["x"], d["y"])
        if not (0 <= cx < W and 0 <= cy < H):
            continue
        rc = device_rect(dark_thin, cx, cy, rects)
        if rc is None:
            continue
        x, y, w, h = rc
        x0, y0, x1, y1 = max(0, x - PORT_TOL), max(0, y - PORT_TOL), min(W, x + w + PORT_TOL + 1), min(H, y + h + PORT_TOL + 1)
        if net[y0:y1, x0:x1].any():
            k = rates.get((d["name"], d["x"], d["y"]), 1.0)
            producers.append({"name": d["name"], "far": [d["x"], d["y"]], "recipe": rc_, "rate": round(out * k, 3), "full": out,
                              "recipe_guessed": guessed})
    # 2. 没配上对的暗管出口接在这张网上：水从基地外直接经暗管进来（没有准入口），量不知道 → 供给只是下限
    external = []
    for bp_name, _ in tf.get("bps", {}).items():
        lp = base_dir.parent.parent / "bp" / bp_name / "links.json"
        mp = base_dir.parent.parent / "bp" / bp_name / "mosaic.png"
        if not lp.exists() or not mp.exists():
            continue
        lk = json.loads(lp.read_text(encoding="utf-8"))
        partner = lk.get("partner") or {}
        paired = {int(a_) for a_, b_ in partner.items() if b_ is not None} | {int(b_) for b_ in partner.values() if b_ is not None}
        shape = None
        for k_, pp in enumerate(lk.get("pipes", [])):
            if k_ in paired or "出口" not in pp.get("name", ""):
                continue
            shape = shape or V.rd(mp).shape
            x_, y_ = V.preview_to_canvas(tf, bp_name, shape, *pp["p"])
            cs = tunnel_comps((int(round(x_)), int(round(y_))), "多口" in pp["name"])
            if any(find(c_) == root for c_ in cs):
                external.append({"name": pp["name"], "p": [int(round(x_)), int(round(y_))], "bp": bp_name})
    gates_supply = sum(float(m.get("rate") or 0) for m in same)
    supply = gates_supply + sum(p_["rate"] for p_ in producers)
    demand = sum(u["need"] for u in users)
    lost = float(gate.get("rate") or 0)
    return {"gate": gate["no"], "gate_p": gate["p"], "fluid": fluid,
            "same_gates": [{"no": m["no"], "rate": m.get("rate"), "via": m.get("via"), "p": m["p"]} for m in same],
            "supply": supply, "supply_gates": gates_supply, "producers": producers, "external_tunnels": external, "supply_known": not external,
            "supply_after": supply - lost, "demand": demand, "users": users, "net_px": int(net.sum())}


GAP_TINY = 6  # 拼合图 px：断口不超过这个直接接上（图标遮挡）
GAP_MAX = 40  # 拼合图 px：两块管子最近处相距 ≤ 这个才当断口候选（隔壁格平行的管子约 35 px，靠近景核实区分）
_GAP_CHECK: dict = {}  # str(base_dir) → fn(pa, pb) -> bool：游戏近景图里这两点之间是不是一根连着的水管（near_patrol 注册）
_GAP_CACHE: dict = {}


def set_gap_check(base_dir: Path, fn) -> None:
    """注册断口核实函数（拼合图坐标两点 → 近景图里是否连着）。注册后这个据点的管网追踪会把核实过的断口接上。"""
    _GAP_CHECK[str(base_dir)] = fn
    for k in [k for k in _GAP_CACHE if k[0] == str(base_dir)]:
        del _GAP_CACHE[k]


_FLUID_CHECK: dict = {}  # str(base_dir) → fn(lab) -> {管网块: 流体}：按近景图颜色给没有输入端的管子定流体（near_patrol 注册）
_FLUID_CACHE: dict = {}


# 近景颜色只能分到「一族」的流体（任务计划第 6 项，2026-11-10 近景实测 + IndustrialPlanner 美术配色表）：
# 沉积酸、液化息壤都是黄色管；壤晶废液、惰性壤晶废液都是深青管。颜色给的是族名，和族里任何一种具体流体都能并网。
FLUID_FAMILIES = {
    "黄色管（沉积酸/液化息壤）": {"沉积酸", "液化息壤", "液化重息壤"},
    "深青管（壤晶废液）": {"壤晶废液", "惰性壤晶废液"},
}


# 弱标签：近景颜色像这种流体、但平移半格内又能看到别的颜色（并排的管子、局部对不准）。并网时遇到强标签（输入端 / 颜色明确）就让位；
# 没有强标签时仍按它算（不把它当成「没标流体」并进清水网）。2026-11-10 武陵城扩容反应池 (1489,1055) 的进水管
WEAK_LABELS = {"疑似污水": "污水"}


def _strong(fs: set) -> set:
    return {x for x in fs if x not in WEAK_LABELS}


def _members(label: str) -> set:
    if label in WEAK_LABELS:
        return {WEAK_LABELS[label]}
    return FLUID_FAMILIES.get(label, {label})


def fluids_compatible(fa: set, fb: set) -> bool:
    """两块管子的流体能不能是同一种：所有标签的「可能流体」有交集。弱标签不挡并网。"""
    fa, fb = _strong(fa), _strong(fb)
    if not fa or not fb:
        return True
    common = None
    for lb in fa | fb:
        common = _members(lb) if common is None else common & _members(lb)
    return bool(common)


def fluids_merge(fa: set, fb: set) -> set:
    """并网后的标签：有具体流体名就去掉能被它说明的族名。"""
    s = fa | fb
    if _strong(s):  # 有强标签：弱标签让位
        s = _strong(s)
    specific = {x for x in s if x not in FLUID_FAMILIES and x not in WEAK_LABELS}
    return {x for x in s if x not in FLUID_FAMILIES or not (FLUID_FAMILIES[x] & specific)}


def carries(labels: set, fluid: str) -> bool:
    return not labels or any(fluid in _members(lb) for lb in labels)


def set_fluid_check(base_dir: Path, fn) -> None:
    """注册管子流体识别（拼合图管网块 → 流体名）。注册后不同流体的管子不会被暗管/桥/断口并到一起。"""
    _FLUID_CHECK[str(base_dir)] = fn
    for k in [k for k in _FLUID_CACHE if k[0] == str(base_dir)]:
        del _FLUID_CACHE[k]


def _fluid_labels(base_dir: Path, lab: np.ndarray) -> dict:
    fn = _FLUID_CHECK.get(str(base_dir))
    if fn is None:
        return {}
    key = (str(base_dir), int(lab.max()), int((lab > 0).sum()))
    if key not in _FLUID_CACHE:
        _FLUID_CACHE[key] = fn(lab)
    return _FLUID_CACHE[key]


def _gap_pairs(base_dir: Path, lab: np.ndarray) -> list[tuple[int, int]]:
    fn = _GAP_CHECK.get(str(base_dir))
    if fn is None:
        return []
    key = (str(base_dir), int(lab.max()), int((lab > 0).sum()))
    if key in _GAP_CACHE:
        return _GAP_CACHE[key]
    H, W = lab.shape
    n = int(lab.max())
    boxes = {}
    ys, xs = np.nonzero(lab)
    for c in range(1, n + 1):
        sel = lab[ys, xs] == c
        if sel.sum() >= 30:
            boxes[c] = (xs[sel].min(), ys[sel].min(), xs[sel].max(), ys[sel].max())
    seen, out = set(), []
    for a, (x0, y0, x1, y1) in boxes.items():
        X0, Y0, X1, Y1 = max(0, x0 - GAP_MAX), max(0, y0 - GAP_MAX), min(W, x1 + GAP_MAX + 1), min(H, y1 + GAP_MAX + 1)
        sub = lab[Y0:Y1, X0:X1]
        dt, _ = cv2.distanceTransformWithLabels((sub != a).astype(np.uint8), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
        for b in np.unique(sub):
            if not b or b == a or (min(a, b), max(a, b)) in seen or b not in boxes:
                continue
            by_, bx_ = np.nonzero(sub == b)
            d = dt[by_, bx_]
            k = int(d.argmin())
            if d[k] > GAP_MAX:
                continue
            seen.add((min(a, b), max(a, b)))
            pb = (bx_[k] + X0, by_[k] + Y0)
            ay, ax = np.nonzero(sub == a)
            j = int(np.hypot(ax + X0 - pb[0], ay + Y0 - pb[1]).argmin())
            pa = (ax[j] + X0, ay[j] + Y0)
            if np.hypot(pa[0] - pb[0], pa[1] - pb[1]) < 3:
                continue  # 本来就挨着（会在同一块里）
            # 只差几个像素的：拼合图上是设备图标（仓库取货口等）盖住了管子，游戏近景里同一处也被图标挡着看不到管子，
            # 不用近景核实直接接（2026-11-08 武陵城精炼炉 (1060,502) 的进水管在取货口图标下断开 4 px）；不同流体的仍由 union 挡住
            if np.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= GAP_TINY or fn(pa, pb):
                out.append((int(a), int(b)))
    _GAP_CACHE[key] = out
    return out


def unpaired_outlets(base_dir: Path) -> list[tuple[int, int]]:
    """所有暗管两头（拼合图坐标）。从它们出发、却连不到任何输入端的管网，水/气是从基地外直接接进暗管的
    （2026-11-08 武陵城：基地外的水泵直接接右边缘的暗管入口 (2260,1090)，没有准入口），进来多少不知道。"""
    tf = json.loads((base_dir / "transform.json").read_text(encoding="utf-8"))
    out = []
    for bp_name in tf.get("bps", {}):
        lp = base_dir.parent.parent / "bp" / bp_name / "links.json"
        mp = base_dir.parent.parent / "bp" / bp_name / "mosaic.png"
        if not lp.exists() or not mp.exists():
            continue
        lk = json.loads(lp.read_text(encoding="utf-8"))
        shape = V.rd(mp).shape
        for k, p in enumerate(lk.get("pipes", [])):
            x, y = V.preview_to_canvas(tf, bp_name, shape, *p["p"])
            out.append((int(round(x)), int(round(y))))
    return out


def area_check(base_dir: Path, ledger: dict, recipes: list[dict], fluid: str = "清水") -> dict:
    """这个据点 fluid 管网追踪可不可信（闭合自检）。可信 = 每台用 fluid 的设备恰好落在一张网上、没有网把别的流体的输入端连进来。
    2026-11-08：景玉谷 12/12 台、4 张网全零余量 → 可信；武陵城 4 张蓝图旋转 90°、管道交叉多，有 8 台没落到网上、一台洪炉落在两张网上、
    壤晶废液输入端被连进清水网 → 不可信，巡检退回原来的判断。返回 {trusted, nets, coverage, problems}。"""
    inputs = load_inputs(base_dir)
    gates = [m for m in inputs if (m.get("item") or "") == fluid]
    nets, seen, where, problems = [], set(), {}, []
    for m in gates:
        r = network(base_dir, ledger, recipes, m["no"], inputs, fluid)
        if not r or r.get("note"):
            problems.append(f"输入端 {m['no']}：{(r or {}).get('note', '没认出管网')}")
            continue
        key = tuple(sorted(g["no"] for g in r["same_gates"]))
        if key in seen:
            continue
        seen.add(key)
        other = [g for g in inputs if g["no"] in key and (g.get("item") or "") != fluid]
        if other:
            problems.append(f"输入端 {key} 这张网连进了别的流体（{'、'.join(f'{g['no']}{g['item']}' for g in other)}）")
        for u in r["users"]:
            where.setdefault((u["name"], *u["far"]), []).append(key)
        nets.append(r)
    # 暗管连着、却没有输入端的网（基地外直接接进暗管，量未知）：网上用 fluid 的设备算「外部暗管来水」，不算没落到网上
    ext_seen = set()
    for p in unpaired_outlets(base_dir):
        r = network(base_dir, ledger, recipes, None, inputs, fluid, start_tunnel=p)
        if not r or r.get("note") or r["same_gates"] or not r["users"]:
            continue  # 接到了有输入端的网（上面已算过）或没有用 fluid 的设备
        sig = tuple(sorted((u["name"], *u["far"]) for u in r["users"]))
        if sig in ext_seen:
            continue
        ext_seen.add(sig)
        key = ("暗管", *p)
        for u in r["users"]:
            where.setdefault((u["name"], *u["far"]), []).append(key)
        r["external"] = list(p)
        nets.append(r)
    dup = {k: v for k, v in where.items() if len(v) > 1}
    if dup:
        problems.append("同一台落在几张网上：" + "、".join(f"{k[0]}({k[1]},{k[2]})" for k in dup))
    # 这个据点所有用 fluid 的设备（有配方的，加按产物图标/同类推断的）
    table = recipe_table(base_dir, ledger, recipes)
    users_all = [(d["name"], d["x"], d["y"]) for d in ledger.get("devices", [])
                 if water_per_min(table.get((d["name"], d["x"], d["y"]), ("", False))[0], fluid, d["name"]) > 0]
    missing = [u for u in users_all if u not in where]
    if missing:
        problems.append(f"{len(missing)} 台用{fluid}的设备没落到任何一张网上：" + "、".join(f"{u[0]}({u[1]},{u[2]})" for u in missing[:8]))
    return {"trusted": not problems, "nets": nets, "coverage": [len(users_all) - len(missing), len(users_all)], "problems": problems}


def impact_image(base_dir: Path, imp: dict, scale: float = 0.5) -> np.ndarray:
    """拼合图上标出：断开的输入端（紫圈）、同网输入端（蓝圈）、管网（红）、下游设备（绿框 + 用量）。"""
    from PIL import Image, ImageDraw, ImageFont
    canvas = V.rd(base_dir / "composite.png")
    logistics = json.loads((base_dir / "logistics.json").read_text(encoding="utf-8"))
    _, lab = cv2.connectedComponents(pipe_mask(canvas, logistics))
    gx, gy = imp["gate_p"]
    w = lab[max(0, gy - 25):gy + 26, max(0, gx - 25):gx + 26]
    ids, c = np.unique(w[w > 0], return_counts=True)
    vis = canvas.copy()
    if len(ids):
        vis[lab == ids[c.argmax()]] = (40, 40, 230)
    pil = Image.fromarray(cv2.cvtColor(vis, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    F = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 26)
    for m in imp["same_gates"]:
        x, y = m["p"]
        col = (170, 0, 200) if m["no"] == imp["gate"] else (0, 110, 230)
        dr.ellipse([x - 30, y - 30, x + 30, y + 30], outline=col, width=6)
        dr.text((x - 150, y - 50), f"输入端{m['no']}" + ("（断开）" if m["no"] == imp["gate"] else ""), fill=col, font=F)
    for u in imp["users"]:
        x, y, w_, h_ = u["rect"]
        dr.rectangle([x, y, x + w_, y + h_], outline=(0, 160, 0), width=6)
        dr.text((x + 6, y + 6), f"{u['name']} {u['need']:g}/分", fill=(0, 120, 0), font=F)
    out = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
