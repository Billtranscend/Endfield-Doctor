"""连接游戏窗口的公共部分：找窗口、建控制器、截图前涂黑 UID。"""

import ctypes
import re
import threading
import time
from pathlib import Path

import cv2
from paths import imwrite as _imwrite  # cv2.imread/imwrite 不认中文路径

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
from maa.toolkit import Toolkit

from paths import ROOT  # noqa: E402,F401
DEBUG_DIR = ROOT / "debug"

# 截图里有玩家 UID 和昵称的位置，保存调试图前要涂黑（按 1280x720 坐标：x, y, w, h）：
# 大世界/俯瞰/简报在左下角；主菜单（Esc）的角色卡在左下偏上
UID_MASKS_720P = [(0, 690, 300, 30), (100, 560, 360, 110)]


def init_toolkit():
    DEBUG_DIR.mkdir(exist_ok=True)
    Toolkit.init_option(str(DEBUG_DIR / "maa"))


def find_game_window():
    for w in Toolkit.find_desktop_windows():
        if w.class_name == "UnityWndClass" and re.search("Endfield", w.window_name):
            return w
    return None


class WindowShapeError(RuntimeError):
    """游戏画面不是 16:9，或运行中窗口尺寸变了：所有坐标都是按 1280×720 量的，这时不能操作游戏。"""


ASPECT = 16 / 9
ASPECT_TOL = 0.01  # 1%：2560×1440 / 1920×1080 / 1280×720 都正好 16:9
# 游戏里可选的 16:9 分辨率（MaaEnd 的提示里列的也是这几档），窗口模式按屏幕大小从大到小挑
SIZES_16_9 = [(3840, 2160), (2560, 1440), (1920, 1080), (1600, 900), (1280, 720)]
FRAME_W, FRAME_H = 16, 80  # 窗口模式要给左右边框、标题栏 + 任务栏留的余量（像素）


class _DpiAware:
    """临时把本线程设成按每台显示器的实际像素算（屏幕和窗口尺寸放在同一个坐标系里比）。"""

    def __enter__(self):
        try:
            self.old = ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except (AttributeError, OSError):
            self.old = None
        return self

    def __exit__(self, *exc):
        if self.old:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(self.old))


class _RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong),
                ("szDevice", ctypes.c_wchar * 32)]


def client_size(hwnd: int) -> tuple[int, int]:
    with _DpiAware():
        rect = _RECT()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.r - rect.l, rect.b - rect.t


def monitor_of(hwnd: int) -> tuple[int, int, str] | None:
    """游戏窗口所在显示器的像素尺寸和设备名（GDI 设备名，形如 DISPLAY1 前面带反斜杠和点）。"""
    with _DpiAware():
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not mon or not user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(info)):
            return None
    r = info.rcMonitor
    return r.r - r.l, r.b - r.t, info.szDevice


def is_16_9(w: int, h: int) -> bool:
    return w > 0 and h > 0 and abs(w / h - ASPECT) <= ASPECT * ASPECT_TOL


def recommend(sw: int, sh: int) -> tuple[bool, tuple[int, int] | None]:
    """这块屏幕上能用的设置：（全屏/无边框能不能用, 窗口模式推荐的分辨率）。
    全屏/无边框的画面和屏幕一样大，屏幕本身是 16:9 才行；窗口模式要比屏幕小一圈，标题栏才不会把画面挤扁。"""
    win = next(((w, h) for w, h in SIZES_16_9 if w + FRAME_W <= sw and h + FRAME_H <= sh), None)
    return is_16_9(sw, sh), win


def aspect_problem(w: int, h: int, screen: tuple[int, int] | None = None) -> str | None:
    """画面不是 16:9 时返回给用户看的说明（给了屏幕尺寸就按这块屏幕给出具体改法）；最小化（0×0）时判断不了，返回 None。
    2026-11-11 灰度群友：窗口模式选 2560×1440，在 1440p 屏幕上标题栏把画面挤成 2560×1368，MaaFw 缩成 1347×720，
    简报列表整体右移约 33 px，物品名全落在名称列外，一行没读到，找列表顶部那步来回往上滚了 40 秒。"""
    if w <= 0 or h <= 0 or is_16_9(w, h):
        return None
    msg = f"游戏画面是 {w}×{h}，不是 16:9。程序的点击和识别位置都是按 16:9 画面量的，比例不对会读错、点错，所以先不操作游戏。"
    if screen and screen[0] > 0 and screen[1] > 0:
        sw, sh = screen
        full, win = recommend(sw, sh)
        ways = []
        if full:
            ways.append("游戏设置里改成「全屏」或「无边框窗口」")
        if win:
            ways.append(f"{'或者' if ways else ''}用窗口模式、分辨率选 {win[0]}×{win[1]}")
        head = f"你的屏幕是 {sw}×{sh}" + ("" if full else "（不是 16:9，全屏/无边框的画面比例也不对）")
        if ways:
            return msg + f"{head}：{'，'.join(ways)}。窗口模式别选和屏幕一样大的分辨率，标题栏会把画面挤扁。"
        return msg + f"{head}，放不下 16:9 的窗口。"
    return msg + "请在游戏设置里改成「全屏」或「无边框窗口」；用窗口模式的话，选一个比屏幕小的 16:9 分辨率（如 1920×1080），别让标题栏把画面挤扁"


