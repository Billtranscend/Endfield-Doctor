"""蓝图分析：预览拼图上的物流件识别（docs/蓝图分析器-设计.md 第 4.3 节）。

认 7 种一格大小的物流件：
  分流器 / 汇流器 / 物流桥（传送带用，方形格）
  管道分流器 / 管道汇流器 / 管道桥（管道用，十字形格）
  准入口（深色方框 + 中间物品图标；管道/物品两种长得一样）

做法：
- 前 6 种：35×35 模板（每类 1–3 个样例）× 4 个旋转，整图匹配，取分数最高的类。
  皇帝产线 9 张实测：真的全在 ≥0.75，假的全在 <0.60，阈值取中间 0.68；169 个逐个目视核对全对。
- 准入口：只比外框（中间图标挖掉），这样任何物品都能认。真的 ≥0.69，假的（设备产物图标）≤0.61，阈值 0.66。
- 准入口是管道的还是物品的：预览上两种外框一样，悬停也看不出（用户确认）。规则：图标是液体/气体 → 一定是管道准入口；
  固体 → 物品准入口。形态来自图标库 resource/bpa/items/ 或作者按图标组标注；都没有时才用局部线索推断。
  管道准入口底下可能还叠着物品准入口（游戏只显示上面的），有传送带经过的标出来，到俯瞰里点开或按 F2 看。
- 物品图标：同一项目里所有准入口的图标按相似度分组，组号相同 = 同一种物品。
"""

from __future__ import annotations

import cv2
import numpy as np

from . import vision as V

LG_DIR = V.RES / "lg"
H = 17  # 模板半边长（35×35）
THR_LG = 0.68
THR_GATE = 0.66
LG_TYPES = ["分流器", "汇流器", "物流桥", "管道分流器", "管道汇流器", "管道桥"]
PIPE_TYPES = {"管道分流器", "管道汇流器", "管道桥"}
GATE = "准入口"

_tpl = None


def _templates():
    global _tpl
    if _tpl is None:
        lg, gate = {}, []
        for p in sorted(LG_DIR.glob("*.png")):
            k = p.stem.rsplit("_", 1)[0]
            im = V.rd(p)
            if k == GATE:
                gate.append(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY))
            else:
                lg.setdefault(k, []).extend(np.rot90(im, r).copy() for r in range(4))
        yy, xx = np.mgrid[-H:H + 1, -H:H + 1]
        gmask = ((np.hypot(xx, yy) > 13.5) & (np.maximum(abs(xx), abs(yy)) <= 16)).astype(np.uint8)
        _tpl = (lg, gate, gmask)
    return _tpl


def _peaks(score, thr, sep=22):
    ys, xs = np.nonzero(score >= thr)
    keep = []
    for i in np.argsort(-score[ys, xs]):
        x, y = int(xs[i]), int(ys[i])
        if all(abs(x - a) > sep or abs(y - b) > sep for a, b in keep):
            keep.append((x, y))
    return keep


def detect(mosaic: np.ndarray) -> list[dict]:
    """→ [{type, p, score, margin}]，准入口另带 sides（四边接的东西）。坐标是拼图坐标（格中心）。"""
    lg, gate, gmask = _templates()
    names = [k for k in LG_TYPES if k in lg]
    maps = np.stack([np.max([cv2.matchTemplate(mosaic, t, cv2.TM_CCOEFF_NORMED) for t in lg[k]], 0) for k in names])
    near = np.stack([cv2.dilate(mp, np.ones((7, 7))) for mp in maps])  # 同一位置 ±3 px 内各类的分数
    best, arg = maps.max(0), maps.argmax(0)
    out = []
    for x, y in _peaks(best, THR_LG):
        sc = np.sort(near[:, y, x])[::-1]
        out.append({"type": names[arg[y, x]], "p": [x + H, y + H], "score": round(float(best[y, x]), 3),
                    "margin": round(float(sc[0] - sc[1]), 3)})
    g = cv2.cvtColor(mosaic, cv2.COLOR_BGR2GRAY)
    gs = np.max([np.nan_to_num(cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED, mask=gmask), nan=-1, posinf=-1, neginf=-1)
                 for t in gate], 0)
    for x, y in _peaks(gs, THR_GATE):
        cx, cy = x + H, y + H
        if any(abs(cx - o["p"][0]) < 15 and abs(cy - o["p"][1]) < 15 for o in out):
            continue
        out.append({"type": GATE, "p": [cx, cy], "score": round(float(gs[y, x]), 3)})
    _gate_sides(mosaic, out)
    return out


