"""物流方向图上「要人核对」的点（flow.json issues）到游戏俯瞰近景里实地拍照（只读）。

2026-11-09 用户：14 个红圈看不清，去游戏里实际检查。
操作边界：只按方向键/WASD、滚轮、CapsLock（进出俯瞰），传送用 navigate.goto_area；不点任何东西、不按 Z。挪镜头前确认还在俯瞰。
坐标：拼合图 → 远景（transform F0/K）→ 近景起点系（F2N），和读准入口限速同一套。
每个点存：整屏截图 + 以预测位置为中心的放大裁图（画圈），debug/shots/<时间>_<据点>_物流核对/。
"""
from __future__ import annotations

import json
import time

import cv2
import numpy as np

import near_patrol as NP
from game import DEBUG_DIR, move_cursor, wheel


def targets(blueprint: str, area: str, F2N: np.ndarray, kinds=("分叉", "单向", "方向冲突", "小三角反了")) -> list[dict]:
    bd = NP.bpa_base_dir(blueprint, area)
    if bd is None or not (bd / "flow.json").exists():
        return []
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    res = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
    out = []
    for k, it in enumerate(res["issues"]):
        if it["kind"] not in kinds:
            continue
        pts = [it.get("at") or res["nodes"][it["a"]]["c"]]
        if it.get("b") is not None and not it.get("at"):
            pts.append(res["nodes"][it["b"]]["c"])
        p = np.mean(np.array(pts, float), axis=0)
        far = np.array([tf["F0"][0] + p[0] * tf["K"], tf["F0"][1] + p[1] * tf["K"]])
        q = F2N @ np.array([far[0], far[1], 1.0])
        out.append({"k": k, "kind": it["kind"], "msg": it["msg"], "p": [int(p[0]), int(p[1])], "q": q, "icon_q": q, "name": it["kind"]})
    return out


def inspect_area(region: str, area: str, blueprint: str, log=print, skip_nav: bool = False) -> dict:
    import navigate
    from patrol import Patrol
    res = {"rows": [], "error": "", "dir": None}
    _, every, scan_dir, F2N = NP.load_targets(blueprint, area, [])
    tg = targets(blueprint, area, F2N)
    if not tg:
        res["error"] = "没有要核对的点"
        return res
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shot_dir = DEBUG_DIR / "shots" / f"{stamp}_{area}_物流核对"
    shot_dir.mkdir(parents=True, exist_ok=True)
    res["dir"] = str(shot_dir)
    log(f"== {area}：物流核对点 {len(tg)} 个")
    if not skip_nav:
        import full_patrol
        from patrol import Patrol as _P
        if _P().scene() == "Scene_Overhead" and not full_patrol.exit_overhead():
            res["error"] = "还在俯瞰里、按 CapsLock 退不出去（快捷建造栏开着？），不传送"
            return res
        if not navigate.goto_area(region, area, blueprint):
            res["error"] = navigate.last_error() or "传送失败"
            return res
        if not navigate.enter_overhead():
            if not navigate.goto_area(region, area, blueprint, force_teleport=True) or not navigate.enter_overhead():
                res["error"] = navigate.last_error() or "进不了俯瞰"
                return res
    p = Patrol(); p.area = area
    if p.scene() != "Scene_Overhead" and not navigate.enter_overhead():
        res["error"] = navigate.last_error() or "进不了俯瞰"
        return res
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
    views = NP.plan_views(tg, start=cam, bounds=NP.cam_bounds(scan_dir))
    log(f"   起点 {nv}/{nl} 个标签对上；{len(views)} 个视点")
    for vi, v in enumerate(views, 1):
        NP._need_overhead(p, f"视点 {vi} 挪镜头前")
        NP._move(p, v["cam"] - cam)
        move_cursor(p.hwnd, *NP.PARK); time.sleep(0.8)
        img = p.shot()
        cam, nv, nl = NP.localize(NP.labels_from_boxes(NP._ocr_all(p)), every, v["cam"])
        full = f"v{vi:02d}.png"
        NP._wr(shot_dir / full, img)
        for e in v["targets"]:
            u = e["q"] - cam
            x, y = int(round(u[0])), int(round(u[1]))
            H, W = img.shape[:2]
            x0, y0, x1, y1 = max(0, x - 200), max(0, y - 150), min(W, x + 200), min(H, y + 150)
            crop = img[y0:y1, x0:x1].copy()
            cv2.circle(crop, (x - x0, y - y0), 30, (0, 0, 255), 2)
            fn = f"k{e['k']:02d}_{e['kind']}_{e['p'][0]}_{e['p'][1]}.png"
            NP._wr(shot_dir / fn, crop)
            res["rows"].append({"k": e["k"], "kind": e["kind"], "msg": e["msg"], "p": e["p"], "u": [x, y], "view": vi,
                                "labels": f"{nv}/{nl}", "shot": fn, "full": full})
            log(f"   #{e['k']} {e['kind']} {tuple(e['p'])} → 画面 ({x},{y})，标签 {nv}/{nl}")
    p.zoom_out()
    (shot_dir / "rows.json").write_text(json.dumps(res["rows"], ensure_ascii=False, indent=1), encoding="utf-8")
    if not skip_nav:
        import full_patrol
        full_patrol.exit_overhead()
    return res
