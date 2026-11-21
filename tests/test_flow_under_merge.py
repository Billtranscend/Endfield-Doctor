"""issue #1 第五轮（续）flow 的两处（2026-11-21，别人的武陵城还剩 5 台没料里归 flow 的两条）：
1. 带子从紧贴着的管道层物流件底下穿过去（flow._under_first、trace 的 hop0）：息壤物品准入口 (拼合图 2418,1600) 北边正前方一格
   就是壤晶废液管道准入口 (2420,1565)，带子从它底下穿过去进扩容反应池（底账 (1729,1156)）南口。原来 _build 一见 _blocked 就整边跳过，
   扩容反应池缺息壤。在合成小图上跑整条 flow.build。
2. 重叠的两截管子拼回一根（flow._merge_overlap）：提纯机 (底账 1454,1136) → 反应池 (804,1215) 的壤晶废液管被两个种子各追了一截，
   两截在一段横管上重叠约 640 px，断头各被「最近标签（远）」认成不相干的设备；反应池和下游封装机 (655,1284) 没料。
   几何部分不用图（照真图的形状缩成整格坐标）；最后一条把两截塞进整条 flow.build，核对调用位置在 _dedupe_free 前面。
不依赖本机 debug 数据。

  .venv/Scripts/python.exe tests/test_flow_under_merge.py
"""
import functools
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _synth as K  # noqa: E402  （顺带把 src 放进 sys.path）
from _testkit import run  # noqa: E402

from bpa import flow as FL  # noqa: E402

C = FL.C
P = K.PITCH


def _globals():
    return {"FL._DEVS": (id(FL._DEVS), list(FL._DEVS)), "FL.USE_PORTS": FL.USE_PORTS,
            "FL.PORT_CMP": (id(FL.PORT_CMP), list(FL.PORT_CMP)), "FL._DARK_THIN": id(FL._DARK_THIN),
            "FL.free_segments": FL.free_segments}


_BEFORE = _globals()


# ------------------------------------------------------------------ 1. 带子从管道层物流件底下穿过去
@functools.lru_cache(maxsize=None)
def gate_canvas(block="准入口", north_belt=True, east_belt=False):
    """照别人的武陵城那一处画：池子（5×3 格）南边一格带子 ← 管道准入口 #0（管子从西边接管源）← 物品准入口 #1 ← 料源。
    block：挡在物品准入口北边的那个物流件；north_belt：管道准入口北边还有没有带子；east_belt：管道准入口东边再伸出一条带子（两个出口）。"""
    cv = K.Canvas(380, 380)
    cv.device("池子", 3, 0, 5, 3)
    cv.device("管源", 0, 3, 2, 3)
    cv.device("料源", 4, 8, 3, 2)
    top = cv.node(block, 5, 4)                                                       # 0
    if block == "准入口":
        top["kind"] = "管道"
    g = cv.node("准入口", 5, 5)                                                      # 1
    g["kind"] = "物品"
    cv.belt([(K.cell(5, 8)[0], K.OY + 8 * P), K.edge(5, 5, "S")])                   # 料源 → 物品准入口
    if north_belt:                                                                   # 物品准入口 → 管道准入口底下 → 池子南边
        cv.belt([K.edge(5, 5, "N"), (K.cell(5, 3)[0], K.OY + 3 * P)])
    cv.pipe([(K.OX + 2 * P, K.cell(0, 4)[1]), K.edge(5, 4, "W")])                    # 管源 → 管道准入口
    if east_belt:
        cv.device("东边", 8, 3, 2, 3)
        cv.belt([K.edge(5, 4, "E"), (K.OX + 8 * P, K.cell(5, 4)[1])])
    img = cv.finish()
    M, nodes = K.prep(img, cv.items)
    return cv, img, M, nodes, K.build(cv, img)


def _gate_n(res):
    """物品准入口 #1 北边出去的带子。"""
    return [lk for lk in res["links"] if lk["layer"] == "belt" and lk.get("a") == 1 and lk["a_side"] == "N"]


