"""物流件的方向和连接（任务计划第 4 项；docs/蓝图分析器-设计.md 4.3「局部结构」、4.7 设计分析的前提）。

输入：据点拼合图 composite.png + logistics.json（物流件、准入口，拼合图坐标）+ 底账设备。
输出：base/<据点>/flow.json（每个物流件每条边接到哪、流向）+ flow.jpg（标注图）。

方向（物流件自己）：
- 分流器 / 管道分流器：深色条贴着的那一边是进料口，另外三边是出口。
- 汇流器 / 管道汇流器：没有深色条的那一边是出口，另外三边是进料口。
- 用模板 × 4 个旋转匹配，最像的那个旋转直接给出进/出口；再按格内四边明暗复核，不一致的（上面压着管道等）标「被压住」。
- 桥：两条直线各自穿过，方向看两头。准入口：一进一出在同一条直线上，方向看两头。

连接（沿传送带 / 管道一格一格走）：
- 拼图格距有累计误差（不同蓝图之间差 6–12 px），不能套全局网格：从物流件中心出发，每走一格按带子/管子实际位置重新对中；
  拐弯时格中心沿行进方向前后挪 ±9 px 找弯道。
- 每格格边的判断：传送带 = 黄色、横截宽 16–28 px；管道 = 深灰、横截 5–12 px（管子上的浅色小三角造成的 ≤2 px 缺口补上）。
- 挨着的格子就是物流件：直接相连（物流件之间常常不隔带子）；挨着的是另一层的物流件（传送带桥的深色拱等）→ 那边不算管道。
- 桥：同一层直穿过去。管道被一格圆图标（仓库口等）压住：隔一格还是同一条线就跳过去（记 jumps）。
- 走到头不是物流件 → 接的是设备：按最后一格里带子/管子往哪边伸判断设备在哪边，再找那边的设备（底账标签 + 设备外框）。

流向（一条连接的方向）：
1. 两头有分流器/汇流器的，按它们的进/出口定（「出口 → 进料口」）；两头说法冲突的记问题。
2. 再看带子/管子上的小三角（每格一个）投票，和 1 对照；没有物流件定方向的（准入口 ↔ 设备）就按小三角。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import cv2
import numpy as np

from . import atomic
from . import centerline as CL
from . import logistics as LG
from . import vision as V

C = V.CELL
DIRS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
LEFT = {"E": "N", "N": "W", "W": "S", "S": "E"}
RIGHT = {v: k for k, v in LEFT.items()}
BACK = {"E": "W", "W": "E", "N": "S", "S": "N"}
BRIDGES = ("物流桥", "管道桥")
SPLIT = ("分流器", "管道分流器")
USE_CENTERLINE = True  # 2026-11-09 第二版：管子沿中心线追踪（centerline.walk）；传送带和 False 时 = 按格子走的旧 trace
USE_BELT_UNDER = True  # 2026-11-09 断头修正的开关（对比测试用）
USE_FAR = True
MERGE = ("汇流器", "管道汇流器")


def _v(d):
    return np.array(DIRS[d], float)


# ------------------------------------------------------------------ 掩码与格边判断
def masks(img: np.ndarray) -> dict:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    yel = (hsv[..., 0] >= 15) & (hsv[..., 0] <= 40) & (hsv[..., 1] > 60) & (hsv[..., 2] > 180)
    drk = (g < 105) & (hsv[..., 1] < 50)
    # 半透明浅灰叠层底下的管子颜色变浅（g 120–150），绝对阈值认不出：和 25 px 内最亮的底色比，暗 ≥ 55 也算（只在 probe 认不出时兜底用）
    bg = cv2.dilate(g, np.ones((25, 25), np.uint8))
    soft = drk | ((bg.astype(int) - g >= 55) & (hsv[..., 1] < 50) & (g < 165))
    return {"belt": yel, "pipe": drk, "pipe_soft": soft, "gray": g, "yel": yel}


def icon_disks(img: np.ndarray, nodes=()) -> tuple[np.ndarray, list]:
    """仓库取货/存货口等一格设备的圆形图标（物流件自己的图标不算）→ 掩码。
    圆盘里深色的图案在横截线上会切出一段 5–12 px 的深色，和管子一模一样：
    2026-11-09 应龙关/武陵城 9 个「分叉」里 7 个是管子旁边紧挨着这种圆，被当成往那边拐。"""
    # 不用 HoughCircles：红色矿石图标中间不暗、边缘梯度弱，霍夫圆要么漏、要么偏 20 px（2026-11-09 武陵城）。
    # 改成「不是浅灰底色」的区域里能放下半径 ≥12.5 px 的圆的地方：管子 ≤12 px 宽、传送带 ≤24 px 宽都放不下。
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    fg = cv2.morphologyEx(((g < 150) | (hsv[..., 1] > 70)).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    dt = cv2.distanceTransform(fg, cv2.DIST_L2, 5)
    peak = (dt == cv2.dilate(dt, np.ones((15, 15), np.uint8))) & (dt >= 12.5) & (dt <= 21)
    m = np.zeros(g.shape, bool)
    disks = []
    yy, xx = np.mgrid[0:g.shape[0], 0:g.shape[1]]
    ys, xs = np.nonzero(peak)
    for x, y in sorted(zip(xs, ys), key=lambda q: -dt[q[1], q[0]]):
        if any(abs(x - a) <= 12 and abs(y - b) <= 12 for a, b, _ in disks):
            continue
        if any(abs(x - n["c"][0]) <= 14 and abs(y - n["c"][1]) <= 14 for n in nodes):
            continue
        r = float(dt[y, x])
        R = int(r) + 2
        win = (slice(max(0, y - R), y + R + 1), slice(max(0, x - R), x + R + 1))
        m[win] |= (xx[win] - x) ** 2 + (yy[win] - y) ** 2 <= R ** 2
        disks.append((float(x), float(y), r))
    return m, disks


def _disk_at(M, Q, tol=12):
    for x, y, r in M.get("disks", ()):
        if abs(x - Q[0]) <= tol and abs(y - Q[1]) <= tol:
            return x, y, r
    return None


def _icon_exit(M, disk, d):
    """管子从 d 方向钻进圆图标底下：在图标外圈一圈找管子出来的地方（除了进来那边）。只有一处 → 出去的方向，否则 None。
    2026-11-09 武陵城 (1325,571)、应龙关 (1287,1564)：管子在仓库口图标底下拐弯，原来只会处理直穿（_jump_ok）。"""
    cx, cy, r = disk
    m = M["pipe"]
    H, W = m.shape
    rr = r + 5
    angs = np.deg2rad(np.arange(0, 360, 3))
    vals = []
    for a in angs:
        x, y = int(round(cx + rr * np.cos(a))), int(round(cy + rr * np.sin(a)))
        vals.append(bool(0 <= x < W and 0 <= y < H and m[y, x]))
    vals = np.array(vals)
    if vals.all() or not vals.any():
        return None
    k0 = int(np.argmin(vals))  # 从一个空位开始数，避免一段跨过 0°
    v = np.roll(vals, -k0)
    runs, s = [], None
    for i, b in enumerate(list(v) + [False]):
        if b and s is None:
            s = i
        elif not b and s is not None:
            runs.append((s, i - 1)); s = None
    back = np.arctan2(-DIRS[d][1], -DIRS[d][0])
    cands = []
    for a, b in runs:
        arc = (b - a + 1) * np.deg2rad(3) * rr
        mid = angs[(int((a + b) / 2) + k0) % len(angs)]
        if not 4 <= arc <= 16:
            continue
        if abs((mid - back + np.pi) % (2 * np.pi) - np.pi) < np.deg2rad(60):
            continue
        cands.append(mid)
    if len(cands) != 1:
        return None
    a = cands[0]
    return min(DIRS, key=lambda e: abs((np.arctan2(DIRS[e][1], DIRS[e][0]) - a + np.pi) % (2 * np.pi) - np.pi))


def _on_icon(M, P, e) -> bool:
    """P 这一格往 e 过边 2–8 px 的地方（probe 看的那一截）落在圆图标里。"""
    m = M["icon"]
    H, W = m.shape
    B = np.asarray(P, float) + _v(e) * C / 2
    hits = 0
    for u in (2, 5, 8):
        x, y = (B + _v(e) * u).round().astype(int)
        hits += bool(0 <= x < W and 0 <= y < H and m[y, x])
    return hits >= 2


def _profile(m, B, d, uo, half=16):
    dx, dy = DIRS[d]
    vx, vy = -dy, dx
    H, W = m.shape
    out = np.zeros(2 * half + 1, bool)
    for i, v in enumerate(range(-half, half + 1)):
        x, y = int(round(B[0] + dx * uo + vx * v)), int(round(B[1] + dy * uo + vy * v))
        if 0 <= x < W and 0 <= y < H:
            out[i] = m[y, x]
    return out


def _runs(p, gap=0):
    p = p.copy()
    if gap:
        idx = np.nonzero(p)[0]
        for a, b in zip(idx[:-1], idx[1:]):
            if 1 < b - a <= gap + 1:
                p[a:b] = True
    r, s = [], None
    for i, b in enumerate(list(p) + [False]):
        if b and s is None:
            s = i
        elif not b and s is not None:
            r.append((s, i - 1)); s = None
    return r


def _cross(M, layer, B, d, uo):
    """B 处（格边附近）垂直于 d 的横截线上，该层的带子/管子中心相对 B 的横向偏移；没有 → None。"""
    p = _profile(M[layer], B, d, uo)
    if layer == "belt":
        idx = np.nonzero(p)[0]
        if len(idx) >= 12 and 16 <= idx.max() - idx.min() <= 28:
            return (idx.max() + idx.min()) / 2 - 16
        if USE_BELT_UNDER and len(idx) >= 4 and 16 <= idx.max() - idx.min() <= 28:
            # 传送带上面压着一条管子，只露出两条黄边：中间那段是管子的深色（2026-11-09 武陵城 (889,1259)）
            mid = _profile(M["pipe"], B, d, uo)[idx.min():idx.max() + 1]
            if mid.sum() >= 5 and p[idx.min():idx.max() + 1].sum() + mid.sum() >= 0.65 * (idx.max() - idx.min() + 1):
                return (idx.max() + idx.min()) / 2 - 16
        return None
    best = None
    for a, b in _runs(p, gap=2):
        c = (a + b) / 2 - 16
        if 5 <= b - a + 1 <= 12 and abs(c) <= 10 and (best is None or abs(c) < abs(best)):
            best = c
    return best


def probe(M, layer, P, d, far=False):
    """从格中心 P 往 d 那条格边：该层有没有连过去（只看过边后 2、5 px 两条横截线，再往里拐弯格就弯走了）。"""
    B = np.asarray(P, float) + _v(d) * C / 2
    pairs = ((2, 5), (5, 8), (8, 11), (11, 14)) if far else ((2, 5), (5, 8))
    for u1, u2 in pairs:  # 格中心估得偏几 px 时，过边 2 px 那条可能正好压在物流件外框线上；设备外框竖线横穿管子时再往外看（2026-11-09 武陵城 (1741,406)）
        a, b = _cross(M, layer, B, d, u1), _cross(M, layer, B, d, u2)
        if a is not None and b is not None and abs(a - b) <= 4:
            return float(a)
    if far and layer == "pipe" and "pipe_soft" in M:  # 浅灰叠层底下变浅的管子
        S = dict(M, pipe=M["pipe_soft"])
        for u1, u2 in ((2, 5), (5, 8)):
            a, b = _cross(S, "pipe", B, d, u1), _cross(S, "pipe", B, d, u2)
            if a is not None and b is not None and abs(a - b) <= 4:
                return float(a)
    return None


def _jump_ok(M, P, d):
    B2 = np.asarray(P, float) + _v(d) * (1.5 * C)
    a, b = _cross(M, "pipe", B2, d, 2), _cross(M, "pipe", B2, d, 5)
    return a is not None and b is not None and abs(a) <= 4 and abs(a - b) <= 3


# ------------------------------------------------------------------ 节点（物流件）
def layer_of(n) -> list[str]:
    t = n["type"]
    if t == LG.GATE:
        k = n.get("kind")
        return ["pipe"] if k == "管道" else ["belt"] if k == "物品" else ["belt", "pipe"]
    return ["pipe"] if t.startswith("管道") else ["belt"]


def fits(n, layer) -> bool:
    return layer in layer_of(n)


def node_at(nodes, Q, d=None, tol=13):
    """Q 附近的物流件；给了行进方向 d 时沿方向放宽到 20 px（格距累计误差），横向 10 px。"""
    for n in nodes:
        ex, ey = abs(n["c"][0] - Q[0]), abs(n["c"][1] - Q[1])
        if d is not None:
            along, across = (ex, ey) if d in ("E", "W") else (ey, ex)
            if along <= 20 and across <= 10:
                return n
        elif ex <= tol and ey <= tol:
            return n
    return None


def _adj(nodes, P, e, layer, me=None):
    n = node_at(nodes, np.asarray(P) + _v(e) * C, d=e)
    return n if n is not None and n is not me and fits(n, layer) else None


def _inner(M, layer, P, e) -> bool:
    """这一格里带子/管子确实伸向 e 那一边（格边内侧 4、7 px 处有）。管子直着从物流件旁边经过、或在格里拐走时没有。"""
    B = np.asarray(P, float) + _v(e) * C / 2
    for uo in (-4, -7):
        c = _cross(M, layer, B, e, uo)
        if c is None or abs(c) > 8:
            return False
    return True


def _adj_ok(M, nodes, P, e, layer):
    """隔壁格是同层物流件，而且这一格里的带子/管子确实伸过去（沿方向按物流件位置对齐再看：格距误差会让 P 离它只有 24 px）。"""
    n = _adj(nodes, P, e, layer)
    if n is None:
        return False
    Q = np.asarray(P, float).copy()
    ax = 0 if e in ("E", "W") else 1
    Q[ax] = n["c"][ax] - DIRS[e][ax] * C
    return _inner(M, layer, Q, e)


def _blocked(nodes, P, e, layer, me=None):
    n = node_at(nodes, np.asarray(P) + _v(e) * C, d=e)
    return n is not None and n is not me and not fits(n, layer)


_tinfo = None


def _side_gray(g, cx, cy, h=17):
    out = {}
    H, W = g.shape
    for k, (dx, dy) in DIRS.items():
        vals = [g[min(H - 1, max(0, cy + dy * t + dx * o)), min(W - 1, max(0, cx + dx * t - dy * o))]
                for t in range(h - 6, h - 2) for o in range(-4, 5)]
        out[k] = float(np.mean(vals))
    return out


def _key_side(t, d):
    if t in SPLIT:
        return min(d, key=d.get)  # 进料口：深色条贴边
    if t in MERGE:
        return max(d, key=d.get)  # 出口：没有深色条的那边
    return None


def orient(img, n) -> dict:
    """分流器 → {in: 边, outs: [3 边]}；汇流器 → {out: 边, ins: [3 边]}；另带 c（对准后的中心）、score、covered（模板和明暗不一致）。"""
    global _tinfo
    lg, _, _ = LG._templates()
    if _tinfo is None:
        _tinfo = {k: [_side_gray(cv2.cvtColor(t, cv2.COLOR_BGR2GRAY), 17, 17) for t in ts] for k, ts in lg.items()}
    k = n["type"]
    x, y = n["p"]
    r = 5
    win = img[max(0, y - 17 - r):y + 18 + r, max(0, x - 17 - r):x + 18 + r]
    best = (-2.0, None, (r, r))
    for t, d in zip(lg.get(k, []), _tinfo.get(k, [])):
        if win.shape[0] < 35 or win.shape[1] < 35:
            continue
        _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED))
        if mx > best[0]:
            best = (mx, d, loc)
    sc, d, loc = best
    cx, cy = max(0, x - 17 - r) + loc[0] + 17, max(0, y - 17 - r) + loc[1] + 17
    out = {"c": [int(cx), int(cy)], "score": round(float(sc), 3)}
    if d is None or k not in SPLIT + MERGE:
        return out
    side = _key_side(k, d)
    px = _key_side(k, _side_gray(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cx, cy))
    others = [s for s in DIRS if s != side]
    if k in SPLIT:
        out.update({"in": side, "outs": others})
    else:
        out.update({"out": side, "ins": others})
    out["covered"] = px != side
    return out


# ------------------------------------------------------------------ 沿线追踪
def _under_exit(M, nodes, n, d, layer):
    """带子走进一格只在管道层的物流件（管道准入口、管道分流器…：管子架在带子上面，带子从底下穿过）：
    这格除了来的那边，只有一边露出带子 → 从那边出去。2026-11-10 景玉谷 (1636,30)、应龙关 (28,952) 的清水管道准入口底下，
    种植机 → 精炼炉 的带子拐了个弯，原来在这里停下、被按最近标签认成了暗管入口。"""
    if layer != "belt" or not USE_PORTS or fits(n, layer) or n["type"] in BRIDGES:
        return None
    Q = np.array(n["c"], float)
    ex = [e for e in (d, LEFT[d], RIGHT[d])
          if not _blocked(nodes, Q, e, layer) and probe(M, layer, Q, e) is not None]
    return ex[0] if len(ex) == 1 else None


def _under_hop(M, nodes, start, P, d, layer, path, first=False):
    """带子要在这里停下、正前方一格是只在管道层的物流件：看它底下穿过去从哪边出来。→ (那格中心, 出去的方向) 或 None。
    只在要停下时才看（带子在这格拐走时不会走到这里）；前面那格的格边常被盖在管子底下，看不到黄带，所以不要求。
    first：第一步（还没走过一格）也看，只有 _build 按 _under_first 核过的物流件边才给（见那里的说明）；原来第一步一律不看。"""
    if layer != "belt" or (len(path) < 2 and not first):
        return None
    n2 = node_at(nodes, np.asarray(P) + _v(d) * C, d=d)
    if n2 is None or n2 is start:
        return None
    e = _under_exit(M, nodes, n2, d, layer)
    return None if e is None else (np.array(n2["c"], float), e)


def _under_first(M, nodes, n, P0, d, layer) -> bool:
    """物流件 n 的 d 边正前方紧贴着一格只在管道层的物流件，带子从它底下穿过去：这条边不整边跳过，追线第一步就从底下穿。
    2026-11-21 issue #1 第五轮（续）：别人的武陵城，息壤物品准入口 (拼合图 2418,1600) 北边正前方一格就是壤晶废液管道准入口
    (2420,1565)，带子从管道准入口底下穿过去，进扩容反应池（底账 (1729,1156)）南边的进料口。原来 _build 一见 _blocked 就整边跳过，
    _under_hop 又要求至少走过一格，这条带子追不出来，扩容反应池缺息壤、没料。
    条件要严，四条都要：这一层是传送带、n 自己在传送带层；挡着的那格只在管道层（管道准入口、管道分流器…，不是两层都占的准入口）；
    不是管道桥；从它底下穿过去只有一边露出带子（_under_exit 找得到唯一出口）。"""
    if layer != "belt" or not fits(n, layer):
        return False
    n2 = node_at(nodes, np.asarray(P0, float) + _v(d) * C, d=d)
    if n2 is None or n2 is n or layer_of(n2) != ["pipe"] or n2["type"] in BRIDGES:
        return False
    return _under_exit(M, nodes, n2, d, layer) is not None


def trace(M, nodes, start, d, layer, maxn=400, hop0=False) -> dict:
    """从物流件 start 的 d 边出发沿 layer 走到头。→ {end: node|device|fork|long, node, side, path, d, jumps}
    hop0：start 这条边正前方紧贴着只在管道层的物流件、带子从底下穿过（_build 用 _under_first 核过），第一步就穿过去。"""
    P = np.array(start["c"], float)
    path, jumps = [P.copy()], []
    for _ in range(maxn):
        n = _adj(nodes, P, d, layer, me=start)
        if n is not None:  # 下一格就是物流件
            Q = np.array(n["c"], float)
            path.append(Q)
            if n["type"] in BRIDGES:
                P = Q
                continue
            return {"end": "node", "node": n, "side": BACK[d], "path": path, "d": d, "jumps": jumps}
        off = None
        if not _blocked(nodes, P, d, layer, me=start):
            for sft in (0, -3, -6):
                off = probe(M, layer, P + _v(d) * sft, d)
                if off is not None:
                    break
            if off is None and USE_FAR and len(path) > 1:
                # 直行要停下之前再看一眼：设备外框竖线横穿管子、管子在浅灰叠层底下变浅，靠边那两条横截线会认不出
                # （2026-11-09 武陵城 (1741,406)、(1358,536)：原来在这里停下，又因为附近有仓库口被当成「接设备」，不报）
                off = probe(M, layer, P, d, far=True)
        if off is None and layer == "pipe" and len(path) > 1 and _jump_ok(M, P, d):
            P = P + _v(d) * C
            path.append(P.copy()); jumps.append([int(P[0]), int(P[1])])
            continue
        if off is None:
            hop = _under_hop(M, nodes, start, P, d, layer, path, first=hop0)
            if hop is not None:
                P, d = hop
                path.append(P.copy()); jumps.append([int(P[0]), int(P[1])])
                continue
            return {"end": "device", "path": path, "d": d, "jumps": jumps,
                    "straight": bool(node_at(nodes, P, tol=6) is not None and node_at(nodes, P, tol=6)["type"] in BRIDGES)}
        dx, dy = DIRS[d]
        P = P + _v(d) * C + np.array([-dy, dx]) * off
        path.append(P.copy())
        n = node_at(nodes, P, d=d)
        if n is not None and n is not start:
            if not fits(n, layer):
                e = _under_exit(M, nodes, n, d, layer)
                if e is not None:
                    P = np.array(n["c"], float); path[-1] = P.copy(); jumps.append([int(P[0]), int(P[1])])
                    d = e
                    continue
                return {"end": "device", "path": path[:-1], "d": d, "jumps": jumps, "note": f"撞上另一层的{n['type']}"}
            P = np.array(n["c"], float); path[-1] = P.copy()
            if n["type"] in BRIDGES:
                continue
            return {"end": "node", "node": n, "side": BACK[d], "path": path, "d": d, "jumps": jumps}
        disk = _disk_at(M, P) if layer == "pipe" else None
        if disk is not None:  # 走进了一格设备的圆图标底下：看管子从哪边出来
            e = _icon_exit(M, disk, d)
            if e is not None and e != BACK[d]:
                P = np.array(disk[:2], float); path[-1] = P.copy()
                jumps.append([int(P[0]), int(P[1])])
                d = e
                continue
        ex = []

        def goes(Q, e):
            """Q 这一格往 e 连过去：隔壁是同层物流件时只认 _adj_ok（格距偏了时 probe 会扎进物流件图标里，
            把箭头的深色当成管子；2026-11-09 武陵城 (1288,678) 弯管左边隔一空格的汇流器）。"""
            if _adj(nodes, Q, e, layer) is not None:
                return _adj_ok(M, nodes, Q, e, layer)
            return not _blocked(nodes, Q, e, layer) and probe(M, layer, Q, e) is not None

        strong = goes(P, d)
        weak = not strong and _adj(nodes, P, d, layer) is None and not _blocked(nodes, P, d, layer) and \
            any(probe(M, layer, P + _v(d) * s, d) is not None for s in (-3, -6))
        if strong or weak:
            ex.append((d, 0))
        for e in (LEFT[d], RIGHT[d]):  # 拐弯：格中心沿行进方向可能偏几 px，前后挪着找
            if _blocked(nodes, P, e, layer):
                continue
            for s in (0, -3, 3, -6, 6, -9, 9, -12):
                if goes(P + _v(d) * s, e):
                    ex.append((e, s)); break
        if len(ex) > 1 and weak:  # 拐角格里靠近看到的「直行」其实是弯道本身
            ex = ex[1:]
        if len(ex) > 1 and "icon" in M:  # 两边都像能走：过格边那一截压在旁边一格设备的圆图标上的，是图标里的深色图案
            keep = [(e, s) for e, s in ex if not _on_icon(M, P + _v(d) * s, e)]
            if keep:
                ex = keep
        if not ex and USE_FAR and not _blocked(nodes, P, d, layer) and _adj(nodes, P, d, layer) is None                 and probe(M, layer, P, d, far=True) is not None:
            ex = [(d, 0)]  # 要停下之前再往外多看几条横截线（见 trace 开头 USE_FAR 那段）
        if not ex and layer == "pipe" and _jump_ok(M, P, d):
            P = P + _v(d) * C
            path.append(P.copy()); jumps.append([int(P[0]), int(P[1])])
            continue
        if not ex:
            hop = _under_hop(M, nodes, start, P, d, layer, path)
            if hop is not None:
                P, d = hop
                path.append(P.copy()); jumps.append([int(P[0]), int(P[1])])
                continue
            return {"end": "device", "path": path, "d": d, "jumps": jumps}
        if len(ex) > 1:
            return {"end": "fork", "path": path, "d": d, "jumps": jumps, "ex": [e for e, _ in ex], "ex_s": ex, "strong": bool(strong)}
        e, s = ex[0]
        if s:
            P = P + _v(d) * s
            path[-1] = P.copy()
        d = e
    return {"end": "long", "path": path, "d": d, "jumps": jumps}


def _bounded_run(m, B, d, uo, c, half=28, maxlen=30) -> bool:
    """横截线放宽到 ±28 px 看：过中心的那段带子两头都得收住（一条带宽）。
    圆角拐弯格里，没伸出去的那边横截线会切到正在拐的弯边：那是顺着走的一整条，±16 px 的窗口里看着也像一条带宽
    （2026-11-09 武陵城 (1258,754)：带子往西拐南进塑形机，北边切到弧顶，被判成往北进了上面的精炼炉）。"""
    p = _profile(m, B, d, uo, half=half)
    i0 = int(round(half + c))
    if not (0 <= i0 < len(p)) or not p[i0]:
        return True  # 中心正好落在小三角的空洞里：不否决
    for a, b in _runs(p, gap=3):
        if a <= i0 <= b:
            return b - a + 1 <= maxlen and a > 0 and b < len(p) - 1
    return True


def stub_side(M, layer, P, d) -> str:
    """走到头的那一格里，带子/管子往哪边伸（直走 / 左 / 右）：设备就在那边。
    看格内离中心 8–14 px 的几条横截线有没有「带子/管子形状」（宽度对、居中），不数深色像素（旁边仓库口的竖框也是深色，2026-11-09 景玉谷）。"""
    best, bs = d, 0
    for sft in (0, -4, -8, -12):  # 格中心沿行进方向可能估过头了（拐弯格实际在后面几 px）
        Ps = np.asarray(P, float) + _v(d) * sft
        for e in (d, LEFT[d], RIGHT[d]):
            if e == d and sft:
                continue
            B = Ps + _v(e) * C / 2
            hits = sum(1 for uo in (-9, -7, -5, -3) if (c := _cross(M, layer, B, e, uo)) is not None and abs(c) <= 7
                       and (layer != "belt" or _bounded_run(M[layer], B, e, uo, c)))
            if hits > bs:
                best, bs = e, hits
        if bs >= 3:
            break
    return best


# ------------------------------------------------------------------ 小三角（流向）
def _pipe_along(M, a, u, L) -> np.ndarray:
    """传送带这一段上哪些点压着管子（顺着走或横穿）：点周围 9×9 里管子像素 ≥25 个。
    管子上的白三角边缘（灰）会被当成带子的三角，方向是管子的流向，不算带子的票
    （2026-11-09 武陵城 (885,234) 汇流器西边那条带：横穿的竖管上的 ▲ 投了 2 张反票，把出口改反成 E）。
    带子自己的深灰小三角在管子掩码里只有几个像素的芯（≤10 个），不会被排除。"""
    m = M["pipe"]
    H, W = m.shape
    n = int(L) + 1
    out = np.zeros(n, bool)
    for t in range(n):
        x, y = int(round(a[0] + u[0] * t)), int(round(a[1] + u[1] * t))
        if 4 <= x < W - 4 and 4 <= y < H - 4:
            out[t] = int(m[y - 4:y + 5, x - 4:x + 5].sum()) >= 25
    return out


def arrow_votes(M, layer, path, nodes=()) -> int:
    """沿路径每段直线上找带子/管子上的小三角：三角形底宽、尖窄，质心靠底。→ 顺着路径的票数 − 逆着的票数。"""
    g = M["gray"]
    H, W = g.shape
    vote = 0
    half = 3 if layer == "pipe" else 5
    for a, b in zip(path[:-1], path[1:]):
        a, b = np.asarray(a, float), np.asarray(b, float)
        dv = b - a
        L = float(np.hypot(*dv))
        if L < 20 or min(abs(dv[0]), abs(dv[1])) > 6:
            continue
        u = dv / L
        vx, vy = -u[1], u[0]
        under = _pipe_along(M, a, u, L) if layer == "belt" else None
        us = [t for t in range(int(L) + 1)
              if not any(np.hypot(*(a + u * t - np.asarray(n["c"]))) < 14 for n in nodes)
              and not (under is not None and under[t])]
        if len(us) < 8:
            continue
        arr = np.zeros((len(us), 2 * half + 1), bool)
        for i, t in enumerate(us):
            for j, v in enumerate(range(-half, half + 1)):
                x, y = int(round(a[0] + u[0] * t + vx * v)), int(round(a[1] + u[1] * t + vy * v))
                if 0 <= x < W and 0 <= y < H:
                    arr[i, j] = (g[y, x] > 125) if layer == "pipe" else (g[y, x] < 175 and not M["yel"][y, x] and g[y, x] > 80)
        n, lab, st, _ = cv2.connectedComponentsWithStats(arr.astype(np.uint8), connectivity=8)
        for k in range(1, n):
            h, w, area = st[k, cv2.CC_STAT_HEIGHT], st[k, cv2.CC_STAT_WIDTH], st[k, cv2.CC_STAT_AREA]
            if not (3 <= h <= 9 and 3 <= w <= 2 * half + 1 and 5 <= area <= 45):
                continue
            rows = np.nonzero((lab == k).any(1))[0]
            widths = (lab[rows] == k).sum(1).astype(float)
            m = len(widths) // 2
            first, last = widths[:m].mean(), widths[-m:].mean()
            if abs(first - last) < 0.8:
                continue
            vote += 1 if first > last else -1  # 底宽在前 → 尖朝路径方向
    return vote


# ------------------------------------------------------------------ 整个据点
# _DARK_THIN、_DEVS、PORT_CMP 是 build 一次运行里的状态，放在模块变量里，下面一串函数（_touching_device、_end_info、_port_hit、
# _port_bridges…）直接读。两个线程同时 build（拼合任务和别的重算撞上）会互相盖掉（2026-11-19 审计第 10 项）。
# 没有改成参数往下传：要改十几个函数的签名，tests/test_port_links.py、test_trace_fixes.py 也是直接给 FL._DEVS 赋值来测单个函数的。
# 改成用 _BUILD_LOCK 把 build / run_base 整个串行化：物流连接本来就是重计算（一个据点几十秒），排队没有损失。
# 加锁顺序见 design._RUN_LOCK 的说明：这把锁在最里层，持锁期间不调 design.run_base
_BUILD_LOCK = threading.RLock()
_DARK_THIN = None
_DEVS = []  # 每台底账设备的端口布局（ports.all_ports），build 里算
USE_PORTS = True  # 2026-11-10 端口布局（游戏数据 + 口格折角定朝向）认带子/管子头接的是哪台设备的哪个口
PORT_CMP = []  # 调试：端口布局和原来的认法说法不一样的地方


def _touching_device(P, d, ledger_pts, rects, tol=(-4, 9)):
    """物流件 d 边外面紧贴着一台设备的外框（外框边离物流件格边 −4…9 px，且横向盖住物流件中心）→ 那台设备。
    2026-11-09 景玉谷：给种植机分水的管道分流器直接贴在种植机的进水口上，中间一格管子都没有。"""
    P = np.asarray(P, float)
    edge = P + _v(d) * C / 2
    cands = sorted(rects, key=lambda r: r[2] * r[3])
    if _DARK_THIN is not None:  # 只有上下两条深色边的设备（种植机等）不在闭合外框列表里：按线找
        from .pipenet import device_rect
        T = edge + _v(d) * 14
        r = device_rect(_DARK_THIN, T[0], T[1], [])
        if r is not None and r[2] <= 9 * C and r[3] <= 9 * C:
            cands = cands + [r]
    for x, y, w, h in cands:
        if d == "E":
            gap, ok = x - edge[0], y + 6 <= P[1] <= y + h - 6
        elif d == "W":
            gap, ok = edge[0] - (x + w), y + 6 <= P[1] <= y + h - 6
        elif d == "S":
            gap, ok = y - edge[1], x + 6 <= P[0] <= x + w - 6
        else:
            gap, ok = edge[1] - (y + h), x + 6 <= P[0] <= x + w - 6
        if ok and tol[0] <= gap <= tol[1]:
            dv = _device_at(edge + _v(d) * 14, ledger_pts, [(x, y, w, h)])
            if dv is not None:
                return dv
    return None


TUNNELS = ("暗管入口", "暗管出口", "多口暗管入口", "多口暗管出口")


def _touching_tunnel(P, d, ledger_pts, tol_along=0.55, tol_lat=0.55):
    """管道物流件 d 边外面没有管子、_touching_device 也没认出贴着的设备时：正前方是不是一台暗管的口。
    暗管只有一圈浅蓝细框，_rects 和端口布局（ports.all_ports）都认不出它的外框；物流件直接贴在暗管口上、
    中间一格管子都没有时就接不上。2026-11-20 issue #1 第五轮：别人的武陵城左列清水准入口后面的汇流器贴着两个暗管入口，
    反馈包第一次带上暗管配对后，对面的暗管出口出不来水，种植机、精炼炉、天有洪炉 9 台没料（没配对时暗管出口按缺口估，盖住了）。
    按游戏数据（building_ports.json）里这台暗管的尺寸和口的位置，四个朝向都试：有一个口朝着物流件（口在 d 的反方向那条边上），
    口的外缘离物流件这条边沿走向 ≤ tol_along 格、横向 ≤ tol_lat 格 → 那台。底账标签当设备中心：量过五个样本 70 台暗管，
    标签离浅蓝框中心多口暗管最多约 0.46 格、单口约 0.33 格，所以容差取 0.55（离隔壁一格还远；从 0.4/0.45 放到 0.55 只多接上一条
    真连接：应龙关多口暗管出口 (627,544) → 汇流器 (711,577)）。
    只认唯一一台。→ {name, c, how, port}（port：这个口是 in 还是 out，调用方拿它和物流件这条边的进出核对）或 None。
    口在哪条边是猜的（四个朝向都试）：连线依据写「贴着暗管口…推断」，设计分析按推断连线标〔推断连线〕。"""
    from . import ports as PT
    lays = PT.layouts()
    P = np.asarray(P, float)
    E = P + _v(d) * C / 2
    hits = {}
    for k, p in enumerate(ledger_pts):
        L = lays.get(p["name"]) if p["name"] in TUNNELS else None
        if not L:
            continue
        for rot in (0, 90, 180, 270):
            for q in L["ports"]:
                if not q["pipe"] or PT._rot_side(q["side"], rot) != BACK[d]:
                    continue
                x, y, w, h = PT._rot_cell(q["x"], q["y"], L["w"], L["d"], rot)
                edge = np.array(p["c"], float) + (np.array([x + 0.5 - w / 2, y + 0.5 - h / 2]) + _v(BACK[d]) * 0.5) * C
                dx, dy = edge - E
                along, lat = dx * _v(d)[0] + dy * _v(d)[1], dx * _v(d)[1] - dy * _v(d)[0]
                if abs(along) <= tol_along * C and abs(lat) <= tol_lat * C:
                    hits.setdefault(k, q["role"])
    if len(hits) != 1:
        return None
    (k, role), = hits.items()
    p = ledger_pts[k]
    return {"name": p["name"], "c": p["c"], "how": TUNNEL_HOW, "port": role}


TUNNEL_HOW = "贴着暗管口（游戏数据尺寸）"


def _drop_ambiguous_tunnel_touch(links, nodes, ledger_pts):
    """_touching_tunnel 四个朝向都试，3×3 的暗管四条边正中都对得上，认不出口到底在哪条边（拼合图上口的小凸台用模板、深色像素都
    判不稳，2026-11-20 拿 68 台已接线的暗管试过，最好 56/68）。暗管的口都在同一条边上：同一台暗管还有别的边像是口时，贴着认的这条不接。
    别的边像是口 = 有别的管道物流件（含准入口）正贴着那条边的口位置，或者已经有连线（追出来的管子、设备直连的管子、
    按最近标签挂上的）从那条边接进来。所以要等设备直连的管子追完再调。
    皇帝武陵城 (965,714) 那台暗管入口：口在西边、直接贴着准入口 55，底边正中贴着一个分流器，不加这一步会把分流器接上。
    顺带：同一个物流件已经从另一条边连着这台暗管时（应龙关汇流器 (93,1601) 东边按最近标签挂着多口暗管出口 (112,1673)），
    贴着认的这条也丢掉，不会同一来源接两条、算两份（design.Graph 不去重）。代价是那条按最近标签认错了边的连线留着。"""
    touch = {id(r) for r in links if r.get("touch") and (r.get("device") or {}).get("how") == TUNNEL_HOW}
    if not touch:
        return links

    def key(dv):
        return dv["name"], round(dv["c"][0]), round(dv["c"][1])
    sides = {}  # 暗管 → 接进来的走向（物流件 / 管子头朝暗管走的方向）
    for r in links:
        if r["layer"] != "pipe":
            continue
        for dv, s in ((r.get("device"), r.get("dev_side")), (r.get("a_dev"), r.get("a_dev_side"))):
            if dv and dv.get("name") in TUNNELS and s:
                sides.setdefault(key(dv), set()).add(s)
    tun = [p for p in ledger_pts if p["name"] in TUNNELS]
    for n in nodes:
        if "pipe" not in layer_of(n):
            continue
        for p in tun:
            if abs(p["c"][0] - n["c"][0]) > 3 * C or abs(p["c"][1] - n["c"][1]) > 3 * C:
                continue
            for d in DIRS:
                if _touching_tunnel(n["c"], d, [p]) is not None:
                    sides.setdefault(key(p), set()).add(d)
    bad = {k for k, s in sides.items() if len(s) > 1}
    return [r for r in links if not (id(r) in touch and key(r["device"]) in bad)]


OUTLETS = ("暗管出口", "多口暗管出口", "仓库取货口")  # 只往外出：物流件进料口贴着它就是接上了（应龙关多口暗管出口 M2 → 汇流器 H2 → G5，2026-11-09）
NOT_PORT = ("息壤供电桩", "供电桩", "中继器", "协议核心", "仓库存取线基段", "仓库存取线源桩", "长距滑索架")  # 不接带子/管子的设备，不当端点
# 长距滑索架：2026-11-14 别人的武陵城 (1736,370)，精炼炉进料带的另一头本来是取货口 (1768,351)，被按最近标签认成了滑索架


def _device_at(T, ledger_pts, rects):
    """T（端口里面一点）落在哪台设备：包住 T 的外框里的底账标签；没有外框就取 2.5 格内最近的标签。"""
    for x, y, w, h in sorted(rects, key=lambda r: r[2] * r[3]):
        if x - 3 <= T[0] <= x + w + 3 and y - 3 <= T[1] <= y + h + 3:
            inside = [p for p in ledger_pts if x <= p["c"][0] <= x + w and y <= p["c"][1] <= y + h]
            if inside:
                p = min(inside, key=lambda p: np.hypot(p["c"][0] - T[0], p["c"][1] - T[1]))
                if p["name"] in ("仓库取货口", "仓库存货口", "仓库口") and max(w, h) > 2 * C:
                    continue  # 1 格大的仓库口不靠大外框认（武陵城 (2222,580) 的带子被一个 127×112 的框认成了隔壁取货口，2026-11-10）
                return {"name": p["name"], "c": p["c"], "rect": [x, y, w, h], "how": "外框"}
    if ledger_pts:
        p = min(ledger_pts, key=lambda p: np.hypot(p["c"][0] - T[0], p["c"][1] - T[1]))
        if np.hypot(p["c"][0] - T[0], p["c"][1] - T[1]) <= 2.5 * C:
            return {"name": p["name"], "c": p["c"], "how": "最近标签"}
    if _DARK_THIN is not None and ledger_pts:  # 大设备（封装机 4×4 格）：端口离标签 >2.5 格，闭合外框又没认出来 → 按深色线找外框
        from .pipenet import device_rect
        r = device_rect(_DARK_THIN, T[0], T[1], [])
        if r is not None and r[2] <= 9 * C and r[3] <= 9 * C:
            x, y, w, h = r
            inside = [p for p in ledger_pts if x <= p["c"][0] <= x + w and y <= p["c"][1] <= y + h]
            if inside:
                p = min(inside, key=lambda p: np.hypot(p["c"][0] - T[0], p["c"][1] - T[1]))
                return {"name": p["name"], "c": p["c"], "rect": [x, y, w, h], "how": "线框"}
    if ledger_pts:  # 还找不到：大设备（研磨机/封装机 4 格宽）的端口离标签可到 3.5 格；只在这一个标签明显最近时用
        ds = sorted(ledger_pts, key=lambda p: np.hypot(p["c"][0] - T[0], p["c"][1] - T[1]))
        d0 = np.hypot(ds[0]["c"][0] - T[0], ds[0]["c"][1] - T[1])
        d1 = np.hypot(ds[1]["c"][0] - T[0], ds[1]["c"][1] - T[1]) if len(ds) > 1 else 1e9
        if d0 <= 3.5 * C and d1 - d0 >= C:
            return {"name": ds[0]["name"], "c": ds[0]["c"], "how": "最近标签（远）"}
    return None


def build(img: np.ndarray, items: list[dict], ledger_pts: list[dict] | None = None, rects=None) -> dict:
    """→ {nodes: [...], links: [...], issues: [...]}。ledger_pts：[{name, c}]（底账设备换到拼合图坐标）。"""
    with _BUILD_LOCK:  # 同一进程里一次只跑一个（_DEVS 等模块变量，见 _BUILD_LOCK 的说明）
        return _build(img, items, ledger_pts, rects)


def _build(img: np.ndarray, items: list[dict], ledger_pts: list[dict] | None, rects) -> dict:
    M = masks(img)
    ledger_pts = [p for p in (ledger_pts or []) if p["name"] not in NOT_PORT]
    nodes = []
    for i, o in enumerate(items):
        n = dict(o, id=i)
        n.update(orient(img, o))
        nodes.append(n)
    M["icon"], M["disks"] = icon_disks(img, nodes)  # 只在分叉时用：管子也会从图标底下拐着穿过去，不能整个抹掉
    if USE_CENTERLINE:
        M["storage_line"] = CL.storage_line_icons(img)
        CL.prepare(M, nodes)
    ledger_pts = ledger_pts or []
    if rects is None:
        from .pipenet import _rects
        rects = _rects(img)
    global _DARK_THIN
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    _DARK_THIN = ((cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) < 120) & (hsv[..., 1] < 60)).astype(np.uint8)
    global _DEVS
    if USE_PORTS:
        from . import ports as PT
        _DEVS = [dict(dp, name=p["name"], c=p["c"]) if dp and dp.get("ports") else None for p, dp in zip(ledger_pts, PT.all_ports(img, ledger_pts))]
    else:
        _DEVS = []
    PORT_CMP.clear()
    raw = []
    for n in nodes:
        if n["type"] in BRIDGES:
            continue
        for lay in layer_of(n):
            for d in DIRS:
                P0 = np.array(n["c"], float)
                hop0 = False
                if _blocked(nodes, P0, d, lay, me=n):
                    # 正前方紧贴着另一层的物流件：一般整边跳过；带子从只在管道层的物流件底下穿过去的除外
                    # （2026-11-21 别人的武陵城息壤准入口 → 管道准入口底下 → 扩容反应池，见 _under_first）
                    hop0 = _under_first(M, nodes, n, P0, d, lay)
                    if not hop0:
                        continue
                r = CL.walk(M, nodes, n, d, lay) if USE_CENTERLINE and lay == "pipe" else None  # 传送带仍按格子走（宽、规整，箭头会打断中心线）
                none_here = not hop0 and ((r is not None and r.get("note") == "边外没有中心线") or
                                          (r is None and probe(M, lay, P0, d) is None and _adj(nodes, P0, d, lay, me=n) is None))
                if none_here:
                    dv = _touching_device(P0, d, ledger_pts, rects)
                    if dv is None and lay == "pipe" and n["type"] != LG.GATE:
                        # 暗管只有浅蓝细框，外框认不出：按游戏数据尺寸看正前方是不是暗管的口；口的进出要和物流件这条边对得上
                        # （物流件出口对暗管入口、物流件进料口对暗管出口；说不准的边照旧放行，和按外框认的一样）
                        dv = _touching_tunnel(P0, d, ledger_pts)
                        if dv is not None and _end_says(n, d) == dv["port"]:  # 两头都进 / 都出：不是这台
                            dv = None
                    outlet = dv is not None and dv["name"] in OUTLETS
                    if dv is not None and n["type"] != LG.GATE and (_end_says(n, d) != "in" or outlet):  # 物流件出口直接贴着设备端口（中间没有带子/管子）；进料口贴着设备一般不算（设备外框挡着，多半不是端口），贴着只出不进的设备（暗管出口等）才算
                        raw.append({"end": "device", "path": [P0.copy()], "d": d, "jumps": [], "a": n["id"], "a_side": d,
                                    "layer": lay, "dev_side": d, "device": dv, "touch": True})
                    continue
                if r is None:
                    r = trace(M, nodes, n, d, lay, hop0=hop0)
                r.update(a=n["id"], a_side=d, layer=lay)
                if r["end"] == "device":
                    r["dev_side"], r["device"] = _end_info(M, lay, r, ledger_pts, rects)
                    if r.get("tip") and r["device"] is None:  # 中心线走到头却找不到设备：中心线断了（或物流件直接贴着设备），用按格子走的结果兜底
                        r2 = trace(M, nodes, n, d, lay)
                        r2.update(a=n["id"], a_side=d, layer=lay)
                        if r2["end"] == "device":
                            r2["dev_side"], r2["device"] = _end_info(M, lay, r2, ledger_pts, rects)
                        if r2["end"] == "node" or r2.get("device") is not None:
                            r2["fallback"] = "格子"
                            r = r2
                raw.append(r)
    links, issues = _pair(raw, nodes)
    # 重叠的两截管子拼回一根要在 _dedupe_free 前面：短的那截 ≥80% 落在长的上时会被丢掉，留下的那截一头还挂着远认的错设备
    links += _dedupe_free(_merge_overlap(_stitch(free_segments(M, nodes, links, ledger_pts, rects), M=M, nodes=nodes), nodes))
    links = _drop_ambiguous_tunnel_touch(links, nodes, ledger_pts)  # 设备直连的管子追完再看（它们也算暗管的口在哪边）
    for lk in links:
        lk["arrows"] = arrow_votes(M, lk["layer"], lk["path"], nodes)
    _fix_covered(links, nodes)
    for lk in links:
        _direction(lk, nodes, M, issues)
    _through_gates(links, nodes)
    if USE_PORTS and _DEVS:
        links += _port_bridges(M, links, nodes)
    out = {"nodes": [_node_out(n) for n in nodes], "links": links, "issues": issues}
    out["structures"] = structures(out)
    return out


def _frac(m, P, h=4) -> float:
    H, W = m.shape
    x, y = int(round(P[0])), int(round(P[1]))
    if not (h <= x < W - h and h <= y < H - h):
        return 0.0
    return float(m[y - h:y + h + 1, x - h:x + h + 1].mean())


def _port_bridges(M, links, nodes, max_cells=4) -> list[dict]:
    """两台设备的带子口之间只隔 1–4 格、追线没追出来的短带（一格弯带、U 形弯带）：按格子走补上。
    思路来自 IndustrialPlanner 的格子模型：带子一格一格地占格，口格外面那一格就是带子的第一格。
    从还没接线的出口格外面那一格出发，只走：格中心是黄带、和上一格之间的格边中点也是黄带（并排的两条带子之间隔着空隙，走不过去）、
    没被已经追出来的连线占着、不在别的设备外框里、不是物流件。走到另一台设备一个还没接线的进口格外面那一格就是接上了；
    只接唯一一个。2026-11-10 武陵城左列：种植机 (155,1158) → 采种机 (120,987) 的一格弯带等。
    出口格外面那一格就是物流桥（没有黄带可走）：桥只能直着过，桥对面那一格要么直接是另一台设备的进口格、要么是带子接着按格子走。
    2026-11-14 别人的武陵城（issue #1）：种植机的砂叶出口 → 物流桥 → 粉碎机进口，中间一格带子都没有，粉碎机算成没料。"""
    import cv2 as _cv
    yel = M["belt"]
    H, W = yel.shape
    cov = np.zeros((H, W), np.uint8)
    for lk in links:
        if lk["layer"] == "belt" and len(lk["path"]) >= 2:
            _cv.polylines(cov, [np.array(lk["path"], np.int32)], False, 1, int(0.5 * C))
    ends = [np.asarray(q, float) for lk in links if lk["layer"] == "belt" for q in (lk["path"][0], lk["path"][-1])]
    ports = []  # (设备序号, 口)
    for i, d in enumerate(_DEVS):
        for p in (d or {}).get("ports") or []:
            if p["pipe"]:
                continue
            e = np.asarray(p["edge"], float)
            if any(np.hypot(*(q - e)) <= 0.75 * C for q in ends):
                continue  # 已经有连线的头在这个口上
            ports.append((i, p))
    boxes = [(i, d["box"]) for i, d in enumerate(_DEVS) if d and d.get("box")]

    def inside_dev(P):
        return any(x + 3 <= P[0] <= x + w - 3 and y + 3 <= P[1] <= y + h - 3 for _, (x, y, w, h) in boxes)

    def at_node(P):
        return any(abs(n["c"][0] - P[0]) <= 0.5 * C and abs(n["c"][1] - P[1]) <= 0.5 * C for n in nodes)

    def bridge_at(P):
        return next((n for n in nodes if n["type"] == "物流桥" and abs(n["c"][0] - P[0]) <= 0.5 * C and abs(n["c"][1] - P[1]) <= 0.5 * C), None)
    vs = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}
    targets = []
    for j, q in ports:
        if q["role"] != "in":
            continue
        v = np.asarray(vs[q["side"]], float)
        targets.append((j, q, np.asarray(q["edge"], float) + v * C / 2, np.asarray(q["edge"], float) + v * 4))
    out = []
    for i, p in ports:
        if p["role"] != "out":
            continue
        v = np.asarray(vs[p["side"]], float)
        e = np.asarray(p["edge"], float)
        s0 = e + v * C / 2
        br = bridge_at(s0)
        if br is not None:
            # 直着过桥：桥这一格同时是对面那个进口格外面的一格（进口朝着这个出口）→ 两个口隔着桥直接对上
            across = [(j, q) for j, q, t, _ in targets
                      if j != i and np.hypot(*(s0 - t)) <= 0.35 * C and tuple(vs[q["side"]]) == tuple(-x for x in vs[p["side"]])]
            if len(across) != 1:
                continue
            j, q = across[0]
            a, b = _DEVS[i], _DEVS[j]
            out.append({"layer": "belt", "free": True, "a": None, "a_side": None, "end": "device", "a_end": "device", "bridge": True,
                        "a_dev": {"name": a["name"], "c": a["c"], "how": "端口布局", "port": "out", "port_c": [round(x, 1) for x in p["edge"]]},
                        "a_dev_side": BACK[p["side"]],
                        "device": {"name": b["name"], "c": b["c"], "how": "端口布局", "port": "in", "port_c": [round(x, 1) for x in q["edge"]]},
                        "dev_side": BACK[q["side"]],
                        "path": [[int(round(x)), int(round(y))] for x, y in (e, br["c"], q["edge"])], "jumps": [[int(round(x)) for x in br["c"]]],
                        "arrows": 0, "dir": "a->b",
                        "basis": [f"端口布局：{a['name']}出口 → 直着过物流桥 → {b['name']}进口（按格子补）"]})
            targets = [t for t in targets if not (t[0] == j and t[1] is q)]
            continue
        if _frac(yel, e + v * 4, 3) < 0.4 or _frac(yel, s0) < 0.4 or cov[int(s0[1]), int(s0[0])] or inside_dev(s0) or at_node(s0):
            continue
        seen = {(round(s0[0]), round(s0[1]))}
        frontier = [(s0, [e, s0])]
        found = {}
        for _ in range(max_cells):
            nxt = []
            for u, path in frontier:
                for j, q, t, te in targets:
                    if j != i and np.hypot(*(u - t)) <= 0.35 * C and _frac(yel, te, 3) >= 0.4 and (j, q["edge"]) not in found:
                        found[(j, q["edge"])] = (j, q, path + [np.asarray(q["edge"], float)])
                for dv in vs.values():
                    w = u + np.asarray(dv, float) * C
                    k = (round(w[0]), round(w[1]))
                    if k in seen or not (0 <= w[0] < W and 0 <= w[1] < H):
                        continue
                    if _frac(yel, w) < 0.4 or _frac(yel, (u + w) / 2, 3) < 0.4 or cov[int(w[1]), int(w[0])] or inside_dev(w) or at_node(w):
                        continue
                    seen.add(k)
                    nxt.append((w, path + [w]))
            frontier = nxt
        if len(found) != 1:
            continue
        j, q, path = next(iter(found.values()))
        a, b = _DEVS[i], _DEVS[j]
        lk = {"layer": "belt", "free": True, "a": None, "a_side": None, "end": "device", "a_end": "device", "bridge": True,
              "a_dev": {"name": a["name"], "c": a["c"], "how": "端口布局", "port": "out", "port_c": [round(x, 1) for x in p["edge"]]},
              "a_dev_side": BACK[p["side"]],
              "device": {"name": b["name"], "c": b["c"], "how": "端口布局", "port": "in", "port_c": [round(x, 1) for x in q["edge"]]},
              "dev_side": BACK[q["side"]], "path": [[int(round(x)), int(round(y))] for x, y in path], "jumps": [],
              "arrows": 0, "dir": "a->b", "basis": [f"端口布局：{a['name']}出口 → {len(path) - 2} 格短带 → {b['name']}进口（按格子补）"]}
        out.append(lk)
        for q2 in path:  # 占上，别的口不再走这几格
            _cv.circle(cov, (int(q2[0]), int(q2[1])), int(0.3 * C), 1, -1)
    return out


