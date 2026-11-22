"""设计分析的「自相矛盾 → 看设备口格箭头 → 自己改 / 待确认」和作者修正（fixes.json）。
issue #1 第四轮（2026-11-14）：精炼炉三条带子全被读成出口；用户定的做法见 docs/蓝图分析器-设计.md。不依赖本机 debug 数据。"""
import contextlib
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import design as D  # noqa: E402
from bpa import project as P  # noqa: E402
from bpa import vision as V  # noqa: E402

DEVS = [{"name": "精炼炉", "x": 1, "y": 1}, {"name": "天有洪炉", "x": 2, "y": 2}, {"name": "种植机", "x": 3, "y": 3}]
BOXES = [(300, 300, 110, 110), (150, 60, 160, 160), (120, 255, 160, 150)]  # 精炼炉 / 天有洪炉（带子头上面）/ 种植机（带子头下面）
CANVAS = [(x + w / 2, y + h / 2) for x, y, w, h in BOXES]
REC = {0: ("芽针×1 → 碳块×2（2秒）", True), 1: ("碳块×1 + 清水×1 → 息壤×1（2秒）", False), 2: ("清水×1 + 芽针种子×1 → 芽针×2（2秒）", False)}
KEY = ["精炼炉@1,1", "天有洪炉@2,2", "种植机@3,3"]


def _run(td, glyph, feeds=None):
    """一条带子：头 (200,240) 夹在天有洪炉下边和种植机上边中间，尾在精炼炉下边；现在读成 精炼炉 → 天有洪炉。
    glyph：{设备序号: {边: in/out}}（顶替 glyph_sides，不画真的折角）。"""
    img = np.full((520, 520, 3), 232, np.uint8)
    for x, y, w, h in BOXES:
        cv2.rectangle(img, (x, y), (x + w, y + h), (40, 40, 40), 2)
    V.wr(Path(td) / "composite.png", img)
    lk = {"layer": "belt", "free": True, "a": None, "end": "device", "dir": "b->a", "basis": ["端口布局：天有洪炉的进口"],
          "a_dev": {"name": "天有洪炉", "c": list(CANVAS[1])}, "a_dev_side": "N",
          "device": {"name": "精炼炉", "c": list(CANVAS[0])}, "dev_side": "N",
          "path": [[200, 240], [200, 430], [355, 430], [355, 425]]}
    G = D.Graph({"nodes": [], "links": [lk]}, DEVS, CANVAS, REC)
    assert [v for v, _ in G.out[("d", 0)]] == [("d", 1)]
    old = D.glyph_sides

    def fake(gray, box):
        i = min(range(3), key=lambda k: abs(BOXES[k][0] - box[0]) + abs(BOXES[k][1] - box[1]))
        return glyph.get(i, {})
    D.glyph_sides = fake
    try:
        fixed, ask = D._starved_by_reading(G, {"links": [lk]}, DEVS, CANVAS, REC, Path(td), feeds=feeds)
    finally:
        D.glyph_sides = old
    feed = [(v, l["basis"][-1]) for v, l in G.inn[("d", 0)]]
    return fixed, ask, feed


def test_both_ends_are_inputs_so_the_other_end_is_another_device():
    """精炼炉没有进料；带子接在它的进口边上，另一头认的天有洪炉那边也是进口 → 另一头其实是紧挨着的种植机的出口。"""
    with tempfile.TemporaryDirectory() as td:
        fixed, ask, feed = _run(td, {0: {"S": "in"}, 1: {"S": "in"}, 2: {"N": "out"}})
        assert len(fixed) == 1 and fixed[0]["kind"] == "另一头改认设备" and not ask, (fixed, ask)
        assert len(feed) == 1 and feed[0][0] == ("d", 2) and "推断" in feed[0][1], feed


def test_other_end_silent_so_just_flip():
    with tempfile.TemporaryDirectory() as td:
        fixed, ask, feed = _run(td, {0: {"S": "in"}})
        assert len(fixed) == 1 and fixed[0]["kind"] == "方向改过来" and not ask
        assert feed[0][0] == ("d", 1) and "推断" in feed[0][1]


def test_cannot_tell_goes_to_the_author():
    """精炼炉那条边的折角没读出来（或说是出口）、或两头都是进口又找不到别的设备：不硬选，列进待确认。"""
    with tempfile.TemporaryDirectory() as td:
        for glyph in ({}, {0: {"S": "out"}}, {0: {"S": "in"}, 1: {"S": "in"}}, {0: {"S": "in"}, 1: {"S": "in"}, 2: {"N": "in"}}):
            fixed, ask, feed = _run(td, glyph)
            assert not fixed and not feed and len(ask) == 1, (glyph, fixed, ask)
            a = ask[0]
            assert a["key"] == KEY[0] and a["answer"] is None and a["applied"] is False and a["links"][0]["key"] == KEY[1], a


def test_author_answer_is_applied():
    with tempfile.TemporaryDirectory() as td:
        fixed, ask, feed = _run(td, {}, feeds={KEY[0]: "to:" + KEY[1]})
        assert fixed[0]["kind"] == "作者确认" and feed[0][0] == ("d", 1) and "推断" not in feed[0][1], (fixed, feed)
        assert ask[0]["answer"] == "to:" + KEY[1] and ask[0]["applied"] is True
        fixed, ask, feed = _run(td, {0: {"S": "in"}}, feeds={KEY[0]: "unused"})  # 作者说没在用：程序不再自己改
        assert not fixed and not feed and ask[0]["answer"] == "unused" and ask[0]["applied"] is True
        fixed, ask, feed = _run(td, {}, feeds={KEY[0]: "none"})                   # 都不是：留在待确认里，带着回答
        assert not fixed and ask[0]["answer"] == "none" and ask[0]["applied"] is False