def _show(tag, img, res):
    return f"{K.dump(img, tag)}\n" + "\n".join(K.link_summary(res))


def test_belt_under_pipe_gate_from_first_step():
    """物品准入口北边紧贴着管道准入口：带子第一步就从管道准入口底下穿过去，接到池子南边（外框），方向 准入口 → 池子，跳过的地方是管道准入口。"""
    cv, img, M, nodes, res = gate_canvas()
    lk = _gate_n(res)
    assert len(lk) == 1, _show("穿底_正例", img, res)
    lk = lk[0]
    dv = lk.get("device") or {}
    assert dv.get("name") == "池子" and dv.get("how") == "外框" and lk["dev_side"] == "N", _show("穿底_正例", img, res)
    assert lk["dir"] == "a->b", _show("穿底_正例", img, res)
    top = nodes[0]["c"]
    assert any(abs(j[0] - top[0]) <= 2 and abs(j[1] - top[1]) <= 2 for j in lk["jumps"]), lk["jumps"]
    assert FL._under_first(M, nodes, nodes[1], np.array(nodes[1]["c"], float), "N", "belt")
    # 管道准入口自己（只在管道层）、管道层：都不走这条
    assert not FL._under_first(M, nodes, nodes[0], np.array(nodes[0]["c"], float), "S", "belt")
    assert not FL._under_first(M, nodes, nodes[1], np.array(nodes[1]["c"], float), "N", "pipe")
    # 料源那边照旧
    assert any(lk["a"] == 1 and lk["a_side"] == "S" and (lk.get("device") or {}).get("name") == "料源" for lk in res["links"]), \
        _show("穿底_正例", img, res)


def test_trace_first_step_needs_hop0():
    """单独调追线：不给 hop0 时第一步不穿（原来的行为，free_segments 的种子也一样），给了才穿。"""
    cv, img, M, nodes, res = gate_canvas()
    r = FL.trace(M, nodes, nodes[1], "N", "belt")
    assert r["end"] == "device" and len(r["path"]) == 1, r
    r = FL.trace(M, nodes, nodes[1], "N", "belt", hop0=True)
    assert r["end"] == "device" and r["d"] == "N" and len(r["path"]) >= 3, r


def test_no_hop_under_pipe_bridge():
    """挡着的是管道桥：不穿（桥不当准入口那样的物流件看），物品准入口北边没有连线，池子也不从这边接带子。"""
    cv, img, M, nodes, res = gate_canvas(block="管道桥")
    assert not _gate_n(res), _show("穿底_管道桥", img, res)
    assert not [lk for lk in res["links"] if lk["layer"] == "belt" and "池子" in
                ((lk.get("device") or {}).get("name"), (lk.get("a_dev") or {}).get("name"))], _show("穿底_管道桥", img, res)
    assert not FL._under_first(M, nodes, nodes[1], np.array(nodes[1]["c"], float), "N", "belt")


def test_no_hop_without_exit():
    """管道准入口底下穿过去哪边都没有带子：不穿，物品准入口北边没有连线（和原来一样）。"""
    cv, img, M, nodes, res = gate_canvas(north_belt=False)
    assert not _gate_n(res), _show("穿底_没出口", img, res)
    assert not FL._under_first(M, nodes, nodes[1], np.array(nodes[1]["c"], float), "N", "belt")


def test_no_hop_with_two_exits():
    """管道准入口底下穿过去北边、东边都露出带子：说不准从哪边出去，不穿。"""
    cv, img, M, nodes, res = gate_canvas(east_belt=True)
    assert not _gate_n(res), _show("穿底_两个出口", img, res)
    assert not FL._under_first(M, nodes, nodes[1], np.array(nodes[1]["c"], float), "N", "belt")


# ------------------------------------------------------------------ 2. 重叠的两截管子拼回一根
G = 2 * C  # 整格坐标原点留边


