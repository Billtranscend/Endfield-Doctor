"""蓝图分析：游戏内操作（F1 共享蓝图页 + 蓝图预览）。

安全边界（docs/蓝图分析器-设计.md 第 6 节）：
  只点：蓝图卡片、「预览」(1232,155)、预览右上关闭 (1232,28)、预览画布里的暗管（显示/隐藏连接线，只读）。
  绝不点：放置蓝图、制造设备、导入蓝图、批量删除、分享 (1232,75)。悬停、拖动画布可以。
  每次点击前核对页面标题，对不上立刻停（抛 SafetyStop）。

「我的蓝图」页签（2026-11-12 游戏内核对）：(1232,155) 是「预览&编辑」，点开的是「编辑蓝图」页，不是「蓝图预览」：
  - 画布和预览页同一套（比例、拖动、滚动条位置、点暗管显示/隐藏连接线都一样）；
  - 右侧多了标签和描述栏，设备一览往下 50 px，底部是「返回 / 保存」，一屏只有 4 行；
  - 点到设备会弹「XX可选图标」小窗（改蓝图里显示的图标）：按 Esc 只关小窗；没改过东西时点右上关闭直接回列表，没有确认框。
  在这一页同样只拖动、点暗管、点右上关闭；绝不点：保存、返回、名字、标签、描述、可选图标小窗里的任何选项。
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path

import cv2
import numpy as np

from . import vision as V

PREVIEW_BTN = (1232, 155)
# 列表页顶部三个页签的文字区域（x0, x1）：当前页签是深色底（灰度约 30），其余是浅色底（约 185）
TABS = {"我的蓝图": (90, 215), "系统蓝图": (280, 405), "共享蓝图": (470, 600)}
TAB_Y = (14, 44)
TAB_DARK = 110
READABLE_TABS = ("共享蓝图", "我的蓝图")
BAD_TAB = "游戏现在在「{tab}」页签，读蓝图只认「共享蓝图」和「我的蓝图」。请切到蓝图所在的页签、让卡片显示在屏幕上，再点「读蓝图」"
# 点开后的页面：标题 → 模式。preview = 共享蓝图的「蓝图预览」；edit = 我的蓝图的「编辑蓝图」（只看不改）
MODES = {"蓝图预览": "preview", "编辑蓝图": "edit"}
# 各模式下设备一览的（首行格子 top, 列表可见区的顶, 底）和鼠标停放处（编辑页底部是「返回 / 保存」，停在按钮下面）
LIST_BOX = {"preview": (253, 240, 716), "edit": (303, 290, 628)}
PARKS = {"preview": (1110, 690), "edit": (1110, 702)}
VK_ESC = 0x1B
CLOSE_BTN = (1232, 28)
XS = [1005, 1075, 1145, 1215]  # 设备一览四列中心
# 悬停提示第二行的分类词，不是设备名（曾把 管道/管道桥/管道汇流器 都记成「物流」）
CATEGORY_WORDS = {"物流", "物流设备", "普通设备", "功能设备", "资源开采", "仓储存取", "基础生产", "合成制造", "电力", "战斗辅助", "种植"}


class SafetyStop(RuntimeError):
    """页面和预期不符：立刻停，不再点任何东西。"""


class Game:
    def __init__(self, log=print):
        import game
        import offline_ocr
        game.init_toolkit()
        self.win = game.find_game_window()
        if self.win is None:
            raise RuntimeError("没找到游戏窗口")
        self.ctrl = game.connect(self.win)
        self._move = game.move_cursor
        self.ocr = offline_ocr.shared()
        self.log = log

    mode = "preview"  # 当前打开的是哪种页面（open_preview 里按标题定）

    def close(self):
        """OCR 是进程内共用的，不关（OfflineOCR.close 会硬退出整个进程）。"""

    # ---- 基本动作
    def move(self, x, y):
        self._move(self.win.hwnd, int(x), int(y))

    def shot(self, park=True) -> np.ndarray:
        if park:
            self.move(*PARKS[self.mode])
            time.sleep(0.35)
        self.ctrl.post_screencap().wait()
        return self.ctrl.cached_image.copy()

    def text(self, img, roi) -> str:
        return "".join(t for _, t, _ in self.ocr.boxes(img, roi)).replace(" ", "")

    def _click(self, x, y, wait=1.0):
        self.ctrl.post_click(int(x), int(y)).wait()
        time.sleep(wait)

    def click_canvas(self, x, y):
        """只允许点在预览画布内。"""
        if not (V.V[0] + 25 <= x <= V.V[2] - 15 and V.V[1] + 15 <= y <= V.V[3] - 15):
            raise SafetyStop(f"点击 ({x:.0f},{y:.0f}) 超出预览画布")
        self._click(x, y, 0.9)
        self.move(*PARKS[self.mode])
        time.sleep(0.4)
        if self.mode == "edit":
            self.dismiss_icon_popup()

    def icon_popup(self, img) -> bool:
        """编辑蓝图页点到设备弹出的「XX可选图标」小窗：浅色标题栏（带「可选图标」）+ 一排图标格 + 下面一块实心深色面板。
        先找实心深色块（便宜），找到了再读它上方的标题确认。"""
        gray = cv2.cvtColor(img[V.V[1]:V.V[3], V.V[0]:V.V[2]], cv2.COLOR_BGR2GRAY)
        solid = cv2.erode((gray < 80).astype(np.uint8), np.ones((40, 40), np.uint8))
        if not solid.any():
            return False  # 画布上的管道、传送带都是细线，没有 40×40 的实心深色块
        ys, xs = np.where(solid)
        x0, y0 = int(xs.min()) + V.V[0], int(ys.min()) + V.V[1]
        return "可选图标" in self.text(img, [max(0, x0 - 30), max(0, y0 - 170), 400, 200])

    def dismiss_icon_popup(self) -> None:
        """点暗管时点偏到设备上会弹「可选图标」小窗：按 Esc 关掉（只关小窗，页面还在），不点里面的任何选项。"""
        img = self.shot(park=False)
        if not self.icon_popup(img):
            return
        self.log("  点到了设备，弹出「可选图标」小窗：按 Esc 关掉（没有选任何图标）")
        self.ctrl.post_key_down(VK_ESC).wait()
        time.sleep(0.08)
        self.ctrl.post_key_up(VK_ESC).wait()
        time.sleep(0.9)
        img = self.shot()
        if self.icon_popup(img) or self.viewer_mode(img) != "edit":
            raise SafetyStop("关「可选图标」小窗后页面不对，停下。游戏里如果还开着小窗或编辑蓝图页，请自己关掉，不要点保存")

    def drag(self, dx, dy):
        cx, cy = 470, 370
        self.ctrl.post_swipe(cx, cy, int(np.clip(cx + dx, 30, 920)), int(np.clip(cy + dy, 80, 660)), 1300).wait()
        time.sleep(1.0)

    # ---- 页面判断
    def on_list(self, img=None) -> bool:
        return "蓝图一览" in self.text(img if img is not None else self.shot(), [25, 55, 120, 30])

    def active_tab(self, img=None) -> str | None:
        """列表页当前的页签名（按页签底色认：深色的那个）；一个深色的都没有或不止一个时返回 None。"""
        img = img if img is not None else self.shot()
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        dark = [n for n, (x0, x1) in TABS.items() if gray[TAB_Y[0]:TAB_Y[1], x0:x1].mean() < TAB_DARK]
        return dark[0] if len(dark) == 1 else None

    def check_tab(self, img=None) -> str | None:
        """明确认出当前是读不了的页签（系统蓝图）时抛 SafetyStop（什么都不点）。认不出页签时不拦，交给后面的标题核对。"""
        tab = self.active_tab(img)
        if tab and tab not in READABLE_TABS:
            raise SafetyStop(BAD_TAB.format(tab=tab))
        return tab

    def viewer_mode(self, img=None) -> str | None:
        """点开蓝图后的页面：「蓝图预览」→ preview，「编辑蓝图」→ edit，都不是 → None。"""
        title = self.text(img if img is not None else self.shot(), [25, 12, 150, 32])
        return next((m for t, m in MODES.items() if t in title), None)

    def on_preview(self, img=None) -> bool:
        """在蓝图预览页，或「我的蓝图」的编辑蓝图页（画布同一套）。"""
        return self.viewer_mode(img) is not None

    def find_cards(self, img=None) -> dict[str, tuple[int, int]]:
        """列表页可见的卡片：名字 → 卡片中心（名字在卡片下方约 62 px）。"""
        img = img if img is not None else self.shot()
        out = {}
        for b, t, _ in self.ocr.boxes(img, [40, 95, 880, 540]):
            t = t.strip().replace(" ", "")
            if len(t) >= 2:
                out[t] = (b[0] + b[2] // 2, b[1] - 62)
        return out

    def open_preview(self, name: str) -> None:
        img = self.shot()
        if not self.on_list(img):
            raise SafetyStop("不在 F1 共享蓝图的列表页")
        tab = self.check_tab(img)
        cards = self.find_cards(img)
        if name not in cards:
            raise SafetyStop(f"列表里没看到卡片「{name}」（只认当前屏幕可见的卡片）")
        same = sum(1 for _, t, _ in self.ocr.boxes(img, [40, 95, 880, 540]) if t.strip().replace(" ", "") == name)
        if same > 1:
            raise SafetyStop(f"屏幕上有 {same} 张都叫「{name}」的卡片，分不清是哪张。请在游戏里把要读的那张改个不重复的名字，项目里也改成同样的名字")
        self._click(*cards[name], 1.2)
        title = self.text(self.shot(), [1050, 108, 200, 30])
        if name.replace("1.6", "") not in title and name not in title:
            raise SafetyStop(f"详情标题「{title}」和卡片「{name}」对不上")
        self._click(*PREVIEW_BTN, 2.0)
        after = self.shot()
        mode = self.viewer_mode(after)
        if mode is None:
            title = self.text(after, [25, 12, 200, 32])
            raise SafetyStop(f"没进到蓝图预览（现在的页面标题读到「{title}」）。游戏停在这个页面，请自己按右上角关闭退回列表")
        self.mode = mode
        if mode == "edit":
            # 2026-11-12 群友（蓝图作者）：蓝图在「我的蓝图」里，这个位置是「预览&编辑」，点开的是「编辑蓝图」页
            self.log(f"  这张在「{tab or '我的蓝图'}」里，打开的是「编辑蓝图」页：只拖动画布、点暗管看连接，不点保存，读完直接关闭")

    def close_preview(self) -> None:
        if not self.on_preview():
            return
        if self.mode == "edit":
            self.dismiss_icon_popup()
        self._click(*CLOSE_BTN, 1.5)
        self.mode = "preview"
        if not self.on_list():
            # 没改过东西时编辑蓝图页点关闭直接回列表（2026-11-12 核对）；回不去说明弹了别的东西，不再点
            raise SafetyStop("点右上角关闭后没回到蓝图列表（可能弹了确认框）。请自己在游戏里处理：不要保存，退回列表")

    # ---- 预览页：尺寸 + 设备一览
    def read_size(self, img) -> tuple[int, int] | None:
        t = self.text(img, [1050, 135, 150, 45])
        m = re.search(r"(\d{2})\D{1,2}(\d{2})", t)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def read_devices(self, img) -> dict[str, int | None]:
        """预览页右侧设备一览（网格：行距 70，未滚动时首行 top=253，一屏 6 行；编辑蓝图页首行 top=303，一屏 4 行，见 LIST_BOX）。
        超过 6 行要滚动：每次滚轮后用图像匹配求出准确的滚动量，只读新露出的行，直到列表不再动。
        认不出（或和已读的重名）的图标悬停读名字并补进图标库。"""
        lib = _load_icons()
        out: dict[str, int | None] = {}
        cur, shift, done = img, 0, 0  # done = 已读的行数（按未滚动时的行号）
        top0, box_top, box_bottom = LIST_BOX[self.mode]
        for _ in range(12 if self.mode == "edit" else 8):
            r = done
            while True:
                top = top0 + 70 * r - shift
                if top + 62 > box_bottom:
                    break
                if top >= box_top:
                    for cx in XS:
                        self._read_tile(cur, cx, top, lib, out)
                    done = r + 1
                r += 1
            # 再往下滚一点，看还有没有行
            self.move(1110, 500)
            time.sleep(0.2)
            _wheel(self.win.hwnd, 1110, 500, -240)
            time.sleep(0.7)
            nxt = self.shot()
            d = _scroll_delta(cur, nxt, box_top, box_bottom)
            if d is None:
                self.log("  设备一览滚动后对不上位置，只读了前面的行")
                break
            if d <= 0:
                break  # 到底了
            cur, shift = nxt, shift + d
        self.move(*PARKS[self.mode])
        return out

    def _read_tile(self, img, cx, top, lib, out) -> None:
        tile = img[top + 4:top + 62, cx - 30:cx + 30]
        if tile.size == 0 or cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY).mean() > 150:
            return  # 空格
        t = (cx, top)
        (n1, s1), (n2, s2) = _match(lib, _icon(img, t))
        txt = "".join(tx for _, tx, _ in self.ocr.boxes(_clean_digits(img, t), _digits_roi(t), only_rec=True))
        m = re.search(r"\d+", txt)
        cnt = int(m.group()) if m else None
        if n1 and s1 >= 0.80 and s1 - s2 >= 0.05 and n1 not in out:  # 重名 = 图标库可能标错，悬停核实
            out[n1] = cnt
            return
        self.move(cx, top + 25)
        time.sleep(0.6)
        im2 = self.shot(park=False)
        name = None
        # 悬停提示两行：上行设备名、下行分类（物流/生产…）。OCR 返回顺序不定，按 y 取最上面那行。
        # 提示一般在格子下方，靠底的格子会翻到上方。
        for roi in ([max(0, cx - 200), top + 15, 400, 80], [max(0, cx - 200), max(0, top - 75), 400, 80]):
            boxes = sorted(self.ocr.boxes(im2, roi), key=lambda z: z[0][1])
            names = [tx.strip() for _, tx, _ in boxes
                     if re.match(r"^[一-鿿]{2,8}$", tx.strip()) and tx.strip() not in CATEGORY_WORDS]
            if names:
                name = names[0]
                break
        if name and name not in out:
            out[name] = cnt
            if name != n1:
                V.wr(V.RES / "icons" / f"{name}_auto_{int(time.time())}_{cx}_{top}.png", _icon(img, t))
                self.log(f"  悬停认出新图标：{name} ×{cnt}")
        else:
            key = f"未识别@{cx},{top}"
            out[key] = cnt
            self.log(f"  设备一览有一格认不出（{key}，悬停读到「{name or ''}」，图标库最像 {n1} {s1:.2f}）")

    # ---- 预览页：采集
    def capture(self, out_dir: Path) -> list[Path]:
        """拖到左上角，再蛇形拖动截图（横 560、纵 240，慢拖减少惯性）。帧存 out_dir/frames。"""
        fdir = out_dir / "frames"
        fdir.mkdir(parents=True, exist_ok=True)
        prev = self.shot()
        for _ in range(8):  # 拖到左上角：滚动条滑块都到起点就停
            self.drag(420, 270)
            cur = self.shot()
            a, b = V.thumbs(prev), V.thumbs(cur)
            prev = cur
            if a == b:
                break
        frames = [prev]
        row_dir = -1
        for _ in range(8):
            for _ in range(5):
                before = V.thumbs(frames[-1])
                self.drag(row_dir * 560, 0)
                cur = self.shot()
                if V.thumbs(cur)[0] == before[0]:
                    break  # 横向到头
                frames.append(cur)
            before = V.thumbs(frames[-1])
            self.drag(0, -240)
            cur = self.shot()
            if V.thumbs(cur)[1] == before[1]:
                break  # 纵向到头
            frames.append(cur)
            row_dir = -row_dir
        paths = []
        for i, im in enumerate(frames):
            p = fdir / f"{i:02d}.png"
            V.wr(p, im)
            paths.append(p)
        return paths

    # ---- 预览页：暗管连接
    def _bring(self, mos, cal, mx, my):
        last = None
        for _ in range(7):
            img = self.shot()
            ox, oy, _ = V.locate(img, mos, cal)
            sx, sy = mx - ox, my - oy
            last = (sx, sy, ox, oy)
            if 60 <= sx <= 880 and 110 <= sy <= 630:
                return last
            self.drag(int(np.clip(470 - sx, -420, 420)), int(np.clip(370 - sy, -260, 260)))
        return last if last and 35 <= last[0] <= 915 and 85 <= last[1] <= 655 else None

    def probe_pipe(self, mos, cal, pipes, i, shot_dir: Path, tag: str):
        """单击第 i 个暗管、跟线、找伙伴，读完把线关掉。返回 (伙伴下标或 None, 说明)。"""
        a = pipes[i]
        b = self._bring(mos, cal, *a["p"])
        if b is None:
            return None, "拖不到画面里"
        sx, sy, ox, oy = b
        end = d = None
        for k in range(3):  # 单击是开关：没出线就再点
            self.click_canvas(sx, sy)
            img = self.shot(park=False)
            V.wr(shot_dir / f"{tag}_{i:02d}_{k}.jpg", img)
            end, d = V.ray_end(img, sx, sy)
            if end is not None and math.hypot(end[0] - sx, end[1] - sy) > 80:
                break
        else:
            return None, "点 3 次都没出线"
        cox, coy, steps = ox, oy, 0
        while V.at_edge(*end) and steps < 8:
            self.drag(int(-d[0] * 380), int(-d[1] * 280))
            steps += 1
            im3 = self.shot()
            cox, coy, _ = V.locate(im3, mos, cal)
            V.wr(shot_dir / f"{tag}_{i:02d}_f{steps}.jpg", im3)
            e2, _ = V.ray_end(im3, a["p"][0] - cox, a["p"][1] - coy, d)
            if e2 is None:
                break
            end = e2
        ex, ey = end[0] + cox, end[1] + coy
        others = [(j, c) for j, c in enumerate(pipes) if j != i]
        r = V.pick_partner(a["p"], (ex, ey), a["name"], [c for _, c in others])
        hx, hy = end
        if 60 <= hx <= 900 and 90 <= hy <= 650:  # 关线：点端点处，确认线没了
            for _ in range(2):
                self.click_canvas(hx, hy)
                if V.cmask(self.shot(park=False)).sum() < 150:
                    break
        if r is None:
            return None, f"端点 ({ex:.0f},{ey:.0f}) 线上没有类型相反的暗管，跟线 {steps} 次"
        return others[r[1]][0], f"沿线差 {r[2]:.0f} 偏离 {r[3]:.0f}，跟线 {steps} 次"

    def link_pipes(self, mos, frame0, pipes, shot_dir: Path, cancelled=lambda: False):
        """逐个单击暗管配对；跑完自检，没过的（未配/单向/矛盾）自动回点，最多 3 轮。"""
        cal = V.calib(frame0)
        shot_dir.mkdir(parents=True, exist_ok=True)
        part, note = {}, {}
        todo = list(range(len(pipes)))
        for rnd in range(3):
            for i in todo:
                if cancelled():
                    return part, note
                j, how = self.probe_pipe(mos, cal, pipes, i, shot_dir, f"r{rnd}")
                if j is not None:
                    part[i] = j
                note[i] = how
                self.log(f"    {pipes[i]['name']} → {pipes[j]['name'] if j is not None else '?'}（{how}）")
            st = V.link_status(len(pipes), part)
            bad = [i for i, s in st.items() if s != "双向"]
            todo = sorted(set(bad + [part[i] for i in bad if i in part]))
            self.log("  第 %d 轮后：%s" % (rnd + 1, "，".join(f"{k} {sum(1 for s in st.values() if s == k)}" for k in ("双向", "单向", "矛盾", "未配"))))
            if not todo:
                break
        return part, note


# ---- 设备一览：滚动
def _wheel(hwnd, x, y, delta):
    import game
    game.wheel(hwnd, x, y, delta)


def _scroll_delta(prev, nxt, box_top: int = 240, box_bottom: int = 716) -> int | None:
    """设备一览滚动了多少 px（内容上移为正）。取上一屏下部一条，在新屏里找。
    box_top / box_bottom：列表可见区（编辑蓝图页底部是按钮，可见区短，条子要取在按钮上面）。"""
    x0, x1, h = 975, 1250, 120
    y0 = min(520, box_bottom - h - 8)
    strip = prev[y0:y0 + h, x0:x1]
    reg = nxt[box_top:box_bottom, x0:x1]
    r = cv2.matchTemplate(reg, strip, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(r)
    if mx < 0.9:
        return None
    return y0 - (box_top + loc[1])


# ---- 设备一览的图标库
_lib = None


def _load_icons():
    global _lib
    if _lib is None:
        _lib = [(p.stem.split("_", 1)[0], V.rd(p)) for p in sorted((V.RES / "icons").glob("*.png"))]
    return _lib


def _icon(img, t):
    cx, top = t
    return img[top + 6:top + 42, cx - 28:cx + 28]


def _digits_roi(t):
    cx, top = t
    return [cx - 28, top + 44, 56, 21]


def _clean_digits(img, t):
    """只留数字区的白色笔画：图标彩色边缘会让「1」读成「21」。"""
    x, y, w, h = _digits_roi(t)
    out = np.zeros_like(img)
    hsv = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    m = ((hsv[..., 2] > 200) & (hsv[..., 1] < 60)).astype(np.uint8)
    # 数字字高 10–12 px；图标的亮边（如传送带的斜边）漏进来只有几 px 高，会被读成「7」
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    keep = np.zeros(n, bool)
    keep[1:] = digit_blobs([tuple(int(v) for v in st[i, :4]) for i in range(1, n)])
    out[y:y + h, x:x + w][keep[lab]] = 255
    return out


def digit_blobs(boxes: list[tuple[int, int, int, int]], min_h: int = 9, gap: int = 2) -> list[bool]:
    """数字区的白色连通块 (left, top, w, h) → 每块留不留。
    笔画只有 1–2 px 粗，细的地方亮度够不上会断：「5」的横和下半之间断开后，一块高 2、一块高 8，
    按单块高度 ≥ 9 判就两块都删了（2026-11-14 issue #1：应龙关上「仓库取货口 15」读成 1，放置检查多出 14 台；
    同一张蓝图前两次读到的「5」是连着的）。左右重叠、上下只隔 ≤ gap px 的块算同一个数字，按合起来的高度判。"""
    grp = list(range(len(boxes)))

    def find(i):
        while grp[i] != i:
            grp[i] = grp[grp[i]]
            i = grp[i]
        return i

    for i, (l1, t1, w1, h1) in enumerate(boxes):
        for j in range(i):
            l2, t2, w2, h2 = boxes[j]
            if min(l1 + w1, l2 + w2) > max(l1, l2) and max(t1, t2) - min(t1 + h1, t2 + h2) <= gap:
                grp[find(i)] = find(j)
    span = {}
    for i, (_, t, _, h) in enumerate(boxes):
        g = find(i)
        a, b = span.get(g, (t, t + h))
        span[g] = (min(a, t), max(b, t + h))
    return [span[find(i)][1] - span[find(i)][0] >= min_h for i in range(len(boxes))]


def _match(lib, ic):
    best = {}
    for name, tp in lib:
        if tp.shape != ic.shape:
            tp = cv2.resize(tp, (ic.shape[1], ic.shape[0]))
        v = float(cv2.matchTemplate(ic, tp, cv2.TM_CCOEFF_NORMED).max())
        best[name] = max(best.get(name, -1), v)
    s = sorted(best.items(), key=lambda z: -z[1])
    return (s[0] if s else (None, -1)), (s[1] if len(s) > 1 else (None, -1))
