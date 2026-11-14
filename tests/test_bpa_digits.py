"""读蓝图：设备一览里每格右下角的数量（2026-11-14 issue #1，「应龙关上」仓库取货口 15 读成 1）。

那位作者第三次读「应龙关上」时，设备一览里「仓库取货口 15」记成了 1，放置检查报「多出 14 台」（底账里是 15 台，基地没问题）。
720p 画面上数字只有 10–12 px 高、笔画 1–2 px 粗。读法是：数字区只留白色笔画，去掉高度 < 9 px 的连通块（图标的亮边），再 OCR。
那一帧里「5」的横和下半之间断开了：一块 5×2、一块 7×8，两块都不到 9 px，被当成杂点删掉，只剩「1」。
同一张蓝图前两次读到的「5」是连着的（一块 7×11）。改：左右重叠、上下只隔 ≤2 px 的块算同一个数字，按合起来的高度判（game_ops.digit_blobs）。

样本 tests/data/bpa_digits/：15_broken.png = 这次那一格（60×70），15_whole.png = 他最早那次同一格。
这里只测「清理」这一步留下了哪些笔画，不跑 OCR（离线重放过：他这一帧改后读成 15，本地 22 张蓝图的读数逐格不变）。
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import game_ops as G  # noqa: E402
from bpa import project as P  # noqa: E402

DATA = ROOT / "tests" / "data" / "bpa_digits"
T = (30, 0)  # 样本贴在画面里的位置：格子中心 x、格子顶


def cleaned(name: str) -> np.ndarray:
    """样本那一格清理后的数字区（白 = 留下的笔画）。"""
    tile = cv2.imdecode(np.fromfile(str(DATA / name), np.uint8), cv2.IMREAD_COLOR)
    img = np.zeros((80, 60, 3), np.uint8)
    img[:70, :60] = tile
    x, y, w, h = G._digits_roi(T)
    return G._clean_digits(img, T)[y:y + h, x:x + w, 0]


def blobs(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    n, _, st, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8))
    return sorted(tuple(int(v) for v in st[i, :4]) for i in range(1, n))


# ---------------------------------------------------------------- 真截图

def test_broken_five_is_kept():
    """「5」断成两截的那一帧：清理后「1」和「5」的两截都在。原来只剩「1」。"""
    got = blobs(cleaned("15_broken.png"))
    assert got == [(21, 5, 4, 11), (28, 8, 7, 8), (29, 5, 5, 2)], got


def test_whole_digits_unchanged():
    """反例：笔画连着的那一帧照旧是两块。"""
    got = blobs(cleaned("15_whole.png"))
    assert got == [(21, 5, 4, 11), (28, 5, 7, 11)], got


# ---------------------------------------------------------------- 规则本身

def test_fragments_of_one_digit_count_together():
    assert G.digit_blobs([(21, 5, 4, 11), (28, 8, 7, 8), (29, 5, 5, 2)]) == [True, True, True]
    assert G.digit_blobs([(21, 5, 4, 11), (28, 5, 7, 11)]) == [True, True]


def test_stray_bright_edges_are_still_dropped():
    """反例：图标的亮边漏进数字区（几 px 高），不管是单独一块、在数字旁边、还是在数字正上方隔得远，都要去掉。"""
    assert G.digit_blobs([(10, 0, 6, 4)]) == [False]  # 单独一小块
    assert G.digit_blobs([(20, 6, 4, 11), (26, 12, 3, 3)]) == [True, False]  # 在数字右边，左右不重叠
    assert G.digit_blobs([(20, 6, 4, 11), (20, 0, 5, 2)]) == [True, False]  # 在数字正上方，隔了 4 px
    assert G.digit_blobs([(10, 2, 5, 3), (11, 7, 5, 3)]) == [False, False]  # 两小块挨着，合起来也只有 8 px 高
    assert G.digit_blobs([]) == []


# ---------------------------------------------------------------- 和上次读的不一样要说

def test_changed_count_is_reported():
    old = {"仓库取货口": 15, "提纯机": 3, "管道": None}
    assert P.devices_changed(old, {"仓库取货口": 1, "提纯机": 3, "管道": None}) == ["仓库取货口 15 → 1"]
    assert P.devices_changed(old, {"仓库取货口": 15, "提纯机": 3, "封装机": 2, "管道": None}) == ["封装机 没有 → 2"]
    assert P.devices_changed(old, {"仓库取货口": 15, "管道": None}) == ["提纯机 3 → 没有"]


def test_first_read_and_same_read_say_nothing():
    """反例：第一次读（没有旧的）、两次一样、只有没数量的物流件不一样：不提。"""
    new = {"仓库取货口": 15, "提纯机": 3, "管道": None}
    assert P.devices_changed(None, new) == []
    assert P.devices_changed({}, new) == []
    assert P.devices_changed(dict(new), new) == []
    assert P.devices_changed({"仓库取货口": 15, "提纯机": 3, "传送带": None}, new) == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
