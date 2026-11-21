"""物流件直接贴在暗管口上（中间一格管子都没有）：flow._touching_tunnel / _drop_ambiguous_tunnel_touch（issue #1 第五轮，2026-11-20）。
别人的武陵城左列清水准入口后面的汇流器贴着两个暗管入口；暗管只有浅蓝细框，外框和端口布局都认不出，原来接不上，
反馈包第一次带上暗管配对后，对面暗管出口出不来水，9 台没料。几何部分不用图；最后一条在合成小图上跑整条 flow.build。
不依赖本机 debug 数据。"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "tests")]

from bpa import flow as FL  # noqa: E402
from bpa import vision as V  # noqa: E402

C = V.CELL
P = np.array([400.0, 400.0])


def _at(d, along=2.0, lat=0.0, jitter=(4.0, 3.0)):
    """物流件在 P，暗管标签放在它 d 边外 along 格（3×3 暗管中心离物流件中心 2 格）、横向 lat 格处，再加一点标签偏差。"""
    v = FL._v(d)
    t = np.array([-v[1], v[0]])
    return list(P + v * (along * C + jitter[0]) + t * (lat * C + jitter[1]))


def test_single_port_tunnel_on_each_side():
    for d in FL.DIRS:
        for name, role in (("暗管入口", "in"), ("暗管出口", "out")):
            hit = FL._touching_tunnel(P, d, [{"name": name, "c": _at(d)}])
            assert hit is not None and hit["name"] == name and hit["port"] == role, (d, name, hit)
            assert hit["how"] == FL.TUNNEL_HOW


def test_single_port_needs_the_side_middle():
    """单口暗管的口在一条边正中：横向偏一格、或者隔得太远都不认。"""
    for d in FL.DIRS:
        assert FL._touching_tunnel(P, d, [{"name": "暗管入口", "c": _at(d, lat=1.0)}]) is None, d
        assert FL._touching_tunnel(P, d, [{"name": "暗管入口", "c": _at(d, along=2.7)}]) is None, d
        assert FL._touching_tunnel(P, d, [{"name": "暗管入口", "c": _at(d, along=1.3)}]) is None, d


def test_multi_port_tunnel_ports_are_one_cell_off_centre():
    """多口暗管（3×5）两个口在长边上、离中心各一格：横向 ±1 格认，正对中心不认。"""
    for d in FL.DIRS:
        for lat in (-1.0, 1.0):
            hit = FL._touching_tunnel(P, d, [{"name": "多口暗管入口", "c": _at(d, lat=lat)}])
            assert hit is not None and hit["port"] == "in", (d, lat)
        assert FL._touching_tunnel(P, d, [{"name": "多口暗管入口", "c": _at(d, lat=0.0)}]) is None, d


def test_label_off_centre_still_found():
    """底账标签离暗管真正的中心可以差到约 0.46 格（多口暗管，量过五个样本 70 台）：偏 0.45 格还认得出。"""
    for d in FL.DIRS:
        v = FL._v(d)
        t = np.array([-v[1], v[0]])
        for off in (v * 0.45 * C, -v * 0.45 * C, t * 0.45 * C):
            c = list(np.array(_at(d, jitter=(0.0, 0.0))) + off)
            assert FL._touching_tunnel(P, d, [{"name": "暗管入口", "c": c}]) is not None, (d, off)
        c = list(np.array(_at(d, lat=1.0, jitter=(0.0, 0.0))) + t * 0.45 * C)
        assert FL._touching_tunnel(P, d, [{"name": "多口暗管入口", "c": c}]) is not None, d


def test_only_tunnels_and_only_one():
    """别的设备不按这条认（它们有自己的外框）；两台暗管都对得上时不认。"""
    assert FL._touching_tunnel(P, "E", [{"name": "精炼炉", "c": _at("E")}]) is None
    two = [{"name": "暗管入口", "c": _at("E")}, {"name": "暗管出口", "c": _at("E", jitter=(-4.0, -3.0))}]
    assert FL._touching_tunnel(P, "E", two) is None


def _touch_link(node, d, tun):
    return {"end": "device", "path": [list(node["c"])], "d": d, "jumps": [], "a": node["id"], "a_side": d, "layer": "pipe",
            "dev_side": d, "device": {"name": tun["name"], "c": tun["c"], "how": FL.TUNNEL_HOW, "port": "in"}, "touch": True}


def test_ambiguous_side_is_dropped():
    """同一台暗管别的边正中也贴着管道物流件（这里是准入口）：说不准口在哪边，贴着认的这条丢掉。
    皇帝武陵城 (965,714)：口在西边接准入口，底边贴着一个分流器。"""
    tun = {"name": "暗管入口", "c": list(P)}
    merger = {"id": 0, "type": "管道汇流器", "c": list(P - np.array([0.0, 2 * C]))}      # 北边，往南贴着暗管
    gate = {"id": 1, "type": "准入口", "kind": "管道", "c": list(P - np.array([2 * C, 0.0]))}  # 西边，往东贴着暗管
    raw = [_touch_link(merger, "S", tun)]
    assert FL._drop_ambiguous_tunnel_touch(list(raw), [merger], [tun]) == raw
    assert FL._drop_ambiguous_tunnel_touch(list(raw), [merger, gate], [tun]) == []
    belt_gate = dict(gate, kind="物品")  # 带子的准入口贴着不算（暗管不接带子）
    assert FL._drop_ambiguous_tunnel_touch(list(raw), [merger, belt_gate], [tun]) == raw
    traced = {"end": "device", "path": [[0, 0]], "layer": "pipe", "dev_side": "W", "device": {"name": "暗管入口", "c": tun["c"]}}
    assert FL._drop_ambiguous_tunnel_touch(raw + [traced], [merger], [tun]) == [traced]  # 已有管子从东边以外的边进来
    same = dict(traced, dev_side="S")
    assert FL._drop_ambiguous_tunnel_touch(raw + [same], [merger], [tun]) == raw + [same]  # 同一条边：不冲突
    piped = {"end": "device", "path": [[0, 0], [1, 1]], "layer": "pipe", "free": True, "a_dev": {"name": "暗管入口", "c": tun["c"]},
             "a_dev_side": "E", "device": {"name": "储液罐", "c": [0, 0]}, "dev_side": "W"}
    assert FL._drop_ambiguous_tunnel_touch(raw + [piped], [merger], [tun]) == [piped]  # 设备直连的管子从别的边接着


def test_merger_touching_frameless_tunnel_on_synthetic_canvas():
    """合成小图：管道汇流器的出口边直接贴着一台暗管入口（图上不画外框，和真图上认不出外框一样）→ 整条 flow.build 接上，
    方向是汇流器 → 暗管；暗管贴在汇流器的进料边上 → 不接。"""
    import _synth as K
    cv = K.Canvas(420, 420)
    node = cv.node("管道汇流器", 5, 5)
    img = cv.finish()
    o = FL.orient(img, node)
    out = o.get("out")
    assert out in FL.DIRS, o
    c = np.array(node["p"], float)
    old = FL.USE_PORTS, FL._DEVS, list(FL.PORT_CMP), FL._DARK_THIN
    try:
        res = FL.build(img, [dict(node)], [{"name": "暗管入口", "c": list(c + FL._v(out) * 2 * C)}], [])
        hit = [lk for lk in res["links"] if (lk.get("device") or {}).get("name") == "暗管入口"]
        assert len(hit) == 1 and hit[0]["device"]["how"] == FL.TUNNEL_HOW and hit[0]["dir"] == "a->b", res["links"]
        assert any("贴着" in b for b in hit[0]["basis"]), hit[0]["basis"]  # 口在哪边是猜的：设计分析要按推断连线标出来
        back_in = [d for d in o.get("ins") or [] if d != out]
        assert back_in, o

        def tun_links(name, d):
            r = FL.build(img, [dict(node)], [{"name": name, "c": list(c + FL._v(d) * 2 * C)}], [])
            return [lk for lk in r["links"] if (lk.get("device") or {}).get("name") == name], r
        got, r = tun_links("暗管入口", back_in[0])          # 暗管入口贴在汇流器进料边：两头都进，不接
        assert not got, r["links"]
        got, r = tun_links("暗管出口", out)                 # 暗管出口贴在汇流器出口边：两头都出，不接
        assert not got, r["links"]
        got, r = tun_links("暗管出口", back_in[0])          # 暗管出口贴在汇流器进料边：接上，暗管 → 汇流器
        assert len(got) == 1 and got[0]["dir"] == "b->a", r["links"]
    finally:
        FL.USE_PORTS, FL._DEVS, FL._DARK_THIN = old[0], old[1], old[3]
        FL.PORT_CMP[:] = old[2]


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
