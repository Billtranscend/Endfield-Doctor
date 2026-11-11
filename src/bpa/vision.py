"""蓝图分析：离线图像处理（不碰游戏）。

- stitch：预览帧拼接（滚动条约束，空网格不会错整格）
- detect：在预览拼图里认暗管（单口/多口 × 入口/出口）和息壤供电桩
- ray_end / pick_partner：暗管连接线识别与配对
- fit_blueprint / composite：按底账自动找锚点，把同一据点的几张蓝图拼到基地上

坐标约定：预览拼图像素 p；远景底账坐标 f（720p、最远缩放）；f = t + K·R(p)，R 为 0/90/180/270° 旋转。
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from paths import ROOT

RES = ROOT / "resource" / "bpa"
V = (8, 66, 932, 672)  # 预览画布可用区域（去掉左侧渐隐、顶部标题、底部滚动条）
CELL = 34.75  # 预览 1 格 = 34.75 px
K = 0.5787  # 远景 px / 预览 px（实测：远景 1 格 ≈ 20.1 px）
VIEW_EFF = (900, 581)  # 滚动条换算用的有效视野（实测）


def rd(p) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def wr(p, im, q: int | None = None) -> None:
    p = Path(p)
    ext = p.suffix.lower()
    params = [cv2.IMWRITE_JPEG_QUALITY, q or 92] if ext in (".jpg", ".jpeg") else []
    cv2.imencode(ext, im, params)[1].tofile(str(p))


# ------------------------------------------------------------------ 滚动条 / 拼接
def thumbs(im):
    """底部、右侧滚动条滑块的 (起, 止)。"""
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    r = np.where(g[679, 0:940] < 90)[0]
    c = np.where(g[60:690, 944] < 90)[0] + 60
    return ((int(r[0]), int(r[-1])) if len(r) > 20 else None), ((int(c[0]), int(c[-1])) if len(c) > 20 else None)


def _crop(im):
    return im[V[1]:V[3], V[0]:V[2]]


_orb = None


def _vectors(a, b):
    global _orb
    if _orb is None:
        _orb = (cv2.ORB_create(3000), cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True))
    orb, bf = _orb
    ga, gb = cv2.cvtColor(_crop(a), cv2.COLOR_BGR2GRAY), cv2.cvtColor(_crop(b), cv2.COLOR_BGR2GRAY)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    if da is None or db is None:
        return np.zeros((0, 2))
    return np.array([np.array(kb[m.trainIdx].pt) - np.array(ka[m.queryIdx].pt) for m in bf.match(da, db)]).reshape(-1, 2)


def _best_near(v, prior, tol=15):
    if len(v) == 0:
        return None
    sel = np.abs(v - prior).max(axis=1) <= tol
    if sel.sum() < 8:
        return None
    c = Counter(map(tuple, np.round(v[sel] / 2).astype(int)))
    (kx, ky), _ = c.most_common(1)[0]
    s2 = np.abs(v - [kx * 2, ky * 2]).max(axis=1) <= 3
    return np.median(v[s2], axis=0), int(s2.sum())


def stitch(frames: list[np.ndarray], log=print) -> np.ndarray:
    """按拍摄顺序拼接预览帧。帧间位移先由滚动条推算，ORB 只在先验 ±15 px 内投票。"""
    T = [thumbs(im) for im in frames]
    V2 = [_vectors(frames[i - 1], frames[i]) for i in range(1, len(frames))]
    sx, sy = [], []
    for i, v in enumerate(V2):
        if len(v) == 0:
            continue
        c = Counter(map(tuple, np.round(v / 2).astype(int)))
        (kx, ky), n = c.most_common(1)[0]
        a, b = T[i], T[i + 1]
        if n < 300:
            continue
        if a[0] and b[0] and abs(b[0][0] - a[0][0]) > 20:
            sx.append(kx * 2 / (b[0][0] - a[0][0]))
        if a[1] and b[1] and abs(b[1][0] - a[1][0]) > 20:
            sy.append(ky * 2 / (b[1][0] - a[1][0]))
    lx = np.median([t[0][1] - t[0][0] for t in T if t[0]]) if any(t[0] for t in T) else None
    ly = np.median([t[1][1] - t[1][0] for t in T if t[1]]) if any(t[1] for t in T) else None
    SX = float(np.median(sx)) if len(sx) >= 2 else (-VIEW_EFF[0] / lx if lx else -1.0)
    SY = float(np.median(sy)) if len(sy) >= 2 else (-VIEW_EFF[1] / ly if ly else -1.0)
    pos = [np.zeros(2)]
    for i, v in enumerate(V2):
        a, b = T[i], T[i + 1]
        px = SX * (b[0][0] - a[0][0]) if a[0] and b[0] else 0.0
        py = SY * (b[1][0] - a[1][0]) if a[1] and b[1] else 0.0
        r = _best_near(v, np.array([px, py]))
        pos.append(pos[-1] + (np.array([px, py]) if r is None else r[0]))
    P = np.array(pos)
    w, h = V[2] - V[0], V[3] - V[1]
    W, H = int(w - P[:, 0].min() + P[:, 0].max()) + 2, int(h - P[:, 1].min() + P[:, 1].max()) + 2
    ox, oy = -P[:, 0].max(), -P[:, 1].max()
    canvas = np.full((H, W, 3), 235, np.uint8)
    best = np.full((H, W), np.inf, np.float32)
    yy, xx = np.mgrid[0:h, 0:w]
    dist = (np.abs(xx - w / 2) / w + np.abs(yy - h / 2) / h).astype(np.float32)
    for p, im in zip(P, frames):
        x, y = int(round(-p[0] - ox)), int(round(-p[1] - oy))
        c = _crop(im)
        sel = dist < best[y:y + h, x:x + w]
        canvas[y:y + h, x:x + w][sel] = c[sel]
        best[y:y + h, x:x + w][sel] = dist[sel]
    return canvas


def size_ok(mosaic: np.ndarray, size_cells: tuple[int, int] | None, tol_px: int = 20) -> tuple[bool, str]:
    """拼图尺寸自检：应等于 蓝图尺寸 × 34.75。"""
    if not size_cells:
        return True, "没读到蓝图尺寸，跳过尺寸自检"
    ew, eh = size_cells[0] * CELL, size_cells[1] * CELL
    h, w = mosaic.shape[:2]
    ok = abs(w - ew) <= tol_px and abs(h - eh) <= tol_px
    return ok, f"拼图 {w}×{h} px，应为 {ew:.0f}×{eh:.0f}（{size_cells[0]}×{size_cells[1]} 格）"


# ------------------------------------------------------------------ 图标识别
TPL_NAMES = {"单_出口": "暗管出口", "单_入口": "暗管入口", "多_出口": "多口暗管出口", "多_入口": "多口暗管入口", "供电桩": "息壤供电桩"}
_tpl = None


def _templates():
    global _tpl
    if _tpl is None:
        _tpl = {}
        for n in TPL_NAMES:
            t = cv2.cvtColor(rd(RES / "tpl" / f"{n}.png"), cv2.COLOR_BGR2GRAY)
            h, w = t.shape
            _tpl[n] = t[h // 2 - 26:h // 2 + 26, w // 2 - 26:w // 2 + 26]  # 中心图标 52×52（图标始终正立）
    return _tpl


def detect(mosaic: np.ndarray, thr: float = 0.72) -> list[dict]:
    """暗管和息壤供电桩：[{name, p:[x,y], score}]。"""
    g = cv2.cvtColor(mosaic, cv2.COLOR_BGR2GRAY)
    hits = []
    for n, t in _templates().items():
        r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.nonzero(r > thr)
        hits += [(float(r[y, x]), int(x) + 26, int(y) + 26, n) for x, y in zip(xs, ys)]
    hits.sort(reverse=True)
    keep = []
    for s, x, y, n in hits:
        if all(abs(x - k[1]) > 40 or abs(y - k[2]) > 40 for k in keep):
            keep.append((s, x, y, n))
    return [{"name": TPL_NAMES[n], "p": [x, y], "score": round(s, 3)} for s, x, y, n in keep]


PIPE_NAMES = ("暗管入口", "暗管出口", "多口暗管入口", "多口暗管出口")


def pipe_count_check(found: list[dict], devices: dict) -> tuple[bool, dict, dict]:
    got = {n: sum(1 for d in found if d["name"] == n) for n in PIPE_NAMES}
    exp = {n: int(devices.get(n) or 0) for n in PIPE_NAMES}
    return got == exp, got, exp


# ------------------------------------------------------------------ 连接线
def locate(img, mos, cal):
    """当前画面在拼图里的偏移（拼图坐标 = 屏幕坐标 + 偏移）：滚动条预测 + ±12 px 模板细调。
    cal = {"t0": 第 00 帧滑块起点, "r": 滑块→画面比例}（见 calib）。"""
    hx, hy = thumbs(img)
    px = -V[0] + (cal["r"][0] * (hx[0] - cal["t0"][0]) if hx and cal["t0"][0] is not None else 0)
    py = -V[1] + (cal["r"][1] * (hy[0] - cal["t0"][1]) if hy and cal["t0"][1] is not None else 0)
    c = img[V[1] + 150:V[3] - 150, V[0] + 250:V[2] - 250]
    x0, y0 = int(round(px)) + V[0] + 250 - 12, int(round(py)) + V[1] + 150 - 12
    H, W = mos.shape[:2]
    if 0 <= x0 and 0 <= y0 and x0 + c.shape[1] + 24 <= W and y0 + c.shape[0] + 24 <= H:
        r = cv2.matchTemplate(mos[y0:y0 + c.shape[0] + 24, x0:x0 + c.shape[1] + 24], c, cv2.TM_CCOEFF_NORMED)
        _, v, _, p = cv2.minMaxLoc(r)
        if v > 0.6:
            return x0 + p[0] - (V[0] + 250), y0 + p[1] - (V[1] + 150), float(v)
    return px, py, 0.99


def calib(frame0: np.ndarray) -> dict:
    hx, hy = thumbs(frame0)
    return {"t0": (hx[0] if hx else None, hy[0] if hy else None),
            "r": (VIEW_EFF[0] / (hx[1] - hx[0]) if hx else 0, VIEW_EFF[1] / (hy[1] - hy[0]) if hy else 0)}


def _edge_mask(m):
    m[:V[1] + 4] = False
    m[V[3] - 4:] = False
    m[:, V[2] - 4:] = False
    m[:, :V[0] + 30] = False
    return m


def cmask(im):
    """连接线的亮青色（严格）：判方向用，避开蓝色环境横幅。"""
    bb, gg, rr = [im[..., k].astype(int) for k in range(3)]
    return _edge_mask((bb > 240) & (gg > 215) & (rr > 140) & (rr < 215))


def cmask_loose(im):
    """宽松：线穿过别的设备后会变淡（约 219,200,146），量长度用。"""
    bb, gg, rr = [im[..., k].astype(int) for k in range(3)]
    return _edge_mask((bb > 180) & (bb - rr > 40) & (gg - rr > 20))


def ray_end(im, cx, cy, d=None):
    """从 (cx,cy) 出发的连接线 → (端点, 方向)。≤110 px 的断口当作被设备挡住，取直线上最远的线段末端。"""
    ys, xs = np.nonzero(cmask(im))
    vx, vy = xs - cx, ys - cy
    rr = np.hypot(vx, vy)
    if d is None:
        sel = rr > 75
        if sel.sum() < 30:
            return None, None
        ang = np.degrees(np.arctan2(vy[sel], vx[sel])) % 360
        h = np.bincount((ang * 2).astype(int) % 720, minlength=720)
        h = h + np.roll(h, 1) + np.roll(h, -1)
        pk = h.argmax() / 2
        d = np.array([math.cos(math.radians(pk)), math.sin(math.radians(pk))])
    along = vx * d[0] + vy * d[1]
    perp = np.abs(vx * d[1] - vy * d[0])
    sel = (perp < 25) & (along > 40)
    if sel.sum() < 15:
        return None, d
    pts = np.stack([xs[sel], ys[sel]], 1).astype(np.float32)
    fx, fy, x0, y0 = cv2.fitLine(pts, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    dd = np.array([fx, fy])
    dd = dd if dd @ d > 0 else -dd
    ys, xs = np.nonzero(cmask_loose(im) | cmask(im))
    perp2 = np.abs((xs - x0) * dd[1] - (ys - y0) * dd[0])
    al2 = (xs - cx) * dd[0] + (ys - cy) * dd[1]
    sel2 = (perp2 < 3) & (al2 > 40)
    if sel2.sum() < 15:
        return None, dd
    al = np.sort(al2[sel2])
    runs = [r for r in np.split(al, np.nonzero(np.diff(al) > 6)[0] + 1) if len(r) >= 8]
    if not runs:
        return None, dd
    # 选中暗管的高亮框边也是青色（离中心 ~50 px）。线出框后常被准入口等挡住、或在出口端渐隐，
    # 和框边之间的断口可达 150 px（应龙关3号 实测），所以框内的段不作为起点。
    far = [r for r in runs if r[-1] >= 75]
    if far:
        runs = far
    t = runs[0][-1]
    for r in runs[1:]:
        if r[0] - t > 110:
            break
        t = r[-1]
    base = np.array([x0, y0]) + dd * ((cx - x0) * dd[0] + (cy - y0) * dd[1])
    return (float(base[0] + dd[0] * t), float(base[1] + dd[1] * t)), dd


def at_edge(x, y) -> bool:
    return not (V[0] + 45 < x < V[2] - 20 and V[1] + 20 < y < V[3] - 20)


def pick_partner(start, end, me_name, cands):
    """拼图坐标：start→end 直线上（偏离 <50）、类型相反、不越过端点 +70 的最远一个暗管。返回 (−沿线, 下标, 沿线差, 偏离) 或 None。"""
    st, en = np.array(start, float), np.array(end, float)
    dv = en - st
    La = np.linalg.norm(dv)
    if La < 1:
        return None
    dv /= La
    kind = "入口" if "入口" in me_name else "出口"
    best = []
    for j, c in enumerate(cands):
        w = np.array(c["p"], float) - st
        al = w @ dv
        pp = abs(w[0] * dv[1] - w[1] * dv[0])
        # 多口暗管的框大一格，线停在离中心更远处（应龙关3号 实测 81–83 px）
        over = 110 if "多口" in c["name"] else 70
        if kind == "入口":
            # 线在出口那端渐隐，隐掉的长度约为全长的 11.5%（实测 137/1320、166/1425、180/1550、197/1730），
            # 所以从入口点出去量到的端点会比出口早停。按距离给余量。
            over = max(over, 0.16 * al + 20)
        if al > 80 and pp < 50 and La - 220 < al < La + over and kind not in c["name"]:
            best.append((-al, j, al - La, pp))
    if not best:
        return None
    # 真配对的暗管几乎正在线上（偏离 ≤4）。只在「偏离不比最小的大 8 px 以上」的里面取最远的，
    # 免得线稍微画过头时取到旁边一个更远、偏离 18 px 的（应龙关2号 实测）。
    pmin = min(b[3] for b in best)
    best = sorted(b for b in best if b[3] <= pmin + 8)
    return best[0]


def link_status(n: int, part: dict[int, int]) -> dict[int, str]:
    """每个暗管 → 双向 / 单向 / 矛盾 / 未配。"""
    st = {}
    for i in range(n):
        j = part.get(i)
        claimed = [k for k, v in part.items() if v == i and k != j]
        if j is None:
            st[i] = "单向" if claimed else "未配"
        elif part.get(j) == i:
            st[i] = "双向"
        else:
            st[i] = "矛盾" if claimed else "单向"
    return st


# ------------------------------------------------------------------ 据点拼合
ROTS = (0, 90, 180, 270)


def Rp(rot, px, py):
    return {0: (px, py), 90: (-py, px), 180: (-px, -py), 270: (py, -px)}[rot]


def fit_blueprint(anchors: list[dict], ledger_devs: list[dict], tol: float = 22.0) -> dict | None:
    """anchors：预览里认出的 [{name, p}]（暗管、息壤供电桩）；ledger_devs：底账 [{name, x, y}]。
    四个旋转各自按「同类型配对 → 平移投票」找最多内点的变换，再用内点求平均平移。
    返回 {rot, t:[x,y], inliers, total, resid, second}（second = 次优解的内点数，用来判断有没有歧义）。"""
    if not anchors:
        return None
    by = {}
    for d in ledger_devs:
        by.setdefault(d["name"], []).append((d["x"], d["y"]))
    by = {k: np.array(v, float) for k, v in by.items()}
    results = []
    for rot in ROTS:
        votes = Counter()
        for a in anchors:
            L = by.get(a["name"])
            if L is None:
                continue
            q = K * np.array(Rp(rot, *a["p"]), float)
            for f in L:
                t = f - q
                votes[(int(round(t[0] / 10)), int(round(t[1] / 10)))] += 1
        for (bx, by_), _ in votes.most_common(5):
            t = np.array([bx * 10.0, by_ * 10.0])
            for _ in range(3):  # 迭代收紧
                res = []
                for a in anchors:
                    L = by.get(a["name"])
                    if L is None:
                        continue
                    q = t + K * np.array(Rp(rot, *a["p"]), float)
                    dd = np.hypot(*(L - q).T)
                    j = int(dd.argmin())
                    if dd[j] < tol * 1.5:
                        res.append(L[j] - q)
                if not res:
                    break
                t = t + np.mean(res, axis=0)
            inl = []
            for a in anchors:
                L = by.get(a["name"])
                if L is None:
                    continue
                q = t + K * np.array(Rp(rot, *a["p"]), float)
                dmin = float(np.hypot(*(L - q).T).min())
                if dmin < tol:
                    inl.append(dmin)
            results.append({"rot": rot, "t": [float(t[0]), float(t[1])], "inliers": len(inl), "total": len(anchors),
                            "resid": float(np.mean(inl)) if inl else 99.0})
    if not results:
        return None
    # 同一解会被多次投票命中：按 (rot, 平移取整) 去重后排序
    uniq = {}
    for r in results:
        k = (r["rot"], round(r["t"][0] / 15), round(r["t"][1] / 15))
        if k not in uniq or r["inliers"] > uniq[k]["inliers"]:
            uniq[k] = r
    ranked = sorted(uniq.values(), key=lambda r: (-r["inliers"], r["resid"]))
    best = dict(ranked[0])
    best["second"] = ranked[1]["inliers"] if len(ranked) > 1 else 0
    return best


def to_preview(rot: int, t, offset_canvas, F0, mos_shape, fx, fy):
    """远景坐标 → 预览拼图坐标（offset_canvas/F0 见 composite 返回的变换）。"""
    qx, qy = (np.array([fx, fy]) - F0) / K - np.array(offset_canvas)
    H, W = mos_shape[:2]
    return {0: (qx, qy), 180: (W - 1 - qx, H - 1 - qy), 90: (qy, H - 1 - qx), 270: (W - 1 - qy, qx)}[rot]


def composite(items: list[dict]) -> tuple[np.ndarray, dict]:
    """items：[{bp, mosaic, rot, t}] → (据点拼合图, 变换 {F0, bps:{bp:{rot,t,offset}}})。
    按基地朝向合成；每个像素取「墨迹」最多的蓝图；管道层压在传送带层上（用户说明）。"""
    ROT = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    prep = []
    for it in items:
        im = it["mosaic"].copy()
        im[:30, :160] = (232, 232, 232)
        H, W = im.shape[:2]
        shift = {0: (0, 0), 90: (H - 1, 0), 180: (W - 1, H - 1), 270: (0, W - 1)}[it["rot"]]
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        ink = ((g < cv2.blur(g, (61, 61)) - 28) | (hsv[..., 1] > 60)).astype(np.float32)
        ink[:, :40] = 0
        ink[:, -12:] = 0
        score = cv2.blur(ink, (13, 13)) + 0.02 * cv2.blur(ink, (61, 61))
        r = ROT[it["rot"]]
        imr = cv2.rotate(im, r) if r is not None else im
        if r is not None:
            score = cv2.rotate(score, r)
        prep.append((it, np.array(it["t"], float), np.array(shift, float), imr, score))
    F0 = np.min([t - K * sh for _, t, sh, _, _ in prep], axis=0)
    placed = []
    for it, t, sh, imr, sc in prep:
        off = np.round((t - F0) / K - sh).astype(int)
        placed.append((it, off, imr, sc))
    Wc = max(o[0] + im.shape[1] for _, o, im, _ in placed)
    Hc = max(o[1] + im.shape[0] for _, o, im, _ in placed)
    canvas = np.full((Hc, Wc, 3), 232, np.uint8)
    best = np.zeros((Hc, Wc), np.float32)
    for _, (x, y), im, sc in placed:
        h, w = sc.shape
        sel = sc > best[y:y + h, x:x + w]
        canvas[y:y + h, x:x + w][sel] = im[sel]
        best[y:y + h, x:x + w][sel] = sc[sel]
    hc = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
    gc = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    under = ((hc[..., 0] >= 15) & (hc[..., 0] <= 40) & (hc[..., 1] > 70)) | ((gc > 205) & (hc[..., 1] < 25))
    for _, (x, y), im, _ in placed:
        hh, ww = im.shape[:2]
        hi = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        gi = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        core = cv2.morphologyEx(((gi < 110) & (hi[..., 1] < 40)).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        pipe = cv2.dilate(core, np.ones((5, 5), np.uint8)).astype(bool)
        sel = pipe & under[y:y + hh, x:x + ww]
        canvas[y:y + hh, x:x + ww][sel] = im[sel]
    tf = {"K": K, "F0": F0.tolist(), "bps": {it["bp"]: {"rot": it["rot"], "t": list(map(float, it["t"])), "offset": off.tolist()} for it, off, _, _ in placed}}
    return canvas, tf


def far_to_canvas(tf: dict, fx: float, fy: float) -> tuple[float, float]:
    c = (np.array([fx, fy]) - np.array(tf["F0"])) / tf["K"]
    return float(c[0]), float(c[1])


def preview_to_canvas(tf: dict, bp: str, mos_shape, px: float, py: float) -> tuple[float, float]:
    b = tf["bps"][bp]
    f = np.array(b["t"]) + K * np.array(Rp(b["rot"], px, py), float)
    return far_to_canvas(tf, *f)