def _port_disk_behind(M, P, e, dv) -> bool:
    """dv（仓库口）的圆图标在带子头 P 背后、P 已经离开图标（> 半径）：带子是从这个口出来往前走的，前面那台才是终点。
    P 还在图标里（带子已经走进这个 1 格大的口）就不算（2026-11-10 武陵城 (1502,407)：原来被换成了更远的存货口）。"""
    best = None
    for x, y, rr in M.get("disks", []):
        dd = float(np.hypot(x - dv["c"][0], y - dv["c"][1]))
        if dd <= C and (best is None or dd < best[0]):
            best = (dd, x, y, rr)
    if best is None:
        return False
    _, x, y, rr = best
    return float(np.hypot(P[0] - x, P[1] - y)) > rr and float(np.dot((x - P[0], y - P[1]), _v(e))) < 0


def _end_info(M, lay, r, ledger_pts, rects):
    e, dv = _end_info_old(M, lay, r, ledger_pts, rects)
    if r["end"] != "device" or not _DEVS:
        return e, dv
    hit = _port_hit(r, lay, e, only_e=dv is not None and e != r["d"])
    if hit is None and lay == "belt" and _weak(dv) and _under_pipe(M, r["path"][-1]):
        # 只在带子头上压着管子时用：不压管子的带子头「往哪边伸」本来就判得出来。不加这个条件时皇帝产线三个据点有 9 条连线换了设备，
        # 武陵城左列种植机 ↔ 采种机那一圈（作者确认过理论满速）变成了间歇 67%（2026-11-14 重算对比发现）
        hit, alt = _side_port(M, r, [p for p in ledger_pts if not _pipe_only(p["name"])], rects)
        if hit is None and alt is not None:
            return alt
    elif hit is None and lay == "belt" and _weak(dv) and _pipe_near(M, r["path"][-1]):
        # 次级分支（只加不改，上面那条不成立时才走）：带子头没正压在管子上，但旁边 12 px 内有管子压在黄带上（_pipe_near）——追线停在拐角点上，拐角点
        # 离开了管子中线，_under_pipe 量不到深色（2026-11-19 审计：别人的武陵城 (301,610) 那条碳块带往南拐过去进天有洪炉 (409,714)，
        # 一直按线框认成左边的采种机，天有洪炉没碳块）。只收端口布局命中的进料口，不用 alt「往那边探着找设备」：
        # 放开 alt 会把皇帝武陵城塑形机 (1247,815) 那条带子改认成精炼炉
        h2, _ = _side_port(M, r, [p for p in ledger_pts if not _pipe_only(p["name"])], rects)
        if h2 is not None and h2[1].get("role") == "in":
            hit = h2
    if hit is None:
        return e, dv
    i, p = hit
    d = _DEVS[i]
    side = BACK[p["side"]]
    new = {"name": d["name"], "c": d["c"], "how": "端口布局", "port": p["role"], "port_c": [round(v, 1) for v in p["edge"]]}
    if dv is None or dv.get("c") != d["c"] or e != side:
        PORT_CMP.append({"layer": lay, "at": [int(v) for v in r["path"][-1]], "old": (e, dv and dv["name"], dv and dv.get("c")),
                         "new": (side, d["name"], d["c"], p["role"])})
    return side, new


