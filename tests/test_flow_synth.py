"""追线合成小图测试（审计改进第 5 项第 4 步，2026-11-20）：只测不修。
追线核心（中心线 CL.walk、按格子走的 FL.trace、整条 FL.build、_port_bridges）原来没有换一台机器也能跑的断言：
tests/test_flow.py 全靠本机实测项目，没有就整条跳过。这里在测试运行时照真图量出来的像素特征现画小图（tests/_synth.py），
不读任何本机数据，换机器也能跑。

两个已知缺陷（审计第 7 项）和一处合成时才发现的不对称用 known_defect 锁住现状：现在不挂，修好以后自动报失败，提醒改成正式断言。
另外几条（T4、T5、T9 的追加部分和 T16、T17）专门守住按格子走的 FL.trace 里几个兜底常数：
_inner 的 8、_disk_at 的 tol 12、拐弯偏移 ±9 / −12、_jump_ok 的 4、USE_FAR —— 改小 / 关掉时整份快照和别的可移植测试都看不出来
（评审在三据点上实测），每一处在这里至少有一条断言会失败。断言写的都是现在代码在合成图上的真实结果。
失败时把那张合成图写到临时目录并打印路径。

  .venv/Scripts/python.exe tests/test_flow_synth.py
"""
import contextlib
import functools
import io
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _synth as K  # noqa: E402  （顺带把 src 放进 sys.path）
from _testkit import known_defect, run  # noqa: E402

from bpa import centerline as CL  # noqa: E402
from bpa import flow as FL  # noqa: E402

P = K.PITCH


def _snapshot():
    """测试会碰到的模块变量：测试前后必须一样（最后一条测试核对）。"""
    return {"FL._DEVS": (id(FL._DEVS), list(FL._DEVS)), "FL.USE_PORTS": FL.USE_PORTS,
            "FL.PORT_CMP": (id(FL.PORT_CMP), list(FL.PORT_CMP)), "FL._DARK_THIN": id(FL._DARK_THIN),
            "CL.walk": CL.walk, "FL.trace": FL.trace, "FL.USE_FAR": FL.USE_FAR,
            "FL.USE_CENTERLINE": FL.USE_CENTERLINE, "FL.USE_BELT_UNDER": FL.USE_BELT_UNDER}


_BEFORE = _snapshot()


# ------------------------------------------------------------------ 小工具
@contextlib.contextmanager
def shown(tag, img, info=None):
    """断言失败时：合成图写到临时目录、打印路径和连线摘要，再把失败往外抛。"""
    try:
        yield
    except AssertionError:
        try:
            p = K.dump(img, tag)
            print(f"  合成图（{tag}）：{p}")
            if info and info.get("res") is not None:
                for s in K.link_summary(info["res"]):
                    print("    " + s)
        except Exception as e:  # noqa: BLE001  写图失败不能盖住原来的断言
            print(f"  合成图（{tag}）没写出来：{type(e).__name__} {e}")
        raise


def ends(lk):
    """→ ((a 名, a 认法), (b 名, b 认法))：物流件记成 #id，设备记名字。"""
    if lk.get("a") is not None:
        A = (f"#{lk['a']}", "物流件")
    else:
        d = lk.get("a_dev")
        A = (d["name"], d.get("how")) if d else (None, None)
    if lk.get("b") is not None:
        B = (f"#{lk['b']}", "物流件")
    else:
        d = lk.get("device")
        B = (d["name"], d.get("how")) if d else (None, None)
    return A, B


def flow_of(lk):
    """连线的流向 → (从, 到) 名字；方向没定 → None。"""
    (a, _), (b, _) = ends(lk)
    return (a, b) if lk.get("dir") == "a->b" else (b, a) if lk.get("dir") == "b->a" else None


def find(res, src, dst, layer):
    """流向是 src → dst 的那几条连线。"""
    return [lk for lk in res["links"] if lk["layer"] == layer and flow_of(lk) == (src, dst)]


def touching(res, name, layer):
    return [lk for lk in res["links"] if lk["layer"] == layer and name in (ends(lk)[0][0], ends(lk)[1][0])]


def near(p, q, tol):
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def hits(r, nid):
    """单次追线 r 走到了物流件 nid。"""
    return r["end"] == "node" and r["node"]["id"] == nid


def brief(r):
    t = r["path"][-1]
    return f"end={r['end']} node={r['node']['id'] if r.get('node') else None} d={r['d']} jumps={r['jumps']} tail=({t[0]:.0f},{t[1]:.0f})"


# ------------------------------------------------------------------ 画布（同一张图几条测试共用，只画一次）
@functools.lru_cache(maxsize=None)
def pipes_canvas():
    """管子：直管进设备甲、L 弯进设备乙、U 弯（分流器 → 汇流器）、管道桥十字。"""
    cv = K.Canvas(720, 620)
    cv.device("设备甲", 9, 0, 3, 3)
    cv.device("设备乙", 8, 6, 3, 3)
    cv.node("管道分流器", 1, 1)                                                     # 0
    cv.pipe([K.edge(1, 1, "E"), (K.OX + 9 * P, K.cell(1, 1)[1])])
    cv.node("管道分流器", 1, 4)                                                     # 1
    cv.pipe([K.edge(1, 4, "E"), K.cell(9, 4), (K.cell(9, 4)[0], K.OY + 6 * P)])
    cv.node("管道分流器", 1, 11)                                                    # 2
    cv.node("管道汇流器", 1, 13)                                                    # 3
    cv.pipe([K.edge(1, 11, "E"), K.cell(5, 11), K.cell(5, 13), K.edge(1, 13, "E")])
    cv.node("管道分流器", 12, 11)                                                   # 4
    cv.node("管道桥", 15, 11)                                                       # 5
    cv.node("管道汇流器", 18, 11)                                                   # 6
    cv.node("管道分流器", 15, 8)                                                    # 7
    cv.node("管道汇流器", 15, 14)                                                   # 8
    cv.pipe([K.edge(12, 11, "E"), K.edge(15, 11, "W")])
    cv.pipe([K.edge(15, 11, "E"), K.edge(18, 11, "W")])
    cv.pipe([K.edge(15, 8, "S"), K.edge(15, 11, "N")])
    cv.pipe([K.edge(15, 11, "S"), K.edge(15, 14, "N")])
    img = cv.finish()
    M, nodes = K.prep(img, cv.items)
    return cv, img, M, nodes, K.build(cv, img)


