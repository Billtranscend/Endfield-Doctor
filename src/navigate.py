"""传送到某个分区的基地，并进入俯瞰。

流程（2026-11-04 用 1.6 皇帝产线账号手动走通，坐标 720p）：
  1. 大世界按 M 打开当前分区地图，左上角标题形如「// 武陵 /景玉谷」。
  2. 不在目标分区时：点右下角「地区总览」→ 目标在另一个地区先点地区页签 → OCR 找到分区名点进去。
     总览里找不到目标分区名（或分区传 None / "?"）时自动探测：把总览里的分区逐个点进去找基地，
     找到就传送，并把真实分区名写回蓝图档案的 areas.json（_discovered 记录），以后不用再探测。
  3. 分区地图里找基地图标（模板 resource/badges/core_icon*.png，援建点和主基地图标不同，逐个模板匹配），点它。
     所有模板都没到 0.7 时，退一步试一次最高分 ≥ 0.6 的候选（仍靠卡片标题把关）。
     特例分区（蓝图 areas.json 里写成对象）：
       drag            先拖地图 [x1, y1, x2, y2]（可选第 5 个数是毫秒，默认 700），基地在默认视野外时用；
       icon            只用这一张模板（resource/badges/ 下）；
       menu_pick       点图标后弹「选择目标」小菜单（设备重叠）时，在点击位置右侧 OCR 这行文字点它；
       single_teleport 卡片右下只有一个「传送」(1140, 660)。
     例：武陵城协议核心在北边视野外 → {"name": "武陵城", "drag": [640,150,640,550], "icon": "core_icon_main.png",
         "menu_pick": "协议核心", "single_teleport": true}
     地图点击后会重新居中，所以每次点之前都重新截图再找。
  4. 右侧信息卡标题含「援建点」或「协议核心」、右下角是「详情」「传送」两个按钮，才点「传送」；
     普通传送点（只有一个「传送」）和预警终端（「前往传送」「追踪」）都不是，Esc 关卡片重找。
  5. 等加载；左上角模式是「探索」时按 TAB 切到「工业」。
  6. 走几步直到顶部出现「进入俯瞰」（Scene_World），再按 CapsLock 进俯瞰（Scene_Overhead）。
     CapsLock 只有提示出现后才按，否则只会切系统大写锁定。
任何一步失败：按 Esc 关掉地图/卡片，返回 False 并打印失败在哪一步。

用法：.venv\\Scripts\\python.exe src\\navigate.py 武陵 景玉谷
      .venv\\Scripts\\python.exe src\\navigate.py --overhead      # 只做第 5、6 步（已经在基地附近）
"""

import ctypes
import re
import sys
import time

import json

import cv2
from paths import imread as _imread  # cv2.imread/imwrite 不认中文路径
import numpy as np

from maa.resource import Resource
from maa.tasker import Tasker

import blueprint as bp
from game import ROOT, check_stop, connect, find_game_window, init_toolkit