def window_problem(hwnd: int) -> str | None:
    mon = monitor_of(hwnd)
    return aspect_problem(*client_size(hwnd), mon[:2] if mon else None)


def check_aspect(hwnd: int) -> None:
    msg = window_problem(hwnd)
    if msg:
        raise WindowShapeError(msg)


# ---- 取消：点了「取消」后下一次发输入/截图就停（2026-11-11 群友：只巡一个分区时点取消要等整个分区扫完）----
class Cancelled(BaseException):
    """用户点了取消。继承 BaseException：扫描里有不少 except Exception 的重试，不能被它们吞掉。"""


_STOP = threading.Event()
RELEASE = ("post_key_up", "post_touch_up")  # 松键不拦：拦了角色会一直按着 W 往前走


def request_stop() -> None:
    _STOP.set()


def clear_stop() -> None:
    _STOP.clear()


def check_stop() -> None:
    if _STOP.is_set():
        raise Cancelled("已取消")


# ---- 运行中窗口尺寸变了就停（学 OEA：「游戏窗口尺寸由 A 变为 B」）----
_SIZES: dict[int, tuple[int, int]] = {}  # hwnd → 连上时的客户区尺寸


def reset_sizes() -> None:
    """新任务开始：下一次发输入时按那时的窗口尺寸重新记（并重新查 16:9）。程序里的游戏连接是常驻的，
    用户中途把窗口化改成全屏后，不重记的话会一直报「尺寸变了」。"""
    _SIZES.clear()


def guard_size(hwnd: int) -> None:
    """每次发输入/截图前查一下：客户区尺寸和记下的不一样就抛 WindowShapeError。最小化（0×0）不算变。
    还没记过（刚 reset_sizes）时先查 16:9，再记。"""
    size = client_size(hwnd)
    if size[0] <= 0 or size[1] <= 0:
        return
    if hwnd not in _SIZES:
        check_aspect(hwnd)
    was = _SIZES.setdefault(hwnd, size)
    if size != was:
        raise WindowShapeError(f"游戏窗口尺寸在运行中由 {was[0]}×{was[1]} 变成了 {size[0]}×{size[1]}，已停止"
                               "（程序的位置都是按开始时的画面算的）。改好窗口后重新开始")


class GuardedController(Win32Controller):
    """发输入、截图前先查窗口尺寸有没有变。"""

    GUARDED = ("post_click", "post_swipe", "post_click_key", "post_key_down", "post_key_up", "post_scroll",
               "post_screencap", "post_touch_down", "post_touch_move", "post_touch_up", "post_input_text")

    def __init__(self, hwnd: int, **kw):
        super().__init__(hwnd, **kw)
        self._hwnd = hwnd

    @property
    def cached_image(self):
        return fit_720p(Win32Controller.cached_image.fget(self))


FIT_TOL = 16  # 宽差这么多像素以内按 1280×720 缩放（比例检查允许 1% 误差）


def fit_720p(img):
    """MaaFw 把截图短边缩到 720，长边按比例：1366×768 屏缩出来是 1281×720，和 1280×720 的参考图、坐标都差一点，
    phaseCorrelate 直接报尺寸不一致（2026-11-12 群友应龙关「src1.size == src2.size」）。差几个像素的统一缩成 1280×720。"""
    if img is None or img.ndim < 2:
        return img
    h, w = img.shape[:2]
    if (w, h) == (1280, 720) or h != 720 or abs(w - 1280) > FIT_TOL:
        return img
    return cv2.resize(img, (1280, 720), interpolation=cv2.INTER_AREA if w > 1280 else cv2.INTER_LINEAR)


def _guarded(name):
    base = getattr(Win32Controller, name)

    def method(self, *a, **k):
        if name not in RELEASE:
            check_stop()
        guard_size(self._hwnd)
        return base(self, *a, **k)

    method.__name__ = name
    return method


for _n in GuardedController.GUARDED:
    if hasattr(Win32Controller, _n):
        setattr(GuardedController, _n, _guarded(_n))