def test_fixes_file_round_trip():
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            bd = P.pdir("项目") / "base" / "某据点"
            bd.mkdir(parents=True)
            assert P.load_fixes("项目") == {} and D.load_fixes(bd) == {}
            P.save_fix("项目", "某据点", "feeds", KEY[0], "to:" + KEY[2])
            P.save_fix("项目", "某据点", "recipes", KEY[0], "芽针×1 → 碳块×2（2秒）")
            assert D.load_fixes(bd) == {"feeds": {KEY[0]: "to:" + KEY[2]}, "recipes": {KEY[0]: "芽针×1 → 碳块×2（2秒）"}}
            P.save_fix("项目", "某据点", "feeds", KEY[0], None)  # 撤销
            assert D.load_fixes(bd)["feeds"] == {}
            for bad in (("links", KEY[0], "x"), ("feeds", KEY[0], "随便写")):
                try:
                    P.save_fix("项目", "某据点", *bad)
                    assert False, bad
                except ValueError:
                    pass
            (P.pdir("项目") / "fixes.json").write_text("{坏的", encoding="utf-8")  # 文件坏了不抛错
            assert D.load_fixes(bd) == {}
        finally:
            P.BPA = old


def test_pool_recipes_fix():
    """池子配方（fixes.json 的 pool_recipes，2026-11-19 审计第 3 项）：值是原始配方列表；[] = 这台没在用（合法值，不是撤销），
    只有 None 才撤销；存的时候删掉同一台老的 recipes[键]；不是这种池子的配方、重复、超格、写到非池子上都拒绝。
    feeds / recipes 两类仍用空串撤销；只有 recipes 的老文件照常读。
    2026-11-19：配方变了，这一台的作者确认作废（别的设备的确认留着；存的和原来一样就不动）；给池子存 recipes 转存成
    pool_recipes=[那一条]（两段互斥、后存的为准），撤销 pool_recipes 时老的 recipes 一起删。"""
    yz, jc = "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）", "清水×1 + 锦草粉末×1 → 锦草溶液×1（2秒）"
    key = "扩容反应池@1570,1196"
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            bd = P.pdir("项目") / "base" / "某据点"
            bd.mkdir(parents=True)
            (P.pdir("项目") / "fixes.json").write_text(json.dumps({"某据点": {"recipes": {key: yz}}}, ensure_ascii=False), encoding="utf-8")
            assert D.load_fixes(bd) == {"recipes": {key: yz}}  # 只有 recipes 的老文件
            def devs_confirmed():
                return set(json.loads((P.pdir("项目") / "confirm.json").read_text(encoding="utf-8"))["某据点"]["devices"])

            def confirm_both():
                conf = {"某据点": {"devices": {key: {"verdict": "ok"}, "精炼炉@1,1": {"verdict": "ok"}}}}
                (P.pdir("项目") / "confirm.json").write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")
            confirm_both()
            P.save_fix("项目", "某据点", "pool_recipes", key, [yz])  # 和老的 recipes[键] 是同一条：配方没变，确认不动
            assert devs_confirmed() == {key, "精炼炉@1,1"}
            P.save_fix("项目", "某据点", "pool_recipes", key, [yz, jc])
            fx = D.load_fixes(bd)
            assert fx["pool_recipes"] == {key: [yz, jc]} and fx["recipes"] == {}, fx  # 老的 recipes[键] 删掉
            assert devs_confirmed() == {"精炼炉@1,1"}  # 配方变了：这台的「对」作废，别的留着
            confirm_both()
            done = P.save_fixes("项目", "某据点", [{"kind": "pool_recipes", "key": key, "value": []}])
            assert D.load_fixes(bd)["pool_recipes"] == {key: []}  # 没在用：存成 []，不是撤销
            assert devs_confirmed() == {"精炼炉@1,1"} and done[0]["revoked"] and not done[0]["kept_wrong"], done  # 撤了的带标记，日志点名
            # 0.10.13：原来说「不对」的，改了配方也留着（只有「对」针对的是旧配方的结果），带 kept_wrong 让日志提醒
            (P.pdir("项目") / "confirm.json").write_text(json.dumps({"某据点": {"devices": {key: {"verdict": "wrong", "actual": "间歇"}}}},
                                                                   ensure_ascii=False), encoding="utf-8")
            done = P.save_fixes("项目", "某据点", [{"kind": "pool_recipes", "key": key, "value": [jc]}])
            assert devs_confirmed() == {key} and done[0]["kept_wrong"] and not done[0]["revoked"], done
            P.save_fix("项目", "某据点", "pool_recipes", key, None)
            assert D.load_fixes(bd)["pool_recipes"] == {}
            # 给池子存老的 recipes（「没有进料」卡的单条下拉、旧界面）：转存成 pool_recipes=[那一条]，不留两段
            P.save_fix("项目", "某据点", "pool_recipes", key, [yz, jc])
            P.save_fix("项目", "某据点", "recipes", key, jc)
            fx = D.load_fixes(bd)
            assert fx["pool_recipes"] == {key: [jc]} and key not in fx["recipes"], fx
            P.save_fix("项目", "某据点", "recipes", key, "")  # 撤销
            assert D.load_fixes(bd)["pool_recipes"] == {}
            (P.pdir("项目") / "fixes.json").write_text(json.dumps({"某据点": {"recipes": {key: yz}}}, ensure_ascii=False), encoding="utf-8")
            P.save_fix("项目", "某据点", "pool_recipes", key, None)  # 撤销池子配方：老文件里的 recipes[键] 一起删
            assert D.load_fixes(bd)["recipes"] == {}
            try:
                P.save_fix("项目", "某据点", "recipes", key, "芽针×1 → 碳块×2（2秒）")  # 转存后照池子配方查：不是这种池子的配方
                assert False
            except ValueError:
                pass
            P.save_fix("项目", "某据点", "recipes", "精炼炉@1,1", yz)
            P.save_fix("项目", "某据点", "recipes", "精炼炉@1,1", "")  # 空串对 recipes 仍是撤销
            assert D.load_fixes(bd)["recipes"] == {}
            bad = [("精炼炉@1,1", [yz]),                                               # 不是池子
                   (key, ["芽针×1 → 碳块×2（2秒）"]),                                  # 不是这种池子的配方
                   (key, [yz, yz]),                                                    # 重复
                   ("反应池@1,1", [yz, "赤铜粉末×1 + 沉积酸×1 → 赤铜溶液×1（2秒）"]),     # 反应池 5 格，6 种物品
                   (key, yz)]                                                          # 不是列表
            for k, v in bad:
                try:
                    P.save_fix("项目", "某据点", "pool_recipes", k, v)
                    assert False, (k, v)
                except ValueError:
                    pass
            assert D.load_fixes(bd)["pool_recipes"] == {}
            try:
                P.save_fix("项目", "某据点", "recipes", key, [yz])  # recipes 仍只收字符串
                assert False
            except ValueError:
                pass
        finally:
            P.BPA = old


