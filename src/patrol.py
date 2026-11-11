"""P3：俯瞰巡检。

在俯瞰画面里找设备的状态标记（阻塞 / 空闲），逐个点开详情读设备名和状态，
再用 WASD 平移镜头覆盖整个基地。镜头位移用相位相关估计，换算成「基地坐标」去重。

只点设备本体和详情面板右上角的关闭按钮；不碰面板里的开关、移动、收纳、收取等按钮。

用法（先在游戏里进入俯瞰）：
  .venv\\Scripts\\python.exe src\\patrol.py                    # 只巡检当前画面（快速模式）
  .venv\\Scripts\\python.exe src\\patrol.py --sweep            # 平移镜头覆盖整个基地（快速模式，1–2 分钟）
  .venv\\Scripts\\python.exe src\\patrol.py --sweep --deep     # 逐台点开读名字和配方（慢，按需用）
  加 --area 分区名 会写进结果文件。
快速模式：连拍三张滤掉闪烁的标记，用标记旁的标签首字猜设备名，不点任何东西；
配方由 combine.py 按设备候选产物兜底判断。
"""

import json
from pathlib import Path
import re
import sys
import time
from dataclasses import asdict, dataclass, replace

import cv2
from paths import imread as _imread  # cv2.imread/imwrite 不认中文路径
import numpy as np

from maa.resource import Resource
from maa.tasker import Tasker

import badges
import fluid_icon
import panel_recipe
from game import DEBUG_DIR, ROOT, check_stop, connect, find_game_window, init_toolkit, move_cursor, wheel

PRODUCT_ICON_R = 20  # 俯瞰里设备旁产物图标圆形的半径（720p、最远缩放）
PRODUCT_MAX_DIFF = 60.0  # 产物图标匹配差异超过这个值就当没认出

# ---- 详情面板（720p）----
PANEL_TITLE_ROI = [185, 126, 340, 40]
PANEL_STATUS_ROI = [500, 566, 320, 32]
# 暗管面板的流速「N 流速 单位/秒」：出口面板在左中（panel_pipe.png），入口面板在右侧（panel_pipe2.png），2026-11-06 定位
PIPE_FLOW_ROI = {"暗管出口": [560, 318, 140, 52], "暗管入口": [1030, 318, 150, 52]}  # 含「流速」「单位/秒」字样：确认是流速区
# 大数字单独用 only_rec 读（检测模式把大字和「单」粘在一起、读成 D/O）：2026-11-06 三张入口面板 only_rec 都正确读出「1」
PIPE_FLOW_DIGIT = {"暗管出口": [578, 322, 55, 46], "暗管入口": [1035, 322, 55, 46]}
# 准入口面板（物品/管道准入口）：右下「当前速度 N 单位/分」、左下「限制速度 N 单位/分」。2026-11-06 用户 1999×967 截图按比例换算到 720p
# 720p 实测（gate_493_1260.png）：「当前速度 30 单位/分」大数字在 (690–720, 470–492)；「限制速度」下方「30 单位/分」在 (200–290, 485–510)
GATE_SPEED_ROI = [685, 462, 100, 36]
GATE_LIMIT_ROI = [195, 480, 110, 36]
GATE_TOGGLE = {"开": (418, 469), "关": (464, 469)}  # 「限制速度」开/关：选中的那个是深色药丸底（gate_493_1260.png）
GATE_NAMES = ("管道准入口", "物品准入口")
POWER_ROI = [555, 20, 100, 28]  # 顶部电量框「7215/9800」（俯瞰和基地旁地面都有；2026-11-06 景玉谷有时不显示，读不到记空）
POWER_PCT_ROI = [679, 19, 60, 25]  # 旁边的「100%」  # 暗管入口/出口面板：大数字 + 「流速」「单位/秒」（2026-11-06 panel_pipe.png 定位）
PANEL_ENV_ROI = [730, 210, 160, 36]  # 面板顶部「无环境」提示（天有洪炉等需要气体散布机环境的设备）
PANEL_REGION_ROI = [190, 492, 200, 28]  # 「武陵仓库 | 全部」，用来知道当前在哪个地区
PANEL_CLOSE = (1114, 140)
CORE_PANEL_TITLE_ROI = [30, 14, 260, 30]  # 全屏核心面板左上角标题「// 次级核心-武陵」
CORE_PANEL_CLOSE = (1245, 27)

# 物流、供电类设备：点到它们说明没点中生产设备，换个位置再点
LOGISTICS = {
    "传送带", "管道", "物流桥", "管道桥", "分流器", "汇流器", "管道分流器", "管道汇流器",
    "物品准入口", "管道准入口", "仓库取货口", "仓库存货口", "暗管入口", "暗管出口",
    "供电桩", "息壤中继器", "仓库存取线源桩", "仓库存取线基段",
    "息壤供电桩", "供电终端", "中继器", "多口暗管入口", "多口暗管出口", "污水接入口", "产物排出口",
    "储液罐", "储气罐", "协议储存箱", "协议核心", "次级核心",
}

BUILDING_NAMES = set(json.loads((ROOT / "data" / "building_names.json").read_text(encoding="utf-8")))
BUILDING_NAMES |= set(LOGISTICS)  # 建筑名表里没有准入口等物流件（2026-11-06 管道准入口面板标题认不出 → 核查点不到），补上
# 快速模式按标签首字猜名字时的候选：有配方的生产设备 + 常见物流件（田块、装饰、炮塔等不猜）
GUESSABLE = sorted(
    {r["machine_name"] for r in json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))}
    | {"气体散布机", "气体收集泵", "水泵", "二型耐酸水泵", "水驱矿机", "电驱矿机", "二型电驱矿机", "废水处理机", "协议储存箱"}
    | {"暗管入口", "暗管出口", "多口暗管入口", "多口暗管出口", "仓库取货口", "仓库存货口", "仓库存取线基段", "仓库存取线源桩"}
    | {"物品准入口", "管道准入口", "息壤供电桩", "供电桩", "息壤中继器"}
)

# 详情面板底部的状态词（2026-11-04 实测见过：生产中、阻塞、空闲中、运行中；其余为预留）
STATUS_RE = re.compile(r"(生产中|运行中|空闲中|阻塞|缺料|待机|未激活|停机|供给不足|电力不足|断电)")

# 俯瞰画面里设备标签（图标 + 截断的名称，如「配…」）所在的区域
MAP_ROI = [340, 80, 720, 465]
LABEL_RE = re.compile(r"^[一-鿿]{1,6}[.。·…]*$")

# 找不到标签时的后备点击位置（相对状态标记中心）。
# 阻塞标记画在设备身上；空闲标记画在设备下方产物图标的左下角，常压在传送带上，要往上找。
CLICK_OFFSETS = {
    "blocked": [(0, 0), (0, 35), (0, -30)],
    "idle": [(19, -51), (19, -66), (19, -36), (0, -51)],
    "closed": [(0, 30), (0, 0), (0, 55)],  # 灰圈叉在设备顶部，设备本体在它下方
}

PAN_KEYS = {"left": 0x41, "right": 0x44, "up": 0x57, "down": 0x53}  # A D W S


