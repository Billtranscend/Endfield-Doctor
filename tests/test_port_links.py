"""取货口 / 设备端口之间的连线（issue #1 第四轮，别人的武陵城，2026-11-14）：
出口隔着一个物流桥直接对着进口；两个取货口之间不连；取货口的物品不穿过生产设备去找准入口；滑索架不当带子端点。
不依赖本机 debug 数据。"""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import design as D  # noqa: E402
from bpa import flow as FL  # noqa: E402
from bpa import vision as V  # noqa: E402

C = V.CELL


def _dev(name, c, ports):
    return {"name": name, "c": list(c), "box": [c[0] - 2 * C, c[1] - 2 * C, 4 * C, 4 * C], "ports": ports}


def _port(edge, side, role):
    return {"edge": [float(edge[0]), float(edge[1])], "side": side, "role": role, "pipe": False}


def _bridges(devs, nodes):
    old = FL._DEVS
    FL._DEVS = devs
    try:
        return FL._port_bridges({"belt": np.zeros((900, 900), np.uint8)}, [], nodes)
    finally:
        FL._DEVS = old


def test_out_port_straight_over_a_bridge():
    """种植机下边的出口 → 物流桥 → 粉碎机上边的进口，中间一格带子都没有：接上（原来粉碎机算成没料，下游 4 台研磨机跟着没料）。"""
    x, y = 300.0, 300.0  # 出口格的边
    a = _dev("种植机", (x, y - 2 * C), [_port((x, y), "S", "out")])
    b = _dev("粉碎机", (x, y + 3 * C), [_port((x, y + C), "N", "in")])
    br = {"id": 0, "type": "物流桥", "c": [x, y + C / 2]}
    lk = _bridges([a, b], [br])
    assert len(lk) == 1 and lk[0]["a_dev"]["name"] == "种植机" and lk[0]["device"]["name"] == "粉碎机" and lk[0]["dir"] == "a->b", lk
    assert "物流桥" in lk[0]["basis"][0] and "按格子补" in lk[0]["basis"][0], lk[0]["basis"]
    # 反例 1：进口在桥的侧面（桥只能直着过）
    b2 = _dev("粉碎机", (x + 3 * C, y + C / 2), [_port((x + C / 2, y + C / 2), "W", "in")])
    assert _bridges([a, b2], [br]) == []
    # 反例 2：那一格不是物流桥（别的物流件）
    assert _bridges([a, b], [dict(br, type="分流器")]) == []
    # 反例 3：对面没有进口
    assert _bridges([a], [br]) == []


def test_two_take_ports_are_never_linked():
    """追线把取货口上面那一格短带追歪了、两头都落在取货口上：这条不算（原来占着取货口的出边，上面的粉碎机没接上）。"""
    devices = [{"name": "仓库取货口"}, {"name": "仓库取货口"}, {"name": "粉碎机"}]
    canvas = [(100, 300), (200, 300), (110, 220)]
    rec = {0: ("", False), 1: ("", False), 2: ("源矿×1 → 源石粉末×1（2秒）", False)}

    def free(i, j, d=None):
        return {"layer": "belt", "free": True, "a": None, "end": "device", "dir": d, "basis": [], "path": [[0, 0], [1, 1]],
                "a_dev": {"name": devices[i]["name"], "c": list(canvas[i])}, "device": {"name": devices[j]["name"], "c": list(canvas[j])}}
    G = D.Graph({"nodes": [], "links": [free(0, 1), free(0, 1, "a->b")]}, devices, canvas, rec)
    assert not G.out[("d", 0)] and not G.inn[("d", 1)], dict(G.out)
    # 反例：取货口 → 粉碎机照常
    G = D.Graph({"nodes": [], "links": [free(0, 2, "a->b")]}, devices, canvas, rec)
    assert [v for v, _ in G.out[("d", 0)]] == [("d", 2)]


class _G:
    def __init__(self, edges):
        self.out = defaultdict(list)
        for u, v in edges:
            self.out[u].append((v, {}))