# ---- HDR（学 OEA：「游戏所在显示器已开启 HDR，请关闭后重试」）：截图颜色会变，管子颜色、标红数字都会认偏 ----
class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _PATH_INFO(ctypes.Structure):  # DISPLAYCONFIG_PATH_INFO（72 字节）
    _fields_ = [("srcAdapter", _LUID), ("srcId", ctypes.c_uint32), ("srcMode", ctypes.c_uint32), ("srcFlags", ctypes.c_uint32),
                ("tgtAdapter", _LUID), ("tgtId", ctypes.c_uint32), ("tgtMode", ctypes.c_uint32), ("tech", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32), ("refreshN", ctypes.c_uint32), ("refreshD", ctypes.c_uint32),
                ("scanline", ctypes.c_uint32), ("available", ctypes.c_int32), ("tgtFlags", ctypes.c_uint32), ("flags", ctypes.c_uint32)]


class _HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", _LUID), ("id", ctypes.c_uint32)]


class _SOURCE_NAME(ctypes.Structure):
    _fields_ = [("header", _HEADER), ("name", ctypes.c_wchar * 32)]


class _COLOR_INFO(ctypes.Structure):
    _fields_ = [("header", _HEADER), ("value", ctypes.c_uint32), ("encoding", ctypes.c_uint32), ("bits", ctypes.c_uint32)]


def hdr_on(hwnd: int) -> bool | None:
    """游戏窗口所在显示器开着 HDR 返回 True；查不到返回 None（只用来提醒，不拦）。"""
    try:
        mon = monitor_of(hwnd)
        if not mon:
            return None
        user32 = ctypes.windll.user32
        np_, nm = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(np_), ctypes.byref(nm)):  # QDC_ONLY_ACTIVE_PATHS
            return None
        paths = (_PATH_INFO * max(np_.value, 1))()
        modes = (ctypes.c_byte * (64 * max(nm.value, 1)))()  # DISPLAYCONFIG_MODE_INFO 64 字节，不用解析
        if user32.QueryDisplayConfig(2, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None):
            return None
        for p in paths[: np_.value]:
            src = _SOURCE_NAME()
            src.header = _HEADER(1, ctypes.sizeof(_SOURCE_NAME), p.srcAdapter, p.srcId)  # GET_SOURCE_NAME
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(src)) or src.name != mon[2]:
                continue
            col = _COLOR_INFO()
            col.header = _HEADER(9, ctypes.sizeof(_COLOR_INFO), p.tgtAdapter, p.tgtId)  # GET_ADVANCED_COLOR_INFO
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(col)):
                return None
            enabled, wide_enforced = bool(col.value & 2), bool(col.value & 4)
            return enabled and not wide_enforced
        return None
    except Exception:
        return None


HDR_HINT = "游戏所在的显示器开着 HDR：截图颜色会变，管子颜色、简报里标红的数字可能认偏。建议在 Windows 显示设置里关掉 HDR 再用"


def connect(window) -> Win32Controller | None:
    """连上游戏窗口。画面不是 16:9 时抛 WindowShapeError（不连、不操作）；之后每次发输入/截图前查窗口尺寸有没有变。"""
    check_aspect(window.hwnd)
    _SIZES[window.hwnd] = client_size(window.hwnd)
    if hdr_on(window.hwnd):
        print(f"注意：{HDR_HINT}")
    controller = GuardedController(
        window.hwnd,
        screencap_method=MaaWin32ScreencapMethodEnum.ScreenDC,
        mouse_method=MaaWin32InputMethodEnum.Seize,
        keyboard_method=MaaWin32InputMethodEnum.Seize,
    )
    if not controller.post_connection().wait().succeeded:
        return None
    return controller


def move_cursor(hwnd: int, x720: int, y720: int):
    """把系统光标移到游戏窗口内的 720p 坐标处（游戏实际分辨率可能不是 720p）。"""

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    check_stop()
    guard_size(hwnd)
    user32 = ctypes.windll.user32
    rect = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = POINT(int(x720 * rect.r / 1280), int(y720 * rect.b / 720))
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    user32.SetCursorPos(pt.x, pt.y)


def wheel(hwnd: int, x720: int, y720: int, delta: int):
    """在指定位置滚轮。MaaFw 的 post_scroll 在简报列表上没反应，所以用系统滚轮事件。

    delta 为负向下滚，每 120 为一格（简报列表一格约移动 140 像素）。
    """
    move_cursor(hwnd, x720, y720)
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)  # MOUSEEVENTF_WHEEL


def mask_uid(image):
    h, w = image.shape[:2]
    sx, sy = w / 1280, h / 720
    for x, y, mw, mh in UID_MASKS_720P:
        cv2.rectangle(
            image,
            (int(x * sx), int(y * sy)),
            (int((x + mw) * sx), int((y + mh) * sy)),
            (0, 0, 0),
            thickness=-1,
        )
    return image


def save_screenshot(controller: Win32Controller, name: str) -> Path:
    controller.post_screencap().wait()
    path = DEBUG_DIR / f"{name}.png"
    _imwrite(str(path), controller.cached_image.copy())  # UID 不敏感，不再打码
    return path