def _xy(i, j):
    return [int(round(G + i * C)), int(round(G + j * C))]


def _dev(name, c, how):
    return {"name": name, "c": list(c), "how": how}


def _seg(path, head, tail, layer="pipe", head_side="E", tail_side="S", jumps=()):
    return {"layer": layer, "free": True, "a": None, "a_side": None, "end": "device", "a_end": "device",
            "a_dev": head, "a_dev_side": head_side, "device": tail, "dev_side": tail_side,
            "path": [list(p) for p in path], "jumps": [list(j) for j in jumps]}


FAR = "最近标签（远）"
POOL = _dev("反应池", _xy(1, 11), "最近标签")       # 2.5 格内的最近标签：真图上进反应池那头就是这样
PURI = _dev("提纯机", _xy(22, 9), "外框")


def _pair(a_weak=None, b_weak=None, layer="pipe", b_dy=0, a_from=20, b_from=4):
    """照真图缩成整格：A（像 L335）从东北拐角 (a_from,2) 往西走到 (4,2)、往南进反应池；B（像 L336）从西北拐角 (b_from,2) 往东
    走到 (20,2)、往南进提纯机。两截在 y=2 那段横管上重叠。a_weak / b_weak：断头那头认出的东西（默认「最近标签（远）」认成别的设备）。
    b_dy：B 的横段整体挪开几 px（不是同一根）。"""
    A = _seg([_xy(a_from, 2), _xy(13, 2), _xy(4, 2), _xy(4, 9), _xy(2, 10)],
             _dev("封装机", _xy(21, 0), FAR) if a_weak is None else a_weak, POOL, layer=layer, jumps=[_xy(13, 2)])
    by = lambda p: [p[0], p[1] + b_dy]  # noqa: E731
    B = _seg([by(_xy(b_from, 2)), by(_xy(13, 2)), by(_xy(20, 2)), _xy(20, 7)],
             _dev("粉碎机", _xy(4, 0), FAR) if b_weak is None else b_weak, PURI, layer=layer, jumps=[_xy(13, 2)])
    return A, B


def _ends(lk):
    return {(lk["a_dev"] or {}).get("name"), (lk["device"] or {}).get("name")}


def test_merge_two_overlapping_pipe_halves():
    """正例：拼成一根 反应池 ↔ 提纯机，路径从反应池那头沿 A 走到 B 的断头、再沿 B 走到提纯机；两个远认的错设备都不留。"""
    A, B = _pair()
    out = FL._merge_overlap([A, B])
    assert len(out) == 1, out
    m = out[0]
    assert _ends(m) == {"反应池", "提纯机"} and m["a_dev"]["name"] == "反应池", m
    assert m["a_dev_side"] == "S" and m["dev_side"] == "S", m        # 两头各带着原来那头的 side
    assert m["path"][0] == _xy(2, 10) and m["path"][-1] == _xy(20, 7), m["path"]
    assert _xy(4, 2) in m["path"] and _xy(20, 2) in m["path"], m["path"]
    assert m["jumps"] == [_xy(13, 2)], m["jumps"]                    # 两截都记了同一处，只留一个
    assert m.get("overlap_merged") and m["layer"] == "pipe" and m.get("free"), m
    # 换个顺序、把 B 倒过来存（断头在 path 末尾）：结果一样
    B2 = dict(B, path=B["path"][::-1], a_dev=B["device"], a_dev_side=B["dev_side"], device=B["a_dev"], dev_side=B["a_dev_side"])
    out = FL._merge_overlap([B2, A])
    assert len(out) == 1 and _ends(out[0]) == {"反应池", "提纯机"}, out
    # 断头没认出（None）也一样拼
    A, B = _pair(a_weak=None, b_weak=None)
    A["a_dev"], B["a_dev"] = None, None
    out = FL._merge_overlap([A, B])
    assert len(out) == 1 and _ends(out[0]) == {"反应池", "提纯机"}, out


