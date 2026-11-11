"""读设备详情面板底部「当前自动生产中的配方」，认出它在跑哪条配方。

做法：找出配方栏里的图标格子（最右一格是产物，其余是原料），对这台设备能做的每条配方，
把配方里每个物品的 AKEData 图标和对应格子做带透明遮罩的匹配，总差异最小的配方胜出。
这样灌装瓶（底图一样）靠原料格里的液体图标区分，液气转化机的正反向配方靠原料/产物位置区分。
图标按需下载，缓存在 .cache/icons/。
"""

import json
import dataclasses as _dc
import difflib
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from itertools import permutations
from pathlib import Path

import cv2
from paths import imread as _imread  # cv2.imread/imwrite 不认中文路径
import numpy as np

from paths import ROOT  # noqa: E402,F401
ICON_CACHE = ROOT / ".cache" / "icons"
ICON_URL = "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/itemiconbig/{}.png"

RECIPES = json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))
ITEM_ICONS = json.loads((ROOT / "data" / "item_icons.json").read_text(encoding="utf-8"))
ID_NAME = json.loads((ROOT / "data" / "item_id_names.json").read_text(encoding="utf-8"))

RECIPE_BAR = (540, 508, 255, 54)  # 720p：配方栏（不含右侧切换按钮）
TILE_MIN, TILE_MAX = 30, 50  # 图标格子边长范围
MAX_ITEM_DIFF = 70.0  # 单个格子的均方根颜色差超过这个值，就当这个物品没对上


@dataclass
class Match:
    recipe: dict
    score: float  # 各格子差异的平均值，越小越像

    @property
    def output(self) -> str:
        return next(iter(self.recipe["outputs"]))


POOL_NAMES = ("反应池", "扩容反应池")
# 多配方反应池面板（中间转盘 + 右侧「选择输出产物」三格）：没有「当前自动生产中的配方」栏，配方认不出。
# 右侧三格就是这台池子会往外送的产物；悬停格子会弹「物品名 / 材料」提示（转盘格悬停没有提示）。
# 2026-11-06 应龙关 扩容反应池：液化息壤(50)/重息壤(0)/液化重息壤(0)，状态阻塞 → 堵的是液化息壤
POOL_OUTPUT_SLOTS = [(825, 290), (825, 385), (825, 480)]
POOL_TIP_ROI = (-260, -120, 520, 300)  # 提示框相对格子的搜索区
POOL_COUNT_ROI = (-34, 14, 68, 30)  # 格子下方的计数
POOL_FULL = 50  # 格子满 = 产物送不出去


POOL_WHEEL_SLOTS = [(505, 270), (597, 270), (440, 338), (660, 338), (440, 428), (660, 428)]  # 扩容反应池转盘六格（TL TR L R BL BR）
# 普通反应池转盘是五格（T L R BL BR），2026-11-07 武陵城两台反应池面板量的；之前套六格的位置漏读了顶格和底格
POOL_WHEEL_SLOTS_BY_NAME = {
    "扩容反应池": POOL_WHEEL_SLOTS,
    "反应池": [(548, 270), (447, 345), (660, 345), (494, 468), (610, 468)],
}
POOL_WHEEL_CENTER = (552, 385)  # 转盘中心（「配方一览」按钮）
POOL_ARROW_YELLOW = 0.12  # 格子与中心之间的箭头里黄色像素占比 ≥ 这个 = 产物格（原料格箭头是白的）


def pool_wheel_slots(machine_name: str) -> list[tuple[int, int]]:
    return POOL_WHEEL_SLOTS_BY_NAME.get(machine_name, POOL_WHEEL_SLOTS)


def pool_arrow_is_output(image: np.ndarray, x: int, y: int) -> bool | None:
    """转盘格到中心之间的箭头颜色：黄 = 这格是产物（向外），白 = 原料（向内）。2026-11-07 三台池子（含正在生产的）对照：
    黄箭头始终在产物格上，不随运行/阻塞状态变。两种颜色都没测到返回 None（空格）。"""
    cx, cy = POOL_WHEEL_CENTER
    best_y = best_w = 0.0
    for f in (0.35, 0.45, 0.55):
        px, py = int(x + (cx - x) * f), int(y + (cy - y) * f)
        roi = image[py - 14 : py + 14, px - 14 : px + 14]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        h, s_, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        best_y = max(best_y, float(((h >= 20) & (h <= 40) & (s_ > 120) & (v > 140)).mean()))
        best_w = max(best_w, float(((s_ < 40) & (v > 170)).mean()))
    if best_y >= POOL_ARROW_YELLOW:
        return True
    if best_w >= 0.15:
        return False
    return None
