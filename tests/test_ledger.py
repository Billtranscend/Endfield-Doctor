"""底账（src/ledger.py）离线用例：并入、容差匹配、来源升级、不一致标注、读写。

用法：.venv\\Scripts\\python.exe tests\\test_ledger.py
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import ledger  # noqa: E402


def test_add_and_near():
    led = ledger.Ledger("测试区")
    a = led.add("天有洪炉", 882, 384, "history", "t1")
    b = led.add("天有洪炉", 884, 386, "history", "t2")  # 复现误差 2 px → 同一台
    assert a is b and a.seen == 2 and a.last == "t2", a
    c = led.add("天有洪炉", 882, 541, "history")  # 157 px 外 → 另一台
    assert c is not a and len(led.devices) == 2
    assert led.near(900, 400) is a and led.near(882, 460) is None
    assert [d.name for d in led.by_type(["天有洪炉"])] == ["天有洪炉", "天有洪炉"]
    assert led.by_type(["封装机"]) == []


def test_source_rank():
    led = ledger.Ledger("测试区")
    led.add("粉碎机", 496, 771, "history")
    e = led.add("研磨机", 497, 772, "history")  # 同等来源、名字不一致 → 标注，名字不改
    assert e.name == "粉碎机" and "两次不一致" in e.note, e
    e = led.add("研磨机", 496, 771, "panel")  # 面板确认 → 以面板为准，记下原名
    assert e.name == "研磨机" and e.source == "panel" and "原 粉碎机" in e.note, e
    e = led.add("粉碎机", 496, 771, "ocr")  # 更低来源不能覆盖面板
    assert e.name == "研磨机" and e.source == "panel", e


def test_save_load(tmp: Path):
    ledger.bp.BLUEPRINTS = tmp
    led = ledger.Ledger("测试区")
    led.add("封装机", 782, 523, "ocr", "t")
    led.add("天有洪炉", 882, 384, "panel", "t")
    p = ledger.save("蓝图X", led)
    assert p.exists() and p.name == "测试区.json"
    back = ledger.load("蓝图X", "测试区")
    assert back.updated and back.types() == {"封装机": 1, "天有洪炉": 1}, back.types()
    assert back.near(883, 385).source == "panel"
    assert ledger.load("蓝图X", "没有的区").devices == []
    assert "2 台" in ledger.text(back)


if __name__ == "__main__":
    test_add_and_near()
    test_source_rank()
    with tempfile.TemporaryDirectory() as t:
        test_save_load(Path(t))
    print("3/3 通过")