def test_merge_ignores_far_bridge_but_not_bridge_at_joint():
    """重叠段中间经过一个管道桥（真图 (1430,1156)，离断头 66 px）照样拼；断头 20 px 内有管道桥 / 物流桥就不拼（桥上两根十字交叉）。"""
    A, B = _pair()
    far = [{"id": 0, "type": "管道桥", "c": _xy(6, 2)}]
    assert len(FL._merge_overlap([A, B], far)) == 1
    for typ in ("管道桥", "物流桥"):
        for at in (_xy(20, 2), _xy(4, 2)):
            nodes = [{"id": 0, "type": typ, "c": [at[0] + 12, at[1] + 8]}]
            assert len(FL._merge_overlap([A, B], nodes)) == 2, (typ, at)
    # 别的物流件（不是桥）在断头旁边：不管
    assert len(FL._merge_overlap([A, B], [{"id": 0, "type": "管道分流器", "c": _xy(20, 3)}])) == 1


def test_merge_only_pipes():
    A, B = _pair(layer="belt")
    assert len(FL._merge_overlap([A, B])) == 2
    A, B = _pair()
    assert len(FL._merge_overlap([A, dict(B, layer="belt")])) == 2


def test_merge_needs_one_sure_one_loose_end_each():
    """两截各要「一头认出了设备、一头没认出或远认」。"""
    A, B = _pair()
    assert len(FL._merge_overlap([dict(A, device=_dev("反应池", _xy(1, 11), FAR)), B])) == 2   # A 两头都远认
    assert len(FL._merge_overlap([dict(A, device=None), B])) == 2                              # A 两头都没认出
    assert len(FL._merge_overlap([A, dict(B, a_dev=_dev("粉碎机", _xy(4, 0), "外框"))])) == 2   # B 两头都认准了
    assert len(FL._merge_overlap([A, dict(B, a_dev=_dev("粉碎机", _xy(4, 0), "最近标签"))])) == 2


def test_merge_sure_ends_must_differ():
    A, B = _pair()
    assert len(FL._merge_overlap([A, dict(B, device=dict(POOL))])) == 2


def test_merge_needs_weak_end_on_the_other_path():
    """B 的横段挪开 12 px（两根并排的管子，不是同一根）：断头不在对方路径上（> 10 px），不拼；挪 6 px 还算同一根。"""
    A, B = _pair(b_dy=12)
    assert len(FL._merge_overlap([A, B])) == 2
    A, B = _pair(b_dy=6)
    assert len(FL._merge_overlap([A, B])) == 1
    # 横段完全重合，只是 A 的断头最后往北拐出去 14 px（拐去了别处，不在 B 上）：不拼；拐出去 6 px 还在 B 上
    for hook, n in ((14, 2), (6, 1)):
        A, B = _pair()
        A["path"] = [[A["path"][0][0], A["path"][0][1] - hook]] + A["path"]
        assert len(FL._merge_overlap([A, B])) == n, hook


def test_merge_needs_one_cell_collinear_overlap():
    """只是两头碰到、重叠不到一格：不拼（那种是 _stitch 的事）。"""
    A, B = _pair(a_from=4.5, b_from=4)   # A 的断头在 (4.5,2)，B 从 (4,2) 起：重叠半格
    assert len(FL._merge_overlap([A, B])) == 2
    A, B = _pair(a_from=5.5, b_from=4)   # 重叠一格半
    assert len(FL._merge_overlap([A, B])) == 1


def test_merge_rejects_crossing_not_collinear():
    """两截各自的断头都落在对方路径上，但中间走的不是同一条线（围成一个圈）：不拼。"""
    A = _seg([_xy(4, 12), _xy(4, 2), _xy(16, 2)], POOL, _dev("封装机", _xy(17, 0), FAR), head_side="S", tail_side="E")
    B = _seg([_xy(4, 6), _xy(16, 6), _xy(16, 2), _xy(16, -2)], _dev("粉碎机", _xy(3, 6), FAR), PURI, head_side="W", tail_side="N")
    assert FL._foot(A["path"][-1], B["path"])[0] <= 10 and FL._foot(B["path"][0], A["path"])[0] <= 10
    assert len(FL._merge_overlap([A, B])) == 2


