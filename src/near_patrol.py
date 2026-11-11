"""近景定向巡检：按排查树指到的分区 + 设备类型，用档案底账把镜头直接移到这些设备旁边，在近景下每个视点看 12 秒，
数阻塞 / 空闲 / 关闭 / 缺气标记。不点任何东西。

和旧的定向巡检（full_patrol.scan_area 单遍远景扫描 + 点开面板）的区别：
  - 只看目标设备，不扫整个据点；不点面板（配方取自档案的配方表 ledger/<分区>_recipes.json，历史巡检点开过的）；
  - 近景（滚入 7 格）下身份按标签核对：底账预测位置附近读到这台设备的名字才算看到了它；
  - 每个视点静止 12 秒（盖住协议容量箱 10 秒一仓的周期），≥80% 帧有标记 = 持续，其余 = 间歇。

需要：档案底账 data/blueprints/<档案>/ledger/<分区>.json，且它记的扫描目录（scan.dir）还在
（far_corner / near_corner 用来标定远景 → 近景，scan.json 用来算镜头能到的范围）。没有就退回旧的定向巡检。

输出：patrol JSON（和 Patrol.save 同格式，devices 里带 near=True），交给 combine 按简报下结论（爆仓 / 回堵 / 设计限速），
另在 "near" 字段里记每台目标设备的观察结果（含缺气，combine 不管缺气）。

安全（2026-11-08 应龙关事故后）：传送后用 navigate.enter_overhead 进俯瞰；顶角前后、每次挪镜头前都核对在俯瞰，
不在就停，绝不在地面按方向键。只按 WASD、滚轮、CapsLock/Tab（navigate 进俯瞰），挪鼠标；不点击，不按 Z。
原型和基线：docs/蓝图分析器-设计.md 10.9。
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

import badges
import strike_scan
from game import DEBUG_DIR, ROOT, move_cursor, wheel
from offline_ocr import imread

# ---------------------------------------------------------------- 常量（近景 = 最远缩放顶角后在 C 处滚入 7 格）
NAMES = set(json.loads((ROOT / "data" / "building_names.json").read_text(encoding="utf-8")))
NAMES |= {"仓库取货口", "仓库存货口", "暗管入口", "暗管出口", "多口暗管入口", "多口暗管出口"}
ICON_OFF = {"仓库取货口": 0.731, "粉碎机": 0.655, "协议储存箱": 0.794, "精炼炉": 0.569, "暗管入口": 0.638, "暗管出口": 0.638,
            "固气转化机": 0.730, "种植机": 0.741, "配件机": 0.704, "采种机": 0.722, "封装机": 0.685, "灌装机": 0.741,
            "研磨机": 0.765, "息壤供电桩": 0.808}  # 图标中心 = 框左端 − ICON_OFF × 字高（和建底账同一规则）
ROI = [260, 80, 840, 460]
LROI = [0, 260, 600, 280]
SAFE = (300, 1040, 150, 500)  # 目标设备的标签图标落在这里：标签完整、上方状态标记也在有效区内
C = np.array([640.0, 330.0])  # 滚入中心
TICKS = 7
SPEED = 714.0  # 近景镜头速度（像素/秒，三次扫描实测 710–718）
PARK = (1265, 705)  # 鼠标停在有效区外，不出悬停提示
PAN = {"left": 0x41, "right": 0x44, "up": 0x57, "down": 0x53}  # A D W S
BADGE_SCALE = 1.3  # 近景下状态标记相对远景模板的放大（三区 276+153+11 个标记统计）
KINDS = ("blocked", "idle", "closed", "strike")
KIND_CN = {"blocked": "阻塞", "idle": "空闲中", "closed": "关闭", "strike": "缺气"}
PERSIST = 0.8  # ≥ 这个比例的帧有标记 = 持续


# ---------------------------------------------------------------- 底账 → 近景坐标
def archive_dir(blueprint: str) -> Path:
    import blueprint as bpm
    return Path(bpm.load(blueprint).dir)


def ledger_ready(blueprint: str, area: str) -> tuple[bool, str]:
    """这个分区能不能做近景定向巡检：档案底账在、它的扫描目录（标定图）也在。"""
    p = archive_dir(blueprint) / "ledger" / f"{area}.json"
    if not p.exists():
        return False, f"档案里没有「{area}」的底账"
    led = json.loads(p.read_text(encoding="utf-8"))
    d = (led.get("scan") or {}).get("dir")
    if not d:
        return False, f"「{area}」底账没记扫描目录（旧格式）"
    sd = ROOT / d
    for f in ("scan.json", "far_corner.png", "near_corner.png"):
        if not (sd / f).exists():
            return False, f"「{area}」底账的扫描目录缺 {f}（{d}）"
    return True, ""


def calib(scan_dir: Path) -> np.ndarray:
    """远景 → 近景起点系的相似变换（far_corner / near_corner 两张图 ORB 配准）。"""
    orb = cv2.ORB_create(4000)
    m = np.zeros((720, 1280), np.uint8); m[80:540, 260:1100] = 255
    a, b = imread(scan_dir / "far_corner.png"), imread(scan_dir / "near_corner.png")
    ka, da = orb.detectAndCompute(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), m)
    kb, db = orb.detectAndCompute(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), m)
    mt = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db), key=lambda x: x.distance)[:500]
    F2N, _ = cv2.estimateAffinePartial2D(np.float32([ka[x.queryIdx].pt for x in mt]), np.float32([kb[x.trainIdx].pt for x in mt]),
                                         ransacReprojThreshold=3)
    return F2N


def cam_bounds(scan_dir: Path):
    """镜头能到的范围（近景起点系）：建底账那次蛇形扫描顶到过四边。"""
    scan = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    dd = np.array(scan["corner_recheck"][1:3])
    cams = np.array([s["cam_near"] for s in scan["screens"]]) - dd
    return cams.min(axis=0), cams.max(axis=0)


def _recipe_id(name: str, output: str | None) -> str | None:
    if not output:
        return None
    from diagnose import RECIPES
    ids = [r["id"] for r in RECIPES if r["machine_name"] == name and output in r["outputs"]]
    return ids[0] if len(ids) == 1 else None


def load_targets(blueprint: str, area: str, machines: list[str]):
    """底账里这些类型的设备（线内 + 线外）→ [{name, far, q, where, recipe, output, recipe_id}]，以及 scan_dir、F2N。"""
    led = json.loads((archive_dir(blueprint) / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
    scan_dir = ROOT / led["scan"]["dir"]
    F2N = calib(scan_dir)
    rp = archive_dir(blueprint) / "ledger" / f"{area}_recipes.json"
    recipes = json.loads(rp.read_text(encoding="utf-8"))["devices"] if rp.exists() else []
    ents = [(e, "线内") for e in led["devices"]] + [(e, "线外") for e in led.get("outside", [])]
    every = [{"name": e["name"], "far": (e["x"], e["y"]), "q": F2N @ np.array([e["x"], e["y"], 1.0]), "where": w} for e, w in ents]
    targets = []
    for t in every:
        if t["name"] not in machines:
            continue
        r = min(((abs(x["x"] - t["far"][0]) + abs(x["y"] - t["far"][1]), x) for x in recipes if x["name"] == t["name"]),
                default=(999, None), key=lambda z: z[0])
        rec = r[1] if r[1] is not None and r[0] <= 3 else None
        ok = rec is not None and rec.get("recipe") and not rec["recipe"].startswith("配方：?") and not rec["recipe"].startswith("（没有在跑")
        t["recipe"] = rec["recipe"] if ok else ""
        t["output"] = rec.get("output") if ok else None
        t["recipe_id"] = _recipe_id(t["name"], t["output"])
        targets.append(t)
    return targets, every, scan_dir, F2N


# ---------------------------------------------------------------- 看画面
def icon_pos(box, name):
    return np.array([box[0] - ICON_OFF.get(name, 0.69) * min(box[3], 30), box[1] + box[3] / 2])


def badge_pos(icon, name):
    """状态标记在标签上方：(图标 x + 11×字数, 图标 y − 51)，±14 px。"""
    return np.array([icon[0] + 11 * len(name), icon[1] - 51])


def labels_from_boxes(boxes):
    out = []
    for b, t in boxes:
        t = t.strip()
        if t not in NAMES or b[3] > 34:
            continue
        if (b[0] <= ROI[0] + 4 + b[3] and b[1] < 260) or b[0] + b[2] >= 1096 or b[1] <= 82 or b[1] + b[3] >= 538:
            continue
        out.append((t, icon_pos(b, t), b))
    return out


def localize(labels, every, cam_est, search=220, tol=12):
    """看到的标签和底账（全部设备）预测位置比对，投票求镜头修正。返回 (cam, 票数, 标签数)。对上太少时放宽再试。"""
    cam, nv, nl = _localize(labels, every, cam_est, search, tol)
    if nv < 3 and nl >= 3:
        cam2, nv2, nl2 = _localize(labels, every, cam_est, 520, tol)
        if nv2 > nv:
            return cam2, nv2, nl2
    return cam, nv, nl


def _localize(labels, every, cam_est, search, tol):
    votes = Counter()
    for name, u, _ in labels:
        for e in every:
            if e["name"] == name:
                dlt = u - (e["q"] - cam_est)
                if abs(dlt[0]) <= search and abs(dlt[1]) <= search:
                    votes[(int(round(dlt[0] / 6)), int(round(dlt[1] / 6)))] += 1
    if not votes:
        return cam_est, 0, len(labels)
    # 周期排列会让错一个周期的位移也有票：每离估计位置 60 px 扣 1 票
    best, bv = None, -1e9
    for k in votes:
        v = sum(votes.get((k[0] + i, k[1] + j), 0) for i in (-1, 0, 1) for j in (-1, 0, 1)) - np.hypot(k[0] * 6, k[1] * 6) / 60.0
        if v > bv:
            best, bv = k, v
    pairs = [u - (e["q"] - cam_est) for name, u, _ in labels for e in every if e["name"] == name
             and abs((u - (e["q"] - cam_est))[0] - best[0] * 6) <= tol and abs((u - (e["q"] - cam_est))[1] - best[1] * 6) <= tol]
    return cam_est - np.median(pairs, axis=0), len(pairs), len(labels)


def plan_views(targets, start=np.zeros(2), bounds=None):
    """目标分成若干视点（贪心：每次取能同时放进安全区最多台的镜头位置），按最近邻排顺序。"""
    left, views = list(targets), []
    xs = (SAFE[0] + 20, (SAFE[0] + SAFE[1]) / 2, SAFE[1] - 20)
    ys = (SAFE[2] + 20, (SAFE[2] + SAFE[3]) / 2, SAFE[3] - 20)
    inside_of = lambda cam: [e for e in left if SAFE[0] <= (e["q"] - cam)[0] <= SAFE[1] and SAFE[2] <= (e["q"] - cam)[1] <= SAFE[3]]  # noqa: E731
    while left:
        best = None
        for e in left:
            for sx in xs:
                for sy in ys:
                    cam = e["q"] - np.array([sx, sy])
                    if bounds is not None:
                        cam = np.clip(cam, bounds[0], bounds[1])
                    ins = inside_of(cam)
                    if best is None or len(ins) > len(best[1]):
                        best = (cam, ins)
        inside = best[1] or [left[0]]
        qs = np.array([e["q"] for e in inside])
        cam = (qs.min(axis=0) + qs.max(axis=0)) / 2 - np.array([(SAFE[0] + SAFE[1]) / 2, (SAFE[2] + SAFE[3]) / 2])
        if bounds is not None:
            cam = np.clip(cam, bounds[0], bounds[1])
        views.append({"cam": cam, "targets": inside})
        ids = {id(e) for e in inside}
        left = [e for e in left if id(e) not in ids]
    order, cur = [], start
    while views:
        v = min(views, key=lambda v: np.abs(v["cam"] - cur).sum())
        order.append(v); views = [w for w in views if w is not v]; cur = v["cam"]
    return order


def _lcs(a, b):
    best = (0, 0, 0)
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > best[0]:
                best = (k, i, j)
    return best


def identify(boxes, name, u_pred, tol=25):
    """身份核对：预测位置附近的框里有一段和设备名连续相同 ≥ min(3, 名字长度) 个字，且这段字的位置对得上。
    OCR 读错个别字、几个标签连成一串都能认。返回 (确认与否, 看到的图标位置)。"""
    for b, t in boxes:
        t = t.strip()
        if abs(b[1] + b[3] / 2 - u_pred[1]) > 20:
            continue
        cw = 0.82 * min(b[3], 30)
        k, i, j = _lcs(t, name)
        uniq2 = k == 2 and j == 0 and sum(1 for n in NAMES if n.startswith(name[:2])) == 1
        if k < min(3, len(name)) and not uniq2:
            continue
        h = min(b[3], 30)
        # 名字第一个字在框里的位置：按字数从左数 / 串中间夹着一个图标（+1.3 字高）/ 从右端往回推
        cands = [b[0] + (i - j) * cw, b[0] + (i - j) * cw + 1.3 * h, b[0] + b[2] - (len(t) - i + j) * cw]
        exp_x = u_pred[0] + ICON_OFF.get(name, 0.69) * h
        best = min(cands, key=lambda v: abs(v - exp_x))
        if abs(best - exp_x) <= tol:
            return True, np.array([best - ICON_OFF.get(name, 0.69) * h, b[1] + b[3] / 2])
    return False, None


_tpl: dict = {}


def _templates():
    if _tpl:
        return _tpl
    for kind, t in badges._load().items():
        bgr = cv2.resize(t["bgr"], None, fx=BADGE_SCALE, fy=BADGE_SCALE, interpolation=cv2.INTER_LINEAR)
        mask = cv2.resize(t["mask"].astype(np.uint8), (bgr.shape[1], bgr.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
        limit = badges.SPECS[kind].get("max_diff", badges.MAX_DIFF)
        if kind == "blocked":
            limit = 35.0  # 近景实测：真标记 ≤34.5，36.8 起是橙色传送带弯道
        _tpl[kind] = (bgr, mask, limit)
    st = strike_scan.seed_template().astype(np.float32)
    _tpl["strike"] = (cv2.resize(st, None, fx=BADGE_SCALE, fy=BADGE_SCALE, interpolation=cv2.INTER_LINEAR), None, strike_scan.THRESH)
    return _tpl


def find_badges(image) -> list[tuple[str, int, int]]:
    """近景有效区（主区 + 左下区）里的状态标记 [(kind, x, y)]。"""
    vm = np.zeros(image.shape[:2], np.uint8); vm[80:540, 260:1100] = 1; vm[260:540, 0:260] = 1
    work = (image * vm[..., None]).astype(np.float32)
    found = []
    for kind, (bgr, mask, limit) in _templates().items():
        th, tw = bgr.shape[:2]
        if kind == "strike":
            om = strike_scan.orange_mask(image).astype(np.float32) * 255 * vm
            res = cv2.matchTemplate(om, bgr, cv2.TM_CCOEFF_NORMED)
            while True:
                _, val, _, (x, y) = cv2.minMaxLoc(res)
                if val < limit:
                    break
                found.append((kind, int(x + tw // 2), int(y + th // 2)))
                res[max(0, y - th):y + th, max(0, x - tw):x + tw] = -1
            continue
        m3 = np.repeat(mask[:, :, None], 3, axis=2).astype(np.float32)
        rms = np.sqrt(np.maximum(cv2.matchTemplate(work, bgr, cv2.TM_SQDIFF, mask=m3), 0) / m3.sum())
        while True:
            val, _, (x, y), _ = cv2.minMaxLoc(rms)
            if val > limit:
                break
            found.append((kind, int(x + tw // 2), int(y + th // 2)))
            rms[max(0, y - th):y + th, max(0, x - tw):x + tw] = np.inf
    return found


def check_frame(img, boxes, targets, cam, found=None):
    found = find_badges(img) if found is None else found
    gates = any("gate_no" in e for e in targets)
    gray = cv2.medianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 3) if gates else None
    drops = None
    out = {}
    for e in targets:
        if "gate_no" in e:  # 流体输入端：没有标签，按图标圆环对准（清水认不出圆时退回水滴）；没有状态标记
            u_pred = e["icon_q"] - cam
            sn = snap_ring(gray, u_pred)
            if sn is None and e.get("fluid") == "清水":
                drops = find_drops(img) if drops is None else drops
                sn = snap_gate(drops, u_pred)
            out[id(e)] = {"id_ok": sn is not None, "kinds": [], "pos": {}, "u": sn if sn is not None else u_pred}
            continue
        u_pred = e["q"] - cam
        ok, u_seen = identify(boxes, e["name"], u_pred)
        u = u_seen if ok else u_pred
        bp = badge_pos(u, e["name"])
        hits = [(k, x, y) for k, x, y in found if abs(x - bp[0]) <= 14 and abs(y - bp[1]) <= 14]
        out[id(e)] = {"id_ok": ok, "kinds": [h[0] for h in hits], "pos": {h[0]: (h[1], h[2]) for h in hits}, "u": u}
    return out


def annotate(img, results, targets):
    out = img.copy()
    for e in targets:
        r = results[id(e)]
        u = r["u"]
        cv2.circle(out, tuple(int(v) for v in u), 18, (0, 200, 0) if r["id_ok"] else (0, 0, 255), 2)
        if "gate_no" in e:  # 输入端没有状态标记：不画找标记的黄框（用户 2026-11-09 看不懂多出来的黄框）
            continue
        bp = badge_pos(u, e["name"])  # 黄框 = 找这台设备状态标记（阻塞/空闲/缺气）的位置
        cv2.rectangle(out, (int(bp[0] - 14), int(bp[1] - 14)), (int(bp[0] + 14), int(bp[1] + 14)), (0, 255, 255), 1)
        if r["kinds"]:
            cv2.putText(out, ",".join(r["kinds"]), (int(bp[0] + 16), int(bp[1])), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    return out


def _wr(path: Path, img) -> None:
    cv2.imencode(path.suffix, img)[1].tofile(str(path))


# ---------------------------------------------------------------- 结构基线：设备周围的管道 / 传送带和上次比少没少
# 水泵供大于求时堵、出水管被断开时也堵，标记一模一样（2026-11-08 用户把景玉谷水泵 (1437,443) 的出水管断开做对照，
# 只看标记分不出来）。能分开的是旁边的结构：管道少了一段。所以每台目标设备存一份「周围的管道/传送带」基线
# （第一次看到时自动存，不自动覆盖），之后每次和它比：少了的管道/传送带块 ≥ STRUCT_MIN 像素 → 结构变了。
STRUCT_HALF = (260, 200)  # 以设备标签图标为中心，左右 ±260、上下 ±200（近景 px）
# 少了的连通块面积下限。断开一格管子 ≈ 300 px（实测 284–419，空管也算管道后断口边缘变窄）；流光/空管/传送带物品
# 由 STRUCT_EDGE、贴边规则和 13 帧并集去掉，所以下限可以低于单帧噪声
STRUCT_MIN = 200
# 「少了」的那块（外扩 8 px，对齐后）和基线的梯度相关要低于这个：管子真没了（边缘消失）；只是颜色变了的边缘还在。
# 2026-11-08 景玉谷离线单帧：断开的出水管 0.34–0.36，颜色变化 0.42–0.71。余量小，实跑靠 13 帧并集先把大部分流光去掉
STRUCT_EDGE = 0.4
STRUCT_THIN = 0.6   # 少了的那块里 ≥ 这个比例的像素横跨管子方向 ±STRUCT_THIN_R 内还有水流 → 水流变细，不是断开
STRUCT_THIN_R = 10


def struct_masks(img) -> np.ndarray:
    """管道（浅蓝）和传送带（黄）掩码，2 通道 uint8（0/1）。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    # 空管（没水）发暗：H 90–98、V 85–158；满管 V 116–218。断口露出的草地 H≈70、V≈58（2026-11-08 景玉谷实测）。
    # V 下限原来 140，断水后洪炉旁的空管被当成「管道少了」（误报），改 80
    pipe = (h >= 85) & (h <= 110) & (s >= 50) & (v >= 80)
    belt = (h >= 18) & (h <= 35) & (s >= 120) & (v >= 150)
    k = np.ones((7, 7), np.uint8)
    return np.stack([cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, k) for m in (pipe, belt)], axis=-1)


