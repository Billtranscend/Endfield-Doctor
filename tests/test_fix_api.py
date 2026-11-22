"""作者修正的保存接口和准入口表接口（0.10.13 功能一的存储和接口部分）：bpa_fix_batch 的任务（存 → 只重算一次 → 前后比对撤「对」）、
bpa_gate_table / bpa_gate_image 的行、小图、总览图，bpa_design 末尾的准入口汇总卡，「对不上的旧设置」的小图。

小据点照 tests/test_gate_fixes.py 的 _scene 造（三条线：输入端 → 物品准入口 L1/L2/L3 → 粉碎机，每台要芽针 30/分），
再补 project.json 和蓝图档案（临时目录里的 data/blueprints/档案：底账、配方表、游戏读到的准入口限速），
project.BPA、project.ROOT 都指到临时目录。任务函数 _bpa_fix_batch 直接同步调（不开线程、不写日志文件）。不读本机实测数据。
"""
import base64
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app"), str(ROOT / "tests")]

import api as A  # noqa: E402
import test_gate_fixes as TG  # noqa: E402
from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import inputs as INP  # noqa: E402
from bpa import project as P  # noqa: E402

SC3 = {"devices": [("粉碎机", (400, y), TG.R["碎芽针"]) for y in (300, 500, 700)],
       "nodes": {f"L{k}": ("准入口", (200, y), "物品") for k, y in ((1, 300), (2, 500), (3, 700))},
       "gates": [(k, (40, y), "芽针", 30, f"L{k}") for k, y in ((1, 300), (2, 500), (3, 700))],
       "links": [(f"L{k}", k - 1, "belt") for k in (1, 2, 3)]}
KEY = {k: D.gate_key(TG.BP, (200, y)) for k, y in ((1, 300), (2, 500), (3, 700))}
DEV = {k: f"粉碎机@400,{y}" for k, y in ((1, 300), (2, 500), (3, 700))}


