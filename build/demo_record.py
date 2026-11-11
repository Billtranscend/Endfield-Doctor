"""录演示视频（第一步：录屏 + 自动点界面）。

找到「终末地基建诊断」窗口，把客户区调成 1280×781（和界面布局测量时一致），边录客户区边按脚本点按钮，
每一步的时间和字幕写进 events.json，交给 demo_render.py 加字幕、加速、压成 H.264。

权限：exe 是管理员进程，往它发鼠标事件的脚本也得用管理员运行（UIPI）；用源码起的窗口（python src\\app\\main.py）普通权限即可。
用法：python build\\demo_record.py --out <输出目录> [--debug-dir <exe 的 debug 目录>] [--dry] [--fps 10]
  --debug-dir  等快速诊断出报告时盯的目录（默认 dist\\EndfieldFactoryDoctor\\_internal\\debug；源码窗口用 debug）
  --dry        不点「开始快速诊断」（不碰游戏，只走界面，用来试脚本）
"""

import argparse
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import cv2
import numpy as np
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def grab_window(hwnd: int, w: int, h: int) -> np.ndarray:
    """PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT) 抓客户区：被别的窗口盖住也能抓到真实内容
    （屏幕截图抓的是屏幕，别的窗口压在上面时录到的就是那个窗口——2026-11-07 第一次正式录就废在这）。"""
    hdc = ctypes.windll.user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
    bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mdc, hbm)
    ctypes.windll.user32.PrintWindow(hwnd, mdc, 3)  # 1 = 只客户区, 2 = 渲染完整内容（WebView2/DirectComposition 需要）
    buf = ctypes.string_at(bits, w * h * 4)
    img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()  # BGRA → BGR
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(mdc)
    ctypes.windll.user32.ReleaseDC(hwnd, hdc)
    return img


ROOT = Path(__file__).resolve().parent.parent
user32 = ctypes.windll.user32
user32.SetProcessDPIAware()
TITLE = "终末地基建诊断"
CLIENT = (1280, 781)  # 界面布局测量时的客户区尺寸

# 客户区坐标（1280×781 下量的）
POS = {
    "nav_home": (114, 108), "nav_blueprint": (114, 153), "nav_quick": (114, 198), "nav_patrol": (114, 243), "nav_reports": (114, 288),
    "bp_new": (1186, 109), "bp_import_zip": (1010, 109), "wiz_close": (1199, 338), "wiz_name": (489, 419),
    "quick_start": (337, 220), "quick_result": (754, 433),
    "patrol_start": (337, 257),
    "report_row": [(754, 153), (754, 192), (754, 231), (754, 270)], "report_page": (754, 420), "report_tab_3": (535, 489),
}


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def find_window() -> int:
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        raise SystemExit(f"没找到窗口「{TITLE}」，先把程序打开")
    return hwnd


def client_rect(hwnd) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def window_rect(hwnd) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def place_window(hwnd, x: int, y: int) -> tuple[int, int, int, int]:
    """把窗口挪到 (x, y)，并把客户区调成 CLIENT 大小（外框尺寸按差值修正两次）。"""
    user32.ShowWindow(hwnd, 9)
    for _ in range(3):
        _, _, ww, wh = window_rect(hwnd)
        _, _, cw, ch = client_rect(hwnd)
        user32.SetWindowPos(hwnd, 0, x, y, ww + (CLIENT[0] - cw), wh + (CLIENT[1] - ch), 0x0040)
        time.sleep(0.3)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return client_rect(hwnd)


class Mouse:
    """不动真实光标：游戏在大世界里把光标锁在屏幕中心（ClipCursor 成一个点），SetCursorPos 会被钳回去，
    点击全落进游戏。改成把鼠标消息直接投给 WebView2 的渲染子窗口（Chrome_RenderWidgetHostHWND），坐标是客户区坐标。"""

    WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEWHEEL = 0x0200, 0x0201, 0x0202, 0x020A

    def __init__(self, hwnd: int, origin: tuple[int, int]):
        self.ox, self.oy = origin
        self.target = self._render_child(hwnd) or hwnd

    @staticmethod
    def _render_child(hwnd: int) -> int:
        found = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(h, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, buf, 256)
            if buf.value == "Chrome_RenderWidgetHostHWND":
                found.append(h)
            return True

        user32.EnumChildWindows(hwnd, proc(cb), 0)
        return found[0] if found else 0

    @staticmethod
    def _lp(x: int, y: int) -> int:
        return ((y & 0xFFFF) << 16) | (x & 0xFFFF)

    def move(self, p):
        user32.PostMessageW(self.target, self.WM_MOUSEMOVE, 0, self._lp(*p))

    def click(self, p, pause: float = 0.9):
        self.move(p)
        time.sleep(0.15)
        user32.PostMessageW(self.target, self.WM_LBUTTONDOWN, 0x0001, self._lp(*p))
        time.sleep(0.06)
        user32.PostMessageW(self.target, self.WM_LBUTTONUP, 0, self._lp(*p))
        time.sleep(pause)

    def wheel(self, p, ticks: int, pause: float = 0.35):
        self.move(p)
        time.sleep(0.15)
        sx, sy = self.ox + p[0], self.oy + p[1]  # WM_MOUSEWHEEL 的 lParam 是屏幕坐标
        for _ in range(abs(ticks)):
            delta = -120 if ticks > 0 else 120
            user32.PostMessageW(self.target, self.WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, self._lp(sx, sy))
            time.sleep(pause)

    @staticmethod
    def type_text(s: str):
        raise NotImplementedError("演示脚本不输入文字")


