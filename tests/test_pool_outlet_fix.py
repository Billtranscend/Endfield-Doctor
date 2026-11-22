"""池子出口送什么的保存和接口（0.10.13 功能三的存储和接口部分）：bpa_fix_batch 的任务（池子配方和出口回答一批存 → 只重算一次 →
这台池子存的时候撤「对」、别的设备按重算前后比对撤）、日志（拼合图坐标、不出现设备键）、池子卡和设计分析表带的字段、撤销配方时
出口回答一起删、导出的 .bpa.zip 带上出口回答、导入方的设计分析和断水影响认得。

小据点是 tests/test_pool_outlets.py 的场景 A（作者那组的最小复刻：池子 2/7、天有洪炉 5/14、提纯机 1/14），用 test_pool_parallel._scene
造，再补 project.json 和蓝图档案（临时目录里的 data/blueprints/档案），project.BPA、project.ROOT 都指到临时目录。底账远景坐标整体挪了
(1000,2000)（transform.F0），设备键「扩容反应池@1300,2300」和拼合图坐标 (300,300) 不一样，日志用哪个看得出来。
任务函数 _bpa_fix_batch 直接同步调（不开线程、不写日志文件）。不读本机实测数据。
"""
import contextlib
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app"), str(ROOT / "tests")]

import api as A  # noqa: E402
import blueprint as bp  # noqa: E402
import test_pool_outlets as TO  # noqa: E402
import test_pool_parallel as TP  # noqa: E402
from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import importer  # noqa: E402
from bpa import pipenet as PN  # noqa: E402
from bpa import project as P  # noqa: E402

SHIFT = (1000, 2000)  # 底账远景坐标 = 拼合图坐标 + SHIFT
PK, HL, TC = "扩容反应池@1300,2300", "天有洪炉@1650,2300", "提纯机@1300,2650"
AUTHOR = {"belt": ["息壤"], "pipe": ["惰性壤晶废液", "壤晶废液"]}


