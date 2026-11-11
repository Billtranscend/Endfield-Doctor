"""跨地区传输（可选设置，2026-11-11）：登记后终点地区「消耗大于产出」不提醒、库存见底才提醒；库存传输的起点注明。"""
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import blueprint as bp  # noqa: E402
import blueprint_tools as bt  # noqa: E402
import diagnose  # noqa: E402

REGIONS = ["武陵", "四号谷地"]


def _row(name, stock, cur, theo):
    return {"name": name, "fluid": None, "stock": stock, "cur_prod": cur[0], "cur_cons": cur[1],
            "theo_prod": theo[0], "theo_cons": theo[1], "theo_cons_red": theo[1] > theo[0]}


def _plan(transfers):
    plan = copy.deepcopy(bp.load("1.6皇帝产线"))
    plan.playbook["transfers"] = transfers
    plan.playbook["normal"] = [x for x in plan.playbook.get("normal", []) if x["product"] != "致密源石粉末"]
    return plan


def _texts(region, rows, plan):
    return [f.text for f in diagnose.Region(region, rows, plan).diagnose()]


def test_normal_notes_and_drain():
    xin = [{"item": "致密源石粉末", "from": "四号谷地", "to": "武陵", "mode": "超库存"}]
    pb = {"normal": [{"product": "赫铜块", "note": "x"}], "transfers": xin}
    assert set(bp.normal_notes(pb, "武陵")) == {"赫铜块", "致密源石粉末"}
    assert set(bp.normal_notes(pb, "四号谷地")) == {"赫铜块"}
    assert "超库存传输" in bp.normal_notes(pb)["致密源石粉末"]

    full = _row("源矿", 90000, (540, 540), (540, 540))
    full2 = _row("赤铜矿", 90000, (84, 84), (510, 510))  # 只有一个 90000 时会被当成 OCR 多认一位的孤值，上限算错
    draining = _row("致密源石粉末", 30000, (270, 300), (270, 660))  # 2026-11-10 武陵实测的形状，库存放低一点让它算在耗库存
    assert any("致密源石粉末 在耗库存" in t for t in _texts("武陵", [full, full2, draining], _plan([])))
    texts = _texts("武陵", [full, full2, draining], _plan(xin))
    assert not any("致密源石粉末" in t for t in texts), texts
    empty = _row("致密源石粉末", 0, (270, 300), (270, 660))
    texts = _texts("武陵", [full, full2, empty], _plan(xin))
    assert any("靠跨地区传输补" in t and "库存只剩 0" in t for t in texts), texts

    xout = [{"item": "致密源石粉末", "from": "武陵", "to": "四号谷地", "mode": "库存"}]
    texts = _texts("武陵", [full, full2, draining], _plan(xout))
    assert any("库存传输送往四号谷地" in t for t in texts), texts


def test_set_transfers_validation():
    tmp = Path(tempfile.mkdtemp())
    old = bp.BLUEPRINTS
    try:
        shutil.copytree(old / "1.6皇帝产线", tmp / "测试档案")
        bp.BLUEPRINTS = tmp
        out = bt.set_transfers("测试档案", [{"item": "蓝铁块", "from": "四号谷地", "to": "武陵", "mode": "超库存传输", "note": " #4 "}], REGIONS)
        assert out == [{"item": "蓝铁块", "from": "四号谷地", "to": "武陵", "mode": "超库存", "note": "#4"}]
        pb = json.loads((tmp / "测试档案" / "playbook.json").read_text(encoding="utf-8"))
        assert pb["transfers"] == out and pb["targets"]  # 只改 transfers，别的不动
        for bad in [{"item": "不存在的东西", "from": "四号谷地", "to": "武陵"},
                    {"item": "蓝铁块", "from": "武陵", "to": "武陵"},
                    {"item": "蓝铁块", "from": "火星", "to": "武陵"},
                    {"item": "蓝铁块", "from": "四号谷地", "to": "武陵", "mode": "瞬移"}]:
            try:
                bt.set_transfers("测试档案", [bad], REGIONS)
            except ValueError:
                continue
            raise AssertionError(f"应当拒绝：{bad}")
    finally:
        bp.BLUEPRINTS = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_archive_migrated():
    """1.6 档案：错题本 #4 的两条从 normal 挪到了 transfers，按武陵算仍是设计内。"""
    pb = bp.load("1.6皇帝产线").playbook
    assert {x["item"] for x in bp.transfers(pb)} == {"蓝铁块", "致密源石粉末"}
    assert {"蓝铁块", "致密源石粉末"} <= set(bp.normal_notes(pb, "武陵"))


if __name__ == "__main__":
    test_normal_notes_and_drain()
    test_set_transfers_validation()
    test_archive_migrated()
    print("3/3 通过")
