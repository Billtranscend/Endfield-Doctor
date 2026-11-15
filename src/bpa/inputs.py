"""蓝图分析：输入端候选 + 带编号截图（docs/蓝图分析器-设计.md 第 2.2 节）。

候选 = 接入口紧贴基地外侧的准入口（from_gates，用物流件识别的结果和用户给的规则）。
旧办法 candidates()（深色方框 + 彩色图标）留作没有物流件结果时用。漏的、错的由作者在界面上增删。
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import vision as V

EDGE = 2.0  # 离据点边缘几格以内算「边上」


def candidates(canvas: np.ndarray) -> list[dict]:
    g = cv2.cvtColor(canvas, cv2.COLOR_BGR2GRAY)
    dark = (g < 80).astype(np.float32)
    k = np.zeros((34, 34), np.float32)
    k[:3, :] = k[-3:, :] = k[:, :3] = k[:, -3:] = 1
    k /= k.sum()
    ring = cv2.filter2D(dark, -1, k)
    center = cv2.blur(dark, (16, 16))
    hsv = cv2.cvtColor(canvas, cv2.COLOR_BGR2HSV)
    belt = (hsv[..., 0] >= 15) & (hsv[..., 0] <= 40) & (hsv[..., 2] > 225)  # 传送带是很亮的黄色，不算物品图标（沉积酸图标是较暗的橙黄）
    sat = ((hsv[..., 1] > 70) & ~belt).astype(np.float32)
    colored = cv2.blur(sat, (22, 22))  # 物品图标有彩色圈；管道拐角、空格子没有
    H, W = g.shape
    band = int(EDGE * V.CELL)
    mask = np.zeros_like(ring, bool)
    mask[:band] = mask[-band:] = True
    mask[:, :band] = True
    mask[:, -band:] = True
    score = np.where(mask & (ring > 0.40) & (center < 0.6) & (colored > 0.08), ring, 0)
    mx = cv2.dilate(score, np.ones((31, 31), np.uint8))
    ys, xs = np.nonzero((score > 0) & (score >= mx))
    pts = sorted(zip(score[ys, xs], xs, ys), reverse=True)
    keep = []
    for s, x, y in pts:
        if all(abs(x - k2[0]) > 28 or abs(y - k2[1]) > 28 for k2 in keep):
            keep.append((int(x), int(y), float(s)))
    keep.sort(key=lambda p: _order(p, W, H))
    return [{"no": i + 1, "p": [x, y], "score": round(s, 2), "source": "auto"} for i, (x, y, s) in enumerate(keep)]


def _order(p, W, H):
    """编号：沿边界顺时针（上 → 右 → 下 → 左）。"""
    x, y = p[0], p[1]
    d = {0: y, 1: W - x, 2: H - y, 3: x}
    side = min(d, key=d.get)
    return (side, x if side == 0 else y if side == 1 else -x if side == 2 else -y)


TOUCH = 1.25  # 准入口中心离据点边缘几格以内算「紧贴外侧」（贴边的实测 25–34 px ≈ 0.7–1.0 格）
_DIRS = {"上": (0, -1), "下": (0, 1), "左": (-1, 0), "右": (1, 0)}
_OPP = {"上": "下", "下": "上", "左": "右", "右": "左"}


def _along(p, q, d):
    ox, oy = q[0] - p[0], q[1] - p[1]
    return ox * d[0] + oy * d[1], abs(ox * d[1] - oy * d[0])


def from_gates(canvas: np.ndarray, lg_canvas: list[dict], groups: dict | None = None,
               pipes_canvas: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """输入端候选（用户给的规则）：准入口的接入口紧贴基地外侧 = 提醒玩家从这里输入。
    - 准入口要紧贴拼合图边缘（TOUCH 格以内）；离边一两格的不算。
    - 两旁（沿边方向）接着分流器/汇流器/桥 → 它是横着走的，入口不朝外，不算（应龙关一排息壤气准入口就是这样）。
      两旁是别的准入口（一排水口）不影响。
    - 朝里一侧接的是暗管出口 → 东西是从暗管出来往外排的（如武陵城的壤晶废液），不算输入。
    预填：液体每口 60（一口一泵，水和沉积酸都这样）；接口按朝里接的暗管填「暗管/多口暗管」，液体没接暗管的填「水泵」；气体的量由作者填。
    返回 (候选, 排除的及原因)。"""
    H, W = canvas.shape[:2]
    groups = groups or {}
    pipes_canvas = pipes_canvas or []
    keep, drop = [], []
    for o in lg_canvas:
        if o["type"] != "准入口":
            continue
        x, y = o["p"]
        dist = {"上": y, "下": H - y, "左": x, "右": W - x}
        e = min(dist, key=dist.get)
        if dist[e] > 2 * V.CELL:
            continue
        g = groups.get(o.get("icon_group")) or {}
        rec = {"p": [x, y], "item": g.get("item", ""), "icon_group": o.get("icon_group"), "kind": o.get("kind", "?"), "bp": o.get("bp"),
               "p_bp": o.get("p_bp")}
        if dist[e] > TOUCH * V.CELL:
            drop.append(dict(rec, why=f"离{e}边 {dist[e] / V.CELL:.1f} 格，没贴着外侧"))
            continue
        lat = [k for k in _DIRS if k not in (e, _OPP[e])]
        side_lg = []
        for k in lat:
            for n in lg_canvas:
                if n is o or n["type"] == "准入口":
                    continue
                al, pp = _along((x, y), n["p"], _DIRS[k])
                if 25 < al < 45 and pp < 10:
                    side_lg.append(n["type"])
        if side_lg:
            drop.append(dict(rec, why=f"两旁接着{'、'.join(side_lg)}，入口不朝外"))
            continue
        inward = _DIRS[_OPP[e]]
        near = sorted((_along((x, y), pp_["p"], inward) + (pp_["name"],) for pp_ in pipes_canvas), key=lambda z: z[0])
        near = [z for z in near if 15 < z[0] < 120 and z[1] < 45]
        via = ""
        if near:
            nm = near[0][2]
            if "出口" in nm:
                drop.append(dict(rec, why=f"朝里接的是{nm}，是往外排的"))
                continue
            via = "多口暗管" if "多口" in nm else "暗管"
        st = g.get("state")
        if st == "液":
            rec["rate"] = 60
            rec["via"] = via or "水泵"
        else:
            rec["rate"] = None
            rec["via"] = via
        keep.append(rec)
    keep.sort(key=lambda r: _order(r["p"], W, H))
    marks = [dict(r, no=i + 1, source="auto", merged=False, note="") for i, r in enumerate(keep)]
    return marks, drop


# ------------------------------------------------------------------ 作者存的标记跟着拼合图走（2026-11-14 issue #1 第四轮）
# 作者存的输入端原来只记拼合图上的坐标。拼合图的原点是各张蓝图位置里最靠左上的那个（vision.composite 的 F0）：重扫底账、
# 重读蓝图、增减蓝图以后原点会变，存的坐标就指到别处。设计分析按位置（15 px）找输入端，一个都找不上，清水就「没有来源」
# （他的武陵城：存着 7 个旧标记，现在的候选是 21 个清水口，一个都对不上；全据点清水 445/960）。
# 现在每个标记另外记「在哪张蓝图预览里的什么位置」（bp、p_bp），这个不随拼合变；每次拼合后按它重新算拼合图坐标（rebase）。
SNAP = 15  # 标记离准入口多少 px 以内算标在它上面（design.analyze 找输入端用的同一个数）


def _hit(p, q, tol=SNAP) -> bool:
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def _gate_at(p, lg_canvas):
    best = None
    for o in lg_canvas:
        if o["type"] == "准入口" and o.get("p_bp") is not None and _hit(p, o["p"]):
            dist = abs(p[0] - o["p"][0]) + abs(p[1] - o["p"][1])
            if best is None or dist < best[0]:
                best = (dist, o)
    return best[1] if best else None


def to_bp(tf: dict, bp: str, p) -> list[float]:
    """拼合图坐标 → 这张蓝图预览里的坐标（vision.preview_to_canvas 反过来）。"""
    b = tf["bps"][bp]
    f = np.array(tf["F0"], float) + tf["K"] * np.array(p, float)
    x, y = V.Rp((360 - int(b["rot"])) % 360, *((f - np.array(b["t"], float)) / V.K))
    return [round(float(x), 1), round(float(y), 1)]


def from_bp(tf: dict, bp: str, p_bp) -> list[int]:
    x, y = V.preview_to_canvas(tf, bp, None, *p_bp)
    return [int(round(x)), int(round(y))]


def anchor(marks: list[dict], tf: dict, lg_canvas: list[dict]) -> list[dict]:
    """给每个标记记上 bp、p_bp。标在准入口上的用那个准入口自己的（读蓝图时定的）；不在准入口上的（作者在图上随手点的）
    算到离它最近的物流件所在的那张蓝图里。没有变换或没有物流件：原样返回。"""
    if not tf or "bps" not in tf:
        return marks
    out = []
    for m in marks:
        m = dict(m)
        g = _gate_at(m["p"], lg_canvas)
        if g is not None:
            m["bp"], m["p_bp"] = g["bp"], list(g["p_bp"])
        else:
            near = [o for o in lg_canvas if o.get("bp") in tf["bps"]]
            if near:
                o = min(near, key=lambda o: (o["p"][0] - m["p"][0]) ** 2 + (o["p"][1] - m["p"][1]) ** 2)
                m["bp"], m["p_bp"] = o["bp"], to_bp(tf, o["bp"], m["p"])
        out.append(m)
    return out


def seen_of(auto: list[dict]) -> list[list]:
    """候选的位置清单 [[bp, [x, y]]]：保存时记下「作者看到过哪些候选」，以后拼合出新的候选才知道是新的还是作者删掉的。"""
    return [[a["bp"], list(a["p_bp"])] for a in auto if a.get("bp") and a.get("p_bp") is not None]


def rebase(saved: list[dict], tf: dict, lg_canvas: list[dict], auto: list[dict], seen: list | None = None) -> tuple[list[dict], list[dict], dict]:
    """作者存的标记 → 按现在的拼合图重新定位。返回 (标记, 对不上的旧标记, {moved, stale, added})。
    - 有 bp、p_bp 的：按现在的变换重新算位置，15 px 内有准入口就吸上去。它所在的蓝图不在这个据点里了 → 对不上。
    - 老版本存的（没有 p_bp）：按原坐标看是不是落在准入口上。落在准入口上的不到一半 = 拼合图变过，这批旧标记全部算对不上
      （碰巧落在某个准入口上的也不留：那多半是另一个口）；一半以上 = 拼合图没变，全部留下并补记 bp、p_bp。
    - 候选里没被标记盖住的：保存时见过（seen 里有）= 作者删掉的，不加回；没见过 = 程序新认出来的，加进来。
      老版本存的没有 seen：只在这次有对不上的旧标记时加（那份存档已经不能用了）；没有的话保持原样。
    对不上的旧标记不参加分析，留着给作者看当时填的物品和流速。"""
    old = [m for m in saved if m.get("p_bp") is None or not m.get("bp")]
    on = sum(1 for m in old if _gate_at(m["p"], lg_canvas) is not None)
    old_ok = not old or on * 2 >= len(old)
    marks, stale, moved = [], [], 0
    for m in saved:
        m = dict(m)
        if m.get("p_bp") is not None and m.get("bp"):
            if m["bp"] not in tf["bps"]:
                stale.append(dict(m, why=f"它所在的蓝图「{m['bp']}」不在这个据点里了"))
                continue
            p = from_bp(tf, m["bp"], m["p_bp"])
            g = _gate_at(p, lg_canvas)
            if g is not None:
                p, m["bp"], m["p_bp"] = list(g["p"]), g["bp"], list(g["p_bp"])
            if not _hit(p, m["p"], 1):
                moved += 1
            m["p"] = p
            marks.append(m)
        elif old_ok:
            marks.append(anchor([m], tf, lg_canvas)[0])
        else:
            stale.append(dict(m, why="存的是旧拼合图上的位置，现在那里没有准入口"))
    added = 0
    if seen is not None or stale:
        known = [(s[0], s[1]) for s in seen or []]
        nxt = max([int(m.get("no") or 0) for m in marks] + [0])
        for a in auto:
            if any(_hit(a["p"], m["p"]) for m in marks):
                continue
            if any(bp == a.get("bp") and a.get("p_bp") is not None and _hit(a["p_bp"], q) for bp, q in known):
                continue  # 保存时见过、现在不在标记里：作者删掉的
            nxt += 1
            marks.append(dict(a, no=nxt))
            added += 1
    return marks, stale, {"moved": moved, "stale": len(stale), "added": added}


# ------------------------------------------------------------------ 物品名
_NAMES = None


def item_names() -> list[str]:
    """分析认得的物品名：配方里出现过的物品 + 全部液体/气体（和 logistics._ake_templates 的候选是同一批）。"""
    global _NAMES
    if _NAMES is None:
        import json
        from paths import ROOT
        rec = json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))
        names = json.loads((ROOT / "data" / "item_id_names.json").read_text(encoding="utf-8"))
        fluids = {f.stem for f in (ROOT / "resource" / "icons" / "fluid").glob("*.png")}
        ids = {k for r in rec for k in list(r["inputs"]) + list(r["outputs"])} | fluids
        _NAMES = sorted({names[i] for i in ids if names.get(i)})
    return _NAMES


def check_items(marks: list[dict]) -> list[dict]:
    """物品名认不出来的标记 → [{no, item, suggest: [名字…]}]。物品那一栏是随手打字的，打错了分析里这个口送的东西没有设备吃
    （2026-11-14 issue #1：应龙关填的「水」「重积酸」，应该是清水、沉积酸）。suggest：包含它的 / 长得像的名字，最多 3 个。"""
    import difflib
    names = item_names()
    known = set(names)
    out = []
    for m in marks:
        it = (m.get("item") or "").strip()
        if not it or it in known:
            continue
        sug = sorted((n for n in names if it in n or n in it), key=len)[:3]
        sug += [n for n in difflib.get_close_matches(it, names, n=3, cutoff=0.5) if n not in sug]
        out.append({"no": m.get("no"), "item": it, "suggest": sug[:3]})
    return out


