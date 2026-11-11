"""任务计划第 1、2 项：作者确认 → 导出的 zip → 导入成蓝图档案，巡检按作者确认过的设计判断。

只用合成的小 zip，在临时目录里导入，不碰 data/blueprints。
"""
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import blueprint as bp  # noqa: E402
from bpa import importer, project as P  # noqa: E402

DESIGN = {"devices": [
    {"device": "配件机", "x": 1158, "y": 878, "c": [1078, 954], "recipe": "赤铜块×1 → 赤铜零件×1（2秒）", "rate": 0.0,
     "state": "溢流位/平时空闲", "basis": ["卡在：赤铜块（能送到 0/30）"], "need": {"赤铜块": 30.0}, "got": {"赤铜块": 0.0}},
    {"device": "塑形机", "x": 1257, "y": 384, "c": [1249, 100], "recipe": "赤铜块×2 + 惰气×1 → 赤铜耐压罐×1（2秒）", "rate": 0.0,
     "state": "没料", "basis": ["卡在：惰气（能送到 0/30）"], "need": {}, "got": {}},
    {"device": "种植机", "x": 606, "y": 679, "c": [124, 610], "recipe": "砂叶种子×1 → 砂叶×1（2秒）", "rate": 1.0,
     "state": "满速", "basis": [], "need": {}, "got": {}},
    {"device": "封装机", "x": 700, "y": 300, "c": [378, 321], "recipe": "赤铜耐压罐×1 + 息壤×1 → 分离芯×2（2秒）", "rate": 0.5,
     "state": "间歇", "basis": ["卡在：息壤"], "need": {}, "got": {}},
]}


def _zip(path: Path, confirm: dict):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "bpa-1", "name": "测试产线", "groups": {"武陵城": ["1.6武陵城1号"]},
                                                "areas": {"武陵": [{"name": "武陵城", "main": True}]}}, ensure_ascii=False))
        z.writestr("confirm.json", json.dumps(confirm, ensure_ascii=False))
        z.writestr("inputs.json", json.dumps({"武陵城": {"marks": [{"no": 1, "p": [10, 10], "item": "清水", "rate": 60}]}}, ensure_ascii=False))
        z.writestr("武陵城/design.json", json.dumps(DESIGN, ensure_ascii=False))
        z.writestr("武陵城/ledger.json", json.dumps({"area": "武陵城", "devices": [], "scan": {"dir": "debug/x"}}, ensure_ascii=False))
        z.writestr("武陵城/transform.json", "{}")
        z.writestr("武陵城/blueprints/1.6武陵城1号/links.json", json.dumps({"pipes": [], "partner": {}}))
        z.writestr("武陵城/raw/1.6武陵城1号/frames/000.png", b"raw")  # 预览原始帧：导入时不要


def test_confirm_import_roundtrip():
    k = {r["device"]: P.device_key(r) for r in DESIGN["devices"]}
    confirm = {"武陵城": {"devices": {
        k["配件机"]: {"verdict": "ok", "note": "溢流配件机"},
        k["塑形机"]: {"verdict": "wrong", "actual": "平时空闲（设计如此）", "note": "平时不转"},
        k["种植机"]: {"verdict": "ok"},
    }}}  # 封装机没确认
    with tempfile.TemporaryDirectory() as td:
        old = bp.BLUEPRINTS
        bp.BLUEPRINTS = Path(td)
        try:
            zp = Path(td) / "测试产线.bpa.zip"
            _zip(zp, confirm)
            assert importer.is_bpa_zip(zp)
            name = importer.import_zip(zp, log=lambda *_: None)
            b = bp.load(name)
            dd = {d["name"]: d for d in b.playbook["device_designs"]}
            # 作者确认过的溢流配件机、说「实际平时空闲」的塑形机 → 设计内；满速的种植机不进（空闲照报）；没确认的封装机只当参考
            assert set(dd) == {"配件机", "塑形机"}, dd
            assert "作者确认" in dd["配件机"]["note"] and "实际平时空闲" in dd["塑形机"]["note"]
            assert [e["name"] for e in b.playbook["bpa_expect"]] == ["封装机"]
            assert "赤铜零件" in b.overflow_products
            assert b.areas == {"武陵": ["武陵城"]}
            d = bp.BLUEPRINTS / name
            assert (d / "ledger" / "武陵城.json").exists() and (d / "ledger" / "武陵城_recipes.json").exists()
            assert (d / "bpa" / "bp" / "1.6武陵城1号" / "links.json").exists()
            assert not list(d.rglob("000.png"))  # 预览原始帧没解出来
            assert json.loads((d / "bpa" / "inputs.json").read_text(encoding="utf-8"))["武陵城"]["marks"][0]["rate"] == 60
            # 同名再导一次：不覆盖，加日期
            assert importer.import_zip(zp, log=lambda *_: None) != name
        finally:
            bp.BLUEPRINTS = old


