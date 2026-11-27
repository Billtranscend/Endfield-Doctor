"""游戏 1.7 的数据（2026-11-27，AKEData 1.7.2@12190603-5）：二选一配方、明晦炉阵列的区域转化、平衡炉、改名的设备。

- 气体反应炉「气态衡铜 + 气态衡息壤」每轮只出清型辉铜气×2 或浊型辉铜气×2，各 50%（IndustrialPlanner 2026-11-25 记的游戏实测）。
  data/recipes.json 里 branches 记原样，outputs 写长期平均（各 ×1）：别处算速率、找「谁产浊型辉铜气」都照普通配方走。
- 表里挂在平衡炉名下的 formation_magic_* 是明晦炉阵列每 10 秒在范围内的带子上做的转化，不是平衡炉的配方：单独标出来，
  平衡炉自己只有 3 条。
- 废水处理机 1.7 改名废料处理机：旧档案里还是旧名，两个名字都要认。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import design as D  # noqa: E402
from _testkit import run  # noqa: E402

REC = json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))
TEXT = "气态衡铜×1 + 气态衡息壤×1 → 清型辉铜气×1 + 浊型辉铜气×1（2秒）"


def test_branch_recipe_data():
    r = next(x for x in REC if x["id"] == "gas_reactor_gas_copper_bright_clear_1")
    assert r["seconds"] == 2 and r["machine_name"] == "气体反应炉", r
    assert r["branches"] == [{"prob": 50, "outputs": {"item_gas_copper_bright_clear": 2}},
                             {"prob": 50, "outputs": {"item_gas_copper_bright_turbid": 2}}], r["branches"]
    assert r["outputs"] == {"item_gas_copper_bright_clear": 1, "item_gas_copper_bright_turbid": 1}, r["outputs"]
    assert sum(1 for x in REC if x.get("branches")) == 1  # 这一版全表只有这一条；多出来的要先在游戏里核过语义再用


def test_branch_recipe_rates_and_note():
    assert TEXT in D._machine_recipes("气体反应炉"), D._machine_recipes("气体反应炉")
    assert D.inputs_of(TEXT) == {"气态衡铜": 30.0, "气态衡息壤": 30.0}, D.inputs_of(TEXT)
    assert D.outputs_of(TEXT) == {"清型辉铜气": 30.0, "浊型辉铜气": 30.0}, D.outputs_of(TEXT)  # 每轮 2 秒、平均各 1 份
    note = D.branch_note("气体反应炉", TEXT)
    assert note and "只出其中一种" in note and "清型辉铜气×2（50%）" in note and "浊型辉铜气×2（50%）" in note, note
    other = next(t for t in D._machine_recipes("气体反应炉") if t != TEXT)
    assert D.branch_note("气体反应炉", other) is None and D.branch_note("精炼炉", TEXT) is None


def test_formation_recipes_apart_from_balancer():
    bal = [x for x in REC if x["machine_name"] == "平衡炉"]
    assert len(bal) == 3 and not any(x.get("formation") for x in bal), [x["id"] for x in bal]
    form = [x for x in REC if x.get("formation")]
    assert {x["machine_name"] for x in form} == {"1阶阵列·明", "1阶阵列·晦", "1阶阵列·衡"}, {x["machine_name"] for x in form}
    layers = {}
    for x in form:
        layers.setdefault(x["formation"]["kind"], set()).add(x["formation"]["layer"])
    assert layers == {"magic_ming": {1, 2, 3, 4}, "magic_hui": {1, 2, 3, 4}, "magic_heng": {1, 2}}, layers
    assert all(x["seconds"] == 10 and len(x["inputs"]) == 1 and len(x["outputs"]) == 1 for x in form)


def test_renamed_device_both_names():
    names = set(json.loads((ROOT / "data" / "building_names.json").read_text(encoding="utf-8")))
    ports = json.loads((ROOT / "data" / "building_ports.json").read_text(encoding="utf-8"))
    assert {"废水处理机", "废料处理机", "平衡炉", "明晦炉", "分影明晦炉"} <= names, names & {"废水处理机", "废料处理机"}
    assert ports["废水处理机"] == ports["废料处理机"]
    assert sum(1 for p in ports["平衡炉"]["ports"] if p["role"] == "in" and not p["pipe"]) == 5
    assert sum(1 for p in ports["平衡炉"]["ports"] if p["role"] == "out" and not p["pipe"]) == 5
    assert [p["pipe"] for p in ports["明晦炉"]["ports"]] == [True] and D.FUEL_MIN["明晦炉"] == D.FUEL_MIN["分影明晦炉"] == 6.0


if __name__ == "__main__":
    sys.exit(run([test_branch_recipe_data, test_branch_recipe_rates_and_note, test_formation_recipes_apart_from_balancer,
                  test_renamed_device_both_names]))
