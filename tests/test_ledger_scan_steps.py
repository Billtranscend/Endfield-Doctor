"""扫底账：每一步镜头走了多少、什么时候算顶到边（2026-11-13 issue #1 第二轮，一位蓝图作者的武陵城）。

他的武陵城 5 张蓝图「拼到基地」全部不确定、底账核对图错位。扫描记录（scan.json）里 13 行有 4 行只拍了 6、2、5、3 屏
（整行 9 屏），第 0 行和其余各行差 512 px。原因是重复结构的假匹配：
  - 一排排一样的精炼炉横向约 205 px 一台，镜头一步走 390，块匹配峰不唯一没结果，退到 ORB 量成 −20（= 390 − 2×205）；
  - 第一次下移时右边一列水泵纵向约 171 px 一台，ORB 量成 −262（= 250 − 3×171）。
  原来「ORB 量到的步长 < 正常的 30%」直接当顶到边收行，下移不查正负号。
改：ledger_scan.step_shift 只收像一步按键的测量，量不出按正常步长暂记；收行只认「再按一步画面真的停了」；
整步 / 半步两种读法冲突时，下一步画面停不停来定。

样本 tests/data/ledger_steps/：他那次扫描里量错的几步前后的截图（只留有效区、灰度、JPEG；UID 和界面文字都不在有效区里）。
  009→010 第一次下移（真位移约 +250，当时记 −262）；041→042 往右（+390，记 −20）；
  043→044 往左（−386，记 −25）；076→077 往右顶到边的半步（约 +274，记 +2）。
模拟镜头的两条用造出来的地图跑整个 scan()：改之前（0.10.7 的 ledger_scan）在同一张图上 5 行里 3 行没拍完、位置最多差 750 px。
"""
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import ledger_scan as L  # noqa: E402

DATA = ROOT / "tests" / "data" / "ledger_steps"


def rd(n: int) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(DATA / f"{n:03d}.jpg"), np.uint8), cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- 一步走了多少（真截图）

def test_first_down_step_is_not_recorded_backwards():
    """009→010：往下按了一步，画面里一列一样的水泵让 ORB 量成 −262。下移不可能是负的：量不出就按正常步长记。"""
    v, how, sure, _ = L.step_shift(rd(9), rd(10), 1, 1, 250.0)
    assert 150 <= v <= 350, (v, how)
    assert how != "still"


def test_expected_step_recovers_shift_on_repeated_machines():
    """041→042：一排一样的精炼炉，不给预期的块匹配没结果、ORB 量成 −20；按正常步长当预期能量对（389）。"""
    v, how, sure, _ = L.step_shift(rd(41), rd(42), 0, 1, 390.0)
    assert sure and how == "block" and abs(v - 389) <= 8, (v, how, sure)


def test_alias_is_never_taken_for_a_small_step():
    """043→044：往左一整步（−386），当时 ORB 量成 −25，被当成顶到左边，这一行只拍了 2 屏。
    现在要么量对，要么说量不出、按正常步长暂记；不能给出一个「确定的小步」。"""
    v, how, sure, _ = L.step_shift(rd(43), rd(44), 0, -1, 390.0)
    assert how != "still"
    assert (not sure and v == -390.0) or (sure and abs(v + 386) <= 15), (v, how, sure)


def test_half_step_at_the_edge_is_left_open():
    """076→077：顶到右边的半步（约 +274），ORB 量成 +2。量不出来，不能当成画面没动，也不能当成确定的结果。"""
    v, how, sure, _ = L.step_shift(rd(76), rd(77), 0, 1, 390.0)
    assert how != "still"
    assert (not sure) or abs(v - 274) <= 20, (v, how, sure)


def test_same_frame_is_still():
    """反例：画面真的没变（顶到边以后再按一步）才是 still。"""
    assert L.step_shift(rd(41), rd(41), 0, 1, 390.0)[:3] == (0.0, "still", True)
    assert L.step_shift(rd(9), rd(9), 1, 1, 250.0)[:3] == (0.0, "still", True)


# ---------------------------------------------------------------- 整个 scan()（模拟镜头）

S = 1.73
XMAX, YMAX = 2050, 980  # 镜头能走的范围：横向 5 整步 + 95 的半步，纵向 3 整步 + 233


