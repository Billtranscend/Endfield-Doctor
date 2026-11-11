"""读准入口限速：用 2026-11-09 实跑存下的面板截图离线重读（有就跑）。
运行：.venv/Scripts/python.exe tests/test_gate_limits.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "tools"))

import gate_limits as GL  # noqa: E402


def test_num():
    assert GL._num("当前速度O单位/分") == 0.0
    assert GL._num("当前速度30单位/分") == 30.0
    assert GL._num("速度未限制XXX") is None


def test_panels():
    from offline_ocr import imread, shared
    d = ROOT / "debug/shots/20261109-011253_武陵城_限速"
    cases = {"gate_2053_1225.png": (True, 30.0, 30.0, "运行中"),   # 壤晶废液口：限速 30
             "gate_2323_166.png": (False, None, None, "运行中"),    # 清水口：速度未限制
             "gate_2326_1805.png": (False, None, None, "阻塞"),     # 阻塞：状态条红底、字认不出
             "gate_341_643.png": (True, 12.0, 0.0, "阻塞")}         # 重息壤口：限速 12，当前 0（OCR 认成 O）
    if not all((d / f).exists() for f in cases):
        return
    ocr = shared()
    for f, (on, lim, spd, st) in cases.items():
        img = imread(d / f)
        r = GL.read_panel(lambda roi: "".join(t for _, t, *_ in sorted(ocr.boxes(img, roi), key=lambda x: x[0][0])).strip(), img, "管道准入口")
        assert (r["limit_on"], r["limit"], r["speed"], r["status"]) == (on, lim, spd, st), (f, r)
        assert r["count_on"] is False, (f, r)
    # 限制通过数量（用户 2026-11-09）：赤铜耐压罐口限量 1，芽针口限量 20；累计都已到上限
    for f, lim in {"gate_853_780.png": 1.0, "gate_679_1667.png": 20.0}.items():
        if not (d / f).exists():
            continue
        img = imread(d / f)
        r = GL.read_panel(lambda roi: "".join(t for _, t, *_ in sorted(ocr.boxes(img, roi), key=lambda x: x[0][0])).strip(), img, "物品准入口")
        assert (r["count_on"], r["count_limit"], r["passed"], r["limit_on"]) == (True, lim, lim, False), (f, r)


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
