"""程序放在中文目录里也要能用（2026-11-12 群友：下载到中文目录不能用）。
cv2.imread / cv2.imwrite 在 Windows 上不认中文路径（静默返回 None / False），基地图标、状态标记模板全读不进来。
程序里读写图片一律走 paths.imread / paths.imwrite；这里再扫一遍源码，防止以后又直接调 cv2。"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import paths  # noqa: E402

# 只给开发者在源码环境里手动跑的诊断脚本，不在程序流程里
DEV_ONLY = {"calib_product_icon.py", "diag_product.py", "diag_tiles.py", "test_fluid_tile.py", "test_recipe.py"}


def test_read_write_chinese_dir():
    tmp = Path(tempfile.mkdtemp()) / "终末地 工具" / "景玉谷"
    tmp.mkdir(parents=True)
    try:
        img = np.random.default_rng(0).integers(0, 255, (30, 40, 3), dtype=np.uint8)
        for name in ("截图.png", "面板.jpg", "无扩展名"):
            p = tmp / name
            assert paths.imwrite(p, img), name
            back = paths.imread(p)
            assert back is not None and back.shape == img.shape, name
        assert (paths.imread(tmp / "截图.png") == img).all()  # png 无损
        gray = paths.imread(tmp / "截图.png", 0)
        assert gray.shape == (30, 40)
        assert paths.imread(tmp / "没有这个文件.png") is None
        src = ROOT / "resource" / "badges"
        tpl = next(src.glob("*.png"))
        shutil.copy(tpl, tmp / "模板.png")
        assert paths.imread(tmp / "模板.png", -1) is not None  # 带 alpha 的模板
    finally:
        shutil.rmtree(tmp.parents[1], ignore_errors=True)


def test_no_raw_cv2_imread_imwrite():
    bad = []
    for f in sorted((ROOT / "src").rglob("*.py")):
        if f.name in DEV_ONLY:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bcv2\.(imread|imwrite)\(", code):
                bad.append(f"{f.relative_to(ROOT)}:{i}")
    assert not bad, "改用 paths.imread / paths.imwrite（中文路径）：" + "、".join(bad)


if __name__ == "__main__":
    test_read_write_chinese_dir()
    test_no_raw_cv2_imread_imwrite()
    print("2/2 通过")
