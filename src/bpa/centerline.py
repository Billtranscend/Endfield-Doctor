"""沿带子/管子的中心线追踪（2026-11-09 第二版追踪，替换按格子一步步走的 flow.trace）。

为什么换：按格子走要先猜格中心，拼图格距会漂 6–12 px，弯道正好落在两格中间时拐不过去，
停在半路又因为附近有仓库口被当成「接设备」，不报（武陵城 (1358,536)、(2267,440)）；
两条线只隔 20 px 时按格子的「前后挪着找」会跳到另一条线上（应龙关 (1253,1580)）。

做法：
1. 线掩码：管子 = 深灰（闭运算补上小三角）再开运算去掉 1–3 px 的设备外框线；传送带 = 黄色，
   被上面的管子盖住的那一段用「黄—管子—黄」补上。物流件方块和一格设备的圆图标从掩码里挖掉。
2. Zhang-Suen 细化成一像素宽的中心线（不加依赖，numpy 实现）。
3. 从物流件某条边外面的中心线像素出发，一个像素一个像素走：岔口按「和前 8 px 走向最顺」选；
   走到头时看头前面是什么——同层物流件（到了；桥就从对面接着走）、圆图标（管子从底下穿过去：
   图标外圈找另一个出口）、都不是 → 接设备（末端走向就是设备那边）。
输出和 flow.trace 一样的 dict，flow.build / free_segments 不用改别的。
"""
from __future__ import annotations

import cv2
from paths import imread as _imread  # cv2.imread/imwrite 不认中文路径
import numpy as np

from . import logistics as LG
from . import vision as V

C = 34.75
DIRS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
BACK = {"E": "W", "W": "E", "N": "S", "S": "N"}
BRIDGES = ("物流桥", "管道桥")
NODE_HALF = 18  # 物流件方块半宽（挖掉的范围）
N8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]


def cardinal(v) -> str:
    x, y = float(v[0]), float(v[1])
    if abs(x) >= abs(y):
        return "E" if x >= 0 else "W"
    return "S" if y >= 0 else "N"


def _unit(v):
    v = np.asarray(v, float)
    n = float(np.hypot(*v))
    return v / n if n > 1e-6 else v


def thin(mask: np.ndarray, max_iter: int = 60) -> np.ndarray:
    """Zhang-Suen 细化。mask: bool → bool（一像素宽中心线）。"""
    img = np.pad(mask.astype(np.uint8), 1)
    for _ in range(max_iter):
        changed = False
        for step in (0, 1):
            P2 = img[:-2, 1:-1]; P3 = img[:-2, 2:]; P4 = img[1:-1, 2:]; P5 = img[2:, 2:]
            P6 = img[2:, 1:-1]; P7 = img[2:, :-2]; P8 = img[1:-1, :-2]; P9 = img[:-2, :-2]
            c = img[1:-1, 1:-1]
            nb = [P2, P3, P4, P5, P6, P7, P8, P9]
            B = sum(n.astype(np.int8) for n in nb)
            seq = nb + [P2]
            A = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.int8) for i in range(8))
            if step == 0:
                m1, m2 = P2 * P4 * P6, P4 * P6 * P8
            else:
                m1, m2 = P2 * P4 * P8, P2 * P6 * P8
            rm = (c == 1) & (B >= 2) & (B <= 6) & (A == 1) & (m1 == 0) & (m2 == 0)
            if rm.any():
                c[rm] = 0
                changed = True
        if not changed:
            break
    return img[1:-1, 1:-1].astype(bool)


_SL_TPL = None