def test_port_item_does_not_look_past_a_maker():
    """图标没看到的取货口 → 精炼炉 → … → 清水准入口：过了精炼炉就不找了（原来取货口被当成出清水的，精炼炉的矿没人送）。"""
    gate = {5: {"item": "清水"}, 7: {"item": "赤铜矿"}}
    G = _G([(("d", 0), ("d", 1)), (("d", 1), ("n", 5))])
    assert D._first_gate_item(G, ("d", 1), gate) == "清水"          # 老行为（不给 stop）
    assert D._first_gate_item(G, ("d", 1), gate, stop={1}) == ""
    # 反例：带子上的物品准入口在设备前面，照样认
    G = _G([(("d", 0), ("n", 7)), (("n", 7), ("d", 1)), (("d", 1), ("n", 5))])
    assert D._first_gate_item(G, ("n", 7), gate, stop={1}) == "赤铜矿"
    # 反例：中转（暗管、储存箱不在 stop 里）照样穿过去
    G = _G([(("d", 3), ("n", 7))])
    assert D._first_gate_item(G, ("d", 3), gate, stop={1}) == "赤铜矿"


def test_unknown_item_by_pipe_or_belt():
    """物品没看到的来料到设备按缺口分：管子来的只能当流体原料
    （原来暗管出口的 120/分被「当作碳块」，天有洪炉的清水算成没人送、碳块带没接上的事也被盖住了）。带子来的照旧不限制。"""
    assert D._unknown_fits(D.UNK_PIPE, "清水") and D._unknown_fits(D.UNK_PIPE, "息壤气") and D._unknown_fits(D.UNK_PIPE, "液化息壤")
    assert not D._unknown_fits(D.UNK_PIPE, "碳块") and not D._unknown_fits(D.UNK_PIPE, "赤铜矿")
    assert D._unknown_fits(D.UNK_BELT, "碳块") and D._unknown_fits(D.UNK_BELT, "息壤") and D._unknown_fits(D.UNK_BELT, "惰气")
    assert D.UNK_BELT == "*" and set(D.UNKNOWN) == {D.UNK_BELT, D.UNK_PIPE}


def _side(yellow_sides, devs, pts):
    """带子头在 (300,300)、往南走到头；yellow_sides 里的那几侧 12 px 处有黄带。"""
    m = np.zeros((600, 600), bool)
    for s in yellow_sides:
        x = 300 + (12 if s == "E" else -12)
        m[292:309, x - 5:x + 6] = True
    old = FL._DEVS
    FL._DEVS = devs
    try:
        return FL._side_port({"belt": m}, {"path": [[300, 250], [300, 300]], "d": "S"}, pts, [])
    finally:
        FL._DEVS = old


def test_belt_head_turns_into_the_side_port():
    """带子往下走到头、最后半格向右拐进旁边设备的进料口，上面压着顺着走的管子（拐弯看不出来）：按哪一侧有黄带认。"""
    furnace = _dev("天有洪炉", (300 + 3 * C, 300 + 2 * C), [_port((300 + 0.7 * C, 302), "W", "in")])
    picker = _dev("采种机", (300 - 3 * C, 300 + C), [_port((300 - 0.7 * C, 302), "E", "in")])
    pts = [{"name": "天有洪炉", "c": furnace["c"]}, {"name": "采种机", "c": picker["c"]}]
    hit, alt = _side(["E"], [furnace, picker], pts)
    assert hit is not None and alt is None and hit[0] == 0 and hit[1]["side"] == "W", (hit, alt)
    hit, alt = _side(["W"], [furnace, picker], pts)
    assert hit is not None and hit[0] == 1, (hit, alt)
    # 反例：两侧都有黄带 / 都没有 → 不认
    assert _side(["E", "W"], [furnace, picker], pts) == (None, None)
    assert _side([], [furnace, picker], pts) == (None, None)
    # 那台设备没有端口布局（外框没认出来）：往那边探着找设备
    hit, alt = _side(["E"], [None, picker], pts)
    assert hit is None and alt is not None and alt[0] == "E" and alt[1]["name"] == "天有洪炉", (hit, alt)


def test_side_look_only_when_a_pipe_covers_the_belt_head():
    """看两侧这条规则只在带子头上压着管子时用（不压管子的照原来的认法，皇帝产线的结果不变）。"""
    g = np.full((200, 200), 232, np.uint8)
    g[:, 96:104] = 60                      # 一根竖着的管子
    assert FL._under_pipe({"gray": g}, (100, 100))
    assert not FL._under_pipe({"gray": g}, (140, 100))
    g[:] = 215                             # 黄带（灰度很浅）
    assert not FL._under_pipe({"gray": g}, (100, 100))