def _gv_row(key, c, kind, item=None, flow=None, **kw):
    bp, p = key.split("@")
    return dict({"key": key, "gate": len(key), "c": list(c), "bp": bp, "p_bp": [int(t) for t in p.split(",")], "kind": kind, "item": item,
                 "item_how": None, "flow": flow or {}, "held": {}, "downstream": 30, "limit": None, "count": None, "src": None, "read": None,
                 "input_no": None, "input_rate": None, "from": [], "to": []}, **kw)


# 手写的准入口表（design._gate_view 的格式）：物品口（口上没认出物品，过的是息壤）、管道口、类型没定的口、作者填过的口
# （p_bp 比记录漂了 5 px：表上的键还是那条记录的）；另有一条对不上的旧设置
GV = {"gates": [_gv_row("图甲@200,300", (1200, 300), "物品", flow={"息壤": 18.0}, held={"息壤": 12.0}),
                _gv_row("图甲@500,300", (1500, 300), "管道", "清水", {"清水": 60.0}),
                _gv_row("图乙@40,40", (40, 40), None, "惰气", {"惰气": 18.0}),
                _gv_row("图甲@210,610", (1205, 600), "物品", "芽针", {"芽针": 6.0}, src="作者", limit=6, p_bp=[205, 600])],
      "stale": [{"key": "旧图@1,1", "bp": "旧图", "p_bp": [1, 1], "kind": "物品", "item": "芽针", "limit": 12, "count": None,
                 "at": "2026-11-20 10:00:00", "why": "蓝图「旧图」不在这个据点里了"}],
      "read_time": None}
OLD_GATES = {"图甲@210,610": {"bp": "图甲", "p_bp": [210, 610], "kind": "物品", "item": "芽针", "limit": 6, "count": None, "at": "2026-11-20 10:00:00"},
             "旧图@1,1": {"bp": "旧图", "p_bp": [1, 1], "kind": "物品", "item": "芽针", "limit": 12, "count": None, "at": "2026-11-20 10:00:00"}}


def _gate_project(td):
    """临时项目：据点目录里放手写的 gates_view.json，fixes.json 里有作者以前填的两条。→ fixes.json 的路径。"""
    bd = P.pdir("项目") / "base" / "某据点"
    bd.mkdir(parents=True)
    (bd / "gates_view.json").write_text(json.dumps(GV, ensure_ascii=False), encoding="utf-8")
    f = P.pdir("项目") / "fixes.json"
    f.write_text(json.dumps({"某据点": {"gates": OLD_GATES}}, ensure_ascii=False), encoding="utf-8")
    return f