POOL_FIBER = ("item_xiranite_powder", "item_xiranite_enr_powder")  # 息壤/重息壤：图标都是绿色绒状，模板分差常只有 3 分，认不准就按「两者之一」


def pool_wheel_slash(image: np.ndarray, x: int, y: int) -> bool:
    """转盘格右上角的红色「禁」斜杠。2026-11-06 三张面板对照：带斜杠的格子计数都是 50（满），液化息壤到 50 时产物格也带——
    所以斜杠是「满」不是「原料」（原料/产物按物品身份分）。只作记录。"""
    tr = image[y - 38 : y - 6, x + 6 : x + 36].astype(int)
    b, g, r = tr[:, :, 0], tr[:, :, 1], tr[:, :, 2]
    return float(((r > 150) & (r - g > 50) & (r - b > 30)).mean()) > 0.05


# 转盘格图标按主色相分大类（OpenCV 色相 0–180；2026-11-06 两张面板实测：沉积酸 10–25、清水 90、息壤绒 70、
# 液化息壤/液化重息壤 水滴 30–50、重息壤（暗）50 左右）。模板分在这些辉光图标上只差 1–3 分，先按色相把候选切开再比模板
POOL_HUE_CLASS = {
    "item_liquid_acid": "orange", "item_liquid_water": "cyan",
    "item_xiranite_powder": "fiber", "item_xiranite_enr_powder": "fiber",
    "item_liquid_xiranite": "drop", "item_liquid_xiranite_enr": "drop",
    "item_liquid_xiranite_lowpoly": "drop", "item_liquid_xiranite_poly": "drop",
    "item_liquid_sewage": "gray", "item_iron_powder": "cyan",  # 蓝铁粉末实测色相 80–110，和清水同类，靠模板分（2026-11-07）
}