VK = {"w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44, "capslock": 0x14, "tab": 0x09, "m": 0x4D, "esc": 0x1B, "f10": 0x79, "space": 0x20}

# ---- 地图（720p）----
MAP_TITLE_ROI = [20, 10, 420, 40]  # 左上角「// 武陵 /景玉谷」（区域是估计的，没实测框过）
OVERVIEW_BUTTON = (1140, 660)  # 右下角「地区总览」
REGION_TABS = {"武陵": (1023, 660), "四号谷地": (1172, 660)}  # 地区总览右下角的地区页签
OVERVIEW_ROI = [0, 40, 1280, 600]  # 2026-11-07 放宽：北部禁区在 y≈55、藏剑谷在 y≈630，原来 60–620 都漏
# 地区总览里分区名的固定位置（720p，2026-11-06 武陵总览截图量的）。总览文字小而灰，OCR 时常漏（「武陵城」整个漏掉、
# 「试验园区」读成「特」），漏了就按这里点，点完仍用地图标题核对
OVERVIEW_POS = {
    "武陵": {"武陵城": (540, 312), "景玉谷": (438, 505), "应龙关": (733, 245), "试验园区": (655, 320),
           "雪松林": (812, 312), "清波寨": (607, 440), "遂明": (851, 540), "藏剑谷": (590, 630), "祀的画": (1112, 190),
           "首墩": (400, 400), "北部禁区": (818, 55)},  # 首墩、北部禁区 2026-11-07 补（debug/overview_wuling_1700.png）；清波寨 原误记青波寨
}  # 地区总览里找分区名的范围（避开底部页签栏，页签也叫「四号谷地」）
CARD_TITLE_ROI = [950, 55, 300, 30]  # 右侧信息卡标题
CARD_WAIT = 3.0  # 点图标后 1.5 秒卡片还是空的，再等最多这么久（慢电脑卡片弹得慢）
CARD_BUTTON_ROI = [960, 640, 320, 42]  # 右下角按钮文字
TELEPORT_BUTTON = (1180, 660)  # 基地卡片的「传送」（左边是「详情」1040,660）
TP_BUTTON = (1140, 660)  # 只有一个「传送」的卡片（如武陵城协议核心）
DRAG_MS = 700  # 拖地图的默认时长
MENU_W, MENU_H = 320, 140  # 「选择目标」小菜单：在点击位置右侧找
# 基地在地图上的名字因分区而异：景玉谷「天王坪援建点」、应龙关「盈天台建设站」、首墩「心脏修缮站」（2026-11-07）、主基地「协议核心」
CORE_TITLE_RE = re.compile(r"援建点|建设站|修缮站|协议核心|核心")
CORE_THRESHOLD = 0.7
CORE_FALLBACK = 0.6  # 所有模板都没到 0.7 时，最高分 ≥ 这个值的候选也试一次
CORE_MERGE = 20  # 不同模板/相邻位置的候选，20 像素内只留分数高的
CORE_MAX_TRIES = 5
# 地区总览里只接受 2–6 个汉字的分区名；这些是总览里认到的非分区文字
AREA_NAME_RE = re.compile(r"^[一-鿿]{2,6}$")
NOT_AREAS = {"奇闻满地跑", "地区总览", "武陵", "四号谷地"}
# 找基地图标时不看的区域：左上标题、右侧信息卡、右下按钮栏
# 2026-11-07：原来把 x≥930 整条都屏蔽（信息卡位置），把首墩的基地图标（x≈1013, y≈540）也挡掉了；找图标时卡片还没开，
# 只挡右上角飞船标和右侧缩放按钮即可
CORE_MASKS = [(0, 0, 460, 60), (1030, 40, 250, 100), (1200, 220, 80, 220), (0, 630, 1280, 90)]
# 地图缩放因人而异（地图打开时沿用上次的缩放），基地图标按这几种大小找。2026-11-11 用 8 张地图比过：
# 1.0–1.5 时 7 张基地图的基地分数比其他位置高 0.09–0.36；加上 0.8/0.9 会多出一批 0.66–0.70 的假匹配，群友那张余量只剩 0.01–0.03
CORE_SCALES = (1.0, 1.1, 1.2, 1.3, 1.4, 1.5)


def in_core_mask(x: int, y: int) -> bool:
    return any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in CORE_MASKS)

# ---- 大世界 ----
MODE_ROI = [40, 22, 110, 30]  # 左上角模式框「探索 / 工业」
LOAD_WAIT = 9.0  # 传送后的加载时间
LOAD_EXTRA = 15.0  # 9 秒后还没读到模式框时，最多再等这么久
# 还在加载画面（左上角「明日方舟」标志，OCR 读成「明日方 / 日方」）时一直等，传送后最多这么久。
# 2026-11-11 群友 H 的应龙关：点传送 24 秒后还是加载画面，被判成传送失败，重试一次才进去
LOAD_MAX = 60.0
LOADING_RE = re.compile(r"明日|日方|方舟")
# 落地后没看到「进入俯瞰」：先按住 W 跳两下往前冲（用户 2026-11-11：比走快很多），还不在区域里再按老办法四处走。
# 2026-11-11 统计 198 次传送：落地就在区域里约 12 秒（9 秒是加载）；在外面时多半往前 2 秒就进去了；
# 偶尔往前走不进去，按 W/A/D/S 四处摸，花了 61–106 秒
MOVES = [("jump", 1.2), ("w", 2.0), ("a", 2.0), ("w", 2.0), ("d", 3.0), ("w", 2.0), ("s", 4.0), ("a", 3.0)]
JUMPS = 2  # 「jump」一步里按几次空格


