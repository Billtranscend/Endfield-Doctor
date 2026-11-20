"""合成小图：照真实拼合图量出来的像素特征现画带子、管子、物流件、设备，给 tests/test_flow_synth.py 用（审计改进第 5 项第 4 步）。
图在测试运行时现画，仓库里不放二进制图；物流件直接贴 resource/bpa/lg 下的模板。文件名以下划线开头，run_all 不会当测试跑。

取样（2026-11-20，皇帝产线三据点的拼合图，只读）：
- 底色 BGR (228,228,230)，灰 229；格线 1 px、灰 218，间距 34.13（追线代码一格按 C=34.75 走，走得越远格中心估得越偏，
  每格约 0.6 px）；拼图左上留边约 21 / 26 px。
- 传送带：黄面掩码 19–20 px 宽，BGR≈(137,242,246)（HSV 中位 29/110/246）；两边各 2 px 灰 190 描边，总宽约 24 px；
  每格一个深灰小三角（灰 111，长 5、底宽 4–5，尖朝流向），三角后面 6–7 px 有一个浅一点的小点；拐弯是圆弧，中线半径约半格。
- 管子：管芯 6 px、灰 61，两边各 2 px 灰 186；每格一个浅色小三角（灰 178，长 5–6、高 5），后面一个灰 105 左右的小点；
  拐弯是圆弧，中线半径约 15。
- 物流件：35×35，直接贴 resource/bpa/lg 下的模板（和 test_bpa.test_logistics_detect_on_synthetic_canvas 一样）。
- 一格设备的圆图标半径 15–16.4（flow.icon_disks 量出来的 r）。
合成图和真图对得上：带子掩码宽 20 px（真图 19–20），管子 6 px（真图 6），圆图标半径 16.2（真图 15–16.4）。

画法：先在 4 倍大的画布上画（线是掩码 + 端头切平），再 INTER_AREA 缩回，斜边、圆弧带上和真图一样的过渡色；物流件模板缩回后再贴。
不画的：两根隔 20 px 的平行管、偏离带子中线 7 px 的管子（取样时的几何错误，不在格上，不代表程序有问题）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src" / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from bpa import vision as V  # noqa: E402

PITCH = 34.13          # 真图的格距
OX, OY = 21.0, 26.0    # 拼图左上留边
BG = (228, 228, 230)
GRIDC = (218, 218, 220)
BELT = (140, 243, 247)
BELT_STRIPE = (128, 242, 246)
BELT_EDGE = (190, 190, 190)
BELT_ARROW = (113, 122, 123)
BELT_DOT = (183, 200, 204)
PIPE = (61, 61, 61)
PIPE_EDGE = (186, 186, 186)
PIPE_ARROW = (178, 178, 178)
PIPE_DOT = (105, 105, 105)
DEV_EDGE = (40, 40, 40)
DEV_FILL = (222, 222, 224)
SS = 4                 # 先画 4 倍大再缩回
LG_DIR = ROOT / "resource" / "bpa" / "lg"


def cell(i, j):
    """格 (i, j) 的中心（缩回后的像素坐标）。"""
    return (OX + (i + 0.5) * PITCH, OY + (j + 0.5) * PITCH)


def edge(i, j, side):
    """格 (i, j) 某条边的中点。"""
    x, y = cell(i, j)
    dx, dy = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}[side]
    return (x + dx * PITCH / 2, y + dy * PITCH / 2)


def rounded(pts, R):
    """折线（只有横竖）→ 圆角后的密集点（按 0.5 px 加密）。"""
    pts = [np.asarray(p, float) for p in pts]
    out = [pts[0]]
    for a, v, b in zip(pts, pts[1:], pts[2:]):
        u = (v - a) / np.hypot(*(v - a))
        w = (b - v) / np.hypot(*(b - v))
        if abs(float(u @ w)) > 0.99 or R <= 0:
            out.append(v)
            continue
        s, e = v - u * R, v + w * R
        c = s + w * R
        a0 = np.arctan2(*(s - c)[::-1])
        a1 = np.arctan2(*(e - c)[::-1])
        da = (a1 - a0 + np.pi) % (2 * np.pi) - np.pi
        out.append(s)
        for t in np.linspace(0, 1, 24)[1:-1]:
            ang = a0 + da * t
            out.append(c + R * np.array([np.cos(ang), np.sin(ang)]))
        out.append(e)
    out.append(pts[-1])
    dense = [out[0]]
    for a, b in zip(out, out[1:]):
        L = float(np.hypot(*(b - a)))
        n = max(1, int(L / 0.5))
        dense += [a + (b - a) * k / n for k in range(1, n + 1)]
    return np.array(dense)


class Canvas:
    def __init__(self, w, h, grid=True):
        self.w, self.h = w, h
        self.big = np.full((h * SS, w * SS, 3), BG, np.uint8)
        if grid:
            for k in range(0, int(w / PITCH) + 2):
                x = int(round((OX + k * PITCH) * SS))
                self.big[:, x:x + SS] = GRIDC
            for k in range(0, int(h / PITCH) + 2):
                y = int(round((OY + k * PITCH) * SS))
                self.big[y:y + SS, :] = GRIDC
        self.pastes = []
        self.items = []
        self.devices = []  # {name, c, rect}

    # ---------------------------------------------------------------- 线
    def _stroke(self, dense, width, cut_ends=True):
        m = np.zeros(self.big.shape[:2], np.uint8)
        P = np.round(dense * SS).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(m, [P], False, 1, max(1, int(round(width * SS))), cv2.LINE_8)
        if cut_ends:  # 端头切平（游戏里线头是方的，贴着设备边 / 物流件边）
            H, W = m.shape
            for p, q in ((dense[0], dense[min(4, len(dense) - 1)]), (dense[-1], dense[max(-5, -len(dense))])):
                t = (np.asarray(p) - np.asarray(q))
                t = t / max(1e-6, np.hypot(*t))
                r = int(width * SS)
                cx, cy = int(p[0] * SS), int(p[1] * SS)
                y0, y1, x0, x1 = max(0, cy - r), min(H, cy + r + 1), max(0, cx - r), min(W, cx + r + 1)
                if y0 >= y1 or x0 >= x1:
                    continue
                yy, xx = np.mgrid[y0:y1, x0:x1]
                d = (xx - p[0] * SS) * t[0] + (yy - p[1] * SS) * t[1]
                m[y0:y1, x0:x1][d > 0] = 0
        return m.astype(bool)

    def _arrows(self, dense, color, dot, L=5.0, Wd=5.0, every=PITCH, first=None):
        seg = np.hypot(*np.diff(dense, axis=0).T)
        s = np.concatenate([[0], np.cumsum(seg)])
        start = every / 2 if first is None else first
        for t0 in np.arange(start, s[-1] - 4, every):
            i = int(np.searchsorted(s, t0))
            i = min(max(i, 1), len(dense) - 2)
            p = dense[i]
            tv = dense[min(i + 4, len(dense) - 1)] - dense[max(i - 4, 0)]
            tv = tv / max(1e-6, np.hypot(*tv))
            nv = np.array([-tv[1], tv[0]])
            tri = np.array([p + tv * L / 2, p - tv * L / 2 + nv * Wd / 2, p - tv * L / 2 - nv * Wd / 2])
            cv2.fillPoly(self.big, [np.round(tri * SS).astype(np.int32)], color, cv2.LINE_8)
            dq = p - tv * (L / 2 + 4)
            cv2.circle(self.big, (int(dq[0] * SS), int(dq[1] * SS)), int(1.0 * SS), dot, -1)

    def belt(self, pts, R=PITCH / 2, arrows=True, stripes=True):
        """传送带。pts：中线折线（流向 = 点的顺序），拐角按半径 R 画圆弧。"""
        d = rounded(pts, R)
        e = self._stroke(d, 24)
        c = self._stroke(d, 20)
        self.big[e] = BELT_EDGE
        if stripes:  # 黄面上的斜纹（真图 B 通道 128–150 交替）
            ys, xs = np.nonzero(c)
            st = ((xs + ys) // (4 * SS)) % 2 == 0
            self.big[ys, xs] = np.where(st[:, None], np.array(BELT, np.uint8), np.array(BELT_STRIPE, np.uint8))
        else:
            self.big[c] = BELT
        if arrows:
            self._arrows(d, BELT_ARROW, BELT_DOT, L=5, Wd=5)
        return d

    def pipe(self, pts, R=15.0, arrows=True, arrow_first=None, core=6):
        """管子。core：管芯宽（真图 6 px；缺陷 c 只在 5 px 时出现）。"""
        d = rounded(pts, R)
        e = self._stroke(d, core + 4)
        c = self._stroke(d, core)
        self.big[e] = PIPE_EDGE
        self.big[c] = PIPE
        if arrows:
            self._arrows(d, PIPE_ARROW, PIPE_DOT, L=5.5, Wd=4.5, first=arrow_first)
        return d

    # ---------------------------------------------------------------- 物流件、设备、圆图标
    def node(self, typ, i, j, rot=0, variant=1, at=None):
        """贴物流件模板（rot：np.rot90 次数，逆时针）。at：直接给中心（缩回后的像素），不按格。"""
        t = V.rd(LG_DIR / f"{typ}_{variant}.png")
        t = np.rot90(t, rot).copy()
        c = cell(i, j) if at is None else at
        self.pastes.append((t, c))
        it = {"type": typ, "p": [int(round(c[0])), int(round(c[1]))]}
        self.items.append(it)
        return it

    def device(self, name, i0, j0, w, h):
        """占 w×h 格的设备：深色外框 3 px + 浅灰底；底账标签放在中心。返回 {name, c, rect}。"""
        x0, y0 = OX + i0 * PITCH, OY + j0 * PITCH
        x1, y1 = x0 + w * PITCH, y0 + h * PITCH
        cv2.rectangle(self.big, (int(x0 * SS), int(y0 * SS)), (int(x1 * SS), int(y1 * SS)), DEV_FILL, -1)
        cv2.rectangle(self.big, (int(x0 * SS) + 2, int(y0 * SS) + 2), (int(x1 * SS) - 2, int(y1 * SS) - 2), DEV_EDGE, 3 * SS)
        c = [(x0 + x1) / 2, (y0 + y1) / 2]
        rect = (int(x0), int(y0), int(x1 - x0), int(y1 - y0))
        self.devices.append({"name": name, "c": c, "rect": rect})
        return self.devices[-1]

    def disk(self, i, j, r=15.5, ring=(40, 200, 160), at=None):
        """一格设备的圆图标（仓库口那种）：深色圆盘 + 彩色外圈。at：直接给圆心。"""
        c = cell(i, j) if at is None else at
        cv2.circle(self.big, (int(c[0] * SS), int(c[1] * SS)), int(r * SS), (35, 35, 35), -1)
        cv2.circle(self.big, (int(c[0] * SS), int(c[1] * SS)), int(r * SS), ring, int(1.5 * SS))
        cv2.circle(self.big, (int(c[0] * SS), int(c[1] * SS)), int(6 * SS), (60, 90, 200), -1)
        return c

    def finish(self):
        """缩回、贴物流件 → BGR 图。画完就不再往上画了：4 倍大的画布释放掉（测试会缓存画布对象，只用 devices / items）。"""
        img = cv2.resize(self.big, (self.w, self.h), interpolation=cv2.INTER_AREA)
        for t, c in self.pastes:
            x, y = int(round(c[0])), int(round(c[1]))
            img[y - 17:y + 18, x - 17:x + 18] = t
        self.big = None
        return img


# -------------------------------------------------------------------- 跑程序的几种入口
def prep(img, items):
    """和 flow._build 开头一样准备 M 和 nodes（不跑追线）：给单独调 CL.walk / FL.trace 用。"""
    from bpa import centerline as CL
    from bpa import flow as FL
    M = FL.masks(img)
    nodes = []
    for i, o in enumerate(items):
        n = dict(o, id=i)
        n.update(FL.orient(img, o))
        nodes.append(n)
    M["icon"], M["disks"] = FL.icon_disks(img, nodes)
    M["storage_line"] = CL.storage_line_icons(img)
    CL.prepare(M, nodes)
    M["img"] = img
    return M, nodes


def build(cv: Canvas, img, use_ports=True, items=None):
    """整条 flow.build（端口布局开着；合成设备的名字不在端口表里 → _DEVS 全是 None）。
    build 会改模块变量 USE_PORTS（这里设的）、_DEVS、PORT_CMP、_DARK_THIN（build 自己改的）：跑完一律还原，不影响同一进程里别的测试。
    items：物流件（不给就用画布上贴的；画完又整体挪过图时传挪过的）。"""
    from bpa import flow as FL
    pts = [{"name": d["name"], "c": list(d["c"])} for d in cv.devices]
    rects = [tuple(d["rect"]) for d in cv.devices]
    old = FL.USE_PORTS, FL._DEVS, list(FL.PORT_CMP), FL._DARK_THIN
    FL.USE_PORTS = use_ports
    try:
        return FL.build(img, cv.items if items is None else items, pts, rects)
    finally:
        FL.USE_PORTS, FL._DEVS, FL._DARK_THIN = old[0], old[1], old[3]
        FL.PORT_CMP[:] = old[2]


def end_a(res, lk) -> str:
    """连线 a 头的简称：物流件 → 「#id类型@边」，设备 → 「名字/认法」。"""
    if lk.get("a") is not None:
        n = res["nodes"][lk["a"]]
        return f"#{lk['a']}{n['type']}@{lk['a_side']}"
    d = lk.get("a_dev")
    return f"{d['name']}/{d.get('how')}" if d else "?"