# ------------------------------------------------------------------ 准入口：管道还是物品（推断）
def _masks(mosaic):
    hsv = cv2.cvtColor(mosaic, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(mosaic, cv2.COLOR_BGR2GRAY)
    yel = (hsv[..., 0] >= 18) & (hsv[..., 0] <= 38) & (hsv[..., 1] > 80) & (hsv[..., 2] > 200)
    drk = (g < 110) & (hsv[..., 1] < 40)
    return yel, drk


def _side(yel, drk, x, y, dx, dy) -> str:
    """格子外 19–32 px 一段：中线是黄色 → 传送带 Y；中线是窄的深灰线、两侧不深 → 管道 P（排除相邻格子的深色外框）。"""
    px, py = -dy, dx
    cen, fl, yl = [], [], []
    Hh, Ww = drk.shape
    for t in range(19, 33):
        cx, cy = x + dx * t, y + dy * t
        for o in range(-3, 4):
            a, b = cx + px * o, cy + py * o
            if 0 <= a < Ww and 0 <= b < Hh:
                cen.append(drk[b, a]); yl.append(yel[b, a])
        for o in (-11, -10, -9, 9, 10, 11):
            a, b = cx + px * o, cy + py * o
            if 0 <= a < Ww and 0 <= b < Hh:
                fl.append(drk[b, a])
    if not cen:
        return "."
    if np.mean(yl) > 0.5:
        return "Y"
    if np.mean(cen) > 0.6 and np.mean(fl) < 0.3:
        return "P"
    return "."


def _gate_sides(mosaic, items) -> None:
    yel, drk = _masks(mosaic)
    lgs = [o for o in items if o["type"] != GATE]
    for o in items:
        if o["type"] != GATE:
            continue
        x, y = o["p"]
        sides = "".join(_side(yel, drk, x, y, dx, dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        nb = [n["type"] for n in lgs if (abs(n["p"][0] - x) < 8 and 28 < abs(n["p"][1] - y) < 42)
              or (abs(n["p"][1] - y) < 8 and 28 < abs(n["p"][0] - x) < 42)]
        pipe = [f"相邻{t}" for t in nb if t in PIPE_TYPES] + (["接管道"] if "P" in sides else [])
        belt = [f"相邻{t}" for t in nb if t not in PIPE_TYPES] + (["接传送带"] if "Y" in sides else [])
        o["sides"] = sides
        o["kind_local"] = "管道" if pipe and not belt else "物品" if belt and not pipe else "?"
        o["kind_basis"] = "、".join(pipe + belt)
        o["belt_touch"] = "Y" in sides or any(t not in PIPE_TYPES for t in nb)


def gate_icon(mosaic, p) -> np.ndarray:
    x, y = p
    return mosaic[y - 11:y + 12, x - 11:x + 12].copy()


_CIRC = (np.hypot(*np.mgrid[-11:12, -11:12]) <= 10.5).astype(np.uint8)


def icon_similar(a, b) -> bool:
    """同一种物品：形状像（掩模相关 ≥0.80）且平均颜色接近（金色云 vs 绿色云形状一样，颜色不同）。"""
    r = cv2.matchTemplate(cv2.copyMakeBorder(a, 2, 2, 2, 2, cv2.BORDER_REPLICATE), b, cv2.TM_CCOEFF_NORMED, mask=_CIRC)
    if float(np.nan_to_num(r).max()) < 0.80:
        return False
    ca = np.array(cv2.mean(a, mask=_CIRC)[:3]); cb = np.array(cv2.mean(b, mask=_CIRC)[:3])
    return float(np.linalg.norm(ca - cb)) < 30


def group_icons(icons: list[np.ndarray]) -> list[int]:
    """贪心分组：和已有组的代表相似就归进去。返回每个图标的组号。"""
    reps, grp = [], []
    for ic in icons:
        for gi, rep in enumerate(reps):
            if icon_similar(rep, ic):
                grp.append(gi)
                break
        else:
            grp.append(len(reps))
            reps.append(ic)
    return grp


ITEM_DIR = V.RES / "items"  # 全局已知物品图标：<形态>_<名字>_<n>.png，形态 = 液/气/固，名字不知道写「未命名」
STATES = ("液", "气", "固")


def _lib(d) -> list[tuple[str, str, np.ndarray, object]]:
    out = []
    for f in sorted(d.glob("*.png")) if d and d.exists() else []:
        parts = f.stem.split("_")
        if len(parts) >= 2 and parts[0] in STATES:
            out.append((parts[0], "" if parts[1] == "未命名" else parts[1], V.rd(f), f))
    return out


def lookup_item(icon, project_dir=None) -> tuple[str | None, str]:
    """图标 → (形态, 名字)。先查项目图标库（作者标的），再查全局库；都没有 → (None, "")。"""
    for d in (project_dir, ITEM_DIR):
        for st, nm, ic, _ in _lib(d):
            if ic.shape == icon.shape and icon_similar(ic, icon):
                return st, nm
    return None, ""


def forget_item(icon, d) -> None:
    """删掉库里和这个图标是同一种的条目（作者改标时用）。"""
    for _, _, ic, f in _lib(d):
        if ic.shape == icon.shape and icon_similar(ic, icon):
            f.unlink()


def save_item(icon, state: str, name: str = "", d=None) -> None:
    d = d or ITEM_DIR
    d.mkdir(parents=True, exist_ok=True)
    k = len(list(d.glob(f"{state}_{name or '未命名'}_*.png"))) + 1
    V.wr(d / f"{state}_{name or '未命名'}_{k}.png", icon)


# ------------------------------------------------------------------ AKEData 物品图标比对（给图标组起名字、定形态）
AKE_MIN, AKE_LEAD = 0.45, 0.12  # 自动采用：分数 ≥0.45 且领先第二名 ≥0.12（皇帝产线：清水/惰气/酸气/沉积酸都过，认错的都不过）
_ake = None


def _ake_templates():
    """候选 = 配方里出现过的物品 + 全部液体/气体（resource/icons/fluid）。图标用 AKEData 的（panel_recipe.icon 有缓存）。"""
    global _ake
    if _ake is None:
        import json
        import panel_recipe as PR
        from paths import ROOT
        rec = json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))
        icons = json.loads((ROOT / "data" / "item_icons.json").read_text(encoding="utf-8"))
        names = json.loads((ROOT / "data" / "item_id_names.json").read_text(encoding="utf-8"))
        fluids = {f.stem for f in (ROOT / "resource" / "icons" / "fluid").glob("*.png")}
        ids = sorted({k for r in rec for k in list(r["inputs"]) + list(r["outputs"])} | fluids)
        _ake = []
        for iid in ids:
            im = PR.icon(icons.get(iid, iid))
            if im is None:
                continue
            st = "气" if iid.startswith("item_gas") else "液" if iid.startswith("item_liquid") else "固"
            sizes = []
            for sz in range(14, 22):
                h, w = im.shape[:2]
                sc = sz / max(h, w)
                t = cv2.resize(im, (max(3, round(w * sc)), max(3, round(h * sc))), interpolation=cv2.INTER_AREA)
                sizes.append((t[..., :3].astype(np.float32), t[..., 3:4].astype(np.float32) / 255, (t[..., 3] > 60).astype(np.uint8)))
            _ake.append((iid, names.get(iid, iid), st, sizes))
    return _ake


_AKE_CACHE: dict = {}


def ake_candidates(icon, top: int = 3) -> list[dict]:
    """准入口图标（23×23）→ AKEData 里最像的几个物品 [{id, name, state, score}]。
    分数 = 形状相关（掩模）− 平均颜色差/120；图标外圈的深色底按实际颜色垫在 AKEData 图标的透明处。
    同一个图标（字节相同）只比一次：设计分析一遍要比 88 次、约 45 秒，断管影响范围要换着输入量重算好几遍（2026-11-10）。"""
    key = (icon.shape, icon.dtype.str, icon.tobytes(), top)
    if key in _AKE_CACHE:
        return [dict(r) for r in _AKE_CACHE[key]]
    res = _ake_candidates(icon, top)
    if len(_AKE_CACHE) > 4000:
        _AKE_CACHE.clear()
    _AKE_CACHE[key] = [dict(r) for r in res]
    return res


def _ake_candidates(icon, top: int = 3) -> list[dict]:
    g = icon.astype(np.float32)
    rr = np.hypot(*np.mgrid[-11:12, -11:12])
    bg = np.median(g[rr > 9.5], axis=0)
    res = []
    for iid, nm, st, sizes in _ake_templates():
        best = -9.0
        for bgr, a, m in sizes:
            if bgr.shape[0] > g.shape[0] or bgr.shape[1] > g.shape[1]:
                continue
            comp = bgr * a + bg * (1 - a)
            r = np.nan_to_num(cv2.matchTemplate(g, comp, cv2.TM_CCOEFF_NORMED, mask=m), nan=-1, posinf=-1, neginf=-1)
            _, mx, _, (x, y) = cv2.minMaxLoc(r)
            mb = m.astype(bool)
            patch = g[y:y + bgr.shape[0], x:x + bgr.shape[1]]
            cd = float(np.linalg.norm(patch[mb].mean(0) - comp[mb].mean(0)))
            best = max(best, mx - cd / 120)
        res.append({"id": iid, "name": nm, "state": st, "score": round(best, 3)})
    res.sort(key=lambda z: -z["score"])
    return res[:top]


def ake_confident(cands: list[dict]) -> bool:
    return bool(cands) and cands[0]["score"] >= AKE_MIN and (len(cands) < 2 or cands[0]["score"] - cands[1]["score"] >= AKE_LEAD)


def decide_kinds(gates: list[dict], groups: dict) -> None:
    """定每个准入口是管道还是物品（用户确认的规则）：
    - 图标是液体或气体 → 一定是管道准入口；图标是固体 → 物品准入口。形态来自图标库或作者标注（groups[组号]["state"]）。
    - 形态不知道的 → 组内按局部线索投票，标「推断」，组内矛盾的标 kind_conflict，请作者标形态。
    - 管道准入口所在格有传送带经过 → maybe_item_below：同一格底下可能还叠着物品准入口，游戏里只显示上面的管道准入口，
      要在俯瞰里点开（弹出的列表会列出这一格所有东西）或按 F2 看传送带图层才知道。"""
    by = {}
    for g in gates:
        by.setdefault(g["icon_group"], []).append(g)
    for gi, mem in by.items():
        st = (groups.get(gi) or {}).get("state")
        ks = [m["kind_local"] for m in mem if m["kind_local"] != "?"]
        cnt = {k: ks.count(k) for k in set(ks)}
        vote = max(cnt, key=cnt.get) if cnt else "?"
        for m in mem:
            if st in ("液", "气"):
                m["kind"], m["kind_how"] = "管道", f"图标是{'液体' if st == '液' else '气体'}"
            elif st == "固":
                m["kind"], m["kind_how"] = "物品", "图标是固体"
            else:
                m["kind"], m["kind_how"] = vote, "推断"
            m["kind_conflict"] = st is None and len(cnt) > 1
            m["maybe_item_below"] = m["kind"] == "管道" and bool(m.get("belt_touch"))


def presence_check(items: list[dict], devices: dict) -> list[str]:
    """设备一览只有种类没有数量：识别到的种类应和一览一致（有 ↔ 有）。返回不一致说明。"""
    bad = []
    for t in LG_TYPES:
        got = sum(1 for o in items if o["type"] == t)
        if (got > 0) != (t in devices):
            bad.append(f"{t}：识别 {got} 个，设备一览{'有' if t in devices else '没有'}")
    has_gate = any(o["type"] == GATE for o in items)
    in_list = any(n in devices for n in ("管道准入口", "物品准入口"))
    if has_gate != in_list:
        bad.append(f"准入口：识别 {sum(1 for o in items if o['type'] == GATE)} 个，设备一览{'有' if in_list else '没有'}")
    return bad