@functools.lru_cache(maxsize=None)
def misc_canvas():
    """圆图标底下直穿 / 拐弯、物流件挨着物流件、管子头离物流件半格、带子从管道准入口底下穿过去拐南。"""
    cv = K.Canvas(800, 640)
    cv.node("管道分流器", 1, 1)                                                     # 0
    cv.node("管道汇流器", 8, 1)                                                     # 1
    cv.pipe([K.edge(1, 1, "E"), K.edge(8, 1, "W")])
    cv.disk(4, 1)
    cv.node("管道分流器", 1, 4)                                                     # 2
    cv.node("管道汇流器", 5, 7)                                                     # 3
    cv.pipe([K.edge(1, 4, "E"), K.cell(5, 4), K.edge(5, 7, "N")])
    cv.disk(5, 4)
    cv.node("分流器", 1, 10)                                                        # 4
    cv.node("汇流器", 2, 10)                                                        # 5
    cv.node("管道分流器", 1, 13)                                                    # 6
    cv.node("管道汇流器", 7, 13)                                                    # 7
    cv.pipe([K.cell(2, 13), K.edge(7, 13, "W")])                                    # 管子头离分流器半格
    cv.node("分流器", 11, 12)                                                       # 8
    g = cv.node("准入口", 14, 12)                                                   # 9
    g["kind"] = "管道"
    cv.belt([K.edge(11, 12, "E"), K.cell(14, 12), (K.cell(14, 12)[0], K.OY + 15 * P)])
    cv.pipe([(K.cell(14, 10)[0], K.OY + 10 * P), K.edge(14, 12, "N")])
    cv.pipe([K.edge(14, 12, "S"), (K.cell(14, 12)[0], K.OY + 14 * P - 2), (K.cell(17, 12)[0], K.OY + 14 * P - 2)])
    cv.device("准入口下游", 13, 15, 3, 2)
    img = cv.finish()
    M, nodes = K.prep(img, cv.items)
    return cv, img, M, nodes, K.build(cv, img)


@functools.lru_cache(maxsize=None)
def belts_canvas(with_pipes=False):
    """带子：直带进设备、L 弯进设备、U 弯（分流器 → 汇流器）、物流桥十字。with_pipes：再画一根横穿直带的竖管、
    一根居中顺着压在 L 弯横段上 4 格的管子（T7）。"""
    cv = K.Canvas(760, 640)
    cv.device("设备直", 10, 0, 3, 3)
    cv.device("设备L", 8, 5, 3, 3)
    cv.node("分流器", 1, 1)                                                         # 0
    cv.belt([K.edge(1, 1, "E"), (K.OX + 10 * P, K.cell(1, 1)[1])])
    cv.node("分流器", 1, 4)                                                         # 1
    cv.belt([K.edge(1, 4, "E"), K.cell(9, 4), (K.cell(9, 4)[0], K.OY + 5 * P)])
    cv.node("分流器", 1, 9)                                                         # 2
    cv.node("汇流器", 1, 11)                                                        # 3
    cv.belt([K.edge(1, 9, "E"), K.cell(5, 9), K.cell(5, 11), K.edge(1, 11, "E")])
    cv.node("分流器", 13, 10)                                                       # 4
    cv.node("物流桥", 16, 10)                                                       # 5
    cv.node("汇流器", 19, 10)                                                       # 6
    cv.node("分流器", 16, 7)                                                        # 7
    cv.node("汇流器", 16, 13)                                                       # 8
    cv.belt([K.edge(13, 10, "E"), K.edge(16, 10, "W")])
    cv.belt([K.edge(16, 10, "E"), K.edge(19, 10, "W")])
    cv.belt([K.edge(16, 7, "S"), K.edge(16, 10, "N")])
    cv.belt([K.edge(16, 10, "S"), K.edge(16, 13, "N")])
    if with_pipes:
        cv.pipe([(K.cell(5, 0)[0], 2), (K.cell(5, 0)[0], K.OY + 3 * P - 4)])
        cv.pipe([(K.cell(3, 3)[0], K.OY + 3 * P - 20), (K.cell(3, 3)[0], K.cell(3, 4)[1]), (K.cell(7, 4)[0], K.cell(3, 4)[1]),
                 (K.cell(7, 4)[0], K.OY + 3 * P - 20)])
    img = cv.finish()
    M, nodes = K.prep(img, cv.items)
    return cv, img, M, nodes, K.build(cv, img)


# ------------------------------------------------------------------ 管子
def test_t01_straight_pipe():
    """T1 直管：管道分流器东边 → 设备甲西边。中心线走到设备边上（线头离外框 ≤6 px），按外框认出设备甲。"""
    cv, img, M, nodes, res = pipes_canvas()
    info = {"res": res}
    with shown("T1_直管", img, info):
        r = CL.walk(M, nodes, nodes[0], "E", "pipe")
        assert r["end"] == "device" and r.get("tip") and r["d"] == "E", brief(r)
        assert abs(r["path"][-1][0] - (K.OX + 9 * P)) <= 6, brief(r)
        lk = find(res, "#0", "设备甲", "pipe")
        assert len(lk) == 1 and ends(lk[0])[1] == ("设备甲", "外框") and lk[0]["dev_side"] == "E", K.link_summary(res)