def test_build_merges_before_dedupe():
    """整条 flow.build（空白图，free_segments 换成直接给这两截）：拼回一根，而且在 _dedupe_free 前面拼——
    B 有 80% 以上落在 A 上，先去重的话 B 会被丢掉，留下 A 一头挂着远认的封装机。"""
    A, B = _pair(b_from=4)
    B["path"] = [B["path"][0], B["path"][2], _xy(20, 3)]               # B 往南只伸出一格：大半落在 A 上
    assert len(FL._dedupe_free([dict(A), dict(B)])) == 1               # 前提：单去重会丢掉 B
    img = np.full((int(G + 14 * C), int(G + 25 * C), 3), K.BG, np.uint8)
    old = FL.free_segments, FL.USE_PORTS, FL._DEVS, list(FL.PORT_CMP), FL._DARK_THIN
    FL.free_segments = lambda *a, **k: [dict(A), dict(B)]
    try:
        res = FL.build(img, [], [{"name": "反应池", "c": POOL["c"]}, {"name": "提纯机", "c": PURI["c"]}], [])
    finally:
        FL.free_segments, FL.USE_PORTS, FL._DEVS, FL._DARK_THIN = old[0], old[1], old[2], old[4]
        FL.PORT_CMP[:] = old[3]
    pipes = [lk for lk in res["links"] if lk["layer"] == "pipe"]
    assert len(pipes) == 1 and _ends(pipes[0]) == {"反应池", "提纯机"}, pipes
    # 桥的保险在整条 build 里也要生效（_build 调 _merge_overlap 时要把物流件传进去）：弱头旁边放一个管道桥，不拼
    weak = A["path"][0]                                                # A 的弱头（远认的封装机那头）
    FL.free_segments = lambda *a, **k: [dict(A), dict(B)]
    try:
        res = FL.build(img, [{"type": "管道桥", "p": [int(weak[0]), int(weak[1])], "c": [int(weak[0]), int(weak[1])]}],
                       [{"name": "反应池", "c": POOL["c"]}, {"name": "提纯机", "c": PURI["c"]}], [])
    finally:
        FL.free_segments, FL.USE_PORTS, FL._DEVS, FL._DARK_THIN = old[0], old[1], old[2], old[4]
        FL.PORT_CMP[:] = old[3]
    pipes = [lk for lk in res["links"] if lk["layer"] == "pipe" and lk.get("free")]
    assert not any(_ends(lk) == {"反应池", "提纯机"} for lk in pipes), pipes


def test_zz_module_globals_restored():
    now = _globals()
    bad = [k for k in _BEFORE if now[k] != _BEFORE[k]]
    assert not bad, bad


TESTS = [test_belt_under_pipe_gate_from_first_step, test_trace_first_step_needs_hop0, test_no_hop_under_pipe_bridge,
         test_no_hop_without_exit, test_no_hop_with_two_exits,
         test_merge_two_overlapping_pipe_halves, test_merge_ignores_far_bridge_but_not_bridge_at_joint, test_merge_only_pipes,
         test_merge_needs_one_sure_one_loose_end_each, test_merge_sure_ends_must_differ, test_merge_needs_weak_end_on_the_other_path,
         test_merge_needs_one_cell_collinear_overlap, test_merge_rejects_crossing_not_collinear, test_build_merges_before_dedupe,
         test_zz_module_globals_restored]   # 最后一条必须在最后

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    assert set(TESTS) == {v for k, v in globals().items() if k.startswith("test_")}, "TESTS 漏了测试"
    sys.exit(run(TESTS))
