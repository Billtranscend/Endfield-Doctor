"""蓝图分析：输入端候选 + 带编号截图（docs/蓝图分析器-设计.md 第 2.2 节）。

候选 = 接入口紧贴基地外侧的准入口（from_gates，用物流件识别的结果和用户给的规则）。
旧办法 candidates()（深色方框 + 彩色图标）留作没有物流件结果时用。漏的、错的由作者在界面上增删。
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import vision as V

EDGE = 2.0  # 离据点边缘几格以内算「边上」


def candidates(canvas: np.ndarray) -> list[dict]:
    g = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    dark = (g < 80).astype(np.float32)
    k = np.zeros((34, 34), np.float32)
    k[:3, :] = k[-3:, :] = k[:, :3] = k[:, -3:] = 1
    k /= k.sum()
    ring = cv2.filter2D(dark, -1, k)
    center = cv2.blur(dark, (16, 16))
    hsv = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
    belt = (hsv[..., 0] >= 15) & (hsv[..., 0] <= 40) & (hsv[..., 2] > 225)  # 传送带是很亮的黄色，不算物品图标（沉积酸图标是较暗的橙黄）
    sat = ((hsv[..., 1] > 70) & ~belt).astype(np.float32)
    colored = cv2.blur(sat, (22, 22))  # 物品图标有彩色圈；管道拐角、空格子没有
    H, W = g.shape
    band = int(EDGE * V.CELL)
    mask = np.zeros_like(ring, bool)
    mask[:band] = mask[-band:] = True
    mask[:, :band] = True
    mask[:, -band:] = True
    score = np.where(mask & (ring > 0.40) & (center < 0.6) & (colored > 0.08), ring, 0)
    mx = cv2.dilate(score, np.ones((31, 31), np.uint8))
    ys, xs = np.nonzero((score > 0) & (score >= mx))
    pts = sorted(zip(score[ys, xs], xs, ys), reverse=True)
    keep = []
    for s, x, y in pts:
        if all(abs(x - k2[0]) > 28 or abs(y - k2[1]) > 28 for k2 in keep):
            keep.append((int(x), int(y), float(s)))
    keep.sort(key=lambda p: _order(p, W, H))
    return [{"no": i + 1, "p": [x, y], "score": round(s, 2), "source": "auto"} for i, (x, y, s) in enumerate(keep)]


def _order(p, W, H):
    """编号：沿边界顺时针（上 → 右 → 下 → 左）。"""
    x, y = p[0], p[1]
    d = {0: y, 1: W - x, 2: H - y, 3: x}
    side = min(d, key=d.get)
    return (side, x if side == 0 else y if side == 1 else -x if side == 2 else -y)


TOUCH = 1.25  # 准入口中心离据点边缘几格以内算「紧贴外侧」（贴边的实测 25–34 px ≈ 0.7–1.0 格）
_DIRS = {"上": (0, -1), "下": (0, 1), "左": (-1, 0), "右": (1, 0)}
_OPP = {"上": "下", "下": "上", "左": "右", "右": "左"}


def _along(p, q, d):
    ox, oy = q[0] - p[0], q[1] - p[1]
    return ox * d[0] + oy * d[1], abs(ox * d[1] - oy * d[0])


def from_gates(canvas: np.ndarray, lg_canvas: list[dict], groups: dict | None = None,
               pipes_canvas: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """输入端候选（用户给的规则）：准入口的接入口紧贴基地外侧 = 提醒玩家从这里输入。
    - 准入口要紧贴拼合图边缘（TOUCH 格以内）；离边一两格的不算。
    - 两旁（沿边方向）接着分流器/汇流器/桥 → 它是横着走的，入口不朝外，不算（应龙关一排息壤气准入口就是这样）。
      两旁是别的准入口（一排水口）不影响。
    - 朝里一侧接的是暗管出口 → 东西是从暗管出来往外排的（如武陵城的壤晶废液），不算输入。
    预填：液体每口 60（一口一泵，水和沉积酸都这样）；接口按朝里接的暗管填「暗管/多口暗管」，液体没接暗管的填「水泵」；气体的量由作者填。
    返回 (候选, 排除的及原因)。"""
    H, W = canvas.shape[:2]
    groups = groups or {}
    pipes_canvas = pipes_canvas or []
    keep, drop = [], []
    for o in lg_canvas:
        if o["type"] != "准入口":
            continue
        x, y = o["p"]
        dist = {"上": y, "下": H - y, "左": x, "右": W - x}
        e = min(dist, key=dist.get)
        if dist[e] > 2 * V.CELL:
            continue
        g = groups.get(o.get("icon_group")) or {}
        rec = {"p": [x, y], "item": g.get("item", ""), "icon_group": o.get("icon_group"), "kind": o.get("kind", "?"), "bp": o.get("bp")}
        if dist[e] > TOUCH * V.CELL:
            drop.append(dict(rec, why=f"离{e}边 {dist[e] / V.CELL:.1f} 格，没贴着外侧"))
            continue
        lat = [k for k in _DIRS if k not in (e, _OPP[e])]
        side_lg = []
        for k in lat:
            for n in lg_canvas:
                if n is o or n["type"] == "准入口":
                    continue
                al, pp = _along((x, y), n["p"], _DIRS[k])
                if 25 < al < 45 and pp < 10:
                    side_lg.append(n["type"])
        if side_lg:
            drop.append(dict(rec, why=f"两旁接着{'、'.join(side_lg)}，入口不朝外"))
            continue
        inward = _DIRS[_OPP[e]]
        near = sorted((_along((x, y), pp_["p"], inward) + (pp_["name"],) for pp_ in pipes_canvas), key=lambda z: z[0])
        near = [z for z in near if 15 < z[0] < 120 and z[1] < 45]
        via = ""
        if near:
            nm = near[0][2]
            if "出口" in nm:
                drop.append(dict(rec, why=f"朝里接的是{nm}，是往外排的"))
                continue
            via = "多口暗管" if "多口" in nm else "暗管"
        st = g.get("state")
        if st == "液":
            rec["rate"] = 60
            rec["via"] = via or "水泵"
        else:
            rec["rate"] = None
            rec["via"] = via
        keep.append(rec)
    keep.sort(key=lambda r: _order(r["p"], W, H))
    marks = [dict(r, no=i + 1, source="auto", merged=False, note="") for i, r in enumerate(keep)]
    return marks, drop


def numbered_image(canvas: np.ndarray, marks: list[dict], scale: float = 0.8, dropped: list[dict] | None = None) -> Image.Image:
    """红圈 = 程序标的输入端，蓝圈 = 作者补的；灰色虚圈 + 字母 = 贴边但程序判断不算输入的准入口（作者可以加回）。"""
    im = cv2.resize(canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    pil = Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    F = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 18)
    W, H = pil.size

    placed = []

    def place(x, y, w):
        """编号牌位置：依次试右上、左上、右下、左下、更远的左边，取第一个不出图、不压别的编号牌的
        （2026-11-09 输入端都贴边，编号被切掉；挨着的两个口编号牌叠在一起）。"""
        bw, bh = w + 8, 24
        cands = [(x + 12, y - 30), (x - 22 - bw, y - 30), (x + 12, y + 8), (x - 22 - bw, y + 8), (x - 22 - bw, y - 12), (x - 30 - 2 * bw, y - 12)]
        for tx, ty in cands + [cands[0]]:
            r = (tx - 3, ty, tx - 3 + bw, ty + bh)
            if r[0] < 0 or r[1] < 0 or r[2] > W or r[3] > H:
                continue
            if any(not (r[2] <= q[0] or r[0] >= q[2] or r[3] <= q[1] or r[1] >= q[3]) for q in placed):
                continue
            placed.append(r)
            return tx, ty
        tx, ty = min(max(3, x + 12), W - bw), min(max(0, y - 30), H - bh)
        placed.append((tx - 3, ty, tx - 3 + bw, ty + bh))
        return tx, ty

    for i, d in enumerate(dropped or []):
        x, y = d["p"][0] * scale, d["p"][1] * scale
        for a0 in range(0, 360, 40):
            dr.arc([x - 18, y - 18, x + 18, y + 18], a0, a0 + 20, fill=(120, 120, 120), width=3)
        t = chr(ord("A") + i % 26)
        w = dr.textlength(t, font=F)
        tx, ty = place(x, y, w)
        dr.rounded_rectangle([tx - 3, ty, tx + w + 5, ty + 24], 5, fill=(120, 120, 120))
        dr.text((tx + 1, ty), t, fill="white", font=F)
    for m in marks:
        x, y = m["p"][0] * scale, m["p"][1] * scale
        col = (220, 30, 30) if m.get("source") == "auto" else (30, 90, 230)
        dr.ellipse([x - 18, y - 18, x + 18, y + 18], outline=col, width=3)
        t = str(m["no"])
        w = dr.textlength(t, font=F)
        tx, ty = place(x, y, w)
        dr.rounded_rectangle([tx - 3, ty, tx + w + 5, ty + 24], 5, fill=col)
        dr.text((tx + 1, ty), t, fill="white", font=F)
    return pil