def _under_pipe(M, P, h=4) -> bool:
    """带子头 P 这一小块大半是深色（管子和管子上的小三角；黄带、底色都是浅的）：上面压着一根管子。"""
    x, y = int(round(P[0])), int(round(P[1]))
    w = M["gray"][max(0, y - h):y + h + 1, max(0, x - h):x + h + 1]
    return w.size > 0 and float((w < 170).mean()) >= 0.5


def _pipe_near(M, P, R=12, n=20, g=2) -> bool:
    """带子头 P 周围 R px 的方窗里，压在黄带上（黄带外扩 g px 以内）的深色像素 ≥ n：旁边有管子压着带子走
    （_under_pipe 要求正中压着，拐角点偏出管子中线就量不到）。
    2026-11-19 评审：原来数窗口里全部深色像素（M["pipe"] 就是低饱和深色），设备进出料口那条粗黑边、图标都算，四个据点「弱认、不压管子」
    的带子头里约一半能过门（皇帝景玉谷 18 个全不是管子，如 (166,31) 研磨机口的黑边）。设备黑边和黄带之间隔着一道浅色缝，外扩 2 px 碰不到：
    实测假的这些最多 8 px，别人的武陵城 (302,610) 那条真压在带子拐角上的管子 53 px。外扩 3 px 时景玉谷 (581,61) 的黑边就到 24 了。"""
    x, y = int(round(P[0])), int(round(P[1]))
    H, W = M["pipe"].shape
    y0, y1, x0, x1 = max(0, y - R - g), min(H, y + R + g + 1), max(0, x - R - g), min(W, x + R + g + 1)
    belt = cv2.dilate(M["belt"][y0:y1, x0:x1].astype(np.uint8), np.ones((2 * g + 1, 2 * g + 1), np.uint8)).astype(bool)
    on = M["pipe"][y0:y1, x0:x1] & belt
    wy, wx = max(0, y - R) - y0, max(0, x - R) - x0
    return int(np.count_nonzero(on[wy:wy + (min(H, y + R + 1) - max(0, y - R)), wx:wx + (min(W, x + R + 1) - max(0, x - R))])) >= n


