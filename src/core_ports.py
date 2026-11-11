"""读协议核心 / 次级核心的 6 个取货口（只读，任务计划第 12 项）。

2026-11-10 用户：协议核心有 6 个出货口，每个可以设置从本地仓库往外出一种物品；俯瞰看不出设的是什么，要点开看。
IndustrialPlanner（registry/entity-definition.ts sp_hub_1 / sp_sub_hub_1）：核心 9×9，取货口在东西两侧
各 3 个（第 2、5、8 格），南北两侧是 7+7 个进货口；每个取货口等价一个独立的仓库取货口（一条带子 30/分）。
游戏里（2026-11-10 实测三个基地）：
- 俯瞰默认缩放下，核心标签（图标 + 「协议核心」/「次级核心」）的中心就是核心中心；
  6 个金色三角在中心 ±132 像素、上下 0 / ±102 像素（格宽约 33 像素，三角在第 4 格边上）。
- 点三角开「协议核心取货口N」面板：左边「当前取货」是本地仓库存量（没设是「--」），
  中间物品格下面的按钮没设是「添加物品」、设了是「替换物品」；鼠标悬停物品格出物品名提示。
- 核心转过方向，编号也跟着转（应龙关次级核心转了 180°：左边从上到下 6/5/4、右边 3/2/1），所以编号按面板标题认。
- 次级核心四角有圆柱压着上下两个三角，点圆柱的位置也能打开取货口面板。

操作边界：只点三角（打开面板）和面板右上角关闭 X；误开了核心全屏面板只点它的关闭。
绝不点：添加物品 / 替换物品、物品格、面板里任何别的东西。物品名靠鼠标悬停读。不按 Z；挪镜头前确认还在俯瞰。
带子有没有接在三角上，面板上看不出来（没接带子的取货口也显示「运行中」），这里不判，存三角外侧的截图供核对。
"""
from __future__ import annotations

import json
import re
import time

import cv2
from paths import imread as _imread  # cv2.imread/imwrite 不认中文路径
import numpy as np

import near_patrol as NP
from game import DEBUG_DIR, ROOT, move_cursor

CORE_NAMES = ("协议核心", "次级核心")
LABEL_DX = -21          # 标签文字中心 → 核心中心（文字左边还有个图标）
PORT_DX = 132           # 三角离核心中心的横向距离
PORT_DY = (-102, 0, 102)
CELL = 33
CENTER = np.array([640.0, 330.0])
TITLE_ROI = [180, 125, 600, 40]
STOCK_ROI = [495, 350, 200, 70]        # 「当前取货」右边的存量（没设是「--」）
BUTTON_ROI = [750, 430, 130, 38]       # 「添加物品」/「替换物品」
STATUS_ROI = [500, 566, 320, 32]
SLOT = (808, 378)                      # 物品格中心：悬停出物品名
TIP_ROI = [800, 378, 340, 70]
CLOSE = (1114, 140)
TITLE_RE = re.compile(r"取货口\s*([1-6])")


def find_core(boxes) -> tuple[str, np.ndarray] | None:
    """整屏 OCR 里找核心标签，返回 (名字, 核心中心)。"""
    for b, t in boxes:
        name = next((n for n in CORE_NAMES if n in t.replace(" ", "")), None)
        if name:
            return name, np.array([b[0] + b[2] / 2 + LABEL_DX, b[1] + b[3] / 2])
    return None


def port_points(center) -> list[tuple[int, int, int]]:
    """(x, y, 侧 -1 左 / 1 右)。"""
    cx, cy = center
    return [(int(round(cx + sx * PORT_DX)), int(round(cy + dy)), sx) for sx in (-1, 1) for dy in PORT_DY]


_TPL: dict = {}
SNAP_R = 28
SNAP_MIN = 0.7  # 协议核心的三角模板（resource/badges/core_port_l/r.png，武陵城截的）；次级核心的三角样子不一样、分数低，不吸附


