"""据点拼合：各张蓝图的格线对齐到同一套格子上（2026-11-14 issue #1，「武陵左上」「武陵左中」拼接处错开）。

游戏里所有蓝图在同一套建造格子上，蓝图之间的相对位置应该正好是整数格。拼合时每张蓝图是各自按锚点（暗管、供电桩）
和底账拟合的，底账有几个像素的误差，接缝处就错开：他那两张差 (6.5, −2.6) px，本地皇帝产线三个据点也有 3–8 px。
预览底色上有网格线（约 229 的底色上 2 px 宽、暗 5–10 的细线，间距 34.13 px，一条线 = 一格的边），
拼合时让各张的格线重合（vision.grid_shifts），每张最多挪半格；对齐后各据点格线相差 ≤ 0.8 px。

顺带量出来的：格线间距是 34.13，不是 CELL 的 34.75。拼图 = 格数 × 34.13 + 固定留边，CELL 是把留边摊进去的近似。
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import vision as V  # noqa: E402
from _testkit import run, skip  # noqa: E402

P = V.GRID_PITCH


def sheet(w: int, h: int, px: float, py: float, blobs=()) -> np.ndarray:
    """造一张预览拼图：底色 229，从 (px, py) 起每隔一格画 2 px 宽的格线（224）；blobs = 设备（深色方块 x, y, 边长）。"""
    im = np.full((h, w, 3), 229, np.uint8)
    for k in range(int(w / P) + 2):
        x = int(round(px + k * P))
        if 0 <= x < w - 1:
            im[:, x:x + 2] = 224
    for k in range(int(h / P) + 2):
        y = int(round(py + k * P))
        if 0 <= y < h - 1:
            im[y:y + 2] = 224
    for x, y, s in blobs:
        im[y:y + s, x:x + s] = (60, 60, 60)
    return im


def wrap(v: float) -> float:
    return (v + P / 2) % P - P / 2


# ---------------------------------------------------------------- 认格线

def test_grid_lines_and_phase():
    g = cv2.cvtColor(sheet(700, 500, 12.0, 20.0, blobs=[(100, 100, 120), (400, 250, 90)]), cv2.COLOR_BGR2GRAY)
    xs, ys = V.grid_lines(g, 0), V.grid_lines(g, 1)
    assert len(xs) >= 18 and len(ys) >= 12, (len(xs), len(ys))
    (phx, fx), (phy, fy) = V.grid_phase(xs), V.grid_phase(ys)
    assert abs(wrap(phx - 12.5)) <= 0.6 and abs(wrap(phy - 20.5)) <= 0.6, (phx, phy)  # 2 px 宽的线，中心在 +0.5
    assert fx > 0.95 and fy > 0.95


def test_no_grid_means_no_lines():
    """反例：没有格线的图（纯底色、或全是设备）认不出线，集中度是 0。"""
    g = np.full((400, 600), 229, np.uint8)
    assert len(V.grid_lines(g, 0)) == 0 and V.grid_phase(V.grid_lines(g, 0)) == (0.0, 0.0)
    assert len(V.grid_lines(np.full((400, 600), 60, np.uint8), 1)) == 0


def test_device_edges_are_not_grid_lines():
    """反例：设备的黑边、深色块不是格线（比底色暗得多）。"""
    im = np.full((400, 600, 3), 229, np.uint8)
    for x in range(40, 560, 37):
        im[:, x:x + 2] = 120
    assert len(V.grid_lines(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), 0)) == 0


# ---------------------------------------------------------------- 该挪多少

def ph(x, y, n=30, focus=1.0):
    return [(x, focus, n), (y, focus, n)]


def test_shifts_bring_grids_together():
    """两张蓝图：原点差 (1000 + 6.5, 300 − 2.6) px、相位一样 → 各挪一半，方向相反，挪完格线重合。"""
    o = [np.array([0.0, 0.0]), np.array([29 * P + 6.5, 9 * P - 2.6])]
    mv = V.grid_shifts(o, [ph(10, 10), ph(10, 10)])
    for ax, want in ((0, 6.5), (1, -2.6)):
        assert abs(mv[0][ax] - want / 2) < 0.05 and abs(mv[1][ax] + want / 2) < 0.05, (ax, mv)
        assert abs(wrap((o[1][ax] + mv[1][ax]) - (o[0][ax] + mv[0][ax]))) < 0.05


def test_phase_difference_is_respected():
    """反例：原点正好差整数格，但两张拼图自己的格线相位不一样（拼图左上角不在格线上）：按相位挪，不是按原点。"""
    o = [np.array([0.0, 0.0]), np.array([20 * P, 0.0])]
    mv = V.grid_shifts(o, [ph(5, 5), ph(11, 5)])
    assert abs((mv[0][0] - mv[1][0]) - 6.0) < 0.05 and abs(mv[0][1]) < 0.01 and abs(mv[1][1]) < 0.01, mv


def test_already_aligned_is_left_alone():
    o = [np.array([3.0, 7.0]), np.array([3.0 + 12 * P, 7.0 - 4 * P]), np.array([3.0 - 5 * P, 7.0 + 20 * P])]
    assert all(np.abs(m).max() < 0.01 for m in V.grid_shifts(o, [ph(8, 9)] * 3))


def test_weak_grid_is_not_used_or_moved():
    """反例：格线认不够（线太少 / 不集中）的蓝图不参加、也不挪；只剩一张认得出的时候谁都不挪。"""
    o = [np.array([0.0, 0.0]), np.array([10 * P + 8.0, 0.0]), np.array([20 * P + 4.0, 0.0])]
    mv = V.grid_shifts(o, [ph(10, 10), ph(10, 10, n=3), ph(10, 10)])
    assert np.abs(mv[1]).max() == 0 and abs(mv[0][0] - 2.0) < 0.05 and abs(mv[2][0] + 2.0) < 0.05, mv
    mv = V.grid_shifts(o[:2], [ph(10, 10), ph(10, 10, focus=0.3)])
    assert all(np.abs(m).max() == 0 for m in mv)


# ---------------------------------------------------------------- 整个拼合

def test_composite_aligns_two_blueprints():
    """两张蓝图，拟合出的平移差了 (5, −3) 预览 px：拼出来格线重合，transform 里的 t 和画布偏移对得上。"""
    a = sheet(700, 520, 10.0, 14.0, blobs=[(200, 200, 100)])
    b = sheet(620, 520, 3.0, 22.0, blobs=[(300, 150, 100)])
    # 真实位置：b 的格线和 a 的重合，b 在 a 右边 21 格；再给 b 的拟合平移加上 (5, −3) px 的误差
    true_b = np.array([21 * P + (10.0 - 3.0), 14.0 - 22.0])
    t_a, t_b = np.array([500.0, 300.0]), np.array([500.0, 300.0]) + V.K * (true_b + np.array([5.0, -3.0]))
    canvas, tf = V.composite([{"bp": "甲", "mosaic": a, "rot": 0, "t": t_a.tolist()}, {"bp": "乙", "mosaic": b, "rot": 0, "t": t_b.tolist()}])
    oa, ob = np.array(tf["bps"]["甲"]["offset"]), np.array(tf["bps"]["乙"]["offset"])
    assert np.abs((ob - oa) - true_b).max() <= 1.0, (ob - oa, true_b)  # 画布上的相对位置回到真实值（取整到像素）
    for bp, t0 in (("甲", t_a), ("乙", t_b)):
        e = tf["bps"][bp]
        assert np.allclose(e["t_fit"], t0) and np.abs(np.array(e["grid_shift"])).max() <= 3.1
        c = V.preview_to_canvas(tf, bp, a.shape, 0, 0)  # t 是对齐后的：预览原点换到画布 = 画布偏移
        assert np.abs(np.array(c) - np.array(e["offset"])).max() <= 0.51, (bp, c, e["offset"])
    # 乙的格线落在甲的格线上：乙的画布偏移 + 它自己的相位，和甲的差整数格
    for ax, (pa, pb) in enumerate(((10.5, 3.5), (14.5, 22.5))):
        assert abs(wrap((ob[ax] + pb) - (oa[ax] + pa))) <= 1.0, (ax, oa, ob)
    assert canvas.shape[1] >= ob[0] + b.shape[1] and canvas.shape[0] >= b.shape[0]


def test_single_blueprint_is_unchanged():
    """反例：只有一张蓝图，没有可对齐的：平移不变。"""
    a = sheet(600, 500, 10.0, 14.0, blobs=[(200, 200, 100)])
    _, tf = V.composite([{"bp": "甲", "mosaic": a, "rot": 0, "t": [500.0, 300.0]}])
    assert tf["bps"]["甲"]["t"] == [500.0, 300.0] and tf["bps"]["甲"]["grid_shift"] == [0.0, 0.0]


def test_local_bases_align_within_a_pixel():
    """本机皇帝产线实测项目（有数据才跑）：景玉谷两张蓝图按原来拟合的平移重新拼合，格线相差 ≤ 1 px（原来 4.8 px）。"""
    proj = ROOT / "debug" / "bpa" / "实测-皇帝产线"
    tfp = proj / "base" / "景玉谷" / "transform.json"
    if not tfp.exists():
        skip("本机没有实测项目景玉谷的拼合结果")
    tf0 = json.loads(tfp.read_text(encoding="utf-8"))
    items = []
    for bp, b in tf0["bps"].items():
        f = proj / "bp" / bp / "mosaic.png"
        if not f.exists():
            skip(f"实测项目里没有 {bp} 的拼图")
        items.append({"bp": bp, "mosaic": V.rd(f), "rot": b["rot"], "t": b.get("t_fit", b["t"])})
    if len(items) < 2:
        skip("实测项目景玉谷不到两张蓝图，没有可对齐的")
    _, tf = V.composite(items)
    rot = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    q = []
    for it in items:
        r = rot[it["rot"]]
        g = cv2.cvtColor(cv2.rotate(it["mosaic"], r) if r is not None else it["mosaic"], cv2.COLOR_BGR2GRAY)
        q.append([(tf["bps"][it["bp"]]["offset"][ax] + V.grid_phase(V.grid_lines(g, ax))[0]) % P for ax in (0, 1)])
        assert np.abs(np.array(tf["bps"][it["bp"]]["grid_shift"])).max() < P / 4
    assert max(abs(wrap(q[1][ax] - q[0][ax])) for ax in (0, 1)) <= 1.0, q


if __name__ == "__main__":
    sys.exit(run([v for k, v in list(globals().items()) if k.startswith("test_")]))