def _side_port(M, r, ledger_pts, rects):
    """带子头最后半格拐进旁边的设备，上面又压着一根顺着走的管子（拐弯的形状看不出来，按「往哪边伸」判成了直走）：
    两侧各看一眼，离带子头 12 px 的地方是黄带（管子盖不到那么宽）。只有一侧是 → 带子拐向那边：那边正对着一个带子口就是那个口；
    那台设备没有端口布局（外框没认出来）就往那边探着找设备。两侧都有黄带或都没有就不认。→ (端口命中, None) 或 (None, (边, 设备)) 或 (None, None)
    2026-11-14 别人的武陵城 (304,99)：碳块带往下走到头向右拐进天有洪炉的进料口，原来按最近标签认成了左边的采种机，天有洪炉的碳块没人送。"""
    from . import ports as PT
    P = np.asarray(r["path"][-1], float)
    sides = [s for s in (LEFT[r["d"]], RIGHT[r["d"]]) if _frac(M["belt"], P + _v(s) * 12, 3) >= 0.4]
    if len(sides) != 1:
        return None, None
    s = sides[0]
    h = PT.port_near(_DEVS, P, s, pipe=False)
    if h is not None:
        return h, None
    # 追线停在拐角上、带子拐过去再走一格才进旁边的设备（U 形）：站到下一格上再看两侧
    # （同一个据点 (302,610)：拐角往南一格，再向右进天有洪炉 (409,714) 的进料口；原来按线框认成了左边的采种机）
    P2 = P + _v(s) * C
    side2 = [t for t in (LEFT[s], RIGHT[s]) if _frac(M["belt"], P2 + _v(t) * 12, 3) >= 0.4]
    if len(side2) == 1:
        h = PT.port_near(_DEVS, P2, side2[0], pipe=False)
        if h is not None:
            return h, None
    for k in (C / 2 + 10, C + 12, 1.5 * C + 12):
        dv = _device_at(P + _v(s) * k, ledger_pts, rects)
        if dv is not None:
            return None, (s, dv)
    return None, None