def snap(img, x: int, y: int, side: int) -> tuple[int, int, float]:
    """预测位置附近找金色三角（朝外：左边 ◁、右边 ▷），分数够才吸过去。"""
    key = "l" if side < 0 else "r"
    if key not in _TPL:
        _TPL[key] = _imread(str(ROOT / "resource" / "badges" / f"core_port_{key}.png"))
    t = _TPL[key]
    if t is None:
        return x, y, 0.0
    h, w = t.shape[:2]
    x0, y0 = max(0, x - SNAP_R - w // 2), max(0, y - SNAP_R - h // 2)
    win = img[y0:y + SNAP_R + h // 2 + 1, x0:x + SNAP_R + w // 2 + 1]
    if win.shape[0] < h or win.shape[1] < w:
        return x, y, 0.0
    r = cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED)
    _, mx, _, (bx, by) = cv2.minMaxLoc(r)
    if mx < SNAP_MIN:
        return x, y, float(mx)
    return x0 + bx + w // 2, y0 + by + h // 2, float(mx)


def _settled_core(p, tries: int = 4):
    """镜头停稳后找核心：连续两帧核心位置差 ≤ 3 像素才算（挪完镜头还在滑，2026-11-10 武陵城左边三个点空了）。"""
    last = None
    for _ in range(tries):
        p.shot()
        hit = find_core(NP._ocr_all(p))
        if hit and last and np.abs(hit[1] - last[1]).max() <= 3:
            return hit
        last = hit
        time.sleep(0.6)
    return last


def _num(text: str) -> int | None:
    m = re.search(r"\d+", text.replace(",", ""))
    return int(m.group()) if m else None


def _stock(boxes) -> int | None:
    """存量是左边那串大数字；右边的物品插图上也有字（中容武陵电池上的「02」会被读成数字粘在后面），只取最左边那个带数字的框。"""
    digits = [(b, t) for b, t in boxes if re.search(r"\d", t)]
    return _num(min(digits, key=lambda bt: bt[0][0])[1]) if digits else None


def _item_name(boxes) -> str:
    """悬停提示：第一行物品名、第二行类别（材料…）。认得的物品名优先。"""
    from diagnose import ID_NAME
    known = set(ID_NAME.values())
    lines = sorted(boxes, key=lambda bt: (bt[0][1], bt[0][0]))
    for _, t in lines:
        t = t.replace(" ", "")
        if t in known:
            return t
    for _, t in lines:
        t = t.replace(" ", "")
        for n in sorted(known, key=len, reverse=True):
            if len(n) >= 2 and n in t:
                return n
    return lines[0][1].replace(" ", "") if lines else ""


def read_panel(p) -> dict:
    """已打开的取货口面板：编号、存量、设没设物品、物品名（悬停读）、状态。"""
    title = p.ocr_text(TITLE_ROI).replace(" ", "")
    m = TITLE_RE.search(title)
    btn = p.ocr_text(BUTTON_ROI).replace(" ", "")
    row = {"title": title, "port": int(m.group(1)) if m else None, "button": btn,
           "configured": "替换" in btn if btn else None, "stock": _stock(p.ocr_boxes(STOCK_ROI)),
           "status": p.ocr_text(STATUS_ROI).replace(" ", ""), "item": ""}
    if row["configured"]:
        move_cursor(p.hwnd, *SLOT); time.sleep(1.0)
        row["item"] = _item_name(p.ocr_boxes(TIP_ROI))
        move_cursor(p.hwnd, *NP.PARK); time.sleep(0.3)
    elif row["configured"] is False:
        row["stock"] = None
    return row


def _close(p, core_panel: bool = False) -> None:
    from patrol import CORE_PANEL_CLOSE
    if core_panel or p.core_panel_open():
        p.click(*CORE_PANEL_CLOSE); time.sleep(1.5)
    elif TITLE_RE.search(p.ocr_text(TITLE_ROI)) or p.panel_open():
        p.click(*CLOSE); time.sleep(1.5)


def read_here(p, log=print, shot_dir=None) -> dict:
    """已经在俯瞰、核心在画面里：找核心 → 必要时把核心挪到画面中间 → 逐个三角点开读 → 关。"""
    NP._need_overhead(p, "找核心前")
    move_cursor(p.hwnd, *NP.PARK); time.sleep(0.5)
    hit = _settled_core(p)
    if hit is None:
        return {"error": "画面里没找到协议核心/次级核心的标签", "rows": []}
    name, c = hit
    if np.abs(c - CENTER).max() > 60:  # 挪镜头让核心居中，上下的三角离开顶部按钮栏
        NP._need_overhead(p, "挪镜头前")
        NP._move(p, c - CENTER)
        time.sleep(1.0)
        hit = _settled_core(p)
        if hit is None:
            return {"error": "挪镜头后找不到核心标签", "rows": []}
        name, c = hit
    rows, seen = [], set()
    for x0, y0, side in port_points(c):
        NP._need_overhead(p, "点取货口前")
        move_cursor(p.hwnd, *NP.PARK); time.sleep(0.3)
        x, y, score = snap(p.shot(), x0, y0, side)
        r = None
        for dx in (0, -18 * side, 18 * side):  # 没开出取货口面板：往里、往外各挪半格再试
            if dx:
                NP._need_overhead(p, "重试点取货口前")
            p.click(x + dx, y); time.sleep(2.0)
            if p.core_panel_open():
                _close(p, core_panel=True)
                r = {"u": [x + dx, y], "error": "点开的是核心全屏面板（已关掉）"}
                continue
            r = read_panel(p)
            r["u"] = [x + dx, y]
            if r["port"] is not None:
                break
            r["error"] = f"点开的不是取货口面板（{r['title'] or '没开出面板'}）"
            _close(p)
        if r.get("error"):
            rows.append(r)
            log(f"   取货口? ({x},{y})：{r['error']}")
            continue
        if r["port"] in seen:
            r["error"] = f"取货口{r['port']} 重复读到"
        else:
            seen.add(r["port"])
        if shot_dir is not None and r["port"]:
            fn = f"core_port{r['port']}.png"
            NP._wr(shot_dir / fn, p.shot())
            r["shot"] = fn
        _close(p)
        log(f"   取货口{r['port'] or '?'} ({x},{y})：" + (r.get("error") or (
            f"{r['item'] or '?'}（仓库存量 {r['stock'] if r['stock'] is not None else '?'}）" if r["configured"] else "没设物品")))
        rows.append(r)
    NP._need_overhead(p, "读完取货口")
    move_cursor(p.hwnd, *NP.PARK); time.sleep(0.5)
    img = p.shot()
    if shot_dir is not None:  # 核心和四周（看三角外面有没有接带子）
        x0, y0 = int(max(0, c[0] - 5.5 * CELL)), int(max(0, c[1] - 5 * CELL))
        NP._wr(shot_dir / "core.png", img[y0:int(c[1] + 5 * CELL), x0:int(c[0] + 5.5 * CELL)])
    rows = sorted([r for r in rows if r.get("port") and not r.get("error")], key=lambda r: r["port"]) + \
        [r for r in rows if not r.get("port") or r.get("error")]
    return {"core": name, "center": [round(float(c[0])), round(float(c[1]))], "rows": rows, "error": ""}


def read_area(region: str, area: str, blueprint: str, log=print, skip_nav: bool = False) -> dict:
    """传送 → 进俯瞰（落地就在核心旁边）→ 读 6 个取货口 → 退出俯瞰 → 存档案 ledger/core_ports/<分区>.json。"""
    import navigate
    from patrol import Patrol
    res = {"path": None, "rows": [], "error": "", "step": "准备"}
    try:
        p = None
        if not skip_nav:
            # 已经在这个分区就不传送（2026-11-10 站在武陵城协议核心旁边强制传送，地图上找不到基地图标）；
            # 进俯瞰后画面里没有核心（人在分区别处），再强制传送到基地
            for force in (False, True):
                res["step"] = "传送"
                if not navigate.goto_area(region, area, blueprint, force_teleport=force):
                    res["error"] = navigate.last_error() or "传送失败"
                    return res
                res["step"] = "进俯瞰"
                if not navigate.enter_overhead():
                    res["error"] = navigate.last_error() or "进不了俯瞰"
                    return res
                p = Patrol(); p.area = area
                move_cursor(p.hwnd, *NP.PARK); time.sleep(0.5)
                p.shot()
                if find_core(NP._ocr_all(p)) or force:
                    break
                log("   画面里没有核心，退出俯瞰、传送到基地")
                import full_patrol
                if not full_patrol.exit_overhead():
                    res["error"] = "按了 CapsLock 仍在俯瞰"
                    return res
        p = p or Patrol(); p.area = area
        if p.scene() != "Scene_Overhead":
            res["error"] = "不在俯瞰"
            return res
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot_dir = DEBUG_DIR / "shots" / f"{stamp}_{area}_核心取货口"
        shot_dir.mkdir(parents=True, exist_ok=True)
        log(f"== {area}：读核心取货口")
        res["step"] = "读取货口"
        r = read_here(p, log=log, shot_dir=shot_dir)
        res.update(rows=r["rows"], error=r["error"])
        if not r["error"]:
            out = {"area": area, "time": stamp, "core": r["core"], "shots": str(shot_dir.relative_to(ROOT)), "rows": r["rows"]}
            d = NP.archive_dir(blueprint) / "ledger" / "core_ports"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{area}.json"
            path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            res["path"] = path
            res["step"] = "完成"
        if not skip_nav:
            import full_patrol
            if not full_patrol.exit_overhead():
                res["error"] = res["error"] or "按了 CapsLock 仍在俯瞰"
        return res
    except Exception as ex:  # noqa: BLE001
        res["error"] = f"{type(ex).__name__}: {ex}"
        return res


def load(blueprint: str, area: str) -> dict:
    p = NP.archive_dir(blueprint) / "ledger" / "core_ports" / f"{area}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def configured(blueprint: str, area: str) -> list[dict]:
    """上次读到的、设了物品的取货口。"""
    return [r for r in load(blueprint, area).get("rows", []) if r.get("configured") and r.get("port")]


def summary(blueprint: str, area: str) -> str:
    """一句话：核心取货口设了什么（给设计分析 / 巡检报告）。没读过返回空串。"""
    d = load(blueprint, area)
    if not d:
        return ""
    rows = [r for r in d.get("rows", []) if r.get("port")]
    cf = [r for r in rows if r.get("configured")]
    if not cf:
        return f"{d.get('core', '核心')} {len(rows)} 个取货口都没设物品（{d.get('time', '')[:8]} 读）"
    s = "、".join(f"{r['port']} 号 {r.get('item') or '?'}（存量 {r['stock'] if r.get('stock') is not None else '?'}）" for r in cf)
    return f"{d.get('core', '核心')}取货口设了：{s}；其余 {len(rows) - len(cf)} 个没设（{d.get('time', '')[:8]} 读；有没有接带子看截图核对）"


if __name__ == "__main__":
    import sys
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if "--here" in sys.argv:
        from patrol import Patrol
        p = Patrol()
        print(json.dumps(read_here(p), ensure_ascii=False, indent=1))
    else:
        r = read_area(a[0], a[1], a[2])
        print(r["error"] or f"已存 {r['path']}")