def storage_line_icons(img: np.ndarray, th: float = 0.7) -> list[dict]:
    """「深色扁方块 + 白三角」= 仓库存取线基段的图标（用户 2026-11-09 游戏里确认）。管子从存取线上面跨过去，
    它不接管子；但它紧贴着管子、又和管子一样深，中心线会拐进去、或把它当成管子的尽头（应龙关 (1372,1005)、(1373,1548)）。
    → [{c, tpl(该方向的模板灰度), x0, y0}]，四个方向都找（武陵城是竖着的）。"""
    global _SL_TPL
    if _SL_TPL is None:
        t = _imread(str(V.RES / "storage_line_base.png"), cv2.IMREAD_GRAYSCALE)
        _SL_TPL = [] if t is None else [t, cv2.rotate(t, cv2.ROTATE_180), cv2.rotate(t, cv2.ROTATE_90_CLOCKWISE),
                                        cv2.rotate(t, cv2.ROTATE_90_COUNTERCLOCKWISE)]
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    out = []
    for T in _SL_TPL:
        r = cv2.matchTemplate(g, T, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.nonzero(r >= th)
        for x, y in sorted(zip(xs, ys), key=lambda q: -r[q[1], q[0]]):
            c = (x + T.shape[1] / 2, y + T.shape[0] / 2)
            if all(abs(c[0] - o["c"][0]) > 16 or abs(c[1] - o["c"][1]) > 16 for o in out):
                out.append({"c": [float(c[0]), float(c[1])], "x0": int(x), "y0": int(y), "tpl": T})
    return out


def line_masks(M: dict, nodes: list[dict], disks: list) -> dict:
    """→ {belt, pipe}: 去掉物流件方块、圆图标后的线掩码（bool）。"""
    k3, k5 = np.ones((3, 3), np.uint8), np.ones((5, 5), np.uint8)
    # 半透明浅灰叠层底下的管子变浅（绝对阈值认不出）：用「比 25 px 内最亮底色暗 ≥55」的 pipe_soft
    pipe = cv2.morphologyEx(M.get("pipe_soft", M["pipe"]).astype(np.uint8), cv2.MORPH_CLOSE, k3)
    pipe = cv2.morphologyEx(pipe, cv2.MORPH_OPEN, k5).astype(bool)
    yel = M["belt"].astype(np.uint8)
    # 传送带被上面横穿的管子盖住：沿带子方向闭运算补出来的缺口里是管子的深色，才补（并排两条带子之间是浅色底，不补）
    gap_h = cv2.morphologyEx(yel, cv2.MORPH_CLOSE, np.ones((1, 17), np.uint8)).astype(bool)
    gap_v = cv2.morphologyEx(yel, cv2.MORPH_CLOSE, np.ones((17, 1), np.uint8)).astype(bool)
    belt = yel.astype(bool) | ((gap_h | gap_v) & M["pipe"])
    belt = cv2.morphologyEx(belt.astype(np.uint8), cv2.MORPH_CLOSE, k5)
    belt = cv2.morphologyEx(belt, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8)).astype(bool)
    H, W = pipe.shape
    for o in M.get("storage_line", []):  # 存取线基段图标：只挖它自己深色的那部分（紧贴着的管子留着）
        T = o["tpl"]
        m = 3  # 匹配位置会差 1–2 px，图标上沿那条深色横线也要挖掉（应龙关 (1373,1548) 中心线顺着它拐进去）
        dark = np.pad((T < 120).astype(np.uint8), m)
        dark = cv2.dilate(dark, np.ones((7, 7), np.uint8)).astype(bool)
        y0, x0 = o["y0"] - m, o["x0"] - m
        ya, xa = max(0, y0), max(0, x0)
        yb, xb = min(H, y0 + dark.shape[0]), min(W, x0 + dark.shape[1])
        before = pipe[ya:yb, xa:xb].copy()
        pipe[ya:yb, xa:xb] &= ~dark[ya - y0:yb - y0, xa - x0:xb - x0]
        # 管子直着从图标上穿过去（武陵城 (1000,538) 那条出水管连穿几个基段图标）：图标框左右（或上下）两边同一行（列）都有管子 → 这一行（列）补回来
        L, Rr = max(0, xa - 3), min(W - 1, xb + 2)
        T, B = max(0, ya - 3), min(H - 1, yb + 2)
        for yy_ in range(ya, yb):
            if pipe[yy_, L] and pipe[yy_, Rr]:
                pipe[yy_, xa:xb] |= before[yy_ - ya] | True
        for xx_ in range(xa, xb):
            if pipe[T, xx_] and pipe[B, xx_]:
                pipe[ya:yb, xx_] = True
    for n in nodes:
        x, y = int(round(n["c"][0])), int(round(n["c"][1]))
        sl = (slice(max(0, y - NODE_HALF), y + NODE_HALF + 1), slice(max(0, x - NODE_HALF), x + NODE_HALF + 1))
        pipe[sl] = False
        belt[sl] = False
    if disks:
        yy, xx = np.mgrid[0:H, 0:W]
        for x, y, r in disks:
            R = int(r) + 3
            x0, y0 = int(x), int(y)
            win = (slice(max(0, y0 - R), y0 + R + 1), slice(max(0, x0 - R), x0 + R + 1))
            pipe[win] &= (xx[win] - x) ** 2 + (yy[win] - y) ** 2 > R ** 2
    return {"pipe": pipe, "belt": belt}


