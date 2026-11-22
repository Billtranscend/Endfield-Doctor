"""并发和原子写（2026-11-19 审计第 10 项）。

界面的每次接口调用都在单独的线程里跑（pywebview）。原来 design.run_base 靠改模块变量 RATE_LIMITED_INTAKE、MAIN_BLOCKED 切换三遍计算的模式，
两次设计分析同时跑会互相改掉对方的开关（审计在副本上 21 轮并发 12 轮算错）；design.json 先清空再写，边写边读 15% 读到空文件。
现在：计算模式改成 analyze 的参数；run_base / flow.build 各加一把锁；项目里的 json 走 atomic.write_json；
api.bpa_fix、bpa_label_gates 改走任务（_start），已有任务在跑时不再开第二个。

前四条不依赖本机数据；最后一条用实测项目「实测-皇帝产线」的副本（tests/_testkit.copy_project），没有就跳过。
运行：.venv/Scripts/python.exe tests/test_concurrency.py
"""
import contextlib
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "tests")]

from _testkit import copy_project, project_ledger, run  # noqa: E402
from bpa import atomic  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import project as P  # noqa: E402
from bpa import vision as V  # noqa: E402


def _row(state, rate):
    return {"device": "封装机", "c": [10, 20], "x": 1, "y": 2, "state": state, "rate": rate, "need": {"壤晶": 30.0},
            "got": {"壤晶": 30.0 * rate}, "basis": []}


def test_run_base_switches_are_parameters():
    """run_base 的三遍用参数切模式，跑的时候和跑完以后模块变量都没被改过；design.json 能解析、不留临时文件。
    analyze 打桩（真算要本机数据），只看 run_base 怎么调它。"""
    seen = []

    def fake(bd, led, recipes, gl, inputs=None, **kw):
        seen.append((kw, D.RATE_LIMITED_INTAKE, D.MAIN_BLOCKED))
        if kw.get("main_blocked"):
            return {"devices": [_row("满速", 1.0)]}
        return {"devices": [_row("溢流位（平时空闲）", 0.0)]}  # 有溢流位 → 会走第三遍
    old_an, old_img = D.analyze, D.image
    D.analyze = fake
    D.image = lambda canvas, res: type("Img", (), {"save": lambda self, *a, **k: None})()
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            bd, arch = td / "base" / "测试", td / "arch"
            bd.mkdir(parents=True)
            (arch / "ledger").mkdir(parents=True)
            (arch / "ledger" / "测试.json").write_text(json.dumps({"devices": []}), encoding="utf-8")
            (arch / "ledger" / "测试_recipes.json").write_text(json.dumps({"devices": [{"name": "封装机", "x": 1, "y": 2, "recipe": "壤晶×1 → 壤晶×1"}]}),
                                                             encoding="utf-8")
            V.wr(bd / "composite.png", __import__("numpy").zeros((8, 8, 3), "uint8"))
            res = D.run_base(bd, arch, "测试")
            # 主遍先跑（0.10.13 功能三：池子出口转送比例按主遍定，第二、三遍用它；没有出口回答时不带 pass_split）
            assert [k for k, _, _ in seen] == [{}, {"rate_limited_intake": False}, {"main_blocked": True}], seen
            assert all(r is True and m is False for _, r, m in seen), "跑的过程中模块变量被改了：" + str(seen)
            assert D.RATE_LIMITED_INTAKE is True and D.MAIN_BLOCKED is False
            dj = json.loads((bd / "design.json").read_text(encoding="utf-8"))
            assert dj["devices"][0]["if_main_blocked"]["rate"] == 1.0, dj["devices"][0]
            assert res["summary"]["states"]["溢流位"] == 1
            assert not list(bd.glob(".*.tmp")), list(bd.glob(".*.tmp"))
    finally:
        D.analyze, D.image = old_an, old_img