def test_merge_into_existing():
    """灰度包（2026-11-10）：包里的档案有错题本、参考简报、备路规则但没底账；作者 zip 合并进去，原有的都保留，底账补上。"""
    with tempfile.TemporaryDirectory() as td:
        old = bp.BLUEPRINTS
        bp.BLUEPRINTS = Path(td)
        try:
            arch = Path(td) / "皇帝产线"
            arch.mkdir()
            areas = {"武陵": [{"name": "武陵城", "main": True, "anchor_label": "协议核心"}]}
            (arch / "areas.json").write_text(json.dumps(areas, ensure_ascii=False), encoding="utf-8")
            (arch / "reference_brief.json").write_text('{"武陵": []}', encoding="utf-8")
            (arch / "tier_rules.json").write_text(json.dumps({"rules": [{"product": "低容武陵电池", "requires_full": ["息壤"], "note": "备路"}],
                                                              "overflow_products": []}, ensure_ascii=False), encoding="utf-8")
            (arch / "playbook.json").write_text(json.dumps({"targets": [{"product": "息壤", "per_min": 360}],
                                                            "device_designs": [{"area": "景玉谷", "name": "封装机", "x": 1, "y": 2, "kinds": ["idle"], "note": "手填"}]},
                                                           ensure_ascii=False), encoding="utf-8")
            zp = Path(td) / "测试产线.bpa.zip"
            k = {r["device"]: P.device_key(r) for r in DESIGN["devices"]}
            _zip(zp, {"武陵城": {"devices": {k["配件机"]: {"verdict": "ok"}}}})
            assert importer.merge_candidates(importer.peek(zp)["groups"]) == ["皇帝产线"]
            for _ in range(2):  # 合并两次：作者那几台不重复
                assert importer.import_zip(zp, into="皇帝产线", log=lambda *_: None) == "皇帝产线"
            b = bp.load("皇帝产线")
            assert json.loads((arch / "areas.json").read_text(encoding="utf-8")) == areas  # 分区登记（主分区、锚点）没被 zip 的覆盖
            assert [r["product"] for r in b.tier_rules] == ["低容武陵电池"] and "赤铜零件" in b.overflow_products
            assert b.playbook["targets"] == [{"product": "息壤", "per_min": 360}]
            assert sorted(d["name"] for d in b.playbook["device_designs"]) == ["封装机", "配件机"], b.playbook["device_designs"]
            assert (arch / "ledger" / "武陵城.json").exists() and (arch / "bpa" / "inputs.json").exists()
            assert [x.name for x in Path(td).iterdir() if x.name.startswith("_merge")] == []  # 备份清掉了
            # 合并失败：整套档案恢复原样
            bad = Path(td) / "坏.bpa.zip"
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr("manifest.json", json.dumps({"format": "bpa-1", "name": "坏", "groups": {"武陵城": ["x"]}}))
                z.writestr("武陵城/design.json", "{不是 json")
            before = sorted(str(x.relative_to(arch)) for x in arch.rglob("*"))
            try:
                importer.import_zip(bad, into="皇帝产线", log=lambda *_: None)
                raise AssertionError("坏 zip 没报错")
            except ValueError:
                pass
            assert sorted(str(x.relative_to(arch)) for x in arch.rglob("*")) == before
        finally:
            bp.BLUEPRINTS = old


if __name__ == "__main__":
    test_confirm_import_roundtrip()
    test_merge_into_existing()
    print("2/2 通过")