def end_b(res, lk) -> str:
    if lk.get("b") is not None:
        n = res["nodes"][lk["b"]]
        return f"#{lk['b']}{n['type']}@{lk['b_side']}"
    d = lk.get("device")
    return f"{d['name']}/{d.get('how')}@{lk.get('dev_side')}" if d else "?"


def link_summary(res) -> list[str]:
    """连线 → 简短文字：层、两头、方向、小三角票数、跳过几处、路径点数和两头坐标。失败时打印用。"""
    out = []
    for lk in res["links"]:
        out.append(f"{lk['layer']:4s} {'free ' if lk.get('free') else ''}{end_a(res, lk)} -> {end_b(res, lk)} dir={lk.get('dir')} "
                   f"arrows={lk.get('arrows')} jumps={len(lk.get('jumps', []))} n={len(lk['path'])} "
                   f"{lk['path'][0]}..{lk['path'][-1]}{' bridge' if lk.get('bridge') else ''}")
    return out


def dump(img, tag: str) -> Path:
    """失败时把合成图写到临时目录，返回路径（测试打印出来，方便打开看）。"""
    p = Path(tempfile.gettempdir()) / f"flow_synth_{tag}.png"
    V.wr(p, img)
    return p


def shift_rows(img, x0, x1, off):
    """x0..x1 这几列整体往下挪 off px（拼接时两张蓝图横向错开；挪出去的从上面绕回来，都是底色）。"""
    img[:, x0:x1] = np.roll(img[:, x0:x1].copy(), off, axis=0)


def squeeze_cols(img, x0, dl):
    """x0 往右整体往左挪 dl px（两张蓝图沿走向重叠 dl），右边空出来的补底色。"""
    W = img.shape[1]
    img[:, x0:W - dl] = img[:, x0 + dl:W].copy()
    img[:, W - dl:] = BG


def overlay(img, x0, x1, y0, y1, a=0.55, g=205):
    """半透明浅灰叠层（游戏界面上的浅灰方块）：x0..x1、y0..y1 往灰 g 拉，权重 a。"""
    reg = img[y0:y1, x0:x1].astype(np.float32)
    img[y0:y1, x0:x1] = (reg * (1 - a) + g * a).clip(0, 255).astype(np.uint8)
