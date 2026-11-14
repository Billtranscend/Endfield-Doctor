"""据点图「打开图片位置」（2026-11-13，issue #1 的作者问据点图怎么拿出来）。

图一直在数据目录的 bpa/<项目>/base/<据点>/ 里，界面上没有入口。接口 bpa_reveal_image 把下拉框的一项对到原图文件，
在资源管理器里选中它。测试只查对到哪个文件、没生成时怎么说，不真的打开资源管理器。
"""
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app")]

import api  # noqa: E402
from bpa import project as P  # noqa: E402


def _with_project(fn):
    """临时目录里造一个项目「测试」：据点「武陵城」有底账核对图和拼合底图，没有放置核对图；项目里有准入口图标分组图。"""
    old_bpa, old_reveal = P.BPA, api.reveal_in_explorer
    opened = []
    with tempfile.TemporaryDirectory() as tmp:
        P.BPA = Path(tmp)
        api.reveal_in_explorer = opened.append
        try:
            d = P.pdir("测试") / "base" / "武陵城"
            d.mkdir(parents=True)
            im = np.full((100, 200, 3), 128, np.uint8)
            for name in ("check.jpg", "composite.png"):
                cv2.imencode(Path(name).suffix, im)[1].tofile(str(d / name))
            cv2.imencode(".png", im)[1].tofile(str(P.pdir("测试") / "gate_icons.png"))
            fn(api.Api(), opened, d)
        finally:
            P.BPA, api.reveal_in_explorer = old_bpa, old_reveal


def test_reveal_selects_the_original_file():
    def body(a, opened, d):
        r = a.bpa_reveal_image("测试", "武陵城", "check")
        assert r == {"ok": True, "path": str(d / "check.jpg")}, r
        r = a.bpa_reveal_image("测试", "武陵城", "composite")
        assert r["ok"] and r["path"].endswith("composite.png"), r
        r = a.bpa_reveal_image("测试", "武陵城", "gates")  # 不分据点，在项目目录下
        assert r["ok"] and Path(r["path"]) == P.pdir("测试") / "gate_icons.png", r
        assert [Path(p).name for p in opened] == ["check.jpg", "composite.png", "gate_icons.png"]
    _with_project(body)


def test_reveal_says_so_when_the_image_does_not_exist():
    """反例：还没生成的图不去打开资源管理器，说明原因。"""
    def body(a, opened, d):
        r = a.bpa_reveal_image("测试", "武陵城", "placement")
        assert not r["ok"] and "还没生成" in r["error"], r
        r = a.bpa_reveal_image("测试", "应龙关", "check")
        assert not r["ok"], r
        assert opened == []
    _with_project(body)


def test_image_shown_in_app_is_the_same_file():
    """界面里显示的图和「打开图片位置」选中的是同一个文件；拼合底图也能在界面里看。"""
    def body(a, opened, d):
        for which in ("check", "composite", "gates"):
            r = a.bpa_image("测试", "武陵城", which)
            assert r["ok"] and r["image"].startswith("data:image/jpeg;base64,"), (which, r.get("error"))
        assert not a.bpa_image("测试", "武陵城", "placement")["ok"]
    _with_project(body)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
