"""流体泵有余量时不报「缺设备」（2026-11-12 好几个群友：快速诊断说缺水、缺沉积酸，可应龙关的输入口都是阻塞）。
泵出不去就堵，游戏不把堵着的泵算进理论值：用得少 → 理论值比参考低，并不是泵少了。
只有用量顶到已放置能力（泵全在满负荷）而且能力比参考少两成以上时才提示。"""
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import blueprint as bp  # noqa: E402
import sources  # noqa: E402


def _brief(theo, cur):
    plan = bp.load("1.6皇帝产线")
    rows = copy.deepcopy(plan.reference_brief["武陵"])
    for r in rows:
        if r["name"] == "沉积酸":
            r["theo_prod"], r["cur_prod"], r["cur_cons"] = theo, cur, cur
    return plan, {"武陵": rows}


def _acid(findings):
    return [f for f in findings if f["item"] == "沉积酸"]


def test_blocked_pumps_not_missing():
    plan, brief = _brief(60, 6)    # 群友：理论 300→60，当前只用 6
    assert not _acid(sources.compare(brief, plan))
    plan, brief = _brief(210, 102)  # 群友 R1：理论 210，当前 102
    assert not _acid(sources.compare(brief, plan))


def test_saturated_pumps_still_reported():
    plan, brief = _brief(120, 120)  # 泵全在满负荷、能力只有参考的 40%：真可能少了泵
    f = _acid(sources.compare(brief, plan))
    assert f and f[0]["kind"] == "缺设备", f


if __name__ == "__main__":
    test_blocked_pumps_not_missing()
    test_saturated_pumps_still_reported()
    print("2/2 通过")
