"""设备端口布局：按游戏数据（FactoryBuildingTable 的端口格）+ 图上的外框和口格折角，算出每台设备每个口在拼合图上的位置。

数据来自 data/building_ports.json（tools/build_data.py 生成）。坐标换算规则用的是 IndustrialPlanner 在游戏里实测校准的那套。
看过的生产设备都是同一个格局：一条长边全是带子进口，对边全是带子出口；以物料从上往下流为准，管子进口在左（W）、出口在右（E）。

图上能直接看到的只有外框和带子口格里的小折角（design.glyph_sides）。所以做法是：
1. 认外框，量出外框是几格乘几格，只留下转过以后占地对得上的朝向（长方形设备剩 2 个，方形设备剩 4 个）；
2. 用口格折角挑朝向：哪条边是进口、哪条边是出口。挑不出唯一一个就不给端口，宁缺毋滥；
3. 朝向定了，带子口和管口的位置、进出全由数据决定，管口不用再从标签往外探。

不能用的情况：外框没认出、外框格数和数据对不上、只有管口的设备（暗管、水泵、储液罐：图上没有折角可读）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import vision as V

C = V.CELL
ROT_SIDE = {"N": "E", "E": "S", "S": "W", "W": "N"}  # 顺时针转 90°：北边转到东边


@lru_cache(maxsize=1)
def layouts() -> dict:
    try:
        from paths import ROOT  # 打包后是 _internal（和 design.py 读 data/recipes.json 一样）
    except ImportError:
        ROOT = Path(__file__).resolve().parents[2]
    f = ROOT / "data" / "building_ports.json"
    if not f.exists():
        return {}
    return json.loads(f.read_text(encoding="utf-8"))


def _rot_side(s: str, rot: int) -> str:
    for _ in range(rot // 90):
        s = ROT_SIDE[s]
    return s


def _rot_cell(x: int, y: int, w: int, d: int, rot: int) -> tuple[int, int, int, int]:
    """(x, y) 在 w×d 格里，整台设备顺时针转 rot 度以后的格子和新的宽高。"""
    for _ in range(rot // 90):
        x, y, w, d = d - 1 - y, x, d, w
    return x, y, w, d


def _fits(n_px: float, cells: int) -> bool:
    return -0.8 <= n_px / C - cells <= 0.2  # 图上外框线往里缩：3×3 画出来约 2.65 格、5×5 约 4.6 格


def rotations(name: str, box) -> list[int]:
    """外框格数对得上的朝向。"""
    lay = layouts().get(name)
    if lay is None or box is None:
        return []
    out = []
    for rot in (0, 90, 180, 270):
        w, d = (lay["w"], lay["d"]) if rot in (0, 180) else (lay["d"], lay["w"])
        if _fits(box[2], w) and _fits(box[3], d):
            out.append(rot)
    return out


def _lines(gray, x, y, w, h, horiz: bool) -> list[int]:
    """外框范围里（两头各多看 4 px）贯通的外框线位置：粗黑线（<90）≥60%，或细灰线（<150）≥80%。horiz = 横线。"""
    H, W = gray.shape
    out = []
    if horiz:
        for yy in range(max(0, y - 4), min(H, y + h + 5)):
            r = gray[yy, max(0, x + 6):min(W, x + w - 6)]
            if (r < 90).mean() >= 0.6 or (r < 150).mean() >= 0.8:  # 粗黑边 / 没有口的那几条细灰边（灰度 120–130）
                out.append(yy)
    else:
        for xx in range(max(0, x - 4), min(W, x + w + 5)):
            r = gray[max(0, y + 6):min(H, y + h - 6), xx]
            if (r < 90).mean() >= 0.6 or (r < 150).mean() >= 0.8:
                out.append(xx)
    return out


def fit_box(gray, name: str, box):
    """外框比这台设备应有的大小大出 0.2–1.2 格（和旁边设备的外框连在一起了）：在多出来的那个方向上，
    找两条间距最接近应有大小（±0.3 格）的贯通深色线，当作真正的外框边。
    2026-11-10 武陵城种植机 (155,1158)：外框连上了上下的东西，高 182 px（5×5 应画约 160，实际边在 1077 / 1237）。"""
    lay = layouts().get(name)
    if lay is None or box is None or rotations(name, box):
        return box
    x, y, w, h = box
    for W_, D_ in {(lay["w"], lay["d"]), (lay["d"], lay["w"])}:
        nx, ny, nw, nh = x, y, w, h
        ok = True
        for horiz, n_px, cells in ((False, w, W_), (True, h, D_)):
            if _fits(n_px, cells):
                continue
            if not (0.2 < n_px / C - cells <= 1.2):
                ok = False
                break
            exp = cells * C - 0.35 * C
            ls = _lines(gray, x, y, w, h, horiz)
            pairs = [(abs((b - a) - exp), a, b) for a in ls for b in ls if b - a > 0 and abs((b - a) - exp) <= 0.3 * C]
            if not pairs:
                ok = False
                break
            _, a, b = min(pairs)
            if horiz:
                ny, nh = a, b - a
            else:
                nx, nw = a, b - a
        nb = (nx, ny, nw, nh)
        if ok and rotations(name, nb):
            return nb
    return box


def belt_sides(name: str, rot: int, pipe: bool = False) -> dict:
    """朝向 rot 时每条边上带子口（pipe=True：管口）的角色：{边: "in"/"out"/"mixed"}。"""
    res = {}
    for p in layouts().get(name, {}).get("ports", []):
        if p["pipe"] == pipe:
            s = _rot_side(p["side"], rot)
            res[s] = p["role"] if res.get(s, p["role"]) == p["role"] else "mixed"
    return res


def thick_sides(gray: np.ndarray, box) -> set:
    """外框哪几条边是粗黑边（物品口所在的边，2–3 px 深黑线；其余边是细灰线）。和 design.glyph_sides 里的判法一样。"""
    x, y, w, h = box
    H, W = gray.shape
    out = set()
    for sd in "NSWE":
        if sd in "NS":
            ys = range(y - 3, y + 2) if sd == "N" else range(y + h - 2, y + h + 3)
            ok = any(0 <= yy < H and (gray[yy, max(0, x + 4):min(W, x + w - 4)] < 70).mean() >= 0.7 for yy in ys)
        else:
            xs = range(x - 3, x + 2) if sd == "W" else range(x + w - 2, x + w + 3)
            ok = any(0 <= xx < W and (gray[max(0, y + 4):min(H, y + h - 4), xx] < 70).mean() >= 0.7 for xx in xs)
        if ok:
            out.add(sd)
    return out


def pick_rotation(name: str, box, glyphs: dict, thick: set | None = None) -> tuple[int | None, str]:
    """→ (朝向, 依据)。glyphs = design.glyph_sides 的结果，thick = 粗黑边。
    每条读到折角的边：这个朝向那边有带子口 → 进出对上算一票、对不上算矛盾；只有管口 → 管口半圆里的 > 也指着流向
    （2026-11-10 看三个据点：精炼炉、种植机、天有洪炉的进水口都读成「进」），同样算票 / 矛盾；什么口都没有 → 矛盾。
    没有矛盾、票数最多的朝向唯一才给；票数一样时用粗黑边挑（粗黑边判得宽，邻居的边线、管子也会算进来，只用来挑，不用来否）。"""
    rots = rotations(name, box)
    if not rots:
        return None, "外框格数和数据对不上" if box is not None else "没认出外框"
    scored = []
    for rot in rots:
        belt, pipe = belt_sides(name, rot), belt_sides(name, rot, pipe=True)
        if not belt:
            return None, "没有带子口，折角定不了朝向"
        good = bad = 0
        for s, r in glyphs.items():
            exp = belt.get(s) or pipe.get(s)
            if exp == r:
                good += 1
            elif exp != "mixed":
                bad += 1
        scored.append((rot, good, bad))
    ok = [t for t in scored if t[2] == 0 and t[1] >= 1]
    top = max((t[1] for t in ok), default=0)
    ok = [t for t in ok if t[1] == top]
    if len(ok) > 1 and thick:
        ok2 = [t for t in ok if set(belt_sides(name, t[0])) <= thick]
        if len(ok2) == 1:
            return ok2[0][0], f"口格折角对上 {ok2[0][1]} 条边，粗黑边挑出朝向"
    if len(ok) == 1:
        return ok[0][0], f"口格折角对上 {ok[0][1]} 条边"
    if not glyphs:
        return None, "折角没读到"
    return None, "折角定不下唯一朝向" if ok else "折角和数据矛盾"


def port_cells(name: str, box, rot: int) -> list[dict]:
    """朝向 rot 时每个口：{c: 拼合图上口格中心, side, role, pipe, edge: 口格外缘中点（真实格线）}。
    外框线画得比真实占地往里缩（3×3 画成约 92 px，真实 3 格 = 104 px）：口格按真实格距 C 从外框中心往外排，不按外框等比缩放。"""
    lay = layouts()[name]
    x0, y0, bw, bh = box
    mx, my = x0 + bw / 2, y0 + bh / 2
    out = []
    for p in lay["ports"]:
        x, y, W, H = _rot_cell(p["x"], p["y"], lay["w"], lay["d"], rot)
        cx, cy = mx + (x - (W - 1) / 2) * C, my + (y - (H - 1) / 2) * C
        side = _rot_side(p["side"], rot)
        v = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}[side]
        edge = (cx + v[0] * C / 2, cy + v[1] * C / 2)
        out.append({"c": (float(cx), float(cy)), "edge": (float(edge[0]), float(edge[1])), "side": side,
                    "role": p["role"], "pipe": p["pipe"]})
    return out


def device_ports(gray: np.ndarray, dark_thin: np.ndarray, rects, name: str, c) -> dict | None:
    """一台设备（底账名 name、标签在 c）→ {box, rot, basis, ports}；朝向定不了时 rot/ports 为 None（box 仍给）。"""
    if name not in layouts():
        return None
    from .design import glyph_sides
    from .pipenet import device_rect
    box = device_rect(dark_thin, c[0], c[1], rects)
    if box is None:
        return {"box": None, "rot": None, "ports": None, "basis": "没认出外框"}
    box = fit_box(gray, name, box)
    g = glyph_sides(gray, box)
    th = thick_sides(gray, box)
    rot, why = pick_rotation(name, box, g, th)
    return {"box": [int(v) for v in box], "rot": rot, "basis": why, "glyphs": g, "thick": sorted(th),
            "ports": port_cells(name, box, rot) if rot is not None else None}


def all_ports(canvas: np.ndarray, ledger_pts: list[dict]) -> list[dict | None]:
    """ledger_pts = [{name, c}] → 每台设备的 device_ports 结果（顺序一致）。"""
    import cv2
    from .pipenet import _rects
    gray = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
    dark_thin = ((gray < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    rects = _rects(canvas)
    return [device_ports(gray, dark_thin, rects, p["name"], p["c"]) for p in ledger_pts]


def port_near(devs: list[dict | None], P, d: str, pipe: bool, tol: float = 0.55 * C) -> tuple[int, dict] | None:
    """带子/管子头 P 正往 d 方向走（走进设备）：哪台设备哪个口的外缘就在前面（口在 d 的反方向那条边上、横向偏差 ≤ tol）。
    唯一一个才给。→ (设备序号, 口)。"""
    back = {"N": "S", "S": "N", "E": "W", "W": "E"}[d]
    v = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}[d]
    hits = []
    for i, dp in enumerate(devs):
        if not dp or not dp.get("ports"):
            continue
        for p in dp["ports"]:
            if p["pipe"] != pipe or p["side"] != back:
                continue
            dx, dy = p["edge"][0] - P[0], p["edge"][1] - P[1]
            along = dx * v[0] + dy * v[1]
            lat = abs(dx * v[1] - dy * v[0])
            if -0.6 * C <= along <= 0.9 * C and lat <= tol:
                hits.append((lat + abs(along) * 0.2, i, p))
    if not hits:
        return None
    hits.sort(key=lambda t: t[0])
    if len(hits) > 1 and hits[1][1] != hits[0][1] and hits[1][0] - hits[0][0] < 0.25 * C:
        return None  # 两台设备的口差不多近：不认
    return hits[0][1], hits[0][2]