def test_save_fixes_batch_and_gates():
    """0.10.13：save_fixes 一次存几项、只写一次；准入口记录的 bp、p_bp、kind、item 从准入口表补（不信界面传的）：
    口上没认出物品的用过口的那种料；作者填过的口（表上 p_bp 漂了几 px）存回同一条、位置用原来记录的；两项都是 None = 都没开。
    撤销 = 删掉这条记录，对不上的旧设置也能撤。"""
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            _gate_project(td)
            bd = P.pdir("项目") / "base" / "某据点"
            done = P.save_fixes("项目", "某据点", [
                {"kind": "feeds", "key": KEY[0], "value": "none"},
                {"kind": "gates", "key": "图甲@200,300", "value": {"limit": 18, "count": None, "bp": "乱写的", "p_bp": [0, 0]}},
                {"kind": "gates", "key": "图甲@500,300", "value": {"limit": 60.0, "count": 20}},
                {"kind": "gates", "key": "图乙@40,40", "value": {"limit": None, "count": None}},
                {"kind": "gates", "key": "图甲@210,610", "value": {"limit": 12}}])
            assert [c["kind"] for c in done] == ["feeds", "gates", "gates", "gates", "gates"], done
            g = D.load_fixes(bd)["gates"]
            assert D.load_fixes(bd)["feeds"] == {KEY[0]: "none"}
            a = g["图甲@200,300"]
            assert (a["bp"], a["p_bp"], a["kind"], a["item"], a["limit"], a["count"]) == ("图甲", [200, 300], "物品", "息壤", 18, None), a
            assert a["at"] and isinstance(a["limit"], int)
            assert (g["图甲@500,300"]["limit"], g["图甲@500,300"]["count"], g["图甲@500,300"]["item"]) == (60, 20, "清水"), g
            assert (g["图乙@40,40"]["limit"], g["图乙@40,40"]["count"], g["图乙@40,40"]["kind"]) == (None, None, ""), g
            assert g["图甲@210,610"]["p_bp"] == [210, 610] and g["图甲@210,610"]["limit"] == 12, g  # 存回原来那条，位置不跟着漂
            assert set(g) == {"图甲@200,300", "图甲@500,300", "图乙@40,40", "图甲@210,610", "旧图@1,1"}, g
            P.save_fixes("项目", "某据点", [{"kind": "gates", "key": "图甲@200,300", "value": None},
                                          {"kind": "gates", "key": "旧图@1,1", "value": None}])     # 撤销；对不上的旧设置也能删
            assert set(D.load_fixes(bd)["gates"]) == {"图甲@500,300", "图乙@40,40", "图甲@210,610"}, D.load_fixes(bd)["gates"]
            assert P.save_fixes("项目", "某据点", []) == []                                    # 空的：不写文件
        finally:
            P.BPA = old


def test_gate_fix_validation():
    """准入口设置的校验：限制速度只收 6 的倍数，物品准入口 0–30、管道准入口和类型没定的口 0–60；限制通过数量 1–5000 的整数；
    键要在准入口表里（对不上的旧设置只能撤销）；还没有准入口表（旧版本算的）不收。一批里有一项不对，整批都不存。
    界面接口 bpa_fix_gates 遇到这些当场报错、不开任务；entries 为空 = 只重算（开任务）。"""
    sys.path.insert(0, str(ROOT / "src" / "app"))
    import api as A
    bad = [("图甲@200,300", {"limit": 36}), ("图甲@500,300", {"limit": 66}), ("图乙@40,40", {"limit": 66}),
           ("图甲@200,300", {"limit": 7}), ("图甲@200,300", {"limit": -6}), ("图甲@200,300", {"limit": "18"}), ("图甲@200,300", {"limit": True}),
           ("图甲@200,300", {"count": 0}), ("图甲@200,300", {"count": 5001}), ("图甲@200,300", {"count": 2.5}), ("图甲@200,300", {"count": "20"}),
           ("图丙@1,1", {"limit": 6}), ("图丙@1,1", None), ("旧图@1,1", {"limit": 6}), ("图甲@200,300", 18)]
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            f = _gate_project(td)
            before = f.read_text(encoding="utf-8")
            a = A.Api()
            started = []
            a._start = lambda kind, fn, *args, **kw: started.append((kind, args, kw)) or {"ok": True}
            for key, v in bad:
                try:
                    P.save_fixes("项目", "某据点", [{"kind": "gates", "key": "图甲@500,300", "value": {"limit": 30}},
                                                  {"kind": "gates", "key": key, "value": v}])
                    assert False, (key, v)
                except ValueError as e:
                    assert str(e), (key, v)
                r = a.bpa_fix_gates("项目", "某据点", {key: v})
                assert r["ok"] is False and r["error"], (key, v, r)
            for k, v in (("pool_outlets", {"belt": []}), ("别的", "x")):
                assert a.bpa_fix_batch("项目", "某据点", [{"kind": k, "key": "扩容反应池@1,1", "value": v}])["ok"] is False
            assert f.read_text(encoding="utf-8") == before and not started, started  # 一项都没存、没开任务
            for lim in (0, 6, 30):                                                     # 边界上的都收
                P.check_fixes("项目", "某据点", [{"kind": "gates", "key": "图甲@200,300", "value": {"limit": lim, "count": 5000}}])
            assert a.bpa_fix_gates("项目", "某据点", {"图甲@200,300": {"limit": 18, "count": 1}})["ok"]
            assert a.bpa_fix_gates("项目", "某据点", {})["ok"]                             # 只重算
            assert [(k, kw) for k, _, kw in started] == [("bpa", {"log_kind": A.FIX_LOG_KIND})] * 2, started
            assert started[1][1][2] == [], started[1]
            (P.pdir("项目") / "base" / "某据点" / "gates_view.json").unlink()            # 旧版本算的据点：还没有准入口表
            r = a.bpa_fix_gates("项目", "某据点", {"图甲@200,300": {"limit": 18}})
            assert r["ok"] is False and "重算" in r["error"], r
            assert a.bpa_fix_gates("项目", "某据点", {})["ok"] and len(started) == 3       # 「重算」按钮照样能点
        finally:
            P.BPA = old