def test_t02_pipe_bends():
    """T2 管子 L 弯（往东拐南进设备乙顶边）、U 弯（分流器 → 汇流器，两头互相追得回来）；7 段管子的中心线是 7 块，没有断的。"""
    cv, img, M, nodes, res = pipes_canvas()
    info = {"res": res}
    with shown("T2_管子弯", img, info):
        r = CL.walk(M, nodes, nodes[1], "E", "pipe")
        assert r["end"] == "device" and r["d"] == "S" and abs(r["path"][-1][1] - (K.OY + 6 * P)) <= 6, brief(r)
        lk = find(res, "#1", "设备乙", "pipe")
        assert len(lk) == 1 and ends(lk[0])[1] == ("设备乙", "外框") and lk[0]["dev_side"] == "S", K.link_summary(res)
        assert hits(CL.walk(M, nodes, nodes[2], "E", "pipe"), 3)
        assert hits(CL.walk(M, nodes, nodes[3], "E", "pipe"), 2)
        lk = find(res, "#2", "#3", "pipe")
        assert len(lk) == 1 and lk[0]["both"], K.link_summary(res)
        n_cc = cv2.connectedComponents(M["cl"]["pipe"].astype(np.uint8), connectivity=8)[0] - 1
        assert n_cc == 7, n_cc


def test_t03_pipe_bridge_cross():
    """T3 管道桥十字：横的、竖的各自直着穿过桥，连线经过桥的中心，没有连线停在桥上。"""
    cv, img, M, nodes, res = pipes_canvas()
    info = {"res": res}
    bridge = nodes[5]["c"]
    with shown("T3_管道桥", img, info):
        for a, b in (("#4", "#6"), ("#7", "#8")):
            lk = find(res, a, b, "pipe")
            assert len(lk) == 1 and lk[0]["both"], (a, b, K.link_summary(res))
            assert any(near(q, bridge, 3) for q in lk[0]["path"]), (a, b, lk[0]["path"])
        assert not touching(res, "#5", "pipe"), K.link_summary(res)
        assert not res["issues"], res["issues"]


def _disk_row(k, turn):
    """管道分流器 (1,1) 往东 k 格到圆图标底下，直穿到汇流器 (k+4,1)，或在图标底下拐南进汇流器 (k+1,4)。"""
    W = int(K.OX + (k + 5) * P) + 10
    cv = K.Canvas(W, 260)
    cv.node("管道分流器", 1, 1)
    if turn:
        cv.node("管道汇流器", 1 + k, 4)
        cv.pipe([K.edge(1, 1, "E"), K.cell(1 + k, 1), K.edge(1 + k, 4, "N")])
    else:
        cv.node("管道汇流器", 1 + k + 3, 1)
        cv.pipe([K.edge(1, 1, "E"), K.edge(1 + k + 3, 1, "W")])
    c = cv.disk(1 + k, 1)
    img = cv.finish()
    M, nodes = K.prep(img, cv.items)
    return img, M, nodes, c


def test_t04_pipe_under_round_icon():
    """T4 管子从一格设备的圆图标底下直穿 / 在底下拐弯：从对面（拐过去的那边）接着走，跳过的地方记在图标中心。
    反例：图标两侧对称各伸出一根管子（北、南），说不清往哪边，不拐。
    按格子走的 FL.trace（中心线断了时的兜底）同样要过：图标离出发的物流件 8 格、14 格时，格中心已经估偏了好几 px，
    要靠 _disk_at 的 12 px 容差认出图标（改成 8 px 时拐不过去、直穿也不再记跳过）。"""
    cv, img, M, nodes, res = misc_canvas()
    info = {"res": res}
    with shown("T4_圆图标", img, info):
        r = CL.walk(M, nodes, nodes[0], "E", "pipe")
        assert hits(r, 1) and len(r["jumps"]) == 1 and near(r["jumps"][0], K.cell(4, 1), 3), brief(r)
        r = CL.walk(M, nodes, nodes[2], "E", "pipe")
        assert hits(r, 3) and r["side"] == "N" and len(r["jumps"]) == 1 and near(r["jumps"][0], K.cell(5, 4), 3), brief(r)
        for a, b in (("#0", "#1"), ("#2", "#3")):
            lk = find(res, a, b, "pipe")
            assert len(lk) == 1 and len(lk[0]["jumps"]) == 1, (a, b, K.link_summary(res))
    # 反例：对称的两个出口
    cv = K.Canvas(320, 300)
    cv.node("管道分流器", 1, 3)
    cv.node("管道汇流器", 5, 0)
    cv.node("管道汇流器", 5, 6)
    cv.pipe([K.edge(1, 3, "E"), K.cell(5, 3)])
    cv.pipe([K.cell(5, 3), K.edge(5, 0, "S")], arrows=False)
    cv.pipe([K.cell(5, 3), K.edge(5, 6, "N")], arrows=False)
    cv.disk(5, 3)
    img2 = cv.finish()
    M2, nodes2 = K.prep(img2, cv.items)
    with shown("T4_对称出口", img2):
        r = CL.walk(M2, nodes2, nodes2[0], "E", "pipe")
        assert r["end"] != "node", brief(r)
        assert r["d"] == "E" and r.get("disk"), brief(r)
    # 按格子走的兜底
    for k, turn in ((8, True), (8, False), (14, True)):
        img3, M3, nodes3, c = _disk_row(k, turn)
        with shown(f"T4_格子走_k{k}_{'拐' if turn else '直'}", img3):
            r = FL.trace(M3, nodes3, nodes3[0], "E", "pipe")
            assert hits(r, 1) and r["side"] == ("N" if turn else "W"), (k, turn, brief(r))
            assert len(r["jumps"]) == 1 and near(r["jumps"][0], c, 3), (k, turn, brief(r))


