"""设计分析的「自相矛盾 → 看设备口格箭头 → 自己改 / 待确认」和作者修正（fixes.json）。
issue #1 第四轮（2026-11-14）：精炼炉三条带子全被读成出口；用户定的做法见 docs/蓝图分析器-设计.md。不依赖本机 debug 数据。"""
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


def test_feedback_pack_splits_under_the_limit():
    """反馈包：每个压缩包不超过上限、都能单独解压、带说明；默认不带游戏原始截图；小的并成一个。"""
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
            assert not any(n.endswith(".jpg") or n.startswith("bp/") for n in names), names
            assert sorted(p.name for p in out.iterdir()) == sorted(x["name"] for x in r)   # 没留下临时文件
            r = P.export_feedback("项目", Path(td) / "out2", raw=True, limit=10_000_000, log=lambda *_: None)
            assert len(r) == 1 and "全部" in r[0]["name"]
            with zipfile.ZipFile(r[0]["path"]) as z:
                assert "bp/某蓝图/frames/00.png" in z.namelist() and "可能带 UID" in z.read("说明.txt").decode("utf-8")
            r = P.export_feedback("项目", Path(td) / "out3", limit=100_000, log=lambda *_: None)   # 单个文件自己就超了：单独一包，写明
            big = [x for x in r if x["note"]]
            assert len(big) == 2 and all(x["files"] == 1 for x in big), r
        finally:
            P.BPA = old


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
