"""从历史巡检里裁「标签图标」做模板，并离线测图标能不能把设备类型认对。

数据：debug/patrol_*.json 里 guessed=False（面板确认过名字）、带 shot/sx/sy 的设备（2026-11-07 共 731 台、29 类）。
每台：离线 OCR 那张每屏截图 → 在标记附近按 guess_name 的位置规则找首字等于名字首字的标签 → 标签文字框左边约 26 px 就是图标。
图标裁出来二值化（白色字形）、按字形外接框对齐、缩放到 20×20，同名取平均做模板；再用留一法算每类的识别率。

用法：.venv\\Scripts\\python.exe src\\tools\\icon_harvest.py [--limit N]
产出：debug/icon_harvest/crops/<名字>/*.png（原始裁片）、debug/icon_harvest/templates/<名字>.png、debug/icon_harvest/report.json
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from offline_ocr import OfflineOCR, imread  # noqa: E402

ROOT = HERE.parent.parent
DEBUG = ROOT / "debug"
OUT = DEBUG / "icon_harvest"
MAP_ROI = [340, 80, 720, 465]


def label_anchor(badge: str, sx: int, sy: int) -> tuple[int, int, int, int]:
    """和 patrol.guess_name 一样的位置规则：返回 (anchor_y, lo, hi, dxmax)。"""
    if badge == "空闲中":
        return sy - 45, sy - 110, sy + 60, 85  # 空闲标记在产物图标左下，标签多在上方；大设备标签在下方 38
    if badge == "关闭":
        return sy + 18, sy - 30, sy + 65, 100
    # 阻塞：多数设备标签在标记下方约 40；大设备（气体反应炉、提纯机）的阻塞标记画在设备中下部、标签在上方约 70（2026-11-07 对照图）。
    # 窗口向上放到 -95，首字一致的约束保证不会抓到邻居
    return sy + 40, sy - 95, sy + 85, 85


def find_label(boxes, name: str, badge: str, sx: int, sy: int):
    """标记附近的设备标签框。优先首字和名字一致的；一致的没有、但窗口里只有一个标签框时也用它（名字来自面板，不靠 OCR 文字；
    密集处首字 OCR 常错：提→音、精→乐、固→豆，2026-11-07 实测），这样能顺便量出首字 OCR 的错误率。
    返回 (box, ocr_ok)；没有返回 (None, False)。"""
    head = name[0]
    anchor, lo, hi, dxmax = label_anchor(badge, sx, sy)
    inwin = []
    for box, text, score in boxes:
        t = re.sub(r"[.。·…]+$", "", text)
        if not t or not re.match(r"^[一-鿿]", t):
            continue
        cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
        if lo <= cy <= hi and abs(cx - sx) <= dxmax:
            inwin.append((abs(cx - sx) + abs(cy - anchor), box, t[0] == head))
    same = [w for w in inwin if w[2]]
    if same:
        return min(same)[1], True
    # 不用「窗口里只有一个标签就算」的兜底：2026-11-07 试过，把邻居标签裁进来，留一法从 97% 掉到 85%（息壤供电桩/气体反应炉的
    # 「模板」其实是别人的图标）。首字 OCR 错的那部分（提→音、固→豆）宁可漏掉；它们的图标会在固定缩放底账扫描时补
    return None, False


ICON = 26  # 图标裁窗边长（图标本体约 22–24 px）
SHIFT = 3  # 匹配时允许的位移（px）
MIN_SAMPLES = 3  # 少于这个样本数的类不做模板、不参与混淆；3–7 个的标 weak
SAME = {"仓库存取线基段": "仓库口", "仓库取货口": "仓库口"}  # 图标完全一样的类并成一类（2026-11-07 对照图）


def crop_icon(image: np.ndarray, box) -> np.ndarray | None:
    """标签文字框左边的图标：右边缘距文字框左边 2–4 px，和文字同心，边长约 24。裁 ICON×ICON 的窗，再多留 SHIFT 像素边给对齐用。"""
    x, y, w, h = box
    cy = y + h // 2
    x1 = x - 2
    x0 = x1 - ICON
    y0 = cy - ICON // 2
    pad = SHIFT
    if x0 - pad < 0 or y0 - pad < 0 or y0 + ICON + pad > image.shape[0] or x1 + pad > image.shape[1]:
        return None
    return image[y0 - pad:y0 + ICON + pad, x0 - pad:x1 + pad]


def binarize(crop: np.ndarray) -> np.ndarray:
    """白色字形 → 255，其余 0。背景（草地/传送带/设备顶面）很少是亮且低饱和的。"""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return ((hsv[..., 2] > 175) & (hsv[..., 1] < 70)).astype(np.uint8) * 255


def normalize(crop: np.ndarray) -> np.ndarray | None:
    """带边的二值裁窗（ICON+2*SHIFT 见方）。字形像素太少说明没裁到图标。"""
    b = binarize(crop)
    if b.shape[0] != ICON + 2 * SHIFT or b.shape[1] != ICON + 2 * SHIFT:
        return None
    core = b[SHIFT:-SHIFT, SHIFT:-SHIFT]
    if (core > 0).sum() < 60:
        return None
    return b


def center(b: np.ndarray) -> np.ndarray:
    return b[SHIFT:-SHIFT, SHIFT:-SHIFT]


def similarity(sample_padded: np.ndarray, template: np.ndarray) -> float:
    """模板（ICON×ICON）在带边样本上滑动 ±SHIFT，取最大归一化相关。"""
    res = cv2.matchTemplate(sample_padded.astype(np.float32), template.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    return float(res.max()) if res.size else -1.0


def make_template(samples: list[np.ndarray]) -> np.ndarray:
    """中位样本：和其他样本相关之和最大的那个，再把其他样本对齐到它上面取平均、二值化。"""
    cores = [center(s) for s in samples]
    best_i, best_sum = 0, -1e9
    for i, c in enumerate(cores):
        tot = sum(similarity(s, c) for j, s in enumerate(samples) if j != i)
        if tot > best_sum:
            best_i, best_sum = i, tot
    ref = cores[best_i].astype(np.float32)
    acc = np.zeros_like(ref)
    n = 0
    for s in samples:
        res = cv2.matchTemplate(s.astype(np.float32), ref, cv2.TM_CCOEFF_NORMED)
        _, _, _, (dx, dy) = cv2.minMaxLoc(res)
        acc += s[dy:dy + ICON, dx:dx + ICON].astype(np.float32)
        n += 1
    return ((acc / n) > 127).astype(np.uint8) * 255


def collect(limit: int | None) -> list[dict]:
    items = []
    for p in sorted(DEBUG.glob("patrol_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("shots"):
            continue
        shots = ROOT / d["shots"]
        for x in d.get("devices", []):
            if x.get("guessed") or not x.get("shot") or "|" in x["name"] or x["name"].startswith("（"):
                continue
            shot = shots / x["shot"]
            if shot.exists():
                items.append({"name": x["name"], "shot": str(shot), "sx": x["sx"], "sy": x["sy"], "badge": x["badge"]})
    return items[:limit] if limit else items


def main(argv: list[str]) -> int:
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    items = collect(limit)
    by_shot = defaultdict(list)
    for it in items:
        by_shot[it["shot"]].append(it)
    print(f"{len(items)} 台设备，{len(by_shot)} 张截图")
    cache_path = OUT / "ocr_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    ocr = None
    (OUT / "crops").mkdir(parents=True, exist_ok=True)
    samples: dict[str, list[np.ndarray]] = defaultdict(list)
    miss = defaultdict(int)
    ocr_hit = defaultdict(lambda: [0, 0])  # 首字 OCR 和名字一致的 / 找到标签的
    t0 = time.time()
    for i, (shot, devs) in enumerate(by_shot.items(), 1):
        img = imread(shot)
        if img is None:
            continue
        if shot in cache:
            boxes = cache[shot]
        else:
            if ocr is None:
                ocr = OfflineOCR()
            boxes = ocr.boxes(img, MAP_ROI)
            cache[shot] = boxes
        for d in devs:
            box, ocr_ok = find_label(boxes, d["name"], d["badge"], d["sx"], d["sy"])
            if box is None:
                miss[d["name"]] += 1
                continue
            ocr_hit[d["name"]][0] += ocr_ok
            ocr_hit[d["name"]][1] += 1
            crop = crop_icon(img, box)
            norm = normalize(crop) if crop is not None else None
            if norm is None:
                miss[d["name"]] += 1
                continue
            k = len(samples[d["name"]])
            samples[d["name"]].append(norm)
            if k < 12:
                sub = OUT / "crops" / d["name"]
                sub.mkdir(parents=True, exist_ok=True)
                cv2.imencode(".png", crop)[1].tofile(str(sub / f"{k:02d}.png"))
        if i % 25 == 0:
            print(f"  {i}/{len(by_shot)} 张，{time.time() - t0:.0f} 秒")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    if ocr:
        print(f"OCR {ocr.n} 次 {ocr.seconds:.0f} 秒（其余走缓存）")
    merged: dict[str, list[np.ndarray]] = defaultdict(list)
    for name, arr in samples.items():
        merged[SAME.get(name, name)] += arr
    samples = merged

    # 模板 = 同名样本平均后再二值化；留一法识别率
    (OUT / "templates").mkdir(parents=True, exist_ok=True)
    templates: dict[str, np.ndarray] = {}
    for name, arr in samples.items():
        if len(arr) < MIN_SAMPLES:
            continue
        templates[name] = make_template(arr)
        cv2.imencode(".png", templates[name])[1].tofile(str(OUT / "templates" / f"{name}.png"))
    report = {"devices": len(items), "shots": len(by_shot), "classes": {}}
    total_ok = total = 0
    for name, arr in samples.items():
        if name not in templates:
            report["classes"][name] = {"samples": len(arr), "missed": miss.get(name, 0), "note": "样本不足"}
            continue
        ok = 0
        confusions = defaultdict(int)
        for i, s in enumerate(arr):
            others = [a for j, a in enumerate(arr) if j != i]
            own = make_template(others)
            best, best_sim = name, similarity(s, own)
            for other, tpl in templates.items():
                if other == name:
                    continue
                sim = similarity(s, tpl)
                if sim > best_sim:
                    best, best_sim = other, sim
            if best == name:
                ok += 1
            else:
                confusions[best] += 1
        total_ok += ok; total += len(arr)
        sims_own = [similarity(s, templates[name]) for s in arr]
        sims_other = [max(similarity(s, t) for o, t in templates.items() if o != name) for s in arr] if len(templates) > 1 else [0]
        oh = [0, 0]
        for raw, canon in list(SAME.items()) + [(name, name)]:
            if canon == name and raw in ocr_hit:
                oh[0] += ocr_hit[raw][0]; oh[1] += ocr_hit[raw][1]
        report["classes"][name] = {"samples": len(arr), "missed": miss.get(name, 0), "acc": round(ok / len(arr), 3),
                                   "ocr_first_char_acc": round(oh[0] / oh[1], 3) if oh[1] else None, "weak": len(arr) < 8,
                                   "own_sim_min": round(min(sims_own), 2), "other_sim_max": round(max(sims_other), 2),
                                   "confused_with": dict(sorted(confusions.items(), key=lambda kv: -kv[1])[:3])}
    report["acc_all"] = round(total_ok / total, 3) if total else None
    report["missed_total"] = sum(miss.values())
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n留一法总识别率 {report['acc_all']}（{total_ok}/{total}），没找到标签/图标 {report['missed_total']} 台")
    for name, r in sorted(report["classes"].items(), key=lambda kv: -kv[1]["samples"]):
        print(f"  {name:12s} 样本 {r['samples']:4d} 漏 {r['missed']:3d}  acc {r.get('acc', '-')}  首字OCR {r.get('ocr_first_char_acc', '-')}  本类最低 {r.get('own_sim_min', '-')} 他类最高 {r.get('other_sim_max', '-')}  混淆 {r.get('confused_with', '')}")
    if "--install" in argv:
        # 装进资源目录给巡检用：resource/icons/labels/<类>.png + index.json（样本数、留一法准确率、阈值参考）
        dest = ROOT / "resource" / "icons" / "labels"
        dest.mkdir(parents=True, exist_ok=True)
        index = {}
        for name, tpl in templates.items():
            cv2.imencode(".png", tpl)[1].tofile(str(dest / f"{name}.png"))
            r = report["classes"][name]
            index[name] = {"samples": r["samples"], "acc": r["acc"], "weak": r["weak"], "ocr_first_char_acc": r["ocr_first_char_acc"], "own_sim_min": r["own_sim_min"], "other_sim_max": r["other_sim_max"]}
        (dest / "index.json").write_text(json.dumps({"icon": ICON, "shift": SHIFT, "same": SAME, "built": time.strftime("%Y-%m-%d"), "classes": index}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已安装 {len(templates)} 个模板到 {dest}")
    if ocr:
        ocr.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