def prepare(M: dict, nodes: list[dict]) -> None:
    """在 M 里放好中心线（flow.build 在认出圆图标之后调用一次）。"""
    if "storage_line" not in M and "img" in M:
        M["storage_line"] = storage_line_icons(M["img"])
    L = line_masks(M, nodes, M.get("disks", []))
    M["cl_mask"] = L
    M["cl"] = {k: thin(v) for k, v in L.items()}


# ------------------------------------------------------------------ 走
def _nodes_hit(nodes, p, v, me=None, reach=12):
    """末端 p 沿走向 v 往前 reach px 内碰到的物流件方块。"""
    for k in range(0, reach + 1, 2):
        q = p + v * k
        for n in nodes:
            if n is me:
                continue
            if abs(q[0] - n["c"][0]) <= NODE_HALF + 3 and abs(q[1] - n["c"][1]) <= NODE_HALF + 3:
                return n
    return None


def _disk_hit(disks, p, v, reach=12):
    for k in range(0, reach + 1, 2):
        q = p + v * k
        for d in disks:
            if np.hypot(q[0] - d[0], q[1] - d[1]) <= d[2] + 5:
                return d
    return None


def _next_node(nodes, c, d, layer, me=None):
    """c（物流件中心）的 d 边紧挨着的同层物流件（格距误差：沿方向 ±20、横向 ±10）。"""
    q = np.asarray(c, float) + np.array(DIRS[d], float) * C
    for n in nodes:
        if n is me:
            continue
        ex, ey = abs(n["c"][0] - q[0]), abs(n["c"][1] - q[1])
        along, across = (ex, ey) if d in ("E", "W") else (ey, ex)
        if along <= 20 and across <= 10 and _layer_fits(n, layer):
            return n
    return None


def _seed(S, P, v, t0=2, t1=16, lat=9):
    """P 往 v 方向 t0..t1 px、横向 ±lat 内最近的中心线像素（先近后偏）。"""
    H, W = S.shape
    vx, vy = -v[1], v[0]
    best = None
    for t in range(t0, t1 + 1):
        for l in sorted(range(-lat, lat + 1), key=abs):
            x, y = int(round(P[0] + v[0] * t + vx * l)), int(round(P[1] + v[1] * t + vy * l))
            if 0 <= x < W and 0 <= y < H and S[y, x]:
                return np.array([x, y], float)
        if best is not None:
            break
    return best


def _layer_fits(n, layer):
    t = n["type"]
    if t == LG.GATE:
        k = n.get("kind")
        return k in (None, "?") or (k == "管道") == (layer == "pipe")
    return (layer == "pipe") == t.startswith("管道")


def _simplify(px: list, nodes_pts: dict) -> list:
    """像素路径 → 折线拐点（给小三角投票用：直段要贴着横/竖）。nodes_pts: 下标 → 物流件中心（插在那里）。"""
    if len(px) < 2:
        return [np.asarray(p, float) for p in px]
    cnt = np.array(px, np.int32).reshape(-1, 1, 2)
    ap = cv2.approxPolyDP(cnt, 2.5, False).reshape(-1, 2)
    return [np.asarray(p, float) for p in ap]


def _seed_across(S, nodes, n, d, layer, max_hops=3):
    """从物流件 n 的 d 边往外找中心线；那边紧挨着的是另一层的物流件（管子从上面跨过去，方块在掩码里被挖掉了）→ 穿过它接着找。
    返回 (落点, 穿过的物流件列表)。2026-11-10 武陵城：扩容反应池的锦草溶液管从物流桥 #81 上拐弯，西边紧挨着传送带汇流器 #84。"""
    v = np.array(DIRS[d], float)
    cur, hop = n, []
    for _ in range(max_hops + 1):
        q = _seed(S, np.asarray(cur["c"], float), v, t0=NODE_HALF - 4, t1=NODE_HALF + 12)
        if q is not None:
            return q, hop
        c = np.asarray(cur["c"], float)
        nxt = None
        for m in nodes:
            if m is cur or m is n or _layer_fits(m, layer) and m["type"] not in BRIDGES:
                continue
            dv = np.asarray(m["c"], float) - c
            along, lat = float(dv @ v), abs(float(dv[0] * -v[1] + dv[1] * v[0]))
            if 2 * NODE_HALF - 8 <= along <= 2 * NODE_HALF + 10 and lat <= 8:
                nxt = m
                break
        if nxt is None or (layer == "pipe") == nxt["type"].startswith("管道"):
            return None, []
        hop.append(nxt)
        cur = nxt
    return None, []


