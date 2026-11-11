"""报告文件名带类型（2026-11-11 用户定）：app_report_快速_<时间> / app_report_深度_<时间>；老文件名照样列出、按时间排。"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app")]

import api  # noqa: E402


def test_report_names():
    tmp = Path(tempfile.mkdtemp())
    old = api.DEBUG
    try:
        api.DEBUG = tmp
        a = api.Api()
        # 0.10.1 及以前的老文件名（没有类型）
        (tmp / "app_report_20261110-214438.json").write_text(json.dumps({"kind": "quick", "stamp": "20261110-214438"}), encoding="utf-8")
        q = a._save_report({"kind": "quick", "stamp": "20261111-090000"})
        p = a._save_report({"kind": "patrol", "stamp": "20261111-100000", "areas": []})
        assert q == "app_report_快速_20261111-090000" and p == "app_report_深度_20261111-100000"
        assert (tmp / f"{q}.json").exists() and (tmp / f"{p}.json").exists()
        ids = [r["id"] for r in a.reports()]
        assert ids == [p, q, "app_report_20261110-214438"], ids  # 按时间新到旧，不按类型分组
        assert a.report(p)["kind"] == "patrol" and a.report("app_report_20261110-214438")["kind"] == "quick"
        assert a.report("../secret") is None
    finally:
        api.DEBUG = old
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_report_names()
    print("1/1 通过")
