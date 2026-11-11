"""设计限速产物的设备缺料空着不报「送不到」（2026-11-10 灰度包实测，景玉谷封装机 (752,732)）。

中容武陵电池是设计限速产物（致密源石粉末准入口限 6，分到每台封装机约 1.2/分）：中容产量达到目标 14.7 时，
做它的封装机缺致密源石粉末空着是设计如此；原来被报成问题「致密源石粉末 送不到 1 台设备」。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import combine as C  # noqa: E402

BRIEF = ROOT / "debug" / "gray_景玉谷_brief_20261110-192713.json"
PATROL = ROOT / "debug" / "gray_景玉谷_patrol_20261110-192542.json"


def test_design_limited_not_starved():
    if not BRIEF.exists():
        return
    _, d = C.run(BRIEF, PATROL, "1.6皇帝产线", merge=False)
    assert not d["verdicts"].get("问题"), [v.get("title") for v in d["verdicts"]["问题"]]
    ok = [v for v in d["verdicts"].get("正常", []) if v["device"]["name"] == "封装机" and v["device"]["x"] == 752]
    assert ok, d["verdicts"]


if __name__ == "__main__":
    test_design_limited_not_starved()
    print("1/1 通过")