def test_belt_head_stops_at_the_corner_before_a_u_turn():
    """追线停在拐角上（往西走到头），带子往南再走一格才向右进旁边设备的进料口：站到下一格上看两侧。"""
    m = np.zeros((600, 600), bool)
    m[307:318, 295:306] = True                              # 拐角往南那一侧 12 px 处有黄带
    y2 = int(300 + C)
    m[y2 - 8:y2 + 9, 307:318] = True                        # 下一格的东侧有黄带（西侧没有）
    furnace = _dev("天有洪炉", (300 + 3 * C, y2 + 2 * C), [_port((300 + 0.5 * C, y2 - 8), "W", "in")])
    pts = [{"name": "天有洪炉", "c": furnace["c"]}]
    old = FL._DEVS
    FL._DEVS = [furnace]
    try:
        hit, alt = FL._side_port({"belt": m}, {"path": [[350, 300], [300, 300]], "d": "W"}, pts, [])
        assert hit is not None and hit[0] == 0 and hit[1]["role"] == "in", (hit, alt)
        m[y2 - 8:y2 + 9, 283:294] = True                    # 反例：下一格两侧都有黄带 → 不按这条认
        hit, alt = FL._side_port({"belt": m}, {"path": [[350, 300], [300, 300]], "d": "W"}, pts, [])
        assert hit is None, (hit, alt)
    finally:
        FL._DEVS = old


def _two_boxes(td, belt):
    """两台设备隔 28 px：左边精炼炉（做碳块），右边天有洪炉（吃碳块）。belt(img, x0, x1)：在缝里画带子。"""
    import cv2
    img = np.full((420, 560, 3), 232, np.uint8)
    cv2.rectangle(img, (100, 150), (210, 260), (40, 40, 40), 2)
    cv2.rectangle(img, (239, 80), (398, 240), (40, 40, 40), 2)
    belt(img, 212, 238)
    V.wr(Path(td) / "composite.png", img)
    devices = [{"name": "精炼炉"}, {"name": "天有洪炉"}]
    canvas = [(155, 205), (318, 160)]
    rec = {0: ("芽针×1 → 碳块×2（2秒）", True), 1: ("碳块×1 + 清水×1 → 息壤×1（2秒）", False)}
    G = D.Graph({"nodes": [], "links": []}, devices, canvas, rec)
    D.adjacent_links(G, Path(td), devices, canvas, rec)
    return [(v, lk["basis"][0]) for v, lk in G.out[("d", 0)]]


def test_one_cell_straight_belt_between_two_devices():
    """两台设备隔一格、中间一条一格长的直带，产物对得上原料 → 接上；顺着缝走的长带子、没有带子都不接。"""
    import tempfile
    Y = (90, 230, 245)  # 黄带
    with tempfile.TemporaryDirectory() as td:
        out = _two_boxes(td, lambda img, x0, x1: img.__setitem__((slice(190, 210), slice(x0, x1)), Y))
        assert len(out) == 1 and out[0][0] == ("d", 1) and "隔一格" in out[0][1], out
    with tempfile.TemporaryDirectory() as td:  # 带子上压着一根顺着缝走的管子：照样认
        def belt_pipe(img, x0, x1):
            img[190:210, x0:x1] = Y
            img[60:400, x0 + 4:x0 + 11] = (60, 60, 60)
        out = _two_boxes(td, belt_pipe)
        assert len(out) == 1 and "隔一格" in out[0][1], out
    with tempfile.TemporaryDirectory() as td:  # 反例：带子顺着缝走（从上到下一长条）
        assert _two_boxes(td, lambda img, x0, x1: img.__setitem__((slice(60, 400), slice(x0 + 3, x1 - 3)), Y)) == []
    with tempfile.TemporaryDirectory() as td:  # 反例：缝里没有带子
        assert _two_boxes(td, lambda img, x0, x1: None) == []


def test_zipline_tower_is_not_a_belt_end():
    assert "长距滑索架" in FL.NOT_PORT and "仓库取货口" not in FL.NOT_PORT


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