class Recorder(threading.Thread):
    def __init__(self, hwnd: int, bbox: tuple[int, int, int, int], path: Path, fps: int):
        super().__init__(daemon=True)
        x, y, w, h = bbox
        self.hwnd, self.w, self.h = hwnd, w, h
        self.bbox = (x, y, x + w, y + h)
        self.fps = fps
        self.writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        self.stop_flag = threading.Event()
        self.started = 0.0
        self.frames = 0

    def run(self):
        self.started = time.time()
        period = 1.0 / self.fps
        next_t = self.started
        while not self.stop_flag.is_set():
            self.writer.write(grab_window(self.hwnd, self.w, self.h))
            self.frames += 1
            next_t += period
            time.sleep(max(0.0, next_t - time.time()))
        self.writer.release()

    def stop(self):
        self.stop_flag.set()
        self.join()

    def now(self) -> float:
        return time.time() - self.started


def wait_new_report(debug_dir: Path, since: float, timeout: float = 300) -> Path | None:
    end = time.time() + timeout
    while time.time() < end:
        new = [p for p in debug_dir.glob("app_report_*.json") if p.stat().st_mtime > since]
        if new:
            return max(new, key=lambda p: p.stat().st_mtime)
        time.sleep(2)
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--debug-dir", default=str(ROOT / "dist" / "EndfieldFactoryDoctor" / "_internal" / "debug"))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--pos", default="-2400,120", help="把窗口挪到哪（屏幕坐标，默认副屏）")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "record.log", "w", encoding="utf-8")

    def say(*s):
        print(*s, file=log, flush=True)
        print(*s, flush=True)

    hwnd = find_window()
    px, py = (int(v) for v in a.pos.split(","))
    bbox = place_window(hwnd, px, py)
    say(f"窗口客户区 {bbox}")
    mouse = Mouse(hwnd, (bbox[0], bbox[1]))
    rec = Recorder(hwnd, bbox, out / "raw.mp4", a.fps)
    events: list[dict] = []

    def mark(text: str, speed: int = 1):
        events.append({"t": round(rec.now(), 2), "text": text, "speed": speed})
        say(f"[{rec.now():6.1f}s] {text}")

    rec.start()
    time.sleep(0.5)
    try:
        # 0 总览
        mouse.click(POS["nav_home"])
        mark("终末地基建诊断：在本机运行，只看游戏画面、按键盘翻界面，不改游戏任何文件")
        time.sleep(4)
        # 1 蓝图
        mouse.click(POS["nav_blueprint"])
        mark("① 蓝图档案：程序按它判断什么是正常。用成品蓝图的，拿作者导出的 zip 点「导入 zip」就行")
        time.sleep(4.5)
        mouse.click(POS["bp_new"], pause=1.2)
        mark("自己搭的蓝图点「新建档案」：起个名、选地区，程序自动读简报、找基地，再勾选确认一次就好")
        time.sleep(5)
        mouse.click(POS["wiz_close"])
        time.sleep(0.8)
        # 2 快速诊断
        mouse.click(POS["nav_quick"])
        mark("② 快速诊断：游戏停在大世界，点开始。程序自己按 F10 读简报、翻页、读完关掉，约 1.5 分钟")
        time.sleep(4)
        if not a.dry:
            since = time.time()
            mouse.click(POS["quick_start"])
            mark("正在读简报……（视频已加速）期间不要碰键鼠", speed=8)
            rep = wait_new_report(Path(a.debug_dir), since)
            say(f"报告：{rep}")
            time.sleep(5)
            mark("结果：先列缺矿 / 缺水口 / 缺气口（对照蓝图基准），再列简报层面的疑点；每条都有依据和建议")
            time.sleep(3)
            for _ in range(6):
                mouse.wheel(POS["quick_result"], 3)
                time.sleep(0.9)
            time.sleep(1.5)
        else:
            mark("（试录：跳过快速诊断）")
            time.sleep(2)
        # 3 报告
        mouse.click(POS["nav_reports"], pause=1.5)
        mark("③ 报告：每次诊断和巡检都存档，随时翻")
        time.sleep(3)
        mouse.click(POS["report_row"][1], pause=2.0)
        mark("深入巡检报告：按分区分页，默认打开有问题的分区；先看统计和产能自检")
        time.sleep(3.5)
        mark("往下：问题 / 待确认 / 正常 分组，每条有依据、建议，问题设备配红框定位图，照图去修")
        mouse.wheel(POS["report_page"], 4)
        time.sleep(4)
        mouse.wheel(POS["report_page"], 5)
        time.sleep(4)
        mouse.wheel(POS["report_page"], 5)
        time.sleep(3)
        # 4 深入巡检
        mouse.click(POS["nav_patrol"])
        mark("④ 深入巡检：自动传送到各分区，进俯瞰逐屏扫描、点面板核对。全基地约 35 分钟，期间别碰电脑")
        time.sleep(5)
        mark("建议：平时跑快速诊断；报了问题看不出在哪台、或大改之后，再跑一次深入巡检。报错就把「报告」里那份 json 发群里")
        time.sleep(5)
    finally:
        rec.stop()
        (out / "events.json").write_text(json.dumps({"fps": a.fps, "frames": rec.frames, "events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
        say(f"录完：{rec.frames} 帧，{out / 'raw.mp4'}")
    return 0


if __name__ == "__main__":
    if "--grab-test" in sys.argv:  # 抓一帧存 PNG，验证被盖住时也能抓到窗口内容
        h = find_window()
        _, _, w, hh = client_rect(h)
        cv2.imwrite(sys.argv[sys.argv.index("--grab-test") + 1], grab_window(h, w, hh))
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