def walk(M, nodes, start, d, layer, maxlen=30000) -> dict:
    """从 start（物流件或 {"type": "seed", "c": 点}）的 d 边沿中心线走到头。返回同 flow.trace。"""
    S = M["cl"][layer]
    disks = M.get("disks", []) if layer == "pipe" else []
    H, W = S.shape
    v = np.array(DIRS[d], float)
    P0 = np.asarray(start["c"], float)
    is_seed = start.get("type") == "seed"
    out_path = [P0.copy()]
    jumps = []
    cur = start
    while not is_seed:  # 物流件挨着物流件（中间没有带子/管子）：直接相连；挨着的是桥就穿过去接着看
        m = _next_node(nodes, cur["c"], d, layer, me=cur)
        if m is None:
            break
        out_path.append(np.asarray(m["c"], float))
        if m["type"] not in BRIDGES:
            return {"end": "node", "node": m, "side": BACK[d], "path": out_path, "d": d, "jumps": jumps, "adj": True}
        cur = m
    P0 = np.asarray(cur["c"], float)
    p = _seed(S, P0, v, t0=-4 if is_seed else NODE_HALF - 4, t1=6 if is_seed else NODE_HALF + 12)
    if p is None and not is_seed:
        # 管子从隔壁那格的中心才开始画（管子头和物流件之间留了半格白）：格子上是相邻的，游戏里相邻就连上。
        # 只认正对着、顺着同方向继续伸出去的管子（2026-11-09 武陵城分流器 #65 (1561,1736) 往东 → 种植机 (1795,1840)，作者确认它有水）
        q = _seed(S, P0, v, t0=NODE_HALF + 13, t1=NODE_HALF + 20, lat=3)
        if q is not None and _seed(S, q, v, t0=6, t1=10, lat=3) is not None:
            p = q
    if p is None:
        return {"end": "device", "path": out_path, "d": d, "jumps": jumps, "tip": True, "note": "边外没有中心线"}
    seg = [p.copy()]
    visited = {(int(p[0]), int(p[1]))}
    prev = [p.copy()]
    total = 0
    me = None if is_seed else start
    leave_me = 0  # 刚出发时可能还贴着自己
    while total < maxlen:
        x, y = int(p[0]), int(p[1])
        back = prev[-2] if len(prev) >= 2 else None
        cands = []
        for dx, dy in N8:
            q = (x + dx, y + dy)
            if not (0 <= q[0] < W and 0 <= q[1] < H) or not S[q[1], q[0]] or q in visited:
                continue
            if back is not None and max(abs(q[0] - back[0]), abs(q[1] - back[1])) <= 1:
                continue
            cands.append(q)
        ref = prev[-9] if len(prev) >= 9 else prev[0] - v * (10 - len(prev))
        dirv = _unit(p - ref) if np.hypot(*(p - ref)) > 1 else v
        if cands:
            score = lambda q: float(np.dot(_unit(np.array(q, float) - p), dirv)) - 0.05 * (abs(q[0] - x) + abs(q[1] - y) - 1)
            q = max(cands, key=score)
            fwd = [c for c in cands if float(np.dot(_unit(np.array(c, float) - p), dirv)) > -0.3]
            if fwd:  # 不掉头：从管子中间的种子往两头追时，往回那一侧不是岔路（2026-11-09 应龙关 (711,508) 往西追成了往东）
                cands = fwd
                q = max(cands, key=score)
            if len(cands) > 1:  # 岔口（两条线挨着、或贴着管子的深色小方块）：每个候选往前探 40 px，死胡同短的不选
                lens = [(_reach(S, c, (x, y), visited | set(cands) - {c}), c) for c in cands]
                longest = max(l for l, _ in lens)
                pool = [c for l, c in lens if l >= min(30, longest)]
                q = max(pool, key=score)
            for c in cands:  # 同一步的旁支都记成走过，别从岔口绕回来
                visited.add(c)
            p = np.array(q, float)
            visited.add(q)
            prev.append(p.copy()); seg.append(p.copy())
            total += 1
            leave_me += 1
            continue
        # 走到头：前面是什么
        v_end = dirv
        n = _nodes_hit(nodes, p, v_end, me=me if leave_me < 40 else None)
        if n is not None and n is start and leave_me < 40:
            n = None
        if n is not None:
            side = cardinal(p - np.asarray(n["c"], float))
            if n["type"] in BRIDGES or not _layer_fits(n, layer):
                # 桥：从对面接着走；另一层的物流件：管子从上面跨过去，同样从对面接着走
                dd = BACK[side]
                if n["type"] in BRIDGES:  # 桥对面直接挨着物流件
                    b, chain = n, [np.asarray(n["c"], float)]
                    while True:
                        m = _next_node(nodes, b["c"], dd, layer, me=b)
                        if m is None or m["type"] not in BRIDGES:
                            break
                        chain.append(np.asarray(m["c"], float)); b = m
                    if m is not None:
                        out_path += _simplify(seg, {}) + chain + [np.asarray(m["c"], float)]
                        return {"end": "node", "node": m, "side": BACK[dd], "path": out_path, "d": dd, "jumps": jumps}
                    n = b
                q, hop = _seed_across(S, nodes, n, dd, layer)
                if q is None and not _layer_fits(n, layer):
                    # 管子在另一层的物流件上面拐弯（武陵城惰气管在赤铜耐压罐物品准入口 (853,780) 上面往东拐去塑形机，2026-11-09）
                    turns = []
                    for e in DIRS:
                        if e in (dd, side):
                            continue
                        qq, hh = _seed_across(S, nodes, n, e, layer)
                        if qq is not None and (int(qq[0]), int(qq[1])) not in visited:
                            turns.append((e, qq, hh))
                    if len(turns) == 1:
                        dd, q, hop = turns[0]
                if q is not None and (n["type"] in BRIDGES or not _layer_fits(n, layer)):
                    out_path += _simplify(seg, {}) + [np.asarray(n["c"], float)] + [np.asarray(h["c"], float) for h in hop]
                    if not _layer_fits(n, layer):
                        jumps.append([int(n["c"][0]), int(n["c"][1])])
                    for h in hop:
                        jumps.append([int(h["c"][0]), int(h["c"][1])])
                    seg = [q.copy()]; p = q; prev = [q.copy()]; v = np.array(DIRS[dd], float)  # dd：直穿或拐弯后的方向
                    visited.add((int(q[0]), int(q[1])))
                    me = n; leave_me = 0
                    continue
                if not _layer_fits(n, layer):
                    out_path += _simplify(seg, {})
                    return {"end": "device", "path": out_path, "d": cardinal(v_end), "jumps": jumps, "tip": True,
                            "note": f"撞上另一层的{n['type']}"}
            if n["type"] not in BRIDGES:
                out_path += _simplify(seg, {}) + [np.asarray(n["c"], float)]
                return {"end": "node", "node": n, "side": side, "path": out_path, "d": BACK[side], "jumps": jumps}
        dk = _disk_hit(disks, p, v_end)
        if dk is not None:
            q, dd = _disk_exit(S, dk, p, v_end, visited)
            if q is not None:
                out_path += _simplify(seg, {}) + [np.array(dk[:2], float)]
                jumps.append([int(dk[0]), int(dk[1])])
                seg = [q.copy()]; p = q
                prev = [q.copy()]; v = np.asarray(dd, float)
                visited.add((int(q[0]), int(q[1])))
                continue
            # 图标外圈找不到出口：图标紧挨着一个同层物流件（准入口/分流器方块把外圈那段管子挖掉了）→ 管子是从图标底下拐进这个物流件的
            # （2026-11-09 应龙关 (1287,950) 仓库口图标正上方就是液化息壤准入口 (1288,919)）
            a_in = _unit(p - np.array(dk[:2], float))
            near = [n for n in nodes if n is not start and _layer_fits(n, layer) and n["type"] not in BRIDGES
                    and np.hypot(n["c"][0] - dk[0], n["c"][1] - dk[1]) <= dk[2] + NODE_HALF + 6
                    and float(np.dot(_unit(np.asarray(n["c"], float) - dk[:2]), a_in)) < 0.5]
            if len(near) == 1:
                n = near[0]
                out_path += _simplify(seg, {}) + [np.array(dk[:2], float), np.asarray(n["c"], float)]
                jumps.append([int(dk[0]), int(dk[1])])
                side = cardinal(np.array(dk[:2], float) - np.asarray(n["c"], float))
                return {"end": "node", "node": n, "side": side, "path": out_path, "d": BACK[side], "jumps": jumps}
            out_path += _simplify(seg, {})
            return {"end": "device", "path": out_path, "d": cardinal(v_end), "jumps": jumps, "tip": True, "disk": list(dk)}
        q = _gap_ahead(S, p, v_end, visited, nodes, M.get("gray"))
        if q is not None:  # 直管被半透明浅灰叠层整段盖住（灰度和叠层一样，掩码救不回来）：同一直线前方还有管子 → 跨过去
            jumps.append([int(p[0]), int(p[1])])
            seg.append(q.copy()); p = q; prev.append(q.copy())
            visited.add((int(q[0]), int(q[1])))
            continue
        out_path += _simplify(seg, {})
        return {"end": "device", "path": out_path, "d": cardinal(v_end), "jumps": jumps, "tip": True}
    out_path += _simplify(seg, {})
    return {"end": "long", "path": out_path, "d": cardinal(v), "jumps": jumps}


