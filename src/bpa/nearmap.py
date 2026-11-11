"""近景大图：把扫底账时拍的近景屏（滚入 7 格，每屏有镜头位置）拼成一张据点俯瞰图，用来在游戏画面上追管道。
蓝图拼合图上管子是统一的深灰线，交叉、叠层、拼接处容易断，而且分不出流体；游戏画面里水管是浅蓝（空管发暗），
颜色就是流体，连通也更可靠（2026-11-08 武陵城拼合图追不闭合后改的）。

坐标：近景起点系（和底账、近景巡检同一套）：屏幕点 s 在第 i 屏 → q = s + (cam_near_i − dd)，dd = scan.json 的 corner_recheck。
只取有效区（去掉顶栏、右侧按钮、底部快捷栏）；几屏重叠处取中位（标签、悬停提示这种只在个别屏出现的就被去掉）。
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from offline_ocr import imread

VALID = (260, 80, 1100, 540)  # x0, y0, x1, y1：主有效区
VALID_LL = (0, 260, 260, 540)  # 左下区


def screens(scan_dir: Path) -> tuple[list[tuple[Path, np.ndarray]], np.ndarray]:
    scan = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    dd = np.array(scan["corner_recheck"][1:3], float)
    out, seen = [], set()
    for s in scan["screens"]:
        if s["file"] in seen:
            continue
        seen.add(s["file"])
        f = scan_dir / "near" / s["file"]
        if not f.exists() and f.with_suffix(".jpg").exists():  # 从蓝图分析 zip 导入的档案：原始截图导出时转成了 jpg
            f = f.with_suffix(".jpg")
        if f.exists():
            out.append((f, np.array(s["cam_near"], float) - dd))
    return out, dd


def build(scan_dir: Path, scale: float = 0.5, max_layers: int = 5) -> dict:
    """返回 {img, origin, scale}：img 是拼好的近景大图（按 scale 缩小），近景起点系的点 q → 图上 ((q − origin) · scale)。"""
    scr, _ = screens(scan_dir)
    cams = np.array([c for _, c in scr])
    origin = cams.min(axis=0) + np.array([VALID_LL[0], VALID[1]])
    size = cams.max(axis=0) + np.array([VALID[2], VALID[3]]) - origin
    W, H = int(size[0] * scale) + 2, int(size[1] * scale) + 2
    acc = np.zeros((max_layers, H, W, 3), np.uint8)
    cnt = np.zeros((H, W), np.int32)
    for f, cam in scr:
        im = imread(f)
        for x0, y0, x1, y1 in (VALID, VALID_LL):
            part = cv2.resize(im[y0:y1, x0:x1], None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            ox, oy = ((cam + np.array([x0, y0]) - origin) * scale).round().astype(int)
            h, w = part.shape[:2]
            h, w = min(h, H - oy), min(w, W - ox)
            if h <= 0 or w <= 0 or ox < 0 or oy < 0:
                continue
            layer = cnt[oy:oy + h, ox:ox + w]
            for k in range(max_layers):
                sel = layer == k
                acc[k, oy:oy + h, ox:ox + w][sel] = part[:h, :w][sel]
            layer += 1
            np.minimum(layer, max_layers, out=layer)
    # 每个像素在几屏里出现过：取中位（亮度排序后取中间那张）
    n = np.maximum(cnt, 1)
    gray = acc.mean(axis=-1)
    gray[np.arange(max_layers)[:, None, None] >= n[None]] = np.nan
    order = np.argsort(np.nan_to_num(gray, nan=1e9), axis=0)
    mid = np.take_along_axis(order, ((n - 1) // 2)[None], axis=0)[0]
    img = np.take_along_axis(acc, mid[None, ..., None].repeat(3, -1), axis=0)[0]
    img[cnt == 0] = 0
    return {"img": img, "origin": origin, "scale": scale, "count": cnt}


def save(m: dict, F2N: np.ndarray, png: Path) -> None:
    """近景大图存成一张无损 png（alpha = 有效区）+ 同名 json（origin、scale、远景→近景配准 F2N）。
    导出蓝图分析 zip 时用：导入方直接用作者这边用原始 png 截图拼好的图，颜色判断（断口、污水管）和作者一致。"""
    bgra = np.dstack([m["img"], np.where(m["count"] > 0, 255, 0).astype(np.uint8)])
    ok, buf = cv2.imencode(".png", bgra)
    png.parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(png))
    png.with_suffix(".json").write_text(json.dumps({"origin": [float(v) for v in m["origin"]], "scale": float(m["scale"]),
                                                    "F2N": np.asarray(F2N, float).tolist()}), encoding="utf-8")


def load(png: Path) -> tuple[dict, np.ndarray] | None:
    """save 存的近景大图 → ({img, origin, scale, count}, F2N)；没有返回 None。"""
    meta = png.with_suffix(".json")
    if not png.exists() or not meta.exists():
        return None
    im = cv2.imdecode(np.fromfile(str(png), np.uint8), cv2.IMREAD_UNCHANGED)
    j = json.loads(meta.read_text(encoding="utf-8"))
    return ({"img": im[..., :3].copy(), "origin": np.array(j["origin"], float), "scale": j["scale"], "count": (im[..., 3] > 0).astype(np.int32)},
            np.array(j["F2N"], float))
