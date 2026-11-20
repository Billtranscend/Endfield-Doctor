"""追线三处小修（2026-11-19 审计第 2 项，别人的武陵城）：
中心线找种子不再被「.5 取偶」卡在偶数行；管子的种子不按格相位挪；带子头旁边（不是正上方）压着管子时只收进料口。
不依赖本机 debug 数据。"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import centerline as CL  # noqa: E402
from bpa import flow as FL  # noqa: E402
from bpa import vision as V  # noqa: E402

C = V.CELL


def test_seed_finds_a_centerline_on_an_odd_row():
    """种子点横向对中后落在 .5 上（6 px 宽的管子量出来的中心都是 .5）：1 px 中心线在奇数行也要找得到。
    原来 round(20.5±l) 全取到偶数行，中心线在第 21 行就一直找不到（别人的武陵城种植机 (2473,1192) 那一圈锦草线）。"""
    S = np.zeros((40, 60), bool)
    S[21, :] = True
    q = CL._seed(S, (10.0, 20.5), (1, 0))
    assert q is not None and int(q[1]) == 21 and int(q[0]) == 12, q
    S[:] = False                     # 竖着走、中心线在奇数列
    S[:, 31] = True
    q = CL._seed(S, (30.5, 5.0), (0, 1))
    assert q is not None and int(q[0]) == 31, q
    # 反例：偶数行照样找得到；超出横向 ±9 的线不认
    S[:] = False
    S[20, :] = True
    q = CL._seed(S, (10.0, 20.5), (1, 0))
    assert q is not None and int(q[1]) in (20, 21) and S[int(q[1]), int(q[0])], q
    S[:] = False
    S[33, :] = True
    assert CL._seed(S, (10.0, 20.5), (1, 0)) is None


def _seeds(masks):
    """free_segments 里每个种子交给追线的起点（追线换成只记录、原地不动的假追线）。物流件在 (31,31)，格相位按它算。"""
    got = []

    def fake(M, nodes, start, d, lay, *a, **k):
        got.append((lay, tuple(start["c"])))
        return {"path": [list(start["c"])], "end": "device", "d": d}
    old = FL.trace, CL.walk
    FL.trace, CL.walk = fake, fake
    try:
        M = {"gray": np.full((200, 200), 232, np.uint8), "belt": masks.get("belt", np.zeros((200, 200), bool)),
             "pipe": masks.get("pipe", np.zeros((200, 200), bool))}
        FL.free_segments(M, [{"id": 0, "type": "分流器", "c": [31.0, 31.0]}], [], [], [])
    finally:
        FL.trace, CL.walk = old
    return got


def test_pipe_seed_is_not_moved_to_the_cell_phase():
    """管子的种子只横向对中、不沿走向挪到格中心（挪了会把种子挪离 6 px 宽的管子，短管、拐角附近追不出来）。"""
    pipe = np.zeros((200, 200), bool)
    pipe[97:103, 20:181] = True      # 横着的管子，6 px 宽，中心 99.5
    got = [c for lay, c in _seeds({"pipe": pipe}) if lay == "pipe"]
    assert got, got
    assert all(c[1] == 99.5 and (c[0] - 6) % 12 == 0 for c in got), got   # 就在格点上，没往物流件的格相位挪
    # 反例：带子照旧对齐到格中心（带子按格子走，要靠格相位拐弯）
    belt = np.zeros((200, 200), bool)
    belt[140:160, :] = True          # 横着的带子，20 px 宽，中心 149.5（贯穿整张图：不要带子头上的竖向种子）
    got = [c for lay, c in _seeds({"belt": belt}) if lay == "belt" and c[1] == 149.5]  # 只看顺着带子走的种子（图边上截断的横截线另算）
    assert len(got) >= 4, got
    k = [(c[0] - 31) / C for c in got]
    assert all(abs(v - round(v)) < 1e-6 for v in k), got


def _dev(name, c, ports):
    return {"name": name, "c": list(c), "box": [c[0] - 2 * C, c[1] - 2 * C, 4 * C, 4 * C], "ports": ports}


def _port(edge, side, role):
    return {"edge": [float(edge[0]), float(edge[1])], "side": side, "role": role, "pipe": False}


def _end(devs, old_dev, head_dark=False, pipe_near=True, yellow_east=True, dev_edge=False):
    """带子头在 (300,300)、往南走到头，原来的认法给出 old_dev。pipe_near：带子头东边 8 px 有一根竖着的管子（不压在头上）。
    dev_edge：头的南边有一条 6 px 宽、横着的长黑边（设备进出料口的粗黑边），和东边那块黄带隔着 2 px 浅色缝。"""
    g = np.full((600, 600), 232, np.uint8)
    belt = np.zeros((600, 600), bool)
    pipe = np.zeros((600, 600), bool)
    if yellow_east:
        belt[292:309, 307:318] = True            # 东侧 12 px 处有黄带：带子往东拐
    if pipe_near:
        pipe[280:321, 306:312] = True
    if dev_edge:
        pipe[311:317, 280:330] = True           # 黄带到第 308 行为止，309–310 是浅色缝
        g[311:317, 280:330] = 40
    if head_dark:
        g[290:311, 290:311] = 60                # 管子正压在带子头上
    M = {"gray": g, "belt": belt, "pipe": pipe}
    pts = [{"name": d["name"], "c": d["c"]} for d in devs if d] + [{"name": old_dev["name"], "c": old_dev["c"]}]
    old = FL._DEVS, FL._end_info_old, list(FL.PORT_CMP)
    FL._DEVS = devs
    FL._end_info_old = lambda *a: ("S", dict(old_dev))
    try:
        return FL._end_info(M, "belt", {"end": "device", "path": [[300, 250], [300, 300]], "d": "S"}, pts, [])
    finally:
        FL._DEVS, FL._end_info_old = old[0], old[1]
        FL.PORT_CMP[:] = old[2]


def test_belt_head_beside_a_pipe_takes_only_the_side_in_port():
    """带子头不在管子正中、旁边 12 px 内有管子（追线停在拐角点上，偏出了管子中线）：只收端口布局命中的进料口。
    别人的武陵城 (301,610) 那条碳块带拐过去进天有洪炉 (409,714)，原来按线框认成左边的采种机，天有洪炉没碳块。"""
    furnace = _dev("天有洪炉", (300 + 3 * C, 300 + 2 * C), [_port((300 + 0.7 * C, 302), "W", "in")])
    picker = {"name": "采种机", "c": [300 - 3 * C, 300 + C], "how": "线框"}
    e, dv = _end([furnace], picker)
    assert e == "E" and dv["name"] == "天有洪炉" and dv["how"] == "端口布局" and dv["port"] == "in", (e, dv)  # 接在带子东边
    # 反例 1：旁边没有管子 → 照原来的认法
    assert _end([furnace], picker, pipe_near=False) == ("S", picker)
    # 反例 1b：旁边只有设备口的粗黑边、没有管子 → 照原来的认法。窗口里深色像素有 50 个（原来只数深色、不看压没压在黄带上，就过门了），
    # 但黑边和黄带之间隔着浅色缝，一个都不压在黄带上（2026-11-19 评审：皇帝景玉谷 (166,31) 研磨机口那种）
    assert _end([furnace], picker, pipe_near=False, dev_edge=True) == ("S", picker)
    # 反例 2：那边对着的是出口 → 不收
    out = _dev("天有洪炉", furnace["c"], [_port((300 + 0.7 * C, 302), "W", "out")])
    assert _end([out], picker) == ("S", picker)
    # 反例 3：那边的设备没有端口布局 → 不往那边探着找设备（主分支才会）
    assert _end([None], picker) == ("S", picker)
    # 反例 4：原来已经按外框认准了 → 不动
    strong = dict(picker, how="外框")
    assert _end([furnace], strong) == ("S", strong)
    # 对照：管子正压在带子头上（主分支）照旧可以往那边探着找设备
    e, dv = _end([None, _dev("天有洪炉", furnace["c"], [])], picker, head_dark=True)
    assert e == "E" and dv["name"] == "天有洪炉", (e, dv)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for f in tests:
        try:
            f(); ok += 1
        except AssertionError as e:
            print("FAIL", f.__name__, str(e)[:300])
    print(f"{ok}/{len(tests)} 通过")
    sys.exit(0 if ok == len(tests) else 1)