def _port_hit(r, lay, e, only_e=False):
    """带子/管子头 r["path"][-1]：按端口布局，正前方是哪台设备的哪个口。先看判出来的设备那边（带子头往哪边伸，弯道末端拐向哪边），
    没有再看行进方向（2026-11-10 应龙关 (742,1297)：带子往下走到头往左拐进左边那台，按行进方向会认成正下方储存箱的出口）。"""
    from . import ports as PT
    P = r["path"][-1]
    for d in dict.fromkeys([e] if only_e else [e, r["d"]]):  # 原来的认法已经在拐向的那边找到了设备：不再按行进方向找
        if d is None:
            continue
        hit = PT.port_near(_DEVS, P, d, pipe=lay == "pipe")
        if hit is not None:
            return hit
    return None


def _pipe_only(name) -> bool:
    """按端口布局数据只有管口、没有带子口的设备（暗管入口/出口、废水处理机、水泵…）：带子头不接它。"""
    from . import ports as PT
    lay = PT.layouts().get(name)
    return bool(lay and lay["ports"]) and all(p["pipe"] for p in lay["ports"])


def _end_info_old(M, lay, r, ledger_pts, rects):
    if r["end"] != "device":
        return None, None
    if lay == "belt" and USE_PORTS:  # 2026-11-10 武陵城采种机 (326,1463) 出来的一格弯带头被按最近标签认成了暗管入口 (111,1567)
        ledger_pts = [p for p in ledger_pts if not _pipe_only(p["name"])]
    if lay == "pipe":  # 仓库口只进出物品、不接管子（管子从存取线上方经过）：管子头不认成仓库口（2026-11-10 应龙关 #27 往北被接到了取货口）
        ledger_pts = [p for p in ledger_pts if p["name"] not in ("仓库取货口", "仓库存货口", "仓库口")]
    P = r["path"][-1]
    if r.get("tip"):  # 中心线追踪：P 是线头（到了设备边上），走向就是设备那边
        e = r["d"]
        T = np.asarray(r["disk"][:2], float) if r.get("disk") else np.asarray(P, float) + _v(e) * 12
        dv = _device_at(T, ledger_pts, rects)
        if dv is None and not r.get("disk"):  # 大设备（扩容反应池 6×5 格）的管口离标签 100+ px、外框又没认出：往里多探一格再认
            # （2026-11-10 武陵城扩容反应池 (1346,1362) 左边两个出液口，原来两根溶液管的这头都是「不知道」）
            dv = _device_at(np.asarray(P, float) + _v(e) * (12 + C), ledger_pts, rects)
        return e, dv
    if lay == "belt":  # 带子头紧贴仓库取货口/存货口的圆图标：就是这个口（1 格大、挂在存取线上，带子直接怼上去；
        # 弯道上判「往哪边伸」不可靠，2026-11-09 武陵城取货口 (1636,556) 的 S 形碳块带被判成往南进了天有洪炉）
        for x, y, rr in M.get("disks", []):
            dd = float(np.hypot(P[0] - x, P[1] - y))
            if dd <= rr + 18 and float(np.dot((x - P[0], y - P[1]), _v(r["d"]))) > -0.3 * dd:  # 图标在背后的不算（一格长的带子两头都挨着它）
                lab = [p for p in ledger_pts if np.hypot(p["c"][0] - x, p["c"][1] - y) <= C and p["name"] in ("仓库取货口", "仓库存货口")]
                if lab:
                    p = min(lab, key=lambda p: np.hypot(p["c"][0] - x, p["c"][1] - y))
                    return CL.cardinal((x - P[0], y - P[1])), {"name": p["name"], "c": p["c"], "how": "贴着仓库口图标"}
    e = r["d"] if r.get("straight") else stub_side(M, lay, P, r["d"])  # 从桥里直接出来进设备：就是直走那边
    dv = _device_at(np.asarray(P) + _v(e) * (C / 2 + 10), ledger_pts, rects)
    if dv is not None and not dv.get("rect") and dv["name"] in ("仓库取货口", "仓库存货口")             and _port_disk_behind(M, P, e, dv):  # 按最近标签认成了背后的仓库口：去前面找
        # （2026-11-09 武陵城 取货口 (888,405) 往北一格进精炼炉 (908,304)：北头探点在精炼炉粗边框外 4 px，取货口标签反而近 2 px）
        ahead = [p for p in ledger_pts if float(np.dot(np.asarray(p["c"]) - np.asarray(P), _v(e))) > 0]
        if dv["c"] not in [p["c"] for p in ahead]:
            dv2 = _device_at(np.asarray(P) + _v(e) * (C / 2 + 10), ahead, rects)
            if dv2 is not None:  # 前面没有别的设备可认（带子已经走进 1 格大的取货口里）：保留原来的
                dv = dv2
    if dv is None and e != r["d"]:  # 判的那边没有设备：按行进方向再找（景玉谷 (30,929) 往南进中容封装机，stub 判成东）
        dv2 = _device_at(np.asarray(P) + _v(r["d"]) * (C / 2 + 10), ledger_pts, rects)
        if dv2 is not None:
            return r["d"], dv2
    if dv is None:  # 探点落在设备边上的口格里（口格自己也是一圈深色线框，里面没有底账标签）：沿同一方向再往设备里探一格；
        # 只认外框/线框（按最近标签认会把同一条带子的头一会儿认成配件机、一会儿认成灌装机，2026-11-09 应龙关试过）
        dv2 = _device_at(np.asarray(P) + _v(e) * (C + 12), ledger_pts, rects)
        if dv2 is not None and dv2.get("rect"):
            dv = dv2
    return e, dv