def _overlay_rows():
    """四行：管道分流器 (1,j) → 管道汇流器 (10,j)，叠层都在第 5 格（离分流器 4 格）。
    j=1：40 px 宽的半透明浅灰叠层（管子变成灰 140 左右）；
    j=4：同样宽、更不透明的叠层（管子灰 170 左右，pipe_soft 也认不出，整段看不见）；
    j=7：同样宽的一段管子整个被抹成底色（白，不是叠层）；
    j=10：60 px 宽的半透明叠层（比 _jump_ok 隔一格能跳的宽，按格子走必须一格一格看过去）。"""
    cv = K.Canvas(420, 440)
    for j in (1, 4, 7, 10):
        cv.node("管道分流器", 1, j)
        cv.node("管道汇流器", 10, j)
        cv.pipe([K.edge(1, j, "E"), K.edge(10, j, "W")])
    img = cv.finish()
    xc = int(K.cell(5, 1)[0])
    for j, wd, a, g in ((1, 40, 0.55, 205), (4, 40, 0.7, 215), (10, 60, 0.55, 205)):
        y = int(K.cell(5, j)[1])
        K.overlay(img, xc - wd // 2, xc - wd // 2 + wd, y - 30, y + 30, a, g)
    y = int(K.cell(5, 7)[1])
    img[y - 12:y + 13, xc - 20:xc + 20] = K.BG
    M, nodes = K.prep(img, cv.items)
    return cv, img, M, nodes


def test_t05_light_gray_overlay_gap():
    """T5 管子被半透明浅灰叠层盖住一段（游戏界面上的浅灰方块）：中心线照样过去；整段看不见时沿直线跨过去（_gap_ahead）。
    反例：缺口 >26 px 而且下面是白底（不是叠层）时不跨。
    按格子走的 FL.trace 靠 USE_FAR（要停下之前再往外多看几条横截线、叠层底下用 pipe_soft）一格一格走过 60 px 宽的叠层，
    两个方向都到、不靠跳（关掉 USE_FAR 时两个方向都停在叠层前）。"""
    cv, img, M, nodes = _overlay_rows()
    with shown("T5_叠层", img):
        r = CL.walk(M, nodes, nodes[0], "E", "pipe")
        assert hits(r, 1), brief(r)
        r = CL.walk(M, nodes, nodes[2], "E", "pipe")
        assert hits(r, 3) and len(r["jumps"]) >= 1, brief(r)
        r = CL.walk(M, nodes, nodes[4], "E", "pipe")
        assert not hits(r, 5), brief(r)                 # 反例：白底的长缺口不跨
        for a, d, b in ((6, "E", 7), (7, "W", 6)):
            r = FL.trace(M, nodes, nodes[a], d, "pipe")
            assert hits(r, b) and r["jumps"] == [], (d, brief(r))


# ------------------------------------------------------------------ 带子
def test_t06_belts():
    """T6 带子：直带进设备（东边）、L 弯进设备（南边进顶边）、U 弯、物流桥十字。5 条连线全对（两头、接在哪边、流向），issues 为空。"""
    cv, img, M, nodes, res = belts_canvas(False)
    info = {"res": res}
    with shown("T6_带子", img, info):
        want = [("#0", "设备直", "E"), ("#1", "设备L", "S")]
        for a, b, side in want:
            lk = find(res, a, b, "belt")
            assert len(lk) == 1 and ends(lk[0])[1] == (b, "外框") and lk[0]["dev_side"] == side, (a, b, K.link_summary(res))
        for a, b in (("#2", "#3"), ("#4", "#6"), ("#7", "#8")):
            lk = find(res, a, b, "belt")
            assert len(lk) == 1 and lk[0]["both"], (a, b, K.link_summary(res))
        bridge = nodes[5]["c"]
        assert all(any(near(q, bridge, 3) for q in lk["path"]) for lk in find(res, "#4", "#6", "belt") + find(res, "#7", "#8", "belt"))
        assert len([lk for lk in res["links"] if lk["layer"] == "belt"]) == 5, K.link_summary(res)
        assert not res["issues"], res["issues"]


def test_t07_pipe_over_belt():
    """T7 管压带：一根竖管横穿直带、一根管子居中顺着压在 L 弯的横段上 4 格。带子照样追到设备（USE_BELT_UNDER 那条兜底）。"""
    cv, img, M, nodes, res = belts_canvas(True)
    info = {"res": res}
    with shown("T7_管压带", img, info):
        for a, b, side in (("#0", "设备直", "E"), ("#1", "设备L", "S")):
            lk = find(res, a, b, "belt")
            assert len(lk) == 1 and ends(lk[0])[1] == (b, "外框") and lk[0]["dev_side"] == side, (a, b, K.link_summary(res))
        r = FL.trace(M, nodes, nodes[1], "E", "belt")          # 没停在管子底下：一直走到拐角那一格（往哪边进设备由 _end_info 判）
        assert r["end"] == "device" and near(r["path"][-1], K.cell(9, 4), 8), brief(r)
        assert not res["issues"], res["issues"]


def test_t08_belt_under_pipe_gate():
    """T8 带子从管道准入口底下穿过去、在底下拐南进设备：按外框认出设备（接在南边），跳过的地方是准入口。"""
    cv, img, M, nodes, res = misc_canvas()
    info = {"res": res}
    gate = nodes[9]["c"]
    with shown("T8_准入口底下", img, info):
        r = FL.trace(M, nodes, nodes[8], "E", "belt")
        assert r["end"] == "device" and r["d"] == "S" and any(near(j, gate, 2) for j in r["jumps"]), brief(r)
        lk = find(res, "#8", "准入口下游", "belt")
        assert len(lk) == 1 and ends(lk[0])[1] == ("准入口下游", "外框") and lk[0]["dev_side"] == "S", K.link_summary(res)
        assert any(near(j, gate, 2) for j in lk[0]["jumps"]), lk[0]["jumps"]
        # 物流件挨着物流件（中间没有带子）：直接相连
        lk = find(res, "#4", "#5", "belt")
        assert len(lk) == 1, K.link_summary(res)


def _seam(lay, sx, off, gap):
    """分流器 (1,2) → 汇流器 (10,2) 一条直线；第 sx 格中间偏右 5 px 一道接缝：右边（到汇流器前）整体往下错开 off px，
    接缝处再留 gap px 宽的底色（两张蓝图之间没画到）。"""
    cv = K.Canvas(420, 160)
    ta, tb = ("分流器", "汇流器") if lay == "belt" else ("管道分流器", "管道汇流器")
    cv.node(ta, 1, 2)
    cv.node(tb, 10, 2)
    (cv.belt if lay == "belt" else cv.pipe)([K.edge(1, 2, "E"), K.edge(10, 2, "W")])
    img = cv.finish()
    xs = int(K.cell(sx, 2)[0]) + 5
    K.shift_rows(img, xs, int(K.cell(10, 2)[0]) - 18, off)
    if gap:
        img[:, xs:xs + gap] = K.BG
    M, nodes = K.prep(img, cv.items)
    go = FL.trace if lay == "belt" else CL.walk
    return img, go(M, nodes, nodes[0], "E", lay), go(M, nodes, nodes[1], "W", lay)


def test_t09_seams():
    """T9 拼接接缝：带子（FL.trace）横向错开 0/4/8/10 px、管子（CL.walk）错开 0/2/4 px，接缝处空 0/3/6 px，两个方向都追得到。
    极限值（带子错开 12、管子错开 6 / 8）只打印、不断言。
    接缝落在汇流器前一格时（带子错开 8 / 10 px，空 0 / 3 px）：最后一格里带子已经偏了 8 px 左右，
    要靠 _inner 的 8 px 容差认出「这一格的带子确实伸向汇流器」（改成 6 px 时停在汇流器前面）。"""
    for lay, offs in (("belt", (0, 4, 8, 10)), ("pipe", (0, 2, 4))):
        for off in offs:
            for gap in (0, 3, 6):
                img, r1, r2 = _seam(lay, 5, off, gap)
                with shown(f"T9_{lay}_off{off}_gap{gap}", img):
                    assert hits(r1, 1), (lay, off, gap, brief(r1))
                    assert hits(r2, 0), (lay, off, gap, brief(r2))
    for off in (8, 10):
        for gap in (0, 3):
            img, r1, _ = _seam("belt", 9, off, gap)
            with shown(f"T9_汇流器前一格_off{off}_gap{gap}", img):
                assert hits(r1, 1), (off, gap, brief(r1))
    lim = []
    for lay, off in (("belt", 12), ("pipe", 6), ("pipe", 8)):
        _, r1, r2 = _seam(lay, 5, off, 0)
        lim.append(f"{lay} 错开 {off}：{'过' if hits(r1, 1) else '断'}/{'过' if hits(r2, 0) else '断'}")
    print("  接缝极限（只打印，不断言）：" + "；".join(lim))


@functools.lru_cache(maxsize=None)
def dev2dev_canvas():
    """设备 → 设备（不经过物流件，free_segments）：甲东边 → 直带 → 乙西边；丙东边 → 往东再拐南 → 丁顶边。
    右下角放一个不相干的分流器：带子种子按最近物流件的格相位对齐（真图里到处都有物流件）。"""
    cv = K.Canvas(560, 520)
    cv.device("甲", 1, 1, 3, 3)
    cv.device("乙", 9, 1, 3, 3)
    cv.device("丙", 1, 6, 3, 3)
    cv.device("丁", 8, 10, 3, 3)
    cv.node("分流器", 14, 13)
    cv.belt([(K.OX + 4 * P, K.cell(4, 2)[1]), (K.OX + 9 * P, K.cell(4, 2)[1])])
    cv.belt([(K.OX + 4 * P, K.cell(4, 7)[1]), K.cell(9, 7), (K.cell(9, 7)[0], K.OY + 10 * P)])
    img = cv.finish()
    return cv, img, K.build(cv, img)


def test_t10_device_to_device_belts():
    """T10 设备 → 设备的带子（直带、L 弯）：两头都按外框认出，流向按小三角（甲 → 乙、丙 → 丁），不多出别的连线。"""
    cv, img, res = dev2dev_canvas()
    info = {"res": res}
    with shown("T10_设备到设备", img, info):
        for a, b in (("甲", "乙"), ("丙", "丁")):
            lk = find(res, a, b, "belt")
            assert len(lk) == 1 and lk[0].get("free"), (a, b, K.link_summary(res))
            assert {h for _, h in ends(lk[0])} == {"外框"}, (a, b, K.link_summary(res))
        assert len(res["links"]) == 2, K.link_summary(res)
        assert not res["issues"], res["issues"]


# ------------------------------------------------------------------ 已知缺陷
@functools.lru_cache(maxsize=None)
def corner_seed_canvas(layer):
    """审计第 7 项缺陷 (a) 的样子：甲（右上）西边出来往西走到拐角 (4,2)，往南两格，再往东进乙西边；
    丙是诱饵，在拐角正南方、底账标签离拐角最近。另一条对照：甲底边往南、拐西进丁东边（L 弯）。layer：belt / pipe。"""
    cv = K.Canvas(560, 420)
    cv.device("甲", 12, 1, 3, 3)
    cv.device("乙", 6, 3, 3, 3)
    cv.device("丙", 1, 3, 3, 3)
    cv.node("分流器" if layer == "belt" else "管道分流器", 13, 9)   # 格相位参照
    draw = cv.belt if layer == "belt" else cv.pipe
    draw([(K.OX + 12 * P, K.cell(4, 2)[1]), K.cell(4, 2), K.cell(4, 4), (K.OX + 6 * P, K.cell(4, 4)[1])])
    if layer == "belt":
        cv.device("丁", 7, 6, 3, 3)
        cv.belt([(K.cell(13, 4)[0], K.OY + 4 * P), K.cell(13, 7), (K.OX + 10 * P, K.cell(13, 7)[1])])
    img = cv.finish()
    return cv, img, K.build(cv, img)


def test_t11_corner_seed_belt():
    """T11 缺陷 (a)：带子的种子落在拐角格（按格相位对齐后离拐角中心偏 5 px），往西 0 步就停，
    多出一条「丙（最近标签）↔ 甲」的假连线，和正确的「甲 → 乙」重叠（带子层不去重）。
    正式断言：「甲 → 乙」在、方向对、两头按外框认出；「甲 → 丁」也对。缺陷部分用 known_defect 锁住。"""
    cv, img, res = corner_seed_canvas("belt")
    info = {"res": res}
    with shown("T11_拐角种子_带子", img, info):
        lk = find(res, "甲", "乙", "belt")
        assert len(lk) == 1 and {h for _, h in ends(lk[0])} == {"外框"} and lk[0]["dev_side"] == "E", K.link_summary(res)
        lk = find(res, "甲", "丁", "belt")
        assert len(lk) == 1 and {h for _, h in ends(lk[0])} == {"外框"} and lk[0]["dev_side"] == "W", K.link_summary(res)

    def fixed():
        bait = touching(res, "丙", "belt")
        assert not bait, "诱饵丙：" + "；".join(f"{a[0]}/{a[1]} - {b[0]}/{b[1]}" for a, b in map(ends, bait))
        assert len(touching(res, "乙", "belt")) == 1, K.link_summary(res)
    known_defect("a 种子落在拐角格（带子）", fixed)


def _l_mask(w, R, H=160, W=160, ss=8):
    """一根 L 形管子的布尔掩码（不画图）：横段往东到 (100,60)，按中线半径 R 拐南；管芯宽 w。"""
    big = np.zeros((H * ss, W * ss), np.uint8)
    d = K.rounded([(20, 60), (100, 60), (100, 150)], R)
    cv2.polylines(big, [np.round(d * ss).astype(np.int32).reshape(-1, 1, 2)], False, 255, int(round(w * ss)), cv2.LINE_8)
    return cv2.resize(big, (W, H), interpolation=cv2.INTER_AREA) >= 128


def _centerline_blocks(m):
    L = CL.line_masks({"pipe": m, "pipe_soft": m, "belt": np.zeros_like(m)}, [], [])
    return cv2.connectedComponents(CL.thin(L["pipe"]).astype(np.uint8), connectivity=8)[0] - 1


def test_t12_pipe_corner_opening():
    """T12 缺陷 (c)：管子拐角被 line_masks 的 5×5 开运算切断（centerline.py 先闭 3×3 再开 5×5）。只在管芯 5 px 时出现。
    正式断言：6 px 宽（真图的管芯）是 1 块；和管子平行的 3 px 设备外框线经 line_masks 后剩 0 像素
    （修 c 时必须守住的反例：直接换成 3×3 开运算会剩 540 像素）。缺陷部分（5 px、R=15/17 断成 2 块）用 known_defect 锁住。"""
    for R in (15, 17):
        m6 = _l_mask(6, R)
        with shown(f"T12_6px_R{R}", m6.astype(np.uint8) * 255):
            assert _centerline_blocks(m6) == 1, R
    m = np.zeros((120, 200), bool)
    m[40:46, 10:190] = True      # 管子 6 px
    m[58:61, 10:190] = True      # 设备外框线 3 px，隔 12 px
    L = CL.line_masks({"pipe": m, "pipe_soft": m, "belt": np.zeros_like(m)}, [], [])
    with shown("T12_外框线", m.astype(np.uint8) * 255):
        assert int(L["pipe"][56:63].sum()) == 0, int(L["pipe"][56:63].sum())
        assert int(L["pipe"][38:48].sum()) > 0

    def fixed():
        got = {R: _centerline_blocks(_l_mask(5, R)) for R in (15, 17)}
        assert all(v == 1 for v in got.values()), f"5 px 宽的 L 形拐角，中心线块数 {got}（应为 1）"
    known_defect("c 管子拐角被开运算切断", fixed)


def test_t13_corner_seed_pipe():
    """T13 同一走向换成管子（设备 → 设备、管子拐角）：中心线在拐角会转弯，只出 1 条「甲 → 乙」，没有诱饵丙。"""
    cv, img, res = corner_seed_canvas("pipe")
    info = {"res": res}
    with shown("T13_拐角种子_管子", img, info):
        pipes = [lk for lk in res["links"] if lk["layer"] == "pipe"]
        assert len(pipes) == 1 and flow_of(pipes[0]) == ("甲", "乙"), K.link_summary(res)
        assert {h for _, h in ends(pipes[0])} == {"外框"}, K.link_summary(res)
        assert not touching(res, "丙", "pipe"), K.link_summary(res)


def test_t14_pipe_head_half_cell_off():
    """T14 管子头离物流件半格（管子从隔壁那格的中心才开始画）：正向（从分流器出发）走到汇流器。
    反向（从汇流器往回）现在走到离分流器还差 17 px 就停了：centerline.py 只在出发端处理半格（walk 开头），
    走到头时 _nodes_hit 只往前探 12 px。合成时发现的，真图里出现过没有还没查，修不修待定：用 known_defect 锁住。"""
    cv, img, M, nodes, res = misc_canvas()
    info = {"res": res}
    with shown("T14_半格", img, info):
        r = CL.walk(M, nodes, nodes[6], "E", "pipe")
        assert hits(r, 7), brief(r)
        assert find(res, "#6", "#7", "pipe"), K.link_summary(res)

    def fixed():
        r = CL.walk(M, nodes, nodes[7], "W", "pipe")
        assert hits(r, 6), "反向没走到分流器：" + brief(r)
    known_defect("半格 管子头离物流件半格（反向，合成发现，修不修待定）", fixed)


# ------------------------------------------------------------------ 端口之间按黄带补的短带
def _dev(name, box, ports):
    x, y, w, h = box
    return {"name": name, "c": [x + w / 2, y + h / 2], "box": [x, y, w, h], "ports": ports}


def _port(edge, side, role):
    """和 ports.all_ports 给的一样（edge 是元组：_port_bridges 拿它当字典键）。"""
    return {"edge": (float(edge[0]), float(edge[1])), "side": side, "role": role, "pipe": False}


def _bridges(img, devs):
    old = FL._DEVS
    FL._DEVS = devs
    try:
        return FL._port_bridges(FL.masks(img), [], [])
    finally:
        FL._DEVS = old


def test_t15_port_bridges_along_yellow():
    """T15 两台设备的带子口之间只隔 1–2 格、追线没追出来的短带：_port_bridges 按黄带一格一格走补上（端口布局是假的，带子是画的）。
    一格弯带：甲东边的出口 → 那一格里拐南 → 乙顶边的进口；U 形短带：甲底边的出口往南一格、往东一格、往北进丙底边的进口。
    各恰好 1 条，依据里写「格短带」。反例：并排两条竖带（中间是浅色缝）不连。"""
    # 一格弯带：甲占 (1..3, 1..3) 格，东边出口在第 2 行；乙占 (4..6, 3..5) 格，顶边进口在第 4 列。
    # 出口格外面那一格 (4,2) 同时是乙进口格外面那一格
    cv = K.Canvas(320, 300)
    a = _dev("甲", (K.OX + 1 * P, K.OY + 1 * P, 3 * P, 3 * P), [_port(K.edge(3, 2, "E"), "E", "out")])
    b = _dev("乙", (K.OX + 4 * P, K.OY + 3 * P, 3 * P, 3 * P), [_port(K.edge(4, 3, "N"), "N", "in")])
    cv.belt([K.edge(3, 2, "E"), K.cell(4, 2), K.edge(4, 3, "N")])
    img = cv.finish()
    with shown("T15_一格弯带", img):
        lk = _bridges(img, [a, b])
        assert len(lk) == 1 and lk[0]["a_dev"]["name"] == "甲" and lk[0]["device"]["name"] == "乙", lk
        assert "1 格短带" in lk[0]["basis"][0], lk[0]["basis"]
    # U 形短带：甲占 (0..1, 1..2) 格、丙占 (2..3, 1..2) 格，挨着；两个口都在底边（第 1、2 列）
    cv = K.Canvas(320, 300)
    a = _dev("甲", (K.OX, K.OY + 1 * P, 2 * P, 2 * P), [_port(K.edge(1, 2, "S"), "S", "out")])
    c = _dev("丙", (K.OX + 2 * P, K.OY + 1 * P, 2 * P, 2 * P), [_port(K.edge(2, 2, "S"), "S", "in")])
    cv.belt([K.edge(1, 2, "S"), K.cell(1, 3), K.cell(2, 3), K.edge(2, 2, "S")])
    img = cv.finish()
    with shown("T15_U形短带", img):
        lk = _bridges(img, [a, c])
        assert len(lk) == 1 and lk[0]["a_dev"]["name"] == "甲" and lk[0]["device"]["name"] == "丙", lk
        assert "2 格短带" in lk[0]["basis"][0], lk[0]["basis"]
    # 反例：两个口各自往南一条竖带，并排，中间隔着浅色缝
    cv = K.Canvas(320, 300)
    cv.belt([K.edge(1, 2, "S"), (K.cell(1, 6)[0], K.cell(1, 6)[1])])
    cv.belt([(K.cell(2, 6)[0], K.cell(2, 6)[1]), K.edge(2, 2, "S")])
    img = cv.finish()
    with shown("T15_并排不连", img):
        assert _bridges(img, [a, c]) == []


# ------------------------------------------------------------------ 按格子走的 FL.trace：格距累计误差、跨过另一层的物流件
def _squeezed_turn(lay, k, dl, turn):
    """分流器 (1,3) 往东 k 格到拐角、拐 turn（S / N）两格进汇流器；第 2 格中间一道接缝：右边整体往左挪 dl px
    （两张蓝图沿走向重叠，格距累计误差）。返回 (img, FL.trace 从分流器往东的结果)。"""
    W = int(K.OX + (k + 4) * P) + 10
    cv = K.Canvas(W, 300)
    ta, tb = ("分流器", "汇流器") if lay == "belt" else ("管道分流器", "管道汇流器")
    cv.node(ta, 1, 3)
    jb = 5 if turn == "S" else 1
    cv.node(tb, 1 + k, jb)
    (cv.belt if lay == "belt" else cv.pipe)([K.edge(1, 3, "E"), K.cell(1 + k, 3), K.edge(1 + k, jb, "N" if turn == "S" else "S")])
    img = cv.finish()
    K.squeeze_cols(img, int(K.cell(2, 3)[0]), dl)
    items = [cv.items[0], dict(cv.items[1], p=[cv.items[1]["p"][0] - dl, cv.items[1]["p"][1]])]
    M, nodes = K.prep(img, items)
    return img, FL.trace(M, nodes, nodes[0], "E", lay)


def test_t16_turn_after_squeezed_seam():
    """T16 按格子走的 FL.trace：过一道沿走向重叠 12 px 的接缝（拼图格距累计误差，不同蓝图之间差 6–12 px）、再走 7 格拐弯进汇流器，
    往南拐、往北拐，带子、管子都拐得过去。这时格中心和弯道沿走向错开了，要靠拐弯时把格中心前后挪着找（flow.py 拐弯那段的偏移表）。
    实测起作用的是往前挪 +9：去掉 ±9 / −12 时四种都停在拐角；只去掉 −9 / −12 时这条不失败——那两个值目前没有测试守住。"""
    for lay in ("belt", "pipe"):
        for turn in ("S", "N"):
            img, r = _squeezed_turn(lay, 8, 12, turn)
            with shown(f"T16_{lay}_{turn}", img):
                assert hits(r, 1) and r["d"] == turn, (lay, turn, brief(r))


def _hop(off):
    """管道分流器 (1,2) → 管道汇流器 (10,2) 一根直管，中间 (4,2) 压着一个带子分流器（管子从上面跨过去）；
    分流器那一格往右整体往下错开 off px（拼接错位正好在它底下）。"""
    cv = K.Canvas(420, 160)
    cv.node("管道分流器", 1, 2)
    cv.node("管道汇流器", 10, 2)
    cv.node("分流器", 4, 2)
    cv.pipe([K.edge(1, 2, "E"), K.edge(10, 2, "W")])
    img = cv.finish()
    K.shift_rows(img, int(K.cell(4, 2)[0]), int(K.cell(10, 2)[0]) - 18, off)
    M, nodes = K.prep(img, cv.items)
    return img, M, nodes


def test_t17_pipe_hops_over_belt_node():
    """T17 按格子走的 FL.trace：管子从带子分流器上面跨过去（_jump_ok：隔一格还是同一条线就跳过去），
    过去以后横向错开 2–3 px（接缝正好在分流器底下）照样跳，两个方向都到对面，跳过的地方记在分流器上。
    （_jump_ok 的横向容差 4 改成 2 时这几种都停在分流器前面。）"""
    for off in (2, 3):
        img, M, nodes = _hop(off)
        with shown(f"T17_跨分流器_off{off}", img):
            for a, d, b in ((0, "E", 1), (1, "W", 0)):
                r = FL.trace(M, nodes, nodes[a], d, "pipe")
                assert hits(r, b), (off, d, brief(r))
                assert len(r["jumps"]) == 1 and near(r["jumps"][0], nodes[2]["c"], 6), (off, d, brief(r))


# ------------------------------------------------------------------ 自测与收尾
def test_known_defect_selftest():
    """known_defect 本身：check 不抛错 → AssertionError（缺陷不在了，提醒改成正式断言）；
    check 抛 AssertionError → 打印「已知缺陷 <tag>：仍在（…）」、不算失败（这里把打印截下来，不算进已知缺陷的行数）；别的异常照常往外抛。"""
    try:
        known_defect("自测", lambda: None)
    except AssertionError as e:
        assert "已不再出现" in str(e) and "known_defect" in str(e), str(e)
    else:
        raise AssertionError("check 不抛错时 known_defect 没报失败")

    def still():
        assert False, "还是两块"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        known_defect("自测", still)
    out = buf.getvalue()
    assert out == "已知缺陷 自测：仍在（还是两块）\n", repr(out)

    def broken():
        raise KeyError("x")
    try:
        known_defect("自测", broken)
    except KeyError:
        pass
    else:
        raise AssertionError("别的异常被 known_defect 吞掉了")


def test_zz_module_globals_restored():
    """最后一条：前面的测试改过的模块变量（FL._DEVS / USE_PORTS / PORT_CMP / _DARK_THIN、CL.walk、FL.trace 和几个开关）都还原了。"""
    now = _snapshot()
    bad = [k for k in _BEFORE if now[k] != _BEFORE[k]]
    assert not bad, bad


TESTS = [test_t01_straight_pipe, test_t02_pipe_bends, test_t03_pipe_bridge_cross, test_t04_pipe_under_round_icon,
         test_t05_light_gray_overlay_gap, test_t06_belts, test_t07_pipe_over_belt, test_t08_belt_under_pipe_gate,
         test_t09_seams, test_t10_device_to_device_belts, test_t11_corner_seed_belt, test_t12_pipe_corner_opening,
         test_t13_corner_seed_pipe, test_t14_pipe_head_half_cell_off, test_t15_port_bridges_along_yellow,
         test_t16_turn_after_squeezed_seam, test_t17_pipe_hops_over_belt_node, test_known_defect_selftest,
         test_zz_module_globals_restored]   # 最后一条必须在最后

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="backslashreplace")  # 直接在 GBK 控制台里跑时，打印不出的字符不要把测试打断
    except (AttributeError, ValueError):
        pass
    assert set(TESTS) == {v for k, v in globals().items() if k.startswith("test_")}, "TESTS 漏了测试"
    sys.exit(run(TESTS))