def test_run_base_is_serialized():
    """两个线程同时调 run_base：第二个要等第一个整个跑完才开始（_RUN_LOCK）。"""
    order, gate = [], threading.Event()

    def fake_inner(bd, archive, area, ledger):
        order.append(("start", area))
        if area == "A":
            gate.wait(2)
            time.sleep(0.2)
        order.append(("end", area))
        return {}
    old = D._run_base
    D._run_base = fake_inner
    try:
        ta = threading.Thread(target=D.run_base, args=(Path("."), None, "A"))
        ta.start()
        time.sleep(0.1)
        tb = threading.Thread(target=D.run_base, args=(Path("."), None, "B"))
        tb.start()
        time.sleep(0.1)
        gate.set()
        ta.join(5); tb.join(5)
    finally:
        D._run_base = old
    assert order == [("start", "A"), ("end", "A"), ("start", "B"), ("end", "B")], order


def test_atomic_write_never_half():
    """一个线程反复原子写（约 100 KB，和皇帝产线的 design.json 差不多大），另一个线程反复读：读到的永远是完整的 json。
    原来的 Path.write_text 在审计的压测里 10 秒 911 次读到空文件。
    读的一方每次读完歇 2 毫秒：一刻不停地读时文件几乎一直开着，写的一方换不上（os.replace 等满约 4.6 秒才放弃）；
    界面最多一秒读一次，2 毫秒已经比实际狠得多。"""
    big = {"devices": [{"device": f"设备{i}", "basis": ["依据" * 20] * 3, "got": {"清水": i}} for i in range(400)]}
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "design.json"
        atomic.write_json(f, big)
        stop = time.time() + 3.0
        stats = {"w": 0, "r": 0, "bad": []}

        def writer():
            k = 0
            while time.time() < stop:
                k += 1
                try:
                    atomic.write_json(f, dict(big, n=k))
                except Exception as e:  # noqa: BLE001
                    stats["bad"].append(f"写失败 {type(e).__name__}: {e}"[:80])
                    return
                stats["w"] += 1

        def reader():
            while time.time() < stop:
                try:
                    d = atomic.read_json(f)  # 和界面一样读（api.bpa_design / project._jload）
                    assert len(d["devices"]) == 400
                    stats["r"] += 1
                except Exception as e:  # noqa: BLE001
                    stats["bad"].append(f"{type(e).__name__}: {e}"[:80])
                time.sleep(0.002)
        th = [threading.Thread(target=writer), threading.Thread(target=reader)]
        [t.start() for t in th]; [t.join() for t in th]
        assert stats["w"] > 20 and stats["r"] > 20, stats
        assert not stats["bad"], f"{len(stats['bad'])} 次出错：{stats['bad'][:3]}"
        assert sorted(p.name for p in Path(td).iterdir()) == ["design.json"], "留下了临时文件"
        assert json.loads(f.read_text(encoding="utf-8"))["n"] == stats["w"]


def test_api_fix_goes_through_job_lock():
    """bpa_fix、bpa_label_gates 走任务：已有任务在跑时直接返回「已有任务在跑」，不写 fixes.json、不算；
    bpa_save_inputs 在蓝图分析任务跑的时候也不存（拼合任务会改写 inputs.json）。"""
    sys.path.insert(0, str(ROOT / "src" / "app"))
    import api as A
    old = P.BPA
    with tempfile.TemporaryDirectory() as td:
        P.BPA = Path(td)
        try:
            (P.BPA / "项目" / "base" / "据点").mkdir(parents=True)
            a = A.Api()
            a._job = A.Job(kind="bpa", state="running")
            r = a.bpa_fix("项目", "据点", "feeds", "精炼炉@1,1", "none")
            assert r == {"ok": False, "error": "已有任务在跑"}, r
            r = a.bpa_label_gates("项目", {"1": {"state": "液", "item": "清水"}})
            assert r == {"ok": False, "error": "已有任务在跑"}, r
            r = a.bpa_save_inputs("项目", "据点", [])
            assert not r["ok"] and "任务在跑" in r["error"], r
            assert not (P.BPA / "项目" / "fixes.json").exists() and not (P.BPA / "项目" / "inputs.json").exists()
            assert a.bpa_fix("项目", "据点", "别的", "k", "none")["ok"] is False  # 参数不对当场报，不开任务
            seen = []                                                             # 修正任务的日志单独一类（反馈包选日志时另算）
            a._start = lambda kind, fn, *args, **kw: seen.append((kind, kw)) or {"ok": True}
            a.bpa_fix("项目", "据点", "feeds", "k", "none")
            assert seen == [("bpa", {"log_kind": A.FIX_LOG_KIND})], seen
        finally:
            P.BPA = old


