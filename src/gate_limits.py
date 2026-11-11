"""读准入口限速（只读）：俯瞰近景里按据点拼合图上每个准入口的位置逐个点开面板，读「当前速度」「限制速度」和两个开关状态，关掉面板。

2026-11-09 用户：下一步读准入口限速。设计分析（docs/蓝图分析器-设计.md 4.7「准入口 ↔ 下游需求」）要拿读到的限速和下游需求、作者填的流速比。

操作边界（和深入巡检点准入口一样）：
- 只点：准入口图标（打开面板）、重叠时「选择目标」菜单里准入口那一行、面板右上角关闭 X；误点到协议核心只点核心面板的关闭。
- 绝不点：限速管理、限制速度/限制通过数量的开/关、-6/+6、滑条、替换物品、收纳。开关状态按两个选项的底色判断（选中 = 深色药丸）。
- 不按 Z；挪镜头前确认还在俯瞰（_need_overhead）。
坐标：拼合图 → 远景 → 近景起点系，再加 near_patrol.GATE_SHIFT 就是画面里准入口的图标；按图标圆环对准后点圆心。
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter

import cv2
import numpy as np

import badges
import near_patrol as NP
from game import DEBUG_DIR, ROOT, move_cursor, wheel

GATE_NAMES = ("管道准入口", "物品准入口")
SPEED_LINE_ROI = [600, 462, 420, 40]  # 「当前速度 30 单位/分」或「速度未限制 XXX」整行
LIMIT_ROI = [195, 480, 110, 36]      # 「限制速度」下面的「30 单位/分」（patrol.GATE_LIMIT_ROI）
LIMIT_TOGGLE = {"开": (418, 469), "关": (464, 469)}   # 限制速度 开/关
COUNT_TOGGLE = {"开": (418, 351), "关": (464, 351)}   # 限制通过数量 开/关
COUNT_ROI = [195, 300, 260, 30]      # 「累计通过数量」那一行（没开时「XXX 已暂停计数」，开了是累计数字）
COUNT_LIMIT_ROI = [197, 364, 150, 30]  # 「限制通过数量」下面的上限数字（开了才有；没开是一张插图）
ICON_BOX = (808, 333, 46)            # 面板中间的物品图标（圆心 x, y, 半宽）：存图，物品名以后按图标认
STATUS_ROI = [500, 566, 320, 32]
STATUS_RE = re.compile(r"(生产中|运行中|空闲中|阻塞|缺料|待机|未激活|停机|供给不足|电力不足|断电)")


def targets(blueprint: str, area: str, F2N: np.ndarray) -> list[dict]:
    """据点拼合图上的全部准入口（logistics.json），能对上输入端编号的带上编号和作者/自动标的物品、流速。"""
    from bpa import pipenet as PN
    bd = NP.bpa_base_dir(blueprint, area)
    if bd is None:
        return []
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    inputs = PN.load_inputs(bd)
    out = []
    for g in json.loads((bd / "logistics.json").read_text(encoding="utf-8")):
        if g.get("type") != "准入口":
            continue
        x, y = g["p"]
        far = np.array([tf["F0"][0] + x * tf["K"], tf["F0"][1] + y * tf["K"]])
        icon = F2N @ np.array([far[0], far[1], 1.0]) + NP.GATE_SHIFT
        m = next((m for m in inputs if abs(m["p"][0] - x) <= 15 and abs(m["p"][1] - y) <= 15), None)
        out.append({"name": "准入口", "p": [int(x), int(y)], "far": (float(far[0]), float(far[1])), "icon_q": icon, "q": icon,
                    "kind": g.get("kind") or g.get("kind_local") or "", "bp": g.get("bp"), "icon_group": g.get("icon_group"),
                    "gate_no": m["no"] if m else None, "item": (m or {}).get("item") or "", "rate": (m or {}).get("rate"),
                    "where": "线内", "recipe": "", "output": None, "recipe_id": None})
    return out


def _toggle_on(img, pos: dict) -> bool | None:
    dark = {k: float(img[py - 6:py + 6, px - 14:px + 14].mean()) for k, (px, py) in pos.items()}
    if abs(dark["开"] - dark["关"]) < 8:
        return None  # 分不清（面板没完全展开/被挡）
    return dark["开"] < dark["关"]


def _num(text: str) -> float | None:
    t = text.replace("单位/分", "")
    t = re.sub(r"(?<![A-Za-z])[Oo〇](?![A-Za-z])", "0", t)  # 「当前速度 0」常被认成字母 O（2026-11-09 武陵城重息壤口）
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    return float(m.group(1)) if m else None


def read_panel(ocr, img, title: str) -> dict:
    """面板开着时读：当前速度那一行（限速没开时写「速度未限制」，没有数字）、限速数值、累计通过数量那一行（没开时「已暂停计数」）、运行状态。
    ocr(roi) -> 文字。开关状态不按底色判：面板半透明，背景透上来会判错（2026-11-09 实跑 13 号口判成开）。"""
    line = ocr(SPEED_LINE_ROI).replace(" ", "")
    unlimited = "未限制" in line
    has_speed = "当前速度" in line or "速度" in line and not unlimited
    speed = None if unlimited else _num(line.split("速度")[-1]) if has_speed else None
    limit = None if unlimited else _num(ocr(LIMIT_ROI))
    count = ocr(COUNT_ROI).replace(" ", "")
    # 限制通过数量（2026-11-09 用户：赤铜耐压罐口开了「限制通过数量 1」，过 1 个就不再放行；芽针、锦草口是 20）
    count_on = False if "暂停计数" in count else True if re.search(r"\d", count) else None
    passed = _num(count.split("数量")[-1]) if count_on else None
    count_limit = _num(ocr(COUNT_LIMIT_ROI)) if count_on else None
    st = STATUS_RE.search(ocr(STATUS_ROI))
    status = st.group(1) if st else ""
    if not status:  # 阻塞时底部状态条是红底、字认不出（2026-11-09 武陵城 13、18 号口）
        bar = img[578:597, 160:1130].astype(int)
        red = (bar[..., 2] > 110) & (bar[..., 2] > 2 * bar[..., 1]) & (bar[..., 2] > 2 * bar[..., 0])  # 阻塞：中间一段红（运行中是黄）
        if red.mean() > 0.008:
            status = "阻塞"
    return {"title": title or "", "speed": speed, "limit": limit,
            "limit_on": False if unlimited else (True if has_speed else _toggle_on(img, LIMIT_TOGGLE)),
            "count_on": count_on, "count_limit": count_limit, "passed": passed, "speed_line": line,
            "status": status}


def _click_ok(x: float, y: float) -> bool:
    """点击位置在有效区里、不在界面按钮上。"""
    if not (NP.SAFE[0] - 40 <= x <= NP.SAFE[1] + 40 and NP.SAFE[2] - 40 <= y <= NP.SAFE[3] + 30):
        return False
    return not any(mx <= x < mx + mw and my <= y < my + mh for mx, my, mw, mh in badges.UI_MASKS)


def open_gate(p, x: int, y: int) -> tuple[bool, str]:
    """点准入口图标打开面板；弹「选择目标」菜单时挑准入口那一行。返回 (开了准入口面板, 说明)。"""
    p.click(x, y)
    time.sleep(1.2)
    if p.core_panel_open():  # 误点到协议核心：只点核心面板的关闭
        from patrol import CORE_PANEL_CLOSE
        p.click(*CORE_PANEL_CLOSE); time.sleep(1.0)
        return False, "点到了协议核心（已关掉）"
    if not p.panel_open():
        x0, y0 = max(0, x - 160), max(0, y - 70)
        roi = [x0, y0, min(420, 1280 - x0), min(180, 720 - y0)]
        hit = next(((b, t) for b, t in p.ocr_boxes(roi) if t.replace(" ", "") in GATE_NAMES), None)
        if hit is None:
            return False, "没开出面板"
        b = hit[0]
        p.click(b[0] + b[2] // 2, b[1] + b[3] // 2)
        time.sleep(1.2)
        if not p.panel_open():
            return False, "选了菜单里的准入口但没开出面板"
    return True, ""


def read_area(region: str, area: str, blueprint: str, log=print, cancelled=lambda: False, skip_nav: bool = False) -> dict:
    """传送 → 进俯瞰 → 顶角 → 滚入 → 按视点逐个点开准入口读限速 → 缩回 → 退出俯瞰 → 存 ledger/gate_limits/<据点>.json。"""
    import navigate
    from patrol import Patrol

    res = {"path": None, "rows": [], "error": "", "step": "准备"}
    try:
        _, every, scan_dir, F2N = NP.load_targets(blueprint, area, [])
        tg = targets(blueprint, area, F2N)
        if not tg:
            res["error"] = "据点拼合图上没有准入口（先在「蓝图分析」里拼合）"
            return res
        bounds = NP.cam_bounds(scan_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot_dir = DEBUG_DIR / "shots" / f"{stamp}_{area}_限速"
        shot_dir.mkdir(parents=True, exist_ok=True)
        log(f"== {area}：读准入口限速 {len(tg)} 个（{dict(Counter(t['kind'] or '?' for t in tg))}）")
        if not skip_nav:
            res["step"] = "传送"
            if not navigate.goto_area(region, area, blueprint):
                res["error"] = navigate.last_error() or "传送失败"
                return res
            res["step"] = "进俯瞰"
            if not navigate.enter_overhead():
                if not navigate.goto_area(region, area, blueprint, force_teleport=True) or not navigate.enter_overhead():
                    res["error"] = navigate.last_error() or "进不了俯瞰"
                    return res
        p = Patrol(); p.area = area
        if p.scene() != "Scene_Overhead" and not navigate.enter_overhead():
            res["error"] = navigate.last_error() or "进不了俯瞰"
            return res
        res["step"] = "顶角 + 滚入"
        p.go_corner()
        NP._need_overhead(p, "顶角后")
        for _ in range(NP.TICKS):
            wheel(p.hwnd, int(NP.C[0]), int(NP.C[1]), 120); time.sleep(0.4)
        move_cursor(p.hwnd, *NP.PARK); time.sleep(1.0)
        p.shot()
        cam, nv, nl = NP.localize(NP.labels_from_boxes(NP._ocr_all(p)), every, np.zeros(2))
        if nv < 2 or (nl >= 4 and nv < 0.5 * nl):
            p.zoom_out()
            res["error"] = f"滚入后只有 {nv}/{nl} 个标签对上底账，定位不了"
            return res
        views = NP.plan_views(tg, start=cam, bounds=bounds)
        log(f"   起点 {nv}/{nl} 个标签对上；{len(views)} 个视点")
        rows = []
        for vi, v in enumerate(views, 1):
            if cancelled():
                log("   已取消，剩下的视点不看了")
                break
            res["step"] = f"视点 {vi}/{len(views)}"
            NP._need_overhead(p, f"视点 {vi} 挪镜头前")
            NP._move(p, v["cam"] - cam)
            p.shot()
            cam, nv, nl = NP.localize(NP.labels_from_boxes(NP._ocr_all(p)), every, v["cam"])
            for e in v["targets"]:
                if cancelled():
                    break
                NP._need_overhead(p, f"视点 {vi} 点准入口前")
                move_cursor(p.hwnd, *NP.PARK); time.sleep(0.3)
                img = p.shot()
                gray = cv2.medianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 3)
                u_pred = e["icon_q"] - cam
                sn = NP.snap_ring(gray, u_pred)
                row = {"p": e["p"], "far": [round(e["far"][0], 1), round(e["far"][1], 1)], "kind": e["kind"], "bp": e["bp"],
                       "gate_no": e["gate_no"], "item": e["item"], "rate": e["rate"], "view": vi}
                if sn is None:
                    row["error"] = "画面里没找到准入口图标（可能被挡住、或基地里这里不是准入口）"
                    rows.append(row); continue
                x, y = int(round(sn[0])), int(round(sn[1]))
                row["u"] = [x, y]
                if not _click_ok(x, y):
                    row["error"] = "图标太靠画面边/压着界面按钮，不点"
                    rows.append(row); continue
                ok, why = open_gate(p, x, y)
                if not ok:
                    row["error"] = why
                    if p.panel_open():
                        p.close_panel()
                    rows.append(row); continue
                pim = p.shot()
                r = read_panel(p.ocr_text, pim, p.panel_title())
                cx, cy, h = ICON_BOX
                fn = f"gate_{e['p'][0]}_{e['p'][1]}.png"
                NP._wr(shot_dir / fn, pim)
                row.update(r, shot=fn)
                if r["title"] not in GATE_NAMES:
                    row["error"] = f"点开的是「{r['title'] or '?'}」，不是准入口"
                p.close_panel()
                NP._need_overhead(p, "关面板后")
                log(f"   {'#' + str(e['gate_no']) if e['gate_no'] else ''} {e['kind']}准入口 {tuple(e['p'])}："
                    + (row.get("error") or f"限速 {'开' if r['limit_on'] else '关' if r['limit_on'] is False else '?'} {r['limit'] if r['limit'] is not None else '?'}/分，当前 {r['speed'] if r['speed'] is not None else '?'}/分 {r['status']}"))
                rows.append(row)
        p.zoom_out()
        out = {"area": area, "time": stamp, "shots": str(shot_dir.relative_to(ROOT)), "rows": rows}
        d = NP.archive_dir(blueprint) / "ledger" / "gate_limits"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{area}.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        res.update(path=path, rows=rows, step="完成")
        if not skip_nav:
            res["step"] = "退出俯瞰"
            import full_patrol
            if not full_patrol.exit_overhead():
                res["error"] = "按了 CapsLock 仍在俯瞰"
        return res
    except Exception as ex:  # noqa: BLE001
        res["error"] = f"{type(ex).__name__}: {ex}"
        return res