def _comp_len(S, x, y, R=15) -> int:
    """(x, y) 所在那段中心线在 ±R 窗口里有多少像素（连通块大小）。"""
    H, W = S.shape
    x0, y0 = max(0, x - R), max(0, y - R)
    win = S[y0:min(H, y + R + 1), x0:min(W, x + R + 1)].astype(np.uint8)
    n, lab = cv2.connectedComponents(win, connectivity=8)
    k = lab[y - y0, x - x0]
    return int((lab == k).sum()) if k else 0


def _gap_ahead(S, p, v, visited, nodes, gray=None, gmin=3, gmax=90):
    """管子走到头时沿原方向（只认横/竖）往前 gmin..gmax px、横向 ±2 找中心线，且接着还能往前走 ≥10 px → 返回落点。
    中间不能跨过物流件。2026-11-09 武陵城 (1000,538) 出水管从两条浅灰竖条底下穿过，有两段完全看不见。"""
    c = cardinal(v)
    vv = np.array(DIRS[c], float)
    H, W = S.shape
    for k in range(gmin, gmax + 1):
        # 短缺口（≤20 px）横向放宽到 ±4：拐弯刚出来就被浅灰方块盖住，弯道末端和接着的直管错开 3 px
        # （2026-11-09 武陵城分流器 #100 (1849,611) 往北拐西，去 3 台天有洪炉的那根，作者确认有水）
        for l in ((0, -1, 1, -2, 2, -3, 3, -4, 4) if k <= 20 else (0, -1, 1, -2, 2)):
            q = p + vv * k + np.array([-vv[1], vv[0]]) * l
            x, y = int(round(q[0])), int(round(q[1]))
            if not (0 <= x < W and 0 <= y < H) or not S[y, x] or (x, y) in visited:
                continue
            mid = p + vv * (k / 2)
            if any(abs(mid[0] - n["c"][0]) <= NODE_HALF and abs(mid[1] - n["c"][1]) <= NODE_HALF for n in nodes):
                return None
            if k > 26:  # 长缺口只跨「浅灰叠层」：缺口那段 ≥80% 不是白底（白底 ~230）
                if gray is None:
                    return None
                gs = [int(gray[int(round(p[1] + vv[1] * t)), int(round(p[0] + vv[0] * t))]) for t in range(2, k - 1)]
                if not gs or sum(g_ <= 222 for g_ in gs) < 0.8 * len(gs):  # 缺口那段不是空白地面（叠层/露出来的管子片段）
                    continue
            # 接着往前还有一段同方向的中心线
            ok = sum(1 for t in range(2, 12) if 0 <= int(round(y + vv[1] * t)) < H and 0 <= int(round(x + vv[0] * t)) < W
                     and S[int(round(y + vv[1] * t)) - 1:int(round(y + vv[1] * t)) + 2, int(round(x + vv[0] * t)) - 1:int(round(x + vv[0] * t)) + 2].any())
            if ok >= 8:
                return np.array([x, y], float)
            if k <= 20 and _comp_len(S, x, y) >= 14:  # 短缺口后面紧接着拐弯（反着追同一根管子）：落点那段中心线够长就算
                return np.array([x, y], float)
    return None


