"""物流件方向和连接（src/bpa/flow.py，任务计划第 4 项）：用皇帝产线实测拼合图跑（有数据才跑）。
运行：.venv/Scripts/python.exe tests/test_flow.py"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bpa import flow as F  # noqa: E402
from bpa import vision as V  # noqa: E402

BASE = ROOT / "debug/bpa/实测-皇帝产线/base"
LED = ROOT / "data/blueprints/1.6皇帝产线/ledger"


def _run(area):
    bd = BASE / area
    if not (bd / "composite.png").exists():
        return None
    led = json.loads((LED / f"{area}.json").read_text(encoding="utf-8"))
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    pts = [{"name": d["name"], "c": list(V.far_to_canvas(tf, d["x"], d["y"]))} for d in led["devices"]]
    return F.build(V.rd(bd / "composite.png"), json.loads((bd / "logistics.json").read_text(encoding="utf-8")), pts)


def _agree(res):
    """物流件定的方向 vs 小三角 ≥2 票：一致数、冲突数。"""
    ok = bad = 0
    for lk in res["links"]:
        nb = [b for b in lk["basis"] if "小三角" not in b and "顺过来" not in b and "端口布局" not in b]
        # 端口布局定的方向不在这里比（弯道上的小三角常读错，2026-11-10 景玉谷天有洪炉 (961,440) 顶上出口的弯带 2 票反了）；
        # 它和小三角 ≥3 票相反时单独记「端口布局存疑」
        if lk["dir"] and nb and abs(lk["arrows"]) >= 2:
            if (lk["arrows"] > 0) == (lk["dir"] == "a->b"):
                ok += 1
            else:
                bad += 1
    return ok, bad


def test_jingyugu():
    res = _run("景玉谷")
    if res is None:
        return
    # 2026-11-09：34 条连接，物流件之间 17 条两头都追回，没有分叉/单向/冲突。
    # 2026-11-10 端口布局：研磨机 (205,355) 右边那条是两条带子被追成了一条（往上流的竖带 + 底下往左流进研磨机的一格），
    # 端口布局和小三角说法相反 → 记「端口布局存疑」、不定方向；只允许这一条
    doubt = [i for i in res["issues"] if i["kind"] == "端口布局存疑"]
    assert not [i for i in res["issues"] if i["kind"] != "端口布局存疑"], res["issues"]
    assert len(doubt) <= 1 and all(abs(i["at"][0] - 270) <= 40 for i in doubt), doubt
    node_links = [lk for lk in res["links"] if lk.get("b") is not None]
    assert len([lk for lk in res["links"] if lk.get("free")]) >= 50  # 设备 → 设备直连的带子（2026-11-09 补的全图走向）
    assert len(node_links) >= 17 and all(lk["both"] for lk in node_links)
    ok, bad = _agree(res)
    assert bad == 0 and ok >= 15, (ok, bad)
    # 暗管出口 → 管道分流器（854,714）的进料口；管子中间被仓库口圆图标压住一格，要跳过去
    lk = [lk for lk in res["links"] if lk["a"] is not None and res["nodes"][lk["a"]]["c"] == [854, 714] and lk["a_side"] == "S"][0]
    assert lk["dir"] == "b->a" and (lk["device"] or {}).get("name") == "暗管出口" and lk["dev_side"] == "E", lk
    assert lk["jumps"], lk
    # 端口布局（2026-11-10）：粉碎机底下那排出料带，原来按最近标签认成了左边隔壁那台；按出料口格认是正上方那台
    for x, want in ((917, [972, 1393]), (1330, [1381, 1393])):
        lk = [lk for lk in res["links"] if lk.get("free") and any(abs(q[0] - x) <= 3 and abs(q[1] - 1429) <= 3 for q in (lk["path"][0], lk["path"][-1]))][0]
        ends = [d for d in (lk.get("a_dev"), lk.get("device")) if d and d.get("how") == "端口布局"]
        assert any(d["name"] == "粉碎机" and abs(d["c"][0] - want[0]) <= 3 and abs(d["c"][1] - want[1]) <= 3 and d["port"] == "out" for d in ends), lk


def _free_between(res, a, b):
    """两头分别是设备 a、b（名字, x, y）的设备直连带。"""
    def hit(d, t):
        return d and d["name"] == t[0] and abs(d["c"][0] - t[1]) <= 4 and abs(d["c"][1] - t[2]) <= 4
    return [lk for lk in res["links"] if lk.get("free") and
            ((hit(lk.get("a_dev"), a) and hit(lk.get("device"), b)) or (hit(lk.get("a_dev"), b) and hit(lk.get("device"), a)))]


def test_short_belts_and_under_pipe_gates():
    """2026-11-10：一格弯带按端口格补（格子模型）；带子从管道准入口底下穿过去；带子头不认成只有管口的暗管。"""
    res = _run("武陵城")
    if res is None:
        return
    lk = _free_between(res, ("种植机", 155, 1158), ("采种机", 120, 987))
    assert lk and lk[0].get("bridge") and lk[0]["dir"] == "a->b" and lk[0]["a_dev"]["name"] == "种植机", lk
    lk = _free_between(res, ("采种机", 326, 1463), ("种植机", 122, 1431))
    assert lk and lk[0]["dir"] and (lk[0]["dir"] == "a->b") == (lk[0]["a_dev"]["name"] == "采种机"), lk
    assert not [lk for lk in res["links"] if lk["layer"] == "belt" and "暗管" in ((lk.get("device") or {}).get("name") or "")]
    res = _run("景玉谷")
    lk = _free_between(res, ("种植机", 1585, 236), ("精炼炉", 1416, 65))  # 中间从清水管道准入口 (1636,30) 底下穿过
    assert lk and lk[0]["jumps"] and (lk[0]["dir"] == "a->b") == (lk[0]["a_dev"]["name"] == "种植机"), lk


def test_port_layout_orientation():
    """端口布局：游戏数据里的端口格 + 口格折角定朝向（2026-11-10，看图核对过的几台）。"""
    from bpa import ports as PT
    bd = BASE / "武陵城"
    if not (bd / "composite.png").exists():
        return
    led = json.loads((LED / "武陵城.json").read_text(encoding="utf-8"))
    tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
    pts = [{"name": d["name"], "c": list(V.far_to_canvas(tf, d["x"], d["y"]))} for d in led["devices"]]
    res = PT.all_ports(V.rd(bd / "composite.png"), pts)
    got = {(p["name"], round(p["c"][0]), round(p["c"][1])): r for p, r in zip(pts, res) if r}

    def one(name, x, y):
        return next(r for (n, cx, cy), r in got.items() if n == name and abs(cx - x) <= 3 and abs(cy - y) <= 3)
    # 精炼炉 (1215,680)：上进下出，进水口在左（W）
    r = one("精炼炉", 1215, 680)
    assert r["rot"] == 0, r
    sides = {(p["role"], p["pipe"]): p["side"] for p in r["ports"]}
    assert sides[("in", False)] == "N" and sides[("out", False)] == "S" and sides[("in", True)] == "W" and sides[("out", True)] == "E", sides
    # 顶上那排精炼炉 (1590,202) 转了 180°：下进上出，进水口在右（E）
    r = one("精炼炉", 1590, 202)
    assert r["rot"] == 180 and {p["side"] for p in r["ports"] if p["pipe"] and p["role"] == "in"} == {"E"}, r
    # 竖着放的灌装机 (787,1072)：左进右出（6×4 转成 4×6）
    r = one("灌装机", 787, 1072)
    assert r["rot"] == 270 and {p["side"] for p in r["ports"] if not p["pipe"] and p["role"] == "in"} == {"W"}, r
    # 只有管口的废水处理机：图上没有折角可读，不给朝向
    assert all(r["rot"] is None for (n, _, _), r in got.items() if n == "废水处理机")


def test_wulingcheng_loop_and_covered():
    res = _run("武陵城")
    if res is None:
        return
    ok, bad = _agree(res)
    assert bad == 0 and ok >= 60, (ok, bad)
    # 重息壤准入口（限速 12）外面的分流回环（4.7：装备原件机 12÷3÷2）
    loops = [s for s in res["structures"] if s["kind"] == "分流回环"]
    assert any(abs(s["c"][0] - 305) <= 6 and abs(s["c"][1] - 645) <= 6 and "准入口" in s["types"] for s in loops), loops
    # 上面压着管道的传送带汇流器：出口 W（西边黄带上两个小三角朝西，2026-11-09 放大核对）。
    # 以前按小三角「改成 E」是把压在上面那根管子的白三角当成了带子的票，管子上的点现在不投票
    n = [n for n in res["nodes"] if n["c"] == [885, 234]][0]
    assert n["covered"] and n["out"] == "W", n
    assert not [i for i in res["issues"] if i["kind"] in ("方向冲突", "小三角反了")], res["issues"]
    # 端口布局（2026-11-10）：塑形机 (535,167) 顶上出口 → U 形弯带 → 左边协议储存箱 (427,167) 顶上进口（原来认成了另一个储存箱）
    lk = [lk for lk in res["links"] if lk.get("free") and lk["path"][0] == [443, 98] and lk["path"][-1] == [478, 104]][0]
    assert lk["dir"] == "b->a" and lk["device"]["name"] == "塑形机" and lk["a_dev"]["name"] == "协议储存箱", lk


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
