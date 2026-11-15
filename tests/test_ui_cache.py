"""升级后窗口里还是旧界面（2026-11-14）：静态文件响应要带「不要缓存」，启动时清掉 WebView2 的网页缓存、不动 Local Storage。
没装 pywebview 的环境跳过。"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src" / "app"), str(ROOT / "src"), str(ROOT / "src" / "tools")]


def _main():
    try:
        import webview  # noqa: F401
        import main as M
    except Exception as e:  # noqa: BLE001
        print("跳过（这个环境加载不了程序入口）：", type(e).__name__, str(e)[:80])
        return None
    return M


def test_static_files_are_not_cacheable_and_old_cache_is_cleared():
    M = _main()
    if M is None:
        return
    from webview import http
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = td
        try:
            d = Path(td) / "pywebview" / "EBWebView" / "Default"
            for sub in ("Cache/Cache_Data", "Code Cache/js", "Local Storage/leveldb"):
                (d / sub).mkdir(parents=True)
                (d / sub / "x").write_text("1")
            done = M.fresh_ui()
            assert "no-cache" in done and "cleared:Cache" in done and "cleared:Code Cache" in done, done
            assert not (d / "Cache").exists() and not (d / "Code Cache").exists()
            assert (d / "Local Storage" / "leveldb" / "x").exists()      # 记着选的项目：不能删
            r = http.bottle.static_file("index.html", root=str(ROOT / "src" / "app" / "ui"))
            assert "no-store" in (r.get_header("Cache-Control") or ""), dict(r.headers)
            assert M.fresh_ui() == ["no-cache"]                           # 再调一次：不重复包、没东西可清
            r = http.bottle.static_file("没有这个文件.html", root=str(ROOT / "src" / "app" / "ui"))
            assert r.status_code == 404                                   # 出错的响应照样给回去
        finally:
            if old is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old


def test_each_version_opens_a_page_address_never_seen_before():
    """清缓存不一定清得干净，所以每一版换一个页面地址：界面文件一变，入口文件名和脚本的 ?v= 都跟着变。"""
    M = _main()
    if M is None:
        return
    import shutil
    with tempfile.TemporaryDirectory() as td:
        old = M.HERE
        (Path(td) / "ui").mkdir()
        for f in ("index.html", "app.js", "app.css", "help.js"):
            shutil.copy2(ROOT / "src" / "app" / "ui" / f, Path(td) / "ui" / f)
        M.HERE = Path(td)
        try:
            e1 = M.ui_entry()
            html = e1.read_text(encoding="utf-8")
            stamp = e1.stem.split("_")[-1]
            assert e1.name.startswith("_index_") and all(f"{f}?v={stamp}" in html for f in ("app.js", "app.css", "help.js")), e1.name
            assert M.ui_entry() == e1                                            # 文件没变：同一个地址，不重写
            js = Path(td) / "ui" / "app.js"
            js.write_text(js.read_text(encoding="utf-8") + "  // 新版", encoding="utf-8")
            e2 = M.ui_entry()
            assert e2 != e1 and not e1.exists() and e2.exists()                  # 界面文件变了：换地址，旧入口删掉
            idx = Path(td) / "ui" / "index.html"
            idx.write_text(idx.read_text(encoding="utf-8").replace('src="app.js"', "src='app.js'"), encoding="utf-8")
            assert M.ui_entry() == idx                                           # 认不出脚本引用的写法：退回原来的 index.html
        finally:
            M.HERE = old


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = 0
    for f in tests:
        try:
            f(); ok += 1
        except AssertionError as e:
            print("FAIL", f.__name__, str(e)[:300])
    print(f"{ok}/{len(tests)} 通过")
    sys.exit(0 if ok == len(tests) else 1)