def caps_on() -> bool:
    ctypes.windll.user32.GetKeyboardState((ctypes.c_ubyte * 256)())
    return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)


class Navigator:
    def __init__(self):
        init_toolkit()
        window = find_game_window()
        if window is None:
            raise SystemExit("没找到游戏窗口。")
        self.controller = connect(window)
        if self.controller is None:
            raise SystemExit("连接游戏窗口失败。")
        resource = Resource()
        resource.post_bundle(str(ROOT / "resource")).wait()
        self.tasker = Tasker()
        self.tasker.bind(resource, self.controller)
        # 基地图标模板：援建点 core_icon.png、主基地 core_icon_main.png ……，有几张用几张
        self.core_tpls = [
            (p.name, img)
            for p in sorted((ROOT / "resource" / "badges").glob("core_icon*.png"))
            if (img := _imread(str(p))) is not None
        ]
        if not self.core_tpls:
            raise SystemExit("没有基地图标模板 resource/badges/core_icon*.png")
        self.last_error = ""  # 最近一次失败在哪一步（给 full_patrol 汇总用）
        self.discovered: str | None = None  # 最近一次自动探测到的分区名

    # ---- 底层（同 patrol.Patrol）----
    def shot(self) -> np.ndarray:
        self.controller.post_screencap().wait()
        return self.controller.cached_image.copy()

    def ocr_boxes(self, roi) -> list[tuple[list[int], str]]:
        node = {"Nav_OCR": {"recognition": "OCR", "roi": roi, "action": "DoNothing", "timeout": 1, "rate_limit": 0}}
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("Nav_OCR", node).wait().get()
        reco = detail.nodes[0].recognition if detail and detail.nodes else None
        return [(list(r.box), r.text.strip()) for r in reco.all_results] if reco else []

    def ocr_text(self, roi) -> str:
        return "".join(t for _, t in sorted(self.ocr_boxes(roi), key=lambda bt: bt[0][0])).strip()

    def scene(self) -> str:
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("P0_DetectScene").wait().get()
        hit = [n.name for n in detail.nodes] if detail else []
        return next((n for n in hit if n.startswith("Scene_") and n != "Scene_Unknown"), "Scene_Unknown")

    def click(self, x: int, y: int):
        self.controller.post_click(int(x), int(y)).wait()

    def key(self, name: str, hold: float = 0.05):
        """按下 → 停顿 → 松开（MaaFw 自带的 post_click_key 太快，CapsLock 偶尔不生效）。"""
        self.controller.post_key_down(VK[name]).wait()
        time.sleep(hold)
        self.controller.post_key_up(VK[name]).wait()
        time.sleep(0.2)

    def jump_forward(self, seconds: float = 1.2, jumps: int = JUMPS):
        """按住 W，期间按 jumps 次空格往前跳（只在大世界里用，进俯瞰前）。松开 W 放在 finally 里，出错也不会一直往前走。"""
        self.controller.post_key_down(VK["w"]).wait()
        try:
            gap = max(0.1, seconds / jumps - 0.25)
            for _ in range(jumps):
                time.sleep(0.15)
                self.controller.post_key_down(VK["space"]).wait()
                time.sleep(0.08)
                self.controller.post_key_up(VK["space"]).wait()
                time.sleep(gap)
        finally:
            self.controller.post_key_up(VK["w"]).wait()
        time.sleep(0.2)

    def map_open(self) -> bool:
        """地图/地区总览开着：左上角标题「// 武陵 /景玉谷」里有「/」或地区名。"""
        text = self.ocr_text(MAP_TITLE_ROI)
        return "/" in text or any(r in text for r in REGION_TABS)

    def menu_open(self) -> bool:
        """大世界里多按 Esc 会打开主菜单（底部有「工业简报」按钮）。"""
        return "简报" in self.ocr_text([680, 575, 130, 36])

    def fail(self, step: str, esc: int = 2) -> bool:
        print(f"失败：{step}")
        self.last_error = step
        for _ in range(esc):  # 只在地图/卡片还开着时按 Esc，否则会在大世界里打开主菜单
            if not self.map_open():
                break
            self.key("esc")
            time.sleep(0.8)
        if self.menu_open():
            self.click(1217, 38)
            time.sleep(1.0)
            print("  关掉了误开的主菜单")
        return False

    # ---- 地图 ----
    def map_title(self) -> tuple[str, str] | None:
        """读地图左上角标题，返回（地区, 分区）；没读到返回 None。"""
        return parse_map_title(self.ocr_text(MAP_TITLE_ROI))

    def open_map(self) -> tuple[str, str] | None:
        self.key("m")
        for _ in range(4):
            time.sleep(1.0)
            title = self.map_title()
            if title:
                return title
        return None

    def open_overview(self, region: str, cur_region: str | None) -> list[tuple[list[int], str]] | None:
        """从分区地图点「地区总览」，需要时切地区页签，返回总览里 OCR 到的文字框；地区不认识返回 None。"""
        if region not in REGION_TABS:
            print(f"  不认识的地区「{region}」，已知：{'、'.join(REGION_TABS)}")
            return None
        known = set(OVERVIEW_POS.get(region, {}))
        for attempt in range(3):
            self.click(*OVERVIEW_BUTTON)
            time.sleep(1.5)
            if region != cur_region:
                self.click(*REGION_TABS[region])
                time.sleep(1.5)
            boxes = self.ocr_boxes(OVERVIEW_ROI)
            names = {t for _, t in boxes}
            if not known or any(k in names or any(k in t for t in names) for k in known):
                return boxes
            # 一个已知分区名都没认到：总览多半没打开（2026-11-06 第一次点时 OCR 读到的是基地画面），等一下再读、再点
            print("  总览里没认到任何已知分区名，重试")
            time.sleep(1.5)
            boxes = self.ocr_boxes(OVERVIEW_ROI)
            if any(k in {t for _, t in boxes} for k in known):
                return boxes
        return boxes

    @staticmethod
    def area_names(boxes) -> list[str]:
        """总览里认到的分区名：只要 2–6 个汉字、不含「…」「.」的，去掉已知的非分区文字。"""
        names = [t for _, t in boxes if AREA_NAME_RE.match(t) and t not in NOT_AREAS]
        return list(dict.fromkeys(names))

    def click_area(self, boxes, area: str) -> bool:
        """在总览里点分区名，进入后用地图标题核对。找不到这个名字返回 False（不打印失败）。"""
        exact = [b for b, t in boxes if t == area]
        loose = [b for b, t in boxes if area in t]
        hits = exact or loose
        if hits:
            x, y, w, h = hits[0]
            cx, cy = x + w // 2, y + h // 2
        else:
            pos = next((tbl[area] for tbl in OVERVIEW_POS.values() if area in tbl), None)
            if pos is None:
                return False
            print(f"  总览 OCR 没认到「{area}」，按固定位置 {pos} 点")
            cx, cy = pos
        self.click(cx, cy)
        time.sleep(2.0)
        title = self.map_title()
        if not title or not same_area(title[1], area):
            print(f"  点了「{area}」，地图标题是 {title}")
            return False
        return True

    def back_to_overview(self, region: str) -> list[tuple[list[int], str]] | None:
        """从分区地图（卡片可能还开着）回到地区总览，返回总览文字框。用 OCR 判断当前在哪一层，不靠 Esc 的次数。"""
        for _ in range(4):
            buttons = self.ocr_text(CARD_BUTTON_ROI)
            if "地区总览" in buttons:  # 分区地图、没有卡片：直接点总览按钮
                return self.open_overview(region, None)
            if not self.map_open():  # 地图被关掉了：重新打开再来
                if self.open_map() is None:
                    return None
                continue
            if any(r in buttons for r in REGION_TABS) and "传送" not in buttons:
                # 已经在总览（右下角是地区页签）：点一下目标地区页签，保证在对的地区
                self.click(*REGION_TABS[region])
                time.sleep(1.5)
                return self.ocr_boxes(OVERVIEW_ROI)
            self.key("esc")  # 还有卡片开着：关掉
            time.sleep(1.0)
        return None

    def discover(self, region: str, boxes) -> tuple[str, str] | None:
        """把总览里的分区逐个点进去找基地；找到时卡片保持打开，返回（分区名, 卡片标题）。"""
        names = self.area_names(boxes)
        print(f"  自动探测：总览里的分区 {'、'.join(names) or '（无）'}")
        for name in names:
            print(f"  -- 探测「{name}」")
            if self.click_area(boxes, name) and self.click_core():
                return name, self.ocr_text(CARD_TITLE_ROI)
            boxes = self.back_to_overview(region)
            if boxes is None:
                print("  回不到地区总览，停止探测")
                return None
        return None

    def find_cores(
        self, image: np.ndarray, threshold: float = CORE_THRESHOLD, tpls: list | None = None
    ) -> list[tuple[float, int, int, str]]:
        """地图上所有像基地图标的位置（分数, 中心 x, 中心 y, 模板名），分数从高到低。
        每个模板按 CORE_SCALES 几种大小分别匹配，再合并：20 像素内只留分数高的。
        2026-11-11 灰度群友的应龙关：地图放大了一档，基地图标比模板大约 1.3 倍，原尺寸只匹配到 0.49；
        原来先把界面区域涂黑再匹配，黑块边缘 (1206, 232) 假匹配到 0.66，点到了他放的地图标记「标记1」，传送失败。
        现在在原图上匹配，中心落在界面区域里的候选丢掉。"""
        cands = []
        for name, tpl in tpls or self.core_tpls:
            for s in CORE_SCALES:
                t = tpl if s == 1.0 else cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
                res = cv2.matchTemplate(image, t, cv2.TM_CCOEFF_NORMED)
                th, tw = t.shape[:2]
                ys, xs = np.where(res >= threshold)
                tag = name if s == 1.0 else f"{name}×{s:g}"
                cands += [(float(res[y, x]), int(x + tw // 2), int(y + th // 2), tag) for y, x in zip(ys, xs)
                          if not in_core_mask(int(x + tw // 2), int(y + th // 2))]
        out: list[tuple[float, int, int, str]] = []
        for c in sorted(cands, reverse=True):
            if all(abs(c[1] - o[1]) > CORE_MERGE or abs(c[2] - o[2]) > CORE_MERGE for o in out):
                out.append(c)
        return out

    def card_is_core(self, single: bool = False, wait: float = CARD_WAIT) -> tuple[bool, str]:
        """卡片是基地：标题含援建点/建设站/协议核心，右下有「传送」（不是「前往传送」）；
        普通基地卡片还有「详情」，single=True（只有一个「传送」）时不要求。
        标题和按钮都读空时（卡片还没弹出来）每 0.5 秒再读，最多再等 wait 秒（2026-11-12 群友：两次都读到「卡片「」，按钮「」」）。"""
        end = time.time() + wait
        while True:
            title = self.ocr_text(CARD_TITLE_ROI)
            buttons = self.ocr_text(CARD_BUTTON_ROI)
            if title.strip() or buttons.strip() or time.time() >= end:
                break
            time.sleep(0.5)
        ok = bool(CORE_TITLE_RE.search(title)) and "传送" in buttons and "前往" not in buttons
        ok = ok and (single or "详情" in buttons)
        return ok, f"卡片「{title}」，按钮「{buttons}」"

    def pick_menu(self, x: int, y: int, text: str) -> bool:
        """点图标后弹出「选择目标」小菜单（几个设备重叠）：OCR 找 text 那一行并点它。

        实测（武陵城协议核心）菜单不在点击位置旁边，而是弹在地图右侧（x 约 900–1100、y 和点击处相近），
        所以先在点击位置右侧找，找不到就扫整个地图区域。
        """
        x0, y0 = min(x + 5, 1280 - MENU_W), max(0, min(y - MENU_H // 2, 720 - MENU_H))
        hits = [b for b, s in self.ocr_boxes([x0, y0, MENU_W, MENU_H]) if text in s]
        if not hits:
            hits = [b for b, s in self.ocr_boxes([200, 60, 1000, 560]) if text in s and b[2] < 200]
        if not hits:
            return False
        bx, by, bw, bh = hits[0]
        print(f"    弹出选择菜单，点「{text}」({bx + bw // 2}, {by + bh // 2})")
        self.click(bx + bw // 2, by + bh // 2)
        time.sleep(1.5)
        return True

    def click_core(self, opts: dict | None = None) -> bool:
        """找基地图标并点开卡片；点错（传送点、预警终端）就 Esc 换下一个。
        opts 是分区特例配置：icon 只用这张模板，menu_pick 处理重叠弹出的选择菜单，single_teleport 卡片只有「传送」。"""
        opts = opts or {}
        tpls = None
        if opts.get("icon"):
            img = _imread(str(ROOT / "resource" / "badges" / opts["icon"]))
            if img is None:
                print(f"  没有图标模板 resource/badges/{opts['icon']}")
                return False
            tpls = [(opts["icon"], img)]
        single = bool(opts.get("single_teleport"))
        last_wrong: tuple[int, int] | None = None
        fallback_used = False
        for i in range(CORE_MAX_TRIES):
            image = self.shot()
            cores = self.find_cores(image, tpls=tpls)
            if last_wrong:
                # 点错的那个图标被地图移到了画面中间附近：去掉离它最近的那个候选
                cores = [c for c in cores if abs(c[1] - last_wrong[0]) + abs(c[2] - last_wrong[1]) > 60]
            if not cores and not fallback_used:
                # 所有模板都没到 0.7：退一步，最高分 ≥ 0.6 的候选试一次
                low = self.find_cores(image, CORE_FALLBACK, tpls)[:1]
                if low:
                    fallback_used = True
                    cores = low
                    print(f"  没有 ≥ {CORE_THRESHOLD} 的基地图标，退一步试最高分的候选")
            if not cores:
                print(f"  第 {i + 1} 次：没找到基地图标（阈值 {CORE_THRESHOLD}，退一步 {CORE_FALLBACK}）")
                return False
            s, x, y, tpl = cores[0]
            print(f"  第 {i + 1} 次：点基地图标 ({x}, {y})，模板 {tpl}，匹配 {s:.2f}")
            self.click(x, y)
            time.sleep(1.5)
            ok, desc = self.card_is_core(single)
            if not ok and opts.get("menu_pick") and self.pick_menu(x, y, opts["menu_pick"]):
                ok, desc = self.card_is_core(single)  # 重叠设备弹出了选择菜单，点了目标那一行
            print(f"    {desc}")
            if ok:
                return True
            self.key("esc")  # 不是基地，关卡片（或选择菜单）重找
            time.sleep(1.0)
            again = self.find_cores(self.shot(), tpls=tpls)
            last_wrong = min(
                ((c[1], c[2]) for c in again), key=lambda p: abs(p[0] - 640) + abs(p[1] - 360), default=None
            )
        return False

    def wait_loaded(self) -> bool:
        """传送后等加载完（左上角出现「探索 / 工业」模式框）。还在加载画面（左上角「明日方舟」标志）就接着等，最多 LOAD_MAX 秒。"""
        start = time.time()
        time.sleep(LOAD_WAIT)
        end = time.time() + LOAD_EXTRA
        while time.time() < end:
            text = self.ocr_text(MODE_ROI)
            if re.search(r"探索|工业", text):
                return True
            if LOADING_RE.search(text):
                end = max(end, min(time.time() + 3.0, start + LOAD_MAX))
            time.sleep(1.5)
        print(f"  传送后 {time.time() - start:.0f} 秒还没加载完")
        return False

    # ---- 主流程 ----
    def goto_area(self, region: str, area: str | None, blueprint: str | None = None, probe: bool = False, force_teleport: bool = False) -> bool:
        """传送到 area 分区的基地。蓝图 areas.json 里这个分区写成对象时，按它的 drag / icon / menu_pick / single_teleport 处理。
        自动探测（总览里逐个分区找基地并写回 areas.json）只在 area 为 None / "?" 或 probe=True 时进行。
        已经在目标分区时不传送（角色通常就在基地旁；本分区地图上玩家箭头压着基地图标，模板也匹配不到），
        force_teleport=True 才强制走传送（进俯瞰失败后 full_patrol 用）。"""
        self.last_error, self.discovered = "", None
        opts = bp.load(blueprint).area_options(area) if area else {}
        probe = probe or area in (None, "?")
        print(f"== 传送到 {region} / {area or '?'} 的基地")
        for _ in range(3):  # 简报还开着（上一步 Esc 没关上）：按 M 没反应，先关简报（2026-11-12 实测）
            if self.scene() != "Scene_Brief":
                break
            print("  简报还开着，先按 Esc 关掉")
            self.key("esc")
            time.sleep(1.5)
        if self.scene() == "Scene_Overhead":  # 2026-11-09：在俯瞰里（快捷建造栏开着、CapsLock 没退出去）照样按 M、按固定位置点了一下地图
            return self.fail("还在俯瞰里：不按 M、不点地图（先退出俯瞰再传送）", esc=0)
        title = self.open_map()
        if title is None:
            return self.fail("第 1 步：按 M 后没读到地图标题（游戏可能开着别的界面，如角色/背包/菜单，请先回到大世界）", esc=1)
        print(f"  当前地图：{title[0]} / {title[1]}")
        found = None  # 自动探测时：（分区名, 卡片标题），卡片已打开
        if not probe and title[1] == area and not force_teleport:
            print("  已在目标分区，不传送（关地图直接找「进入俯瞰」）")
            self.key("esc")
            time.sleep(1.2)
            if self.map_open():
                self.key("esc")
                time.sleep(1.0)
            return True
        if not probe and title[1] == area:
            print("  已在目标分区，强制传送：跳过地区总览")
        else:
            boxes = self.open_overview(region, title[0])
            if boxes is None:
                return self.fail(f"第 2 步：不认识的地区「{region}」")
            if probe or not self.click_area(boxes, area):
                if area and any(t == area or area in t for _, t in boxes):
                    return self.fail(f"第 2 步：点了「{area}」但没进到这个分区")
                print(f"  地区总览里没有「{area or '?'}」，认到：{'、'.join(t for _, t in boxes)}")
                if not probe:
                    return self.fail(f"第 2 步：地区总览里没有「{area}」（要自动探测请传 area=None 或 probe=True）")
                found = self.discover(region, boxes)
                if found is None:
                    return self.fail("第 2 步（自动探测）：总览里的分区都没找到基地")
                self.discovered = found[0]
                print(f"{area or region} 的基地在 {found[0]}（{found[1]}）")
                save_discovery(blueprint, region, area, found[0])
        if found is None:
            if opts.get("drag"):  # 基地在默认视野外：先拖地图（滚轮对地图缩放无效）
                x1, y1, x2, y2, *ms = opts["drag"]
                print(f"  拖地图 ({x1}, {y1}) → ({x2}, {y2})")
                self.controller.post_swipe(x1, y1, x2, y2, ms[0] if ms else DRAG_MS).wait()
                time.sleep(1.5)
            if not self.click_core(opts):
                return self.fail("第 3/4 步：没找到基地（援建点 / 建设站 / 协议核心）")
        self.click(*(TP_BUTTON if opts.get("single_teleport") else TELEPORT_BUTTON))
        print("  已点「传送」，等加载")
        if not self.wait_loaded():
            return self.fail("第 5 步：传送后没读到左上角模式框（还在加载或传送没生效）", esc=0)
        print("  传送完成")
        return True

    def enter_overhead(self) -> bool:
        """走几步进协议区域，出现「进入俯瞰」提示后按 CapsLock。"""
        if self.scene() == "Scene_Overhead":
            print("已经在俯瞰。")
            return True
        if "探索" in self.ocr_text(MODE_ROI):
            self.key("tab")
            time.sleep(1.5)
            print("已从探索切到工业模式")
        for i, (k, sec) in enumerate([("", 0)] + MOVES):
            if k == "jump":
                self.jump_forward(sec)
                time.sleep(1.0)  # 等落地，「进入俯瞰」提示出来
            elif k:
                self.key(k, sec)
                time.sleep(1.0)
            s = self.scene()
            print(f"第 {i} 步 {k or '原地'} → {s}")
            if s == "Scene_World":
                self.key("capslock")
                time.sleep(2.5)
                if self.scene() == "Scene_Overhead":
                    print(f"已进入俯瞰。大写锁定：{'开' if caps_on() else '关'}")
                    return True
                self.last_error = "进俯瞰：按了 CapsLock 但没进俯瞰"
                print(self.last_error)
                return False
        self.last_error = "进俯瞰：走了几步都没看到「进入俯瞰」提示"
        print("走了几步都没看到「进入俯瞰」提示，请手动把角色走进协议区域。")
        return False

    def restore_caps(self):
        """之前在区域外按过 CapsLock、系统大写锁定被切到开启时，按一次复原。"""
        if caps_on():
            self.key("capslock")
            print(f"复原大写锁定 → {'开' if caps_on() else '关'}")


def parse_map_title(text: str) -> tuple[str, str] | None:
    """地图左上角标题「// 武陵 /景玉谷」→（地区, 分区）。第一级必须是已知地区（允许 OCR 漏字，如「陵」）。
    2026-11-10 灰度 0.10.1 实测：深入巡检开始时游戏停在角色「装备」页，同一位置的「洁尔佩塔/装备」被读成「祀/装备」，
    当成了地图标题（地区「祀」、分区「装备」），接着在大世界里按地图按钮的位置乱点了一通。"""
    words = re.findall(r"[一-鿿]+", text)
    if not words or not any(r in words[0] or words[0] in r for r in REGION_TABS):
        return None
    if len(words) >= 2:
        return words[0], words[1]
    return words[0], words[0]  # 四号谷地可能只有一级标题（未确认）


def same_area(title_name: str, area: str) -> bool:
    """地图标题里的分区名和总览里 OCR 到的名字是否同一个：总览字小常被截短（「试验园」/「试验园区」），按前缀算同名。"""
    return bool(title_name) and bool(area) and (title_name == area or title_name.startswith(area) or area.startswith(title_name))


def save_discovery(blueprint: str | None, region: str, requested: str | None, found: str):
    """把探测到的分区名写回蓝图档案 areas.json：替换占位名，并在 _discovered 里留记录。"""
    path = bp.BLUEPRINTS / (blueprint or bp.DEFAULT) / "areas.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        areas = data.setdefault(region, [])  # 元素可能是字符串或带特例配置的对象

        def name(a):
            return a["name"] if isinstance(a, dict) else a

        names = [name(a) for a in areas]
        if requested in names:
            k = names.index(requested)
            areas[k] = {**areas[k], "name": found} if isinstance(areas[k], dict) else found
        if found not in [name(a) for a in areas]:
            areas.append(found)
        data.setdefault("_discovered", {})[requested or region] = found
        path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        print(f"  已写回 {path}：{region} 的「{requested or '?'}」→「{found}」")
    except Exception as e:  # 写不进去不影响这次传送
        print(f"  警告：探测结果没写回 areas.json：{e}")


_nav: Navigator | None = None


def _get() -> Navigator:
    global _nav
    if _nav is None:
        _nav = Navigator()
    return _nav


def goto_area(region: str, area: str | None, blueprint: str | None = None, probe: bool = False, force_teleport: bool = False) -> bool:
    """传送到 region 地区 area 分区的基地（援建点 / 建设站 / 协议核心；特例分区按 areas.json 的配置）。
    area 为 None / "?" 或 probe=True 时自动探测；失败原因见 last_error()，探测到的分区见 discovered()。
    已在目标分区时不传送，force_teleport=True 强制传送。"""
    return _get().goto_area(region, area, blueprint, probe, force_teleport)


def last_error() -> str:
    return _nav.last_error if _nav else ""


def discovered() -> str | None:
    return _nav.discovered if _nav else None


def enter_overhead(restore_caps: bool = False) -> bool:
    nav = _get()
    if restore_caps:
        nav.restore_caps()
    return nav.enter_overhead()


def main(argv: list[str]) -> int:
    if "--overhead" in argv:
        return 0 if enter_overhead("--restore-caps" in argv) else 2
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 1
    if not goto_area(args[0], args[1]):
        return 2
    return 0 if enter_overhead() else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