def test_revoke_changed_confirms():
    """按作者修正重算前后比对：开工率或状态变了、原来点「对」的撤掉；原来说「不对」的留着（只进 wrong，日志提醒）；
    没变的、重算后不在了的不动。"""
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            P.pdir("项目").mkdir(parents=True)
            conf = {"某据点": {"devices": {k: {"verdict": v, "actual": "间歇" if v == "wrong" else ""} for k, v in
                                          (("甲@1,1", "ok"), ("乙@2,2", "wrong"), ("丙@3,3", "ok"), ("丁@4,4", "ok"), ("戊@5,5", "ok"))}},
                    "别的据点": {"devices": {"甲@1,1": {"verdict": "ok"}}}}
            (P.pdir("项目") / "confirm.json").write_text(json.dumps(conf, ensure_ascii=False), encoding="utf-8")

            def st(rate, state, dev="粉碎机"):
                return {"device": dev, "c": [10.4, 20.6], "rate": rate, "state": state}
            before = {"甲@1,1": st(1.0, "满速"), "乙@2,2": st(0.5, "间歇"), "丙@3,3": st(1.0, "满速"), "丁@4,4": st(0.3, "间歇"),
                      "戊@5,5": st(0.0, "没料")}
            after = {"甲@1,1": st(0.6, "间歇"), "乙@2,2": st(0.4, "间歇"), "丙@3,3": st(1.0 + 1e-9, "满速"), "戊@5,5": st(0.0, "配方未知"),
                     "新@6,6": st(1.0, "满速")}
            rv = P.revoke_changed("项目", "某据点", before, after)
            assert [c["key"] for c in rv["changed"]] == ["甲@1,1", "乙@2,2", "戊@5,5"], rv  # 状态变了、开工率没变的也算
            assert rv["revoked"] == ["甲@1,1", "戊@5,5"] and rv["wrong"] == ["乙@2,2"], rv
            assert rv["changed"][0]["before"] == {"rate": 1.0, "state": "满速"} and rv["changed"][0]["after"]["rate"] == 0.6, rv
            got = json.loads((P.pdir("项目") / "confirm.json").read_text(encoding="utf-8"))
            assert set(got["某据点"]["devices"]) == {"乙@2,2", "丙@3,3", "丁@4,4"}, got   # 不对的留、没变的留、不在了的留
            assert got["别的据点"]["devices"] == {"甲@1,1": {"verdict": "ok"}}                   # 别的据点不动
            assert P.revoke_changed("项目", "某据点", after, after)["changed"] == []
        finally:
            P.BPA = old


def test_pending_cards_have_open():
    """待确认每张卡带 open（界面「待确认 N 处」只数它）：池子卡没答 = 回答是 None（[] 是「这台没在用」，算答了）；
    没有进料卡没答 = 回答是空的。"""
    sys.path.insert(0, str(ROOT / "src" / "app"))
    import api as A
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            (P.pdir("项目") / "base" / "某据点").mkdir(parents=True)
            pool = {"kind": "池子配方", "key": "扩容反应池@1,1", "device": "扩容反应池[1, 1]", "c": [1, 1], "ins": [], "outs": []}
            feed = {"kind": "没有进料", "key": KEY[0], "device": "精炼炉[1, 1]", "c": [1, 1], "links": []}
            items = [dict(pool, answer=None), dict(pool, answer=[]), dict(pool, answer=["x"]),
                     dict(feed, answer=None), dict(feed, answer=""), dict(feed, answer="none")]
            got = [r["open"] for r in A.Api()._pending_view("项目", "某据点", {"pending": items})]
            assert got == [True, False, False, True, True, False], got
        finally:
            P.BPA = old


# 池子出口送什么（0.10.13 功能三）：手写的 design.json，只要池子行（键从这里认，日志的拼合图坐标从这里取）
PK2, HL2 = "扩容反应池@1798,454", "天有洪炉@1685,483"
POOL_DJ = {"devices": [{"device": "扩容反应池", "x": 1798, "y": 454, "c": [2587, 217], "rate": 1.0, "state": "满速"},
                       {"device": "反应池", "x": 804, "y": 1215, "c": [1100, 1500], "rate": 1.0, "state": "满速"},
                       {"device": "天有洪炉", "x": 1685, "y": 483, "c": [2392, 267], "rate": 0.0, "state": "没料"}],
           "pending": [], "notes": []}
AUTHOR = {"belt": ["息壤"], "pipe": ["惰性壤晶废液", "壤晶废液"]}
YZ = "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"


@contextlib.contextmanager
def _pool_project():
    """临时项目：据点「某据点」只有手写的 design.json。→ 据点目录。"""
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            bd = P.pdir("项目") / "base" / "某据点"
            bd.mkdir(parents=True)
            (bd / "design.json").write_text(json.dumps(POOL_DJ, ensure_ascii=False), encoding="utf-8")
            yield bd
        finally:
            P.BPA = old


def _outlets(bd) -> dict:
    return {k: {f: v for f, v in r.items() if f != "at"} for k, r in (D.load_fixes(bd).get("pool_outlets") or {}).items()}


def _set_confirm(devs: dict):
    (P.pdir("项目") / "confirm.json").write_text(json.dumps({"某据点": {"devices": devs}}, ensure_ascii=False), encoding="utf-8")


def _confirmed() -> dict:
    return {k: v["verdict"] for k, v in json.loads((P.pdir("项目") / "confirm.json").read_text(encoding="utf-8"))["某据点"]["devices"].items()}