def make_world(seed: int, repeated: bool) -> np.ndarray:
    """造一张近景大地图：草地（低对比）+ 140 台各不相同的设备；repeated 时再加三排一样的机器（横向 205 px 一台）、
    右上一片一样的水泵（纵向 171 px 一台，左右两列相隔 340 px）。"""
    rng = np.random.default_rng(seed)
    W, H = XMAX + 1280, YMAX + 720
    g = cv2.GaussianBlur(rng.normal(90, 10, (H, W)).astype(np.float32), (0, 0), 3)

    def tile(w, h):
        t = (rng.random((h // 10 + 1, w // 10 + 1)) > 0.5).astype(np.float32) * 150 + 40
        t = cv2.resize(t, (w, h), interpolation=cv2.INTER_NEAREST)
        t[:6] = t[-6:] = 20
        t[:, :6] = t[:, -6:] = 20
        return cv2.GaussianBlur(t, (0, 0), 1.2)

    for _ in range(140):
        x, y = int(rng.integers(0, W - 110)), int(rng.integers(0, H - 110))
        g[y:y + 100, x:x + 100] = tile(100, 100)
    if repeated:
        t = tile(190, 180)
        for y in (330, 560, 790):
            for x in range(220, 2900, 205):
                g[y - 15:y + 215, x - 8:x + 197] = 90
                g[y:y + 180, x:x + 190] = t
        t = tile(240, 150)
        for y in range(0, 700, 171):
            g[y:y + 171, 2380:3120] = 90
            for x in (2420, 2760):
                g[y + 10:y + 160, x:x + 240] = t
    return cv2.cvtColor(np.clip(g, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


class FakePatrol:
    """按方向键 = 镜头按 711 px/s 走，到边停；滚轮 = 从最远缩放切到近景。"""
    hwnd = 0
    area = ""

    def __init__(self, world):
        self.world, self.far, self.cam = world, True, np.zeros(2)

    def scene(self):
        return "Scene_Overhead"

    def go_corner(self):
        self.far, self.cam = True, np.zeros(2)

    def zoom_in(self):
        if self.far:
            self.far, self.cam = False, np.array([640.0, 330.0]) * (S - 1)

    def zoom_out(self):
        self.far = True

    def shot(self):
        if self.far:
            return cv2.resize(self.world[:int(720 * S), :int(1280 * S)], (1280, 720), interpolation=cv2.INTER_AREA)
        x, y = int(round(self.cam[0])), int(round(self.cam[1]))
        return self.world[y:y + 720, x:x + 1280].copy()

    def hold(self, vk, seconds):
        d = {0x41: (-1, 0), 0x44: (1, 0), 0x57: (0, -1), 0x53: (0, 1)}[vk]
        self.cam = np.clip(self.cam + np.array(d) * 711.0 * seconds, 0, [XMAX, YMAX])

    def ocr_boxes(self, roi):
        return []


def sim_scan(seed: int, repeated: bool):
    """跑一遍 scan()，返回 {行: [(镜头真正的位置, 记下的位置 − 真正的位置)]}。不写截图、不等待、不碰游戏。"""
    p = FakePatrol(make_world(seed, repeated))
    stubs = {"game": types.SimpleNamespace(move_cursor=lambda *a: None, wheel=lambda *a: p.zoom_in()),
             "patrol": types.SimpleNamespace(PAN_KEYS={"left": 0x41, "right": 0x44, "up": 0x57, "down": 0x53}),
             "offline_ocr": types.SimpleNamespace(shared=lambda: types.SimpleNamespace(boxes=lambda img, roi: []))}
    saved = {k: sys.modules.get(k) for k in stubs}
    time0, wr0 = L.time, L._wr
    truth = {}
    out = Path(tempfile.mkdtemp(prefix="simscan_"))
    try:
        sys.modules.update(stubs)
        L.time = types.SimpleNamespace(sleep=lambda s: None, time=time0.time)
        L._wr = lambda path, im: truth.__setitem__(Path(path).name, p.cam.copy())  # 存这一屏时镜头真正在哪
        L.scan(p, "模拟", out, extent=((XMAX + 1280) / S, (YMAX + 720) / S), log=lambda *a: None, rotate=False)
        rec = json.loads((out / "scan.json").read_text(encoding="utf-8"))
    finally:
        L.time, L._wr = time0, wr0
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        shutil.rmtree(out, ignore_errors=True)
    rows = {}
    for s in rec["screens"]:
        t = truth[s["file"]]
        rows.setdefault(s["row"], []).append((t, np.array(s["cam_near"]) - t))
    return rows


def check_rows(rows):
    assert len(rows) == 6, len(rows)  # 5 行 + 到底时多存的一屏
    for r, xs in rows.items():
        if len(xs) > 1:  # 每一行都从左边界拍到右边界
            assert min(t[0] for t, _ in xs) <= 2 and max(t[0] for t, _ in xs) >= XMAX - 2, (r, [t[0] for t, _ in xs])
        assert max(float(np.abs(e).max()) for _, e in xs) <= 5, (r, [e.round().tolist() for _, e in xs])
    assert [round(xs[0][0][1]) for xs in rows.values()][:5] == [0, 249, 498, 747, 980]


def test_scan_covers_every_row_on_a_repetitive_base():
    """重复结构多的基地：每行拍全，记下的镜头位置和真正的位置差 ≤ 5 px。"""
    for seed in (2, 3):
        check_rows(sim_scan(seed, repeated=True))


def test_scan_on_a_plain_base_is_unchanged():
    """反例：没有重复结构的基地照常扫完，位置一样准。"""
    check_rows(sim_scan(4, repeated=False))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
