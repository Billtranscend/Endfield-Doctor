"""打包成 Windows 程序（PyInstaller onedir）。

.venv\\Scripts\\python.exe build\\build_exe.py [--dist dist-next] [--keep-ledger]
产物：dist\\EndfieldFactoryDoctor\\EndfieldFactoryDoctor.exe（带管理员清单，启动会弹 UAC）
随包：data/ resource/ .cache/（物品图标缓存）、ui/、MaaFw 的 DLL 和 OCR 模型。debug/ 在运行时写到 _internal/debug。

灰度包（2026-11-10 用户定）：蓝图档案只带通用部分（分区、错题本、参考简报、备路规则），不带 ledger/：
- 底账、配方表、准入口限速、核心取货口是打包人自己基地上读的；底账指向的扫描图在 debug/ 里，不随包。
- 近景结构基线是打包人基地的截图特征，别人基地摆得稍有不同就会报「管子少了」。
测试者导入作者导出的蓝图分析 zip 就有底账、配方表和准入口限速；近景基线第一次巡检时按自己的画面存。
--keep-ledger：原样带上（只给自己用的包）。

瘦身（2026-11-10 用户：灰度包 200 多 MB 太大）：
- 物品图标只带程序会匹配的 177 个（生产链物品、流体、空容器），无损重压（像素逐个相同）：128 MB → 约 11 MB。缩到 128 px 不行：准入口图标分组按 14–21 px 匹配，
  两次缩放后武陵城设计分析回归挂了一项。
- 不带 .cache/akedata（16 MB）：程序运行时只读 data/*.json，这些表只有 tools/build_data.py 生成数据时用。
- 打完删掉 cv2 的视频编解码 DLL（31 MB，程序不读写视频，录演示视频的脚本在源码环境里跑）和 MaaFW 的 GPU 推理后端
  DirectML / dxcompiler / dxil（38 MB；OCR 走 CPU，删掉后同一批截图识别结果逐字相同。webgpu_dawn 是 MaaToolkit 的硬依赖，不能删）。
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 输出目录：默认 dist；旧版 exe 还开着（文件被占用）时用 --dist 另一个目录，例如 --dist dist-next
DIST = ROOT / (sys.argv[sys.argv.index("--dist") + 1] if "--dist" in sys.argv else "dist")
STAGE = ROOT / "build" / "stage"


def hidden_modules() -> list[str]:
    """src、src/tools、src/app 下的所有模块：很多是在函数里按需 import 的（near_patrol、core_ports、pool_channels…），
    一律列成 hidden import，免得漏。测试/探针脚本不带。"""
    out = []
    for d in (ROOT / "src", ROOT / "src" / "tools", ROOT / "src" / "app"):
        for f in sorted(d.glob("*.py")):
            n = f.stem
            if n.startswith(("test_", "probe_", "diag_")) or n in ("devserver", "main"):
                continue
            out.append(n)
    return out


def stage_data(keep_ledger: bool) -> Path:
    """data/ 拷到 build/stage/data；灰度包去掉每套档案的 ledger/（见文件头）。"""
    dst = STAGE / "data"
    if dst.exists():
        shutil.rmtree(dst)
    ignore = None if keep_ledger else (lambda d, names: [n for n in names if n == "ledger" and Path(d).parent.name == "blueprints"])
    shutil.copytree(ROOT / "data", dst, ignore=ignore)
    left = [str(p.relative_to(dst)) for p in dst.glob("blueprints/*/ledger")]
    assert keep_ledger or not left, left
    return dst


def needed_icons() -> set[str]:
    """程序会去匹配的物品图标：生产链（配方表）里的物品、流体、灌装瓶的空容器（panel_recipe.icon_ids 的映射）。
    缓存里其余的（武器箱、角色道具…，487 个里 310 个）运行时用不到。"""
    import json
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]
    import panel_recipe as pr
    rec = json.loads((ROOT / "data" / "recipes.json").read_text(encoding="utf-8"))
    items = {k for r in rec for k in list(r["inputs"]) + list(r["outputs"])}
    items |= {f.stem for f in (ROOT / "resource" / "icons" / "fluid").glob("*.png")} | set(pr.BASE_CONTAINER.values())
    need = set()
    for i in items:
        need.update(pr.icon_ids(i))
        need.add(pr.ITEM_ICONS.get(i, i))
    return need


def stage_cache() -> Path:
    """.cache 只带程序会用到的物品图标，无损重压（像素逐个相同）。"""
    from PIL import Image
    dst = STAGE / ".cache" / "icons"
    if dst.parent.exists():
        shutil.rmtree(dst.parent)
    dst.mkdir(parents=True)
    need = needed_icons()
    for f in sorted((ROOT / ".cache" / "icons").glob("*.png")):
        if f.stem in need:
            Image.open(f).save(dst / f.name, optimize=True)
    return dst.parent


DROP_AFTER = ["cv2/opencv_videoio_ffmpeg*.dll", "maa/bin/DirectML.dll", "maa/bin/dxcompiler.dll", "maa/bin/dxil.dll",
              "PIL/_avif*.pyd"]  # Pillow 的 AVIF 解码（AvifImagePlugin 导不进来时只是标成不支持）


def slim(dist_app: Path) -> int:
    n = 0
    for pat in DROP_AFTER:
        for f in (dist_app / "_internal").glob(pat):
            n += f.stat().st_size
            f.unlink()
    return n


def main() -> int:
    data = stage_data("--keep-ledger" in sys.argv)
    args = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "src" / "app" / "main.py"),
        "--name", "EndfieldFactoryDoctor",
        "--onedir", "--windowed", "--uac-admin", "--noconfirm", "--clean",
        "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "work"), "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT / "src"), "--paths", str(ROOT / "src" / "tools"), "--paths", str(ROOT / "src" / "app"),
        "--add-data", f"{data};data",
        "--add-data", f"{ROOT / 'resource'};resource",
        "--add-data", f"{ROOT / 'src' / 'app' / 'ui'};ui",
        "--collect-all", "maa",
        "--collect-all", "webview",
        "--collect-submodules", "bpa",
    ]
    if (ROOT / ".cache" / "icons").exists():
        args += ["--add-data", f"{stage_cache()};.cache"]
    for h in hidden_modules():
        args += ["--hidden-import", h]
    print(" ".join(args))
    rc = subprocess.call(args, cwd=str(ROOT))
    if rc == 0:
        print(f"瘦身：删掉 {slim(DIST / 'EndfieldFactoryDoctor') / 1e6:.0f} MB（视频编解码 DLL、GPU 推理后端）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
