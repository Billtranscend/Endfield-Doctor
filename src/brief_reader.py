"""P1：简报读取器。

遍历简报里的每个地区，滚动读完整个列表，导出每种物品的
库存、当前生产/消耗、理论生产/消耗（以及理论消耗是否标红）。
同名的灌装瓶、耐压罐用图标里的液体/气体小图标区分。

只在简报界面里点击下拉框和滚动，不碰其他按钮。
用法：.venv\\Scripts\\python.exe src\\brief_reader.py [地区 ...] [--diagnose]
加 --diagnose 时读完直接跑诊断（src/diagnose.py）。
"""

import difflib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
from paths import imwrite as _imwrite  # cv2.imread/imwrite 不认中文路径
from maa.resource import Resource
from maa.tasker import Tasker

import fluid_icon
from game import DEBUG_DIR, ROOT, check_stop, connect, find_game_window, init_toolkit, mask_uid, wheel

# ---- 720p 下的界面布局 ----
LIST_ROI = [240, 95, 870, 545]  # 列表可见区域
LIST_CENTER = (600, 360)  # 滚轮位置
NAME_X = (295, 412)  # 名称、本地仓储所在列
CUR_X = (850, 940)  # 当前数据列
THEO_X = (1020, 1105)  # 理论数据列
ICON_X = (410, 466)  # 物品图标
REGION_DROPDOWN = (826, 661)
REGION_MENU_ROI = [730, 530, 200, 120]

PAGE_TICKS = 3  # 一页滚 3 格，约 6 行，和上一页重叠约 2 行（游戏把一次滚轮事件只算一格）

NUM_RE = re.compile(r"(\d+)\s*/\s*分")
STOCK_RE = re.compile(r"本地仓储\s*(\d+)?")

VOCAB: dict[str, list[str]] = json.loads((ROOT / "data" / "item_names_cn.json").read_text(encoding="utf-8"))
VARIANT_NAMES = {n for n, ids in VOCAB.items() if any(i.startswith(("item_fbottle_", "item_gasjar_")) for i in ids)}
LIQUIDS = {p.stem for p in fluid_icon.ICON_DIR.glob("item_liquid_*.png")}
GASES = {p.stem for p in fluid_icon.ICON_DIR.glob("item_gas_*.png")}


@dataclass
class Row:
    name: str
    fluid: str | None  # 灌装物 id（item_liquid_* / item_gas_*），空瓶或非灌装物为 None
    stock: int | None  # 流体没有本地仓储，为 None
    cur_prod: int | None
    cur_cons: int | None
    theo_prod: int | None
    theo_cons: int | None
    theo_cons_red: bool
    ocr_name: str  # OCR 原文，便于核对纠错
    fluid_guess: str | None = None  # 最像的液体（判为空瓶时也保留，便于校准）
    fluid_score: float | None = None
    fluid_contrast: float | None = None


