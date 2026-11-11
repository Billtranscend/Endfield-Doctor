"""桌面程序入口：pywebview 窗口（Edge WebView2）+ Api。

开发期：.venv\\Scripts\\python.exe src\\app\\main.py [--debug]
打包后：dist\\终末地基建诊断\\终末地基建诊断.exe（build\\build_exe.py）
发送键鼠需要管理员权限：exe 带 requireAdministrator 清单；开发期用管理员终端启动。
"""

import os
import sys
from pathlib import Path

import webview

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from api import Api  # noqa: E402


def selftest() -> int:
    """--selftest：不开窗口，把后端几个查询的结果写到 debug/selftest.json（打包后验证模块、数据、MaaFw 都能加载）。"""
    import json
    import traceback
    from paths import ROOT

    out = {}
    api = Api()
    for name in ("version", "blueprints", "reports", "game_status"):
        try:
            out[name] = getattr(api, name)()
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}
    # 打包后按需 import 的模块能不能加载（灰度包 2026-11-10：漏打包的模块只有用到那个功能才报错）
    import importlib
    mods = {}
    for m in ("near_patrol", "core_ports", "pool_channels", "gate_limits", "combine", "expert", "diagnose", "patrol", "full_patrol",
              "bpa.design", "bpa.pipenet", "bpa.project", "bpa.importer", "bpa.flow", "bpa.ledger_scan", "bpa.nearmap", "offline_ocr", "strike_scan"):
        try:
            importlib.import_module(m)
            mods[m] = "ok"
        except Exception as e:  # noqa: BLE001
            mods[m] = f"{type(e).__name__}: {e}"
    out["modules"] = mods
    dbg = ROOT / "debug"
    dbg.mkdir(exist_ok=True)
    (dbg / "selftest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    api = Api()
    ui = HERE / "ui" / "index.html"
    window = webview.create_window(
        "终末地基建诊断",
        str(ui),
        js_api=api,
        width=1280,
        height=820,
        min_size=(1024, 680),
        background_color="#0f1113",
    )
    # 不把 window 挂到 Api 上：pywebview 会递归把 Window 的方法也暴露给 JS
    webview.start(debug="--debug" in argv, private_mode=False)
    if "offline_ocr" in sys.modules and sys.modules["offline_ocr"].started():
        # 用过离线 OCR（读蓝图 / 扫底账）：MaaFW 自定义控制器的线程退出时会卡住，窗口关了直接硬退出
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