@contextlib.contextmanager
def project(confirm=None):
    """临时项目「项目」、据点「测试」：场景 A，作者勾了池子的两条配方，先算一遍（写 design.json）→ 据点目录。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bd, led, rec, _ = TP._scene(td, TO._scene_a())
        for d in led["devices"] + rec:  # 底账远景坐标挪开，拼合图坐标不变
            d["x"], d["y"] = d["x"] + SHIFT[0], d["y"] + SHIFT[1]
        (bd / "transform.json").write_text(json.dumps({"K": 1.0, "F0": list(SHIFT), "bps": {}}), encoding="utf-8")
        proj = bd.parent.parent
        (proj / "project.json").write_text(json.dumps({"name": "项目", "groups": {"测试": ["图甲"]}, "archive": "档案"}, ensure_ascii=False),
                                           encoding="utf-8")
        (proj / "fixes.json").write_text(json.dumps({"测试": {"pool_recipes": {PK: [TO.YH, TO.FY]}}}, ensure_ascii=False), encoding="utf-8")
        (bd / "inputs_auto.json").write_text("[]", encoding="utf-8")
        arch = td / "data" / "blueprints" / "档案" / "ledger"
        arch.mkdir(parents=True)
        (arch / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
        (arch / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
        if confirm:
            (proj / "confirm.json").write_text(json.dumps({"测试": {"devices": confirm}}, ensure_ascii=False), encoding="utf-8")
        old = P.BPA, P.ROOT
        P.BPA, P.ROOT = td, td
        try:
            P.rerun_design("项目", "测试")
            yield bd
        finally:
            P.BPA, P.ROOT = old


def _job(changes) -> tuple[A.Job, str, int]:
    """同步跑一次修正任务 → (任务, 日志, 重算了几次)。"""
    job = A.Job(kind="bpa", state="running")
    buf, n, old = io.StringIO(), [], P.rerun_design
    P.rerun_design = lambda *a, **k: n.append(1) or old(*a, **k)
    try:
        with contextlib.redirect_stdout(buf):
            A.Api()._bpa_fix_batch(job, "项目", "测试", changes)
    finally:
        P.rerun_design = old
    return job, buf.getvalue(), len(n)


def _rows(bd) -> dict:
    return {P.device_key(r): r for r in json.loads((bd / "design.json").read_text(encoding="utf-8"))["devices"]}


def _conf(bd) -> dict:
    return json.loads((bd.parent.parent / "confirm.json").read_text(encoding="utf-8"))["测试"]["devices"]


def test_fix_job_recipes_and_outlets_once():
    """池子卡上配方和出口一起存（一批两项）：只重算一次。结果和场景 A 手算的一样（池子 2/7、洪炉 5/14、提纯机 1/14）；
    这台池子原来的「对」存的时候就撤（配方没变、出口回答变了），天有洪炉的「对」按重算前后比对撤，提纯机说过「不对」的留着；
    日志写「扩容反应池 (300,300) 出口：带出口送息壤；管出口送惰性壤晶废液、壤晶废液」（拼合图坐标），不出现设备键。
    撤销出口回答：回到原来的结果，逐台一样。"""
    conf = {PK: {"verdict": "ok"}, HL: {"verdict": "ok"}, TC: {"verdict": "wrong", "actual": "间歇"}}
    with project(confirm=conf) as bd:
        base = _rows(bd)
        assert base[PK]["rate"] == 1.0 and base[HL]["rate"] == 0.0 and base[TC]["rate"] == 0.25, (base[PK]["rate"], base[HL]["rate"])
        assert base[PK]["c"] == [300, 300], base[PK]["c"]
        job, log, n = _job([{"kind": "pool_recipes", "key": PK, "value": [TO.YH, TO.FY]}, {"kind": "pool_outlets", "key": PK, "value": AUTHOR}])
        r = _rows(bd)
        assert n == 1, n
        assert abs(r[PK]["rate"] - 2 / 7) < 2e-3 and abs(r[HL]["rate"] - 5 / 14) < 2e-3 and abs(r[TC]["rate"] - 1 / 14) < 2e-3, r[PK]["rate"]
        assert "扩容反应池 (300,300) 出口：带出口送息壤；管出口送惰性壤晶废液、壤晶废液" in log, log
        assert "扩容反应池 (300,300) → 2 条配方同时跑" in log and "@" not in log, log
        assert "改了出口送什么的 1 台原来点过「对」" in log and "改了配方的" not in log, log
        assert "结果变了 3 台" in log and "原来说过「不对」，结果也变了" in log, log
        assert job.result["revoked"] == [PK, HL] and job.result["wrong"] == [TC] and job.result["changed"] == 3, job.result
        assert set(_conf(bd)) == {TC}, _conf(bd)
        fx = D.load_fixes(bd)
        assert {k: v for k, v in fx["pool_outlets"][PK].items() if k != "at"} == AUTHOR and fx["pool_outlets"][PK]["at"], fx
        assert fx["pool_recipes"] == {PK: [TO.YH, TO.FY]}, fx
        # 同样的回答再存一次（只有存的时间不同）：这台的「对」不撤
        (bd.parent.parent / "confirm.json").write_text(json.dumps({"测试": {"devices": {PK: {"verdict": "ok"}}}}, ensure_ascii=False),
                                                       encoding="utf-8")
        job, log, n = _job([{"kind": "pool_outlets", "key": PK, "value": AUTHOR}])
        assert set(_conf(bd)) == {PK} and job.result["revoked"] == [] and "都没变" in log, (job.result, log)
        job, log, n = _job([{"kind": "pool_outlets", "key": PK, "value": None}])     # 撤销
        assert "扩容反应池 (300,300) 出口送什么 → 撤销" in log and job.result["revoked"] == [PK] and n == 1, log
        assert PK not in D.load_fixes(bd)["pool_outlets"], D.load_fixes(bd)
        after = _rows(bd)
        assert {k: (v["rate"], v["state"], v["basis"]) for k, v in after.items()} == \
               {k: (v["rate"], v["state"], v["basis"]) for k, v in base.items()}, "撤销后回到原来的结果"
        assert not any("pass_out" in v for v in after.values())


def test_cards_and_rows_carry_outlets():
    """池子卡（待确认里的、表格行「改配方」打开的）带 outlets（作者的回答）、pass_out（design.json 这台那一行的）、outlet_notes
    （design.json 说明里点了这台的）；设计分析表的池子行透传 pass_out，别的行没有。只在接口拼，design.json 的待确认不多字段。
    中间产物、产物没出口的说明挂到卡上；撤销池子配方（这台没在用）时出口回答一起删，日志说一句。"""
    with project() as bd:
        a = A.Api()
        card = a.bpa_pool_card("项目", "测试", PK)["card"]
        assert card["outlets"] is None and card["pass_out"] is None and card["outlet_notes"] == [], card
        _job([{"kind": "pool_outlets", "key": PK, "value": AUTHOR}])
        card = a.bpa_pool_card("项目", "测试", PK)["card"]
        assert {k: v for k, v in card["outlets"].items() if k != "at"} == AUTHOR, card["outlets"]
        po = card["pass_out"]
        assert len(po) == 1 and po[0]["item"] == "息壤" and abs(po[0]["out"] - 150 / 7) < 0.02 and card["outlet_notes"] == [], po
        rows = {r["key"]: r for r in a.bpa_design("项目", "测试")["devices"]}
        assert rows[PK]["pass_out"] == po and not any("pass_out" in r for k, r in rows.items() if k != PK), rows[PK]
        dj = json.loads((bd / "design.json").read_text(encoding="utf-8"))
        assert not any("outlets" in p or "outlet_notes" in p for p in dj.get("pending") or []), "不写回 design.json"
        _job([{"kind": "pool_outlets", "key": PK, "value": {"belt": [], "pipe": ["液化息壤", "壤晶废液"]}}])
        card = a.bpa_pool_card("项目", "测试", PK)["card"]
        kinds = sorted(n["kind"] for n in card["outlet_notes"])
        assert kinds == ["产物没出口", "出口送中间产物"] and card["pass_out"] is None, card["outlet_notes"]
        assert card["outlets"]["belt"] == [] and card["outlets"]["pipe"] == ["液化息壤", "壤晶废液"], card["outlets"]
        # 待确认里的池子卡（_pending_view 收整份 design.json）：同样带上
        dj = json.loads((bd / "design.json").read_text(encoding="utf-8"))
        it = {"kind": "池子配方", "key": PK, "device": "扩容反应池[300, 300]", "c": [300, 300], "ins": [], "outs": [], "answer": None}
        pc = a._pending_view("项目", "测试", dict(dj, pending=[it]))[0]
        assert pc["outlets"]["pipe"] == ["液化息壤", "壤晶废液"] and len(pc["outlet_notes"]) == 2 and pc["open"], pc
        job, log, _ = _job([{"kind": "pool_recipes", "key": PK, "value": []}])      # 这台没在用：出口回答一起删
        assert PK not in D.load_fixes(bd).get("pool_outlets", {}) and "出口送什么的回答一起删了" in log, log
        assert a.bpa_pool_card("项目", "测试", PK)["card"]["outlets"] is None


def test_api_rejects_bad_outlets():
    """接口：出口回答不合法当场报错、不开任务、fixes.json 不动；合法的开一个修正任务。"""
    with project() as bd:
        a = A.Api()
        started = []
        a._start = lambda kind, fn, *args, **kw: started.append(args) or {"ok": True}
        f = bd.parent.parent / "fixes.json"
        before = f.read_text(encoding="utf-8")
        bad = [(PK, {"belt": ["清水"]}), (PK, {"pipe": ["息壤"]}), (PK, {"belt": ["息壤", "清水"]}), (PK, {"pipe": ["清水", "清水"]}),
               (PK, {"belt": "息壤"}), (PK, {"belt": ["息壤"], "at": "x"}), (PK, "息壤"), (HL, {"belt": ["息壤"]}),
               ("扩容反应池@1,1", {"belt": ["息壤"]}), ("扩容反应池@1,1", None)]
        for k, v in bad:
            r = a.bpa_fix("项目", "测试", "pool_outlets", k, v)
            assert r["ok"] is False and r["error"] and "@" not in r["error"], (k, v, r)
        r = a.bpa_fix_batch("项目", "测试", [{"kind": "pool_recipes", "key": PK, "value": [TO.YH]},
                                             {"kind": "pool_outlets", "key": PK, "value": {"pipe": ["清水", "污水", "壤晶废液"]}}])
        assert r["ok"] is False and "管出口最多选 2 种" in r["error"], r
        assert f.read_text(encoding="utf-8") == before and not started, started
        assert a.bpa_fix("项目", "测试", "pool_outlets", PK, AUTHOR)["ok"] and len(started) == 1, started


def test_export_import_keeps_outlets():
    """导出的 .bpa.zip 带 fixes.json 的 pool_outlets；导入以后放在 档案/bpa/fixes.json，导入方的设计分析（断水影响用的同一个 analyze）
    认得：池子行有 pass_out；息壤的输入端断了，天有洪炉从池子出口拿的息壤跟着没了（没有出口回答时它本来就是 0，不会出现在名单里）。"""
    with project() as bd:
        _job([{"kind": "pool_outlets", "key": PK, "value": AUTHOR}])
        td = P.BPA
        zp = td / "项目.bpa.zip"
        P.export("项目", zp, log=lambda *_: None, lite=True)
        with zipfile.ZipFile(zp) as z:
            fx = json.loads(z.read("fixes.json"))
        assert fx["测试"]["pool_outlets"][PK]["belt"] == ["息壤"], fx
        old = bp.BLUEPRINTS
        bp.BLUEPRINTS = td / "导入"
        try:
            name = importer.import_zip(zp, log=lambda *_: None)
            dest = bp.BLUEPRINTS / name
            ib = dest / "bpa" / "base" / "测试"
            assert D.load_fixes(ib)["pool_outlets"][PK]["pipe"] == AUTHOR["pipe"], D.load_fixes(ib)
            led = json.loads((dest / "ledger" / "测试.json").read_text(encoding="utf-8"))
            recs = json.loads((dest / "ledger" / "测试_recipes.json").read_text(encoding="utf-8"))["devices"]
            res = D.analyze(ib, led, recs, None, PN.load_inputs(ib))
            pool = next(r for r in res["devices"] if P.device_key(r) == PK)
            assert abs(pool["rate"] - 2 / 7) < 2e-3 and pool["pass_out"][0]["item"] == "息壤", (pool["rate"], pool.get("pass_out"))
            lost = D.cut_impact(ib, led, recs, None, PN.load_inputs(ib), 3, "息壤")
            hl = next((u for u in lost if (u["name"], u["x"], u["y"]) == ("天有洪炉", 1650, 2300)), None)
            assert hl is not None and abs(hl["before"] - 150 / 7) < 0.05 and hl["after"] == 0.0, lost
        finally:
            bp.BLUEPRINTS = old


if __name__ == "__main__":
    sys.exit(run([test_fix_job_recipes_and_outlets_once, test_cards_and_rows_carry_outlets, test_api_rejects_bad_outlets,
                  test_export_import_keeps_outlets]))