def _valid_full() -> np.ndarray:
    vm = np.zeros((720, 1280), np.uint8); vm[80:540, 260:1100] = 1; vm[260:540, 0:260] = 1
    return vm


def _crop(arr, u, fill=0):
    """以 u 为中心裁 (2·hy, 2·hx)，出界处补 fill。"""
    hx, hy = STRUCT_HALF
    x0, y0 = int(round(u[0])) - hx, int(round(u[1])) - hy
    shape = (2 * hy, 2 * hx) + arr.shape[2:]
    out = np.full(shape, fill, arr.dtype)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(arr.shape[1], x0 + 2 * hx), min(arr.shape[0], y0 + 2 * hy)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = arr[sy0:sy1, sx0:sx1]
    return out


def baseline_dir(blueprint: str, area: str) -> Path:
    return archive_dir(blueprint) / "ledger" / "near_baseline" / area


def _bkey(e) -> str:
    return f"{e['name']}_{int(round(e['far'][0]))}_{int(round(e['far'][1]))}"


def compare_struct(cur: dict, base: dict) -> dict:
    """cur/base：{"m": 2 通道并集掩码, "valid": 有效区, "badge": 标记处（不比）, "img": 首帧裁图}。返回 {lost_px, boxes, shift}。"""
    a = (base["m"].max(axis=-1)).astype(np.float32)
    b = (cur["m"].max(axis=-1)).astype(np.float32)
    (dx, dy), resp = cv2.phaseCorrelate(a, b)
    if abs(dx) > 20 or abs(dy) > 20 or resp < 0.05:
        dx = dy = 0.0
    M = np.float32([[1, 0, -dx], [0, 1, -dy]])  # 当前 → 基线坐标
    H, W = a.shape
    cm = cv2.warpAffine(cur["m"], M, (W, H), flags=cv2.INTER_NEAREST)
    cv_ = cv2.warpAffine(cur["valid"], M, (W, H), flags=cv2.INTER_NEAREST)
    cb = cv2.warpAffine(cur["badge"], M, (W, H), flags=cv2.INTER_NEAREST)
    keep = (base["valid"] > 0) & (cv_ > 0) & (base["badge"] == 0) & (cb == 0)
    cm_d = cv2.dilate(cm, np.ones((5, 5), np.uint8))  # 2 px 容差
    lost = ((base["m"] > 0) & (cm_d == 0)).any(axis=-1) & keep
    lost = cv2.morphologyEx(lost.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(lost)
    # 管道颜色会变（流光、水量变了发暗），那一截掩码里没有，但管子还在：边缘形状和基线一样。真断开处管子的边缘没了。
    def _grad(im):
        g = cv2.GaussianBlur(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32), (3, 3), 0)
        return cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))

    gb, gc = _grad(base["img"]), _grad(cv2.warpAffine(cur["img"], M, (W, H)))

    def _edge_same(i):
        x, y, w, h = st[i][:4]
        x0, y0, x1, y1 = max(0, x - 8), max(0, y - 8), min(W, x + w + 8), min(H, y + h + 8)
        a, b = gb[y0:y1, x0:x1].ravel(), gc[y0:y1, x0:x1].ravel()
        if a.std() < 1e-3 or b.std() < 1e-3:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

    # 贴着裁图 / 有效区边缘的块不算：结构只露出一截，传送带上的物品一动就像「少了」（2026-11-08 水泵 (1313,241) 裁图底边）
    edge = cv2.dilate((1 - keep.astype(np.uint8)), np.ones((13, 13), np.uint8))
    edge[:6, :] = 1; edge[-6:, :] = 1; edge[:, :6] = 1; edge[:, -6:] = 1

    def _touch_edge(i):
        return bool(edge[lab == i].any())

    # 水流变细：管道有容量，流量小时水流变细、两边露出透明管壁（看到的是底下的地面），掩码里管子两侧少了一条，但横跨管子方向
    # 旁边还有水流；真断开是整个宽度一起没了，横向旁边没有水（用户 2026-11-08 三张截图：满管全蓝、流量小时水流变细）
    bp, cp = base["m"][..., 0] > 0, cm[..., 0] > 0

    def _thinned(i):
        ys, xs = np.nonzero(lab == i)
        cy, cx = int(ys.mean()), int(xs.mean())
        w = bp[max(0, cy - 20):cy + 21, max(0, cx - 20):cx + 21]
        if not w.any():
            return False
        horiz = w.any(axis=0).sum() >= w.any(axis=1).sum()  # 基线里这段管子是横的：横跨方向 = 竖直
        r = STRUCT_THIN_R
        if horiz:
            near = np.array([cp[max(0, y - r):y + r + 1, x].any() for y, x in zip(ys, xs)])
        else:
            near = np.array([cp[y, max(0, x - r):x + r + 1].any() for y, x in zip(ys, xs)])
        return float(near.mean()) >= STRUCT_THIN

    keep_i = [i for i in range(1, n) if st[i][4] >= STRUCT_MIN and not _touch_edge(i) and _edge_same(i) < STRUCT_EDGE and not _thinned(i)]
    boxes = [[int(v) for v in st[i][:4]] for i in keep_i]
    # 断口所在的那根管子（基线掩码里的连通块）通到哪：离裁图中心（本设备标签图标）多近。是本设备自己的管子时很近，
    # 别的设备的管子只是从裁图里经过（2026-11-08：断的是 (1437,443) 的出水管，(1437,541) 的裁图里也看得到）
    _, blab = cv2.connectedComponents((base["m"].max(axis=-1) > 0).astype(np.uint8))
    cyc, cxc = H / 2, W / 2
    own = []
    for i in keep_i:
        ids = np.unique(blab[(lab == i) & (blab > 0)])
        ys, xs = np.nonzero(np.isin(blab, ids)) if len(ids) else (np.array([]), np.array([]))
        own.append(round(float(np.hypot(xs - cxc, ys - cyc).min()), 1) if len(xs) else 1e9)
    return {"lost_px": int(sum(st[i][4] for i in keep_i)), "boxes": boxes, "pipe_to_dev": own,
            "shift": [round(float(dx), 1), round(float(dy), 1)]}