def pool_slot_hue(image: np.ndarray, x: int, y: int) -> tuple[str | None, int]:
    """格子图标的主色相类别和亮像素数。红色「禁」斜杠（色相 ≥ 165）不计。"""
    t = image[y - 30 : y + 16, x - 22 : x + 22]
    hsv = cv2.cvtColor(t, cv2.COLOR_BGR2HSV)
    m = (hsv[:, :, 2] > 90) & (hsv[:, :, 1] > 60) & (hsv[:, :, 0] < 165) & (hsv[:, :, 0] >= 6)
    m[:26, 26:] = False  # 右上角的红色「满」斜杠不参与（2026-11-07：绿水滴被斜杠带成 orange）
    n = int(m.sum())
    if n < 20:
        return None, n
    h = int(np.bincount(hsv[:, :, 0][m] // 10, minlength=18).argmax() * 10)
    if h < 30:
        return "orange", n
    if 80 <= h <= 110:
        return "cyan", n
    if 55 <= h < 80:
        return "fiber", n
    if 28 <= h < 55:
        return "drop", n
    return None, n


def pool_wheel_item(image: np.ndarray, x: int, y: int, cands: list[str]) -> str | None:
    """转盘格里的物品：先按主色相把候选切成一类（橙=沉积酸、青=清水、绒=息壤/重息壤、水滴=液化息壤/液化重息壤…），
    再在这一类里比模板。息壤/重息壤分不开时返回 "item_xiranite_powder|item_xiranite_enr_powder"；都不像返回 None。"""
    if not cands:
        return None
    cls, _ = pool_slot_hue(image, x, y)
    # 色相认得出：只留这一类的候选 + 没登记色相类别的候选（壤晶、芽针粉末等没登记的仍可比模板）
    pool = [c for c in cands if POOL_HUE_CLASS.get(c) in (cls, None)] if cls else []
    if cls and not pool:
        return None  # 候选全都登记了别的色相类别：不是候选里的东西（2026-11-07 绒状格被硬套成污水）
    if not pool:
        pool = cands  # 色相认不出类别：退回全部候选比模板
    if len(pool) == 1:
        return pool[0]
    tile = image[y - 32 : y + 20, x - 26 : x + 26]
    scored = sorted((_icon_diff_free(tile, icon_ids(o)[0], sizes=(36, 40, 44, 48)), o) for o in pool)
    best_d, best = scored[0]
    if best_d > 40 and pool is cands:
        return None
    if best in POOL_FIBER and len(scored) > 1 and scored[1][1] in POOL_FIBER and scored[1][0] - best_d < 5:
        return "|".join(POOL_FIBER)
    return best


def pool_input_candidates(outputs: list[str]) -> list[str]:
    """能产出这些产物的反应池配方的原料集合。"""
    out = []
    for r in RECIPES:
        if r["machine_name"] in POOL_NAMES and set(r["outputs"]) & set(outputs):
            out += [i for i in r["inputs"] if i not in out]
    return out


@_dc.dataclass
class PoolMatch(Match):
    """反应池：右侧输出格读到的 [(物品 id, 计数或 None)] + 转盘里有物品的格子 [(物品 id 或 None, 计数, 是否原料)]。
    recipe 是合成的（id "pool:{json}"），不在配方表里。"""
    slots: list = _dc.field(default_factory=list)
    wheel: list = _dc.field(default_factory=list)

    @property
    def output(self) -> str:
        full = [i for i, c in self.slots if c is not None and c >= POOL_FULL]
        if len(full) == 1:
            return full[0]  # 阻塞时满的那格就是送不出去的产物
        return "|".join(i for i, _ in self.slots)


def make_pool_match(machine_name: str, slots: list[tuple[str, int | None]], wheel: list | None = None) -> PoolMatch:
    ids = [i for i, _ in slots]
    wheel = list(wheel or [])
    rid = "pool:" + json.dumps({"out": ids, "wheel": [[i, c, bool(inp)] for i, c, inp in wheel]}, ensure_ascii=False, separators=(",", ":"))
    recipe = {"id": rid, "machine_name": machine_name, "inputs": {}, "outputs": {i: 1 for i in ids}, "seconds": 0}
    return PoolMatch(recipe=recipe, score=0.0, slots=list(slots), wheel=wheel)


def parse_pool_id(recipe_id: str | None) -> dict | None:
    """Device.recipe_id "pool:{json}" → {"out": [...], "wheel": [[id|None, count|None, is_input], ...]}；旧格式 "pool:a|b|c" 只有 out。"""
    if not recipe_id or not recipe_id.startswith("pool:"):
        return None
    body = recipe_id[5:]
    if body.startswith("{"):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None
    return {"out": [i for i in body.split("|") if i], "wheel": None}


@lru_cache(maxsize=None)
def _name_index() -> dict[str, str]:
    return {label(i): i for i in ALL_ITEMS}


def item_by_name(text: str) -> str | None:
    """OCR 出来的物品名 → 物品 id（精确优先，其次 difflib ≥ 0.75）。太短的字不猜。"""
    t = (text or "").strip().replace(" ", "")
    if len(t) < 2:
        return None
    idx = _name_index()
    if t in idx:
        return idx[t]
    hit = difflib.get_close_matches(t, idx.keys(), n=1, cutoff=0.75)
    return idx[hit[0]] if hit else None


@lru_cache(maxsize=None)
def icon(icon_id: str) -> np.ndarray | None:
    path = ICON_CACHE / f"{icon_id}.png"
    if not path.exists():
        ICON_CACHE.mkdir(parents=True, exist_ok=True)
        try:
            # AKEData 会拒绝 Python 默认的 User-Agent（403），用通用的浏览器标识
            req = urllib.request.Request(ICON_URL.format(icon_id), headers={"User-Agent": "Mozilla/5.0"})
            path.write_bytes(urllib.request.urlopen(req, timeout=30).read())
        except Exception:
            return None
    im = _imread(str(path), cv2.IMREAD_UNCHANGED)
    if im is None or im.shape[2] != 4:
        return None
    ys, xs = np.where(im[:, :, 3] > 30)
    return im[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]


LABEL_BAND = (540, 480, 460, 32)  # 「当前自动生产中的配方」标签所在的横带（标签在配方栏正上方、左对齐）
LABEL_TO_CENTER = 127.5  # 标签左缘到配方栏中心的距离（标准布局 548 → 675.5；气体产出布局 676 → 803.5）


def row_center(image: np.ndarray) -> float:
    """配方栏的横向中心。大多数面板配方栏在 ROW_CENTER；带「气体产出」开关的面板（固气转化机等）整栏右移约 128 像素，
    右边没有「全部收取」按钮（2026-11-05 应龙关：产物格落在旧搜索区外面，赤铜块→气态赤铜被认成息壤→息壤气）。
    按标签「当前自动生产中的配方」的左缘定位：取横带里第一个宽 ≥100 像素的亮字簇。找不到按 ROW_CENTER。"""
    x0, y0, w, h = LABEL_BAND
    band = image[y0 : y0 + h, x0 : x0 + w]
    bright = (band.min(axis=2) > 170).sum(axis=0)
    cols = np.where(bright >= 1)[0] + x0
    start = prev = None
    for c in cols:
        if start is None:
            start = c
        elif c - prev > 12:
            if prev - start >= 100:
                return float(start) + LABEL_TO_CENTER
            start = c
        prev = c
    if start is not None and prev - start >= 100:
        return float(start) + LABEL_TO_CENTER
    return ROW_CENTER


def find_tiles(image: np.ndarray, center: float | None = None) -> list[tuple[int, int, int, int]]:
    """配方栏里的浅色图标格子，从左到右。最右边的是产物。center：配方栏横向中心（见 row_center）。"""
    x0, y0, w, h = RECIPE_BAR
    if center is not None:
        x0 = int(round(center - w / 2))
    bar = image[y0 : y0 + h, x0 : x0 + w]
    light = (bar.min(axis=2) > 185).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(light)
    tiles = []
    for k in range(1, n):
        x, y, tw, th, area = stats[k]
        if TILE_MIN <= tw <= TILE_MAX and TILE_MIN <= th <= TILE_MAX and area > 0.3 * tw * th:
            tiles.append((int(x0 + x), int(y0 + y), int(tw), int(th)))
    return sorted(tiles)


# 灌装瓶 / 耐压罐在游戏里画成「空容器底图 + 液滴叠加」，和 AKEData 的灌装图标差很多，
# 所以这类物品额外拿对应空容器的图标做备选
BASE_CONTAINER = {
    "item_fbottle_iron_": "item_iron_bottle",
    "item_fbottle_ironenr_": "item_iron_enr_bottle",
    "item_fbottle_copper_": "item_copper_bottle",
    "item_fbottle_copperenr_": "item_copper_enr_bottle",
    "item_fbottle_glass_": "item_glass_bottle",
    "item_fbottle_glassenr_": "item_glass_enr_bottle",
    "item_gasjar_copper_": "item_copper_jar",
}


FLUID_SUFFIX = {  # 灌装瓶 id 后缀 → 液体/气体物品 id
    "water": "item_liquid_water", "acid": "item_liquid_acid", "copper": "item_liquid_copper",
    "copper_enr": "item_liquid_copper_enr", "grass_1": "item_liquid_plant_grass_1", "grass_2": "item_liquid_plant_grass_2",
    "sewage": "item_liquid_sewage", "xiranite": "item_liquid_xiranite", "xiranite_enr": "item_liquid_xiranite_enr",
    "xiranite_lowpoly": "item_liquid_xiranite_lowpoly", "xiranite_poly": "item_liquid_xiranite_poly",
}


def fluid_of(item_id: str) -> str | None:
    """灌装瓶/耐压罐里装的液体或气体的物品 id。"""
    for prefix in BASE_CONTAINER:
        if item_id.startswith(prefix):
            rest = item_id[len(prefix):]
            if rest.startswith("gas_"):
                return "item_gas_" + rest[4:]
            return FLUID_SUFFIX.get(rest)
    return None


@lru_cache(maxsize=None)
def composite_icon(item_id: str) -> np.ndarray | None:
    """游戏里灌装瓶画成「空容器底图 + 液滴缩小叠在中下部」，按同样方式合成一张图标（带 alpha）。
    液滴高度约为容器高度的 36%，中心在容器高度 58% 处（按简报里的图标量的）。"""
    fluid = fluid_of(item_id)
    base_id = next((b for p, b in BASE_CONTAINER.items() if item_id.startswith(p)), None)
    if fluid is None or base_id is None:
        return None
    base, fl = icon(ITEM_ICONS.get(base_id, base_id)), icon(fluid)
    if base is None or fl is None:
        return None
    out = base.copy()
    H, W = out.shape[:2]
    h = max(4, int(H * 0.36))
    w = max(4, int(fl.shape[1] * h / fl.shape[0]))
    small = cv2.resize(fl, (w, h), interpolation=cv2.INTER_AREA)
    x0, y0 = (W - w) // 2, int(H * 0.58 - h / 2)
    region = out[y0 : y0 + h, x0 : x0 + w].astype(np.float32)
    a = small[:, :, 3:4].astype(np.float32) / 255.0
    region[:, :, :3] = small[:, :, :3] * a + region[:, :, :3] * (1 - a)
    region[:, :, 3] = np.maximum(region[:, :, 3], small[:, :, 3])
    out[y0 : y0 + h, x0 : x0 + w] = region.astype(np.uint8)
    return out


def icon_ids(item_id: str) -> list[str]:
    ids = [ITEM_ICONS.get(item_id, item_id)]
    for prefix, base in BASE_CONTAINER.items():
        if item_id.startswith(prefix) and base in ITEM_ICONS:
            ids.append(ITEM_ICONS[base])
    return ids


def item_diff(tile: np.ndarray, item_id: str) -> float:
    """物品图标缩放到几种尺寸在格子里滑动，返回遮罩内最小均方根差；没图标返回 inf。"""
    return min(_icon_diff(tile, i) for i in icon_ids(item_id))


LABEL_ROWS = 6  # 格子底部的数量标签「1」占的行数，算前景时去掉
FG_THRESHOLD = 30  # 和背景色差超过这个值算前景


def foreground_box(tile: np.ndarray) -> tuple[int, int, int, int] | None:
    """格子里实际画出图标的范围 (x0, y0, x1, y1)。背景色取四边中位数。"""
    border = np.concatenate([tile[0], tile[-1], tile[:, 0], tile[:, -1]])
    bg = np.median(border, axis=0)
    body = tile[: tile.shape[0] - LABEL_ROWS]
    fg = np.abs(body.astype(int) - bg).max(axis=2) > FG_THRESHOLD
    ys, xs = np.where(fg)
    if len(xs) < 20:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _icon_diff(tile: np.ndarray, icon_id: str) -> float:
    return _icon_best(tile, icon_id)[0]


def _icon_diff_free(tile: np.ndarray, icon_id: str, sizes=(22, 26, 30, 34), ic: np.ndarray | None = None) -> float:
    """图标在小块里自由滑动匹配（给俯瞰里的产物图标用：小块就是图标本身，没有浅色背景干扰）。
    传 ic 时直接用给定的图标（如合成的灌装瓶）。"""
    if ic is None:
        ic = icon(icon_id)
    if ic is None:
        return float("inf")
    th, tw = tile.shape[:2]
    tile_f = tile.astype(np.float32)
    best = float("inf")
    for size in sizes:
        s = size / max(ic.shape[:2])
        w, h = max(4, round(ic.shape[1] * s)), max(4, round(ic.shape[0] * s))
        if w > tw or h > th:
            continue
        small = cv2.resize(ic, (w, h), interpolation=cv2.INTER_AREA)
        mask = (small[:, :, 3] > 200).astype(np.float32)
        if mask.sum() < 20:
            continue
        mask3 = np.repeat(mask[:, :, None], 3, axis=2)
        res = cv2.matchTemplate(tile_f, small[:, :, :3].astype(np.float32), cv2.TM_SQDIFF, mask=mask3)
        best = min(best, float(np.sqrt(max(res.min(), 0) / mask3.sum())))
    return best


def _icon_best(tile: np.ndarray, icon_id: str) -> tuple[float, tuple[int, int, int, int] | None]:
    """图标按几种固定大小缩放、在格子中心摆放（只允许几像素微调），算遮罩内均方根颜色差。

    返回（差异, 最佳位置 (x, y, w, h)）。不让图标在格子里随意滑动缩放：
    淡色图标缩小后贴在浅色背景上也能得到很小的差异。tile 必须是 canonical_tiles 给出的标准格子。
    """
    ic = icon(icon_id)
    if ic is None:
        return float("inf"), None
    th, tw = tile.shape[:2]
    tile_f = tile.astype(np.float32)
    cx, cy = ICON_CENTER
    best, best_box = float("inf"), None
    for size in ICON_SIZES:
        s = size / max(ic.shape[:2])
        w, h = max(4, round(ic.shape[1] * s)), max(4, round(ic.shape[0] * s))
        small = cv2.resize(ic, (w, h), interpolation=cv2.INTER_AREA)
        mask = (small[:, :, 3] > 200).astype(np.float32)
        if mask.sum() < 20:
            continue
        mask3 = np.repeat(mask[:, :, None], 3, axis=2)
        sx0, sy0 = cx - w // 2 - JITTER, cy - h // 2 - JITTER
        sx1, sy1 = sx0 + w + 2 * JITTER, sy0 + h + 2 * JITTER
        # 图标比格子大的部分裁掉（遮罩也跟着裁）
        ox0, oy0 = max(0, -sx0), max(0, -sy0)
        ox1, oy1 = w - max(0, sx1 - tw), h - max(0, sy1 - th)
        if ox1 - ox0 < 8 or oy1 - oy0 < 8:
            continue
        tpl = small[oy0:oy1, ox0:ox1, :3].astype(np.float32)
        m3 = mask3[oy0:oy1, ox0:ox1]
        region = tile_f[max(0, sy0) : min(th, sy1), max(0, sx0) : min(tw, sx1)]
        if region.shape[0] < tpl.shape[0] or region.shape[1] < tpl.shape[1] or m3.sum() < 20:
            continue
        res = cv2.matchTemplate(region, tpl, cv2.TM_SQDIFF, mask=m3)
        val, _, loc, _ = cv2.minMaxLoc(res)
        d = float(np.sqrt(max(val, 0) / m3.sum()))
        if d < best:
            best = d
            best_box = (max(0, sx0) + loc[0], max(0, sy0) + loc[1], tpl.shape[1], tpl.shape[0])
    return best, best_box


def split_tiles(tiles: list[tuple[int, int, int, int]]) -> tuple[list, list]:
    """按格子间最大的横向空隙（「»  2秒」箭头所在）切成原料格和产物格。"""
    if len(tiles) < 2:
        return tiles, []
    gaps = [tiles[i + 1][0] - (tiles[i][0] + tiles[i][2]) for i in range(len(tiles) - 1)]
    k = max(range(len(gaps)), key=lambda i: gaps[i]) + 1
    return tiles[:k], tiles[k:]


# 配方栏格子的标准布局（720p，2026-11-04 从灌装机 2+1、配件机 1+1、精炼炉 2+2 三种面板量得）：
# 整行居中于 ROW_CENTER；格子宽 TILE_W、间距 PITCH；原料和产物之间是 79 像素的「»  2秒」箭头块。
# 行宽 = PITCH × 格数 + 71。
TILE_Y, TILE_W, TILE_H, PITCH = 514, 42, 40, 46
ROW_CENTER = 675.5
ARROW_BLOCK = 79
ICON_SIZES = tuple(range(24, 38, 2))  # 图标在格子里画出来的大小（最长边像素）
ICON_CENTER = (21, 18)  # 图标中心相对格子左上角
JITTER = 4


def canonical_tiles(image: np.ndarray) -> tuple[list, list]:
    """数出原料格、产物格各几个，再按标准布局给出每个格子的位置（比浅色连通块的边界可靠）。"""
    center = row_center(image)
    raw = find_tiles(image, center)
    ins, outs = split_tiles(raw)
    n_in, n_out = len(ins), len(outs)
    n = n_in + n_out
    # 需要气体环境的配方（天有洪炉、提纯机等）上方多一行「稳定环境」标签，整排格子会下移约 10 像素，
    # 所以纵坐标取实际检出的格子
    y = int(np.median([t[1] for t in raw])) if raw else TILE_Y
    left = round(center - (PITCH * n + ARROW_BLOCK - 8) / 2)
    in_x = [left + PITCH * i for i in range(n_in)]
    out_left = left + PITCH * n_in - (PITCH - TILE_W) + ARROW_BLOCK
    out_x = [out_left + PITCH * j for j in range(n_out)]
    box = lambda x: (x, y, TILE_W, TILE_H)  # noqa: E731
    return [box(x) for x in in_x], [box(x) for x in out_x]


def is_filled_container(item_id: str) -> bool:
    return item_id.startswith(("item_fbottle_", "item_gasjar_"))


def _assign(tiles: list[np.ndarray], items: list[str]) -> tuple[float, int, int] | None:
    """把 items 分配到 tiles（一一对应，试全部排列取总差异最小），返回（差异之和, 没对上的个数, 计分格数）。

    数量对不上返回 None。没对上的物品按 MAX_ITEM_DIFF 罚分，不直接否决。
    灌装瓶/耐压罐在游戏里是「空容器 + 液滴」叠画，和哪个图标都对不上，所以不计分，配方由其余格子决定。
    """
    if len(tiles) != len(items):
        return None
    diffs = [[item_diff(t, it) for it in items] for t in tiles]
    best = None
    for perm in permutations(range(len(items))):
        total, missed, counted = 0.0, 0, 0
        for ti, j in enumerate(perm):
            if is_filled_container(items[j]):
                continue
            counted += 1
            if diffs[ti][j] > MAX_ITEM_DIFF:
                missed += 1
                total += MAX_ITEM_DIFF * 1.5
            else:
                total += diffs[ti][j]
        if best is None or total < best[0]:
            best = (total, missed, counted)
    return best


def identify_recipe(image: np.ndarray, machine_name: str) -> Match | None:
    """认出面板里正在跑的配方。认不出返回 None。"""
    ins, outs = canonical_tiles(image)
    if not ins and not outs:
        return None
    in_tiles = [image[y : y + h, x : x + w] for x, y, w, h in ins]
    out_tiles = [image[y : y + h, x : x + w] for x, y, w, h in outs]
    best: Match | None = None
    for r in RECIPES:
        if r["machine_name"] != machine_name:
            continue
        outputs = list(r["outputs"])
        if len(out_tiles) < len(outputs):
            outputs = outputs[: len(out_tiles)]  # 面板少画了产物，只比画出来的
        s_out = _assign(out_tiles, outputs)
        s_in = _assign(in_tiles, list(r["inputs"]))
        if s_out is None or s_in is None:
            continue
        counted = s_out[2] + s_in[2]
        if counted == 0 or s_out[1] + s_in[1] > counted // 2:
            continue  # 没有能计分的格子，或一半以上没对上：不是这条
        score = (s_out[0] + s_in[0]) / counted
        if best is None or score < best.score:
            best = Match(r, round(score, 1))
    return best


SLOT_AREA = (540, 200, 460, 290)  # 面板里设备示意图的范围：原料格、产物格都画在这里
SLOT_ICON_SIZES = (40, 44, 48, 52, 56)  # 原料格里的图标比配方栏的大
SLOT_MARGIN = 8.0  # 配方原料的差异不比最像的物品差这么多，就当格里是配方原料
ALL_ITEMS = sorted({i for r in RECIPES for i in (*r["inputs"], *r["outputs"])})  # 生产链里会出现的物品


def warning_marks(image: np.ndarray) -> list[tuple[int, int]]:
    """设备示意图里黄色圆底「!」的中心（画在出问题的原料格正上方约 58 像素）。"""
    x0, y0, w, h = SLOT_AREA
    reg = image[y0 : y0 + h, x0 : x0 + w]
    yel = ((reg[:, :, 2] > 200) & (reg[:, :, 1] > 170) & (reg[:, :, 0] < 80)).astype(np.uint8)
    n, _, stats, cen = cv2.connectedComponentsWithStats(yel)
    return [
        (int(cen[k][0]) + x0, int(cen[k][1]) + y0)
        for k in range(1, n)
        if stats[k][4] > 150 and 18 <= stats[k][2] <= 40 and 18 <= stats[k][3] <= 40
    ]


def slot_item(image: np.ndarray, mark: tuple[int, int], prefer: list[str]) -> tuple[str | None, float]:
    """「!」下面那个原料格里是什么物品：在配方原料 + 生产链全部物品里找最像的。
    配方原料只要不比最像的差太多（SLOT_MARGIN）就算是它（滑动模板匹配偏宽松，赤铜块对砂叶格也能到 64）。
    返回（物品 id 或 None, 差异）。2026-11-05 用应龙关砂叶格校准：砂叶 27、源石粉末 38、赤铜块 64。"""
    cx, cy = mark
    tile = image[cy + 30 : cy + 92, cx - 32 : cx + 32]  # 去掉顶上的黄色箭头尖和底部数量标签
    if tile.shape[0] < 60 or tile.shape[1] < 60:
        return None, float("inf")

    def diff(item_id: str) -> float:
        return min((_icon_diff_free(tile, i, sizes=SLOT_ICON_SIZES) for i in icon_ids(item_id)), default=float("inf"))

    scores = {it: diff(it) for it in set(prefer) | set(ALL_ITEMS) if not is_filled_container(it)}
    if not scores:
        return None, float("inf")
    best = min(scores, key=scores.get)
    best_d = scores[best]
    for it in prefer:
        if scores.get(it, float("inf")) <= min(MAX_ITEM_DIFF, best_d + SLOT_MARGIN):
            return it, scores[it]
    return (best, best_d) if best_d <= MAX_ITEM_DIFF else (None, best_d)


GAUGE_X, GAUGE_Y = 806, 260  # 设备示意图顶部的供给气表（息壤气供给）：「!」在它正上方 = 供给不足，不是原料格
INPUT_MAX_X = 790  # 原料格都在示意图左半边；右半边的「!」是产物格/出口（阻塞时出现）


def classify_mark(mark: tuple[int, int]) -> str:
    """「!」是谁的：gauge（供给气表）/ input（原料格）/ output（产物格或出口）。
    2026-11-05 应龙关 41 张面板：原料格「!」在 (724,332)；气表「!」在 (806,215)（面板下方写「息壤气供给不足」）；
    阻塞的配件机/固气转化机「!」在出口 (940,228) 或产物格 (966,295) 上方。"""
    cx, cy = mark
    if abs(cx - GAUGE_X) < 30 and cy < GAUGE_Y:
        return "gauge"
    return "input" if cx < INPUT_MAX_X else "output"


def gauge_warning(image: np.ndarray) -> bool:
    return any(classify_mark(m) == "gauge" for m in warning_marks(image))


def foreign_inputs(image: np.ndarray, recipe: Match | None) -> list[str]:
    """原料格上有「!」且格里的物品不是配方原料：返回这些物品的 id（认不出的记 "?"）。
    2026-11-05 应龙关 F：准入口把赤铜块改成砂叶，固气转化机的原料格堆了 50 砂叶、顶上黄色「!」，面板「空闲中」。
    格里就是配方原料的、或认成了配方产物的（其实是产物格）不算；气表和出口上的「!」不算。"""
    inputs = list(recipe.recipe["inputs"]) if recipe else []
    outputs = list(recipe.recipe["outputs"]) if recipe else []
    out = []
    for mark in warning_marks(image):
        if classify_mark(mark) != "input":
            continue
        it, _ = slot_item(image, mark, inputs + outputs)
        if it in inputs or it in outputs:
            continue
        out.append(it or "?")
    return out


def identify_output(image: np.ndarray, machine_name: str) -> tuple[str | None, float]:
    """兼容旧接口：返回（产物物品 id, 差异）。"""
    m = identify_recipe(image, machine_name)
    return (m.output, m.score) if m else (None, float("inf"))


def label(item_id: str | None) -> str:
    """显示名：灌装瓶带上灌装物，如「赤铜瓶(芽针溶液)」。"""
    if item_id is None:
        return "?"
    name = ID_NAME.get(item_id, item_id)
    for prefix, kind in (("item_fbottle_", "liquid"), ("item_gasjar_", "gas")):
        if item_id.startswith(prefix):
            for fid, fname in ID_NAME.items():
                if fid.startswith(f"item_{kind}_") and item_id.endswith("_" + fid.split("_", 2)[2].replace("plant_", "")):
                    return f"{name}({fname})"
    return name


NO_RECIPE = "（没有在跑的配方）"
RECIPE_LIST_BTN = (580, 522, 200, 30)  # 没设配方时配方栏位置只有一个「本设备可自动生产的配方一览」按钮，文字在这一带


def has_recipe_label(image: np.ndarray) -> bool:
    """配方栏上方有没有「当前自动生产中的配方」标签（宽 ≥100 像素的亮字簇）。"""
    return row_center(image) != ROW_CENTER or bool(find_tiles(image))


def no_recipe_set(image: np.ndarray) -> bool:
    """面板没设配方：配方栏没有标签和配方瓦片，只有「本设备可自动生产的配方一览」按钮。
    2026-11-05 应龙关：配件机 (624,882)、固气转化机 (1043,985) 都是这样，永远「空闲中」；之前只能记成「配方：?」，
    和「没认出配方」分不开。扩容反应池是多配方转盘布局，配方栏也是空的，但按钮位置没有文字，不会误判。"""
    x, y, w, h = RECIPE_LIST_BTN
    band = image[y : y + h, x : x + w]
    bright_cols = int(((band.min(axis=2) > 170).sum(axis=0) > 0).sum())
    return bright_cols >= 60 and not has_recipe_label(image)


def describe(m: Match | None) -> str:
    if m is None:
        return "配方：?"
    if isinstance(m, PoolMatch):
        text = "池产物：" + "/".join(f"{label(i)}({c if c is not None else '?'})" for i, c in m.slots)
        full = [label(i) for i, c in m.slots if c is not None and c >= POOL_FULL]
        if full:
            text += f"（{'、'.join(full)} 满，送不出去）"
        if m.wheel:
            text += "；池内：" + "/".join(
                f"{'|'.join(label(x) for x in (i or '?').split('|')) if i else '?'}({c if c is not None else '?'}{'，原料' if inp else ''})"
                for i, c, inp in m.wheel
            )
        return text
    ins = " + ".join(f"{label(i)}×{n}" for i, n in m.recipe["inputs"].items())
    outs = " + ".join(f"{label(o)}×{n}" for o, n in m.recipe["outputs"].items())
    return f"{ins} → {outs}（{m.recipe['seconds']}秒）"