def _branches(cands):
    """同一步的候选像素按互相挨着分组（一条线的阶梯会给出两个挨着的候选，不算岔口）。"""
    groups = []
    for c in cands:
        for g in groups:
            if any(max(abs(c[0] - o[0]), abs(c[1] - o[1])) <= 1 for o in g):
                g.append(c); break
        else:
            groups.append([c])
    return groups


def _reach(S, q0, frm, visited, limit=40) -> int:
    """从 q0 沿中心线贪心往前走（不回头），能走多少 px（到 limit 为止）。"""
    H, W = S.shape
    seen = {frm, q0}
    q, n = q0, 0
    while n < limit:
        nxt = None
        for dx, dy in N8:
            r = (q[0] + dx, q[1] + dy)
            if 0 <= r[0] < W and 0 <= r[1] < H and S[r[1], r[0]] and r not in seen and r not in visited:
                nxt = r; break
        if nxt is None:
            break
        for dx, dy in N8:  # 把这一步的邻居都记上，别在阶梯上原地打转
            seen.add((q[0] + dx, q[1] + dy))
        q = nxt; n += 1
    return n


def _disk_exit(S, dk, p_in, v_in, visited):
    """管子钻进圆图标底下：图标外圈（r+3…r+14）里的中心线像素按连通块分组，去掉进来那块；
    剩一块 → 从那块离圆心最近的像素接着走；好几块 → 取和进来方向最顺的（直穿 > 拐弯），但要明显更顺。"""
    cx, cy, r = dk
    H, W = S.shape
    R1 = int(r) + 14
    x0, y0 = max(0, int(cx) - R1), max(0, int(cy) - R1)
    win = S[y0:int(cy) + R1 + 1, x0:int(cx) + R1 + 1].copy()
    yy, xx = np.mgrid[y0:y0 + win.shape[0], x0:x0 + win.shape[1]]
    dist = np.hypot(xx - cx, yy - cy)
    win &= (dist >= r + 3) & (dist <= R1)
    n, lab = cv2.connectedComponents(win.astype(np.uint8), connectivity=8)
    a_in = _unit(np.asarray(p_in, float) - [cx, cy])
    cands = []
    for k in range(1, n):
        ys, xs = np.nonzero(lab == k)
        pts = np.stack([xs + x0, ys + y0], 1).astype(float)
        rad = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        if len(pts) < 3 or rad.max() - rad.min() < 2:  # 零星几个像素、或贴着图标边转的一小段（图标边缘细化剩下的），不是伸出去的管子
            continue
        if any((int(a), int(b)) in visited for a, b in pts):
            continue
        cen = pts.mean(0)
        a = _unit(cen - [cx, cy])
        if float(np.dot(a, a_in)) > 0.5:  # 进来那边
            continue
        i = int(np.argmin(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)))
        if len(pts) >= 4:  # 真出口是从图标往外伸的（走向沿半径）；从旁边擦过去的管子走向绕着图标（切向）→ 不算
            ctr = pts - pts.mean(0)
            w_, vecs = np.linalg.eigh(ctr.T @ ctr)
            axis = vecs[:, -1]
            if w_[-1] > 4 * max(w_[0], 1e-6) and abs(float(np.dot(axis, a))) < 0.5:
                continue
        q0 = (int(pts[i][0]), int(pts[i][1]))
        ln = _reach(S, q0, q0, set(), limit=40)  # 真管子从图标边伸出去一大截；图标外框角上细化剩下的小勾走不远
        cands.append((float(np.dot(a, v_in)), pts[i], a, ln))
    long_ = [c for c in cands if c[3] >= 25]
    cands = long_ or cands
    if not cands:
        return None, None
    cands.sort(key=lambda c: -c[0])
    if len(cands) > 1 and cands[0][0] - cands[1][0] < 0.3:
        return None, None
    return cands[0][1], cands[0][2]
