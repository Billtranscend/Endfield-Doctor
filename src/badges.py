"""俯瞰里设备的状态标记识别。

目前确认的两种（2026-11-04 在游戏里点开设备核对）：
  blocked  红色圆环加斜杠 → 详情面板状态「阻塞」（输出满了，下游不收）
  idle     浅蓝色「zz」   → 详情面板状态「空闲中」（没有原料进来）

做法：模板只保留标记本身的像素（按颜色做遮罩），用带遮罩的模板匹配，避开背景干扰。
模板来自 resource/badges/*.png，由 make_templates() 从截图里裁出。
"""

from dataclasses import dataclass
from pathlib import Path

import cv2
from paths import imread as _imread, imwrite as _imwrite  # cv2.imread/imwrite 不认中文路径
import numpy as np

from paths import ROOT

BADGE_DIR = ROOT / "resource" / "badges"

# 俯瞰画面里会被界面挡住、不该找标记的区域（720p：x, y, w, h）
UI_MASKS = [
    (0, 0, 340, 260),  # 左上：小地图、任务
    (520, 10, 240, 70),  # 顶部：理智、进出俯瞰提示
    (1060, 0, 220, 720),  # 右侧：菜单按钮
    (0, 545, 1280, 175),  # 底部：快捷建造栏
]

# 每种标记：从哪张截图、哪个位置裁模板，以及标记像素的颜色条件（BGR）
SPECS = {
    "blocked": {"label": "阻塞", "color": lambda b, g, r: (r > 190) & (g < 150) & (b < 160) & (r - g > 70)},
    # 2026-11-06 应龙关 21 屏统计：真实空闲 zz 13.6–24.1 分；37–45 分的全是误匹配（次级核心白色花瓣、仓库取货口/存取线基段、
    # 息壤供电桩——点开面板都是「运行中」）。中间 25–35 是空档，阈值定 32
    "idle": {"label": "空闲中", "color": lambda b, g, r: (b > 170) & (g > 130) & (r < 150) & (b - r > 60), "max_diff": 32.0},
    # 关闭（用户手动关掉的设备）：设备顶部灰色圆圈叉（直径约 22 像素）。模板从 720p 俯瞰截图裁出，
    # 灰白色的东西太多，所以这种标记用更紧的 max_diff
    # 模板遮罩是整个圆盘（含圈内暗色区域），只比灰色像素时「仓…」取货口一排都会误检。
    # 阈值 42：同一角度得分 0，Ctrl+R 转镜头后 37.6；15 张无关截图 0 误检（2026-11-04 校准）
    "closed": {"label": "关闭", "color": lambda b, g, r: (abs(r - g) < 28) & (abs(g - b) < 28) & (r > 150), "max_diff": 42.0},
}
PAIR_KINDS: dict[str, tuple[str, int]] = {}  # 某种标记必须在多少像素内有另一种标记才算数（目前没有）

MAX_DIFF = 45.0  # 标记像素与模板的平均颜色差（0~255），超过就不算
SIZE_TOLERANCE = 0.35  # 色块外框和模板尺寸的允许偏差比例


@dataclass
class Badge:
    kind: str
    x: int  # 标记中心，720p 坐标
    y: int
    score: float


def make_templates(screenshot: str, crops: dict[str, tuple[int, int, int, int]]):
    """从截图裁模板：crops = {kind: (x, y, w, h)}。保存为 PNG，alpha 通道就是颜色遮罩。"""
    BADGE_DIR.mkdir(parents=True, exist_ok=True)
    img = _imread(screenshot)
    for kind, (x, y, w, h) in crops.items():
        patch = img[y : y + h, x : x + w]
        b, g, r = (patch[:, :, i].astype(int) for i in range(3))
        mask = SPECS[kind]["color"](b, g, r).astype(np.uint8) * 255
        _imwrite(str(BADGE_DIR / f"{kind}.png"), np.dstack([patch, mask]))


def _load():
    out = {}
    for kind in SPECS:
        im = _imread(str(BADGE_DIR / f"{kind}.png"), cv2.IMREAD_UNCHANGED)
        if im is None:
            continue
        mask = im[:, :, 3] > 0
        ys, xs = np.where(mask)
        out[kind] = {
            "bgr": im[:, :, :3].astype(np.float32),
            "mask": mask,
            "box": (xs.max() - xs.min() + 1, ys.max() - ys.min() + 1),
            "fill": mask.sum() / ((xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)),
        }
    return out


def find_badges(image: np.ndarray) -> list[Badge]:
    """在一张 720p 俯瞰截图里找所有状态标记。

    只比较模板里标记本身的像素（遮罩），算平均颜色差；差异小于 MAX_DIFF 的位置算命中，再去掉相邻的重复。
    """
    work = image.copy()
    for x, y, w, h in UI_MASKS:
        work[y : y + h, x : x + w] = 0
    work = work.astype(np.float32)
    found: list[Badge] = []
    for kind, t in _load().items():
        mask3 = np.repeat(t["mask"][:, :, None], 3, axis=2).astype(np.float32)
        res = cv2.matchTemplate(work, t["bgr"], cv2.TM_SQDIFF, mask=mask3)
        rms = np.sqrt(np.maximum(res, 0) / (mask3.sum()))  # 每个标记像素、每个通道的均方根差
        th, tw = t["mask"].shape
        limit = SPECS[kind].get("max_diff", MAX_DIFF)
        while True:
            val, _, (x, y), _ = cv2.minMaxLoc(rms)
            if val > limit:
                break
            found.append(Badge(kind, int(x + tw // 2), int(y + th // 2), round(float(val), 1)))
            rms[max(0, y - th) : y + th, max(0, x - tw) : x + tw] = np.inf  # 抑制邻近重复
    # 需要成对出现的标记：没有搭档的丢掉；搭档本身只是辅助线索，不单独报
    helpers = {h for h, _ in PAIR_KINDS.values()}
    kept = []
    for b in found:
        if b.kind in PAIR_KINDS:
            partner, dist = PAIR_KINDS[b.kind]
            if not any(o.kind == partner and abs(o.x - b.x) <= dist and abs(o.y - b.y) <= dist for o in found):
                continue
        if b.kind in helpers:
            continue
        kept.append(b)
    return kept