def numbered_image(canvas: np.ndarray, marks: list[dict], scale: float = 0.8, dropped: list[dict] | None = None) -> Image.Image:
    """红圈 = 程序标的输入端，蓝圈 = 作者补的；灰色虚圈 + 字母 = 贴边但程序判断不算输入的准入口（作者可以加回）。"""
    im = cv2.resize(canvas, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    pil = Image.fromarray(cv2.cvtColor(im, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    F = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 18)
    W, H = pil.size

    placed = []

    def place(x, y, w):
        """编号牌位置：依次试右上、左上、右下、左下、更远的左边，取第一个不出图、不压别的编号牌的
        （2026-11-09 输入端都贴边，编号被切掉；挨着的两个口编号牌叠在一起）。"""
        bw, bh = w + 8, 24
        cands = [(x + 12, y - 30), (x - 22 - bw, y - 30), (x + 12, y + 8), (x - 22 - bw, y + 8), (x - 22 - bw, y - 12), (x - 30 - 2 * bw, y - 12)]
        for tx, ty in cands + [cands[0]]:
            r = (tx - 3, ty, tx - 3 + bw, ty + bh)
            if r[0] < 0 or r[1] < 0 or r[2] > W or r[3] > H:
                continue
            if any(not (r[2] <= q[0] or r[0] >= q[2] or r[3] <= q[1] or r[1] >= q[3]) for q in placed):
                continue
            placed.append(r)
            return tx, ty
        tx, ty = min(max(3, x + 12), W - bw), min(max(0, y - 30), H - bh)
        placed.append((tx - 3, ty, tx - 3 + bw, ty + bh))
        return tx, ty

    for i, d in enumerate(dropped or []):
        x, y = d["p"][0] * scale, d["p"][1] * scale
        for a0 in range(0, 360, 40):
            dr.arc([x - 18, y - 18, x + 18, y + 18], a0, a0 + 20, fill=(120, 120, 120), width=3)
        t = chr(ord("A") + i % 26)
        w = dr.textlength(t, font=F)
        tx, ty = place(x, y, w)
        dr.rounded_rectangle([tx - 3, ty, tx + w + 5, ty + 24], 5, fill=(120, 120, 120))
        dr.text((tx + 1, ty), t, fill="white", font=F)
    for m in marks:
        x, y = m["p"][0] * scale, m["p"][1] * scale
        col = (220, 30, 30) if m.get("source") == "auto" else (30, 90, 230)
        dr.ellipse([x - 18, y - 18, x + 18, y + 18], outline=col, width=3)
        t = str(m["no"])
        w = dr.textlength(t, font=F)
        tx, ty = place(x, y, w)
        dr.rounded_rectangle([tx - 3, ty, tx + w + 5, ty + 24], 5, fill=col)
        dr.text((tx + 1, ty), t, fill="white", font=F)
    return pil
