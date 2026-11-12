"""灰度包上手视频 + 端到端测试（2026-11-10）：按群友的顺序在打包版上走一遍，边走边录。

导入作者分析包（合并进现有档案）→ 快速诊断 → 深入巡检（只勾一个分区）→ 报告页看结果、点开定位图。
点界面用 demo_record 的办法（把鼠标消息直接投给 WebView2 子窗口，不动真实光标）；导入 zip 的文件对话框
直接往文件名框里写路径、按「打开」。录完交给 demo_render.py 加字幕。

用法（管理员终端，exe 要先开着）：
  python build\\gray_walkthrough.py --out <录制目录> --debug-dir <解压出来的 exe>\\_internal\\debug --zip <作者分析包> [--area 武陵城] [--skip-patrol]
坐标是 1280×781 客户区下量的（开发服务器同尺寸 getBoundingClientRect，2026-11-10 界面改版后）。
"""

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_record import Mouse, Recorder, find_window, place_window, wait_new_report  # noqa: E402

user32 = ctypes.windll.user32
POS = {
    "nav_home": (114, 104), "nav_blueprint": (114, 147), "nav_quick": (114, 233), "nav_patrol": (114, 276), "nav_reports": (114, 319), "nav_help": (114, 405), "help_game": (350, 109),
    "bp_import_zip": (1010, 109), "modal_merge": (792, 495),
    "quick_start": (337, 222), "quick_result": (754, 520),
    "area": {"景玉谷": (287, 172), "应龙关": (375, 172), "武陵城": (463, 172)}, "patrol_start": (337, 259),
    "ritem": [(401, 202), (401, 265), (401, 328)], "report_view": (907, 450),
    "quick_focus": (501, 269), "report_focus": (769, 91),  # 点一下拿焦点的空白处（提示文字 / 报告标题的时间），不会点到按钮
}


def page_down(mouse, focus, times: int, gap: float = 1.6):
    """往 WebView2 投滚轮消息不滚（2026-11-10 实测），改成先点页内空白处拿焦点、再投 PageDown。"""
    mouse.click(focus, pause=0.4)
    for _ in range(times):
        user32.PostMessageW(mouse.target, 0x0100, 0x22, 0)
        time.sleep(0.05)
        user32.PostMessageW(mouse.target, 0x0101, 0x22, 0)
        time.sleep(gap)


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _cls(h) -> str:
    b = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(h, b, 64)
    return b.value


