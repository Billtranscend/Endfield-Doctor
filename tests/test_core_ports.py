"""任务计划第 12 项：协议核心 / 次级核心 6 个取货口（只读）。

- 核心标签 → 核心中心 → 6 个三角的点击位置（2026-11-10 三个基地实测：±132、0/±102）。
- 三角吸附：预测偏了 20 像素（镜头还在滑）也能吸到三角上；分数不够不吸。
- 存量取最左边的数字框（物品插图上的「02」不能粘进去）；物品名取悬停提示里认得的那行。
- 汇总一句话；档案里没读过返回空串。
"""
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import core_ports as CP  # noqa: E402
from paths import imread  # noqa: E402


def test_geometry():
    name, c = CP.find_core([([600, 290, 30, 20], "息壤"), ([657, 275, 83, 24], "协议核心")])
    assert name == "协议核心" and abs(c[0] - 677.5) < 1 and abs(c[1] - 287) < 1, c
    assert CP.find_core([([0, 0, 10, 10], "研磨机")]) is None
    pts = CP.port_points(np.array([640.0, 330.0]))
    assert [(x, y) for x, y, _ in pts] == [(508, 228), (508, 330), (508, 432), (772, 228), (772, 330), (772, 432)]
    assert [s for *_, s in pts] == [-1, -1, -1, 1, 1, 1]


def test_snap():
    t = imread(str(ROOT / "resource" / "badges" / "core_port_l.png"))
    img = np.full((720, 1280, 3), 60, np.uint8)
    h, w = t.shape[:2]
    img[300 - h // 2:300 - h // 2 + h, 500 - w // 2:500 - w // 2 + w] = t  # 三角真实中心 (500, 300)
    x, y, sc = CP.snap(img, 520, 296, -1)  # 预测偏了 20 像素
    assert abs(x - 500) <= 1 and abs(y - 300) <= 1 and sc > 0.9, (x, y, sc)
    x, y, sc = CP.snap(np.full((720, 1280, 3), 60, np.uint8), 520, 296, -1)  # 没有三角：不动
    assert (x, y) == (520, 296)


def test_stock_and_item():
    assert CP._stock([([620, 360, 40, 30], "02"), ([530, 370, 90, 30], "71575")]) == 71575
    assert CP._stock([([530, 370, 90, 30], "--")]) is None
    assert CP._item_name([([820, 410, 40, 20], "材料"), ([820, 385, 140, 24], "中容武陵电池")]) == "中容武陵电池"
    assert CP._item_name([([820, 385, 140, 24], "砂叶粉末材料")]) == "砂叶粉末"


def test_summary():
    import blueprint as bp
    with tempfile.TemporaryDirectory() as td:
        old = bp.BLUEPRINTS
        bp.BLUEPRINTS = Path(td)
        try:
            d = Path(td) / "测试"
            (d / "ledger" / "core_ports").mkdir(parents=True)
            (d / "tier_rules.json").write_text(json.dumps({"rules": []}), encoding="utf-8")
            assert CP.summary("测试", "武陵城") == ""
            rows = [{"port": i, "configured": i in (2, 6), "item": {2: "中容武陵电池", 6: "砂叶粉末"}.get(i, ""),
                     "stock": {2: 71597, 6: 90000}.get(i)} for i in range(1, 7)]
            (d / "ledger" / "core_ports" / "武陵城.json").write_text(
                json.dumps({"area": "武陵城", "time": "20261110-150000", "core": "协议核心", "rows": rows}, ensure_ascii=False), encoding="utf-8")
            s = CP.summary("测试", "武陵城")
            assert "2 号 中容武陵电池（存量 71597）" in s and "6 号 砂叶粉末" in s and "其余 4 个没设" in s, s
            assert [r["port"] for r in CP.configured("测试", "武陵城")] == [2, 6]
        finally:
            bp.BLUEPRINTS = old


if __name__ == "__main__":
    test_geometry()
    test_snap()
    test_stock_and_item()
    test_summary()
    print("4/4 通过")
