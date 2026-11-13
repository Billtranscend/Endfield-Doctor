"""远景底账扫描：把一个据点里每台设备的 {名字, 最远缩放坐标} 扫出来（data/blueprints/<档案>/ledger/<据点>.json）。
据点拼合（把蓝图拼到基地上）靠它找锚点；巡检也用它。docs/蓝图分析器-设计.md 第 4.5 节、docs/设备识别方案.md。

三步：
  scan()        游戏内：俯瞰，顶到地图左上角（最远缩放）→ 滚入 7 格（标签显示全名）→ 蛇形扫完整个据点，每屏存图，记近景镜头位置。
                只按方向键、滚轮（进俯瞰时 CapsLock）。不点任何东西，不按 Z（建造目录）。
  recognize()   离线：每屏 OCR 标签全名 + 标签左边的类型图标核对 → 换算回最远坐标 → 多屏合并 → 去掉只见一次的误识别。
  save_ledger() 写成底账文件。规划线（协议核心边界）可选：给了就把线外的设备（野外水泵等）单列，不进底账。

来源：scratchpad 的 ledger_scan.py / ledger_offline.py / make_ledger.py（2026-11-07 皇帝产线三个据点实测，见 docs/P3-交接.md）。
和原脚本的区别：据点范围（extent）可不给（按「画面不动 / 没有标签」判到头）；去掉了皇帝产线专用的真值对账和历史巡检对照。

换算：近景屏的点 u，镜头位移 cam（近景像素，从近景左上角起），近景角落相对「最远角落滚入 7 格」那一屏的内容位移 dd：
  q = u + cam − dd，最远坐标 = N2F · q（N2F：用两张角落截图配准出的「近景 → 最远」相似变换）。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

from paths import ROOT

TICKS = 7  # 滚入几格：标签显示全名的一档
ZOOM_NEAR = 1.73  # 滚入 TICKS 格后相对最远缩放的放大倍数（实测 1.728–1.731）
C = np.array([640.0, 330.0])  # 滚轮缩放中心
ROI = [260, 80, 840, 460]  # 去掉左侧任务栏/小地图、右侧菜单、底部建造栏
LROI = [0, 260, 600, 280]  # 左下：任务栏文字在 y 245 以上，下面干净；宽到 600 让跨 260 的标签完整
CW_RATIO = 0.82  # 近景标签每个字宽 ≈ 0.8 × 字高
DEFAULT_EXTENT = (2300, 2200)  # 档案里没登记据点范围时的上限（最远坐标），到头靠「画面不动 / 没标签」判
EXTRA_NAMES = {"物品准入口", "管道准入口", "仓库取货口", "仓库存货口", "仓库存取线基段", "暗管入口", "暗管出口", "多口暗管入口", "多口暗管出口"}
# 图标中心到 OCR 框左端的距离（× 字高，字高封顶 30），三区扫描 312 个干净标签实测；n ≥ 8 的用本类中位，其余 0.69
ICON_OFF = {"仓库取货口": 0.731, "粉碎机": 0.655, "协议储存箱": 0.794, "精炼炉": 0.569, "暗管入口": 0.638, "暗管出口": 0.638,
            "固气转化机": 0.730, "种植机": 0.741, "配件机": 0.704, "采种机": 0.722, "封装机": 0.685, "灌装机": 0.741, "研磨机": 0.765, "息壤供电桩": 0.808}

_MASK = np.zeros((720, 1280), np.uint8)
_MASK[80:540, 260:1100] = 255


def _orb():
    return cv2.ORB_create(3000), cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)


def similarity(a, b):
    """b 相对 a 的相似变换：(缩放, 平移 dx, dy, 内点数)。平移是「画面内容」的位移。"""
    orb, bf = _orb()
    ka, da = orb.detectAndCompute(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), _MASK)
    kb, db = orb.detectAndCompute(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), _MASK)
    if da is None or db is None:
        return None
    m = sorted(bf.match(da, db), key=lambda x: x.distance)[:400]
    if len(m) < 20:
        return None
    src = np.float32([ka[x.queryIdx].pt for x in m])
    dst = np.float32([kb[x.trainIdx].pt for x in m])
    M, inl = cv2.estimateAffinePartial2D(src, dst, ransacReprojThreshold=4)
    if M is None:
        return None
    return float(np.hypot(M[0, 0], M[1, 0])), float(M[0, 2]), float(M[1, 2]), int(inl.sum())


_EFF = (260, 80, 1100, 540)  # 有效区（x0, y0, x1, y1）：同 _MASK


def block_shift(a, b, expect=None, radius: int = 60):
    """b 相对 a 的「镜头位移」（近景像素，= −画面内容位移）：a 切 160×160 的块，在 b 的有效区里模板匹配，
    分 ≥ 0.8 且峰值唯一（第二峰低 0.08 以上，防止一排一样的机器错一个周期），≥ 2 块一致（±3 px）才算数。
    比 ORB 相似变换可靠：2026-11-08 景玉谷实测 ORB 量错 4 步（43 记成… 实为 248 等），块匹配给出结果的 0 错。
    返回 (dx, dy, 一致块数) 或 None（空地 / 草地上没有纹理时会测不出）。
    expect=(dx, dy)：只在预期镜头位移 ±radius 里找。一列上下重复的机器（应龙关右边一列「仓库取货口 + 固气转化机」，
    约 200 px 一组）在真位置移出画面时，错一组的那个峰会变成「唯一」的，2026-11-08 把 249 量成 44；给了预期就搜不到它。"""
    X0, Y0, X1, Y1 = _EFF
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    reg = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)[Y0:Y1, X0:X1]
    res = []
    for qx in range(X0, X1 - 160 + 1, 113):
        for qy in (Y0, Y0 + 150, Y1 - 160):
            t = ga[qy:qy + 160, qx:qx + 160]
            if t.std() < 18:
                continue
            ox = oy = 0
            sub = reg
            if expect is not None:  # 这块在 b 里的预期左上角（有效区坐标）= q − 预期镜头位移 − 有效区原点
                ex, ey = int(round(qx - expect[0] - X0)), int(round(qy - expect[1] - Y0))
                ox, oy = max(0, ex - radius), max(0, ey - radius)
                sub = reg[oy:min(reg.shape[0], ey + 160 + radius), ox:min(reg.shape[1], ex + 160 + radius)]
                if sub.shape[0] < 160 or sub.shape[1] < 160:
                    continue
            r = cv2.matchTemplate(sub, t, cv2.TM_CCOEFF_NORMED)
            _, s1, _, (x, y) = cv2.minMaxLoc(r)
            if s1 < 0.8:
                continue
            r[max(0, y - 25):y + 26, max(0, x - 25):x + 26] = -1
            if r.size and s1 - r.max() < 0.08:
                continue
            res.append((x + ox + X0 - qx, y + oy + Y0 - qy))
    if len(res) < 2:
        return None
    v = np.array(res, float)
    ok = np.abs(v - np.median(v, axis=0)).max(axis=1) <= 3
    if ok.sum() < 2:
        return None
    return float(-v[ok, 0].mean()), float(-v[ok, 1].mean()), int(ok.sum())


STILL_DIFF = 6.0  # 有效区平均灰度差小于这个 = 画面没变（按了方向键镜头没动：顶到边了）
PAN_SPEED = 711.0  # 俯瞰里按住方向键，镜头每秒走多少近景像素（滚入 7 格时；11-07 至 11-13 五次实测 710–717，横竖一样）


def frame_diff(a, b) -> float:
    """两屏有效区的平均灰度差。"""
    X0, Y0, X1, Y1 = _EFF
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)[Y0:Y1, X0:X1].astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)[Y0:Y1, X0:X1].astype(np.float32)
    return float(np.abs(ga - gb).mean())


def step_shift(prev, cur, axis: int, sign: int, typical: float):
    """按了一步方向键以后，镜头沿 axis（0 横、1 竖）走了多少 → (位移, how, sure, alt)。
    sign：这一步该往哪边（+1 右 / 下，−1 左）；typical：正常一步多远（正数，按键时长固定所以是已知的）。
    how：still（画面没变）/ block / orb / nominal（量不出，或量出来不像一步按键：按正常步长暂记）。
    sure=False 只跟着 nominal：这一步可能是整步（重复结构上量不出），也可能是顶到边的半步，
    调用方再按一步看画面动不动才知道。alt：另一种读法（没有就 None）——
    nominal 时是 ORB 量到的同方向小步；block 量到整步时是「不给预期的块匹配量到的同方向小步」：
    按预期量到整步、不给预期却量到小步，两个里有一个错了周期（应龙关 11-08 是小步错：249 量成 44；
    顶到边的半步旁边有两台一样的机器时是整步错），也要看下一步画面停不停。

    只收「像一步按键」的测量：另一个方向 < 40 px，本方向同号且在正常步长的 0.6–1.4 倍里。
    2026-11-13 issue #1：一位作者的武陵城一排排一样的精炼炉（横向约 205 px 一台），块匹配峰不唯一没结果，
    退到 ORB 量成 −20（= 390 − 2×205）；第一次下移被右边一列水泵（约 171 px 一台）量成 −262（= 250 − 3×171）。
    原来把「ORB 量到的步长 < 正常的 30%」直接当顶到边收行，下移连正负号都不查：13 行里 4 行没拍完，第 0 行和其余各行差 512 px。
    顺序：块匹配 → 按正常步长当预期的块匹配（±80）→ 块匹配量到的同方向小步（峰唯一，是顶到边的半步）→ ORB（只收整步）。"""
    if frame_diff(prev, cur) < STILL_DIFF:
        return 0.0, "still", True, None
    o = 1 - axis

    def kind(m):
        if m is None or abs(m[o]) >= 40:
            return "bad"
        v = sign * m[axis]
        return "full" if 0.6 * typical <= v <= 1.4 * typical else "part" if -3 <= v < 0.6 * typical else "bad"

    m = block_shift(prev, cur)
    k = kind(m)
    if k == "full":
        return float(m[axis]), "block", True, None
    exp = [0.0, 0.0]
    exp[axis] = sign * typical
    m2 = block_shift(prev, cur, expect=tuple(exp), radius=80)
    if kind(m2) == "full":
        return float(m2[axis]), "block", True, (float(m[axis]) if k == "part" else None)
    if k == "part":
        return float(m[axis]), "block", True, None
    r = similarity(prev, cur)
    alt = None
    if r and r[3] >= 30:
        mo = (-r[1], -r[2])
        ko = kind(mo)
        if ko == "full":
            return float(mo[axis]), "orb", True, None
        if ko == "part":
            alt = float(mo[axis])
    return float(sign * typical), "nominal", False, alt


REFINE_TOL = 12  # 离线重算：块匹配和扫描时记的步长差多少 px 以上才改（11-07 三次旧扫描里最大差 9 px，量错的都差 85 px 以上）


def refine_positions(d: Path, log=print) -> dict:
    """离线复核每屏的镜头位置（全局最小二乘）：
    1) 相邻两屏用 block_shift 重量一步，和扫描时记的差 > REFINE_TOL 就先改过来（给第 2 步一个好的初值）；
    2) 所有有重叠的两屏（相邻的、上下行的）都用 block_shift 量相对位置，和扫描时记的步长（测不出时的弱约束）一起
       解最小二乘，第一屏固定；残差 > 5 px 的测量当离群剔掉再解（最多 3 轮）。
       2026-11-08 景玉谷实测：一步纵向两种办法都没测出、按中位 258 补了（实为 248），只有上下行重叠能发现。
    3) 重定 dd（近景角落那屏在全局近景坐标里的偏移，同原来手动跑的 fix_dd.py）。
    原值存 cam_near_raw / corner_recheck_raw，写回 scan.json；已经复核过（refined）的不再做。"""
    d = Path(d)
    from offline_ocr import imread
    sp = d / "scan.json"
    rec = json.loads(sp.read_text(encoding="utf-8"))
    if rec.get("refined"):
        return rec
    sc = rec["screens"]
    N = len(sc)
    imgs = {x["file"]: imread(d / "near" / x["file"]) for x in sc}
    raw = [np.array(x["cam_near"], float) for x in sc]
    fixes, off, pos = [], np.zeros(2), []
    edges = []  # (i, j, P_j − P_i, (x 权重, y 权重), 是不是块匹配测的)
    ONE = (1.0, 1.0)
    # 正常一步多远（按键时长固定）：横移、下移各取中位；一行最后一步（顶到边）、最后一行（到底）可以短
    hs = [abs(raw[i][0] - raw[i - 1][0]) for i in range(1, N) if sc[i]["row"] == sc[i - 1]["row"]]
    vs = [abs(raw[i][1] - raw[i - 1][1]) for i in range(1, N) if sc[i]["row"] != sc[i - 1]["row"]]
    med_h = float(np.median([h for h in hs if h > 100])) if any(h > 100 for h in hs) else 390.0
    med_v = float(np.median([v for v in vs if v > 100])) if any(v > 100 for v in vs) else 250.0
    last_row = max(x["row"] for x in sc)

    def plausible(i, m):
        """相邻两屏的测量像不像一步按键：横移一步约 med_h、下移约 med_v（行末 / 最后一行可以短）。"""
        if sc[i]["row"] == sc[i - 1]["row"]:
            row_end = i + 1 >= N or sc[i + 1]["row"] != sc[i]["row"] or sc[i + 1]["file"] == sc[i]["file"]
            return abs(m[1]) <= 8 and (0.6 * med_h <= abs(m[0]) <= 1.4 * med_h or (row_end and abs(m[0]) < 0.6 * med_h) or abs(m[0]) < 3)
        return abs(m[0]) <= 8 and (0.6 * med_v <= abs(m[1]) <= 1.4 * med_v or sc[i]["row"] == last_row or abs(m[1]) < 3)

    for i in range(N):
        sc[i]["cam_near_raw"] = raw[i].tolist()
        if i:
            step = raw[i] - raw[i - 1]
            same_row = sc[i]["row"] == sc[i - 1]["row"]
            m = block_shift(imgs[sc[i - 1]["file"]], imgs[sc[i]["file"]])
            if m and not plausible(i, m):
                # 不像一步按键（周期排列错一组）：按正常步长当预期再量一次
                sgn = np.sign(step) if np.any(step) else np.array([1.0, 1.0])
                exp = (sgn[0] * med_h, 0.0) if same_row else (0.0, med_v)
                m2 = block_shift(imgs[sc[i - 1]["file"]], imgs[sc[i]["file"]], expect=exp, radius=80)
                fixes.append(f"{sc[i - 1]['file']}→{sc[i]['file']} 块匹配 ({m[0]:.0f},{m[1]:.0f}) 不像一步按键"
                             + (f"，按预期重量 ({m2[0]:.0f},{m2[1]:.0f})" if m2 else f"，按预期也量不到，按正常一步 ({exp[0]:.0f},{exp[1]:.0f}) 记"))
                if m2 is None:  # 用正常步长当中等权重的约束（不能退回扫描时记的那个错值：后面跨行比对按它找，会又错一组）
                    prior = np.array(exp, float)
                    off += prior - step
                    edges.append((i - 1, i, prior, (0.3, 0.3), False))
                    pos.append(raw[i] + off)
                    continue
                m = m2
            if m:
                edges.append((i - 1, i, np.array(m[:2]), ONE, True))
                if np.abs(np.array(m[:2]) - step).max() > REFINE_TOL:
                    off += np.array(m[:2]) - step
                    fixes.append(f"{sc[i - 1]['file']}→{sc[i]['file']} 记 ({step[0]:.0f},{step[1]:.0f}) 实测 ({m[0]:.0f},{m[1]:.0f})")
            else:
                # 测不出：按扫描时记的步长作弱约束。按中位补的 / 按速度估的最不可信（到底部边界只走一半时，按中位补就错了，
                # 2026-11-08 景玉谷第二次实测：实为 150 记成 248，和一条 ORB 横移步平分误差，两屏偏 50 px）。
                # 按键只沿一个方向走：横移 y 不变、下移 x 不变，这个分量是硬约束（ORB 在第 0 行树冠上视差大，会量出斜的步）
                w = {"median": 0.002, "nominal": 0.002}.get(sc[i].get("step"), 0.05)
                if same_row:
                    edges.append((i - 1, i, np.array([step[0], 0.0]), (w, 1.0), False))
                else:
                    edges.append((i - 1, i, np.array([0.0, step[1]]), (1.0, w), False))
        pos.append(raw[i] + off)
    pos = np.array(pos)
    for i in range(N):
        for j in range(i + 2, N):
            dc = pos[j] - pos[i]
            if abs(dc[0]) > 600 or abs(dc[1]) > 380 or (np.abs(dc) < 1).all():
                continue
            m = block_shift(imgs[sc[i]["file"]], imgs[sc[j]["file"]], expect=tuple(dc), radius=60)
            if m:
                edges.append((i, j, np.array(m[:2]), ONE, True))
    # 地图边界是直的：每行顶到左/右边的那一屏 x 相同（实测各行 ±1 px）；第一屏是顶到左上角的。
    # 第 0 行多在树冠上测不出，ORB 每步量大 10–40%（视差），靠这条把它拉回来。
    rows = defaultdict(list)
    for i, x in enumerate(sc):
        rows[x["row"]].append(i)
    side = {"左": [0], "右": []}
    for r, ids in rows.items():
        last = ids[-1]
        if len(ids) >= 2 and abs(pos[last][0] - pos[ids[-2]][0]) < 120:
            side["右" if r % 2 == 0 else "左"].append(last)
    for ids in side.values():
        ids = sorted(set(ids))
        for a, b in zip(ids, ids[1:]):
            edges.append((a, b, np.zeros(2), (1.0, 0.0), False))
    keep = list(range(len(edges)))
    sol = pos.copy()
    dropped = 0
    for _ in range(12):
        sol = np.zeros((N, 2))
        for ax in (0, 1):
            rws = [k for k in keep if edges[k][3][ax] > 0]
            A = np.zeros((len(rws) + 1, N)); bb = np.zeros(len(rws) + 1)
            for r, k in enumerate(rws):
                a, b, v, w, _ = edges[k]
                A[r, a], A[r, b], bb[r] = -w[ax], w[ax], w[ax] * v[ax]
            A[-1, 0], bb[-1] = 10.0, 10.0 * pos[0][ax]
            sol[:, ax] = np.linalg.lstsq(A, bb, rcond=None)[0]
        res = {k: np.abs(sol[edges[k][1]] - sol[edges[k][0]] - edges[k][2]).max() for k in keep if edges[k][4]}
        bad = [k for k, e in res.items() if e > 5]
        if not bad:
            break
        worst = max(bad, key=res.get)  # 一次只剔最差的，免得一条错的测量把好的也带歪
        keep.remove(worst)
        dropped += 1
    moved = np.abs(sol - pos).max(axis=1)
    for i in range(N):
        sc[i]["cam_near"] = sol[i].tolist()
    nm = sum(1 for e in edges if e[4])
    nw = sum(1 for e in edges if not e[4] and e[0] + 1 == e[1])
    log(f"  镜头位置复核：{nm} 对重叠屏量了相对位置（另 {nw} 步测不出，按扫描时记的）；边界屏 左 {len(set(side['左']))} 右 {len(set(side['右']))}"
        + (f"；先改了 {len(fixes)} 步量错的：" + "；".join(fixes) if fixes else "")
        + f"；整体求解后最多再挪 {moved.max():.0f} px" + (f"（剔掉 {dropped} 个不一致的测量）" if dropped else ""))
    # dd：near_corner 切 6 块，在所有屏里找，dd = 屏内位置 − 块在 near_corner 里的位置 + cam，取中位
    if (d / "near_corner.png").exists():
        n0 = cv2.cvtColor(imread(d / "near_corner.png"), cv2.COLOR_BGR2GRAY)
        grays = [(x, cv2.cvtColor(imgs[x["file"]], cv2.COLOR_BGR2GRAY)[80:540, 260:1100]) for x in sc]
        found = []
        for qx in (300, 560, 820):
            for qy in (110, 330):
                tpl = n0[qy:qy + 180, qx:qx + 220]
                if tpl.std() < 15:
                    continue
                best = None
                for x, g in grays:
                    _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED))
                    if best is None or v > best[0]:
                        best = (v, x, loc)
                v, x, loc = best
                if v >= 0.75:
                    found.append(np.array([loc[0] + 260 - qx, loc[1] + 80 - qy]) + np.array(x["cam_near"]))
        old = rec.get("corner_recheck")
        if found:
            f = np.array(found)
            med = np.median(f, axis=0)
            spread = float(np.abs(f - med).max())
            rec["corner_recheck_raw"] = rec.get("corner_recheck_raw", old)
            rec["corner_recheck"] = [1.0, float(med[0]), float(med[1]), len(f)]
            log(f"  原点偏移 dd 重定：{med.round().tolist()}（{len(f)} 块，最大偏离 {spread:.1f} px）"
                + ("  ← 各块不一致，坐标可能不准" if spread > 6 else ""))
        elif not found:
            log("  警告：近景角落在各屏里都找不到，原点偏移 dd 用扫描时测的值（可能不准）")
    rec["refined"] = True
    sp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


# Ctrl+R：俯瞰视角逆时针转 90°（画面内容绕屏幕中心顺时针转 90°），缩放不变，按 4 次回原样（2026-11-08 用户告知 + 实测）
VK_CONTROL, VK_R = 0x11, 0x52
SCREEN_C = (640.0, 360.0)
_ROT_BACK = {1: cv2.ROTATE_90_COUNTERCLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_CLOCKWISE}  # 把按 k 次后的截图转正


def _derot_pt(pt, k, W=1280, H=720):
    """按 k 次 Ctrl+R 后截图上的点 → 转正后的图（cv2.rotate(_ROT_BACK[k])）上的坐标。"""
    x, y = pt
    return {1: (y, W - 1 - x), 2: (W - 1 - x, H - 1 - y), 3: (H - 1 - y, x)}[k]


def rot_theory_T(k, W=1280, H=720):
    """理论平移：原屏坐标 = 转正坐标 + T（绕屏幕中心转）。"""
    dx, dy = _derot_pt(SCREEN_C, k, W, H)
    return np.array([SCREEN_C[0] - dx, SCREEN_C[1] - dy])


def rot_offset(base, rot, k):
    """量补拍图（按了 k 次）相对原屏的平移 T（原屏坐标 = 转正坐标 + T）：原屏有效区切块，在转正后的补拍图里模板匹配，
    ≥2 块一致才算；量不出或和理论值差 > 15 px 时用理论值。标签和 UI 不随画面转，会拉低匹配分，所以门槛 0.6。"""
    X0, Y0, X1, Y1 = _EFF
    ga = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    D = cv2.cvtColor(cv2.rotate(rot, _ROT_BACK[k]), cv2.COLOR_BGR2GRAY)
    th = rot_theory_T(k, rot.shape[1], rot.shape[0])
    res = []
    for qx in range(X0 + 60, X1 - 120 - 60 + 1, 100):
        for qy in range(Y0 + 40, Y1 - 120 - 40 + 1, 100):
            t = ga[qy:qy + 120, qx:qx + 120]
            if t.std() < 20:
                continue
            # 只在理论位置附近 ±40 px 找：转正后同一块地面应该落在 (q − T)
            ex, ey = int(qx - th[0]), int(qy - th[1])
            x0, y0 = max(0, ex - 40), max(0, ey - 40)
            win = D[y0:ey + 120 + 40, x0:ex + 120 + 40]
            if win.shape[0] < 120 or win.shape[1] < 120:
                continue
            r = cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED)
            _, s1, _, (x, y) = cv2.minMaxLoc(r)
            if s1 < 0.6:
                continue
            res.append((qx - (x0 + x), qy - (y0 + y)))
    if len(res) >= 2:
        v = np.array(res, float)
        med = np.median(v, axis=0)
        ok = np.abs(v - med).max(axis=1) <= 3
        if ok.sum() >= 2 and np.abs(v[ok].mean(axis=0) - th).max() <= 15:
            return v[ok].mean(axis=0), int(ok.sum())
    return th, 0


def _ctrl_r(p):
    c = p.controller
    c.post_key_down(VK_CONTROL).wait(); time.sleep(0.05)
    c.post_key_down(VK_R).wait(); time.sleep(0.08)
    c.post_key_up(VK_R).wait(); time.sleep(0.05)
    c.post_key_up(VK_CONTROL).wait()


def _wr(p: Path, im) -> None:
    cv2.imencode(p.suffix, im)[1].tofile(str(p))


# ------------------------------------------------------------------ ① 游戏内扫描
def scan(p, area: str, out_dir: Path, extent: tuple[int, int] | None = None, log=print, cancelled=lambda: False,
         rotate: bool = True) -> Path:
    """p：patrol.Patrol（已连上游戏，人在这个据点的俯瞰里）。截图存 out_dir/near/NNN.png，过程存 out_dir/scan.json。
    rotate：每屏存完马上 OCR（结果进 ocr_cache.json，识别时直接用），标签互相压住（crowd_count > 0）就按 Ctrl+R 三次、
    每次补拍一张（NNN_r1/r2/r3.png，视角转 90/180/270°），第 4 次转回原样并核对画面没变（变了就停：方向乱了蛇形会走错）。"""
    from game import move_cursor, wheel
    from patrol import PAN_KEYS
    p.area = area
    extent = tuple(extent) if extent else DEFAULT_EXTENT
    (out_dir / "near").mkdir(parents=True, exist_ok=True)
    rec = {"area": area, "ticks": TICKS, "C": C.tolist(), "extent_far": list(extent), "screens": []}

    def need_overhead(where):
        """不在俯瞰就绝不按方向键（2026-11-08 应龙关：传送后在探索模式没进俯瞰，方向键让角色在地面走了几十步）。"""
        sc = p.scene()
        if sc != "Scene_Overhead":
            raise RuntimeError(f"{where}：不在俯瞰（当前 {sc}），扫描停止，没有再按方向键")

    if p.scene() != "Scene_Overhead":
        import navigate
        log("== 不在俯瞰，先进俯瞰（探索模式先按 Tab 切到工业模式，再按 CapsLock）")
        if not navigate.enter_overhead():
            raise RuntimeError(f"进不了俯瞰：{navigate.last_error()}。请把角色走进这个据点的协议区域后再扫")
    log("== 顶到左上角（最远缩放）")
    p.go_corner()
    need_overhead("顶到左上角后")
    far = p.shot()
    _wr(out_dir / "far_corner.png", far)
    for _ in range(TICKS):
        wheel(p.hwnd, int(C[0]), int(C[1]), 120)
        time.sleep(0.5)
    move_cursor(p.hwnd, 1265, 705)  # 鼠标留在画面中央会出「[点击]查看详情」提示挡住标签：挪到右下角（有效区外）
    time.sleep(1.0)
    near0 = p.shot()
    _wr(out_dir / "near_corner.png", near0)
    sim = similarity(far, near0)
    s = sim[0] if sim else None
    if s is None or not (ZOOM_NEAR * 0.92 <= s <= ZOOM_NEAR * 1.08):
        # 滚入 7 格的放大倍数是固定的（11-07/08 六次实测 1.728–1.731）；量歪了（2026-11-08 程序里实测量成 1.259）
        # 会把据点范围算小、提前收行，所以按固定值
        log(f"== 滚入 {TICKS} 格：放大倍数量成 {s if s is None else round(s, 3)}，不可信，按 {ZOOM_NEAR}")
        s = ZOOM_NEAR
    else:
        log(f"== 滚入 {TICKS} 格，放大 ×{s:.3f}")
    rec["scale"] = s

    speeds = {}
    for direction in ("right", "down"):
        vals = []
        for _ in range(2):
            a = p.shot()
            p.hold(PAN_KEYS[direction], 0.6)
            time.sleep(0.6)
            b = p.shot()
            r = similarity(a, b)
            if r and r[3] >= 30:
                vals.append(abs(r[1] if direction == "right" else r[2]) / 0.6)
        speeds[direction] = vals
    rec["speeds"] = speeds
    # 标定也会被重复结构量错（issue #1：下移标定量成 79、257 px/s），只收和已知速度差不多的
    pan = PAN_SPEED * s / ZOOM_NEAR
    okv = [v for vs in speeds.values() for v in vs if 0.8 * pan <= v <= 1.25 * pan]
    nominal = float(np.median(okv)) if okv else pan

    p.hold(PAN_KEYS["up"], 3.0); time.sleep(0.3)
    p.hold(PAN_KEYS["left"], 4.0); time.sleep(0.6)
    cam = np.array([0.0, 0.0])
    check = similarity(near0, p.shot())
    if check:
        rec["corner_recheck"] = check
    W, H = extent[0] * s, extent[1] * s
    STEP_X, STEP_Y = 0.55, 0.35  # 秒：约 550 / 350 近景像素，留重叠
    row, n = 0, 0
    prev = p.shot()
    good_x, good_y = [], []
    vpend = None  # 下移的两种读法，含义同横移的 pend（见下）：("A", 整步的 y) / ("B", 小步的 y)

    def typical(goods, hold):
        """正常一步多远：量到过 2 步以上用它们的中位，否则按速度 × 按键时长。"""
        return float(np.median(goods)) if len(goods) >= 2 else nominal * hold

    edge_x = {"left": 0.0, "right": None}  # 最近一次确认顶到边时的镜头 x（地图边界是直的，各行一样；左边界就是起点）
    last_saved = [None, None]  # 最近存的那一屏：scan.json 里的条目、它的镜头位置数组（顶边对齐时回改）
    saved_all = []  # 每一屏的（条目, 镜头位置数组）：下移改记时整行一起改

    def set_last_x(x, step=None):
        if last_saved[0] is not None:
            last_saved[0]["cam_near"][0] = float(x)
            last_saved[1][0] = x
            if step:
                last_saved[0]["step"] = step

    def shift_row(r, dy):
        for ent, arr in saved_all:
            if ent["row"] == r:
                ent["cam_near"][1] += float(dy)
                arr[1] += dy

    def has_labels():
        boxes = p.ocr_boxes([260, 80, 840, 460])
        return sum(1 for _, t in boxes if len(t) >= 2 and re.match(r"^[一-鿿]", t)) >= 2

    def still(a, b):
        return frame_diff(a, b) < STILL_DIFF

    ocr_cache, names = {}, _names()
    n_rot = n_skip = 0

    def settle(prev_img):
        """转视角动画约 0.72 s（2026-11-08 实测 4 次，之后帧差 < 1）：0.55 s 后每 0.05 s 截一张，相邻两张有效区帧差 < 1.5 就算停稳（最多 2.5 s）。"""
        t0 = time.time()
        time.sleep(0.55)
        last = p.shot()
        while time.time() - t0 < 2.5:
            time.sleep(0.05)
            cur = p.shot()
            a = cv2.cvtColor(last, cv2.COLOR_BGR2GRAY)[80:540, 260:1100].astype(np.float32)
            b = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)[80:540, 260:1100].astype(np.float32)
            if float(np.abs(a - b).mean()) < 1.5:
                return cur
            last = cur
        return p.shot()

    covered = []  # 已补拍屏的镜头位置

    def rotate_shots(img, fname, cam_now):
        """这一屏竖条里有标签压住、且还没被前面的补拍覆盖：转 90°、270° 各补拍一张（180° 只按过去不拍：
        离线比过，去掉它景玉谷 131 台里 129 台位置差 ≤6 px），第 4 次转回原样。"""
        nonlocal n_rot, n_skip
        import offline_ocr
        ocr = offline_ocr.shared()
        boxes = ocr.boxes(img, ROI)
        ocr_cache[fname] = boxes  # 左下那块（LROI）识别时再读
        crowd = crowd_boxes(boxes, names)
        if not rotate or not crowd:
            return []
        g = [np.array(c) + cam_now for c in crowd if ROT_STRIP[0] <= c[0] <= ROT_STRIP[1]]
        todo = [q for q in g if not any(ROT_STRIP[0] <= q[0] - cr[0] <= ROT_STRIP[1] and 80 <= q[1] - cr[1] <= 540 for cr in covered)]
        if not todo:
            n_skip += 1
            return []
        out, cur = [], img
        for i in (1, 2, 3):
            _ctrl_r(p)
            cur = settle(cur)
            if i == 2:
                continue
            rn = fname.replace(".png", f"_r{i}.png")
            _wr(out_dir / "near" / rn, cur)
            out.append(rn)  # OCR 留给识别
        covered.append(cam_now)
        for attempt in range(4):
            _ctrl_r(p)
            cur = settle(cur)
            m = block_shift(img, cur)
            if still(img, cur) or (m and abs(m[0]) <= 3 and abs(m[1]) <= 3):
                break
            log(f"  转回原样没对上（第 {attempt + 1} 次），再按一次 Ctrl+R")
        else:
            raise RuntimeError("Ctrl+R 转回原样失败：视角方向不对，扫描停止（不能带着错的方向继续走）")
        n_rot += 1
        return out

    how = None  # 上一屏到这一屏这一步是怎么量的：block / orb / median（量出来不像，按中位补）/ nominal（没量出，按速度估）/ still；离线复核按它定权重

    def save(img, c, r):
        nonlocal n, how
        n += 1
        _wr(out_dir / "near" / f"{n:03d}.png", img)
        ent = {"file": f"{n:03d}.png", "cam_near": list(map(float, c)), "row": r, "step": how}
        rec["screens"].append(ent)
        arr = np.array(c, float)
        last_saved[:] = [ent, arr]
        saved_all.append((ent, arr))
        if how != "still":  # 同一位置重复存的不再补拍
            rot = rotate_shots(img, ent["file"], arr)
            if rot:
                ent["rot"] = rot
        how = "still"  # 同一位置再存一屏（顶到边）时

    while not cancelled():
        need_overhead(f"第 {row + 1} 行开始前")
        direction = "right" if row % 2 == 0 else "left"
        sg = 1 if direction == "right" else -1
        side = "右" if sg > 0 else "左"
        # 收行只认一件事：再按一步，画面真的停了（帧差 < STILL_DIFF，或块匹配确认没动）。量到的步长小不算。
        unsure, x0, alt = 0, 0.0, None  # 连着几步量不出（按正常步长暂记的）；第一步量不出之前的 x；那一步 ORB 量到的同方向小步
        # 上一步有两种读法时留着另一种，看这一步画面停不停再定：
        #   ("A", 整步的 x)：记的是块匹配量到的小步；这一步画面还在走 → 不是顶到边，改成整步
        #   ("B", 小步的 x)：记的是按预期量到的整步；这一步画面停了 → 上一步是顶到边的半步，改成小步
        pend = None
        clamped = 0  # 暂记的位置连着几次被已知边界挡住
        while not cancelled():
            save(prev, cam, row)
            if direction == "right" and cam[0] >= W - 900 and not has_labels():
                break
            if cam[0] > W - 1280 + 2 * nominal * STEP_X or cam[0] < -2 * nominal * STEP_X or clamped >= 3:
                # 兜底：一直量不出、画面又一直在变（不该发生）。记的位置已经在边界上 / 地图外面两步远，别无限走下去
                log(f"  这一行记到 x={cam[0]:.0f}，一直量不出也不见画面停，收行（位置不可信，靠离线复核）")
                break
            p.hold(PAN_KEYS[direction], STEP_X); time.sleep(0.5)
            cur = p.shot()
            typ = typical(good_x, STEP_X)
            dx, how, sure, a2 = step_shift(prev, cur, 0, sg, typ)
            prev = cur
            stopped = how == "still" or (how == "block" and abs(dx) < 3)
            if pend and pend[0] == "A" and not stopped:
                log(f"  上一步量成小步（记到 x={cam[0]:.0f}），这一步画面还在走：不是顶到边，按正常步长改记到 x={pend[1]:.0f}")
                cam[0] = pend[1]
                set_last_x(cam[0], "nominal")
            elif pend and pend[0] == "B" and stopped:
                log(f"  上一步按预期量成整步（记到 x={cam[0]:.0f}），这一步画面停了：上一步是顶到{side}边的半步，改记到 x={pend[1]:.0f}")
                cam[0] = pend[1]
                set_last_x(cam[0])
            if stopped:
                if unsure:
                    # 前面有量不出、按正常步长暂记的步，现在画面停了：最后那一步其实是顶到边的半步，暂记的位置要改
                    if edge_x[direction] is not None:
                        cam[0] = edge_x[direction]
                        log(f"  画面停了：顶到{side}边，位置对齐到{side}边界 x={cam[0]:.0f}")
                    elif unsure == 1 and alt is not None and pend is None:
                        cam[0] = x0 + alt
                        log(f"  画面停了：顶到{side}边，还没有{side}边界的位置，上一步按 ORB 量到的 {alt:.0f} 记")
                    else:
                        log(f"  画面停了：顶到{side}边，还没有{side}边界的位置，暂记的 x={cam[0]:.0f} 可能偏大（靠离线复核）")
                    set_last_x(cam[0])
                else:
                    edge_x[direction] = float(cam[0])
                how = "still"
                save(prev, cam, row)
                break
            pend = None
            if not sure:
                # 量不出（重复结构），或者是顶到边的半步：按正常步长暂记，再走一步，画面停了才算到边
                unsure += 1
                if unsure == 1:
                    x0, alt = float(cam[0]), a2
                cam[0] += dx
                if edge_x[direction] is not None and sg * (cam[0] - edge_x[direction]) > 0:
                    cam[0] = edge_x[direction]  # 不会走出边界
                    clamped += 1
                log(f"  横向一步量不出，按正常步长暂记到 x={cam[0]:.0f}，再走一步看画面停不停")
                continue
            cam[0] += dx
            if 0.6 * typ <= abs(dx):
                if a2 is not None:
                    pend = ("B", float(cam[0] - dx + a2))
                else:
                    good_x.append(abs(dx))
                unsure = clamped = 0
            else:
                pend = ("A", float(cam[0] - dx + sg * typ))
        if cam[1] >= H - 500 and not has_labels():
            log("== 走出基地了（这一行没有设备标签）")
            break
        p.hold(PAN_KEYS["down"], STEP_Y); time.sleep(0.5)
        cur = p.shot()
        typ = typical(good_y, STEP_Y)
        dy2, how, sure, a2 = step_shift(prev, cur, 1, 1, typ)
        if how == "still" or (how == "block" and abs(dy2) < 3):
            if vpend and vpend[0] == "B":
                log(f"  上一次下移按预期量成整步，现在到底了：那是到底前的半步，这一行的 y 从 {cam[1]:.0f} 改记到 {vpend[1]:.0f}")
                shift_row(row, vpend[1] - cam[1])
                cam[1] = vpend[1]
            log("== 到底了（画面没变）")
            how = "still"
            save(cur, cam, row + 1)
            break
        if vpend and vpend[0] == "A":
            log(f"  上一次下移量成小步，现在还能往下走：不是到底，这一行的 y 从 {cam[1]:.0f} 按正常步长改记到 {vpend[1]:.0f}")
            shift_row(row, vpend[1] - cam[1])
            cam[1] = vpend[1]
        vpend = None
        if not sure:  # 重复结构上量不出（或到底前的半步）：按正常步长记，离线复核靠上下行重叠改
            log(f"  纵向一步量不出，按正常步长 {dy2:.0f} 记")
        elif 0.6 * typ <= dy2:
            if a2 is not None:
                vpend = ("B", float(cam[1] + a2))
            else:
                good_y.append(dy2)
        else:
            vpend = ("A", float(cam[1] + typ))
        cam[1] += dy2
        prev = cur
        row += 1
        log(f"  第 {row} 行，已存 {n} 屏")
    (out_dir / "scan.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    if ocr_cache:
        (out_dir / "ocr_cache.json").write_text(json.dumps(ocr_cache, ensure_ascii=False), encoding="utf-8")
    if rotate:
        log(f"== 标签压住的 {n_rot} 屏已按 Ctrl+R 转视角补拍（另 {n_skip} 屏压住的地方已被邻屏补拍覆盖，没重复拍）")
    p.zoom_out()  # 不调 ensure_overhead：它在有面板开着时会点击关闭，扫底账约定不点击
    log(f"== 扫描完成：{len(rec['screens'])} 屏")
    return out_dir


# ------------------------------------------------------------------ ② 离线识别
def _names() -> set[str]:
    names = set(json.loads((ROOT / "data" / "building_names.json").read_text(encoding="utf-8")))
    return names | EXTRA_NAMES


def _split_names(box, text, NAMES):
    """标签互相压住时 OCR 会连成一串：从左往右贪心切成已知全名，切不动的字丢掉；第二段起从右端往回推位置。"""
    longer = [x for x in NAMES if text in x and x != text]
    if len(text) >= 3 and len(longer) == 1:
        return [(box, longer[0])]
    out, i, n = [], 0, len(text)
    while i < n:
        best = None
        for j in range(n, i + 1, -1):
            seg = text[i:j]
            if seg in NAMES:
                best = (j, seg)
                break
            cands = [x for x in NAMES if x.startswith(seg)]
            if len(seg) >= 2 and len(cands) == 1:
                best = (j, cands[0])
                break
        if best is None:
            i += 1
            continue
        j, name = best
        x0 = box[0] + box[2] * i / n
        out.append(([round(x0), box[1], round(box[2] * (j - i) / n), box[3]], name, i))
        i = j
    h = min(box[3], 30)
    cw, iw = CW_RATIO * h, 1.3 * h
    right = box[0] + box[2]
    res = []
    for pi, (bb, name, i0) in enumerate(out):
        if pi > 0 or i0 > 0:
            x0 = right - (n - i0) * cw - (len(out) - 1 - pi) * iw
            bb = [round(x0), bb[1], round(len(name) * cw), bb[3]]
        res.append((bb, name))
    return res

def _merge_fragments(boxes, NAMES):
    """同一行、首尾相接的碎片拼起来，拼出来是建筑名就用拼的。"""
    items = sorted([(bx, re.sub(r"[.。·…]+$", "", t)) for bx, t, _ in boxes if re.match(r"^[一-鿿]", t)],
                   key=lambda z: (z[0][1] // 10, z[0][0]))
    out, used = [], set()
    for i, (bx, t) in enumerate(items):
        if i in used:
            continue
        cur_b, cur_t = list(bx), t
        for j in range(i + 1, len(items)):
            if j in used:
                continue
            b2, t2 = items[j]
            same_row = abs((b2[1] + b2[3] / 2) - (cur_b[1] + cur_b[3] / 2)) <= 6
            gap = b2[0] - (cur_b[0] + cur_b[2])
            if same_row and -4 <= gap <= 14 and (cur_t + t2) in NAMES or (
                    same_row and -4 <= gap <= 14 and any(nm.startswith(cur_t + t2) for nm in NAMES) and cur_t not in NAMES):
                cur_t += t2
                cur_b = [cur_b[0], min(cur_b[1], b2[1]), b2[0] + b2[2] - cur_b[0], max(cur_b[3], b2[3])]
                used.add(j)
        if cur_t in NAMES:
            out.append((cur_b, cur_t, 1.0))
        else:
            parts = _split_names(cur_b, cur_t, NAMES)
            out += [(b2, t2, 0.5) for b2, t2 in parts] if parts else [(cur_b, cur_t, 1.0)]
    return out

def crowd_count(boxes, NAMES, roi=ROI) -> int:
    return len(crowd_boxes(boxes, NAMES, roi))


def crowd_boxes(boxes, NAMES, roi=ROI) -> list:
    """一屏里「标签互相压住」的地方（框中心，屏幕坐标）：连成一串被切开的（_merge_fragments 给 0.5 分）、几个标签被框成一个（框高 > 45）、
    只露开头的前缀碎片。> 0 就值得按 Ctrl+R 转个方向补拍（标签总是横着画，转 90° 后同一排设备的标签上下错开）。"""
    out = []
    for box, t, score in _merge_fragments(boxes, NAMES):
        cut = (box[0] <= roi[0] + 4 + box[3] and roi[0] > 0 or box[0] + box[2] >= roi[0] + roi[2] - 4
               or box[1] <= roi[1] + 2 or box[1] + box[3] >= roi[1] + roi[3] - 2)
        if cut:
            continue
        if score == 0.5 or box[3] > 45 or (t not in NAMES and len(t) >= 2 and any(nm.startswith(t) and nm != t for nm in NAMES)):
            out.append((box[0] + box[2] / 2, box[1] + box[3] / 2))
    return out


# 转 90°/270° 补拍能换回原屏的范围（原屏 x）：转 90° 覆盖 360–820，转 270° 覆盖 460–920，取并集留 20 px 余量。
# 这个竖条比横移一步（390）宽，所以一排里每个位置总会落在某一屏的竖条里；已经被前面补拍覆盖的重叠处不再重复补拍。
ROT_STRIP = (380, 900)


# 标签（图标 + 文字）以设备为中心摆放；底账记的是「图标中心」，它离设备锚点有一个固定的屏幕偏移 o（近景 px），
# 名字越长图标越靠左。转视角后 o 在地图上换了方向，补拍读数 − 原屏读数 = (P_k − I)·o（P_k：补拍坐标转回原屏的线性部分）。
# 2026-11-08 景玉谷实测（每种设备三个方向解出同一个 o）：3 字 ≈ (−31.5, −3.2)，4 字 ≈ (−42, −3.1)，5 字 ≈ (−51.5, −3.5)。
_P_ROT = {1: np.array([[0.0, 1.0], [-1.0, 0.0]]), 2: -np.eye(2), 3: np.array([[0.0, -1.0], [1.0, 0.0]])}


def label_offset_default(name: str) -> np.ndarray:
    return np.array([-10.0 * len(name) - 1.5, -3.2])


def _rot_correct(raw, F2N, log=print):
    """把补拍读数换成原视角的「图标中心」约定：减去 (P_k − I)·o。o 每种设备从本次扫描的配对里解（≥3 对、六成在中位 4 px 内），
    配不上的（如一排挤在一起的仓库取货口）按字数公式。"""
    L = F2N[:, :2]  # 远景 → 近景（线性部分）
    Li = np.linalg.inv(L)
    S = lambda k: _P_ROT[k] - np.eye(2)  # noqa: E731
    base = [r for r in raw if not r[3].startswith("转") and not r[5] and not r[6]]
    est = defaultdict(list)
    for r in raw:
        if not r[3].startswith("转") or r[5] or r[6]:
            continue
        k = int(r[3][1])
        pred = np.array(r[1:3]) - Li @ (S(k) @ label_offset_default(r[0]))  # 按公式先估，原屏读数应在这附近
        c = [(abs(b[1] - pred[0]) + abs(b[2] - pred[1]), b) for b in base
             if b[0] == r[0] and abs(b[1] - pred[0]) <= 20 and abs(b[2] - pred[1]) <= 20]
        if c:
            b = min(c, key=lambda z: z[0])[1]
            dn = L @ (np.array(r[1:3]) - np.array(b[1:3]))
            est[r[0]].append(np.linalg.solve(S(k), dn))
    o_name, notes = {}, []
    for nm, v in est.items():
        a = np.array(v)
        med = np.median(a, axis=0)
        good = np.abs(a - med).max(axis=1) <= 4
        if len(a) >= 3 and good.mean() >= 0.6:
            o_name[nm] = a[good].mean(axis=0)
            notes.append(f"{nm}({o_name[nm][0]:.0f},{o_name[nm][1]:.0f})")
    log(f"  补拍读数校正：{len(o_name)} 种设备按本次配对解出图标偏移，其余按字数公式" + (f"；" + "、".join(notes) if notes else ""))
    out = []
    for r in raw:
        if r[3].startswith("转"):
            k = int(r[3][1])
            o = o_name.get(r[0], label_offset_default(r[0]))
            q = np.array(r[1:3]) - Li @ (S(k) @ o)
            r = (r[0], float(q[0]), float(q[1])) + tuple(r[3:])
        out.append(r)
    return out


def recognize(d: Path, log=print) -> list[dict]:
    """scan() 的目录 → 合并后的设备 [{name, x, y, seen, spread, edge_only, expected, how}]（最远坐标）。
    OCR 结果缓存在 d/ocr_cache.json（重跑很快）。结果也存 d/ledger_near.json。"""
    import labelicon
    import offline_ocr
    from offline_ocr import imread

    d = Path(d)
    scan_rec = refine_positions(d, log)
    dd = np.array(scan_rec["corner_recheck"][1:3]) if scan_rec.get("corner_recheck") else np.zeros(2)
    NAMES = _names()
    ids = labelicon.LabelIcons()

    orb = cv2.ORB_create(4000)
    a, b = imread(d / "far_corner.png"), imread(d / "near_corner.png")
    ka, da = orb.detectAndCompute(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), _MASK)
    kb, db = orb.detectAndCompute(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), _MASK)
    mt = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db), key=lambda x: x.distance)[:500]
    F2N, inl = cv2.estimateAffinePartial2D(np.float32([ka[x.queryIdx].pt for x in mt]), np.float32([kb[x.trainIdx].pt for x in mt]),
                                           ransacReprojThreshold=3)
    N2F = cv2.invertAffineTransform(F2N)
    log(f"  最远角落 → 近景角落：缩放 ×{np.hypot(F2N[0, 0], F2N[1, 0]):.3f}，内点 {int(inl.sum())}")

    def to_far(q):
        return N2F @ np.array([q[0], q[1], 1.0])

    def icon_off(name, box):
        return ICON_OFF.get(name, 0.69) * min(box[3], 30)

    def identify_scaled(img, box, head):
        """近景标签更大（字高 26 vs 20）：按字高等比例裁图标，缩回模板尺寸再认。"""
        k = min(box[3], 30) / 20.0
        side, pad, gap = round(ids.icon * k), round(ids.shift * k), round(2 * k)
        x1 = box[0] - gap
        x0, y0 = x1 - side, box[1] + box[3] // 2 - side // 2
        if x0 - pad < 0 or y0 - pad < 0 or y0 + side + pad > img.shape[0]:
            return None
        crop = img[y0 - pad:y0 + side + pad, x0 - pad:x1 + pad]
        crop = cv2.resize(crop, (ids.icon + 2 * ids.shift, ids.icon + 2 * ids.shift), interpolation=cv2.INTER_AREA)
        sc = ids.scores(crop)
        if not sc:
            return None
        (n1, s1), (n2, s2) = sc[0], (sc[1] if len(sc) > 1 else (None, -1))
        ok = s1 >= labelicon.ACCEPT and s1 - s2 >= labelicon.MARGIN and (n1.startswith(head) or (n1 == "仓库口" and head == "仓"))
        return n1, s1, ok

    def split_names(box, text):
        return _split_names(box, text, NAMES)

    def merge_fragments(boxes):
        return _merge_fragments(boxes, NAMES)

    def icon_window(img, box):
        h = box[3]
        x1, x0 = box[0], int(box[0] - 3.0 * h)
        y0, y1 = int(box[1] + h / 2 - 0.9 * h), int(box[1] + h / 2 + 0.9 * h)
        if x0 < 0 or y0 < 0 or y1 > img.shape[0]:
            return None
        hsv = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        m = ((hsv[..., 2] > 200) & (hsv[..., 1] < 60)).astype(np.float32)
        return cv2.resize(m, (72, 24), interpolation=cv2.INTER_AREA)

    def win_score(a_, b_):
        if a_ is None or b_ is None:
            return -1
        return float(cv2.matchTemplate(np.pad(a_, 3), b_, cv2.TM_CCOEFF_NORMED).max())

    cache_p = d / "ocr_cache.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    ocr = None
    raw, frag, edge_cut, ambiguous = [], Counter(), Counter(), Counter()
    prefix_frags, selfref = [], defaultdict(list)
    rot_T = {}

    def shots():
        """(文件, 截图, OCR 缓存键, roi, 镜头, 「截图坐标 → 原屏坐标」, 标记)：原屏两块 roi + 补拍图（转正后对回原屏）。"""
        for sc in scan_rec["screens"]:
            img = imread(d / "near" / sc["file"])
            cam = np.array(sc["cam_near"])
            ident = lambda u: np.asarray(u, float)  # noqa: E731
            yield sc["file"], img, sc["file"], ROI, cam, ident, ""
            yield sc["file"], img, sc["file"] + "#L", LROI, cam, ident, ""
            for rn in sc.get("rot", []):
                k = int(rn.rsplit("_r", 1)[1][0])
                rimg = imread(d / "near" / rn)
                T, nb = rot_offset(img, rimg, k)
                rot_T[rn] = (T.round(1).tolist(), nb)
                mp = (lambda kk, TT: (lambda u: np.array(_derot_pt(u, kk), float) + TT))(k, T)
                yield rn, rimg, rn, ROI, cam, mp, f"转{k}·"

    try:
        for f, img, key, roi, cam, mapper, tag in shots():
            if True:
                if key not in cache:
                    ocr = ocr or offline_ocr.shared()
                    cache[key] = ocr.boxes(img, roi)
                for box, text, score in merge_fragments(cache[key]):
                    t = text.strip()
                    # 框比字宽出很多：OCR 把左边图标也框进来了，起点改从右端推
                    if score == 1.0 and t in NAMES and 20 <= box[3] <= 34 and box[2] > 1.25 * len(t) * CW_RATIO * min(box[3], 30) + 6:
                        box = [round(box[0] + box[2] - len(t) * CW_RATIO * min(box[3], 30)), box[1],
                               round(len(t) * CW_RATIO * min(box[3], 30)), box[3]]
                    lcut = box[0] <= roi[0] + 4 + box[3] and roi[0] > 0
                    rcut = box[0] + box[2] >= roi[0] + roi[2] - 4
                    cut = lcut or rcut or box[1] <= roi[1] + 2 or box[1] + box[3] >= roi[1] + roi[3] - 2
                    tall = box[3] > 45  # 几个上下错开的标签被框成一个
                    edge_cut[f] += cut
                    if (lcut and any(nm != t and nm.endswith(t) for nm in NAMES)) or (rcut and any(nm != t and nm.startswith(t) for nm in NAMES)):
                        ambiguous[t] += 1
                        continue
                    r = identify_scaled(img, box, t[0])
                    icon_name, icon_ok = (r[0], r[2]) if r else (None, False)
                    if t in NAMES:
                        agree = icon_ok and ids.canonical(t) == ids.canonical(icon_name) or (t in ("仓库取货口", "仓库存取线基段") and icon_name == "仓库口")
                        how = "全名+图标" if agree else ("全名(无模板)" if ids.canonical(t) not in ids.templates and t not in ("仓库取货口", "仓库存取线基段")
                                                     else f"全名(图标像{icon_name})")
                        name = t
                    elif icon_ok:
                        how, name = "图标+首字", icon_name
                    else:
                        frag[t] += 1
                        if len(t) >= 2 and sum(1 for nm in NAMES if nm.startswith(t) and nm != t) >= 1 and not cut:
                            prefix_frags.append((t, box, f, cam, icon_window(img, box), mapper, tag))
                        continue
                    if not cut and not tall and t in NAMES:
                        selfref[name].append(icon_window(img, box))
                    u = mapper(np.array([box[0] - icon_off(name, box), box[1] + box[3] / 2]))
                    far = to_far(u + cam - dd)
                    # crowd：标签互相压住时切开 / 框在一起推出来的位置（±20 px）；有补拍的清楚读数时不用它定位置
                    raw.append((name, float(far[0]), float(far[1]), tag + how, f, bool(cut or tall), bool(score == 0.5 or tall)))
    finally:
        cache_p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 被压住、只露开头的标签：拿左边图标窗口和本次扫描里完整标签的图标比
    resolved = []
    for t, box, f, cam, win, mapper, tag in prefix_frags:
        cands = [nm for nm in NAMES if nm.startswith(t) and nm != t and selfref.get(nm)]
        scores = sorted(((max(win_score(win, rr) for rr in selfref[nm][:15]), nm) for nm in cands), reverse=True)
        if t in NAMES and selfref.get(t):
            scores = sorted(scores + [(max(win_score(win, rr) for rr in selfref[t][:15]), t)], reverse=True)
        best, second = (scores + [(-1, None), (-1, None)])[:2]
        far = to_far(mapper(np.array([box[0] - icon_off(best[1] or t, box), box[1] + box[3] / 2])) + cam - dd)
        resolved.append({"t": t, "f": f, "best": best, "second": second, "far": far, "tag": tag})
    groups = []
    for r in resolved:
        g = next((g for g in groups if g[0]["t"] == r["t"] and np.abs(g[0]["far"] - r["far"]).max() <= 35), None)
        (g.append(r) if g else groups.append([r]))
    for g in groups:
        tops = {r["best"][1] for r in g}
        margin = float(np.mean([r["best"][0] - r["second"][0] for r in g]))
        score = float(np.mean([r["best"][0] for r in g]))
        single = [r for r in g if r["best"][0] >= 0.45 and r["best"][0] - r["second"][0] >= 0.2]
        multi = len(g) >= 3 and len(tops) == 1 and margin >= 0.05 and score >= 0.6
        for r in (g if multi else single):
            raw.append((r["best"][1], float(r["far"][0]), float(r["far"][1]), f"{r['tag']}前缀「{r['t']}」+图标窗口", r["f"], False, True))
    if rot_T:
        nb = [v[1] for v in rot_T.values()]
        log(f"  补拍 {len(rot_T)} 张：和原屏的平移量出 {sum(1 for b in nb if b)} 张（其余用理论值）")
        raw = _rot_correct(raw, F2N, log)
    (d / "raw.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    # 合并：同名 35 px 内算一台；先并完整标签，贴边的后并
    led = []
    for name, x, y, how, f, cut, crowd in sorted(raw, key=lambda r: (r[5], r[6])):
        hit = next((e for e in led if e["name"] == name and abs(e["x"] - x) <= 35 and abs(e["y"] - y) <= 35), None)
        isrot = how.startswith("转")
        good = not cut and not crowd and isrot  # 补拍里读清楚的
        if hit:
            hit["xs"].append(x); hit["ys"].append(y); hit["cut"].append(cut); hit["how"].add(how)
            hit["crowd"].append(crowd); hit["rot"].append(isrot); hit["good_rot"] |= good
        else:
            led.append({"name": name, "x": x, "y": y, "xs": [x], "ys": [y], "cut": [cut], "how": {how},
                        "crowd": [crowd], "rot": [isrot], "good_rot": good})
    # 第二遍：按多屏中位算完位置后，同名 25 px 内的再并一次（先并的种子位置可能偏，贴边读数会另起一台：
    # 武陵城规划线上那个暗管出口原来被记成两台）
    def _ok(e):
        """定位置用哪些读数：不贴边的；有补拍的清楚读数时，再去掉标签压住推出来的。"""
        ok = [i for i, c in enumerate(e["cut"]) if not c] or list(range(len(e["xs"])))
        if e["good_rot"]:
            ok = [i for i in ok if not e["crowd"][i]] or ok
        return ok

    def _pos(e):
        ok = _ok(e)
        return float(np.median(np.array(e["xs"])[ok])), float(np.median(np.array(e["ys"])[ok]))
    merged = True
    while merged:
        merged = False
        for i in range(len(led)):
            for j in range(i + 1, len(led)):
                a_, b_ = led[i], led[j]
                if a_["name"] == b_["name"]:
                    (ax, ay), (bx_, by_) = _pos(a_), _pos(b_)
                    if abs(ax - bx_) <= 25 and abs(ay - by_) <= 25:
                        a_["xs"] += b_["xs"]; a_["ys"] += b_["ys"]; a_["cut"] += b_["cut"]; a_["how"] |= b_["how"]
                        a_["crowd"] += b_["crowd"]; a_["rot"] += b_["rot"]; a_["good_rot"] |= b_["good_rot"]
                        del led[j]
                        merged = True
                        break
            if merged:
                break
    # 「仓库口」= 只认出图标（仓库取货口 / 仓库存取线基段 图标一样）、字没读全；同一位置已有全名的就并进去
    # （景玉谷两条「仓库口」都和仓库取货口重合 ≤3 px，档案底账里也是重复记录）
    for e in [e for e in led if e["name"] == "仓库口"]:
        ex, ey = _pos(e)
        near_full = [(abs(_pos(o)[0] - ex) + abs(_pos(o)[1] - ey), o) for o in led  # 同一行 25 px 内（应龙关那条差 20 px）
                     if o["name"] in ("仓库取货口", "仓库存取线基段") and abs(_pos(o)[0] - ex) <= 25 and abs(_pos(o)[1] - ey) <= 8]
        if near_full:
            o = min(near_full, key=lambda z: z[0])[1]
            o["xs"] += e["xs"]; o["ys"] += e["ys"]; o["cut"] += [True] * len(e["xs"]); o["how"] |= e["how"]
            o["crowd"] += [True] * len(e["xs"]); o["rot"] += e["rot"]
            led.remove(e)
    inside = lambda u, r: r[0] <= u[0] <= r[0] + r[2] and r[1] <= u[1] <= r[1] + r[3]  # noqa: E731
    for e in led:
        ok = _ok(e)
        xs, ys = np.array(e["xs"])[ok], np.array(e["ys"])[ok]
        e["x"], e["y"] = float(np.median(xs)), float(np.median(ys))
        e["spread"] = float(max(np.ptp(xs), np.ptp(ys)))
        e["edge_only"] = all(e["cut"])
        q = F2N @ np.array([e["x"], e["y"], 1.0])
        e["expected"] = sum(1 for sc in scan_rec["screens"]
                            if inside(q - np.array(sc["cam_near"]) + dd, ROI) or inside(q - np.array(sc["cam_near"]) + dd, LROI))
    ghosts = [e for e in led if len(e["xs"]) == 1 and e["expected"] >= 4]
    # 只有贴边截断的读数（≤2 次）、同一行 50 px 内已有读得完整的同名设备：截断的标签起点推不准，是那台的重复
    # （2026-11-08 程序里实测：底排两台仓库取货口之间多出一台，两次读数都在屏边、图标也认成别的）
    # 应龙关多口暗管出口：两次截断读数、离完整的那台 54 px，本该完整看到 ≥3 次却一次没有 → 门槛放到 70 px 并要求 expected ≥ 3
    ghosts += [e for e in led if e not in ghosts and e["edge_only"] and len(e["xs"]) <= 2 and e["expected"] >= 3
               and any(o is not e and o["name"] == e["name"] and not o["edge_only"]
                       and abs(o["x"] - e["x"]) <= 70 and abs(o["y"] - e["y"]) <= 15 for o in led)]
    # 只在补拍里读到的（原视角一次都没有）：补拍里要有 ≥2 次完整读数才收，贴边截断的半截名（「供电桩」）、只读到一次的不收
    ghosts += [e for e in led if e not in ghosts and not any(not r for r in e["rot"])
               and sum(1 for r, c in zip(e["rot"], e["cut"]) if r and not c) < 2]  # 只见 1 次、却本该被 ≥4 屏看到 → 误识别
    led = [e for e in led if e not in ghosts]
    out = [{k: v for k, v in e.items() if k not in ("xs", "ys", "how", "cut", "crowd", "rot", "good_rot")}
           | {"how": sorted(e["how"]), "seen": len(e["xs"]) - sum(e["rot"]), "seen_rot": sum(e["rot"])} for e in led]
    (d / "ledger_near.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if rot_T:
        (d / "rot_offsets.json").write_text(json.dumps(rot_T, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  识别：看到 {len(raw)} 次，合并成 {len(out)} 台；去掉误识别 {len(ghosts)} 个；碎片/认不出 {sum(frag.values())} 个")
    return out


# ------------------------------------------------------------------ ③ 写底账
def save_ledger(archive: str, area: str, near: list[dict], scan_dir: Path, boundary: list[float] | None = None,
                dest: Path | None = None, log=print) -> Path:
    """写 data/blueprints/<archive>/ledger/<area>.json（dest 给了就写 dest）。
    boundary = [x0, x1, y0, y1]（最远坐标，协议核心规划线）：给了就只收线内（+10 余量）的设备，压线又孤立的、线外的单列。"""
    import ledger as L
    scan_dir = Path(scan_dir)
    scan_rec = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    stamp = scan_dir.name.split("_")[-1]
    label_cx = lambda e: e["x"] + 9 + 6.2 * len(e["name"])  # noqa: E731  底账坐标是标签左边图标的位置，线内外按标签中心判
    if boundary:
        x0, x1, y0, y1 = boundary
        M = 10
        inside = [e for e in near if x0 - M <= label_cx(e) <= x1 + M and y0 - M <= e["y"] <= y1 + M]

        def isolated(e, r=150):  # 150 px 内没有别的设备（同名 30 px 内的算它自己的重复读数，不算）
            return all(max(abs(o["x"] - e["x"]), abs(o["y"] - e["y"])) > r for o in near
                       if o is not e and not (o["name"] == e["name"] and max(abs(o["x"] - e["x"]), abs(o["y"] - e["y"])) <= 30))
        on_line = [e for e in inside if min(abs(label_cx(e) - x0), abs(label_cx(e) - x1), abs(e["y"] - y0), abs(e["y"] - y1)) <= 15 and isolated(e)]
        inside = [e for e in inside if e not in on_line]
        outside = [e for e in near if e not in inside and e not in on_line]
    else:
        inside, on_line, outside = list(near), [], []
    led = L.Ledger(area, zoom="far")
    for e in inside:
        notes = []
        if e.get("edge_only"):
            notes.append("没有完整读数（贴边或和别的标签叠字），位置按多屏中位")
        if any("前缀" in h for h in e.get("how", [])):
            notes.append("标签被压住，按图标判定")
        if e.get("seen_rot"):
            notes.append(f"转视角补拍读到 {e['seen_rot']} 次" + ("（原视角没读到）" if e["seen"] == 0 else ""))
        if e["seen"] == 1:
            notes.append("只见 1 次")
        led.devices.append(L.Entry(e["name"], round(e["x"]), round(e["y"]), "ocr", e["seen"], stamp, "；".join(notes)))
    if dest is None:
        p = L.save(archive, led)
    else:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"area": area, "zoom": "far", "updated": time.strftime("%Y%m%d-%H%M%S"),
                                    "devices": [vars(x) for x in led.devices]}, ensure_ascii=False, indent=1), encoding="utf-8")
        p = dest
    body = json.loads(p.read_text(encoding="utf-8"))
    body["coords"] = "最远缩放坐标（go_corner 原点）；由近景 7 格扫描换算（scale/C 见 scan）"
    body["boundary"] = list(boundary) if boundary else None
    body["on_line"] = [{"name": e["name"], "x": round(e["x"]), "y": round(e["y"]), "seen": e["seen"]} for e in on_line]
    body["outside"] = [{"name": e["name"], "x": round(e["x"]), "y": round(e["y"]), "seen": e["seen"]} for e in outside]
    try:
        rel = str(scan_dir.relative_to(ROOT))
    except ValueError:
        rel = str(scan_dir)
    body["scan"] = {"dir": rel, "scale": scan_rec["scale"], "C": scan_rec["C"], "screens": len(scan_rec["screens"])}
    p.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  底账已存 {p.name}：{len(inside)} 台" + (f"（线外 {len(outside)}、压线 {len(on_line)} 不收）" if boundary else ""))
    return p


# ------------------------------------------------------------------ ④ 换掉蓝图档案里的底账
def adopt_to_archive(archive: str, area: str, scan_dir: Path, log=print, tol: int = 15) -> dict:
    """用一次新扫描（scan_dir，已 recognize 过）换掉 data/blueprints/<archive>/ledger/<area>.json：
    - 旧文件备份到 ledger/备份/<area>_<旧 updated>.json（配方表同样备份）；
    - 规划线沿用旧底账的 boundary（没有就不分线内外）；
    - <area>_recipes.json 里每条配方按「同名、离旧位置最近、tol px 内、一台只配一条」挂到新底账的设备上，坐标改成新的
      （定向巡检原型按 ≤3 px 找配方，不改坐标会丢）；配不上的列在返回值里。"""
    import shutil
    scan_dir = Path(scan_dir)
    lp = ROOT / "data" / "blueprints" / archive / "ledger" / f"{area}.json"
    rp = lp.with_name(f"{area}_recipes.json")
    old = json.loads(lp.read_text(encoding="utf-8")) if lp.exists() else {}
    bak = lp.parent / "备份"
    bak.mkdir(exist_ok=True)
    tag = old.get("updated") or time.strftime("%Y%m%d-%H%M%S")
    for f in (lp, rp):
        if f.exists():
            shutil.copy2(f, bak / f"{f.stem}_{tag}.json")
    near = json.loads((scan_dir / "ledger_near.json").read_text(encoding="utf-8"))
    save_ledger(archive, area, near, scan_dir, old.get("boundary"), dest=None, log=log)
    new = json.loads(lp.read_text(encoding="utf-8"))
    report = {"devices_old": len(old.get("devices", [])), "devices_new": len(new["devices"]), "recipes": 0, "recipes_lost": []}
    if rp.exists():
        rec = json.loads(rp.read_text(encoding="utf-8"))
        pairs = []
        for i, r in enumerate(rec.get("devices", [])):
            for j, e in enumerate(new["devices"]):
                if e["name"] == r["name"] and abs(e["x"] - r["x"]) <= tol and abs(e["y"] - r["y"]) <= tol:
                    pairs.append((abs(e["x"] - r["x"]) + abs(e["y"] - r["y"]), i, j))
        used_r, used_e = set(), set()
        for _, i, j in sorted(pairs):
            if i in used_r or j in used_e:
                continue
            used_r.add(i); used_e.add(j)
            r, e = rec["devices"][i], new["devices"][j]
            r["x"], r["y"] = e["x"], e["y"]
        report["recipes"] = len(used_r)
        report["recipes_lost"] = [(r["name"], r["x"], r["y"], r.get("recipe")) for i, r in enumerate(rec["devices"]) if i not in used_r]
        rec["devices"] = [r for i, r in enumerate(rec["devices"]) if i in used_r] + [r for i, r in enumerate(rec["devices"]) if i not in used_r]
        rec["ledger"] = f"已挂到 {new.get('updated')} 的底账（坐标随底账更新）"
        rp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  档案底账已换：{area} 旧 {report['devices_old']} 台 → 新 {report['devices_new']} 台；配方 {report['recipes']} 条挂上"
        + (f"，{len(report['recipes_lost'])} 条没对上：{report['recipes_lost']}" if report["recipes_lost"] else "") + f"；旧文件备份在 {bak.relative_to(ROOT)}")
    return report