def find_file_dialog(pid: int, timeout: float = 15) -> int:
    end = time.time() + timeout
    while time.time() < end:
        found = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(h, _):
            if _cls(h) == "#32770" and _pid(h) == pid and user32.IsWindowVisible(h):
                found.append(h)
            return True
        user32.EnumWindows(proc(cb), 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return 0


def fill_file_dialog(dlg: int, path: str) -> bool:
    """「打开」对话框：文件名框（ComboBoxEx32 id 0x47C 里的 Edit）写路径，再按 IDOK。"""
    combo = user32.GetDlgItem(dlg, 0x47C)
    edit = 0
    if combo:
        edit = user32.FindWindowExW(combo, 0, "ComboBox", None)
        edit = user32.FindWindowExW(edit, 0, "Edit", None) if edit else user32.FindWindowExW(combo, 0, "Edit", None)
    if not edit:
        edit = user32.GetDlgItem(dlg, 0x480)  # 老式对话框的文件名框
    if not edit:
        return False
    WM_SETTEXT, BM_CLICK, WM_COMMAND = 0x000C, 0x00F5, 0x0111
    user32.SendMessageW(edit, WM_SETTEXT, 0, ctypes.c_wchar_p(path))
    time.sleep(0.4)
    ok = user32.GetDlgItem(dlg, 1)
    if ok:
        user32.SendMessageW(ok, BM_CLICK, 0, 0)
    else:
        user32.PostMessageW(dlg, WM_COMMAND, 1, 0)
    return True


def wait_archive_merged(debug_dir: Path, timeout: float = 300) -> bool:
    """合并完成 = 包里那套档案出现了底账。"""
    led = debug_dir.parent / "data" / "blueprints" / "1.6皇帝产线" / "ledger" / "武陵城.json"
    end = time.time() + timeout
    while time.time() < end:
        if led.exists():
            return True
        time.sleep(1)
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--debug-dir", required=True)
    ap.add_argument("--zip", required=True)
    ap.add_argument("--area", default="武陵城")  # 「全部」= 三个分区都巡
    ap.add_argument("--skip-patrol", action="store_true")
    ap.add_argument("--patrol-only", action="store_true")  # 已导入、已跑过快速诊断时只跑深入巡检
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--pos", default="-2400,120")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # 输出重定向到文件时默认 GBK，✓ ✗ 写不进去
    # 文件对话框按它自己的当前目录解析相对路径：找不到文件会弹一个（主窗口最小化时看不见的）错误框，脚本卡在按「打开」那一步
    a.zip = str(Path(a.zip).resolve())
    if not Path(a.zip).is_file():
        raise SystemExit(f"没有这个文件：{a.zip}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    debug_dir = Path(a.debug_dir)
    log = open(out / "record.log", "w", encoding="utf-8")

    def say(*s):
        print(*s, file=log, flush=True)
        print(*s, flush=True)

    hwnd = find_window()
    pid = _pid(hwnd)
    px, py = (int(v) for v in a.pos.split(","))
    bbox = place_window(hwnd, px, py)
    say(f"窗口客户区 {bbox}，进程 {pid}")
    mouse = Mouse(hwnd, (bbox[0], bbox[1]))
    rec = Recorder(hwnd, bbox, out / "raw.mp4", a.fps)
    events: list[dict] = []
    result = {"merged": False, "quick_report": None, "patrol_report": None}

    def mark(text: str, speed: int = 1):
        events.append({"t": round(rec.now(), 2), "text": text, "speed": speed})
        say(f"[{rec.now():6.1f}s] {text}")

    rec.start()
    time.sleep(0.5)
    try:
        if not a.patrol_only:
          mouse.click(POS["nav_home"])
          mark("打开后先看「总览」：左下角显示连上了游戏、当前蓝图是「1.6皇帝产线」")
          time.sleep(4)
          mouse.move(POS["help_game"])
          mark("每个功能旁边都有「?」：鼠标放上去看一句话说明，点一下跳到「帮助」页对应的小节")
          time.sleep(4)
          mouse.move((700, 600))
          mouse.click(POS["nav_help"], pause=1.0)
          mark("左栏最下面的「帮助」：每个功能的详细说明、结论等级怎么看、常见问题")
          time.sleep(4)
          # 1 导入作者分析包
          mouse.click(POS["nav_blueprint"])
          mark("第一步：「蓝图」页 →「导入 zip」，选包里的「皇帝产线1.6-作者分析包.bpa.zip」")
          time.sleep(4)
          mouse.click(POS["bp_import_zip"], pause=0.5)
          dlg = find_file_dialog(pid)
          say(f"文件对话框 {dlg}")
          if not dlg or not fill_file_dialog(dlg, a.zip):
              raise RuntimeError("没找到文件对话框 / 文件名框")
          time.sleep(3)
          mark("选「合并进『1.6皇帝产线』」：补上底账、配方表、准入口限速，原有的错题本和参考简报都保留")
          time.sleep(3.5)
          mouse.click(POS["modal_merge"], pause=0.5)
          mark("合并中，约半分钟（已加速）", speed=6)
          result["merged"] = wait_archive_merged(debug_dir)
          say(f"合并完成：{result['merged']}")
          time.sleep(3)
          mark("合并好了：档案卡片上能看到三个据点")
          time.sleep(4)
          # 2 快速诊断
          mouse.click(POS["nav_quick"])
          mark("第二步：游戏停在大世界，点「开始快速诊断」。程序自己按 F10 开简报、翻页、读完关掉，约 2 分钟，期间别碰键鼠")
          time.sleep(5)
          since = time.time()
          mouse.click(POS["quick_start"])
          mark("正在读简报（已加速）", speed=8)
          rep = wait_new_report(debug_dir, since, timeout=420)
          result["quick_report"] = rep.name if rep else None
          say(f"快速诊断报告：{rep}")
          time.sleep(4)
          mark("先看「排查树」：全是 ✓ 就是正常。有 ✗ 的那步会写去哪个分区、看哪类设备，点「定向巡检这几处」直接去查")
          time.sleep(6)
          page_down(mouse, POS["quick_focus"], 3)
          mark("往下是缺矿 / 缺水口 / 缺气口（和蓝图基准比），以及简报层面的疑点，每条都有依据和建议")
          time.sleep(5)
        # 3 深入巡检（只勾一个分区）
        if not a.skip_patrol:
            mouse.click(POS["nav_patrol"])
            mark(f"第三步（可选）：「深入巡检」自动传送、进俯瞰逐屏扫、点开面板核对。这里只勾{a.area}演示，全基地约 30–40 分钟")
            time.sleep(4)
            for name, p in POS["area"].items():
                if a.area != "全部" and name != a.area:  # --area 全部：三个分区都巡
                    mouse.click(p, pause=0.4)
            time.sleep(2)
            since = time.time()
            mouse.click(POS["patrol_start"])
            mark("巡检中：程序控制游戏，只点开面板读数，不按开关、不收取、不换物品。别碰键鼠（已加速）", speed=40)
            rep = wait_new_report(debug_dir, since, timeout=4200 if a.area == "全部" else 2400)
            result["patrol_report"] = rep.name if rep else None
            say(f"巡检报告：{rep}")
            time.sleep(4)
            mark("巡检完会读一次简报，把扫到的设备状态和简报合起来下结论")
            time.sleep(4)
        # 4 报告
        mouse.click(POS["nav_reports"], pause=1.5)
        mark("「报告」页：左边是历史列表，右边是详情。红字是问题数，黄字是待确认数")
        time.sleep(4)
        mouse.click(POS["ritem"][0], pause=2.0)
        mark("详情：问题 / 待确认 / 正常分组，每条有依据和建议，有问题的设备配定位图；点缩略图看大图")
        time.sleep(3)
        page_down(mouse, POS["report_focus"], 4, gap=2.5)
        mark("遇到看不懂或判断错的：「设置」→「打开数据目录」，把最新的 app_report_*.json 发到群里")
        time.sleep(5)
    finally:
        rec.stop()
        (out / "events.json").write_text(json.dumps({"fps": a.fps, "frames": rec.frames, "events": events, "result": result},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
        say(f"录完：{rec.frames} 帧，{out / 'raw.mp4'}；{result}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
