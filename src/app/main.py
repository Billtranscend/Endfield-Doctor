"""桌面程序入口：pywebview 窗口（Edge WebView2）+ Api。

开发期：.venv\\Scripts\\python.exe src\\app\\main.py [--debug]
打包后：dist\\终末地基建诊断\\终末地基建诊断.exe（build\\build_exe.py）
发送键鼠需要管理员权限：exe 带 requireAdministrator 清单；开发期用管理员终端启动。
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from paths import unblock_downloaded  # noqa: E402

if getattr(sys, "frozen", False):
    # 浏览器下载的 zip 解压出来的文件带「来自网络」标记，.NET 不加载这样的 DLL，窗口起不来：先解除（要在 pythonnet 加载之前）
    unblock_downloaded()

import webview  # noqa: E402

from api import Api  # noqa: E402


def fresh_ui() -> list[str]:
    """升级后窗口里显示的还是旧界面（2026-11-14 用户：新包「设置」页没有「导出反馈包」，版本号却已经是 0.10.10——
    版本号是后端给的，页面是缓存里的旧 index.html）。原因有三条凑在一起：
      1. pywebview 非隐私模式下内置的静态文件服务固定用 127.0.0.1:42001，换版本、换目录地址都一样；
      2. 它想加的「不要缓存」头没加上（6.2.1：头设在 bottle.response 上，static_file 返回的是另一个响应对象）；
      3. 响应里只有 Last-Modified，WebView2 就按「文件有多旧」估一个保质期（旧一天 ≈ 两个多小时），期内不问服务器直接用缓存。
    所以刚用过旧版、马上换新版的人看到的是旧界面配新后端。两处一起改：
      - 给静态文件响应真的带上不缓存的头（以后的版本不会再被缓存）；
      - 启动时清掉 WebView2 的网页缓存（已经缓存下来的旧页面）。只删 Cache / Code Cache，不动 Local Storage（记着选的项目）。
    → 做了哪几件（给测试看）。"""
    done = []
    try:
        from webview import http as _http
        orig = _http.bottle.static_file
        if not getattr(orig, "_no_cache", False):
            def static_file(*a, **k):
                r = orig(*a, **k)
                try:
                    r.set_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    r.set_header("Pragma", "no-cache")
                    r.set_header("Expires", "0")
                except Exception:  # 返回的不是响应对象（出错时）：原样给回去
                    pass
                return r
            static_file._no_cache = True
            _http.bottle.static_file = static_file
        done.append("no-cache")
    except Exception:
        pass
    import shutil
    root = Path(os.environ.get("APPDATA") or "") / "pywebview" / "EBWebView" / "Default"
    for sub in ("Cache", "Code Cache"):
        d = root / sub
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)  # 另一份程序开着时删不掉：不管，它那份界面已经加载完了
            if not d.exists():
                done.append(f"cleared:{sub}")
    return done


def ui_entry() -> Path:
    """窗口要打开的页面。清缓存不一定清得干净（另一份程序开着、上一份的浏览器进程还没退完时删不掉，2026-11-14 自检时就碰上了），
    所以不指望它：每一版用一个没出现过的页面地址——把 index.html 抄成 _index_<戳>.html，里面的 app.js / app.css / help.js 也带上 ?v=<戳>。
    戳 = 这几个文件的大小和修改时间，文件一变地址就变，缓存里的旧页面对不上号。地址里的主机和端口没变，Local Storage 还在。
    写不了（目录只读）就退回 index.html。"""
    import hashlib
    ui = HERE / "ui"
    src = ui / "index.html"
    try:
        h = hashlib.sha1()
        for f in ("index.html", "app.js", "app.css", "help.js"):
            st = (ui / f).stat()
            h.update(f"{f}:{st.st_size}:{int(st.st_mtime)};".encode())
        stamp = h.hexdigest()[:10]
        dst = ui / f"_index_{stamp}.html"
        if not dst.exists():
            html = src.read_text(encoding="utf-8")
            for a, b in (('href="app.css"', f'href="app.css?v={stamp}"'), ('src="help.js"', f'src="help.js?v={stamp}"'),
                         ('src="app.js"', f'src="app.js?v={stamp}"')):
                if html.count(a) != 1:
                    return src
                html = html.replace(a, b)
            for old in ui.glob("_index_*.html"):
                old.unlink(missing_ok=True)
            dst.write_text(html, encoding="utf-8")
        return dst
    except OSError:
        return src


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
              "bpa.design", "bpa.pipenet", "bpa.project", "bpa.importer", "bpa.flow", "bpa.ledger_scan", "bpa.nearmap", "bpa.recipe_icons", "offline_ocr", "strike_scan",
              # 0.10.12 补全：原来靠 design / flow 的导入间接带进来，有的是函数里才导入（flow 的 ports），漏打包时自检看不出（0.10.11 打包记录）
              "bpa.pool_model", "bpa.atomic", "bpa.ports", "bpa.centerline", "bpa.inputs", "bpa.placement", "bpa.logistics", "bpa.vision", "bpa.game_ops"):
        try:
            importlib.import_module(m)
            mods[m] = "ok"
        except Exception as e:  # noqa: BLE001
            mods[m] = f"{type(e).__name__}: {e}"
    out["modules"] = mods
    # 界面文件是不是这一版的、静态文件响应带不带「不要缓存」（2026-11-14：升级后窗口里还是旧界面）
    ui = {"fresh_ui": fresh_ui()}
    try:
        from webview import http as _http
        r = _http.bottle.static_file("index.html", root=str(HERE / "ui"))
        ui["cache_control"] = r.get_header("Cache-Control")
        html = (HERE / "ui" / "index.html").read_text(encoding="utf-8")
        js = (HERE / "ui" / "app.js").read_text(encoding="utf-8")
        ui["markers"] = {k: (k in html or k in js) for k in ("bpaDsPending", "fbProj", "feedbackExport", "bpaPending",
                                                             "bpaPoolCard", "bpaPoolFix", "bpaPendPoolCard", "bpaWriteState",  # 这四个是 0.10.11 的池子配方卡
                                                             # 0.10.13 设计分析页的准入口限速表和待确认里的准入口汇总卡
                                                             # （载入表的函数叫 bpaGateLoad：bpaGateTable 是图标分组表原有的元素 id，标记查它永远命中）
                                                             "bpaDsGates", "bpaFixJob", "bpaGateLoad", "bpaGateRender", "bpaGateEdit", "bpaGateSave",
                                                             "bpaGateAllOff", "bpaGateUndo", "bpaGateBig", "bpaGateStaleDel", "bpaGateCard",
                                                             # 0.10.13 待确认里的暗管来料卡；跑完重新载入「输入端」页
                                                             "bpaTunCard", "bpaTunFix", "bpaInReload",
                                                             # 0.10.13 池子卡的「出口送什么」、设计分析表池子行的「出口送原料」
                                                             "bpaPoolOutRender", "bpaPoolOutRead", "bpaPoolOutPick", "bpaPoolOutWords",
                                                             "bpaPoolOutNorm", "bpaPoolList", "bpaPoolPassWords", "bpaPoolNoteText")}
        e = ui_entry()
        ui["entry"] = e.name
        ui["entry_versioned"] = e.name != "index.html" and "app.js?v=" in e.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        ui["error"] = f"{type(e).__name__}: {e}"
    out["ui"] = ui
    dbg = ROOT / "debug"
    dbg.mkdir(exist_ok=True)
    (dbg / "selftest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    api = Api()
    fresh_ui()
    ui = ui_entry()
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