@contextlib.contextmanager
def project(sc=SC3, limits=None, gates_fix=None, confirm=None, **kw):
    """临时项目「项目」、据点「测试」，先按现在的 fixes.json 算一遍（写 design.json、gates_view.json）→ (据点目录, 底账, 配方表, 档案限速)。
    kw 交给 TG._scene（比如 K = transform 的比例）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bd, led, rec, gl, _ = TG._scene(td, sc, limits=limits, gates_fix=gates_fix, **kw)
        proj = bd.parent.parent
        (proj / "project.json").write_text(json.dumps({"name": "项目", "groups": {"测试": [TG.BP]}, "archive": "档案"}, ensure_ascii=False),
                                           encoding="utf-8")
        (bd / "inputs_auto.json").write_text("[]", encoding="utf-8")
        arch = td / "data" / "blueprints" / "档案" / "ledger"
        (arch / "gate_limits").mkdir(parents=True)
        (arch / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
        (arch / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
        if gl:
            (arch / "gate_limits" / "测试.json").write_text(json.dumps(gl, ensure_ascii=False), encoding="utf-8")
        if confirm:
            (proj / "confirm.json").write_text(json.dumps({"测试": {"devices": confirm}}, ensure_ascii=False), encoding="utf-8")
        old = P.BPA, P.ROOT
        P.BPA, P.ROOT = td, td
        try:
            P.rerun_design("项目", "测试")
            yield bd, led, rec, gl
        finally:
            P.BPA, P.ROOT = old


def _job(changes) -> tuple[A.Job, str]:
    """同步跑一次修正任务 → (任务, 日志)。"""
    job = A.Job(kind="bpa", state="running")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        A.Api()._bpa_fix_batch(job, "项目", "测试", changes)
    return job, buf.getvalue()


def _rates(bd) -> dict:
    return {f"{r['device']}@{r['x']},{r['y']}": r["rate"] for r in json.loads((bd / "design.json").read_text(encoding="utf-8"))["devices"]}


def _img(url: str):
    assert url.startswith("data:image/jpeg;base64,"), url[:40]
    return cv2.imdecode(np.frombuffer(base64.b64decode(url.split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)


def _gate(key: str, limit=None, count=None):
    return {"kind": "gates", "key": key, "value": {"limit": limit, "count": count}}


def test_fix_job_saves_once_and_revokes_changed():
    """一个任务存两个口、只重算一次：两台的开工率变了。原来点「对」的那台撤掉，说「不对」的留着，没变的那台「对」留着，
    日志点名（拼合图坐标，和设计分析表一样）。撤销一个口：回到原来的结果。"""
    conf = {DEV[1]: {"verdict": "ok"}, DEV[2]: {"verdict": "wrong", "actual": "间歇"}, DEV[3]: {"verdict": "ok"}}
    with project(confirm=conf) as (bd, *_):
        assert _rates(bd) == {DEV[1]: 1.0, DEV[2]: 1.0, DEV[3]: 1.0}
        job, log = _job([_gate(KEY[1], 18), _gate(KEY[2], 12)])
        assert _rates(bd) == {DEV[1]: 0.6, DEV[2]: 0.4, DEV[3]: 1.0}, _rates(bd)
        assert job.result["changed"] == 2 and job.result["revoked"] == [DEV[1]] and job.result["wrong"] == [DEV[2]], job.result
        dv = json.loads((bd.parent.parent / "confirm.json").read_text(encoding="utf-8"))["测试"]["devices"]
        assert set(dv) == {DEV[2], DEV[3]} and dv[DEV[2]]["verdict"] == "wrong", dv
        assert "物品准入口 (200,300)（芽针） → 限制速度 18/分" in log, log
        assert "结果变了 2 台" in log and "粉碎机 (400,300) 满速 100% → " in log and "60%" in log, log
        assert "1 台原来点过「对」" in log and "粉碎机 (400,300)" in log.split("已经撤掉")[1], log
        assert "1 台原来说过「不对」" in log and "说法留着" in log, log
        g = D.load_fixes(bd)["gates"]
        assert set(g) == {KEY[1], KEY[2]} and g[KEY[1]]["limit"] == 18 and g[KEY[1]]["item"] == "芽针" and g[KEY[1]]["p_bp"] == [200, 300], g
        job, log = _job([{"kind": "gates", "key": KEY[1], "value": None}])    # 撤销
        assert _rates(bd) == {DEV[1]: 1.0, DEV[2]: 0.4, DEV[3]: 1.0} and set(D.load_fixes(bd)["gates"]) == {KEY[2]}, _rates(bd)
        assert job.result["changed"] == 1 and job.result["revoked"] == [] and "撤销" in log, (job.result, log)
        job, log = _job([])                                                     # 只重算：没变
        assert job.result["changed"] == 0 and "只重算" in log and "都没变" in log, log
        # 改配方（结果不一定变）：这台原来的「对」存的时候就撤掉、日志点名；「不对」留着、日志提醒
        (bd.parent.parent / "confirm.json").write_text(json.dumps({"测试": {"devices": {DEV[3]: {"verdict": "ok"}, DEV[1]: {"verdict": "wrong"}}}},
                                                                ensure_ascii=False), encoding="utf-8")
        rc = "芽针×1 → 芽针粉末×1（2秒）"
        job, log = _job([{"kind": "recipes", "key": DEV[3], "value": rc}, {"kind": "recipes", "key": DEV[1], "value": rc}])
        assert "改了配方的 1 台原来点过「对」" in log and "粉碎机 (400,700)" in log.split("改了配方的 1 台原来点过")[1], log
        assert "改了配方的 1 台原来说过「不对」" in log and DEV[3] in job.result["revoked"], (log, job.result)
        dv = json.loads((bd.parent.parent / "confirm.json").read_text(encoding="utf-8"))["测试"]["devices"]
        assert set(dv) == {DEV[1]} and dv[DEV[1]]["verdict"] == "wrong", dv


def test_gate_table_rows_and_summary_card():
    """准入口表：作者填过的排最前，再是有料经过的（同物品从上到下），最后是输入端；默认筛选只显示有料的和作者填过的；
    每行一张 140×140 小图，有总览图。别人的据点（没有游戏读数文件）：待确认末尾多一张准入口汇总卡，「上面没说的都没开」以后变成已答。
    有游戏读数的据点不出这张卡。"""
    with project(gates_fix={KEY[2]: TG._fix((200, 500), 12)}) as (bd, *_):
        a = A.Api()
        t = a.bpa_gate_table("项目", "测试")
        assert t["ok"] and not t["need_rerun"] and not t["has_read"] and t["read_date"] == "", t.get("error")
        rows = t["rows"]
        assert [r["key"] for r in rows[:3]] == [KEY[2], KEY[1], KEY[3]] and [r["no"] for r in rows] == list(range(1, 7)), rows
        assert [r["seg"] for r in rows] == [0, 1, 1, 2, 2, 2] and [r["group"] for r in rows[3:]] == ["输入端"] * 3, rows
        assert [r["show"] for r in rows] == [True] * 3 + [False] * 3 and rows[0]["fix"]["limit"] == 12 and rows[1]["fix"] is None, rows
        assert t["counts"] == {"有料": 3, "已填": 1, "说没开": 0, "游戏读到": 0, "输入端": 3, "没追到": 0, "填不了": 0}, t["counts"]
        assert all(_img(r["thumb"]).shape[:2] == (140, 140) for r in rows) and _img(t["overview"]) is not None, "小图"
        assert rows[0]["item_name"] == "芽针" and rows[0]["flow"] == {"芽针": 12} and rows[0]["held"] == {"芽针": 18}, rows[0]
        big = _img(a.bpa_gate_image("项目", "测试", KEY[2])["image"])
        assert max(big.shape[:2]) == 480, big.shape
        assert a.bpa_gate_image("项目", "测试", "没有这个@1,1")["ok"] is False
        d = a.bpa_design("项目", "测试")
        card = d["pending"][-1]
        assert card["kind"] == "准入口限速" and card["open"] and card["n"] == 3 and card["n_open"] == 2, card
        assert card["keys"] == [KEY[2], KEY[1], KEY[3]] and card["open_keys"] == [KEY[1], KEY[3]], card
        assert card["answered"] == {"filled": 1, "off": 0} and "还有 2 个没说" in card["msg"] and "这 2 个口都没开" in card["msg"], card
        assert _img(card["image"]) is not None and d["gate_summary"]["已填"] == 1 and not d["gate_summary"]["need_rerun"], d["gate_summary"]
        _job([_gate(k) for k in card["open_keys"]])                            # 上面没说的这些都没开
        card = a.bpa_design("项目", "测试")["pending"][-1]
        assert card["kind"] == "准入口限速" and not card["open"] and card["answered"] == {"filled": 1, "off": 2} and "都答过了" in card["msg"], card
    with project(limits={"L1": {"limit": 12}}) as (bd, *_):                     # 皇帝产线这种：有游戏读数
        a = A.Api()
        t = a.bpa_gate_table("项目", "测试")
        assert t["has_read"] and t["read_date"] == "11-09" and t["rows"][0]["key"] == KEY[1] and t["rows"][0]["src"] == "游戏", t["rows"][0]
        d = a.bpa_design("项目", "测试")
        assert not any(p["kind"] == "准入口限速" for p in d["pending"]) and d["gate_summary"]["游戏读到"] == 1, d["gate_summary"]


def test_stale_settings_and_need_rerun():
    """对不上的旧设置：蓝图还在据点里的，按旧位置（from_bp）换到现在的拼合图上、出虚线圈的小图；蓝图不在了的只写字（没有图）。
    对不上的只能撤销，不能改值。旧版本算的据点（没有 gates_view.json）：表、大图都返回 need_rerun。"""
    drift = TG._fix((200, 330), 18)                                            # 漂了 30 px：附近没有准入口
    gone = TG._fix((200, 300), 18, bp="别的图")
    with project(gates_fix={"漂了": drift, "别的图@200,300": gone}) as (bd, *_):
        a = A.Api()
        t = a.bpa_gate_table("项目", "测试")
        st = {s["key"]: s for s in t["stale"]}
        assert set(st) == {"漂了", "别的图@200,300"}, st
        tf = json.loads((bd / "transform.json").read_text(encoding="utf-8"))
        assert st["漂了"]["c"] == INP.from_bp(tf, TG.BP, [200, 330]) and _img(st["漂了"]["thumb"]).shape[:2] == (140, 140), st["漂了"]
        assert st["别的图@200,300"]["c"] is None and st["别的图@200,300"]["thumb"] == "", st["别的图@200,300"]
        assert a.bpa_gate_image("项目", "测试", "漂了")["ok"]
        r = a.bpa_gate_image("项目", "测试", "别的图@200,300")
        assert not r["ok"] and "不在这个据点" in r["error"], r
        try:
            P.check_fixes("项目", "测试", [_gate("漂了", 6)])
            assert False, "对不上的旧设置不能改值"
        except ValueError:
            pass
        P.check_fixes("项目", "测试", [{"kind": "gates", "key": "漂了", "value": None}])  # 能删
        (bd / "gates_view.json").unlink()
        t = a.bpa_gate_table("项目", "测试")
        assert not t["ok"] and t["need_rerun"] and "重算" in t["error"], t
        assert a.bpa_gate_image("项目", "测试", KEY[1])["need_rerun"]
        assert a.bpa_design("项目", "测试")["gate_summary"] == {"need_rerun": True}
        _job([])                                                               # 「重算」= 不带改动的修正任务
        assert a.bpa_gate_table("项目", "测试")["ok"]


def test_gate_ends_all_marked():
    """表里「从哪来 → 到哪去」列出的每台设备（各最多 3 台、都写了坐标），小图、大图上都标出来：一边只有一台写「进」「出」，
    几台按表里的顺序写「进1」「进2」…（界面表格也这样标）。"""
    row = {"from": [{"name": "仓库取货口", "c": [60, 300]}],
           "to": [{"name": "粉碎机", "c": [500, 200]}, {"name": "粉碎机", "c": [500, 420]}]}
    assert [(d["c"], t) for d, t, _ in A._gate_ends(row)] == [([60, 300], "进"), ([500, 200], "出1"), ([500, 420], "出2")]
    img = np.full((800, 800, 3), 232, np.uint8)
    assert _img(A._gate_pic(img, [300, 300], row, 140, 210)).shape[:2] == (140, 140)
    big = _img(A._gate_pic(img, [300, 300], row, 480, 150, names=True))
    blue = (np.abs(big.astype(int) - np.array(A.GATE_OUT[::-1])).sum(axis=2) < 40)   # 「出」的蓝底字块（BGR）
    n, _ = cv2.connectedComponents(blue.astype(np.uint8))
    assert n - 1 >= 2, n                                                                  # 两台都标了


if __name__ == "__main__":
    sys.exit(run([test_fix_job_saves_once_and_revokes_changed, test_gate_table_rows_and_summary_card, test_stale_settings_and_need_rerun,
                  test_gate_ends_all_marked]))