def struct_image(base_img, cur_img, boxes, shift) -> np.ndarray:
    """左：基线；右：这次。红框 = 这次少了的管道/传送带。"""
    b, c = base_img.copy(), cur_img.copy()
    for x, y, w, h in boxes:
        cv2.rectangle(b, (x - 6, y - 6), (x + w + 6, y + h + 6), (0, 0, 255), 2)
        cv2.rectangle(c, (int(x + shift[0]) - 6, int(y + shift[1]) - 6), (int(x + w + shift[0]) + 6, int(y + h + shift[1]) + 6), (0, 0, 255), 2)
    for im, t in ((b, "baseline"), (c, "now")):
        cv2.putText(im, t, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return np.hstack([b, np.full((b.shape[0], 6, 3), 255, np.uint8), c])


def check_struct(blueprint: str, area: str, e: dict, cur: dict, shot_dir: Path) -> dict:
    """和基线比；没有基线就存成基线（不自动覆盖已有的）。返回 {"baseline": "new"|"ok", lost_px, boxes, image}。"""
    d = baseline_dir(blueprint, area)
    f = d / f"{_bkey(e)}.npz"
    if not f.exists():
        d.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(f, m=cur["m"], valid=cur["valid"], badge=cur["badge"], img=cur["img"])
        return {"baseline": "new", "lost_px": 0, "boxes": []}
    z = np.load(f)
    base = {k: z[k] for k in ("m", "valid", "badge", "img")}
    r = compare_struct(cur, base)
    r["baseline"] = "ok"
    if "cam" in cur and "u" in cur:  # 少了的块换到近景起点系，几台设备的裁图看到同一处时去重用
        hx, hy = STRUCT_HALF
        r["at"] = [[round(float(cur["cam"][0] + cur["u"][0] - hx + x + w / 2), 1), round(float(cur["cam"][1] + cur["u"][1] - hy + y + h / 2), 1)]
                   for x, y, w, h in r["boxes"]]
    if r["boxes"]:
        fn = f"struct_{_bkey(e)}.png"
        _wr(shot_dir / fn, struct_image(base["img"], cur["img"], r["boxes"], r["shift"]))
        r["image"] = fn
    return r


def reset_baseline(blueprint: str, area: str) -> dict:
    """任务计划第 8 项「重设基线」：基地改过布局后，旧的结构基线（每台设备周围的管子/带子、输入端外那段管子）会一直报「少了」。
    把这个分区现有的基线整个挪进 near_baseline/_备份/<分区>_<时间>/（不删，挪回去就恢复），下次近景巡检按当时的画面重新存。
    → {moved: 挪走几个文件, backup: 备份目录}；没有基线时 moved=0。"""
    import shutil
    import time as _t
    d = baseline_dir(blueprint, area)
    files = sorted(d.glob("*.npz")) if d.exists() else []
    if not files:
        return {"moved": 0, "backup": None}
    bk = d.parent / "_备份" / f"{area}_{_t.strftime('%Y%m%d-%H%M%S')}"
    bk.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.move(str(f), str(bk / f.name))
    return {"moved": len(files), "backup": str(bk)}


def baseline_info(blueprint: str, area: str) -> dict:
    """这个分区的结构基线：几个文件、最早 / 最晚存的时间。"""
    import time as _t
    d = baseline_dir(blueprint, area)
    files = sorted(d.glob("*.npz")) if d.exists() else []
    if not files:
        return {"area": area, "count": 0}
    ts = [f.stat().st_mtime for f in files]
    fmt = lambda t: _t.strftime("%Y-%m-%d %H:%M", _t.localtime(t))  # noqa: E731
    return {"area": area, "count": len(files), "oldest": fmt(min(ts)), "newest": fmt(max(ts))}


def seed_baseline_from_run(blueprint: str, patrol_path: Path, overwrite: bool = False, log=print) -> int:
    """用一次已跑过的近景巡检（patrol JSON + 存下的原始帧）补存结构基线：每个视点用存下的几帧取并集。
    用在功能上线前已跑过、确认正常的那次。返回存了几台。"""
    pj = json.loads(Path(patrol_path).read_text(encoding="utf-8"))
    area, near = pj["area"], pj.get("near") or {}
    shot_dir = ROOT / pj["shots"]
    targets, _, _, _ = load_targets(blueprint, area, sorted({r["name"] for r in near.get("rows", [])}))
    by_key = {_bkey(t): t for t in targets}
    cams = {v["view"]: np.array(v["cam"], float) for v in near.get("views", [])}
    d = baseline_dir(blueprint, area)
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in near.get("rows", []):
        e = by_key.get(f"{r['name']}_{r['far'][0]}_{r['far'][1]}")
        if e is None or not r.get("id_ok") or r.get("view") not in cams:
            continue
        f = d / f"{_bkey(e)}.npz"
        if f.exists() and not overwrite:
            continue
        frames = sorted(p for p in shot_dir.glob(f"v{r['view']:02d}_f*.png"))
        if not frames:
            continue
        u = e["q"] - cams[r["view"]]
        cur = None
        for fp in frames:
            img = imread(fp)
            bm = np.zeros((720, 1280), np.uint8)
            for _, bx, by in find_badges(img):
                cv2.circle(bm, (bx, by), 24, 1, -1)
            if cur is None:
                cur = {"m": _crop(struct_masks(img), u), "badge": _crop(bm, u), "valid": _crop(_valid_full(), u), "img": _crop(img, u)}
            else:
                cur["m"] |= _crop(struct_masks(img), u); cur["badge"] |= _crop(bm, u)
        np.savez_compressed(f, m=cur["m"], valid=cur["valid"], badge=cur["badge"], img=cur["img"])
        n += 1
    log(f"== {area}：从 {Path(patrol_path).name} 补存结构基线 {n} 台")
    return n


# ---------------------------------------------------------------- 判读（缺气 + 设计说明；阻塞/空闲交给 combine）
def device_design(playbook: dict, area: str, name: str, far, kind: str) -> str | None:
    for d in playbook.get("device_designs", []):
        if d.get("area") != area or d.get("name") != name or kind not in d.get("kinds", ["idle", "strike"]):
            continue
        if "x" in d and (abs(d["x"] - far[0]) > 15 or abs(d["y"] - far[1]) > 15):
            continue
        return d["note"]
    return None


def design_expect(playbook: dict, area: str, name: str, far) -> dict | None:
    """导入的蓝图分析里这台设备的设计开工率（作者没确认过的，只当参考、不改判）：{rate, state, recipe}。"""
    for d in playbook.get("bpa_expect", []):
        if d.get("area") == area and d.get("name") == name and abs(d.get("x", 1e9) - far[0]) <= 15 and abs(d.get("y", 1e9) - far[1]) <= 15:
            return d
    return None


SOURCE_FLUID = {"水泵": "清水", "二型耐酸水泵": "沉积酸"}


def bpa_base_dir(blueprint: str, area: str) -> Path | None:
    """这套档案对应的蓝图分析项目里，这个据点的拼合结果目录（composite / logistics / inputs_auto）。最近改过的优先。"""
    best = None
    for pj in (ROOT / "debug" / "bpa").glob("*/project.json"):
        try:
            if json.loads(pj.read_text(encoding="utf-8")).get("archive") != blueprint:
                continue
        except Exception:  # noqa: BLE001
            continue
        d = pj.parent / "base" / area
        if (d / "composite.png").exists() and (d / "inputs_auto.json").exists():
            if best is None or d.stat().st_mtime > best.stat().st_mtime:
                best = d
    if best is None:  # 从作者的蓝图分析 zip 导入的档案：拼合结果在档案自己的 bpa/ 下（任务计划第 2 项）
        from bpa import importer
        best = importer.base_dir(blueprint, area)
    return best


GATE_REACH = 95  # 近景 px：水泵管道的末端离输入端（拼合图换算过来）多近算接上（换算后准入口和画面里的节点差约 48 px）


def pump_gate(blueprint: str, area: str, e: dict, F2N: np.ndarray, fluid: str = "清水") -> tuple[int | None, str]:
    """没断开的水泵进的是哪个输入端：在它的结构基线裁图里，从机身出发、基地外那段管道的末端，离哪个输入端最近。
    管道出了裁图（泵离基地远）时，按出画面的方向找下一个输入端，标「推断」。返回 (输入端编号, 说明)。
    2026-11-08 景玉谷：(1313,241)→1、(1357,345)→2、(1437,443)→3（和断口对上的一致）、(1437,541)→4、(1436,640) 管子往下出了画面。"""
    f = baseline_dir(blueprint, area) / f"{_bkey(e)}.npz"
    bd = bpa_base_dir(blueprint, area)
    if not f.exists() or bd is None:
        return None, "没有结构基线或蓝图拼合结果"
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    from bpa import pipenet as PN
    inp = [m for m in PN.load_inputs(bd) if (m.get("item") or "") == fluid]
    if not inp:
        return None, f"据点没有{fluid}输入端"
    hx, hy = STRUCT_HALF
    org = np.asarray(e["q"], float) - np.array([hx, hy])  # 裁图 (0,0) 在近景起点系里的位置
    gates = [(m["no"], F2N @ np.array([tf["F0"][0] + m["p"][0] * tf["K"], tf["F0"][1] + m["p"][1] * tf["K"], 1.0]) - org) for m in inp]
    z = np.load(f)
    pipe = z["m"][..., 0].astype(np.uint8)
    inside = [p for _, p in gates if -60 <= p[0] <= 2 * hx + 60 and -60 <= p[1] <= 2 * hy + 60]
    edge_x = min((p[0] for p in inside), default=None)
    if edge_x is not None:
        pipe[:, :max(0, int(edge_x) - 12)] = 0  # 基地里面的管网不追（准入口所在的竖边以内）
    _, lab = cv2.connectedComponents(pipe)
    ys, xs = np.nonzero(lab)
    if not len(xs):
        return None, "裁图里没认出管道"
    d0 = np.hypot(xs - hx, ys - hy)
    ids = np.unique(lab[ys[d0 <= 70], xs[d0 <= 70]])
    if not len(ids):
        return None, "机身旁没有管道"
    yy, xx = np.nonzero(np.isin(lab, ids))
    # 末端 = 离机身最远的点（管子从泵出发通到准入口）
    far_i = int(np.hypot(xx - hx, yy - hy).argmax())
    end = np.array([xx[far_i], yy[far_i]], float)
    dists = sorted((float(np.min(np.hypot(xx - p[0], yy - p[1]))), no, p) for no, p in gates)
    if dists[0][0] <= GATE_REACH:
        return dists[0][1], f"管道通到输入端 {dists[0][1]}（{dists[0][0]:.0f} px）"
    H, W = pipe.shape
    if end[0] <= 8 or end[1] <= 8 or end[0] >= W - 9 or end[1] >= H - 9:
        v = end - np.array([hx, hy])
        ahead = [(float(np.hypot(*(p - end))), no) for no, p in gates if float(np.dot(p - end, v)) > 0]
        if ahead:
            dd, no = min(ahead)
            return no, f"管道出了画面，按方向推断是输入端 {no}（离出口 {dd:.0f} px）"
    return None, f"管道末端离最近的输入端 {dists[0][0]:.0f} px，对不上"


_area_check_cache: dict = {}
GAP_WATER = 0.85  # 断口这段在近景图里 ≥ 这个比例的采样点是水管色，才算游戏里连着


def water_color(hsv: np.ndarray) -> np.ndarray:
    """近景图里清水管的颜色（满管浅蓝、空管发暗、带光晕时发白）。"""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (h >= 82) & (h <= 110) & (((s >= 50) & (v >= 95)) | ((s >= 22) & (v >= 150)))


_near_map_cache: dict = {}


def _near_map(blueprint: str, area: str):
    """近景大图 + 拼合图点 → 大图坐标的换算（同一进程缓存）。没有拼合结果或扫描图时返回 None。"""
    from bpa import nearmap as NM
    led = json.loads((archive_dir(blueprint) / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
    scan_dir = ROOT / led["scan"]["dir"]
    bd = bpa_base_dir(blueprint, area)
    pre = NM.load(bd / "nearmap.png") if bd is not None else None  # 从蓝图分析 zip 导入的：作者那边拼好的近景大图（无损，颜色和作者一致）
    if bd is None or (pre is None and not (scan_dir / "scan.json").exists()):
        return None
    key = (str(bd), str(scan_dir), (bd / "composite.png").stat().st_mtime)
    if key not in _near_map_cache:
        tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
        m, F2N = pre if pre is not None else (NM.build(scan_dir, scale=0.5), calib(scan_dir))
        A = np.array([[tf["K"], 0, tf["F0"][0]], [0, tf["K"], tf["F0"][1]], [0, 0, 1.0]])
        M = np.vstack([F2N, [0, 0, 1]]) @ A  # 拼合图 → 近景起点系
        M = (np.diag([m["scale"], m["scale"], 1.0]) @ np.array([[1, 0, -m["origin"][0]], [0, 1, -m["origin"][1]], [0, 0, 1.0]]) @ M)[:2]
        hsv = cv2.cvtColor(m["img"], cv2.COLOR_BGR2HSV)
        _near_map_cache.clear()
        _near_map_cache[key] = {"bd": bd, "hsv": hsv, "valid": m["count"] > 0, "M": M}
    return _near_map_cache[key]


def gap_checker(blueprint: str, area: str, log=None):
    """拼合图上两点之间在游戏近景图里是不是一根连着的水管：拼合图 → 远景 → 近景起点系 → 近景大图，沿线采样看颜色。
    只在拼合图认为「这里像断口」的地方看，屋顶、通道、洪炉内部这些蓝色的东西碰不到。"""
    nm = _near_map(blueprint, area)
    if nm is None:
        return None
    water = water_color(nm["hsv"]) & nm["valid"]
    M = nm["M"]
    Hm, Wm = water.shape

    def to_map(p):
        return M @ np.array([p[0], p[1], 1.0])

    def fn(pa, pb):
        a, b = to_map(pa), to_map(pb)
        n = max(6, int(np.hypot(*(b - a)) / 2))
        hits = 0
        for t in np.linspace(0, 1, n):
            x, y = (a + (b - a) * t).round().astype(int)
            if not (0 <= x < Wm and 0 <= y < Hm):
                return False
            hits += bool(water[max(0, y - 2):y + 3, max(0, x - 2):x + 3].any())
        ok = hits / n >= GAP_WATER
        if log is not None:
            log.append({"pa": [int(v) for v in pa], "pb": [int(v) for v in pb], "frac": round(hits / n, 2), "ok": ok})
        return ok

    return fn


SEWAGE_MIN = 40  # 一块管子在近景图里至少这么多个深紫色采样点、且是蓝色点的 2 倍以上，才定成污水管
SHIFT_R, SHIFT_STEP = 12, 4  # 近景图像素：定污水前在这个范围内平移复核（局部对不准的容差，约半格）


def sewage_color(hsv: np.ndarray) -> np.ndarray:
    """近景图里污水管的颜色：深紫（精炼炉排出、去废水处理机的管子）。清水是饱和的蓝，惰气是偏白的浅青。"""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (h >= 115) & (h <= 150) & (s >= 35) & (v >= 35) & (v <= 130)


TEAL_FRAC, YELLOW_FRAC = 0.2, 0.3  # 深青 / 黄色采样点占这块管子的比例下限（周围的黄带子、标签会混进一些黄点）
# 黄色管（沉积酸/液化息壤）不自动定：近景里和传送带、黄黑警示条、物品图标同一个色相，2026-11-10 三个基地标出 10 块黄管，
# 核对下来几乎全是压着传送带/警示条的管子碎块（景玉谷没有黄色流体却标了 4 块）。沉积酸的网靠输入端定流体。
USE_YELLOW = False


def teal_color(hsv: np.ndarray) -> np.ndarray:
    """壤晶废液 / 惰性壤晶废液管：深青（任务计划第 6 项，2026-11-10 武陵城提纯机旁近景实测 H 87–100、S 75–160、V 62–118，
    清水是 H 100–103、S 170–190、V 120–150；IndustrialPlanner 美术表 壤晶废液 #11433b / #00ceff）。
    色相上限 97：压在「仓库取货口」标签底下的水管（标签暗底 + 蓝光）是 H 99–101 的暗青，按 101 会把武陵城三台精炼炉的水管
    当成壤晶废液、清水网从 24/24 掉到 21/24；真的壤晶废液管中位 H 93–95。"""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (h >= 84) & (h <= 97) & (s >= 80) & (v >= 40) & (v <= 105)


def yellow_color(hsv: np.ndarray) -> np.ndarray:
    """沉积酸 / 液化息壤管：金黄（2026-11-10 应龙关液气转化机旁沉积酸管 H 17–28、S 77–160、V 120–195；
    美术表 沉积酸 #ffeea0 / #ffd200、液化息壤 #fff699），两种颜色分不开，只定到「黄色管」这一族。"""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return (h >= 16) & (h <= 30) & (s >= 70) & (v >= 110)


def fluid_checker(blueprint: str, area: str):
    """按近景图颜色给拼合图的管网块定流体：深紫色为主的块是污水；深青为主是壤晶废液一族、黄色为主是沉积酸/液化息壤一族（第 6 项）。
    返回 fn(lab) -> {块号: 流体或族名}。（2026-11-08 用户：精炼炉做赤铜块的配方会排污水，不要和清水混；清水只算蓝色管。
    武陵城的污水管原来被暗管、断口并进了清水网）。有输入端的块按输入端的流体（pipenet 先填输入端，颜色只补没有的）。"""
    nm = _near_map(blueprint, area)
    if nm is None:
        return None
    hsv, M, valid = nm["hsv"], nm["M"], nm["valid"]
    Hm, Wm = valid.shape
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    blue = (h >= 85) & (h <= 112) & (s >= 50) & (v >= 80) & valid
    blue_bright = (h >= 85) & (h <= 112) & (s >= 50) & (v >= 115) & valid
    purple = sewage_color(hsv) & valid
    teal = teal_color(hsv) & valid
    yellow = yellow_color(hsv) & valid

    def fn(lab):
        from bpa.pipenet import FLUID_FAMILIES
        teal_name, yellow_name = (next(k for k in FLUID_FAMILIES if w_ in k) for w_ in ("深青", "黄色"))
        n = int(lab.max()) + 1
        ys, xs = np.nonzero(lab)
        u = np.rint(M[0, 0] * xs + M[0, 1] * ys + M[0, 2]).astype(int)
        w = np.rint(M[1, 0] * xs + M[1, 1] * ys + M[1, 2]).astype(int)
        ok = (u >= 0) & (u < Wm) & (w >= 0) & (w < Hm)
        L = lab[ys[ok], xs[ok]]
        tot = np.bincount(L, minlength=n)
        cnt = {k: np.bincount(L, weights=m[w[ok], u[ok]], minlength=n) for k, m in
               (("b", blue), ("bb", blue_bright), ("p", purple), ("t", teal), ("y", yellow))}
        # 近景图和拼合图局部对不准（一张蓝图拼在基地上偏了半格）时，并排的污水管、水管会互相看成对方：
        # 平移 ±SHIFT_R 像素里，只要有一处明显是蓝管（蓝 > 2×紫），就不按颜色定成污水，交给连通关系（接在哪个输入端上）。
        # 2026-11-10 武陵城扩容反应池 (1489,1055) 的进水管投到了旁边的污水管上（紫 270、蓝 0），被定成污水，清水网 24/25
        bluer = np.zeros(n, bool)
        for dx in range(-SHIFT_R, SHIFT_R + 1, SHIFT_STEP):
            for dy in range(-SHIFT_R, SHIFT_R + 1, SHIFT_STEP):
                if dx == 0 and dy == 0:
                    continue
                uu, ww = u + dx, w + dy
                ok2 = (uu >= 0) & (uu < Wm) & (ww >= 0) & (ww < Hm)
                L2 = lab[ys[ok2], xs[ok2]]
                b2 = np.bincount(L2, weights=blue[ww[ok2], uu[ok2]], minlength=n)
                p2 = np.bincount(L2, weights=purple[ww[ok2], uu[ok2]], minlength=n)
                bluer |= (b2 >= SEWAGE_MIN) & (b2 > 2 * p2)
        out = {}
        for c in range(1, n):
            if cnt["p"][c] >= SEWAGE_MIN and cnt["p"][c] > 2 * cnt["b"][c]:
                out[c] = "疑似污水" if bluer[c] else "污水"
            elif cnt["t"][c] >= SEWAGE_MIN and cnt["t"][c] >= TEAL_FRAC * tot[c] and cnt["t"][c] > 3 * cnt["bb"][c] and cnt["t"][c] > cnt["y"][c]:
                out[c] = teal_name
            elif USE_YELLOW and cnt["y"][c] >= 60 and cnt["y"][c] >= YELLOW_FRAC * tot[c] and cnt["y"][c] > 3 * cnt["bb"][c]:
                out[c] = yellow_name
        return out

    return fn


_registered: set = set()


def register_near_checks(blueprint: str, area: str) -> None:
    """管网追踪用上游戏近景图：断口核实（拼合图断开、游戏里连着的接上）+ 管子流体（污水管不并进清水网）。
    2026-11-08 用户确认武陵城那串断口游戏里是连着的，默认启用。"""
    from bpa import pipenet as PN
    bd = bpa_base_dir(blueprint, area)
    if bd is None or str(bd) in _registered:
        return
    _registered.add(str(bd))
    g, f = gap_checker(blueprint, area), fluid_checker(blueprint, area)
    if g is not None:
        PN.set_gap_check(bd, g)
    if f is not None:
        PN.set_fluid_check(bd, f)


def network_trusted(blueprint: str, area: str, fluid: str = "清水") -> tuple[bool, str]:
    """这个据点的管网追踪能不能用（bpa.pipenet.area_check 闭合自检，同一进程里缓存）。"""
    from bpa import pipenet as PN
    bd = bpa_base_dir(blueprint, area)
    if bd is None:
        return False, "没有这个据点的蓝图拼合结果"
    register_near_checks(blueprint, area)
    key = (str(bd), fluid, (bd / "composite.png").stat().st_mtime)
    if key not in _area_check_cache:
        led = json.loads((archive_dir(blueprint) / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
        rp = archive_dir(blueprint) / "ledger" / f"{area}_recipes.json"
        recs = json.loads(rp.read_text(encoding="utf-8"))["devices"] if rp.exists() else []
        _area_check_cache[key] = PN.area_check(bd, led, recs, fluid)
    r = _area_check_cache[key]
    why = "" if r["trusted"] else f"管网追踪没闭合（{r['coverage'][0]}/{r['coverage'][1]} 台用{fluid}的设备落到网上；{'；'.join(r['problems'])[:160]}）"
    return r["trusted"], why


def own_network_note(blueprint: str, area: str, row: dict, rows: list[dict], reg=None) -> tuple[str, str] | None:
    """持续阻塞、没有断开的水泵：按它自己那张管网判（用户 2026-11-08：不能拿全地区清水总量说供大于求）。
    返回 (等级, 说明)：这张网有余量 → 正常；零余量但下游产物爆仓 → 正常；零余量、下游也不爆仓 → 待确认。"""
    from bpa import pipenet as PN
    fluid = SOURCE_FLUID.get(row["name"])
    if not fluid:
        return None
    ok, why = network_trusted(blueprint, area, fluid)
    if not ok:
        return None  # 管网不可信：保留合并诊断原来的判断（api.attach_near 会注明没按自己那张网核对）
    targets, _, _, F2N = load_targets(blueprint, area, [row["name"]])
    e = next((t for t in targets if _bkey(t) == f"{row['name']}_{row['far'][0]}_{row['far'][1]}"), None)
    if e is None:
        return None
    no, how = pump_gate(blueprint, area, e, F2N, fluid)
    if no is None:
        return "待确认", f"一直阻塞，但没追到它进的是哪个输入端（{how}），判断不了是不是供大于求"
    bd = bpa_base_dir(blueprint, area)
    led = json.loads((archive_dir(blueprint) / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
    rp = archive_dir(blueprint) / "ledger" / f"{area}_recipes.json"
    recs = json.loads(rp.read_text(encoding="utf-8"))["devices"] if rp.exists() else []
    register_near_checks(blueprint, area)
    net = PN.network(bd, led, recs, no, fluid=fluid)
    if not net or net.get("note"):
        return "待确认", f"进输入端 {no}（{how}），但这张管网没认出来"
    users = "、".join(f"{u['name']} ({u['far'][0]},{u['far'][1]}) {u['need']:g}/分" for u in net["users"]) or "没认出下游设备"
    head = f"{how}；这张网 {len(net['same_gates'])} 个进水口共 {net['supply']:g}/分，下游 {users}，设计合计 {net['demand']:g}/分"
    if not net["users"]:
        return "待确认", head + "：下游一台都没认出，判断不了"
    if net["demand"] < net["supply"]:
        return "正常", head + f"，有 {net['supply'] - net['demand']:g}/分 余量，泵堵是供大于求"
    full = []
    if reg is not None:
        full = sorted({u["product"] for u in net["users"] if (it := reg.by_label.get(u["product"])) is not None and reg.full(it)})
    if full:
        return "正常", head + f"，零余量；现在 {'、'.join(full)} 爆仓，下游用{fluid}少，泵在等（库存降下来就会全开）"
    # 零余量又没爆仓：看下游这次的状态。阻塞标记只说明流量小于泵的产量，不是一点没流；零余量网里下游稍微少用一点泵就会堵。
    # 真断管时下游会缺水空闲（2026-11-08 断开输入端 3 时那 3 台洪炉 9/13、5/13、8/13 帧空闲）
    seen = {(r["name"], r["far"][0], r["far"][1]): r for r in rows}
    obs = [(u, seen.get((u["name"], u["far"][0], u["far"][1]))) for u in net["users"]]
    idle = [u for u, r in obs if r is not None and r["counts"].get("idle", 0) >= 3]
    unseen = [u for u, r in obs if r is None]
    if idle:
        return "待确认", head + f"，零余量，下游 {'、'.join(f'{u['name']} ({u['far'][0]},{u['far'][1]})' for u in idle)} 在空闲、泵却一直堵：水可能没送到，查这台泵到输入端 {no} 的管子"
    if unseen:
        return "待确认", head + f"，零余量，下游 {len(unseen)} 台这次没看（{'、'.join(u['name'] for u in unseen)}），判断不了"
    return "正常", head + f"，零余量；下游 {len(obs)} 台这次都没有空闲（在正常用{fluid}），泵堵是下游用得略少于设计（阻塞只说明流量小于泵的产量）"


def describe_impact(blueprint: str, area: str, row: dict, rows: list[dict], reg=None) -> tuple[str, np.ndarray | None, dict | None]:
    """源头设备（水泵）出水管断开的影响范围：进的哪个输入端、同网还有谁供、下游设备和设计用量、这次巡检看到它们什么状态。"""
    from bpa import pipenet as PN
    gate_no = row.get("gate_no")
    fluid = row.get("fluid", "清水") if gate_no is not None else SOURCE_FLUID.get(row["name"])
    if gate_no is not None and fluid != "清水":
        return "", None, None  # 管网追踪和用量只核对过清水
    at = ((row.get("struct") or {}).get("at_far") or [None])[0]
    if not fluid or (at is None and gate_no is None):
        return "", None, None
    bd = bpa_base_dir(blueprint, area)
    if bd is None:
        return "影响范围：没有这个据点的蓝图拼合结果（先在「蓝图分析」里读蓝图并拼合），追不了管网", None, None
    led = json.loads((archive_dir(blueprint) / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
    rp = archive_dir(blueprint) / "ledger" / f"{area}_recipes.json"
    recs = json.loads(rp.read_text(encoding="utf-8"))["devices"] if rp.exists() else []
    register_near_checks(blueprint, area)
    imp = PN.network(bd, led, recs, gate_no, fluid=fluid) if gate_no is not None else PN.impact(bd, led, recs, tuple(at), fluid=fluid)
    if not imp or not imp.get("gate"):
        return f"影响范围：{(imp or {}).get('note', '断点对不上输入端')}", None, imp
    trusted, why = network_trusted(blueprint, area, fluid)
    seen = {(r["name"], r["far"][0], r["far"][1]): r for r in rows}
    parts = []
    for u in imp["users"]:
        r = seen.get((u["name"], u["far"][0], u["far"][1]))
        state = ""
        if r is not None:
            c, n = r["counts"], r["frames"]
            state = "，这次" + ("、".join(f"{KIND_CN[k]} {c[k]}/{n}" for k in ("idle", "blocked", "strike") if c.get(k)) or f"{n} 帧无标记")
        parts.append(f"{u['name']} ({u['far'][0]},{u['far'][1]}) {u['need']:g}/分 做{u['product']}{'（配方按同类推断）' if u.get('recipe_guessed') else ''}{state}")
    others = [g for g in imp["same_gates"] if g["no"] != imp["gate"]]
    short = imp["demand"] - imp["supply_after"]
    txt = ((f"影响范围：输入端 {imp['gate']}（{fluid}）没水进来" if gate_no is not None else f"影响范围：这根出水管进的是输入端 {imp['gate']}（{fluid}）")
           + (f"，同一张管网还有输入端 {'、'.join(str(g['no']) for g in others)}" if others else "，这张管网只有它一个进水口")
           + f"。下游 {len(imp['users'])} 台：" + "；".join(parts)
           + f"。设计用量合计 {imp['demand']:g}/分，原来供 {imp['supply']:g}/分，断开后只剩 {imp['supply_after']:g}/分")
    if short > 0:
        n_dev = short / max(u["need"] for u in imp["users"]) if imp["users"] else 0
        txt += f"，缺 {short:g}/分（约 {n_dev:g} 台设备的用量会没水）"
    else:
        txt += "，剩下的还够用"
    idle = [u for u in imp["users"] if (r := seen.get((u["name"], u["far"][0], u["far"][1]))) is not None and r["counts"].get("idle", 0) >= 3]
    if idle:
        # 2026-11-08：断开后这张网上 3 台洪炉 9/13、4/13、9/13 帧空闲（断开前没有空闲）——缺水已经显现
        txt += f"。已经看到影响：下游 {len(idle)} 台出现空闲（{'、'.join(f'{u['name']} ({u['far'][0]},{u['far'][1]})' for u in idle)}），是缺{fluid}的迹象"
    elif reg is not None and short > 0:
        full = sorted({u["product"] for u in imp["users"] if (it := reg.by_label.get(u["product"])) is not None and reg.full(it)})
        if full:
            txt += f"。现在 {'、'.join(full)} 爆仓，这些设备用水少，影响暂时显现不出来；库存一降下来就会缺水空闲"
    if imp.get("producers"):
        txt += "。网上另有产" + fluid + "的设备：" + "、".join(f"{p_['name']} ({p_['far'][0]},{p_['far'][1]}) {p_['rate']:g}/分" for p_ in imp["producers"])
    if imp.get("external_tunnels"):
        txt += f"。另有 {len(imp['external_tunnels'])} 个暗管出口从基地外直接进水（量不知道），上面的供给只是下限"
    lost_out: dict = {}
    pri = _priority_cut(blueprint, area, bd, imp["gate"], fluid, lost_out) if gate_no is None or gate_no == imp["gate"] else None
    if pri is not None:
        if "减产的" in pri and "，剩下的还够用" in txt:
            # 2026-11-10 断水实测：整张网总量 780 ≥ 720「还够用」，但池子在分流器后面，断的正是它那一路
            txt = txt.replace("，剩下的还够用", "，按整张网总量算还够用")
            pri = "但总量够不等于每台都有：" + pri
        txt += "。" + pri
    # 按优先级算会减产的设备（合并诊断里这些设备的条目要注明是缺水停的，见 api.attach_near）
    imp["cut_lost"] = [u for u in lost_out.get("lost", []) if u["rate_after"] + 1e-6 < u["rate_before"]]
    if not trusted:
        txt += f"。注意：这个据点{why}，下游列表和用量可能不全，以游戏里为准"
    img = None
    try:
        img = PN.impact_image(bd, imp)
    except Exception:  # noqa: BLE001
        img = None
    return txt, img, imp


def _priority_cut(blueprint: str, area: str, bd, gate_no, fluid: str, out: dict | None = None) -> str | None:
    """任务计划第 9 项：按分流优先级（设计分析计算器：主路先、1 进 1 出汇流器后的备路只拿剩的）算这个输入端断了谁先没水。
    算不出来返回 None（不影响原来按整张网总量的说法）。"""
    if gate_no is None:
        return None
    try:
        from bpa import design as DS
        from bpa import pipenet as PN
        led = json.loads((archive_dir(blueprint) / "ledger" / f"{area}.json").read_text(encoding="utf-8"))
        rp = archive_dir(blueprint) / "ledger" / f"{area}_recipes.json"
        recs = json.loads(rp.read_text(encoding="utf-8"))["devices"] if rp.exists() else []
        glp = archive_dir(blueprint) / "ledger" / "gate_limits" / f"{area}.json"
        gl = json.loads(glp.read_text(encoding="utf-8")) if glp.exists() else None
        lost = DS.cut_impact(bd, led, recs, gl, PN.load_inputs(bd), gate_no, fluid)
    except Exception as e:  # noqa: BLE001
        print(f"  按分流优先级算断水影响失败：{type(e).__name__}: {e}")
        return None
    if out is not None:
        out["lost"] = lost
    if not lost:
        return f"按分流优先级算：断开后没有设备少{fluid}（同网别的口顶上了）"
    drop = [u for u in lost if u["rate_after"] + 1e-6 < u["rate_before"]]
    idle = len(lost) - len(drop)  # 本来就因为别的原料没在转的，少了水也不减产
    if not drop:
        return f"按分流优先级算：断开后有 {len(lost)} 台少{fluid}，但它们本来就因为别的原料没在转，不减产"
    return f"按分流优先级算（主路先、备路只拿剩的），断开后减产的：" + "；".join(
        f"{u['name']} ({u['x']},{u['y']}) {fluid} {u['before']:g}→{u['after']:g}/分，开工 {u['rate_before']:.0%}→{u['rate_after']:.0%}" for u in drop[:8])         + (f" 等 {len(drop)} 台" if len(drop) > 8 else "") + (f"（另有 {idle} 台少{fluid}但本来就没在转）" if idle else "")


def dedupe_struct(rows: list[dict], tol: float = 40.0) -> None:
    """几台设备的裁图看到同一处断开时，只算在「状态也异常」（持续阻塞/空闲）的那台上；其余标 struct_dup_of，不再单独报。
    （2026-11-08：断开的是水泵 (1437,443) 的出水管，下面那台 (1437,541) 的裁图也看到了同一处）"""
    def persistent(r):
        n = max(1, r.get("frames", 1))
        return any(r["counts"].get(k, 0) >= PERSIST * n for k in ("blocked", "idle"))

    # 同一处断开被几台看到：优先算在状态也异常的那台；都异常（或都正常）时算在离断口最近的那台
    # （2026-11-08 实跑：断开后下面那台水泵 (1437,541) 也持续阻塞 12/13，两台都报成问题）
    def dist(r, a):
        q = r.get("q")
        return float(np.hypot(q[0] - a[0], q[1] - a[1])) if q else 1e9

    have = [r for r in rows if (r.get("struct") or {}).get("at")]
    for r in have:
        for a in r["struct"]["at"]:
            group = [o for o in have if any(np.hypot(a[0] - b[0], a[1] - b[1]) <= tol for b in o["struct"]["at"])]
            if len(group) < 2:
                continue
            # 断口那根管子通到本设备旁（≤60 px）的优先：实测自己的出水管 30 px，邻居的 99 px
            owner = min(group, key=lambda o: (min((o["struct"].get("pipe_to_dev") or [1e9])) > 60, not persistent(o), dist(o, a)))
            if owner is not r:
                r["struct_dup_of"] = f"{owner['name']} ({owner['far'][0]}, {owner['far'][1]})"


def judge_near(row: dict, playbook: dict) -> tuple[str, str]:
    """每台目标设备的观察结论（报告里「近景定向巡检」一节）：(等级, 说明)。
    缺气 combine 不管，这里下结论；阻塞/空闲只给观察，结论以合并诊断为准。"""
    if row.get("gate_no") is not None:
        return judge_gate(row)
    n, c, name, area = row["frames"], row["counts"], row["name"], row["area"]
    import blueprint as bpm
    low = bpm.normal_notes(playbook)  # 错题本 normal + 跨地区传输送进来的
    prod = row.get("product")
    if not row["id_ok"]:
        return "待确认", "没认出这台设备（预测位置附近没读到它的标签：可能被挡住、没摆或底账旧了）"
    st = row.get("struct") or {}
    if st.get("lost_px", 0) >= STRUCT_MIN and not row.get("struct_dup_of"):
        blocked = c.get("blocked", 0) >= PERSIST * n
        idle = c.get("idle", 0) >= PERSIST * n
        what = f"旁边的管道/传送带和基线比少了 {len(st['boxes'])} 段（{st['lost_px']} 像素）"
        if blocked:
            return "问题", f"持续阻塞 {c['blocked']}/{n} 帧，且{what}：输出被断开了"
        if idle:
            return "问题", f"持续空闲 {c['idle']}/{n} 帧，且{what}：输入被断开了"
        return "待确认", f"{what}；状态标记没异常，看一眼是不是改过布局"
    if c.get("closed"):
        return "观察", f"关闭 {c['closed']}/{n} 帧（结论见合并诊断）"
    if c.get("strike"):
        note = device_design(playbook, area, name, row["far"], "strike")
        if name == "气体散布机":
            return "问题", f"缺气 {c['strike']}/{n} 帧：散布机没拿到足够的气体（出现就算）"
        if prod in low:
            return "设计内", f"缺气 {c['strike']}/{n} 帧；产物 {prod} 优先级低（{low[prod]}）"
        if note:
            return "设计内", f"缺气 {c['strike']}/{n} 帧；{note}"
        if c["strike"] >= PERSIST * n:
            return "问题", f"持续缺气 {c['strike']}/{n} 帧（低于最低气体流量；产物 {prod or '未知'}）"
        return "待确认", f"断续缺气 {c['strike']}/{n} 帧（产物 {prod or '未知'}）"
    parts = [f"{KIND_CN[k]} {c[k]}/{n}" for k in ("blocked", "idle") if c.get(k)]
    if not parts:
        return "正常", f"{n} 帧都没有标记"
    persist = any(c.get(k, 0) >= PERSIST * n for k in ("blocked", "idle"))
    note = device_design(playbook, area, name, row["far"], "idle") if c.get("idle") else None
    if note:
        return "设计内", "、".join(parts) + f"；{note}"
    return ("观察" if persist else "正常"), ("持续" if persist else "间歇") + "：" + "、".join(parts) + ("（结论见合并诊断）" if persist else "（容量箱/等料周期）")



# ---------------------------------------------------------------- 清水输入端（当水泵看）
# 用户 2026-11-08：基地外的水泵太远，不用扫；把每个清水准入口当一台水泵（60/分），检查它有水进来就行，水泵不用在意。
GATE_SHIFT = np.array([48.0, 6.0])  # 近景 px：拼合图换算过来的输入端位置 + 这个 = 画面里的流体图标（三个据点实测，和输入端在哪条边无关）
GATE_SNAP = 14    # 近景 px：预测位置附近 ±这么多内最近的图标圆环（相邻输入端间距约 34）
GATE_LOOK = 45.0  # 规划视点时以图标外侧这么远的点为目标：图标和外面那段管子都落在有效区
GATE_BOX = (20, 90, 17)  # 图标中心外侧 [20, 90] px、横向 ±17 px（相邻输入端间距约 35）：看接在图标上的那段管子
GATE_LOST = 0.25  # 断开且管道像素比基线少 ≥ 这个比例才报问题（拆掉一格约少一半；只断开没少多少 → 待确认）
GATE_DIM = 0.7    # 管子还连着、但亮度不到基线的这个比例：流变细了（流量变小），报观察


def gate_targets(blueprint: str, area: str, F2N: np.ndarray, fluids: tuple | None = ("清水",)) -> list[dict]:
    """据点拼合图上的流体输入端 → 近景巡检目标（fluids=None：所有标了流体的输入端）。朝外方向按离拼合图哪条边最近。"""
    bd = bpa_base_dir(blueprint, area)
    if bd is None or not (bd / "inputs_auto.json").exists():
        return []
    from bpa import vision as V
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    H, W = V.rd(bd / "composite.png").shape[:2]
    out = []
    from bpa import pipenet as PN
    for m in PN.load_inputs(bd):  # 作者填过的输入端（含每个口的流速）优先
        item = m.get("item") or ""
        if not item or (fluids is not None and item not in fluids):
            continue
        x, y = m["p"]
        d = {(-1, 0): x, (1, 0): W - x, (0, -1): y, (0, 1): H - y}
        o = np.array(min(d, key=d.get), float)
        far = np.array([tf["F0"][0] + x * tf["K"], tf["F0"][1] + y * tf["K"]])
        icon = F2N @ np.array([far[0], far[1], 1.0]) + GATE_SHIFT
        out.append({"name": f"{item}输入端{m['no']}", "gate_no": m["no"], "fluid": item, "rate": m.get("rate"), "far": (float(far[0]), float(far[1])), "icon_q": icon, "out": o,
                    "q": icon + o * GATE_LOOK, "where": "边缘", "recipe": "", "output": None, "recipe_id": None})
    return out


def snap_ring(gray, u_pred):
    """流体图标（清水水滴、气体云、沉积酸金色水滴、壤晶废液绿水滴…）外面都有一圈浅色圆环：预测位置附近找最近的圆。
    （2026-11-08 三个据点 31 个输入端离线：都在 8 px 内）"""
    x0, y0 = int(round(u_pred[0])) - 45, int(round(u_pred[1])) - 35
    if x0 < 0 or y0 < 0 or x0 + 90 > gray.shape[1] or y0 + 70 > gray.shape[0]:
        return None
    cs = cv2.HoughCircles(gray[y0:y0 + 70, x0:x0 + 90], cv2.HOUGH_GRADIENT, dp=1, minDist=26, param1=90, param2=16, minRadius=16, maxRadius=24)
    if cs is None:
        return None
    best = None
    for cx, cy, _ in cs[0]:
        d = np.array([cx + x0, cy + y0]) - u_pred
        if abs(d[0]) <= GATE_SNAP and abs(d[1]) <= GATE_SNAP and (best is None or np.abs(d).sum() < best[0]):
            best = (np.abs(d).sum(), np.array([cx + x0, cy + y0], float))
    return best[1] if best else None


def fluid_pipe_mask(img, fluid: str):
    """近景里这种流体管子的颜色掩码；没有颜色参数的流体返回 None。
    清水饱和蓝、惰气偏白浅青（同一套蓝青色范围，和 struct_masks 的管道一样）；息壤气是带绿光的管子（H 53–54、S 91–118、V 132–138 实测）。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    if fluid in ("清水", "惰气"):
        m = (h >= 85) & (h <= 110) & (s >= 50) & (v >= 80)
    elif fluid == "息壤气":
        m = (h >= 40) & (h <= 66) & (s >= 60) & (v >= 100)
    elif fluid in ("壤晶废液", "惰性壤晶废液"):
        m = teal_color(hsv)  # 深青（任务计划第 6 项实测）；沉积酸/液化息壤的金黄和传送带同色相，不用
    else:
        return None
    return cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))


def find_drops(img) -> list[tuple[float, float]]:
    """近景画面里清水准入口/水泵图标中间的水滴：亮青色、被暗圆环包着（和管道不连）的小块。返回中心列表。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = ((h >= 82) & (h <= 102) & (s >= 110) & (v >= 170)).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        x, y, w, hh, a = st[i]
        if 100 <= a <= 600 and 10 <= w <= 28 and 15 <= hh <= 34 and 1.0 <= hh / max(1, w) <= 2.2:
            out.append((float(cen[i][0]), float(cen[i][1])))
    return out


def snap_gate(drops, u_pred, tol=(30, 16)):
    best = None
    for x, y in drops:
        dx, dy = x - u_pred[0], y - u_pred[1]
        if abs(dx) <= tol[0] and abs(dy) <= tol[1] and (best is None or abs(dx) + abs(dy) < best[0]):
            best = (abs(dx) + abs(dy), np.array([x, y]))
    return best[1] if best else None


def gate_box(o, center):
    """外侧观察框（裁图坐标 x0, y0, x1, y1）和起始带（贴着图标的那 22 px：紧挨图标那段管子发白、不算管道色）。"""
    a, b, w = GATE_BOX
    cx, cy = center
    if o[0]:
        sg = int(o[0])
        x0, x1 = sorted((cx + sg * a, cx + sg * b))
        box = (x0, cy - w, x1, cy + w)
        sx0, sx1 = sorted((cx + sg * a, cx + sg * (a + 22)))
        start = (sx0, cy - w, sx1, cy + w)
    else:
        sg = int(o[1])
        y0, y1 = sorted((cy + sg * a, cy + sg * b))
        box = (cx - w, y0, cx + w, y1)
        sy0, sy1 = sorted((cy + sg * a, cy + sg * (a + 22)))
        start = (cx - w, sy0, cx + w, sy1)
    return box, start


def gate_measure(m_pipe, img, valid, o, center) -> dict:
    """接在图标外侧的那段管子：从贴着图标处一直连到观察框外沿（connected）、管道像素数、亮度中位数。"""
    box, start = gate_box(o, center)
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    sub = m_pipe[y0:y1 + 1, x0:x1 + 1].astype(np.uint8) & valid[y0:y1 + 1, x0:x1 + 1].astype(np.uint8)
    sub &= (cv2.cvtColor(img[y0:y1 + 1, x0:x1 + 1], cv2.COLOR_BGR2HSV)[..., 2] <= 230).astype(np.uint8)  # 去掉旁边的水滴图标（V≈250，满管 116–218）
    # 沿管子方向补 ≤15 px 的小缺口：流动箭头/接头偶尔把单帧的管道色截断（气管的白箭头约 12 px；拆掉的管子至少少一格 ≈35 px，补不上）
    k = np.ones((1, 15), np.uint8) if o[0] else np.ones((15, 1), np.uint8)
    sub = cv2.morphologyEx(sub, cv2.MORPH_CLOSE, k)
    n, lab = cv2.connectedComponents(sub, connectivity=8)
    sx0, sy0, sx1, sy1 = [int(round(v)) - off for v, off in zip(start, (x0, y0, x0, y0))]
    st = set(np.unique(lab[max(0, sy0):sy1 + 1, max(0, sx0):sx1 + 1])) - {0}
    # 外沿：除贴着图标那条边以外的三条边；朝外那条留 12 px 宽（流动箭头正好挡在最外几列时也算连出去）
    if o[0] > 0:
        edges = [lab[:, -12:].ravel(), lab[0, 8:], lab[-1, 8:]]
    elif o[0] < 0:
        edges = [lab[:, :12].ravel(), lab[0, :-8], lab[-1, :-8]]
    elif o[1] > 0:
        edges = [lab[-12:, :].ravel(), lab[8:, 0], lab[8:, -1]]
    else:
        edges = [lab[:12, :].ravel(), lab[:-8, 0], lab[:-8, -1]]
    far_ids = set(np.concatenate(edges).tolist()) - {0}
    # 框有一截伸出有效区（图标靠画面边）：碰到有效区边线也算接出去了（2026-11-08 应龙关 6 号息壤气口某一屏）
    inval = (valid[y0:y1 + 1, x0:x1 + 1] == 0).astype(np.uint8)
    if inval.any():
        far_ids |= set(np.unique(lab[cv2.dilate(inval, np.ones((5, 5), np.uint8)) > 0]).tolist()) - {0}
    conn = bool(st & far_ids)
    hsv = cv2.cvtColor(img[y0:y1 + 1, x0:x1 + 1], cv2.COLOR_BGR2HSV)
    pix = hsv[sub > 0]
    return {"connected": conn, "px": int(sub.sum()), "v": float(np.median(pix[:, 2])) if len(pix) else 0.0,
            "s": float(np.median(pix[:, 1])) if len(pix) else 0.0}


def check_gate(blueprint: str, area: str, e: dict, cur: dict) -> dict:
    """输入端外侧那段管子（按这种流体的管子颜色，多帧并集）：和基线比连通和亮度。基线存 <key>_gate.npz；
    还没有时，清水/惰气用结构基线（同一套蓝青色掩码，2026-11-08 晚存的 22 个清水口）。返回 {now, base, baseline, color}。"""
    hx, hy = STRUCT_HALF
    if cur.get("gm") is None:
        return {"color": False}
    now = gate_measure(cur["gm"], cur["img"], cur["valid"], e["out"], (hx, hy))
    d = baseline_dir(blueprint, area)
    fg, fs = d / f"{_bkey(e)}_gate.npz", d / f"{_bkey(e)}.npz"
    out = {"now": now, "color": True}
    if fg.exists():
        z = np.load(fg)
        out["base"], out["baseline"] = gate_measure(z["gm"], z["img"], z["valid"], e["out"], (hx, hy)), "ok"
    elif fs.exists() and e.get("fluid") in ("清水", "惰气"):
        z = np.load(fs)
        out["base"], out["baseline"] = gate_measure(z["m"][..., 0], z["img"], z["valid"], e["out"], (hx, hy)), "ok"
    else:
        d.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(fg, gm=cur["gm"], img=cur["img"], valid=cur["valid"])
        out["baseline"] = "new"
    return out


def judge_gate(row: dict) -> tuple[str, str]:
    g = row.get("gate") or {}
    now, base = g.get("now"), g.get("base")
    no, fl = row.get("gate_no"), row.get("fluid") or "清水"
    lose = f"少 {row['rate']:g}/分" if row.get("rate") else "少一个口的量"
    if not row["id_ok"]:
        return "待确认", f"没在画面里找到输入端 {no} 的{fl}图标（可能被挡住或镜头没到位）"
    if g.get("color") is False:
        return "观察", f"认出了输入端 {no}（{fl}），但还没有{fl}管子的颜色参数，查不了有没有{fl}进来"
    if now is None:
        return "待确认", f"输入端 {no}（{fl}）没量出来（{g.get('error', '')}）"
    if base is None or g.get("baseline") == "new":
        if not now["connected"]:
            return "观察", f"第一次看（已存基线）：输入端 {no} 外面看不到接出去的{fl}管子（可能直接接暗管出口），这个口查不了有没有{fl}进来"
        return "正常", f"有{fl}管子接进来（第一次看，已存基线）"
    if base["connected"] and not now["connected"]:
        if now["px"] <= (1 - GATE_LOST) * base["px"]:
            return "问题", f"输入端 {no} 外面接进来的{fl}管子和基线比断开了、少了 {base['px'] - now['px']} 像素：这个口没{fl}进来（{lose}）"
        return "待确认", f"输入端 {no} 外面的{fl}管子这次没连到底，但管子像素和基线差不多（{now['px']}/{base['px']}）：看一眼是不是被挡住了"
    if base["connected"] and base["v"] > 0 and now["v"] < GATE_DIM * base["v"]:
        # 管道有容量：流量小时水流变细、两边透明，看起来变暗；还连着就是还有东西在进（用户 2026-11-08）
        return "观察", f"有{fl}进来，但管子里的流比基线细（亮度 {now['v']:.0f}/{base['v']:.0f}）：这个口现在流量变小了"
    if not base["connected"]:
        # 应龙关：清水/沉积酸/惰气从旁边的暗管出口直接进输入端，外面没有一段看得见的管子
        return "观察", f"输入端 {no} 外面看不到接出去的{fl}管子（可能直接接暗管出口），这个口查不了有没有{fl}进来"
    return "正常", f"有{fl}进来（外面的管子连着，亮度 {now['v']:.0f}/基线 {base['v']:.0f}）"


# ---------------------------------------------------------------- 一个分区
def _need_overhead(p, where: str) -> None:
    sc = p.scene()
    if sc != "Scene_Overhead":
        raise RuntimeError(f"{where}：不在俯瞰（当前 {sc}），停止，没有按方向键")


def _move(p, delta) -> None:
    """按镜头位移（近景像素）平移：先横后纵，时长 = 距离 / 速度。"""
    for axis, (neg, pos) in ((0, ("left", "right")), (1, ("up", "down"))):
        d = float(delta[axis])
        if abs(d) < 25:
            continue
        p.hold(PAN[pos if d > 0 else neg], abs(d) / SPEED)
        time.sleep(0.15)
    time.sleep(0.5)


def _ocr_all(p):
    return p.ocr_boxes(ROI) + p.ocr_boxes(LROI)


def scan_area(region: str, area: str, blueprint: str, machines: list[str], window: float = 12.0, log=print,
              cancelled=lambda: False, skip_nav: bool = False) -> dict:
    """传送 → 进俯瞰 → 顶角 → 滚入 7 格 → 按视点看目标设备 → 缩回 → 退出俯瞰 → 存 patrol JSON。
    返回 {"patrol_path", "devices", "rows", "timing", "error", "step"}；出错时 error 非空（不抛异常）。"""
    import navigate
    from patrol import Patrol

    res = {"patrol_path": None, "devices": 0, "rows": [], "timing": {}, "error": "", "step": "准备"}
    t0 = time.time()
    try:
        targets, every, scan_dir, F2N = load_targets(blueprint, area, machines)
        # 水泵不看（基地外太远），改看每个流体输入端有没有东西进来（用户 2026-11-08：清水口当水泵；应龙关气液类型多，也要看）
        targets = [t for t in targets if t["name"] != "水泵"] + gate_targets(blueprint, area, F2N, None)
        if not targets:
            res["error"] = f"底账里没有 {'、'.join(machines)}"
            return res
        bounds = cam_bounds(scan_dir)
        log(f"== {area}：近景定向巡检 {len(targets)} 台（{dict(Counter(e['name'] for e in targets))}），底账扫描 {scan_dir.name}")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shot_dir = DEBUG_DIR / "shots" / f"{stamp}_{area}_近景"
        shot_dir.mkdir(parents=True, exist_ok=True)
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
        if p.scene() != "Scene_Overhead":
            res["step"] = "进俯瞰"
            if not navigate.enter_overhead():
                res["error"] = navigate.last_error() or "进不了俯瞰"
                return res
        t_nav = time.time()
        res["step"] = "顶角 + 滚入"
        p.go_corner()  # 回不到俯瞰时会抛异常，不按方向键
        _need_overhead(p, "顶角后")
        for _ in range(TICKS):
            wheel(p.hwnd, int(C[0]), int(C[1]), 120); time.sleep(0.4)
        move_cursor(p.hwnd, *PARK); time.sleep(1.0)
        p.shot()
        cam, nv, nl = localize(labels_from_boxes(_ocr_all(p)), every, np.zeros(2))
        if nv < 2 or (nl >= 4 and nv < 0.5 * nl):
            # 对不上底账：缩放不对 / 底账和基地不符。不乱走，缩回停下。
            # （武陵城左上角那一屏只有 2 个标签，2/2 是对的：原来要求 ≥3 时 2026-11-08 程序里实跑误停）
            p.zoom_out()
            res["error"] = f"滚入后只有 {nv}/{nl} 个标签对上底账，定位不了（缩放不对或底账过旧，先重扫底账）"
            return res
        views = plan_views(targets, start=cam, bounds=bounds)
        log(f"   起点 {nv}/{nl} 个标签对上；{len(views)} 个视点，每个看 {window:g} 秒")
        t_ready = time.time()
        per = {id(e): {"e": e, "n": 0, "c": Counter(), "id_ok": False, "view": None, "shots": {}} for e in targets}
        vlog = []
        for vi, v in enumerate(views, 1):
            if cancelled():
                log("   已取消，剩下的视点不看了")
                break
            res["step"] = f"视点 {vi}/{len(views)}"
            _need_overhead(p, f"视点 {vi} 挪镜头前")
            _move(p, v["cam"] - cam)
            p.shot(); boxes = _ocr_all(p)
            cam, nv, nl = localize(labels_from_boxes(boxes), every, v["cam"])
            off = any(not (SAFE[0] - 30 <= (e["q"] - cam)[0] <= SAFE[1] + 30 and SAFE[2] - 30 <= (e["q"] - cam)[1] <= SAFE[3] + 30) for e in v["targets"])
            if off:  # 差得多：补走一次
                _need_overhead(p, f"视点 {vi} 补走前")
                _move(p, v["cam"] - cam)
                p.shot(); boxes = _ocr_all(p)
                cam, nv, nl = localize(labels_from_boxes(boxes), every, v["cam"].copy())
            log(f"   视点 {vi}：{len(v['targets'])} 台，{nv}/{nl} 个标签对上{'（补走一次）' if off else ''}")
            vlog.append({"view": vi, "cam": [round(float(x), 1) for x in cam], "votes": nv, "labels": nl, "redo": off, "n": len(v["targets"])})
            n = int(window) + 1
            ts = time.time()
            acc = {}  # 每台目标：结构掩码并集（多帧并集盖住管道流动动画）
            for k in range(n):
                img = p.shot()
                if k == 3 and any(not per[id(e)]["id_ok"] for e in v["targets"]):
                    boxes = boxes + _ocr_all(p)  # 镜头刚停时偶尔漏读：补读一次
                found = find_badges(img)
                r = check_frame(img, boxes, v["targets"], cam, found)
                sm = struct_masks(img)
                fm = {}  # 这一帧各流体管子颜色掩码（输入端用）
                bm = np.zeros((720, 1280), np.uint8)
                for _, bx, by in found:
                    cv2.circle(bm, (bx, by), 24, 1, -1)
                for e in v["targets"]:
                    x = r[id(e)]
                    a = acc.get(id(e))
                    if a is None:
                        u = x["u"]
                        if "gate_no" in e and e["fluid"] not in fm:
                            fm[e["fluid"]] = fluid_pipe_mask(img, e["fluid"])
                        a = acc[id(e)] = {"u": u, "cam": cam.copy(), "m": _crop(sm, u), "badge": _crop(bm, u), "valid": _crop(_valid_full(), u),
                                          "gm": _crop(fm[e["fluid"]], u) if "gate_no" in e and fm.get(e["fluid"]) is not None else None,
                                          "img": _crop(img, u)}
                    else:
                        a["m"] |= _crop(sm, a["u"]); a["badge"] |= _crop(bm, a["u"])
                        if a.get("gm") is not None:
                            if e["fluid"] not in fm:
                                fm[e["fluid"]] = fluid_pipe_mask(img, e["fluid"])
                            a["gm"] |= _crop(fm[e["fluid"]], a["u"])
                fname = f"v{vi:02d}_f{k:02d}.png"
                saved = False
                for e in v["targets"]:
                    s, x = per[id(e)], r[id(e)]
                    s["n"] += 1; s["id_ok"] |= x["id_ok"]; s["c"].update(x["kinds"]); s["view"] = vi
                    for kind, pos in x["pos"].items():
                        if kind not in s["shots"]:  # 每种标记第一次出现的那帧：定位图裁它
                            s["shots"][kind] = (fname, pos)
                            saved = True
                if k == 0:
                    _wr(shot_dir / f"v{vi:02d}_标注.png", annotate(img, r, v["targets"]))
                if saved or k == 0:
                    _wr(shot_dir / fname, img)
                if k < n - 1:
                    time.sleep(max(0.0, ts + (k + 1) - time.time()))
            for e in v["targets"]:  # 结构基线：只比认出来的（位置可信）
                s = per[id(e)]
                if s["id_ok"] and id(e) in acc:
                    s["u"] = [round(float(x), 1) for x in acc[id(e)]["u"]]
                    if "gate_no" in e:  # 先量输入端外侧的管子（基线在下面 check_struct 第一次看时才存）
                        try:
                            s["gate"] = check_gate(blueprint, area, e, acc[id(e)])
                        except Exception as ex:  # noqa: BLE001
                            s["gate"] = {"error": f"{type(ex).__name__}: {ex}"}
                    try:
                        s["struct"] = check_struct(blueprint, area, e, acc[id(e)], shot_dir)
                        if s["struct"].get("boxes"):
                            log(f"   ！{e['name']} {tuple(int(round(t)) for t in e['far'])}：旁边的管道/传送带比基线少了 {s['struct']['lost_px']} 像素")
                    except Exception as ex:  # noqa: BLE001  结构比对出错不影响状态采样
                        s["struct"] = {"error": f"{type(ex).__name__}: {ex}"}
        p.zoom_out()
        t_end = time.time()
        # 结构断开处换成远景坐标（影响范围要在据点拼合图上找它进的输入端）
        Ai = np.linalg.inv(np.vstack([F2N, [0, 0, 1]]))
        for s in per.values():
            st = s.get("struct") or {}
            if st.get("at"):
                st["at_far"] = [[round(float(v), 1) for v in (Ai @ np.array([a[0], a[1], 1.0]))[:2]] for a in st["at"]]
        # patrol JSON：有标记的目标设备（combine 只看有标记的），坐标用底账远景坐标（和巡检同一坐标系：顶角原点、最远缩放）
        devices, rows = [], []
        for s in per.values():
            e, c, n = s["e"], s["c"], max(1, s["n"])
            rows.append({"area": area, "name": e["name"], "far": [round(float(v)) for v in e["far"]], "q": [round(float(v), 1) for v in e["q"]],
                         "where": e["where"], "view": s["view"],
                         "frames": s["n"], "id_ok": s["id_ok"], "counts": {k: int(c[k]) for k in KINDS if c[k]},
                         "shots": {k: [f, int(xy[0]), int(xy[1])] for k, (f, xy) in s["shots"].items()},
                         "struct": s.get("struct"),
                         **({"gate_no": e["gate_no"], "fluid": e["fluid"], "rate": e.get("rate"), "gate": s.get("gate"), "u": s.get("u")} if "gate_no" in e else {}),
                         "recipe": e["recipe"], "product": e["recipe"].split("→")[1].split("×")[0].strip() if "→" in e["recipe"] else None})
            if not s["id_ok"]:
                continue
            kind = "closed" if c["closed"] else max(("blocked", "idle"), key=lambda k: c[k]) if (c["blocked"] or c["idle"]) else None
            if kind is None:
                continue  # 没标记或只有缺气：combine 不管，留在 rows 里
            st = KIND_CN[kind]
            sf, (sx, sy) = s["shots"][kind]
            devices.append({"name": e["name"], "status": st, "badge": st, "world_x": int(round(e["far"][0])), "world_y": int(round(e["far"][1])),
                            "recipe": e["recipe"] or "", "output": e["output"], "recipe_id": e["recipe_id"], "guessed": True, "near": True,
                            "env": None, "seen": f"近景 {c[kind]}/{n} 帧", "passes": 2 if c[kind] >= PERSIST * n else 1,
                            "foreign": None, "shot": sf, "panel": "", "warn": "", "flow": "", "sx": int(sx), "sy": int(sy)})
        res["timing"] = {"传送+进俯瞰": round(t_nav - t0, 1), "顶角+滚入+定位": round(t_ready - t_nav, 1), "视点": round(t_end - t_ready, 1)}
        out = {"region": region, "area": area, "time": stamp, "shots": str(shot_dir.relative_to(ROOT)), "power": None, "checks": [],
               "recheck": None, "panels": None, "devices": devices,
               "near": {"machines": machines, "window": window, "targets": len(targets), "views": vlog, "timing": res["timing"],
                        "ledger_scan": str(scan_dir.relative_to(ROOT)), "rows": rows}}
        path = DEBUG_DIR / f"patrol_{stamp}.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        res.update(patrol_path=path, devices=len(devices), rows=rows, step="完成")
        if not skip_nav:
            res["step"] = "退出俯瞰"
            import full_patrol
            if not full_patrol.exit_overhead():
                res["error"] = "按了 CapsLock 仍在俯瞰"
        return res
    except Exception as ex:  # noqa: BLE001
        res["error"] = f"{type(ex).__name__}: {ex}"
        return res