def container_by_color(tile: np.ndarray) -> str | None:
    """俯瞰产物图标里的容器类型：按圆心附近饱和像素的色相判断。
    返回 "copper"（赤铜瓶，红棕）/ "iron"（蓝铁瓶，蓝）/ "jar"（赤铜耐压罐，灰色金属）/ None（不是容器）。"""
    h, w = tile.shape[:2]
    cy, cx = h // 2, w // 2
    yy, xx = np.ogrid[:h, :w]
    inside = (yy - cy) ** 2 + (xx - cx) ** 2 <= (min(h, w) // 2 - 3) ** 2
    hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
    sat = (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 60) & inside
    total = inside.sum()
    if sat.sum() < 0.08 * total:
        return "jar" if (hsv[:, :, 2][inside] > 90).mean() > 0.25 else None
    hue = hsv[:, :, 0][sat]
    red = ((hue < 14) | (hue > 160)).mean()
    blue = ((hue > 95) & (hue < 130)).mean()
    if red > 0.45 and red > blue:
        return "copper"
    if blue > 0.45 and blue > red:
        return "iron"
    return None


def load_active_items() -> set[str]:
    """最新简报（debug/brief_*.json）里理论或当前有产出/消耗的物品 id（各地区并集）。没有简报就返回空集。"""
    try:
        import diagnose

        path = max(DEBUG_DIR.glob("brief_*.json"), key=lambda p: p.stat().st_mtime)
        brief = json.loads(path.read_text(encoding="utf-8"))
        items = set()
        for rows in brief.values():
            for r in rows:
                if any((r.get("cur_prod"), r.get("cur_cons"), r.get("theo_prod"), r.get("theo_cons"))):
                    iid = diagnose.item_id(r["name"], r["fluid"])
                    if iid:
                        items.add(iid)
        return items
    except (ValueError, OSError, KeyError):
        return set()


@dataclass
class Device:
    name: str
    status: str
    badge: str
    world_x: int  # 基地坐标（以巡检开始时画面左上角为原点，720p 像素）
    world_y: int
    recipe: str = ""  # 可读的配方描述
    output: str | None = None  # 产物物品 id
    recipe_id: str | None = None
    guessed: bool = False  # 快速模式：名字由标签首字猜出、状态由标记而非面板得出
    env: str | None = None  # 深查面板顶部的环境提示：「无环境」= 气体散布机没给它环境气体
    seen: str = ""  # 快速模式：这一屏连拍里标记出现的次数，如 "5/5"
    passes: int = 1  # 两遍扫描里出现的遍数（1 = 只在一遍里出现，多半是瞬时标记）
    foreign: str | None = None  # 面板原料格上有「!」且堆着配方外的物品（显示名，如「砂叶」）：准入口物品设错了
    shot: str = ""  # 看到它的那张每屏截图文件名（shot_dir 下），sx/sy 是标记在那张图里的位置：定位图直接裁这张
    panel: str = ""  # 点开它时存的面板图文件名（panel_dir 下），离线复核配方/入口识别用
    warn: str = ""  # 面板上其他警告：供给气表「!」= 息壤气供给不足
    flow: str = ""  # 暗管入口/出口面板里的流速（「N 单位/秒」里的 N）；错题本 #7.1：右下角暗管应恒定 2/秒
    sx: int = 0
    sy: int = 0


class Patrol:
    def __init__(self):
        self.last_env = None
        self.last_foreign = None  # 最近点开的面板：原料格里堆着的配方外物品（显示名）
        self.open_all = False  # 快速扫描时把每个稳定的生产设备标记当场点开（第二遍用）：标记就在眼前，不会点错邻居
        self.known: list[Device] = []  # 第一遍已经点开过面板的设备：第二遍同位置、同标记的不再点，只看标记还在不在
        self.only_names: set[str] | None = None  # 定向巡检：只点这些设备类型的面板（标签名），其余只记标记
        self.last_panel = ""  # 最近存的面板图文件名
        self.last_no_recipe = False  # 最近点开的面板没设配方（只有「配方一览」按钮）
        self.menu_possible = True  # 上一次点击没开出面板，画面上可能挂着「选择目标」小菜单（ensure_overhead 才去找）
        self.last_flow = ""  # 最近点开的暗管面板流速
        self.flow_samples = 1  # 读流速时连读几次取最大（定点核查用 5）
        self.power = ""  # 顶部电量框「用电/上限」，扫描开始时读一次；错题本 #3：电量超过 9800 → 核心旁放热能池
        self.checks: list[dict] = []  # 错题本定点核查结果（run_checks）
        self.recheck_stats: dict | None = None  # 多时点确认的统计（定向巡检）
        self.last_warn = ""
        self.panel_dir: Path | None = None  # 设了就把每次点开的面板存一张 jpg（离线核对配方/入口识别）
        self.panel_count = 0
        self.trust_nominal = False  # 固定网格扫描时为 True：用标签/中央相关/名义的里程计
        self.cached_labels = None  # 固定网格扫描时上一次 OCR 的标签，pan 和 scan 共用
        self.cached_feats = None  # 上一屏的里程计特征
        self.last_pan_stuck = False
        init_toolkit()
        window = find_game_window()
        if window is None:
            raise SystemExit("没找到游戏窗口。")
        self.hwnd = window.hwnd
        self.controller = connect(window)
        resource = Resource()
        resource.post_bundle(str(ROOT / "resource")).wait()
        self.tasker = Tasker()
        self.tasker.bind(resource, self.controller)
        self.cam = np.array([0.0, 0.0])  # 当前画面左上角的基地坐标
        self.devices: list[Device] = []
        self.region: str | None = None  # 从第一个打开的面板里读到的地区名
        self.active_items = load_active_items()  # 最新简报里有产出/消耗的物品 id，用来缩小产物候选
        self.shot_dir: Path | None = None  # 设了就把快速模式每屏截图存进去
        self.last_screen_has_base: bool | None = None
        self.area: str | None = None  # 分区名，用来找参考图校准坐标原点

    # ---- 底层 ----
    # 耗时统计（2026-11-06 整扫 49 分钟 vs 24 分钟，日志没时间戳查不出慢在哪）：截图/OCR 次数与累计秒数
    stats = {"shot_n": 0, "shot_t": 0.0, "ocr_n": 0, "ocr_t": 0.0}

    def shot(self) -> np.ndarray:
        t = time.perf_counter()
        self.controller.post_screencap().wait()
        self.stats["shot_n"] += 1; self.stats["shot_t"] += time.perf_counter() - t
        return self.controller.cached_image.copy()

    def ocr_boxes(self, roi) -> list[tuple[list[int], str]]:
        """一次 OCR。节点带 timeout=1/rate_limit=0：MaaFW 识别不到东西会按 timeout（默认 20 秒）一直重试，
        2026-11-06 查出核心面板标题区在俯瞰里通常是空的，每次 ensure_overhead 白等 20.3 秒，整扫从 24 分钟拖到 49 分钟。"""
        node = {"Patrol_OCR": {"recognition": "OCR", "roi": roi, "action": "DoNothing", "timeout": 1, "rate_limit": 0}}
        t = time.perf_counter()
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("Patrol_OCR", node).wait().get()
        self.stats["ocr_n"] += 1; self.stats["ocr_t"] += time.perf_counter() - t
        reco = detail.nodes[0].recognition if detail and detail.nodes else None
        return [(list(r.box), r.text.strip()) for r in reco.all_results] if reco else []

    def ocr_rec(self, roi) -> str:
        """只识别不检测：把 roi 当成一行文字整体认（孤立的大数字用）。"""
        node = {"Patrol_OCR_rec": {"recognition": "OCR", "roi": roi, "only_rec": True, "action": "DoNothing", "timeout": 1, "rate_limit": 0}}
        t = time.perf_counter()
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("Patrol_OCR_rec", node).wait().get()
        self.stats["ocr_n"] += 1; self.stats["ocr_t"] += time.perf_counter() - t
        reco = detail.nodes[0].recognition if detail and detail.nodes else None
        return "".join(r.text for r in reco.all_results).strip() if reco else ""

    def ocr_text(self, roi) -> str:
        return "".join(t for _, t in sorted(self.ocr_boxes(roi), key=lambda bt: bt[0][0])).strip()

    def device_labels(self) -> list[tuple[int, int, str]]:
        """俯瞰画面里所有设备标签的中心位置和文字（全屏 OCR 一次）。"""
        out = []
        for box, text in self.ocr_boxes(MAP_ROI):
            if LABEL_RE.match(text):
                out.append((box[0] + box[2] // 2, box[1] + box[3] // 2, text))
        return out

    def scene(self) -> str:
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("P0_DetectScene").wait().get()
        hit = [n.name for n in detail.nodes] if detail else []
        return next((n for n in hit if n.startswith("Scene_") and n != "Scene_Unknown"), "Scene_Unknown")

    def click(self, x: int, y: int):
        self.controller.post_click(int(x), int(y)).wait()

    def hold(self, vk: int, seconds: float):
        self.controller.post_key_down(vk).wait()
        time.sleep(seconds)
        self.controller.post_key_up(vk).wait()

    # ---- 详情面板 ----
    def read_panel(self, title: str) -> panel_recipe.Match | None:
        """已打开的设备面板：认配方、看原料格有没有堆配方外物品（存到 last_foreign），按需存一张面板图。"""
        img = self.controller.cached_image
        recipe = panel_recipe.identify_recipe(img, title)
        if recipe is None and title in panel_recipe.POOL_NAMES:
            recipe = self.read_pool_slots(title)  # 多配方池子：悬停右侧三个输出格读产物名和计数
        foreign = panel_recipe.foreign_inputs(img, recipe) if recipe else []
        self.last_foreign = "、".join(panel_recipe.label(f) if f != "?" else "（认不出的物品）" for f in foreign) or None
        self.last_warn = "息壤气供给不足" if panel_recipe.gauge_warning(img) else ""
        self.last_no_recipe = recipe is None and panel_recipe.no_recipe_set(img)
        if self.last_no_recipe:
            print("  （面板没设配方）")
        marks = panel_recipe.warning_marks(img)
        if recipe is None and any(panel_recipe.classify_mark(m) == "input" for m in marks):
            print("  （原料格有「!」但配方没认出，不判入口物品）")
        self.last_panel = ""
        if self.panel_dir:
            self.panel_dir.mkdir(parents=True, exist_ok=True)
            self.panel_count += 1
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])  # cv2.imwrite 不支持中文路径（Windows），会静默失败
            if ok:
                self.last_panel = f"{self.panel_count:03d}_{title}.jpg"
                (self.panel_dir / self.last_panel).write_bytes(buf.tobytes())
        return recipe

    def desc(self, recipe: panel_recipe.Match | None) -> str:
        """配方的可读描述；最近点开的面板没设配方时记成「（没设配方）」，和「没认出」区分开。"""
        if recipe is None and self.last_no_recipe:
            return panel_recipe.NO_RECIPE
        return panel_recipe.describe(recipe)

    def run_checks(self) -> list[dict]:
        """错题本定点核查（蓝图 playbook.checks）。每项 near=[x,y] 是本分区扫描坐标；走到能看到它的那一屏，
        取 100 px 内最近的「kind」标签：暗 → 点开读流速；gate → 在那根暗管周围找准入口面板读「当前速度」。要在俯瞰里调用。"""
        import blueprint
        checks = [c for c in blueprint.load(None).playbook.get("checks", []) if c.get("area") == self.area]
        if not checks:
            return []
        out = []
        self.ensure_overhead()
        self.go_corner()
        move_cursor(self.hwnd, 1270, 8)
        for c in checks:
            rec = {"id": c["id"], "note": c.get("note", ""), "expect_flow": c.get("expect_flow"), "expect_rate": c.get("expect_rate"), "fluid": c.get("fluid")}
            nx, ny = c["near"]
            # 让目标落在画面上半部：下方要留出找准入口的余地（底部建造栏遮住 y ≥ 545，2026-11-06 下面那根暗管的准入口就在栏后面）
            rows, cols = max(0, min(3, int(round((ny - 300) / 356)))), max(0, (nx - 400) // 356)
            self.trust_nominal = True
            try:
                self.go_corner()
                for d, n in (("down", rows), ("right", cols)):
                    for _ in range(n):
                        self.cached_labels = self.device_labels()
                        self.cached_feats = self.features(self.shot(), self.cached_labels)
                        self.pan(d, 0.6)
                        if self.last_pan_stuck:
                            break
            finally:
                self.trust_nominal = False
                self.cached_labels = None
                self.cached_feats = None
            sx, sy = nx - self.cam[0], ny - self.cam[1]
            labels = self.device_labels()
            kind = "暗" if c["kind"] in ("暗", "gate") else c["kind"]
            cands = sorted(((abs(x - sx) + abs(y - sy), x, y, t) for x, y, t in labels if t.startswith(kind)), key=lambda z: z[0])
            shot = self.shot()
            cv2.circle(shot, (int(sx), int(sy)), 12, (255, 0, 255), 2)
            for _, x, y, t in cands:
                cv2.circle(shot, (x, y), 10, (0, 255, 255), 2)
            if not cands or cands[0][0] > 100:
                rec.update(found=None, error=f"附近 100 px 没有「{kind}…」标签（最近 {cands[0][0]:.0f} px）" if cands else "画面里没有这种标签")
                print(f"  核查 {c['id']}：{rec['error']}")
                cv2.imencode(".png", shot)[1].tofile(str(DEBUG_DIR / f"check_{c['id']}.png"))
                out.append(rec)
                continue
            _, lx, ly, lt = cands[0]
            cv2.circle(shot, (lx, ly), 16, (0, 0, 255), 3)
            cv2.imencode(".png", shot)[1].tofile(str(DEBUG_DIR / f"check_{c['id']}.png"))  # 紫圈=登记位置，黄圈=候选，红圈=选中
            rec["world"] = [int(self.cam[0] + lx), int(self.cam[1] + ly)]
            if c["kind"] == "gate":
                gate = self.open_gate_near(lx, ly)
                rec.update(gate or {"found": None, "error": "暗管周围没点到准入口"})
                print(f"  核查 {c['id']}：{rec.get('found')} 当前速度 {rec.get('speed') or '?'}/分，限速 {rec.get('limit') or '?'}（期望 {c.get('expect_rate')}）@{rec['world']}")
            else:
                self.flow_samples = 10  # 约 7 秒窗口：错题本说流速「要出现 2」，取窗口内最大值
                try:
                    res = self.open_logistics(lx, ly)
                finally:
                    self.flow_samples = 1
                self.ensure_overhead()
                rec.update(found=res[0] if res else None, status=res[1] if res else None, flow=self.last_flow if res else "")
                print(f"  核查 {c['id']}：{rec['found']} {rec.get('status')} 流速 {rec.get('flow') or '?'}/秒（期望 {c.get('expect_flow')}）@{rec['world']}")
            out.append(rec)
        self.checks = out
        return out

    def open_gate_near(self, lx: int, ly: int) -> dict | None:
        """在一根暗管标签周围找准入口（物品/管道准入口）：逐点点击，开出准入口面板就读「当前速度」「限制速度」；
        点到重叠处弹「选择目标」菜单时挑准入口那一行。面板里的「替换物品」绝不能点。"""
        seen = []
        offsets = [(0, 45), (-45, 0), (45, 0), (0, -45), (-45, 45), (45, 45), (-45, -45), (45, -45),
                   (0, 80), (-80, 0), (80, 0), (0, -80), (-80, 45), (80, 45), (-30, 70), (30, 70), (0, 110), (-110, 0), (110, 0)]
        for dx, dy in offsets:
            x, y = lx + dx, ly + dy
            if any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in badges.UI_MASKS):
                continue
            self.ensure_overhead()
            self.click(x, y)
            time.sleep(1.0)
            if not self.panel_open():
                time.sleep(0.5)
                if not self.panel_open():
                    self.menu_possible = True
                    # 「选择目标」菜单里直接挑准入口
                    x0, y0 = max(0, x - 160), max(0, y - 70)
                    roi = [x0, y0, min(420, 1280 - x0), min(180, 720 - y0)]
                    hit = next(((b, t) for b, t in self.ocr_boxes(roi) if t.replace(" ", "") in GATE_NAMES), None)
                    if hit is None:
                        continue
                    b = hit[0]
                    self.click(b[0] + b[2] // 2, b[1] + b[3] // 2)
                    time.sleep(1.2)
                    if not self.panel_open():
                        continue
            title = self.panel_title()
            if title not in GATE_NAMES:
                seen.append(f"({dx},{dy})→{title or '?'}")
                self.close_panel()
                continue
            st = STATUS_RE.search(self.ocr_text(PANEL_STATUS_ROI))
            speed = re.search(r"(\d+(?:\.\d+)?)", self.ocr_text(GATE_SPEED_ROI).replace("单位/分", ""))
            limit = re.search(r"(\d+(?:\.\d+)?)", self.ocr_text(GATE_LIMIT_ROI).replace("单位/分", ""))
            img = self.shot()
            cv2.imencode(".png", img)[1].tofile(str(DEBUG_DIR / f"gate_{int(self.cam[0] + x)}_{int(self.cam[1] + y)}.png"))
            # 限速开关：两个选项里背景更暗的是选中的（选中=深色药丸、白字）
            dark = {k: float(img[py - 6 : py + 6, px - 14 : px + 14].mean()) for k, (px, py) in GATE_TOGGLE.items()}
            limit_on = dark["开"] < dark["关"]
            self.close_panel()
            return {"found": title, "status": st.group(1) if st else "?", "speed": speed.group(1) if speed else "",
                    "limit": limit.group(1) if limit else "", "limit_on": limit_on, "probe": f"({dx},{dy})"}
        self.ensure_overhead()
        print("  准入口探测：" + ("、".join(seen) if seen else "周围各点都没开出面板"))
        return None

    def read_power(self) -> str:
        """读顶部电量框，返回「用电/上限」或空串。"""
        m = re.search(r"(\d{3,5})\s*/\s*(\d{3,5})", self.ocr_text(POWER_ROI))
        if not m:
            return ""
        pct = re.search(r"(\d{1,3})\s*%", self.ocr_text(POWER_PCT_ROI))
        self.power = f"{m.group(1)}/{m.group(2)}" + (f" {pct.group(1)}%" if pct else "")
        print(f"== 电量 {self.power}")
        return self.power

    def read_pool_slots(self, title: str) -> panel_recipe.PoolMatch | None:
        """（扩容）反应池面板：鼠标悬停右侧「选择输出产物」三格，读提示里的物品名和格子下方的计数。读不到返回 None。"""
        slots = []
        for x, y in panel_recipe.POOL_OUTPUT_SLOTS:
            move_cursor(self.hwnd, x, y)
            time.sleep(0.7)
            dx, dy, w, h = panel_recipe.POOL_TIP_ROI
            texts = [t for _, t in self.ocr_boxes([max(0, x + dx), max(0, y + dy), w, h])]
            iid = next((i for i in (panel_recipe.item_by_name(t) for t in texts) if i), None)
            if iid is None:
                continue
            cx, cy, cw, ch = panel_recipe.POOL_COUNT_ROI
            m = re.search(r"\d+", self.ocr_text([x + cx, y + cy, cw, ch]))
            slots.append((iid, int(m.group()) if m else None))
        move_cursor(self.hwnd, 1270, 8)
        if not slots:
            print("  （池子输出格没读到物品名）")
            return None
        # 转盘六格：有计数的格子才有物品；右上角红斜杠 = 原料；图标只在这台池子相关配方的原料/产物里比
        img = self.shot()
        outs = [i for i, _ in slots]
        in_cands = panel_recipe.pool_input_candidates(outs)
        wheel = []
        for x, y in panel_recipe.pool_wheel_slots(title):  # 反应池五格 / 扩容反应池六格
            cx, cy, cw, ch = panel_recipe.POOL_COUNT_ROI
            m = re.search(r"\d+", self.ocr_text([x + cx, y + cy, cw, ch]))
            if not m:
                continue  # 空格
            is_out = panel_recipe.pool_arrow_is_output(img, x, y)  # 黄箭头 = 产物格，白箭头 = 原料格
            cands = outs + in_cands
            if is_out is True:
                cands = [i for i in outs] or cands
            elif is_out is False:
                cands = [i for i in in_cands if i not in outs] or cands
            item = panel_recipe.pool_wheel_item(img, x, y, cands)
            if item and "|" in item and is_out is not None:  # 息壤|重息壤：按箭头方向在产物/原料里挑
                pick = [i for i in item.split("|") if (i in outs) == is_out]
                if len(pick) == 1:
                    item = pick[0]
            is_input = (not is_out) if is_out is not None else (bool(item) and all(i in in_cands and i not in outs for i in item.split("|")))
            wheel.append((item, int(m.group()), is_input))
        match = panel_recipe.make_pool_match(title, slots, wheel)
        print("  " + panel_recipe.describe(match))
        return match

    def panel_title(self) -> str | None:
        """读详情面板标题；只认建筑表里的名字。没开面板返回 None，认不出返回空串。"""
        boxes = self.ocr_boxes(PANEL_TITLE_ROI)
        if not boxes:
            return None
        # 标题常和「耗电功率值: 20」认成一整串，所以找文字里包含的最长建筑名
        text = "".join(t for _, t in sorted(boxes, key=lambda bt: bt[0][0])).replace(" ", "")
        hits = [n for n in BUILDING_NAMES if n in text]
        return max(hits, key=len) if hits else ""

    _close_tpl = None

    def panel_open(self, image=None) -> bool:
        """有没有详情面板开着：看右上角固定位置的关闭按钮「X」在不在。"""
        if Patrol._close_tpl is None:
            Patrol._close_tpl = _imread(str(ROOT / "resource" / "badges" / "panel_close.png"))
        if image is None:
            image = self.shot()
        x, y = PANEL_CLOSE
        h, w = Patrol._close_tpl.shape[:2]
        region = image[y - h : y + h, x - w : x + w]
        score = cv2.matchTemplate(region, Patrol._close_tpl, cv2.TM_CCOEFF_NORMED).max()
        return float(score) > 0.8

    def core_panel_open(self) -> bool:
        """点到协议核心/次级核心会弹全屏面板（左上角「// 次级核心-武陵」，关闭钮在右上角）。
        里面有「跨地区传输」「设备制造」，绝不能点，只能关掉。"""
        return bool(re.search(r"核心", self.ocr_text(CORE_PANEL_TITLE_ROI)))

    def close_panel(self):
        """关掉面板；没关上就再点一次（面板弹出有动画，太早点会落空）。"""
        for _ in range(3):
            self.click(*PANEL_CLOSE)
            time.sleep(0.8)
            if not self.panel_open():
                return
        print("  警告：面板关不掉")

    def pick_overlap_menu(self, x: int, y: int) -> bool:
        """点到重叠的东西（设备压着管道/传送带）时，游戏在点击处弹「选择目标」小菜单，列出各个名字。
        优先选生产设备，其次选暗管口/准入口这类有意义的物流件；只有传送带/管道就不选。选中后面板会打开。"""
        x0, y0 = max(0, x - 160), max(0, y - 70)
        roi = [x0, y0, min(420, 1280 - x0), min(180, 720 - y0)]
        items = []
        for box, text in self.ocr_boxes(roi):
            name = text.replace(" ", "")
            if name in BUILDING_NAMES or name in LOGISTICS:
                items.append((name, box[0] + box[2] // 2, box[1] + box[3] // 2))
        if not items:
            return False
        machines = [i for i in items if i[0] not in LOGISTICS]
        useful = [i for i in items if i[0] in LOGISTICS and i[0] not in ("传送带", "管道", "物流桥", "管道桥")]
        pick = (machines or useful or [None])[0]
        if pick is None:
            return False
        self.click(pick[1], pick[2])
        time.sleep(1.2)
        return self.panel_open()

    BUILD_ROI = [20, 10, 260, 50]  # 左上角「//建造状态」（放置预览模式）

    def build_mode(self) -> bool:
        return "建造" in self.ocr_text(self.BUILD_ROI).replace(" ", "")

    def ensure_overhead(self):
        """点完设备后确认还在俯瞰：设备面板没关就关，核心面板开着就关，残留的「选择目标」小菜单也清掉。
        误点到底部建造栏会进入放置预览（左上角「建造状态」），按 Esc 取消——绝不能点「确认放置」。"""
        if self.build_mode():
            self.controller.post_key_down(0x1B).wait(); time.sleep(0.05); self.controller.post_key_up(0x1B).wait()
            time.sleep(1.2)
            print("  误入放置预览，已按 Esc 取消")
        if self.core_panel_open():  # 先于普通面板判断：核心全屏面板上 panel_open 的 X 模板也可能匹配，拿普通面板的 X 位置去点会点到面板里的东西
            self.click(*CORE_PANEL_CLOSE)
            time.sleep(1.2)
            print("  关掉了核心面板")
        if self.panel_open():
            self.close_panel()
        if self.menu_possible:  # 只有「点了但没开出面板」之后才可能留着「选择目标」小菜单；整图 OCR 一次 1 秒，别每次都做
            self.menu_possible = False
            self.clear_overlap_menu()

    def clear_overlap_menu(self):
        """「选择目标」小菜单会一直挂在画面上遮住标签（快速模式不点击，不会自然消失）。
        点它的第一项打开那个面板，再关掉面板，画面就干净了。"""
        for box, text in self.ocr_boxes(MAP_ROI):
            if "选择" in text and ("目标" in text or box[2] < 60):
                self.click(box[0] + box[2] // 2, box[1] + box[3] // 2 + 38)
                time.sleep(1.0)
                if self.panel_open():
                    self.close_panel()
                print("  清掉了残留的选择目标菜单")
                return

    def click_points(self, badge: badges.Badge, labels) -> list[tuple[int, int]]:
        """候选点击位置：先是标记上方最近的设备标签，再是固定偏移。"""
        near = [
            (abs(lx - badge.x) + abs(ly - (badge.y - 45)), lx, ly)
            for lx, ly, _ in labels
            if badge.y - 110 <= ly <= badge.y + 20 and abs(lx - badge.x) <= 70
        ]
        points = [(lx, ly) for _, lx, ly in sorted(near)[:2]]
        if badge.kind == "blocked":
            points.insert(0, (badge.x, badge.y))
        top = self.idle_on_top(badge, labels) if badge.kind == "idle" else None
        if top:  # 标记在大设备顶部：先点它正下方的标签（设备本体），别点上方那台
            points.insert(0, (top[0], top[1] + 8))
        below = self.idle_logistics_below(badge, labels) if badge.kind == "idle" else None
        if below:  # 标记是下方物流件的：先点它的标签
            points.insert(0, (below[0], below[1] + 4))
        points += [(badge.x + dx, badge.y + dy) for dx, dy in CLICK_OFFSETS[badge.kind]]
        # 标签没认出来时，再在标记周围转一圈（大设备的本体可能离标记较远）
        ring = [(-40, -40), (40, -40), (-40, 20), (40, 20), (0, -80)]
        if badge.kind == "idle":
            ring.insert(0, (0, 42))  # 物流件（暗管口等）的空闲 zz 画在它顶部、本体在正下方约 40 像素；标签没读出时先试这里
        points += [(badge.x + dx, badge.y + dy) for dx, dy in ring]
        # 落在界面（底部建造栏、右侧菜单等）上的点一律不点：建造栏点到分类会展开，点到设备会进入放置模式
        points = [(x, y) for x, y in points if not any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in badges.UI_MASKS)]
        # 次级核心（标签「次…」）130 像素内的点也不点：会弹全屏核心面板（2026-11-06 挨着核心的多口暗管出口，备选点转一圈连开三次核心面板）
        cores = [(lx, ly) for lx, ly, t in labels if t.startswith("次")]
        points = [(x, y) for x, y in points if not any(abs(x - cx) <= 130 and abs(y - cy) <= 130 for cx, cy in cores)]
        return list(dict.fromkeys(points))

    def badge_still_there(self, badge: badges.Badge) -> bool:
        """满负荷产线里标记会一闪而过。点之前再截一张，标记不在了就当闪烁跳过。"""
        for b in badges.find_badges(self.shot()):
            if b.kind == badge.kind and abs(b.x - badge.x) <= 10 and abs(b.y - badge.y) <= 10:
                return True
        return False

    def open_device(self, badge: badges.Badge, labels, check_badge: bool = True) -> tuple[str, str, panel_recipe.Match | None] | None:
        """点开标记所属的设备，返回（设备名, 状态, 配方）。点到物流件或认不出的面板就关掉换位置。
        check_badge=False：不管标记还在不在都点（间歇空闲的设备要认名字，标记闪没了也得点）。"""
        self.last_env = None  # 本次点开的面板是否标「无环境」
        self.last_foreign = None
        if check_badge and not self.badge_still_there(badge):
            return "（闪烁）", "生产中", None
        logistics_hit = None  # 点到的第一个物流件（暗管口、准入口等）：标记可能就是它的
        t_open = time.perf_counter()
        tries_after_logistics = 0
        for k, (x, y) in enumerate(self.click_points(badge, labels)[:7]):
            if logistics_hit is not None:
                tries_after_logistics += 1
                if tries_after_logistics > 2:
                    break  # 第一下就点到物流件（暗管口等），标记多半就是它的：再试两个点没有生产设备就收手，别把 7 个点试满
            if time.perf_counter() - t_open > 30:
                print(f"  （点开已用 {time.perf_counter() - t_open:.0f} 秒，第 {k + 1} 个点）")
            self.ensure_overhead()  # 上一次点开的面板还没关，先关掉再点
            self.click(x, y)
            time.sleep(1.0)
            if self.core_panel_open():  # 点到协议核心/次级核心：全屏面板，只能从右上角关，里面什么都不能点
                self.ensure_overhead()
                continue
            if not self.panel_open():
                time.sleep(0.5)  # 面板弹出有动画，再等一下
                if not self.panel_open():
                    self.menu_possible = True
                    if self.core_panel_open():
                        self.ensure_overhead()
                        continue
                    if not self.pick_overlap_menu(x, y):
                        continue  # 没打开面板（点到空地）
            title = self.panel_title()
            if not title:
                self.close_panel()
                continue
            if title in LOGISTICS:
                if logistics_hit is None and title not in ("传送带", "管道"):
                    m = STATUS_RE.search(self.ocr_text(PANEL_STATUS_ROI))
                    logistics_hit = (title, m.group(1) if m else "?", None)
                self.close_panel()
                continue
            m = STATUS_RE.search(self.ocr_text(PANEL_STATUS_ROI))
            if "无环境" in self.ocr_text(PANEL_ENV_ROI).replace(" ", ""):
                self.last_env = "无环境"
            recipe = self.read_panel(title)
            if self.region is None:
                r = re.search(r"([一-鿿]{2,6})仓库", self.ocr_text(PANEL_REGION_ROI))
                if r:
                    self.region = r.group(1)
                    print(f"  地区：{self.region}")
            self.close_panel()
            return title, (m.group(1) if m else "?"), recipe
        self.ensure_overhead()
        return logistics_hit  # 没找到生产设备时，标记多半属于点到的那个物流件（如空闲的暗管出口）

    def open_logistics(self, x: int, y: int) -> tuple[str, str] | None:
        """点一下物流件（暗管口等）的标签，读面板标题和状态后关掉。没开出面板或不是物流件返回 None。"""
        if any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in badges.UI_MASKS):
            return None
        self.ensure_overhead()
        self.click(x, y)
        time.sleep(1.0)
        if not self.panel_open():
            time.sleep(0.5)
            self.menu_possible = True
            if not self.panel_open() and not self.pick_overlap_menu(x, y):
                return None
        title = self.panel_title()
        if not title or title not in LOGISTICS:
            self.close_panel()
            return None
        m = STATUS_RE.search(self.ocr_text(PANEL_STATUS_ROI))
        self.last_flow = ""
        kind = next((k for k in PIPE_FLOW_ROI if k in title), None)
        if kind:
            text = self.ocr_text(PIPE_FLOW_ROI[kind])
            if "单位" in text or "流速" in text:  # 先确认这块真是流速区，再单独读大数字
                vals = []
                for _ in range(self.flow_samples):  # 流速会跳（错题本 #7.1「要出现 2」），连读几次取最大
                    digit = self.ocr_rec(PIPE_FLOW_DIGIT[kind]).replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1")
                    fm = re.search(r"(\d+(?:\.\d+)?)", digit)
                    if fm:
                        vals.append(float(fm.group(1)))
                    if self.flow_samples > 1:
                        time.sleep(0.6)
                if vals:
                    v = max(vals)
                    self.last_flow = str(int(v)) if v.is_integer() else str(v)
        self.close_panel()
        return title, (m.group(1) if m else "?")

    # ---- 一屏（快速模式：不点开面板）----
    STABLE_SHOTS, STABLE_GAP, STABLE_MIN = 5, 1.0, 4  # 每屏连拍 5 张、间隔 1 秒，至少 4 张都在才算稳定

    def stable_badges(self, shots: int | None = None, gap: float | None = None) -> tuple[list[tuple[badges.Badge, int]], np.ndarray]:
        """连拍几张，只保留出现次数够多的标记（满负荷产线里阻塞/空闲标记每个周期闪两三秒）。
        返回 [(标记, 出现次数)]，以及最后一张图。4 秒窗口里闪两三秒的标记出现次数 2–3，达不到 4/5 就被滤掉；
        更长的（种植机一个周期阻塞 5 秒以上）靠第二遍扫描对照（passes）和 combine 的「产物在正常流动」规则。"""
        shots = shots or self.STABLE_SHOTS
        gap = self.STABLE_GAP if gap is None else gap
        images = []
        for i in range(shots):
            images.append(self.shot())
            if i < shots - 1:
                time.sleep(gap)
        sets = [badges.find_badges(im) for im in images]
        seen: list[tuple[badges.Badge, int]] = []
        for k, s0 in enumerate(sets):
            for b in s0:
                if any(o.kind == b.kind and abs(o.x - b.x) <= 8 and abs(o.y - b.y) <= 8 for o, _ in seen):
                    continue
                n = sum(any(o.kind == b.kind and abs(o.x - b.x) <= 8 and abs(o.y - b.y) <= 8 for o in s1) for s1 in sets)
                seen.append((b, n))
        stable = [(b, n) for b, n in seen if n >= min(self.STABLE_MIN, shots)]
        return stable, images[-1]


    def guess_name(self, badge: badges.Badge, labels) -> str:
        """用标记上方最近的设备标签（通常只露出首字，如「固…」）猜设备名。猜不出返回空串。"""
        # 空闲标记画在产物图标左下、设备标签在其上方约 45 像素；阻塞标记画在设备顶部、标签在其下方约 40 像素；
        # 关闭的灰圈叉位置不固定：精炼炉的标签在它右侧同高（+32,-4），种植机的在右下（+17,+40），窗口放宽
        if badge.kind == "idle" and (below := self.idle_logistics_below(badge, labels)):
            # 物流件（暗管口、取货口…）的空闲标记画在它顶部、标签在下方约 40 像素；上方的生产设备标签不是它的主人
            head = re.sub(r"[.。·…]+$", "", below[2])
            cands = [n for n in GUESSABLE if n.startswith(head)]
            return cands[0] if len(cands) == 1 else "|".join(cands)
        if badge.kind == "idle" and self.idle_on_top(badge, labels):
            # 大设备（封装机、反应池…）的空闲标记画在设备顶部，标签在它下方约 38 像素，和阻塞标记一样的布局
            anchor, lo, hi, dxmax = badge.y + 38, badge.y + 20, badge.y + 60, 45
        elif badge.kind == "idle":
            anchor, lo, hi, dxmax = badge.y - 45, badge.y - 110, badge.y + 20, 85
        elif badge.kind == "closed":
            anchor, lo, hi, dxmax = badge.y + 18, badge.y - 30, badge.y + 65, 100
        else:
            anchor, lo, hi, dxmax = badge.y + 40, badge.y - 30, badge.y + 85, 85
        near = [
            (abs(lx - badge.x) + abs(ly - anchor), text)
            for lx, ly, text in labels
            if lo <= ly <= hi and abs(lx - badge.x) <= dxmax
        ]
        if not near:
            return ""
        if badge.kind == "closed":
            # 关闭的是生产设备时，旁边常挨着一排「仓…」取货口标签；有生产设备的标签就优先用它
            def is_machine(text):
                h = re.sub(r"[.。·…]+$", "", text)
                return any(n.startswith(h) and n not in LOGISTICS for n in GUESSABLE)
            machine_near = [t for t in near if is_machine(t[1])]
            if machine_near:
                near = machine_near
        head = re.sub(r"[.。·…]+$", "", min(near)[1])
        cands = [n for n in GUESSABLE if n.startswith(head)]
        if not cands:
            return ""
        return cands[0] if len(cands) == 1 else "|".join(cands)

    @staticmethod
    def idle_on_top(badge: badges.Badge, labels) -> tuple[int, int, str] | None:
        """空闲标记正下方 20–60 像素有生产设备的标签 → 标记画在大设备顶部（封装机做分离芯的那种），
        产物图标在标记下方约 78 像素而不是右上。返回那条标签。"""
        for lx, ly, text in labels:
            if 20 <= ly - badge.y <= 60 and abs(lx - badge.x) <= 45:
                head = re.sub(r"[.。·…]+$", "", text)
                if head and any(n.startswith(head) and n not in LOGISTICS for n in GUESSABLE):
                    return (lx, ly, text)
        return None

    @staticmethod
    def idle_logistics_below(badge: badges.Badge, labels) -> tuple[int, int, str] | None:
        """空闲标记正下方 20–60 像素是物流件标签（暗管口、取货口…）、且正下方没有生产设备标签 → 标记是这个物流件的。
        2026-11-05 应龙关：两个空闲的暗管入口，zz 画在它们标签上方 38/43 像素；上方 45–80 像素恰好是生产中的种植机/精炼炉标签，
        按「标签在上方」的小设备布局会把 zz 算到种植机头上（报成「种植机 荞花 空闲」、点开又显示生产中）。
        物流件的空闲 zz 和大设备一样画在顶部、标签在下方。"""
        best = None
        for lx, ly, text in labels:
            if 20 <= ly - badge.y <= 60 and abs(lx - badge.x) <= 45:
                head = re.sub(r"[.。·…]+$", "", text)
                if not head:
                    continue
                cands = [n for n in GUESSABLE if n.startswith(head)]
                if not cands:
                    continue
                if any(n not in LOGISTICS for n in cands):
                    return None  # 正下方有生产设备标签：是大设备顶部布局，归 idle_on_top 管
                d = abs(lx - badge.x) + abs(ly - (badge.y + 40))
                if best is None or d < best[0]:
                    best = (d, (lx, ly, text))
        return best[1] if best else None

    @staticmethod
    def idle_above(badge: badges.Badge, labels) -> tuple[int, int, str] | None:
        """空闲标记上方（小设备布局：标签在标记上方约 45 像素）有生产设备标签时返回那条标签。"""
        best = None
        for lx, ly, text in labels:
            if badge.y - 110 <= ly <= badge.y + 20 and abs(lx - badge.x) <= 85:
                head = re.sub(r"[.。·…]+$", "", text)
                if head and any(n.startswith(head) and n not in LOGISTICS for n in GUESSABLE):
                    d = abs(lx - badge.x) + abs(ly - (badge.y - 45))
                    if best is None or d < best[0]:
                        best = (d, (lx, ly, text))
        return best[1] if best else None

    def resolve_idle(self, points: list[tuple[int, int]]) -> tuple[str, str, panel_recipe.Match | None, str | None] | None:
        """空闲标记上下各有一台设备、分不清是谁的：逐个点开，面板状态是「空闲中」的那台就是标记的主人。
        返回（设备名, 状态, 配方, 环境提示）；都不空闲返回 None。一个基地里这种情况只有一两处，多花几秒换准确。"""
        for x, y in points:
            if any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in badges.UI_MASKS):
                continue
            self.ensure_overhead()
            self.click(x, y)
            time.sleep(1.2)
            if not self.panel_open():
                time.sleep(0.5)
                self.menu_possible = True
                if not self.panel_open() and not self.pick_overlap_menu(x, y):
                    continue
            title = self.panel_title()
            if not title or title in LOGISTICS:
                self.close_panel()
                continue
            m = STATUS_RE.search(self.ocr_text(PANEL_STATUS_ROI))
            status = m.group(1) if m else "?"
            env = "无环境" if "无环境" in self.ocr_text(PANEL_ENV_ROI).replace(" ", "") else None
            self.last_foreign = None
            recipe = self.read_panel(title)
            self.close_panel()
            if status == "空闲中":
                return title, status, recipe, env
        self.ensure_overhead()
        return None

    def guess_output(self, image: np.ndarray, badge: badges.Badge, machines: list[str], on_top: bool = False) -> tuple[str | None, str | None]:
        """从标记旁画着的产物图标认产物（不点开面板）。返回（产物 id, 配方 id），认不出返回 (None, None)。

        空闲标记在产物图标的左下（图标中心在标记右上约 14 像素）；阻塞标记在设备顶部，产物图标在其下方约 78 像素。
        灌装瓶/耐压罐底图一样，认出底图后再用液滴识别分变体。息壤/重息壤这类图标相近的会同时列出。
        """
        dx, dy = (14, -14) if badge.kind == "idle" and not on_top else (0, 78)
        cx, cy = badge.x + dx, badge.y + dy
        r = PRODUCT_ICON_R
        if not (r <= cx < image.shape[1] - r and r <= cy < image.shape[0] - r):
            return None, None
        tile = image[cy - r : cy + r, cx - r : cx + r]
        recipes = [rc for rc in panel_recipe.RECIPES if rc["machine_name"] in machines]
        outputs = sorted({o for rc in recipes for o in rc["outputs"]})
        if self.active_items:  # 只在简报里有产出/消耗记录的物品里挑，排除这个基地根本不做的（赫铜瓶、紫晶瓶…）
            outputs = [o for o in outputs if o in self.active_items] or outputs
        if not outputs:
            return None, None
        # 灌装瓶/耐压罐在俯瞰里是斜画在深色圆里的小图，模板法分不开（赤铜瓶 45 vs 蓝铁瓶 48）；
        # 改用颜色：瓶身色相分 赤铜瓶（红）/ 蓝铁瓶（蓝）/ 耐压罐（灰），再用液滴识别分灌装物
        containers = {}
        for o in outputs:
            if panel_recipe.is_filled_container(o):
                base = next((b for p, b in panel_recipe.BASE_CONTAINER.items() if o.startswith(p)), None)
                if base:
                    containers.setdefault(base, []).append(o)
        picked_container = None
        if containers:
            kind = container_by_color(tile)
            want = {"copper": "item_copper_bottle", "iron": "item_iron_bottle", "jar": "item_copper_jar"}.get(kind)
            if want in containers:
                picked_container = want
                liquid = want != "item_copper_jar"
                cands = {p.stem for p in fluid_icon.ICON_DIR.glob("item_liquid_*.png" if liquid else "item_gas_*.png")}
                fluid, _ = fluid_icon.detect(tile, cands, heights=range(8, 18, 2), max_score=600)
                items = containers[want]
                if fluid:
                    suffix = ("_" + fluid.removeprefix("item_liquid_").replace("plant_", "")) if liquid else ("_gas_" + fluid.removeprefix("item_gas_"))
                    hit = [i for i in items if i.endswith(suffix)]
                    if hit:
                        rid = next((rc["id"] for rc in recipes if hit[0] in rc["outputs"]), None)
                        return hit[0], rid
                # 液滴没认出：容器定了，灌装物列全部候选让合并逻辑逐个判断
                output = "|".join(items)
                rid = next((rc["id"] for rc in recipes if items[0] in rc["outputs"]), None)
                return output, rid
            # 颜色看着不像任何瓶子：把瓶子全部排除，只在非容器产物里比
            outputs = [o for o in outputs if not panel_recipe.is_filled_container(o)]
            if not outputs:
                return None, None
        scored = []
        for o in outputs:
            d = panel_recipe._icon_diff_free(tile, panel_recipe.icon_ids(o)[0], sizes=(22, 26, 30, 34))
            scored.append((d, o))
        scored.sort(key=lambda t: t[0])
        best_d, output = scored[0]
        if best_d > PRODUCT_MAX_DIFF:
            return None, None
        # 第二名差距很小（息壤 vs 重息壤）就两个都报，让合并逻辑按两种都查
        if len(scored) > 1 and scored[1][0] <= best_d * 1.12:
            output = output + "|" + scored[1][1]
        rid = next((rc["id"] for rc in recipes if scored[0][1] in rc["outputs"]), None)
        return output, rid

    def scan_view_fast(self) -> list[Device]:
        """快速模式：连拍滤闪烁 + 标签猜设备名 + 产物图标认产物，不点任何东西。一屏约 2 秒。"""
        t_screen = time.perf_counter()
        st0 = dict(self.stats)
        stable_pairs, image = self.stable_badges()
        counts = {id(b): n for b, n in stable_pairs}
        stable = [b for b, _ in stable_pairs]
        if self.shot_dir:  # 每屏存一张，离线分析和回归测试用
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            # cv2.imwrite 遇到中文路径静默失败（2026-11-06 目录名带「景玉谷」时整目录为空、定位图出不来），用 imencode + tofile
            cv2.imencode(".png", image)[1].tofile(str(self.shot_dir / f"{int(self.cam[0])}_{int(self.cam[1])}.png"))
        labels = self.cached_labels if self.cached_labels is not None else self.device_labels()
        if self.trust_nominal and self.cached_feats is None:
            self.cached_feats = self.features(image, labels)
        self.cached_labels = None
        self.last_screen_has_base = bool(stable) or bool(labels)  # 给 sweep 判断是否扫到了基地边缘
        found = []
        cores = [(lx, ly) for lx, ly, t in labels if t.startswith("次")]  # 次级核心：大白色花瓣结构，纹理会被当成空闲 zz（分数 39–45）
        for b in stable:
            if b.kind == "idle" and any(abs(b.x - cx) <= 130 and abs(b.y - cy) <= 130 for cx, cy in cores):
                continue
            wx, wy = int(self.cam[0] + b.x), int(self.cam[1] + b.y)
            if any(abs(d.world_x - wx) < 25 and abs(d.world_y - wy) < 25 for d in self.devices):
                continue
            status = badges.SPECS[b.kind]["label"]
            prev = self.known_at(status, wx, wy)
            if prev is not None:
                # 第一遍已经点开过（标签没读出来 / 上下两台分不清的那几台）：同位置同标记，身份沿用第一遍，不再点。
                # 2026-11-11 群友：「详细巡检会频繁查看堵塞或未工作的设备，即使已经看过很多遍」；11-10 景玉谷 12 次点开里 2 次是这种重复
                dev = replace(prev, world_x=wx, world_y=wy, passes=1, seen=f"{counts.get(id(b), 0)}/{self.STABLE_SHOTS}")
                self.devices.append(dev)
                found.append(dev)
                print(f"  {dev.badge:<4} {dev.name:<8} {dev.recipe}  @({wx}, {wy})（第一遍点开过，这遍只看标记还在不在）")
                continue
            name = self.guess_name(b, labels) or "（无标签）"
            machines = [m for m in name.split("|") if m not in LOGISTICS and m != "（无标签）"]
            # 关闭的设备产物图标位置不固定，不猜产物；关闭本身就是要报的结论
            top = self.idle_on_top(b, labels) if b.kind == "idle" else None
            above = self.idle_above(b, labels) if b.kind == "idle" else None
            if top and above:
                # 标记上方是小设备、下方是大设备（封装机/反应池…），标签都够近，分不清是谁空闲：点开看面板
                res = self.resolve_idle([(top[0], top[1] + 8), (above[0], above[1])])
                if res:
                    title, st, recipe, env = res
                    dev = Device(
                        title, st, status, wx, wy, recipe=self.desc(recipe),
                        output=recipe.output if recipe else None, recipe_id=recipe.recipe["id"] if recipe else None, env=env,
                        seen=f"{counts.get(id(b), 0)}/{self.STABLE_SHOTS}", foreign=self.last_foreign, panel=self.last_panel, warn=self.last_warn,
                    )
                    self.devices.append(dev)
                    found.append(dev)
                    print(f"  {dev.badge:<4} {dev.name:<8} {dev.status:<4} {dev.recipe}{f'（入口堆着 {dev.foreign}）' if dev.foreign else ''}  @({wx}, {wy})（上下两台分不清，点开确认）")
                    continue
            if name == "（无标签）" and b.kind != "closed":
                # 标签没读出来的稳定标记：点开看（一个基地十来台），不然只能并成一条「无标签待确认」。
                # 不检查标记还在不在：要认的是身份，闪烁的暗管口（2026-11-06 (485,1172)）也得点开
                res = self.open_device(b, labels, check_badge=False)
                if res and res[0] not in ("（闪烁）", "（没点开）"):
                    title, st, recipe = res
                    dev = Device(
                        title, st, status, wx, wy, recipe=self.desc(recipe),
                        output=recipe.output if recipe else None, recipe_id=recipe.recipe["id"] if recipe else None,
                        env=self.last_env, seen=f"{counts.get(id(b), 0)}/{self.STABLE_SHOTS}", foreign=self.last_foreign, panel=self.last_panel, warn=self.last_warn,
                    )
                    self.devices.append(dev)
                    found.append(dev)
                    print(f"  {dev.badge:<4} {dev.name:<8} {dev.status:<4} {dev.recipe}  @({wx}, {wy})（无标签，点开确认）")
                    self.ensure_overhead()
                    continue
                self.ensure_overhead()
            if self.open_all and b.kind == "idle" and not machines and "|" in name and (below := self.idle_logistics_below(b, labels)):
                # 暗管入口|暗管出口 这种分不清的物流件：第二遍点一下它的标签，读准确名字和状态（入口空闲和出口空闲意义不同）
                res = self.open_logistics(below[0], below[1] + 4)
                self.ensure_overhead()
                if res:
                    dev = Device(res[0], res[1], status, wx, wy, seen=f"{counts.get(id(b), 0)}/{self.STABLE_SHOTS}", flow=self.last_flow)
                    self.devices.append(dev)
                    found.append(dev)
                    print(f"  {dev.badge:<4} {dev.name:<8} {dev.status:<4}{f'  流速 {dev.flow}/秒' if dev.flow else ''}  @({wx}, {wy})（物流件，点开确认）")
                    continue
            if self.open_all and b.kind != "closed" and machines and (self.only_names is None or any(m in self.only_names for m in machines)):
                # 全开：稳定的生产设备标记当场点开（定向巡检时只点 only_names 里的类型）（2026-11-05：第三遍按坐标回头点，里程计差几十像素就点到密集排布的邻居，
                # 右下角那台砂叶故障的固气转化机被当成了它上面那台）。点开失败再按标签猜。
                res = self.open_device(b, labels, check_badge=False)
                self.ensure_overhead()
                if res and res[0] not in ("（闪烁）", "（没点开）"):
                    title, st, recipe = res
                    dev = Device(
                        title, st, status, wx, wy, recipe=self.desc(recipe),
                        output=recipe.output if recipe else None, recipe_id=recipe.recipe["id"] if recipe else None,
                        env=self.last_env, seen=f"{counts.get(id(b), 0)}/{self.STABLE_SHOTS}", foreign=self.last_foreign, panel=self.last_panel, warn=self.last_warn,
                    )
                    self.devices.append(dev)
                    found.append(dev)
                    print(f"  {dev.badge:<4} {dev.name:<8} {dev.status:<4} {dev.recipe}{'（无环境）' if dev.env else ''}{f'（入口堆着 {dev.foreign}）' if dev.foreign else ''}  @({wx}, {wy})（点开确认）")
                    continue
            on_top = top is not None
            output, rid = self.guess_output(image, b, machines, on_top) if machines and b.kind != "closed" else (None, None)
            desc = "产物：" + "/".join(panel_recipe.label(o) for o in output.split("|")) if output else ""
            dev = Device(name, status, status, wx, wy, recipe=desc, output=output, recipe_id=rid, guessed=True,
                         seen=f"{counts.get(id(b), 0)}/{self.STABLE_SHOTS}")
            self.devices.append(dev)
            found.append(dev)
            print(f"  {dev.badge:<4} {dev.name:<8} {dev.recipe}  @({wx}, {wy})")
        print(f"  （本屏 {time.perf_counter() - t_screen:.0f} 秒：截图 {self.stats['shot_n'] - st0['shot_n']} 次 {self.stats['shot_t'] - st0['shot_t']:.0f} 秒，"
              f"OCR {self.stats['ocr_n'] - st0['ocr_n']} 次 {self.stats['ocr_t'] - st0['ocr_t']:.0f} 秒）")
        shot_name = f"{int(self.cam[0])}_{int(self.cam[1])}.png"
        for d in found:
            d.shot, d.sx, d.sy = shot_name, int(d.world_x - self.cam[0]), int(d.world_y - self.cam[1])
        return found

    KNOWN_TOL = 30  # 第一遍和第二遍同一台设备的坐标差（两遍对照也用 30；实测相邻屏同一标记只差 ±2 像素）

    def known_at(self, badge: str, wx: int, wy: int) -> "Device | None":
        """第一遍点开过、同一种标记、位置对得上的设备。"""
        return next((d for d in self.known if d.badge == badge and abs(d.world_x - wx) <= self.KNOWN_TOL
                     and abs(d.world_y - wy) <= self.KNOWN_TOL), None)

    # ---- 一屏（深入模式：逐个点开面板）----
    def scan_view(self) -> list[Device]:
        image = self.shot()
        labels = self.device_labels()
        found = []
        for b in badges.find_badges(image):
            wx, wy = int(self.cam[0] + b.x), int(self.cam[1] + b.y)
            if any(abs(d.world_x - wx) < 25 and abs(d.world_y - wy) < 25 for d in self.devices):
                continue  # 之前的画面里已经看过
            res = self.open_device(b, labels)
            name, status, recipe = res if res else ("（没点开）", "", None)
            dev = Device(
                name,
                status,
                badges.SPECS[b.kind]["label"],
                wx,
                wy,
                recipe=self.desc(recipe),
                output=recipe.output if recipe else None,
                recipe_id=recipe.recipe["id"] if recipe else None,
                env=self.last_env,
                foreign=self.last_foreign, panel=self.last_panel, warn=self.last_warn,
            )
            self.devices.append(dev)
            found.append(dev)
            print(f"  {dev.badge:<4} {dev.name:<6} {dev.status or '?':<4} {dev.recipe}{'（无环境）' if dev.env else ''}{f'（入口堆着 {dev.foreign}）' if dev.foreign else ''}  @({wx}, {wy})")
        return found

    # ---- 镜头平移 ----
    @staticmethod
    def _gray(image):
        g = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
        for x, y, w, h in badges.UI_MASKS:
            g[y : y + h, x : x + w] = 0
        return g

    PAN_SPEED = 593.0  # 像素/秒（720p、最远缩放，按住 0.6 秒约 356 像素，2026-11-04 多次扫描标定）
    MINIMAP_MOVED = 5.0  # 小地图平均像素差超过这个值 = 镜头真的动了（边界 2–3，0.6 秒移动 20–27，0.3 秒约 10）

    @staticmethod
    def _gray_map(image: np.ndarray) -> np.ndarray:
        """只取地图中央（MAP_ROI，不含四周界面）的灰度图，给相位相关用。"""
        x, y, w, h = MAP_ROI
        return cv2.cvtColor(image[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY).astype(np.float32)

    @staticmethod
    def _constellation_shift(before, after, axis: int, nominal: float, tol: int = 14):
        """两屏之间「同名标签 + 同类标记」整体平移量：对每个候选位移数有多少个特征能对上（标记算 2 分），取支持最多的。
        候选包括 0（镜头顶在地图边界上没动，画面一样）。重复的标签（一排「仓…」）两两配对会出一堆错配，
        但只有真实位移能让所有特征同时对上，所以按支持数选而不是按配对中位数。
        返回（位移, 支持数, 第二名支持数）；没有特征时返回 (None, 0, 0)。"""
        if not before or not after:
            return None, 0, 0
        pairs = [
            (ax - bx, ay - by)[axis]
            for ax, ay, at in before
            for bx, by, bt in after
            if at == bt and abs((ay, ax)[axis] - (by, bx)[axis]) <= tol
        ]
        cands = sorted({round(d) for d in pairs if abs(d) <= 20 or nominal * 0.3 <= abs(d) <= nominal * 1.4})
        if not cands:
            return None, 0, 0
        scored = []
        for sft in cands:
            sup = 0
            for ax, ay, at in before:
                tx, ty = (ax - sft, ay) if axis == 0 else (ax, ay - sft)
                if any(bt == at and abs(tx - bx) <= tol and abs(ty - by) <= tol for bx, by, bt in after):
                    sup += 2 if at.startswith("#") else 1
            scored.append((sup, sft))
        scored.sort(reverse=True)
        best_sup, best = scored[0]
        second = max((sup for sup, sft in scored if abs(sft - best) > 20), default=0)
        return float(best), best_sup, second

    def features(self, image: np.ndarray, labels=None):
        """里程计用的特征：设备标签 (x, y, 文字) + 状态标记 (x, y, "#kind")。"""
        labels = self.device_labels() if labels is None else labels
        return list(labels) + [(b.x, b.y, "#" + b.kind) for b in badges.find_badges(image)]

    @staticmethod
    def _orb_shift(before: np.ndarray, after: np.ndarray, axis: int) -> tuple[float | None, int, int]:
        """两张截图地图区域的 ORB 特征匹配，返回（沿 axis 的位移众数, 支持数, 匹配总数）。
        2026-11-06 用 YL11 的 20 张截图离线核对：真走 356 像素时支持 478–721/1000，顶在右边界只挪 25–31 像素时支持 1800+/1900；
        标签星座法在后一种情况只有 2 个支持、相位相关也不认，结果按名义 356 记，整列设备被记成多 330 像素的「新设备」
        （应龙关只有一台扩容反应池，报告里成了 (980,690) 和 (1310,690) 两台）。"""
        orb = cv2.ORB_create(3000)
        ga = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)[80:545, 340:1060]
        gb = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)[80:545, 340:1060]
        ka, da = orb.detectAndCompute(ga, None)
        kb, db = orb.detectAndCompute(gb, None)
        if da is None or db is None or len(ka) < 20 or len(kb) < 20:
            return None, 0, 0
        matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
        if len(matches) < 20:
            return None, 0, len(matches)
        d = np.array([[ka[m.queryIdx].pt[0] - kb[m.trainIdx].pt[0], ka[m.queryIdx].pt[1] - kb[m.trainIdx].pt[1]] for m in matches])
        main, other = d[:, axis], d[:, 1 - axis]
        hist, edges = np.histogram(main, bins=np.arange(-600, 601, 8))
        k = int(hist.argmax())
        sel = (main >= edges[k] - 8) & (main < edges[k + 1] + 8) & (np.abs(other) < 12)
        if sel.sum() == 0:
            return None, 0, len(matches)
        return float(np.median(main[sel])), int(sel.sum()), len(matches)

    @staticmethod
    def _minimap(image: np.ndarray) -> np.ndarray:
        """左上角小地图的圆形区域（720p）。"""
        return cv2.cvtColor(image[55:165, 50:160], cv2.COLOR_BGR2GRAY).astype(np.float32)

    def pan(self, direction: str, seconds: float) -> np.ndarray:
        """平移镜头，返回位移（720p 像素）。

        位移优先用相位相关估计。相关测不到时（没纹理的草地、或真到了地图边界），
        用状态标记的位置判断：标记位置完全没变才算边界，否则按名义速度算。
        像素差的均值/中位数都不可靠：动画让均值达 10–25，草地上中位数又是 0。
        """
        shot_before = self.shot()
        before = self._gray(shot_before)
        labels_before = self.cached_labels if self.trust_nominal else None
        self.hold(PAN_KEYS[direction], seconds)
        time.sleep(0.6)
        shot_after = self.shot()
        after = self._gray(shot_after)
        (dx, dy), _ = cv2.phaseCorrelate(before, after)
        move = np.array([-dx, -dy])  # 画面内容向左移 dx<0 表示镜头向右走了 -dx
        # 平移只沿一个轴：相关给出明显的斜向位移（如 (-107, 72)）是草地上的乱值，不能信
        axis = 0 if direction in ("left", "right") else 1
        if abs(move[1 - axis]) > 40:
            move = np.array([0.0, 0.0])
        if self.trust_nominal:
            # 固定网格扫描的里程计：① 两屏之间标签+标记的整体平移（含「没动」= 顶在地图边界）
            # ② 去掉界面后的中央区域相位相关 ③ 名义速度。
            # 地图四周都是硬边界：顶住了画面就不变，这时必须记 0（2026-11-04 曾按名义 356 记，整行重复）。
            # 镜头速度也不是常数（协议核心那一列 0.6 秒只走 264 像素），所以能测就用测的。
            nominal = self.PAN_SPEED * seconds
            sign = 1 if direction in ("right", "down") else -1
            feats_before = self.cached_feats if self.cached_feats is not None else self.features(shot_before)
            labels_after = self.device_labels()
            feats_after = self.features(shot_after, labels_after)
            self.cached_labels = labels_after  # 下一屏扫描直接用，不用再 OCR
            self.cached_feats = feats_after
            self.last_pan_stuck = False
            # ① ORB 整图匹配（最稳）：支持 ≥ 60 就信；|位移| ≤ 名义 30% 视为顶住边界（记实际小位移，本行到头）
            o_est, o_sup, o_tot = self._orb_shift(shot_before, shot_after, axis)
            if o_est is not None and o_sup >= 60 and o_sup >= o_tot * 0.25:
                if abs(o_est) <= nominal * 0.3:
                    self.last_pan_stuck = True
                    print(f"  （镜头顶在边界上：ORB 实测只挪了 {o_est:.0f} 像素，支持 {o_sup}/{o_tot}）")
                    move = np.array([0.0, 0.0]); move[axis] = o_est
                    self.cam += move
                    return move
                if nominal * 0.3 < sign * o_est <= nominal * 1.4:
                    move = np.array([0.0, 0.0]); move[axis] = o_est
                    self.cam += move
                    return move
            est, sup, second = self._constellation_shift(feats_before, feats_after, axis, nominal)
            if est is not None and sup >= 6 and sup >= second * 2:
                if abs(est) <= 20:
                    self.last_pan_stuck = True
                    print(f"  （镜头顶在边界上没动：{sup} 个特征原地不动）")
                    return np.array([0.0, 0.0])
                if not (nominal * 0.3 <= sign * est <= nominal * 1.4):
                    est = None
            else:
                est = None
            if est is None:
                (cdx, cdy), resp = cv2.phaseCorrelate(self._gray_map(shot_before), self._gray_map(shot_after))
                c = (-cdx, -cdy)[axis]
                if abs((-cdx, -cdy)[1 - axis]) <= 40 and nominal * 0.5 <= sign * c <= nominal * 1.2:
                    est = c
            if est is None:
                est = sign * nominal
                print(f"  （特征支持 {sup}/{second}、相关都没测出位移，按名义 {est:.0f} 算）")
            move = np.array([0.0, 0.0]); move[axis] = est
            self.cam += move
            return move
        if False:
            # 固定网格扫描：镜头在基地范围内不会撞边界，位移按名义速度算，相关值只做核对
            # （相关在草地上会给 0 或乱值，一次错就把后面整行的坐标带偏——2026-11-04 B2/B4 两次扫描都是这样）
            # 镜头速度不是常数：经过某些地块（如协议核心上方那一列）同样按 0.6 秒只走 264 而不是 356 像素，
            # 各次、各方向都一样，所以相关值只要方向对、量级合理（名义的 0.5–1.2 倍）就信它；
            # 只有相关失败（0 或乱值）才按名义速度补
            nominal = self.PAN_SPEED * seconds
            sign = 1 if direction in ("right", "down") else -1
            measured = move[axis]
            if not (nominal * 0.5 <= sign * measured <= nominal * 1.2):
                if abs(measured) > 20:
                    print(f"  （相关测得 {measured:.0f}，和名义 {sign * nominal:.0f} 对不上，按名义算）")
                move = np.array([0.0, 0.0]); move[axis] = sign * nominal
            self.cam += move
            return move
        if np.abs(move).max() < 20:
            # 相关没测到位移：要么真到边界，要么在没纹理的草地上。看左上角小地图：
            # 镜头真的动了小地图会跟着变（平均像素差 20–27），顶在边界上它不变（2–3）。2026-11-04 实测
            mini_diff = float(np.abs(self._minimap(shot_after) - self._minimap(shot_before)).mean())
            if mini_diff < self.MINIMAP_MOVED:
                move = np.array([0.0, 0.0])
            else:
                nominal = self.PAN_SPEED * seconds
                move = {
                    "right": np.array([nominal, 0.0]), "left": np.array([-nominal, 0.0]),
                    "down": np.array([0.0, nominal]), "up": np.array([0.0, -nominal]),
                }[direction]
                print(f"  （相关没测到位移，小地图变了 {mini_diff:.1f}，按名义速度算：{direction}）")
        self.cam += move
        return move

    def zoom_out(self, ticks: int = 30):
        """把镜头缩到最远。标记模板、标签距离、点击偏移都是按最远缩放校准的，不同基地进俯瞰时缩放不一样。"""
        for _ in range(ticks):
            wheel(self.hwnd, 640, 360, -120)
            time.sleep(0.05)
        time.sleep(1.0)

    CORNER_HOLD = (4.0, 4.0)  # 向上、向左各按住多少秒：俯瞰镜头在地图左上角是硬边界，按够长就顶死在角上

    def recenter(self) -> bool:
        """退出再进入俯瞰：镜头回到角色身上。"""
        for _ in range(2):
            self.controller.post_key_down(0x14).wait(); time.sleep(0.05); self.controller.post_key_up(0x14).wait()
            time.sleep(2.2)
            if self.scene() == "Scene_World":
                self.controller.post_key_down(0x14).wait(); time.sleep(0.05); self.controller.post_key_up(0x14).wait()
                time.sleep(2.5)
            if self.scene() == "Scene_Overhead":
                return True
        return False

    ANCHOR_DIR = ROOT / "resource" / "anchors"

    def go_corner(self, step: float = 0.6, max_steps: int = 12):
        """坐标原点 = 地图左上角（硬边界）。重新进俯瞰、缩到最远，再向上、向左各按足够久顶死在角上，
        这样不管角色站在哪，每次扫描的原点都一样（2026-11-04 实测：顶住边界后再按镜头不动）。
        角上的画面和这个分区存的参考图（resource/anchors/<分区>_corner.png）做相位相关，只用来核对，
        偏差大就打警告（说明没顶到角、或缩放不对）。"""
        if not self.recenter():
            sc = self.scene()
            if sc != "Scene_Overhead":
                # 不在俯瞰时 WASD 会让角色在地面走（2026-11-08 应龙关事故）：直接停，不按方向键
                raise RuntimeError(f"顶角前没能回到俯瞰（当前 {sc}），没有按方向键")
            print("  警告：重新进俯瞰没确认，但当前在俯瞰，继续")
        self.zoom_out()
        misses = 0
        for attempt in range(5):
            ok = self._corner_once()
            if ok is not False:
                break
            misses += 1
            if misses == 1:
                # 第一次对不上先重拍一次再说：2026-11-06 刚进俯瞰就顶角，第一张截图响应 0.00（过场画面），
                # 画面其实没转；这时按 Ctrl+R 反而把镜头转歪
                print("  第一次对不上，等 1.5 秒重拍再核对")
                time.sleep(1.5)
                continue
            if attempt < 4:
                # 角上画面和参考图对不上：最常见是用户用 Ctrl+R 转过镜头（2026-11-05 早上就是转了 90°）。
                # 转一下再顶角重试，最多转 3 次（4 个方向）。
                print("  角上画面和参考图对不上，按 Ctrl+R 转镜头后重试")
                self.controller.post_key_down(0x11).wait(); time.sleep(0.05)
                self.controller.post_key_down(0x52).wait(); time.sleep(0.05)
                self.controller.post_key_up(0x52).wait(); time.sleep(0.05)
                self.controller.post_key_up(0x11).wait()
                time.sleep(1.5)
        print(f"== 已到扫描起点（地图左上角），坐标原点 (0, 0)")

    def _corner_once(self) -> bool | None:
        """顶到左上角并和参考图核对。返回 True=一致，False=对不上，None=没有参考图（这次存为参考图）。"""
        up, left = self.CORNER_HOLD
        self.hold(PAN_KEYS["up"], up)
        time.sleep(0.3)
        self.hold(PAN_KEYS["left"], left)
        time.sleep(0.3)
        self.hold(PAN_KEYS["up"], 1.0)  # 再补一下，防止第一次向上时还没缩放到位
        time.sleep(0.6)
        self.cam[:] = 0
        if self.area:
            path = self.ANCHOR_DIR / f"{self.area}_corner.png"
            cur = self.shot()
            if path.exists():
                ref = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
                (dx, dy), resp = cv2.phaseCorrelate(self._gray(ref), self._gray(cur))
                if resp > 0.05 and abs(dx) < 15 and abs(dy) < 15:
                    print(f"== 已顶到地图左上角，和参考图一致（偏差 ({dx:.0f}, {dy:.0f})，响应 {resp:.2f}）")
                    return True
                print(f"== 警告：角上画面和参考图对不上（偏差 ({dx:.0f}, {dy:.0f})，响应 {resp:.2f}），坐标可能不准")
                return False
            self.ANCHOR_DIR.mkdir(parents=True, exist_ok=True)
            cv2.imencode(".png", cur)[1].tofile(str(path))
            print(f"== 第一次扫描 {self.area}，左上角画面已存为参考图")
        return None

    def screen_has_base(self, image: np.ndarray) -> bool:
        """画面里有没有基地内容：有设备标签或状态标记就算有。扫到全是草地的屏就到边了。"""
        if badges.find_badges(image):
            return True
        return bool(self.device_labels())

    CONFIRM_SKIP = {"仓库取货口", "仓库存取线基段", "仓库存取线源桩", "仓库存货口", "协议储存箱", "暗管入口", "暗管出口", "多口暗管出口", "息壤中继器", "息壤供电桩", "储气罐"}

    def confirm_pass(self, extent: tuple[int, int] | None = None, step: float = 0.6):
        """第三遍：把两遍都稳定、但名字/状态还是猜的生产设备逐台点开面板确认（快速模式按标签猜名在设备密集处不可靠，
        2026-11-04 应龙关三台「固气转化机」里两台其实是多口暗管出口和配件机）。
        点开时标记已不在的改记 passes=1（瞬时）。物流件/供电件不点。"""
        KINDS = {"阻塞": "blocked", "空闲中": "idle", "关闭": "closed"}
        targets = [
            d for d in self.devices
            if d.guessed and d.badge != "关闭"
            and not (d.name != "（无标签）" and all(n in self.CONFIRM_SKIP for n in d.name.split("|")))
        ]  # 两遍都在的 和 只在一遍出现的（间歇）都点：间歇的名字也必须是面板的
        done: set[int] = set()
        print(f"== 第三遍：逐台点开确认 {len(targets)} 台")

        def visit():
            labels = None
            for d in targets:
                if id(d) in done:
                    continue
                sx, sy = int(d.world_x - self.cam[0]), int(d.world_y - self.cam[1])
                if not (80 <= sx <= 1200 and 100 <= sy <= 500):
                    continue  # 不在这一屏的安全区（底部建造栏以上）
                if labels is None:
                    labels = self.device_labels()
                b = badges.Badge(KINDS.get(d.badge, "blocked"), sx, sy, 0.0)
                still = self.badge_still_there(b)
                res = self.open_device(b, labels, check_badge=False)
                self.ensure_overhead()
                done.add(id(d))
                if not res or res[0] in ("（没点开）", "（闪烁）"):
                    print(f"  确认失败 {d.name} @({d.world_x}, {d.world_y})，保留猜测")
                    continue
                title, st, recipe = res
                if not still:
                    d.passes = 1  # 第三次看时标记已不在：间歇
                d.name, d.status, d.guessed, d.env, d.foreign = title, st, False, self.last_env, self.last_foreign
                d.panel, d.warn = self.last_panel, self.last_warn
                d.recipe = self.desc(recipe)
                d.output = recipe.output if recipe else None
                d.recipe_id = recipe.recipe["id"] if recipe else None
                print(f"  确认 {title:<8} {st:<4} {d.recipe}{f'（入口堆着 {d.foreign}）' if d.foreign else ''}  @({d.world_x}, {d.world_y}){'' if still else '（标记已不在：间歇）'}")

        if targets:
            self.sweep_grid(extent, step, visit=visit)
            left = [d for d in targets if id(d) not in done]
            print(f"== 第三遍完成：确认 {len(done)} 台，没走到 {len(left)} 台")

    def sweep_once(self, extent: tuple[int, int] | None = None, step: float = 0.6):
        """定向巡检：只扫一遍、边扫边点开（only_names 过滤）。没有第二遍对照，所有标记按稳定算（passes=2），
        瞬时闪烁靠面板状态（生产中）排除。时间约为两遍的一半。"""
        self.ensure_overhead()
        self.read_power()
        self.open_all = True
        try:
            self.sweep_grid(extent, step)
        finally:
            self.open_all = False
        # 定向模式只对点开过面板的设备下结论：没点开的生产设备名字/配方都是猜的，单凭猜测在别的规则里会出假问题
        # （2026-11-07 景玉谷：两台没点开的封装机猜成 中容/锦草软饮，被按致密源石粉末报「送不到」）。关闭标记是确定的，保留；物流件保留
        before = len(self.devices)
        self.devices = [d for d in self.devices if not d.guessed or d.badge == "关闭" or (d.name != "（无标签）" and all(m in LOGISTICS for m in d.name.split("|")))]
        for d in self.devices:
            d.passes = 2
        self.recheck_stats = self.recheck()  # 单遍没有两遍对照，用 12 秒静止复查代替：容量箱周期造成的间歇阻塞降为瞬时
        print(f"== 单遍扫描完成：{len(self.devices)} 台（定向模式，不做两遍对照；{before - len(self.devices)} 台没点开的只记标记不下结论）")

    RECHECK_WINDOW, RECHECK_GAP = 12.0, 1.0  # 多时点确认：每屏 12 秒、每秒一帧（13 帧），盖住协议容量箱一个 10 秒存货周期

    def recheck(self, kinds: tuple[str, ...] = ("阻塞", "空闲中")) -> dict:
        """多时点确认（用户 2026-11-07）：协议容量箱每 10 秒往仓库存一次货，传输跟不上入料时上游会周期性阻塞，
        每屏 4 秒的连拍可能整段落在阻塞那一半里。这里把扫描里稳定的阻塞/空闲设备按屏分组，镜头移过去静止拍 12 秒（每秒一帧）：
          - 有的帧在、有的帧不在 → 间歇（容量箱周期 / 等料周期），记 passes=1，不进常规规则；
          - 每帧都在 → 持续，保持原判（持续阻塞再由 combine 看最终产物是否爆仓）；
          - 一帧都对不上 → 可能是镜头里程计漂了，不改结论，只记一笔（宁可漏降级，不能把真问题降掉）。
        先在第一帧里按 60 像素找同类标记对上（goto_world 有几十像素误差），之后镜头不动，按这个位置 ±10 像素逐帧数。"""
        KINDS = {"阻塞": "blocked", "空闲中": "idle"}
        todo = [d for d in self.devices if d.badge in kinds and d.passes >= 2]
        todo.sort(key=lambda d: (d.world_y // 300, d.world_x))
        stats = {"devices": len(todo), "screens": 0, "intermittent": 0, "persistent": 0, "unmatched": 0}
        if not todo:
            return stats
        print(f"== 多时点确认：{len(todo)} 台稳定的阻塞/空闲设备，每屏静止 {self.RECHECK_WINDOW:g} 秒")
        n = int(self.RECHECK_WINDOW / self.RECHECK_GAP) + 1
        while todo:
            d0 = todo[0]
            if not self.goto_world(d0.world_x, d0.world_y):
                print(f"  到不了 ({d0.world_x}, {d0.world_y})，跳过")
                todo.pop(0)
                stats["unmatched"] += 1
                continue
            here = [d for d in todo if 60 <= d.world_x - self.cam[0] <= 1220 and 80 <= d.world_y - self.cam[1] <= 560]
            if d0 not in here:
                here.append(d0)
            frames = []
            for i in range(n):
                frames.append(badges.find_badges(self.shot()))
                if i < n - 1:
                    time.sleep(self.RECHECK_GAP)
            stats["screens"] += 1
            for d in here:
                kind = KINDS[d.badge]
                ex, ey = d.world_x - self.cam[0], d.world_y - self.cam[1]
                anchor = None
                for fs in frames:
                    near = [b for b in fs if b.kind == kind and abs(b.x - ex) <= 60 and abs(b.y - ey) <= 60]
                    if near:
                        anchor = min(near, key=lambda b: abs(b.x - ex) + abs(b.y - ey))
                        break
                if anchor is None:
                    d.seen += f"|复查{self.RECHECK_WINDOW:g}s 没对上"
                    stats["unmatched"] += 1
                    continue
                hits = sum(any(b.kind == kind and abs(b.x - anchor.x) <= 10 and abs(b.y - anchor.y) <= 10 for b in fs) for fs in frames)
                d.seen += f"|复查{self.RECHECK_WINDOW:g}s {hits}/{n}"
                if hits < n:
                    d.passes = 1
                    stats["intermittent"] += 1
                    print(f"  间歇 {d.name:<8} {d.badge} {hits}/{n} 帧  @({d.world_x}, {d.world_y})")
                else:
                    stats["persistent"] += 1
                    print(f"  持续 {d.name:<8} {d.badge} {hits}/{n} 帧  @({d.world_x}, {d.world_y})")
            todo = [d for d in todo if d not in here]
        print(f"== 多时点确认完成：{stats}")
        return stats

    def sweep_twice(self, extent: tuple[int, int] | None = None, step: float = 0.6, on_device=None):
        """同一网格扫两遍，隔了几分钟还在的标记才算数：只在一遍里出现的记 passes=1（瞬时），两遍都在的 passes=2。
        第二遍里新出现的也记 passes=1。坐标按第一遍的为准（两遍路径一样，30 像素内对应）。"""
        self.open_all = False
        self.ensure_overhead()
        self.read_power()
        self.sweep_grid(extent, step, on_device=on_device)
        first = list(self.devices)
        self.devices = []
        # 第一遍已经点开确认过的（无标签、上下两台分不清的）第二遍不再点
        self.known = [d for d in first if not d.guessed and d.name not in ("（无标签）", "（没点开）", "（闪烁）")]
        print(f"== 第一遍完成，开始第二遍（对照瞬时标记；稳定的生产设备当场点开确认，第一遍点开过的 {len(self.known)} 台不再点）")
        self.open_all = True
        try:
            self.sweep_grid(extent, step)
        finally:
            self.open_all = False
            self.known = []
        second = self.devices
        for d in first:
            hit = next((e for e in second if e.badge == d.badge and abs(e.world_x - d.world_x) <= 30 and abs(e.world_y - d.world_y) <= 30), None)
            if hit:
                d.passes = 2
                if hit.name != "（无标签）" and (d.name == "（无标签）" or not hit.guessed):
                    # 第二遍点开确认过的（guessed=False）一律以第二遍为准：名字、状态、配方、入口、面板图
                    d.name, d.status, d.recipe, d.output, d.recipe_id, d.guessed, d.env = hit.name, hit.status, hit.recipe, hit.output, hit.recipe_id, hit.guessed, hit.env
                    d.foreign, d.shot, d.sx, d.sy, d.panel, d.warn = hit.foreign, hit.shot, hit.sx, hit.sy, hit.panel, hit.warn
                d.seen += f"+{hit.seen}"
        matched = {id(e) for d in first for e in second if e.badge == d.badge and abs(e.world_x - d.world_x) <= 30 and abs(e.world_y - d.world_y) <= 30}
        extra = [e for e in second if id(e) not in matched]
        for e in extra:
            e.passes = 1
        self.devices = first + extra
        n2 = sum(d.passes == 2 for d in self.devices)
        print(f"== 两遍对照：{len(first)} + {len(extra)} 台，其中两遍都在 {n2} 台，只出现一遍 {len(self.devices) - n2} 台（瞬时）")

    def sweep_grid(self, extent: tuple[int, int] | None = None, step: float = 0.6, deep: bool = False, on_device=None, max_cols: int = 12, max_rows: int = 12, visit=None):
        """固定网格扫描：从地图左上角（硬边界）出发，每行向右走到顶住右边界为止，再下一行；向下顶住就结束。
        每行都先回左边界再出发，x 的里程误差不跨行累积。extent=(宽, 高) 只做上限。
        不看画面里有没有基地内容，边界决定走几屏，所以每次走的屏数一样、坐标一样。"""
        scan = self.scan_view if deep else self.scan_view_fast
        step_px = self.PAN_SPEED * step
        if extent:
            max_cols = min(max_cols, max(1, int(np.ceil(max(0, extent[0] - 1280) / step_px)) + 1))
            max_rows = min(max_rows, max(1, int(np.ceil(max(0, extent[1] - 720) / step_px)) + 1))

        def scan_here():
            if visit is not None:  # 第三遍：不扫描，按坐标点开已知设备
                visit()
                return
            found = scan()
            if on_device:
                for d in found:
                    on_device(d, int(d.world_x - self.cam[0]), int(d.world_y - self.cam[1]))

        self.ensure_overhead()
        self.go_corner()
        move_cursor(self.hwnd, 1270, 8)  # 光标停到地图外：悬停提示框是静止的，会骗相位相关
        self.trust_nominal = True
        self.cached_labels = None
        self.cached_feats = None
        try:
            print(f"== 网格扫描（最多 {max_cols} 列 × {max_rows} 行，步长 {step_px:.0f} 像素，走到边界为止）")
            for r in range(max_rows):
                if r > 0:
                    self.hold(PAN_KEYS["left"], 3.5)  # 回到左边界（硬边界）重新出发
                    time.sleep(0.6)
                    self.cam[0] = 0
                    self.cached_labels = None
                    self.cached_feats = None
                print(f"== 第 {r + 1} 行，基地坐标 ({self.cam[0]:.0f}, {self.cam[1]:.0f})")
                scan_here()
                for _ in range(max_cols - 1):
                    if self.cached_labels is None:
                        self.cached_labels = self.device_labels()
                    if self.cached_feats is None:
                        self.cached_feats = self.features(self.shot(), self.cached_labels)
                    self.pan("right", step)
                    if self.last_pan_stuck:
                        break  # 顶住右边界：这一行到头
                    print(f"== 镜头 right，基地坐标 ({self.cam[0]:.0f}, {self.cam[1]:.0f})")
                    scan_here()
                if r < max_rows - 1:
                    if self.cached_labels is None:
                        self.cached_labels = self.device_labels()
                    self.cached_feats = self.features(self.shot(), self.cached_labels)
                    self.pan("down", step)
                    if self.last_pan_stuck:
                        print("== 顶住下边界：扫完")
                        break
        finally:
            self.trust_nominal = False
            self.cached_labels = None
            self.cached_feats = None

    def sweep(self, step: float = 0.6, max_steps: int = 10, deep: bool = False, on_device=None):
        """蛇形平移覆盖基地：一行向一个方向走，连续两屏没有基地内容就换行；整行都没内容就结束。

        deep=False 用快速模式（不点面板，整个基地 1–2 分钟）；deep=True 逐台点开读配方（慢，按需用）。
        on_device(device, screen_x, screen_y) 可选回调：每扫到一台新设备就调一次（定点操作用）。
        """
        scan = self.scan_view if deep else self.scan_view_fast

        def scan_here() -> bool:
            before = len(self.devices)
            self.last_screen_has_base = None
            found = scan()
            if on_device:
                for d in found:
                    on_device(d, int(d.world_x - self.cam[0]), int(d.world_y - self.cam[1]))
            if self.last_screen_has_base is None:  # 深入模式没算，补一次
                self.last_screen_has_base = self.screen_has_base(self.shot())
            return self.last_screen_has_base or len(self.devices) > before

        self.ensure_overhead()  # 上次扫描可能留下没关的面板，面板开着镜头推不动
        self.go_corner(step, max_steps)
        print("== 当前画面")
        row_has_base = scan_here()
        horizontal = "right"
        started = row_has_base
        start_x = float(self.cam[0])  # 起点那一列的 x：原点按参考图校准后可能是负数，左边界不能写死成 0
        for row in range(max_steps):
            empty_run = 0
            for _ in range(max_steps):
                if horizontal == "left" and self.cam[0] <= start_x + 20:
                    break  # 已经回到起点这一列，再往左只有草地
                move = self.pan(horizontal, step)
                if np.abs(move).max() < 20:
                    break  # 真到地图边界了
                print(f"== 镜头 {horizontal}，位移 ({move[0]:.0f}, {move[1]:.0f})，基地坐标 ({self.cam[0]:.0f}, {self.cam[1]:.0f})")
                if scan_here():
                    row_has_base = True
                    empty_run = 0
                else:
                    empty_run += 1
                    if empty_run >= 2 and row_has_base:
                        break  # 这一行已经扫过基地、又连续两屏空地：到行尾了
            if started and not row_has_base:
                break  # 整行没有基地内容：扫完了
            started = started or row_has_base
            move = self.pan("down", step)
            if np.abs(move).max() < 20:
                break
            print(f"== 镜头 down，基地坐标 ({self.cam[0]:.0f}, {self.cam[1]:.0f})")
            row_has_base = scan_here()
            horizontal = "left" if horizontal == "right" else "right"

    # ---- 定点深查 ----
    def goto_world(self, wx: int, wy: int, step: float = 0.6, tries: int = 30) -> bool:
        """把镜头移到基地坐标 (wx, wy) 附近（目标落在画面中部）。坐标系以 go_corner 定的原点为准。

        步长和扫描一样用 0.6 秒：小步平移时小地图变化太小，容易被误判成边界。
        剩余距离不足一步时用短步微调。连续两次没动才算真到边界。
        """
        stuck = 0
        for _ in range(tries):
            dx = wx - (self.cam[0] + 640)
            dy = wy - (self.cam[1] + 330)
            if abs(dx) < 220 and abs(dy) < 180:
                return True
            direction = ("right" if dx > 0 else "left") if abs(dx) >= abs(dy) else ("down" if dy > 0 else "up")
            remain = abs(dx) if direction in ("left", "right") else abs(dy)
            move = self.pan(direction, step if remain > self.PAN_SPEED * step * 0.8 else 0.3)
            if np.abs(move).max() < 10:
                stuck += 1
                if stuck >= 2:
                    return abs(dx) < 400 and abs(dy) < 300  # 到边界推不动了，目标够近就算到了
            else:
                stuck = 0
        return False

    def deep_check(self, targets: list[tuple[int, int, str]]) -> list[Device]:
        """对快速模式标出的几个坐标逐个点开读设备名、状态、配方。targets: [(世界x, 世界y, 标记类型), ...]"""
        self.ensure_overhead()
        self.go_corner()
        found = []
        for wx, wy, kind in targets:
            if not self.goto_world(wx, wy):
                print(f"  到不了 ({wx}, {wy})")
                continue
            sx, sy = int(wx - self.cam[0]), int(wy - self.cam[1])
            labels = self.device_labels()
            fake = badges.Badge(kind, sx, sy, 0.0)
            res = self.open_device(fake, labels, check_badge=False)  # 定点深查是明确要看这台：标记不在也点开
            name, status, recipe = res if res else ("（没点开）", "", None)
            dev = Device(
                name, status, badges.SPECS[kind]["label"], wx, wy,
                recipe=self.desc(recipe), output=recipe.output if recipe else None,
                recipe_id=recipe.recipe["id"] if recipe else None, env=self.last_env, foreign=self.last_foreign, panel=self.last_panel, warn=self.last_warn,
            )
            self.devices.append(dev)
            found.append(dev)
            print(f"  {dev.badge:<4} {dev.name:<6} {dev.status or '?':<4} {dev.recipe}{'（无环境）' if dev.env else ''}{f'（入口堆着 {dev.foreign}）' if dev.foreign else ''}  @({wx}, {wy})")
        return found

    # ---- 保存 ----
    def save(self, area: str | None = None):
        """把巡检结果存成 debug/patrol_<时间>.json（含地区、分区），返回路径。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = DEBUG_DIR / f"patrol_{stamp}.json"
        out = {
            "region": self.region, "area": area, "time": stamp,
            "shots": str(self.shot_dir.relative_to(ROOT)) if self.shot_dir else None,  # 每屏截图目录（定位图、离线复核用）
            "power": self.power,  # 顶部电量框「用电/上限 百分比」
            "checks": self.checks,  # 错题本定点核查（playbook.checks）
            "recheck": self.recheck_stats,  # 多时点确认（定向巡检：每屏静止 12 秒复查阻塞/空闲）
            "panels": str(self.panel_dir.relative_to(ROOT)) if self.panel_dir else None,  # 点开过的面板图
            "devices": [asdict(d) for d in self.devices],
        }
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return path


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # 后台跑时日志逐行落盘，中途被杀也能看到进度
    area = None  # 分区名（武陵城 / 景玉谷 / 应龙关），面板里读不到，由调用方给
    if "--area" in argv:
        area = argv[argv.index("--area") + 1]
    patrol = Patrol()
    patrol.area = area
    if patrol.scene() != "Scene_Overhead":
        print("当前不在俯瞰。请先进入俯瞰（CapsLock）。")
        return 2
    deep = "--deep" in argv
    if "--no-shots" not in argv:  # 默认存每屏截图和每张面板图（离线复核、定位图、回归都靠它们；--no-shots 关掉）
        patrol.shot_dir = DEBUG_DIR / "shots" / time.strftime("%Y%m%d-%H%M%S")
        patrol.panel_dir = DEBUG_DIR / "panels" / time.strftime("%Y%m%d-%H%M%S")
    t0 = time.time()
    if "--confirm-from" in argv:
        # 用一份已有的扫描结果（同一坐标系：地图左上角为原点）只跑第三遍确认，省掉前两遍
        import blueprint
        from dataclasses import fields as _fields

        src_path = Path(argv[argv.index("--confirm-from") + 1])
        raw = json.loads(src_path.read_text(encoding="utf-8"))
        names = {f.name for f in _fields(Device)}
        patrol.devices = [Device(**{k: v for k, v in r.items() if k in names}) for r in raw["devices"]]
        patrol.region = raw.get("region")
        area = area or raw.get("area")
        patrol.area = area
        extent = blueprint.load(None).area_options(area).get("extent") if area else None
        patrol.confirm_pass(tuple(extent) if extent else (2300, 2200))
    elif "--at" in argv:
        # 定点深查：--at "x,y,blocked;x,y,idle"（类型可省，默认 blocked）
        targets = []
        for item in argv[argv.index("--at") + 1].split(";"):
            parts = item.split(",")
            targets.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else "blocked"))
        patrol.deep_check(targets)
    elif "--checks" in argv:  # 只跑错题本定点核查（已在俯瞰里）
        patrol.run_checks()
        print(json.dumps(patrol.checks, ensure_ascii=False))
        return 0
    elif "--sweep" in argv:
        import blueprint

        extent = blueprint.load(None).area_options(area).get("extent") if area else None
        if "--old-sweep" in argv:
            patrol.sweep(deep=deep)
        elif "--once" in argv or deep:
            patrol.sweep_grid(tuple(extent) if extent else None, deep=deep)
        else:
            if not extent:
                print("（分区没登记 extent，按 [2300, 2200] 封顶扫；扫完按日志里的设备范围登记）")
            ext = tuple(extent) if extent else (2300, 2200)
            patrol.sweep_twice(ext)  # 第二遍已当场点开确认；按坐标回头点的第三遍会点错邻居，只在 --confirm 时跑
            patrol.run_checks()  # 错题本定点核查（playbook.checks）
            if "--confirm" in argv:
                patrol.confirm_pass(ext)
    else:
        print("== 当前画面")
        (patrol.scan_view if deep else patrol.scan_view_fast)()
    print(f"用时 {time.time() - t0:.0f} 秒（{'深入' if deep else '快速'}模式）")

    if patrol.region is None:
        # 快速模式不开面板读不到地区名：按 --region，或从蓝图档案的分区表反查
        if "--region" in argv:
            patrol.region = argv[argv.index("--region") + 1]
        elif area:
            import blueprint

            patrol.region = next((r for r, areas in blueprint.load(None).areas.items() if area in areas), None)
    path = patrol.save(area)
    print(f"\n地区 {patrol.region} / {area or '?'}，共 {len(patrol.devices)} 台有标记的设备，已保存：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