def test_feedback_logs_keep_the_merge_log():
    """bpa_fix 走任务以后每答一条待确认写一个日志（app_log_bpa-fix_*）。反馈包选日志时修正日志另算：
    先有 1 个据点拼合日志、后面连着 10 个修正日志，拼合日志也要在里面（原来取最近 8 个，全被修正日志占了，2026-11-19）。"""
    import os
    sys.path.insert(0, str(ROOT / "src" / "app"))
    import api as A
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        t0 = time.time() - 1000
        names = ["app_log_读蓝图_1.log", "app_log_bpa_2.log"] + [f"app_log_{A.FIX_LOG_KIND}_{i:02d}.log" for i in range(10)]
        for k, n in enumerate(names):
            (d / n).write_text("x", encoding="utf-8")
            os.utime(d / n, (t0 + k, t0 + k))
        got = [p.name for p in A.feedback_logs(d)]
        assert got[:2] == names[:2] and got[2:] == names[-4:], got   # 其它日志全在（≤ 8 个），修正日志取最近 4 个，按时间排
        for k in range(10):                                           # 其它日志多了照旧只取最近 8 个
            (d / f"app_log_快速_{k:02d}.log").write_text("x", encoding="utf-8")
            os.utime(d / f"app_log_快速_{k:02d}.log", (t0 + 100 + k, t0 + 100 + k))
        got = A.feedback_logs(d)
        assert len(got) == 12 and sum(p.name.startswith(f"app_log_{A.FIX_LOG_KIND}_") for p in got) == 4, [p.name for p in got]


def test_concurrent_run_base_real_project():
    """本机数据：皇帝应龙关（有溢流位，会走 MAIN_BLOCKED 那一遍）两个线程同时算，和单线程结果逐台一样。
    把 _RUN_LOCK 临时换成空锁，验的是「模式改成参数」这一层本身，不靠锁挡。"""
    proj = copy_project("实测-皇帝产线")
    base = "应龙关"
    arch = ROOT / "data" / "blueprints" / json.loads((proj / "project.json").read_text(encoding="utf-8"))["archive"]
    led = project_ledger(proj, base)
    bd = proj / "base" / base

    def key(res):
        return {f"{r['device']}@{tuple(r['c'])}": json.dumps([round(r["rate"], 4), r["state"], r.get("got"), r.get("available"), r.get("limit_by"),
                                                              r.get("if_main_blocked")], sort_keys=True, ensure_ascii=False) for r in res["devices"]}
    ref = key(D.run_base(bd, arch, base, ledger=led))
    old = D._RUN_LOCK
    D._RUN_LOCK = contextlib.nullcontext()
    try:
        for off in (0.0, 0.5):
            out = {}

            def go(n, delay):
                time.sleep(delay)
                out[n] = key(D.run_base(bd, arch, base, ledger=led))
            th = [threading.Thread(target=go, args=("A", 0.0)), threading.Thread(target=go, args=("B", off))]
            [t.start() for t in th]; [t.join() for t in th]
            for n in ("A", "B"):
                bad = [k for k in ref if ref[k] != out[n].get(k)]
                assert not bad, f"错开 {off} 秒，{n} 有 {len(bad)} 台和单线程不一样：{bad[:3]}"
            assert key(json.loads((bd / "design.json").read_text(encoding="utf-8"))) == ref
    finally:
        D._RUN_LOCK = old


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run([test_run_base_switches_are_parameters, test_run_base_is_serialized, test_atomic_write_never_half,
                  test_api_fix_goes_through_job_lock, test_feedback_logs_keep_the_merge_log, test_concurrent_run_base_real_project]))