def test_pool_outlets_fix_validation():
    """出口回答的格式：键要是 design.json 里的反应池 / 扩容反应池；只有 belt、pipe 两项，都是物品名列表：带出口最多 1 个、是固体，
    管出口最多 2 个、不重复、是液体或气体；存的时候加 at。没有这一项（或 None）= 没说，空列表 = 什么都不送，两项都没说（{}）= 撤销。
    在不在池子配方里这里不查。不合法的抛错，一批里有一项不对整批都不存；撤销也可以删 design.json 里已经没有的旧回答；
    还没算过设计分析的据点不收。"""
    with _pool_project() as bd:
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_outlets", "key": PK2, "value": AUTHOR}])
        rec = D.load_fixes(bd)["pool_outlets"][PK2]
        assert set(rec) == {"belt", "pipe", "at"} and _outlets(bd)[PK2] == AUTHOR and rec["at"], rec
        assert done[0]["row"]["c"] == [2587, 217] and done[0]["value"]["belt"] == ["息壤"], done      # 日志用的拼合图坐标
        P.save_fix("项目", "某据点", "pool_outlets", PK2, {"belt": ["息壤"]})                          # 管出口没说
        assert _outlets(bd)[PK2] == {"belt": ["息壤"]}, _outlets(bd)
        P.save_fix("项目", "某据点", "pool_outlets", PK2, {"belt": [], "pipe": []})                    # 什么都不送
        assert _outlets(bd)[PK2] == {"belt": [], "pipe": []}, _outlets(bd)
        P.save_fix("项目", "某据点", "pool_outlets", PK2, {"belt": None, "pipe": ["清水"]})             # None = 没说
        assert _outlets(bd)[PK2] == {"pipe": ["清水"]}, _outlets(bd)
        P.save_fix("项目", "某据点", "pool_outlets", "反应池@804,1215", {"pipe": ["沉积酸", "惰气"]})   # 反应池也行；配方这里不查
        assert set(_outlets(bd)) == {PK2, "反应池@804,1215"}, _outlets(bd)
        P.save_fix("项目", "某据点", "pool_outlets", "反应池@804,1215", {})                             # 两项都没说 = 撤销
        P.save_fix("项目", "某据点", "pool_outlets", PK2, None)                                          # 撤销
        assert _outlets(bd) == {}, _outlets(bd)
        before = (P.pdir("项目") / "fixes.json").read_text(encoding="utf-8")
        bad = [(PK2, {"belt": ["清水"]}),                       # 液体走不了带出口
               (PK2, {"pipe": ["息壤"]}),                       # 固体走不了管出口
               (PK2, {"belt": ["息壤", "壤晶"]}),                # 带出口只有 1 个选择
               (PK2, {"pipe": ["清水", "污水", "壤晶废液"]}),     # 管出口最多 2 种
               (PK2, {"pipe": ["清水", "清水"]}),                # 重复
               (PK2, {"pipe": ["不存在的液体"]}),                 # 认不出来
               (PK2, {"belt": "息壤"}),                         # 不是列表（spec 草稿的字符串写法不收）
               (PK2, {"belt": [1]}), (PK2, {"belt": [""]}),
               (PK2, {"belt": ["息壤"], "at": "2026-11-21"}),     # 只能有 belt、pipe
               (PK2, "息壤"), (PK2, ["息壤"]),
               (HL2, {"belt": ["息壤"]}),                         # 不是池子
               ("扩容反应池@9,9", {"belt": ["息壤"]}),             # design.json 里没有这台
               ("扩容反应池@9,9", None)]                            # 没有、也没存过：撤销也不收
        for k, v in bad:
            try:
                P.save_fixes("项目", "某据点", [{"kind": "pool_outlets", "key": PK2, "value": AUTHOR},
                                              {"kind": "pool_outlets", "key": k, "value": v}])
                assert False, (k, v)
            except ValueError as e:
                assert str(e) and "@" not in str(e), (k, v, e)                                         # 报错里不出现设备键
        assert (P.pdir("项目") / "fixes.json").read_text(encoding="utf-8") == before, "一项都没存"
        f = P.pdir("项目") / "fixes.json"
        f.write_text(json.dumps({"某据点": {"pool_outlets": {"扩容反应池@9,9": dict(AUTHOR, at="x")}}}, ensure_ascii=False), encoding="utf-8")
        P.save_fix("项目", "某据点", "pool_outlets", "扩容反应池@9,9", None)                            # 底账重扫后对不上的旧回答能删
        assert _outlets(bd) == {}, _outlets(bd)
        (bd / "design.json").unlink()
        try:
            P.save_fix("项目", "某据点", "pool_outlets", PK2, AUTHOR)
            assert False
        except ValueError as e:
            assert "还没算" in str(e), e