def free_segments(M, nodes, links, ledger_pts, rects, step=12) -> list[dict]:
    """不经过物流件的带子/管子（设备 → 设备直连）：在还没被追到的带子/管子中段取种子点，往两头各追一遍。
    带子的种子点沿走向按最近的物流件对齐到格中心（同一张蓝图里格相位基本一致；管子不对齐，见下），这样拐弯搜索 ±12 px 够用。
    两头都接设备（或一头分叉）才收；碰到物流件的说明物流件那头已经追过（或追失败过），跳过免得重复。"""
    H, W = M["gray"].shape
    cov = {lay: np.zeros((H, W), np.uint8) for lay in ("belt", "pipe")}
    import cv2 as _cv
    for lk in links:
        pts = np.array(lk["path"], np.int32)
        if len(pts) >= 2:
            _cv.polylines(cov[lk["layer"]], [pts], False, 1, 24 if lk["layer"] == "belt" else 14)
    for n in nodes:
        for lay in ("belt", "pipe"):
            _cv.rectangle(cov[lay], (int(n["c"][0]) - 18, int(n["c"][1]) - 18), (int(n["c"][0]) + 18, int(n["c"][1]) + 18), 1, -1)
    ncs = np.array([n["c"] for n in nodes], float) if nodes else np.zeros((0, 2))
    out = []
    for lay in ("belt", "pipe"):
        m = M[lay]
        ys, xs = np.mgrid[step // 2:H:step, step // 2:W:step]
        if lay == "pipe":  # 管子只有 6 px 宽，会从两列格点（间隔 12）中间穿过去、一个种子都落不上（2026-11-10 武陵城扩容反应池的锦草溶液管）
            m = _cv.dilate(m.astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
        cand = m[ys, xs] & (cov[lay][ys, xs] == 0)
        for x, y in zip(xs[cand], ys[cand]):
            if cov[lay][y, x]:
                continue
            seeds = []
            for axis, (d, pd) in (("h", ("E", "N")), ("v", ("S", "E"))):
                c = _cross(M, lay, (float(x), float(y)), d, 0)   # 横截线垂直于走向
                c2 = _cross(M, lay, (float(x), float(y)), d, 6)
                strict = not (c is None or c2 is None or abs(c - c2) > 2 or abs(c) > 6)
                if not strict and (c is None or abs(c) > 6):
                    continue
                dx, dy = DIRS[d]
                P = np.array([x - dy * c, y + dx * c], float)  # 横向对中
                if len(ncs) and lay == "belt":  # 沿走向对齐到格中心
                    # 只对带子做：管子走的是中心线追踪（CL.walk 从种子附近找 1 px 中心线），不需要格相位；管子只有 6 px 宽，
                    # 对齐会把种子沿走向挪出去最多半格，短管、拐角附近反而挪离了线（2026-11-19 审计；要和 centerline._seed
                    # 的取整修法一起上：单改这一处时皇帝武陵城有 6 台状态变了，两处一起改皇帝产线 0 台变化）
                    k = int(np.argmin(np.hypot(*(ncs - P).T)))
                    ax = 0 if axis == "h" else 1
                    P[ax] = ncs[k][ax] + round((P[ax] - ncs[k][ax]) / C) * C
                seeds.append((P, d, strict))
            seeds.sort(key=lambda s: not s[2])  # 先用两条横截线都对得上的方向
            if not seeds or not seeds[0][2]:
                continue
            for si, (P, d, strict) in enumerate(seeds):
                pseudo = {"type": "seed", "c": [float(P[0]), float(P[1])], "id": -1}
                tr = CL.walk if USE_CENTERLINE and lay == "pipe" else trace
                r1 = tr(M, nodes, pseudo, d, lay)
                r2 = tr(M, nodes, pseudo, BACK[d], lay)
                path = [list(map(float, q)) for q in r2["path"][::-1]] + [list(map(float, q)) for q in r1["path"][1:]]
                pts = np.array(path, np.int32)
                _cv.polylines(cov[lay], [pts], False, 1, 24 if lay == "belt" else 14)
                _cv.circle(cov[lay], (int(x), int(y)), 14, 1, -1)
                if lay == "belt" and si + 1 < len(seeds) and len(r1["path"]) == 1 and len(r2["path"]) == 1 \
                        and r1["end"] == r2["end"] == "device":
                    continue  # 往两头都一步也走不动：种子方向判反了（带子大半压在管子底下，露出的一小块看着像竖的，
                    # 2026-11-09 武陵城 精炼炉 (1112,748) → 塑形机 (972,850)），换另一个方向再追一次
                if len(path) < 2 or r1["end"] == "node" or r2["end"] == "node":
                    break
                e1, d1 = _end_info(M, lay, r1, ledger_pts, rects)
                e2, d2 = _end_info(M, lay, r2, ledger_pts, rects)
                plen = float(sum(np.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path[:-1], path[1:])))
                short = plen < 1.6 * C if USE_CENTERLINE and lay == "pipe" else len(path) < 3  # 中心线的路径是折线拐点，不是一格一个点
                if d1 is None and d2 is None and short:
                    break  # 两头都没接设备的长段先留着，可能是被拐角断开的中段，_stitch 拼完还没接设备的再丢
                if d1 is not None and d2 is not None and d1.get("c") == d2.get("c") and (tr is not trace or USE_PORTS):
                    break  # 中心线绕着设备图案自己转了一圈（设备上的深色图案），两头是同一台设备；
                    # 传送带也一样（2026-11-10 景玉谷精炼炉 (1416,65)：带子头不再认成暗管入口后，从一头绕回同一台的一段，没有用）
                if short and (d1 is None or d2 is None or d1.get("c") == d2.get("c")):
                    break  # 一格长的只收两头是两台不同设备的（仓库口 → 设备的短带）
                out.append({"layer": lay, "free": True, "a": None, "a_side": None, "end": r1["end"] if r1["end"] != "device" else "device",
                            "a_end": r2["end"], "a_dev": d2, "a_dev_side": e2, "device": d1, "dev_side": e1,
                            "path": [[int(a), int(b)] for a, b in path], "jumps": r1.get("jumps", []) + r2.get("jumps", [])})
                break
    return out


def _dedupe_free(free, tol=8, frac=0.8) -> list[dict]:
    """同一根管子/带子被两个种子各追了一遍：一段的点 ≥80% 都在另一段更长的同层路径上（≤8 px），丢掉短的。
    2026-11-10 武陵城：锦草溶液管从中间又追了一段，一头停在汇流器上被认成了封装机。"""
    pts = [_dense(f["path"]) for f in free]
    lens = [float(sum(np.hypot(*(np.asarray(b, float) - np.asarray(a, float))) for a, b in zip(f["path"][:-1], f["path"][1:]))) for f in free]
    drop = set()
    for i, f in enumerate(free):
        for j, g in enumerate(free):
            if i == j or j in drop or f["layer"] != "pipe" or g["layer"] != "pipe" or lens[j] <= lens[i]:  # 只管管道：传送带去重会误删并排的真带子（景玉谷试过）
                continue
            d = np.min(np.hypot(pts[i][:, None, 0] - pts[j][None, :, 0], pts[i][:, None, 1] - pts[j][None, :, 1]), axis=1)
            if float((d <= tol).mean()) >= frac:
                drop.add(i)
                break
    return [f for k, f in enumerate(free) if k not in drop]


def _weak(dv) -> bool:
    """断头没接设备，或只按最近标签 / 深色线框弱认的设备（外框、贴着仓库口图标的算认准了）。"""
    return dv is None or str(dv.get("how", "")).startswith("最近标签") or dv.get("how") == "线框"


def _on_path(p, q, path, tol=6) -> bool:
    """缺口中点就在某一截自己追过的路径上：两截是重叠的，不是断开的（景玉谷 (467,1632)：一段假带子的头落在真带子上）。"""
    m = (np.asarray(p, float) + np.asarray(q, float)) / 2
    for a, b in zip(path[:-1], path[1:]):
        a, b = np.asarray(a, float), np.asarray(b, float)
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 < 1e-6 else min(1.0, max(0.0, float((m - a) @ ab) / L2))
        if np.hypot(*(a + t * ab - m)) <= tol:
            return True
    return False


def _covered_gap(M, p, q, nodes=(), maxgap=2.5 * C) -> bool:
    """两个断头在同一条横/竖线上（偏差 ≤6 px），相距 ≤2.5 格，中间那段 ≥90% 的点有带子或管子（带子被顺着走的管子压住半边）。
    2026-11-09 武陵城 取货口 (1636,556) → 天有洪炉 (1679,919) 的碳块带：往左鼓出去那段左半边压着一根竖管，断成两截。"""
    p, q = np.asarray(p, float), np.asarray(q, float)
    dx, dy = q - p
    L = float(np.hypot(dx, dy))
    if L < 8 or L > maxgap or min(abs(dx), abs(dy)) > 6:
        return False
    for nd in nodes:  # 中间夹着物流件（桥是十字交叉，两边不是同一条）
        c = np.asarray(nd["c"], float)
        t = float(np.dot(c - p, (dx, dy))) / (L * L)
        if -0.05 <= t <= 1.05 and np.hypot(*(p + t * np.array([dx, dy]) - c)) <= 20:
            return False
    m = M["belt"] | M["pipe"]
    H, W = m.shape
    ok = n = 0
    yel = M["yel"]
    ly = 0
    lat = (12, 0) if abs(dy) > abs(dx) else (0, 12)  # 横向 ±12 px 的窗
    for t in np.linspace(0, 1, int(L) + 1):
        x, y = int(round(p[0] + dx * t)), int(round(p[1] + dy * t))
        n += 1
        ok += 0 <= x < W and 0 <= y < H and bool(m[max(0, y - 2):y + 3, max(0, x - 2):x + 3].any())
        ly += 0 <= x < W and 0 <= y < H and bool(yel[max(0, y - lat[1]):y + lat[1] + 1, max(0, x - lat[0]):x + lat[0] + 1].any())
    return ok >= 0.9 * n and ly >= 0.8 * n  # 还得一路看得到黄色带面（设备的深色外框线也在管子掩码里，2026-11-10 景玉谷被它拼错过）


def _stitch(free, reach=1.6 * C, M=None, nodes=()) -> list[dict]:
    """同一条带子在拐角处断成两段（两段的断头都没接设备，离得 ≤ 1.6 格）→ 拼成一条。
    2026-11-09 景玉谷：精炼炉 → 天有洪炉的碳块长带在上边缘拐角断开，两头都成了「没接设备」。"""
    free = list(free)
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(free):
            for j, b in enumerate(free):
                if i >= j or a["layer"] != b["layer"]:
                    continue
                ends_a = [("head", a["path"][0], a.get("a_dev")), ("tail", a["path"][-1], a.get("device"))]
                ends_b = [("head", b["path"][0], b.get("a_dev")), ("tail", b["path"][-1], b.get("device"))]
                hit = next(((ea, eb) for ea in ends_a for eb in ends_b
                            if ea[2] is None and eb[2] is None and np.hypot(ea[1][0] - eb[1][0], ea[1][1] - eb[1][1]) <= reach), None)
                if hit is None and M is not None and a["layer"] == "belt":
                    hit = next(((ea, eb) for ea in ends_a for eb in ends_b
                                if _weak(ea[2]) and _weak(eb[2]) and _covered_gap(M, ea[1], eb[1], nodes)
                                and not _on_path(ea[1], eb[1], a["path"]) and not _on_path(ea[1], eb[1], b["path"])), None)
                if hit is None:
                    continue
                (wa, _, _), (wb, _, _) = hit
                pa = a["path"] if wa == "tail" else a["path"][::-1]           # a 的断头放到末尾
                pb = b["path"] if wb == "head" else b["path"][::-1]           # b 的断头放到开头
                head_dev = a.get("a_dev") if wa == "tail" else a.get("device")
                head_side = a.get("a_dev_side") if wa == "tail" else a.get("dev_side")
                tail_dev = b.get("device") if wb == "head" else b.get("a_dev")
                tail_side = b.get("dev_side") if wb == "head" else b.get("a_dev_side")
                m = dict(a, path=pa + pb, a_dev=head_dev, a_dev_side=head_side, device=tail_dev, dev_side=tail_side,
                         jumps=a.get("jumps", []) + b.get("jumps", []), stitched=a.get("stitched", 0) + b.get("stitched", 0) + 1)
                free = [x for k, x in enumerate(free) if k not in (i, j)] + [m]
                changed = True
                break
            if changed:
                break
    return [f for f in free if f.get("a_dev") or f.get("device")]


FAR_HOW = "最近标签（远）"


def _loose_end(dv) -> bool:
    """设备直连管子的一头没认出设备，或只按「最近标签（远）」认的（离标签 2.5–3.5 格，_device_at 最后一档）。
    注意和 _stitch 用的 _weak 不是一回事：_weak 把 2.5 格内的最近标签、线框也算弱；这里只有远认的算弱，近的最近标签、线框算认准了
    （2026-11-21：别人的武陵城那根管子进反应池 (804,1215) 那头就是 2.2 格内的最近标签，按 _weak 算两截就拼不上）。别把两个合成一个。"""
    return dv is None or dv.get("how") == FAR_HOW


def _dense(path, step=4) -> np.ndarray:
    """折线按 step px 加密成点（含两头）。"""
    out = []
    for a, b in zip(path[:-1], path[1:]):
        a, b = np.asarray(a, float), np.asarray(b, float)
        n = max(1, int(np.hypot(*(b - a)) // step))
        out += [a + (b - a) * t / n for t in range(n)]
    return np.array(out + [np.asarray(path[-1], float)])


def _plen(path) -> float:
    return float(sum(np.hypot(*(np.asarray(b, float) - np.asarray(a, float))) for a, b in zip(path[:-1], path[1:])))


def _foot(p, path):
    """点 p 到折线 path 最近的那一段 → (距离, 段号 k（path[k]–path[k+1]）, 垂足)。"""
    p = np.asarray(p, float)
    if len(path) < 2:
        q = np.asarray(path[0], float)
        return float(np.hypot(*(q - p))), 0, q
    best = None
    for k, (a, b) in enumerate(zip(path[:-1], path[1:])):
        a, b = np.asarray(a, float), np.asarray(b, float)
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 < 1e-6 else min(1.0, max(0.0, float((p - a) @ ab) / L2))
        q = a + t * ab
        dd = float(np.hypot(*(q - p)))
        if best is None or dd < best[0]:
            best = (dd, k, q)
    return best


def _on_poly(pts, path, tol) -> float:
    """pts 里落在折线 path 上（≤ tol px）的点占多少。"""
    return float(np.mean([_foot(p, path)[0] <= tol for p in pts])) if len(pts) else 0.0


def _merge_overlap(free, nodes=(), tol=10.0, along_tol=8.0, min_overlap=C, bridge_r=20.0) -> list[dict]:
    """同一根管子被两个种子各追了一截、两截中间重叠，各自的断头又被「最近标签（远）」认成了不相干的设备 → 拼回一根。
    2026-11-21 issue #1 第五轮（续）：别人的武陵城，提纯机 (底账 1454,1136) → 反应池 (804,1215) 的壤晶废液管被追成两截自由段：
    一截从东北拐角 (拼合图 2005,1160) 起、往西再往南进反应池，拐角那头按远认成了封装机；另一截从西北拐角 (1364,1163) 起、
    往东再往南进提纯机，拐角那头远认成了粉碎机；两截在 y≈1156 那段横管上重叠约 640 px。两截各算一条连线，提纯机的壤晶废液
    送不到反应池，反应池 (804,1215) 和它下游的封装机 (655,1284) 没料。_stitch 只拼两个断头挨着的（拐角断开），_dedupe_free 只丢
    ≥80% 落在另一截上的，这种两头各伸出去一大截的重叠都不管。
    条件（复核要求加的保险，都要满足）：
    - 只对管子（传送带并排的真带子多，_dedupe_free 试过误删）；
    - 两截各有一头认出了设备（外框、端口布局、线框、2.5 格内的最近标签）、另一头没认出或是最近标签（远）。
      不只认外框 / 端口布局：上面这根管子进反应池那头就是 2.2 格内的最近标签（反应池没认出外框和端口布局）；
    - A 的弱头落在 B 的路径上、B 的弱头也落在 A 的路径上（≤ tol px）；
    - 两截共线重叠至少一格：A 从 B 的弱头落点走到自己弱头的那一截、B 从自己弱头走到 A 的弱头落点的那一截，各自 ≥ min_overlap，
      而且加密后的点 ≥90% 落在对方路径上（≤ along_tol px）——只是十字交叉、或者一头碰到另一截的，不算；
    - 两个弱头 bridge_r px 内没有管道桥 / 物流桥（桥上是两根管子十字交叉，断在桥边的不一定是同一根）；
    - 两截认出的设备不是同一台。
    拼成一条：A 的准头 → A 沿路走到 B 的弱头落点 → B 剩下的路 → B 的准头。方向照旧由 _direction 按端口布局和小三角定。"""
    free = list(free)
    bridges = [np.asarray(n["c"], float) for n in nodes if n["type"] in BRIDGES]

    def weak_at(f):
        """弱头在 path 的哪一头（'head' / 'tail'）；不是「一头准、一头弱」→ None。"""
        h, t = _loose_end(f.get("a_dev")), _loose_end(f.get("device"))
        return "head" if h and not t else "tail" if t and not h else None

    def sure(f, weak):
        """准头那头 → (设备, side, end 类型)。"""
        return (f.get("device"), f.get("dev_side"), f.get("end")) if weak == "head" else (f.get("a_dev"), f.get("a_dev_side"), f.get("a_end"))

    def key(dv):
        return dv["name"], round(dv["c"][0]), round(dv["c"][1])

    changed = True
    while changed:
        changed = False
        for i, A in enumerate(free):
            wa = weak_at(A) if A["layer"] == "pipe" and len(A["path"]) >= 2 else None
            if wa is None:
                continue
            PA = [list(q) for q in (A["path"] if wa == "tail" else A["path"][::-1])]  # A：准头 → 弱头
            for j, B in enumerate(free):
                wb = weak_at(B) if j != i and B["layer"] == "pipe" and len(B["path"]) >= 2 else None
                if wb is None:
                    continue
                PB = [list(q) for q in (B["path"] if wb == "head" else B["path"][::-1])]  # B：弱头 → 准头
                (da, sa, ea), (db, sb, eb) = sure(A, wa), sure(B, wb)
                if key(da) == key(db):
                    continue
                xa, xb = PA[-1], PB[0]  # 两个弱头
                fa, ka, qa = _foot(xa, PB)
                fb, kb, qb = _foot(xb, PA)
                if fa > tol or fb > tol:
                    continue
                if any(np.hypot(*(c - np.asarray(x, float))) <= bridge_r for c in bridges for x in (xa, xb)):
                    continue
                ov_a = [list(qb)] + PA[kb + 1:]       # A 上的重叠段：B 的弱头落点 → A 的弱头
                ov_b = PB[:ka + 1] + [list(qa)]       # B 上的重叠段：B 的弱头 → A 的弱头落点
                if _plen(ov_a) < min_overlap or _plen(ov_b) < min_overlap:
                    continue
                if _on_poly(_dense(ov_a), PB, along_tol) < 0.9 or _on_poly(_dense(ov_b), PA, along_tol) < 0.9:
                    continue
                path = [[int(round(x)), int(round(y))] for x, y in PA[:kb + 1] + PB]
                jumps = []
                for q in A.get("jumps", []) + B.get("jumps", []):
                    if list(q) not in jumps:
                        jumps.append(list(q))
                m = dict(A, path=path, a_dev=da, a_dev_side=sa, a_end=ea, device=db, dev_side=sb, end=eb, jumps=jumps,
                         stitched=A.get("stitched", 0) + B.get("stitched", 0) + 1, overlap_merged=True)
                free = [x for k, x in enumerate(free) if k not in (i, j)] + [m]
                changed = True
                break
            if changed:
                break
    return free


def _through_gates(links, nodes):
    """准入口一进一出在同一条直线上：一边方向知道了，对面那边顺着传过去（可能连着传好几个口）。"""
    def ends(lk):
        yield lk["a"], lk["a_side"], (lk["dir"] == "b->a") if lk["dir"] else None  # True = 流进这个物流件
        if lk.get("b") is not None:
            yield lk["b"], lk["b_side"], (lk["dir"] == "a->b") if lk["dir"] else None
    for _ in range(10):
        changed = False
        at = {}
        for lk in links:
            for nid, side, into in ends(lk):
                if nid is None:
                    continue
                at[(nid, side, lk["layer"])] = (lk, into)
        for (nid, side, lay), (lk, into) in list(at.items()):
            if nodes[nid]["type"] != LG.GATE or into is None:
                continue
            opp = at.get((nid, BACK[side], lay))
            if not opp or opp[0]["dir"] is not None:
                continue
            o = opp[0]
            # 对面那条：into → 从这个口流出去
            o_from_here = (o["a"] == nid and o["a_side"] == BACK[side])
            out_dir = "a->b" if o_from_here else "b->a"
            o["dir"] = out_dir if into else ("b->a" if out_dir == "a->b" else "a->b")
            o["basis"] = o.get("basis", []) + ["经准入口顺过来"]
            changed = True
        if not changed:
            break


def _node_out(n):
    keep = ("id", "type", "p", "c", "bp", "kind", "icon_group", "item", "in", "outs", "out", "ins", "covered", "fixed_by_arrows", "score")
    return {k: n[k] for k in keep if k in n}


def _pair(raw, nodes):
    """A→B 和 B→A 两次追踪合成一条连接；只有一头追到的记「单向」。"""
    links, issues, used = [], [], set()
    by = {(r["a"], r["a_side"], r["layer"]): i for i, r in enumerate(raw)}
    for i, r in enumerate(raw):
        if i in used:
            continue
        used.add(i)
        lk = {"layer": r["layer"], "a": r["a"], "a_side": r["a_side"], "end": r["end"],
              "path": [[int(x), int(y)] for x, y in r["path"]], "jumps": r.get("jumps", [])}
        if r["end"] == "node":
            lk.update(b=r["node"]["id"], b_side=r["side"])
            j = by.get((r["node"]["id"], r["side"], r["layer"]))
            if j is not None and raw[j]["end"] == "node" and raw[j]["node"]["id"] == r["a"] and raw[j]["side"] == r["a_side"]:
                used.add(j); lk["both"] = True
            else:
                lk["both"] = False
                if not (nodes[r["node"]["id"]]["type"] in BRIDGES):
                    issues.append({"kind": "单向", "a": r["a"], "b": r["node"]["id"], "layer": r["layer"],
                                   "msg": f"从 {nodes[r['a']]['type']} 追到 {nodes[r['node']['id']]['type']}，反过来没追回来"})
        elif r["end"] == "device":
            lk.update(dev_side=r.get("dev_side"), device=r.get("device"))
            if r.get("touch"):
                lk["touch"] = True
        elif r["end"] == "fork":
            lk["ex"] = r.get("ex"); lk["ex_s"] = r.get("ex_s"); lk["strong"] = r.get("strong"); lk["d"] = r["d"]
            issues.append({"kind": "分叉", "a": r["a"], "layer": r["layer"], "at": lk["path"][-1],
                           "msg": "走到一格同时能往两边走（可能是两条线在这里挨着），没继续追"})
        else:
            issues.append({"kind": "太长", "a": r["a"], "layer": r["layer"], "msg": "追了 400 格还没到头"})
        links.append(lk)
    return links, issues


def _fix_covered(links, nodes):
    """上面压着管道的分流器/汇流器（模板和明暗说法不一致）：按接在各边的带子上的小三角重新定进/出口。
    例：武陵城 4 个传送带汇流器，横向那两边被管道盖住，模板猜的出口方向和小三角相反（2026-11-09）。"""
    for n in nodes:
        if not n.get("covered") or n["type"] not in SPLIT + MERGE:
            continue
        into = {}
        for lk in links:
            if lk["layer"] not in layer_of(n) or abs(lk["arrows"]) < 2:
                continue
            if lk["a"] == n["id"]:
                into[lk["a_side"]] = lk["arrows"] < 0
            if lk.get("b") == n["id"]:
                into[lk["b_side"]] = lk["arrows"] > 0
        if n["type"] in MERGE:
            outs = [sd for sd, v in into.items() if not v]
            new = outs[0] if len(outs) == 1 else None
            if new is None and into.get(n["out"]) is True:  # 模板猜的出口其实在进料：出口在被压住那条线的另一头
                new = BACK[n["out"]]
            if new and new != n["out"]:
                n["out"], n["ins"] = new, [sd for sd in DIRS if sd != new]
                n["fixed_by_arrows"] = True
        else:
            ins = [sd for sd, v in into.items() if v]
            new = ins[0] if len(ins) == 1 else None
            if new is None and into.get(n["in"]) is False:
                new = BACK[n["in"]]
            if new and new != n["in"]:
                n["in"], n["outs"] = new, [sd for sd in DIRS if sd != new]
                n["fixed_by_arrows"] = True


def _end_says(n, side):
    """物流件某一边说的方向：'in'（东西从这边进来）/ 'out'（从这边出去）/ None（说不准）。"""
    if n["type"] in SPLIT and "in" in n:
        return "in" if side == n["in"] else "out"
    if n["type"] in MERGE and "out" in n:
        return "out" if side == n["out"] else "in"
    return None


def _port_says(dv):
    """设备那头按端口布局认出的口：'in'（东西进设备）/ 'out' / None。"""
    return dv.get("port") if dv else None


def _direction(lk, nodes, M, issues):
    if lk.get("free"):  # 设备 → 设备直连：两头的口（端口布局）说了算，没有再看小三角
        v = lk["arrows"]
        pa, pb = _port_says(lk.get("a_dev")), _port_says(lk.get("device"))
        says = {("a->b" if pa == "out" else "b->a") if pa else None, ("a->b" if pb == "in" else "b->a") if pb else None} - {None}
        if len(says) == 1:
            lk["dir"] = says.pop()
            nm = lambda dv: dv["name"] if dv else "?"  # noqa: E731
            src, dst = (lk.get("a_dev"), lk.get("device")) if lk["dir"] == "a->b" else (lk.get("device"), lk.get("a_dev"))
            lk["basis"] = [f"端口布局：{nm(src)}出口 → {nm(dst)}进口" if (pa and pb) else
                           f"端口布局：{nm(lk.get('a_dev') if pa else lk.get('device'))}的{'出口' if (pa or pb) == 'out' else '进口'}"]
            if abs(v) >= 3 and (v > 0) != (lk["dir"] == "a->b"):  # 端口布局只管线头那一格；小三角大票数相反多半是两条带子被追成了一条
                # （2026-11-10 景玉谷研磨机 (205,355)：往上流的竖带和底下往左流进研磨机的一格接成了一条），两边都不信
                issues.append({"kind": "端口布局存疑", "a": None, "b": None, "layer": lk["layer"], "at": lk["path"][len(lk["path"]) // 2],
                               "msg": f"端口布局说 {lk['dir']}，小三角 {v:+d} 票相反，不定方向（{lk['path'][0]} … {lk['path'][-1]}）"})
                lk["dir"], lk["basis"] = None, []
            return
        if len(says) == 2:
            issues.append({"kind": "端口布局存疑", "a": None, "b": None, "layer": lk["layer"], "at": lk["path"][len(lk["path"]) // 2],
                           "msg": f"两头设备的口都说{'出' if pa == 'out' else '进'}（{lk['path'][0]} … {lk['path'][-1]}）"})
        lk["dir"] = ("a->b" if v > 0 else "b->a") if abs(v) >= 2 else None
        lk["basis"] = [f"小三角 {abs(v)} 票"] if lk["dir"] else []
        return
    a = nodes[lk["a"]]
    sa = _end_says(a, lk["a_side"])
    sb = _end_says(nodes[lk["b"]], lk["b_side"]) if lk.get("b") is not None else None
    pb = _port_says(lk.get("device")) if lk.get("b") is None else None
    if sb is None and pb:  # 另一头是设备、按端口布局认出了是进口还是出口
        sb = pb
    votes = lk["arrows"]
    basis = []
    fwd = None  # True：a → b（沿 path 方向）
    if sa:
        fwd = sa == "out"; basis.append(f"{a['type']}{'出口' if sa == 'out' else '进料口'}")
    if sb:
        fb = sb == "in"
        guess = lk.get("b") is None and (lk.get("device") or {}).get("how") == TUNNEL_HOW
        basis.append(f"{nodes[lk['b']]['type'] if lk.get('b') is not None else lk['device']['name']}{'进料口' if sb == 'in' else '出口'}"
                     + ("（贴着暗管口，中间没有管子，口的位置按游戏数据尺寸推断）" if guess else "（端口布局）" if lk.get("b") is None else ""))
        if fwd is None:
            fwd = fb
        elif fwd != fb:
            issues.append({"kind": "方向冲突", "a": lk["a"], "b": lk.get("b"), "layer": lk["layer"],
                           "msg": f"两头都说{'出' if sa == 'out' else '进'}（{a['type']} {lk['a_side']} 边 ↔ "
                                  f"{nodes[lk['b']]['type'] + ' ' + lk['b_side'] + ' 边' if lk.get('b') is not None else lk['device']['name'] + '的口（端口布局）'}）"})
            fwd = None
    if votes and pb and sb == pb and not sa and (votes > 0) != (pb == "in") and abs(votes) >= 3:
        issues.append({"kind": "端口布局存疑", "a": lk["a"], "b": None, "layer": lk["layer"],
                       "msg": f"端口布局说 {'a→b' if pb == 'in' else 'b→a'}，小三角 {votes:+d} 票相反，不定方向"})
        lk["dir"], lk["basis"] = None, []
        return
    if votes:
        fv = votes > 0
        if fwd is None and abs(votes) >= 2:
            fwd = fv; basis.append(f"小三角 {abs(votes)} 票")
        elif fwd is not None and fv != fwd and abs(votes) >= 3:
            issues.append({"kind": "小三角反了", "a": lk["a"], "b": lk.get("b"), "layer": lk["layer"],
                           "msg": f"物流件说 {'a→b' if fwd else 'b→a'}，小三角 {votes:+d} 票相反"})
    lk["dir"] = None if fwd is None else ("a->b" if fwd else "b->a")
    lk["basis"] = basis


# ------------------------------------------------------------------ 局部结构（给设计分析计算器用，4.7）
def _flows(res):
    """有方向的连接 → [(从, 到, 连接)]，「从/到」是 ('n', 物流件 id) 或 ('d', 设备名@坐标)。"""
    out = []
    for lk in res["links"]:
        if not lk.get("dir"):
            continue
        if lk["a"] is None:
            dv = lk.get("a_dev") or {}
            c = dv.get("c") or lk["path"][0]
            a = ("d", f"{dv.get('name') or '设备'}@{int(c[0])},{int(c[1])}")
        else:
            a = ("n", lk["a"])
        if lk.get("b") is not None:
            b = ("n", lk["b"])
        elif lk["end"] == "device":
            dv = lk.get("device") or {}
            c = dv.get("c") or lk["path"][-1]
            b = ("d", f"{dv.get('name') or '设备'}@{int(c[0])},{int(c[1])}")
        else:
            continue
        out.append((a, b, lk) if lk["dir"] == "a->b" else (b, a, lk))
    return out


def structures(res) -> list[dict]:
    """局部结构：单进单出汇流器（降优先级/溢流位）、分流回环、准入口紧挨分流/汇流器、设备几条进料带。"""
    nodes = res["nodes"]
    fl = _flows(res)
    out = []
    for n in nodes:
        if n["type"] not in MERGE + SPLIT:
            continue
        ins = [f for f in fl if f[1] == ("n", n["id"])]
        outs = [f for f in fl if f[0] == ("n", n["id"])]
        sides = {lk["a_side"] if lk["a"] == n["id"] else lk["b_side"] for lk in res["links"] if n["id"] in (lk["a"], lk.get("b"))}
        if n["type"] in MERGE and len(ins) == 1 and len(outs) == 1 and len(sides) == 2:
            out.append({"kind": "单进单出汇流器", "node": n["id"], "c": n["c"], "layer": "pipe" if n["type"].startswith("管道") else "belt",
                        "note": "只用来降低这一路的优先级：主路满了/堵了才往这边走（4.7 优先级）"})
    # 准入口紧挨着分流器/汇流器（中间没有带子）
    for lk in res["links"]:
        if lk.get("b") is None or len(lk["path"]) != 2:
            continue
        a, b = nodes[lk["a"]], nodes[lk["b"]]
        types = {a["type"], b["type"]}
        if LG.GATE in types and types & set(SPLIT + MERGE):
            g, o = (a, b) if a["type"] == LG.GATE else (b, a)
            out.append({"kind": f"准入口紧挨{o['type']}", "node": o["id"], "gate": g["id"], "c": o["c"], "layer": lk["layer"]})
    # 分流回环：有向图里经过分流器的环
    adj = {}
    for a, b, lk in fl:
        if a[0] == "n" and b[0] == "n":
            adj.setdefault(a[1], []).append(b[1])
    seen = set()
    for s0 in [n["id"] for n in nodes if n["type"] in SPLIT]:
        stack = [(s0, [s0])]
        while stack:
            u, pth = stack.pop()
            for v in adj.get(u, []):
                if v == s0 and len(pth) >= 2:
                    key = frozenset(pth)
                    if key not in seen:
                        seen.add(key)
                        out.append({"kind": "分流回环", "nodes": pth, "c": nodes[s0]["c"],
                                    "types": [nodes[i]["type"] for i in pth],
                                    "note": "分流器分出去的一路绕回汇流器：进设备的那一路只拿到 1/n（4.7 分流回环）"})
                elif v not in pth and len(pth) < 8:
                    stack.append((v, pth + [v]))
    # 同一组回环（共用物流件）合成一条
    loops = [x for x in out if x["kind"] == "分流回环"]
    merged = []
    for lp in loops:
        for m in merged:
            if set(m["nodes"]) & set(lp["nodes"]):
                m["nodes"] = m["nodes"] + [i for i in lp["nodes"] if i not in m["nodes"]]
                m["types"] = [nodes[i]["type"] for i in m["nodes"]]
                m["loops"] += 1
                break
        else:
            merged.append(dict(lp, loops=1))
    out = [x for x in out if x["kind"] != "分流回环"] + merged
    # 设备的进料带/管数
    dev_in = {}
    for a, b, lk in fl:
        if b[0] == "d":
            dev_in.setdefault((b[1], lk["layer"]), []).append(a)
    for (dv, lay), srcs in dev_in.items():
        out.append({"kind": "设备进料", "device": dv, "layer": lay, "n": len(srcs),
                    "from": [nodes[x[1]]["type"] for x in srcs if x[0] == "n"]})
    return out


# ------------------------------------------------------------------ 项目入口
def run_base(bd: Path, ledger: dict | None = None) -> dict:
    """bd = 项目 base/<据点>/。写 flow.json、flow.jpg，返回结果。"""
    with _BUILD_LOCK:  # 连写 flow.json 一起排队：两次同时算同一个据点时，后写的是后算完的那份
        return _run_base(bd, ledger)


def _run_base(bd: Path, ledger: dict | None) -> dict:
    img = V.rd(bd / "composite.png")
    items = json.loads((bd / "logistics.json").read_text(encoding="utf-8"))
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    pts = []
    for dv in (ledger or {}).get("devices", []):
        cx, cy = V.far_to_canvas(tf, dv["x"], dv["y"])
        pts.append({"name": dv["name"], "c": [float(cx), float(cy)]})
    res = build(img, items, pts)
    atomic.write_json(bd / "flow.json", res)  # 原子写：设计分析、界面的据点图会读它（见 atomic.py）
    image(img, res).save(bd / "flow.jpg", quality=88)
    return res


def image(img, res):
    from PIL import Image, ImageDraw, ImageFont
    pil = Image.fromarray(cv2.cvtColor((img * 0.75 + 60).clip(0, 255).astype(np.uint8), cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    F = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 14)
    nodes = res["nodes"]
    for lk in res["links"]:
        pts = [tuple(p) for p in lk["path"]]
        col = (30, 110, 230) if lk["layer"] == "pipe" else (230, 140, 0)
        if lk["dir"] is None:
            col = (150, 150, 150)
        dr.line(pts, fill=col, width=4)
        if lk["dir"] and len(pts) >= 2:  # 每段中间画箭头
            seq = pts if lk["dir"] == "a->b" else pts[::-1]
            for p, q in zip(seq[:-1], seq[1:]):
                if np.hypot(q[0] - p[0], q[1] - p[1]) < 20:
                    continue
                mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
                ux, uy = (q[0] - p[0]), (q[1] - p[1]); ln = np.hypot(ux, uy); ux, uy = ux / ln, uy / ln
                dr.polygon([(mx + ux * 7, my + uy * 7), (mx - ux * 5 - uy * 6, my - uy * 5 + ux * 6), (mx - ux * 5 + uy * 6, my - uy * 5 - ux * 6)], fill=col)
        if lk.get("free"):
            x, y = pts[0]
            dr.rectangle([x - 5, y - 5, x + 5, y + 5], fill=(200, 0, 0) if not lk.get("a_dev") else (0, 150, 0))
        if lk["end"] == "device":
            x, y = pts[-1]
            dr.rectangle([x - 5, y - 5, x + 5, y + 5], fill=(200, 0, 0) if not lk.get("device") else (0, 150, 0))
        for j in lk.get("jumps", []):
            dr.ellipse([j[0] - 6, j[1] - 6, j[0] + 6, j[1] + 6], outline=(160, 0, 200), width=2)
    for n in nodes:
        x, y = n["c"]
        if "in" in n or "out" in n:
            s = n.get("in") or n.get("out")
            dx, dy = DIRS[s]
            col = (220, 0, 0) if "in" in n else (0, 160, 0)
            a, b = ((x + dx * 30, y + dy * 30), (x + dx * 14, y + dy * 14)) if "in" in n else ((x + dx * 14, y + dy * 14), (x + dx * 30, y + dy * 30))
            dr.line([a, b], fill=col, width=4)
            dr.ellipse([b[0] - 4, b[1] - 4, b[0] + 4, b[1] + 4], fill=col)
            if n.get("covered"):
                dr.rectangle([x - 18, y - 18, x + 18, y + 18], outline=(200, 0, 200), width=2)
    for it in res["issues"]:
        p = it.get("at") or nodes[it["a"]]["c"]
        dr.ellipse([p[0] - 22, p[1] - 22, p[0] + 22, p[1] + 22], outline=(230, 0, 0), width=3)
        dr.text((p[0] + 20, p[1] - 22), it["kind"], fill=(230, 0, 0), font=F)
    return pil