class BriefReader:
    def __init__(self):
        init_toolkit()
        window = find_game_window()
        if window is None:
            raise SystemExit("没找到游戏窗口。")
        self.hwnd = window.hwnd
        self.controller = connect(window)
        if self.controller is None:
            raise SystemExit("连接游戏窗口失败。")
        resource = Resource()
        resource.post_bundle(str(ROOT / "resource")).wait()
        self.tasker = Tasker()
        self.tasker.bind(resource, self.controller)

    # ---- 底层：截图 + OCR ----
    def ocr(self, roi):
        node = {"Reader_OCR": {"recognition": "OCR", "roi": roi, "action": "DoNothing", "timeout": 1, "rate_limit": 0}}
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("Reader_OCR", node).wait().get()
        reco = detail.nodes[0].recognition
        image = self.controller.cached_image
        return [(r.box, r.text.strip(), r.score) for r in reco.all_results], image

    def read_stock(self, sbox) -> int | None:
        """对库存那一行单独做纯识别（不做文字检测）。流体本来就没有库存，读不到数字返回 None。"""
        roi = [sbox[0] - 2, sbox[1] - 3, ICON_X[0] - sbox[0], sbox[3] + 6]
        node = {"Reader_Stock": {"recognition": "OCR", "roi": roi, "only_rec": True, "action": "DoNothing", "timeout": 1, "rate_limit": 0}}
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("Reader_Stock", node).wait().get()
        reco = detail.nodes[0].recognition if detail and detail.nodes else None
        text = "".join(r.text for r in reco.all_results) if reco else ""
        m = STOCK_RE.search(text.replace(" ", ""))
        return int(m.group(1)) if m and m.group(1) else None

    def is_brief(self) -> bool:
        check_stop()  # 识别也走截图：点了取消就别再等
        detail = self.tasker.post_task("P0_DetectScene").wait().get()
        return bool(detail) and any(n.name == "Scene_Brief" for n in detail.nodes)

    # ---- 解析一屏 ----
    def parse_page(self, results, image) -> list[Row]:
        def in_col(box, col):
            cx = box[0] + box[2] / 2
            return col[0] <= cx <= col[1]

        def cy(box):
            return box[1] + box[3] / 2

        names = sorted(  # 必须按从上到下排序，翻页拼接靠行顺序
            (
                (box, text)
                for box, text, _ in results
                if in_col(box, NAME_X) and not text.startswith("本地") and re.search(r"[一-鿿]", text)
            ),
            key=lambda bt: bt[0][1],
        )
        roi_top, roi_bottom = LIST_ROI[1], LIST_ROI[1] + LIST_ROI[3]
        rows = []
        for nbox, ntext in names:
            top = cy(nbox)
            if top - 16 < roi_top or top + 42 > roi_bottom:
                continue  # 行碰到列表上下边缘，可能被裁掉一部分，下一页再读
            band = [(b, t) for b, t, _ in results if top - 8 <= cy(b) <= top + 30]
            stock_box = [(b, t) for b, t in band if t.startswith("本地") and in_col(b, NAME_X)]
            cur = sorted([(b, t) for b, t in band if in_col(b, CUR_X) and NUM_RE.search(t)], key=lambda bt: bt[0][1])
            theo = sorted([(b, t) for b, t in band if in_col(b, THEO_X) and NUM_RE.search(t)], key=lambda bt: bt[0][1])
            if not stock_box or len(cur) != 2 or len(theo) != 2:
                continue  # 被列表上下边缘裁掉的半行，下一页再读

            name = correct_name(ntext)
            if name is None:
                continue
            # 库存为 0 时那个「0」是红色的，OCR 常把它切成单独的框，所以把同一行右侧的文字拼起来再读
            sbox = stock_box[0][0]
            same_line = sorted(
                (b, t) for b, t in band if abs(cy(b) - cy(sbox)) <= 6 and sbox[0] <= b[0] < ICON_X[0]
            )
            m = STOCK_RE.search("".join(t for _, t in same_line).replace(" ", ""))
            stock = int(m.group(1)) if m and m.group(1) else None
            if stock is None:  # 只裁库存那一小块，单独再认一次
                stock = self.read_stock(sbox)

            fluid, match = None, None
            if name in VARIANT_NAMES:
                y = int(top)
                icon = image[max(0, y - 14) : y + 40, ICON_X[0] : ICON_X[1]]
                fluid, match = fluid_icon.detect(icon, GASES if "罐" in name else LIQUIDS)

            rows.append(
                Row(
                    name=name,
                    fluid=fluid,
                    stock=stock,
                    cur_prod=num(cur[0][1]),
                    cur_cons=num(cur[1][1]),
                    theo_prod=num(theo[0][1]),
                    theo_cons=num(theo[1][1]),
                    theo_cons_red=is_red(image, theo[1][0]),
                    ocr_name=ntext,
                    fluid_guess=match.fluid if match else None,
                    fluid_score=round(match.score, 1) if match else None,
                    fluid_contrast=round(match.contrast, 1) if match else None,
                )
            )
        return rows

    # ---- 读一个地区 ----
    def read_region(self, max_pages: int = 40) -> list[Row]:
        if not self.scroll_to_top():
            print("  列表里一个物品名都没认出来（列位置对不上），不往下翻了")
            return []

        rows: list[Row] = []
        stale = 0
        for page_no in range(max_pages):
            results, image = self.ocr(LIST_ROI)
            page = self.parse_page(results, image)
            before = len(rows)
            new = merge_page(rows, page)
            if page_no > 0 and page and new == len(page) and before:
                print(f"  警告：第 {page_no + 1} 页和上一页没有重叠，中间可能漏行（从「{page[0].name}」开始）")
            stale = stale + 1 if new == 0 else 0
            if stale >= 2:  # 连续两页没有新行：到底了
                break
            self.scroll(-PAGE_TICKS)
            time.sleep(0.6)
        return rows

    def scroll(self, ticks: int):
        """按格滚动：负数向下。游戏把一次滚轮事件只算一格，所以逐格发送。"""
        for _ in range(abs(ticks)):
            wheel(self.hwnd, *LIST_CENTER, 120 if ticks > 0 else -120)
            time.sleep(0.04)

    def scroll_to_top(self) -> bool:
        """反复向上滚，直到屏幕上的物品名不再变化。连续 3 次一个物品名都没认出来就放弃（返回 False）：
        2026-11-11 群友画面不是 16:9，一行都对不上列，原来要白滚 20 轮（约 40 秒）。"""
        last, blank = None, 0
        for _ in range(20):
            self.scroll(10)
            time.sleep(0.5)
            results, image = self.ocr(LIST_ROI)
            names = [r.name for r in self.parse_page(results, image)]
            if names and names == last:
                return True
            blank = 0 if names else blank + 1
            if blank >= 3:
                return False
            last = names
        return bool(last)

    def save_page(self, tag: str) -> Path:
        """读不出行时存一张当前简报截图（UID 涂黑），方便看是哪里对不上。"""
        self.controller.post_screencap().wait()
        img = mask_uid(self.controller.cached_image.copy())
        path = DEBUG_DIR / f"brief_empty_{tag}_{time.strftime('%Y%m%d-%H%M%S')}.png"
        _imwrite(str(path), img)
        return path

    # ---- 地区下拉 ----
    def _menu_items(self):
        results, _ = self.ocr(REGION_MENU_ROI)
        # 菜单区域和列表的「生产 / 消耗」标签重叠，要排除
        return [
            (t, (b[0] + b[2] // 2, b[1] + b[3] // 2))
            for b, t, _ in results
            if re.fullmatch(r"[一-鿿]{2,6}", t) and t not in ("生产", "消耗")
        ]

    def region_options(self) -> list[tuple[str, tuple[int, int]]]:
        """打开地区下拉（已打开就不再点），返回各选项文字和位置。"""
        opts = self._menu_items()
        if not opts:
            self.controller.post_click(*REGION_DROPDOWN).wait()
            time.sleep(0.8)
            opts = self._menu_items()
        return opts

    def select_region(self, name: str) -> bool:
        for text, pos in self.region_options():
            if text == name:
                self.controller.post_click(*pos).wait()
                time.sleep(1.0)
                return True
        self.controller.post_click(*REGION_DROPDOWN).wait()  # 收起下拉
        time.sleep(0.5)
        return False


def same_row(a: Row, b: Row) -> bool:
    """两页里看到的是不是同一行。

    只比名称、液体和库存：生产/消耗（包括理论值）在两次截图之间会实时变化，库存也会小幅变动。
    """
    if (a.name, a.fluid, a.fluid_guess) != (b.name, b.fluid, b.fluid_guess):
        return False
    if a.stock is None or b.stock is None:
        return True  # 某一页没读出库存数字（红色的 0 偶尔漏检），不据此判为不同行
    if abs(a.stock - b.stock) <= max(50, 0.01 * max(a.stock, b.stock)):
        return True
    return digit_slip(a.stock, b.stock)


def digit_slip(x: int, y: int) -> bool:
    """x、y 是不是同一个数多/少了末尾一位（OCR 把名称后面的竖线分隔条认成了「1」拼在库存后面）。
    2026-11-05 武陵简报：砂叶一页读成 899971、下一页 89997，两页对不上，后半页被重复追加，仓库上限被算成 900000。"""
    big, small = max(x, y), min(x, y)
    return small > 0 and big // 10 == small


def merge_page(rows: list[Row], page: list[Row]) -> int:
    """按顺序把新一页接到已读结果后面：找已读结尾和新页开头最长的重叠，只追加重叠之后的行。

    不按名称去重，因为同名灌装瓶可能被认成同一种液体，按名称去重会丢行。
    重叠部分如果旧行没读出库存、新页读出了，就回填。返回追加的行数。
    """
    for k in range(min(len(rows), len(page)), 0, -1):
        if all(same_row(rows[len(rows) - k + i], page[i]) for i in range(k)):
            for i in range(k):
                old = rows[len(rows) - k + i]
                if old.stock is None and page[i].stock is not None:
                    old.stock = page[i].stock
                elif old.stock is not None and page[i].stock is not None and digit_slip(old.stock, page[i].stock):
                    old.stock = min(old.stock, page[i].stock)  # 多了一位的是错的
            rows.extend(page[k:])
            return len(page) - k
    rows.extend(page)
    return len(page)


def num(text: str) -> int | None:
    m = NUM_RE.search(text.replace(" ", ""))
    return int(m.group(1)) if m else None


def is_red(image, box) -> bool:
    x, y, w, h = box
    patch = image[y : y + h, x : x + w].astype(int)
    b, g, r = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    return bool(((r > 170) & (g < 90) & (b < 90)).mean() > 0.05)


def correct_name(text: str) -> str | None:
    """OCR 结果纠错：只接受物品词表里的名字。"""
    text = text.replace(" ", "")
    if text in VOCAB:
        return text
    hit = difflib.get_close_matches(text, VOCAB.keys(), n=1, cutoff=0.6)
    return hit[0] if hit else None


def main(argv: list[str]) -> int:
    diagnose_after = "--diagnose" in argv
    argv = [a for a in argv if a != "--diagnose"]
    reader = BriefReader()
    if not reader.is_brief():
        print("当前不在简报界面，请先按 F10 打开简报。")
        return 2

    regions = argv
    if not regions:
        regions = [t for t, _ in reader.region_options()]
        reader.controller.post_click(*REGION_DROPDOWN).wait()  # 收起下拉
        time.sleep(0.5)
    print(f"地区：{regions}")

    out = {}
    for region in regions:
        if not reader.select_region(region):
            print(f"没找到地区「{region}」，跳过。")
            continue
        rows = reader.read_region()
        out[region] = [asdict(r) for r in rows]
        print(f"\n== {region}：{len(rows)} 行")
        for r in rows:
            fluid = f"({r.fluid.split('_', 2)[2]})" if r.fluid else ""
            red = " 红" if r.theo_cons_red else ""
            if r.fluid_guess:
                red += f"   [像 {r.fluid_guess.split('_', 2)[2]} 差异 {r.fluid_score} 反差 {r.fluid_contrast}]"
            print(
                f"{r.name}{fluid:<16} 库存 {r.stock if r.stock is not None else '-':>6} "
                f"当前 {r.cur_prod}/{r.cur_cons}  理论 {r.theo_prod}/{r.theo_cons}{red}"
            )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = DEBUG_DIR / f"brief_{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已保存：{path}")
    # 进出俯瞰按 CapsLock 的次数偶尔不对称（某次按键被游戏吞掉），系统大写锁定会被留在「开」。
    # 简报界面里按 CapsLock 只切系统状态、不会切俯瞰（2026-11-06 实测），读完简报顺手复原
    import navigate

    if navigate.caps_on():
        navigate._get().key("capslock")
        print("系统大写锁定是开的，在简报界面按了一次 CapsLock 复原为关")
    if diagnose_after:
        import diagnose

        print()
        diagnose.main([str(path)])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