def test_pool_recipes_drop_outlets():
    """撤销池子配方（None，老入口给池子存 recipes 空串也是）、说这台没在用（[]）：同一台的出口回答一起删，这一项带 outlets_dropped；
    改成别的配方：出口回答留着（不在新配方里的算的时候写说明）。一批里同时说了这台的出口：按批里的，不管先后。一批只写一次文件。"""
    with _pool_project() as bd:
        def put():
            P.save_fix("项目", "某据点", "pool_outlets", PK2, AUTHOR)
        for kind, v in (("pool_recipes", None), ("pool_recipes", []), ("recipes", "")):
            put()
            done = P.save_fixes("项目", "某据点", [{"kind": kind, "key": PK2, "value": v}])
            assert PK2 not in _outlets(bd) and done[0]["outlets_dropped"], (kind, v, done)
        P.save_fix("项目", "某据点", "pool_outlets", "反应池@804,1215", {"pipe": ["沉积酸"]})
        put()
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_recipes", "key": PK2, "value": [YZ]}])
        assert _outlets(bd)[PK2] == AUTHOR and not done[0].get("outlets_dropped"), done
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_recipes", "key": PK2, "value": None}])
        assert set(_outlets(bd)) == {"反应池@804,1215"}, _outlets(bd)                                  # 别的池子的不动
        for batch in ([{"kind": "pool_recipes", "key": PK2, "value": None}, {"kind": "pool_outlets", "key": PK2, "value": {"belt": ["息壤"]}}],
                      [{"kind": "pool_outlets", "key": PK2, "value": {"belt": ["息壤"]}}, {"kind": "pool_recipes", "key": PK2, "value": []}]):
            put()
            done = P.save_fixes("项目", "某据点", batch)
            assert _outlets(bd)[PK2] == {"belt": ["息壤"]} and not any(c.get("outlets_dropped") for c in done), (batch, done)
        writes, old = [], P._jsave
        P._jsave = lambda p, obj: writes.append(Path(p).name) or old(p, obj)
        try:
            P.save_fixes("项目", "某据点", [{"kind": "pool_recipes", "key": PK2, "value": [YZ]}, {"kind": "pool_outlets", "key": PK2, "value": AUTHOR}])
        finally:
            P._jsave = old
        assert writes.count("fixes.json") == 1 and D.load_fixes(bd)["pool_recipes"][PK2] == [YZ] and _outlets(bd)[PK2] == AUTHOR, writes


def test_outlets_revoke_only_this_pool():
    """出口回答（不算存的时间）变了：这台池子原来点的「对」存的时候就撤（这一项带 revoked），「不对」留着（带 kept_wrong）；
    下游设备的「对」不在这里动（重算前后比对再定）。同样的回答再存一次不撤。配方和出口一起改：两项都带标记。"""
    with _pool_project():
        _set_confirm({PK2: {"verdict": "ok"}, HL2: {"verdict": "ok"}})
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_outlets", "key": PK2, "value": AUTHOR}])
        assert done[0]["revoked"] and not done[0]["kept_wrong"] and _confirmed() == {HL2: "ok"}, (done, _confirmed())
        _set_confirm({PK2: {"verdict": "ok"}, HL2: {"verdict": "ok"}})
        f = P.pdir("项目") / "fixes.json"
        fx = json.loads(f.read_text(encoding="utf-8"))
        fx["某据点"]["pool_outlets"][PK2]["at"] = "2026-01-01 00:00:00"                                  # 存的时间不同
        f.write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_outlets", "key": PK2, "value": AUTHOR}])   # 一样的回答
        assert not done[0].get("revoked") and _confirmed() == {PK2: "ok", HL2: "ok"}, (done, _confirmed())
        _set_confirm({PK2: {"verdict": "wrong", "actual": "间歇"}})
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_outlets", "key": PK2, "value": {"belt": []}}])
        assert done[0]["kept_wrong"] and not done[0]["revoked"] and _confirmed() == {PK2: "wrong"}, (done, _confirmed())
        _set_confirm({PK2: {"verdict": "ok"}})
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_recipes", "key": PK2, "value": [YZ]},
                                             {"kind": "pool_outlets", "key": PK2, "value": {"belt": ["息壤"]}}])
        assert all(c["revoked"] for c in done) and _confirmed() == {}, (done, _confirmed())
        _set_confirm({PK2: {"verdict": "ok"}})
        done = P.save_fixes("项目", "某据点", [{"kind": "pool_outlets", "key": PK2, "value": None}])     # 撤销也是变了
        assert done[0]["revoked"] and _confirmed() == {}, done


