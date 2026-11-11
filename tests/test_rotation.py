"""任务计划第 13 项（巡检侧）：两种植物轮换。

- 登记了「锦草 / 芽针」轮换：采种机在采芽针、旁边的种植机在种锦草却空闲 → 设计内；搭档也在种锦草 → 不改判。
- 没登记：不改判。
- 档案登记：植物名要认识、一组两种不同的；导入蓝图分析时作者确认过的轮换设备 → 登记。
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import combine as C  # noqa: E402
from diagnose import ID_NAME  # noqa: E402

NAME_ID = {v: k for k, v in ID_NAME.items()}


def _dev(name, out, x, y, status):
    return C.Device(name=name, status=status, badge=status, x=x, y=y, recipe="", output=NAME_ID[out], recipe_id=None)


def _reg(pairs):
    return SimpleNamespace(blueprint=SimpleNamespace(rotating_plants=pairs), items={}, area="武陵城")


def test_rotation_rule():
    pairs = [{"plants": ["锦草", "芽针"]}]
    planter = _dev("种植机", "锦草", 1000, 1000, "空闲中")
    picker = _dev("采种机", "芽针种子", 1100, 1000, "生产中")
    note = C.plant_rotation(_reg(pairs), planter, [planter, picker])
    assert note and "轮换" in note and "芽针" in note, note
    # 搭档也在种锦草（没错开）：不是轮换间隙，不改判
    picker2 = _dev("采种机", "锦草种子", 1100, 1000, "生产中")
    assert C.plant_rotation(_reg(pairs), planter, [planter, picker2]) is None
    # 没登记 / 搭档太远：不改判
    assert C.plant_rotation(_reg([]), planter, [planter, picker]) is None
    far = _dev("采种机", "芽针种子", 1400, 1000, "生产中")
    assert C.plant_rotation(_reg(pairs), planter, [planter, far]) is None
    # 登记在别的分区：不改判
    assert C.plant_rotation(_reg([{"plants": ["锦草", "芽针"], "area": "景玉谷"}]), planter, [planter, picker]) is None


def test_register_and_import_rotation():
    import blueprint as bp
    import blueprint_tools as bt
    from bpa import importer
    with tempfile.TemporaryDirectory() as td:
        old = bp.BLUEPRINTS
        bp.BLUEPRINTS = Path(td)
        try:
            d = Path(td) / "测试"
            d.mkdir()
            (d / "tier_rules.json").write_text(json.dumps({"rules": []}), encoding="utf-8")
            assert bt.set_rotating_plants("测试", [{"plants": ["锦草种子", "芽针"], "area": "武陵城"}]) == [{"plants": ["锦草", "芽针"], "area": "武陵城"}]
            assert bp.load("测试").rotating_plants == [{"plants": ["锦草", "芽针"], "area": "武陵城"}]
            for bad in ([{"plants": ["锦草", "锦草"]}], [{"plants": ["锦草", "不存在的草"]}], [{"plants": ["锦草"]}]):
                try:
                    bt.set_rotating_plants("测试", bad)
                    raise AssertionError(f"应该拒绝：{bad}")
                except ValueError:
                    pass
        finally:
            bp.BLUEPRINTS = old
    row = {"device": "种植机", "x": 1, "y": 2, "rate": 1.0, "state": "满速", "recipe": "清水×1 + 锦草种子×1 → 锦草×2（2秒）",
           "basis": ["交替配方：清水×1 + 锦草种子×1 → 锦草×2（2秒） / 清水×1 + 芽针种子×1 → 芽针×2（2秒）；各自的原料够跑 50% + 50%"
                     " → 约 100% 时间在转（种植机种锦草，相连的采种机却是芽针：按两种植物轮换算（推断））"]}
    from bpa import project as P
    ex = importer.expectations({"devices": [row]}, {"devices": {P.device_key(row): {"verdict": "ok"}}}, "武陵城")
    assert ex.get("rotations") == [{"plants": ["锦草", "芽针"], "area": "武陵城", "note": "种植机 (1,2) 蓝图分析推断、作者确认"}], ex
    ex = importer.expectations({"devices": [row]}, {}, "武陵城")  # 没确认：不登记
    assert not ex.get("rotations")


if __name__ == "__main__":
    test_rotation_rule()
    test_register_and_import_rotation()
    print("2/2 通过")