def test_feedback_pack_splits_under_the_limit():
    """反馈包：每个压缩包不超过上限、都能单独解压、带说明；默认不带游戏原始截图；小的并成一个。
    2026-11-19：每张蓝图的结构文件（暗管配对 links.json 等）和 mosaic.png 默认带上，frames/、link_shots/ 只在 raw 时带。"""
    import zipfile
    with tempfile.TemporaryDirectory() as td:
        old = P.BPA
        P.BPA = Path(td) / "bpa"
        try:
            d = P.pdir("项目")
            rng = np.random.default_rng(0)
            for base in ("甲", "乙"):
                bd = d / "base" / base
                bd.mkdir(parents=True)
                (bd / "composite.png").write_bytes(rng.integers(0, 256, 300_000, dtype=np.uint8).tobytes())  # 压不动的 300 KB
                (bd / "design.json").write_text(json.dumps({"x": list(range(2000))}), encoding="utf-8")
                (bd / "check.jpg").write_bytes(b"x" * 1000)                                                    # 标注图：不放
            (d / "bp" / "某蓝图" / "frames").mkdir(parents=True)
            (d / "bp" / "某蓝图" / "frames" / "00.png").write_bytes(rng.integers(0, 256, 200_000, dtype=np.uint8).tobytes())
            (d / "bp" / "某蓝图" / "link_shots").mkdir()
            (d / "bp" / "某蓝图" / "link_shots" / "r0_00_0.jpg").write_bytes(b"x" * 1000)                          # 点暗管的游戏截图
            for f in ("links.json", "anchors.json", "devices.json", "size.json", "logistics.json"):
                (d / "bp" / "某蓝图" / f).write_text("{}", encoding="utf-8")
            (d / "bp" / "某蓝图" / "mosaic.png").write_bytes(b"x" * 1000)
            (d / "project.json").write_text(json.dumps({"name": "项目", "groups": {"甲": [], "乙": []}}, ensure_ascii=False), encoding="utf-8")
            (d / "inputs.json").write_text("{}", encoding="utf-8")
            out = Path(td) / "out"
            r = P.export_feedback("项目", out, version="9.9.9", limit=400_000, log=lambda *_: None)
            assert len(r) == 2 and all(x["size"] <= 400_000 and not x["note"] for x in r), r   # 项目 + 甲 一包，乙 一包
            names = []
            for x in r:
                with zipfile.ZipFile(x["path"]) as z:
                    assert "说明.txt" in z.namelist() and "9.9.9" in z.read("说明.txt").decode("utf-8")
                    names += z.namelist()
            assert "base/甲/composite.png" in names and "base/乙/design.json" in names and "inputs.json" in names
            assert not any(n.endswith(".jpg") for n in names), names
            assert {"bp/某蓝图/links.json", "bp/某蓝图/anchors.json", "bp/某蓝图/devices.json", "bp/某蓝图/size.json",
                    "bp/某蓝图/logistics.json", "bp/某蓝图/mosaic.png"} <= set(names), names            # 结构文件默认带
            assert not any("/frames/" in n or "/link_shots/" in n for n in names), names              # 游戏截图默认不带
            assert sorted(p.name for p in out.iterdir()) == sorted(x["name"] for x in r)   # 没留下临时文件
            r = P.export_feedback("项目", Path(td) / "out2", raw=True, limit=10_000_000, log=lambda *_: None)
            assert len(r) == 1 and "全部" in r[0]["name"]
            with zipfile.ZipFile(r[0]["path"]) as z:
                nl = z.namelist()
                assert "bp/某蓝图/frames/00.png" in nl and "bp/某蓝图/link_shots/r0_00_0.jpg" in nl and "可能带 UID" in z.read("说明.txt").decode("utf-8")
                assert nl.count("bp/某蓝图/links.json") == 1, nl                                            # 结构文件不重复放
            r = P.export_feedback("项目", Path(td) / "out3", limit=100_000, log=lambda *_: None)   # 单个文件自己就超了：单独一包，写明
            big = [x for x in r if x["note"]]
            assert len(big) == 2 and all(x["files"] == 1 for x in big), r
        finally:
            P.BPA = old


def test_tunnel_files_missing_per_blueprint():
    """「暗管配对缺失」按蓝图逐张看（2026-11-19）：只有本该有配对、却读不到的蓝图才算缺；全是外接暗管（带 open）的蓝图
    read_blueprints 本来就不写 links.json，不算缺（原来按整个据点「有暗管、0 对」判，这种据点重读也消不掉提示）。"""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "项目"
        bd = proj / "base" / "甲"
        bd.mkdir(parents=True)
        (bd / "transform.json").write_text(json.dumps({"bps": {k: {} for k in "ABCDEF"}}), encoding="utf-8")

        def bp(name, anchors=None, partner=None, mosaic=True):
            d = proj / "bp" / name
            d.mkdir(parents=True)
            if anchors is not None:
                (d / "anchors.json").write_text(json.dumps({"anchors": anchors}, ensure_ascii=False), encoding="utf-8")
            if partner is not None:
                (d / "links.json").write_text(json.dumps({"pipes": [], "partner": partner}), encoding="utf-8")
            if mosaic:
                (d / "mosaic.png").write_bytes(b"x")
        tun = {"name": "暗管入口", "p": [1, 1]}
        bp("A")                                                              # 整个 bp/ 文件都没有（别人的武陵城反馈包）→ 缺
        bp("B", [dict(tun, open=True), {"name": "暗管出口", "open": True}])    # 全是外接暗管，没有 links.json → 不缺
        bp("C", [tun, {"name": "暗管出口"}], {"0": 1, "1": 0})                  # 配上了 → 不缺
        bp("D", [tun], {"0": 1}, mosaic=False)                               # 没有 mosaic.png，tunnel_pairs 读不了 → 缺
        bp("E", [tun], {"0": None})                                          # 一对都没配上 → 缺
        bp("F", [{"name": "息壤供电桩"}])                                       # 没有暗管 → 不缺
        assert D.tunnel_files_missing(bd) == ["A", "D", "E"], D.tunnel_files_missing(bd)
        (bd / "transform.json").unlink()                                     # 还没拼合过：不知道有哪几张蓝图 → 空名单，不抛异常
        assert D.tunnel_files_missing(bd) == []
    # summary 的 warnings 只挑「暗管配对缺失」：「暗管未配对」（外接的，重读也配不出来）和种植机轮换那种字符串说明都不进
    res = {"devices": [], "notes": ["种植机 (1,1) 锦草 / 芽针轮换", {"kind": "暗管未配对", "msg": "外接"}, {"kind": "暗管配对缺失", "msg": "缺 A"}]}
    assert D.summary(res)["warnings"] == ["缺 A"], D.summary(res)


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
