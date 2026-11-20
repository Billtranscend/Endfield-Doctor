"""设计分析整份快照（审计改进第 5 项）：样本、图标比对缓存、离线运行和比对工具。库 + 命令行。
文件名以下划线开头，tests/run_all.py 不会把它当测试跑；快照测试 tests/test_design_snapshot.py 调这里的函数。

  python tests/_designsnap.py build-fixture 皇帝产线 --reason "…"    从 debug/bpa/实测-皇帝产线 和 data/blueprints/1.6皇帝产线 重做样本
  python tests/_designsnap.py record-memo 皇帝产线 --reason "…"      在 .cache/icons 上真算三据点，重录图标比对缓存 ake_memo.json.gz
  python tests/_designsnap.py verify 皇帝产线 --ref <item3f.json>    样本离线跑 stored / reflow，和 item3f 逐台比
  python tests/_designsnap.py verify 皇帝产线 --exact-vs-project     同时在 debug/ 下整份项目的副本上跑，design.json / flow.json / 连线逐字段相等
  python tests/_designsnap.py verify 皇帝产线 --placeholder-check    蓝图 mosaic.png 换回真图再跑一遍，和占位图逐字段相等
  python tests/_designsnap.py verify 皇帝产线 --dump <文件>          打包结果写到文件（两个进程各跑一遍、逐字节比，查确定性）
  python tests/_designsnap.py verify 皇帝产线 --expect-vs <item3f>   不重算：期望快照 expect/<据点>.json 和 item3f 逐台比（私有的加 --ref-prefix 别人）
  python tests/_designsnap.py inspect 皇帝产线                       文件数、体积、manifest、memo 条数、隐私 grep、拼合图逐像素核对
快照测试和 --accept / --init / --sync-truth / --show / --no-memo / --rerecord-ake / --audit-truth 在 tests/test_design_snapshot.py。

第 2 步（期望快照、比较器、实测判词）也在这里：
  pack_section   design.json（+ flow.json）→ 快照段：设备行键 =「设备名@底账x,y」；准入口、待确认、物流 issues、连线、structures 的键
                 一律是取整的画布坐标 + 类型 / 物品（物流节点编号只作行内字段，flow 重新编号不报变化）；连线按物料流向写成
                 起点 → 终点（从哪头追到的不算变化），每头带接口边、认法、类型。
  compare        结论变了（硬字段；state 只比「（」之前的类别）/ 措辞（依据全文 + state 括号里的说明，按改法归并）/ 新增·消失 /
                 行上新键（ROW_RULES 规定不该有的单列）/ 其它段逐条（多了 / 少了 / 哪个字段变了）/ 连线（人话，接口边变了单列）。
                 画布坐标、迭代轮数、design.json 原文 sha1 只提示。
  verdict / judge_change   实测判定（口径照 snapplan/truth/annotate.py）和变化判词，修订 1.6–1.8。
  diff_fixture   最先打印（都以「差异 」开头）：各段计数、状态迁移直方图（防退化的闸）、✗ 清单、--accept 要什么；然后逐台明细。
  期望文件 expect/<据点>.json：format 1；键排序、不转义中文，缩进到 devices / links 这一层，设备行、连线、条目各占一行；
                 reflow 段里和 stored 段完全相同的设备行 / 段写成「同 stored」（load_expect 展开）。

样本（tests/data/design_snap/<名字>/）
  proj/           和真实项目同构：inputs.json、base/<据点>/{composite.png, flow.json, logistics.json, transform.json[, inputs_auto.json]}、
                  bp/<蓝图>/anchors.json[, links.json, mosaic.png]。设计分析和物流连接只认 bd = proj/base/<据点> 和 bd.parent.parent
  archive/ledger/ <据点>.json（底账）、<据点>_recipes.json、gate_limits/<据点>.json。run_base 的 archive 参数指到 archive/，
                  中间这层 ledger/ 少了不报错，会悄悄改走机身图标推配方（design.py:2683-2691）
  manifest.json   只覆盖 proj/、archive/：每个文件的 sha256、原图的 sha256 和 shape、底账来源、仓库数据哈希、环境、生成原因
  ake_memo.json.gz 图标比对缓存（logistics._AKE_CACHE 落盘）+ 指纹。指纹不对的缓存一律不用
  truth.json、expect/、changelog.jsonl：第 2 步的实测标注和期望快照，build-fixture 绝不删除、不改写

冻结了什么：11-13 存的 flow.json、当时界面选中的底账、档案配方表、准入口限速。以后档案更新不会带动快照，这是有意的。
拼合图按 PNG 压缩级 9 无损重存（生成时逐像素核对）；有 links.json 的蓝图 mosaic.png 换成同尺寸纯白占位图
（代码只读 .shape，vision.py:581-584 的 preview_to_canvas 不用 mos_shape；不能删，删了等于没有暗管配对）。

离线运行 offline()：PR.ICON_CACHE 指到空临时目录、禁止联网（计数）、包住 LG._ake_candidates 数缓存未命中、
FL.image 换成空操作（flow.py 出图写死了系统字体路径，出图对结果没有影响），退出时全部还原。
"""
from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import dataclasses
import gzip
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
for _p in (str(TESTS), str(ROOT / "src" / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SNAP = TESTS / "data" / "design_snap"
PRIVATE = Path(os.environ.get("BPA_PRIVATE_FIXTURES") or (TESTS / "data" / "private"))
MEMO_NAME = "ake_memo.json.gz"
MANIFEST = "manifest.json"
FORMAT = 1
# 已知样本的来源（只读）。build-fixture 只在有 debug/ 的机器上用
SOURCES = {"皇帝产线": {"src": "debug/bpa/实测-皇帝产线", "archive": "data/blueprints/1.6皇帝产线", "item3f": "皇帝"}}
BASE_FILES = ("logistics.json", "transform.json")
# 仓库里会影响结果、但不在 src/ 的数据（变了要在 diff 开头提示）
REPO_DATA_FILES = ("data/recipes.json", "data/item_icons.json", "data/item_id_names.json", "data/item_names_cn.json",
                   "data/building_ports.json", "resource/bpa/storage_line_base.png")
REPO_DATA_DIRS = ("resource/bpa/lg", "resource/bpa/items", "resource/icons/fluid")
_PRIVACY_FIXED = (r"AppData", r"scratchpad", r"Users[\\/]", r"[A-Za-z]:[\\/]")


def _privacy_re() -> re.Pattern:
    """公开样本里不该出现的本机痕迹。本机用户名运行时取（家目录名、USERNAME / USER），源码里不写字面量；
    不到 3 个字符的名字太容易误中，不拿来搜。"""
    names = {n for n in (Path.home().name, os.environ.get("USERNAME", ""), os.environ.get("USER", ""))
             if len(n) >= 3}
    alts = [f"(?i:{re.escape(n)})" for n in sorted(names)] + list(_PRIVACY_FIXED)
    return re.compile("|".join(alts))


PRIVACY = _privacy_re()
# 打印用：不把用户名回显到输出里
PRIVACY_SHOWN = "|".join(("<本机用户名>",) + _PRIVACY_FIXED)


class MemoStale(Exception):
    """图标比对缓存的指纹和现在的代码 / 数据对不上，而本机又没有完整的 .cache/icons，没法现算。"""


class IconsIncomplete(Exception):
    """要现算（录缓存、--no-memo、抽查）时本机图标不齐：模板数不等于模板 id 数，或 manifest 列的图标文件缺。"""


# ------------------------------------------------------------------ 小工具
def jload(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def jdump(obj, indent=1) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=True)


def sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def rel(p: Path) -> str:
    """仓库内的路径写成相对路径；仓库外的只写「(仓库外)/文件名」，manifest 里不出现本机绝对路径。"""
    p = Path(p).resolve()
    try:
        return p.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return f"(仓库外)/{p.name}"


def dir_sha1(d: Path) -> str:
    h = hashlib.sha1()
    for f in sorted((x for x in d.rglob("*") if x.is_file()), key=lambda x: x.relative_to(d).as_posix()):
        h.update(f.relative_to(d).as_posix().encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def git_code() -> str:
    """git 短 sha，工作区有改动（不算未跟踪文件）加 +dirty；不在仓库 / 没有 git → 说明文字。"""
    try:
        kw = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], timeout=30, **kw)
        if r.returncode:
            return "不在 git 仓库里"
        d = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"], timeout=60, **kw)
        return r.stdout.strip() + ("+dirty" if d.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "没有 git"


def env_versions() -> dict:
    import cv2
    import numpy as np
    import PIL
    return {"python": sys.version.split()[0], "cv2": cv2.__version__, "numpy": np.__version__, "PIL": PIL.__version__}


def fixture_dir(name: str) -> Path:
    """样本目录：公开的在 tests/data/design_snap/<名字>，私有的在 tests/data/private/<名字>（BPA_PRIVATE_FIXTURES 可改到仓库外）。"""
    for d in (SNAP / name, PRIVATE / name):
        if (d / MANIFEST).exists():
            return d
    return SNAP / name


def load_manifest(fx: Path) -> dict:
    return jload(Path(fx) / MANIFEST)


def covered_dirs(man: dict) -> list[str]:
    return list(man.get("covers") or ["proj", "archive"])


# ------------------------------------------------------------------ 做样本
def _imencode_png9(im) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".png", im, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    assert ok, "PNG 编码失败"
    return buf.tobytes()


def build_fixture(name: str, src_proj: Path, archive: Path | None, out: Path, reason: str, *, ledgers: dict | None = None,
                  bases: list[str] | None = None, log=print) -> dict:
    """从真实项目 src_proj（只读）和蓝图档案 archive（只读，可以没有）做样本，写到 out/。
    只重写 out/proj、out/archive（archive=None 时是 out/ledger）和 out/manifest.json；
    truth.json、expect/、changelog.jsonl、ake_memo.json.gz 一概不动（memo 由 record_memo 重录）。
    底账：项目自己扫的 <项目>/ledger/<据点>.json 优先，没有再用档案的（同 _testkit.project_ledger / project.ledger_path）；
    archive=None 时必须用 ledgers={据点: 底账路径} 给。返回 manifest。"""
    import cv2  # noqa: F401
    import numpy as np
    from bpa import vision as V
    src_proj, out = Path(src_proj), Path(out)
    archive = Path(archive) if archive else None
    assert src_proj.is_dir(), f"没有源项目 {rel(src_proj)}"
    pj = jload(src_proj / "project.json") if (src_proj / "project.json").exists() else {}
    bases = list(bases or (pj.get("groups") or {}) or sorted(p.name for p in (src_proj / "base").iterdir() if p.is_dir()))
    led_dir = "archive/ledger" if archive else "ledger"
    covers = ["proj", "archive"] if archive else ["proj", "ledger"]
    had_expect = (out / "expect").exists()
    for sub in covers:  # 只删样本输入这几层，不整目录 rmtree
        if (out / sub).exists():
            shutil.rmtree(out / sub)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}

    def put_copy(src: Path, dst_rel: str, how: str = "原样复制"):
        dst = out / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        files[dst_rel] = {"src": rel(src), "how": how}

    inp = {}
    if (src_proj / "inputs.json").exists():
        put_copy(src_proj / "inputs.json", "proj/inputs.json", "原样复制（作者在输入端页填的；没填的据点用 inputs_auto.json）")
        inp = jload(src_proj / "inputs.json") or {}
    man_bases = {}
    for base in bases:
        bs, bdr = src_proj / "base" / base, f"proj/base/{base}"
        (out / bdr).mkdir(parents=True, exist_ok=True)
        # 拼合图：PNG 压缩级 9 无损重存，存完立刻解码、和原图逐像素核对
        im = V.rd(bs / "composite.png")
        assert im is not None, f"读不到 {rel(bs / 'composite.png')}"
        (out / bdr / "composite.png").write_bytes(_imencode_png9(im))
        back = V.rd(out / bdr / "composite.png")
        assert back is not None and np.array_equal(back, im), f"{base} 拼合图重存后和原图不一样"
        files[f"{bdr}/composite.png"] = {"src": rel(bs / "composite.png"), "src_sha256": sha256(bs / "composite.png"),
                                         "shape": list(im.shape), "pixels_sha256": hashlib.sha256(im.tobytes()).hexdigest(),
                                         "how": "PNG 压缩级 9 无损重存，生成时解码逐像素核对过"}
        # flow.json：去缩进（11-13 存的物流连接结果，stored 段用它）
        (out / bdr / "flow.json").write_text(json.dumps(jload(bs / "flow.json"), ensure_ascii=False, separators=(",", ":")),
                                            encoding="utf-8")
        files[f"{bdr}/flow.json"] = {"src": rel(bs / "flow.json"), "src_sha256": sha256(bs / "flow.json"), "how": "去缩进"}
        for f in BASE_FILES:
            put_copy(bs / f, f"{bdr}/{f}")
        marks = ((inp.get(base) or {}).get("marks")) if isinstance(inp, dict) else None
        if not marks and (bs / "inputs_auto.json").exists():  # pipenet.load_inputs：inputs.json 里没有这个据点的标注才读它
            put_copy(bs / "inputs_auto.json", f"{bdr}/inputs_auto.json", "原样复制（inputs.json 里没有这个据点，输入端用自动候选）")
        tf = jload(bs / "transform.json")
        bps = list(tf.get("bps") or {})
        for bp in bps:
            s, dr = src_proj / "bp" / bp, f"proj/bp/{bp}"
            if not (s / "anchors.json").exists():
                continue
            put_copy(s / "anchors.json", f"{dr}/anchors.json")
            if (s / "links.json").exists():
                put_copy(s / "links.json", f"{dr}/links.json")
                m = V.rd(s / "mosaic.png")
                assert m is not None, f"读不到 {rel(s / 'mosaic.png')}"
                (out / dr / "mosaic.png").write_bytes(_imencode_png9(np.full((m.shape[0], m.shape[1], 3), 255, np.uint8)))
                files[f"{dr}/mosaic.png"] = {"src": rel(s / "mosaic.png"), "src_sha256": sha256(s / "mosaic.png"), "shape": list(m.shape),
                                             "how": "同尺寸纯白占位图：代码只读 shape（vision.py:581-584 的 preview_to_canvas 不用 mos_shape）；"
                                                    "不能删，删了等于没有暗管配对"}
        # 底账
        if ledgers and base in ledgers:
            led_src = Path(ledgers[base])
        else:
            own = src_proj / "ledger" / f"{base}.json"
            led_src = own if own.exists() else (archive / "ledger" / f"{base}.json" if archive else None)
        assert led_src is not None and led_src.exists(), f"{base} 没有底账"
        put_copy(led_src, f"{led_dir}/{base}.json", "冻结当时界面选中的那份（项目自己扫的优先，没有用档案的）")
        if archive:
            for s, dr in ((archive / "ledger" / f"{base}_recipes.json", f"archive/ledger/{base}_recipes.json"),
                          (archive / "ledger" / "gate_limits" / f"{base}.json", f"archive/ledger/gate_limits/{base}.json")):
                if s.exists():
                    put_copy(s, dr, "冻结档案里的那份")
            assert (out / f"archive/ledger/{base}_recipes.json").exists(), f"{base} 档案里没有配方表，样本会改走机身图标推配方"
        man_bases[base] = {"blueprints": bps, "ledger": f"{led_dir}/{base}.json", "ledger_src": rel(led_src),
                           "inputs": "inputs.json 里作者填的" if marks else "inputs_auto.json（自动候选）"}
    for r, info in files.items():
        p = out / r
        info["sha256"] = sha256(p)
        info["bytes"] = p.stat().st_size
    ids = template_ids()
    man = {
        "format": FORMAT,
        "name": name,
        "doc": ("设计分析整份快照的样本输入（审计改进第 5 项）。manifest 只覆盖 " + "、".join(f"{c}/" for c in covers)
                + "；expect/、truth.json、changelog.jsonl、ake_memo.json.gz 不在范围内（memo 由指纹单独校验）。"
                "冻结了 11-13 存的 flow.json、当时界面选中的底账、档案配方表和准入口限速，档案以后更新不会带动快照。"
                "拼合图按 PNG 压缩级 9 无损重存；有 links.json 的蓝图 mosaic.png 是同尺寸纯白占位图（代码只读 shape）。"
                "重做样本：python tests/_designsnap.py build-fixture <名字> --reason …（要有 debug/）。"),
        "covers": covers,
        "archive": "archive" if archive else None,
        "source": {"project": rel(src_proj), "archive": rel(archive) if archive else None},
        "bases": man_bases,
        "files": dict(sorted(files.items())),
        "repo_data_sha1": repo_data_sha1(),
        "ake_icons": {"dir": ".cache/icons", "template_ids": len(ids), "files": sorted({f"{ic}.png" for _, ic, _ in ids}),
                      "note": "图标比对模板 = 配方里出现过的物品 + resource/icons/fluid；现算（录缓存、--no-memo、抽查）时这些都要在"},
        "env": env_versions(),
        "made": {"date": time.strftime("%Y-%m-%d %H:%M"), "git": git_code(), "reason": reason},
    }
    (out / MANIFEST).write_text(jdump(man), encoding="utf-8")
    log(f"样本 {name}：{len(files)} 个输入文件，{sum(i['bytes'] for i in files.values()) / 1e6:.2f} MB；据点 {', '.join(bases)}")
    for base, b in man_bases.items():
        log(f"  {base}：底账来自 {b['ledger_src']}；输入端 {b['inputs']}；蓝图 {len(b['blueprints'])} 张")
    if had_expect:
        log("目标目录已有 expect/：之后必须 --rerecord-ake 再 --accept（tests/test_design_snapshot.py）")
    return man


def repo_data_sha1() -> dict:
    out = {}
    for r in REPO_DATA_FILES:
        p = ROOT / r
        out[r] = sha1_bytes(p.read_bytes()) if p.exists() else None
    for r in REPO_DATA_DIRS:
        p = ROOT / r
        out[r] = dir_sha1(p) if p.exists() else None
    return out


def check_manifest(fx: Path) -> list[str]:
    """样本输入完整性：manifest 覆盖的目录里，每个文件 sha256 都对、没有多余文件、没有缺文件；有档案的样本每个据点都有配方表。
    → 问题清单（空 = 没问题）。expect/、truth.json、changelog.jsonl、ake_memo.json.gz 不在检查范围内。"""
    fx = Path(fx)
    if not (fx / MANIFEST).exists():
        return [f"没有 {MANIFEST}"]
    man = load_manifest(fx)
    probs = []
    have = set()
    for c in covered_dirs(man):
        for p in (fx / c).rglob("*") if (fx / c).exists() else []:
            if p.is_file():
                have.add(p.relative_to(fx).as_posix())
    want = set(man["files"])
    for r in sorted(have - want):
        probs.append(f"多余文件 {r}")
    for r in sorted(want - have):
        probs.append(f"缺文件 {r}")
    for r in sorted(want & have):
        if sha256(fx / r) != man["files"][r]["sha256"]:
            probs.append(f"sha256 对不上 {r}")
    if man.get("archive"):
        for base in man["bases"]:
            if not (fx / man["archive"] / "ledger" / f"{base}_recipes.json").exists():
                probs.append(f"缺 {man['archive']}/ledger/{base}_recipes.json（会悄悄改走机身图标推配方）")
    return probs


# ------------------------------------------------------------------ 图标比对缓存：原函数、指纹
_ORIG = None
_ACTIVE = None   # 正在生效的 OfflineState（offline() 不许嵌套）


def _originals() -> SimpleNamespace:
    """第一次调用时记下要打补丁的原对象（指纹用它们的源码，不受 offline() 里包装函数的影响）。"""
    global _ORIG
    if _ORIG is None:
        import urllib.request
        import panel_recipe as PR
        from bpa import flow as FL, logistics as LG
        objs = SimpleNamespace(cand=LG._ake_candidates, templates=LG._ake_templates, ake=LG.ake_candidates, icon=PR.icon,
                               urlopen=urllib.request.urlopen, image=FL.image, icon_dir=Path(PR.ICON_CACHE))
        for k, v in vars(objs).items():
            assert not getattr(v, "_designsnap_patch", False), f"记原函数时 {k} 已经被换掉了"
        _ORIG = objs
    return _ORIG


def template_ids() -> list[tuple[str, str, str]]:
    """和 logistics._ake_templates 同一口径派生的模板：[(物品 id, 图标 id, 名字)]，按 id 排序。"""
    from paths import ROOT as R
    rec = jload(R / "data" / "recipes.json")
    icons = jload(R / "data" / "item_icons.json")
    names = jload(R / "data" / "item_id_names.json")
    fluids = {f.stem for f in (R / "resource" / "icons" / "fluid").glob("*.png")}
    ids = sorted({k for r in rec for k in list(r["inputs"]) + list(r["outputs"])} | fluids)
    return [(i, icons.get(i, i), names.get(i, i)) for i in ids]


def fingerprint() -> tuple[str, dict]:
    """图标比对缓存的指纹 → (总指纹, {组成: 子哈希})。
    组成：LG._ake_candidates / _ake_templates / ake_candidates、PR.icon 的源码（取保存下来的原函数，不是 offline() 的包装）；
    派生出的模板 id 列表、它们的图标映射和名字；resource/icons/fluid 的文件字节；cv2、numpy 版本。
    不哈希整个 recipes.json：改配方秒数这类不影响模板的修改不该让缓存作废。"""
    import cv2
    import numpy as np
    from paths import ROOT as R
    O = _originals()
    fl = hashlib.sha1()
    for f in sorted((R / "resource" / "icons" / "fluid").glob("*.png")):
        fl.update(f.name.encode("utf-8") + b"\0" + f.read_bytes() + b"\0")
    parts = {
        "src:_ake_candidates": inspect.getsource(O.cand),
        "src:_ake_templates": inspect.getsource(O.templates),
        "src:ake_candidates": inspect.getsource(O.ake),
        "src:PR.icon": inspect.getsource(O.icon.__wrapped__),
        "templates": json.dumps(template_ids(), ensure_ascii=False),
        "fluid_icons": fl.hexdigest(),
        "cv2": cv2.__version__,
        "numpy": np.__version__,
    }
    h = hashlib.sha1()
    sub = {}
    for k, v in parts.items():
        b = v.encode("utf-8")
        sub[k] = sha1_bytes(b)[:12]
        h.update(k.encode("utf-8") + b"\0" + b + b"\0")
    return h.hexdigest()[:16], sub


def read_memo(path: Path) -> dict:
    return json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))


def _memo_key(e: dict) -> tuple:
    return (tuple(e["shape"]), e["dtype"], base64.b64decode(e["bytes_b64"]), e["top"])


def load_memo(memo) -> int:
    """把缓存条目灌进 LG._AKE_CACHE（键和 logistics.ake_candidates 一样：(shape, dtype.str, 图标字节, top)）。不查指纹，调用方先查。"""
    from bpa import logistics as LG
    m = read_memo(memo) if isinstance(memo, (str, Path)) else memo
    for e in m["entries"]:
        LG._AKE_CACHE[_memo_key(e)] = [dict(r) for r in e["res"]]
    return len(m["entries"])


def memo_entries_from_cache() -> list[dict]:
    """LG._AKE_CACHE → 可以落盘的条目（按图标字节和 top 排序，文件内容稳定）。dtype.str 是「|u1」，不拿它拼字符串键。"""
    from bpa import logistics as LG
    rows = []
    for (shape, dtype, b, top), res in LG._AKE_CACHE.items():
        rows.append({"shape": list(shape), "dtype": dtype, "bytes_b64": base64.b64encode(b).decode("ascii"), "top": top,
                     "res": [dict(r) for r in res]})
    rows.sort(key=lambda e: (sha1_bytes(base64.b64decode(e["bytes_b64"])), e["top"], e["shape"]))
    return rows


def write_memo(path: Path, entries: list[dict], reason: str, fp: tuple[str, dict]) -> None:
    doc = {"format": FORMAT, "fingerprint": fp[0], "parts": fp[1], "made": time.strftime("%Y-%m-%d %H:%M"), "code": git_code(),
           "env": env_versions(), "reason": reason,
           "doc": "logistics._AKE_CACHE 落盘：键 = 图标 shape、dtype、字节、top；值 = 候选 [{id, name, state, score}]。指纹对不上一律不用",
           "entries": entries}
    Path(path).write_bytes(gzip.compress(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"), 9, mtime=0))


def stale_parts(memo: dict, cur: dict) -> list[str]:
    old = memo.get("parts") or {}
    return [k for k in sorted(set(old) | set(cur)) if old.get(k) != cur.get(k)]


# ------------------------------------------------------------------ 离线运行
@dataclasses.dataclass
class OfflineState:
    icon_dir: Path = None              # 运行时 PR.ICON_CACHE
    empty_dir: Path | None = None      # offline() 建的空临时图标目录
    live: bool = False                 # 用真图标现算（没装缓存，或缓存过期 / 未命中后切过去的）
    memo_state: str = "none"           # ok 装上了 / stale 指纹不一致（没装） / none 没给缓存
    memo_entries: int = 0
    stale: list = dataclasses.field(default_factory=list)   # 指纹里变了的组成
    needs_rerecord: bool = False       # 指纹不一致或运行中未命中：memo 要重录
    icons_ok: bool | None = None       # 图标齐不齐（查过才有值）
    icons_detail: str = ""
    net: int = 0                       # 联网尝试次数（都被挡下）
    net_ids: list = dataclasses.field(default_factory=list)
    miss: int = 0                      # LG._ake_candidates 真算的次数 = 缓存未命中
    miss_keys: list = dataclasses.field(default_factory=list)
    fingerprint: str = ""
    fp_parts: dict = dataclasses.field(default_factory=dict)

    def reset_counts(self):
        self.net, self.net_ids, self.miss, self.miss_keys = 0, [], 0, []


class _NoImage:
    def save(self, *a, **k):
        pass


def _no_image(*a, **k):
    return _NoImage()


_no_image._designsnap_patch = True


def icons_complete(man: dict | None = None, icon_dir: Path | None = None) -> tuple[bool, str]:
    """「图标齐全」：manifest 列的所需图标文件（和按现在数据派生的）都在 icon_dir（默认 .cache/icons），
    而且 len(LG._ake_templates()) 等于模板 id 总数（现在 245）。只能在 offline() 里调（禁网）；
    调完 PR.ICON_CACHE 指着 icon_dir、LG._ake 是用它建的模板。缺文件时不建模板（免得去下载）。"""
    import panel_recipe as PR
    from bpa import logistics as LG
    assert _ACTIVE is not None, "icons_complete 要在 offline() 里调"
    O = _originals()
    icon_dir = Path(icon_dir or O.icon_dir)
    ids = template_ids()
    need = sorted({f"{ic}.png" for _, ic, _ in ids} | set(((man or {}).get("ake_icons") or {}).get("files") or []))
    missing = [f for f in need if not (icon_dir / f).exists()]
    if missing:
        return False, f"{rel(icon_dir)} 缺 {len(missing)}/{len(need)} 个图标文件（例 {', '.join(missing[:3])}）"
    PR.ICON_CACHE = icon_dir
    PR.icon.cache_clear()
    LG._ake = None
    n = len(O.templates())
    if n != len(ids):
        return False, f"模板 {n}/{len(ids)} 个（有图标读不出来）"
    return True, f"模板 {n}/{len(ids)} 个，图标文件 {len(need)} 个都在"


@contextlib.contextmanager
def offline(fx: Path | None = None, *, memo: bool | Path = True, live: bool = False):
    """离线跑设计分析 / 物流连接的上下文。进入时：
      - PR.ICON_CACHE 指到空临时目录，PR.icon.cache_clear()，LG._ake 置 None（模板缓存是模块全局，空目录下建出来是空的）；
      - urllib.request.urlopen 换成「计数后抛错」；LG._ake_candidates 换成「计数后调原函数」（= 缓存未命中次数）；
      - FL.image 换成空操作；LG._AKE_CACHE 清空。
    memo（默认 fx/ake_memo.json.gz）装之前先核指纹：
      - 一致 → 装上；
      - 不一致、本机图标齐全 → 不装，改成用 .cache/icons 现算（st.live、st.needs_rerecord；test_memo 据此判失败「请 --rerecord-ake」）；
      - 不一致、本机图标不齐 → 抛 MemoStale（测试里 skip「图标比对缓存过期且本机没有完整的 .cache/icons」）。
      任何情况下都不拿过期的 memo 跑。
    运行中未命中：本机图标齐全 → 切到 .cache/icons 现算、照常出结果，st.needs_rerecord=True（「memo 要重录」）；
                 本机图标不齐 → 按空模板算（结果作废），st.miss_keys 记下哪几条，调用方据 st.miss 判失败。
    live=True（录缓存、--no-memo、抽查）：不装 memo，PR.ICON_CACHE 指 .cache/icons，先断言图标齐全（不齐抛 IconsIncomplete）。
    退出时以上全部还原（_AKE_CACHE 恢复进入前的内容）。"""
    global _ACTIVE
    import urllib.request
    import panel_recipe as PR
    from bpa import flow as FL, logistics as LG
    assert _ACTIVE is None, "offline() 不许嵌套"
    O = _originals()
    st = OfflineState()
    st.fingerprint, st.fp_parts = fingerprint()           # 打任何补丁之前算
    man = load_manifest(fx) if fx and (Path(fx) / MANIFEST).exists() else None
    saved = {"icon_dir": PR.ICON_CACHE, "ake": LG._ake, "cache": dict(LG._AKE_CACHE)}
    empty = Path(tempfile.mkdtemp(prefix="designsnap_icons_"))

    def blocked(req, *a, **k):
        st.net += 1
        st.net_ids.append(str(getattr(req, "full_url", req)).rsplit("/", 1)[-1][:60])
        raise OSError("快照测试里禁止联网")
    blocked._designsnap_patch = True

    def counted(icon, top=3):
        st.miss += 1
        st.miss_keys.append({"shape": list(icon.shape), "top": top, "sha1": sha1_bytes(icon.tobytes())[:12]})
        if not st.live and st.memo_state != "none":
            st.needs_rerecord = True
            if st.icons_ok is None:
                st.icons_ok, st.icons_detail = icons_complete(man)
            if st.icons_ok:
                st.live = True           # icons_complete 已经把 PR.ICON_CACHE 指到 .cache/icons、建好模板
                st.icon_dir = Path(PR.ICON_CACHE)
            else:
                PR.ICON_CACHE = empty    # 不齐：按空模板算，结果作废，调用方按 st.miss 判失败
                PR.icon.cache_clear()
                LG._ake = []
        return O.cand(icon, top)
    counted._designsnap_patch = True

    _ACTIVE = st
    try:
        urllib.request.urlopen = blocked
        LG._ake_candidates = counted
        FL.image = _no_image
        LG._AKE_CACHE.clear()
        PR.ICON_CACHE = empty
        PR.icon.cache_clear()
        LG._ake = None
        st.empty_dir = empty
        if live:
            st.icons_ok, st.icons_detail = icons_complete(man)
            if not st.icons_ok:
                raise IconsIncomplete(f"要现算，但本机图标不齐：{st.icons_detail}")
            st.live = True
        elif memo:
            mp = Path(memo) if isinstance(memo, (str, Path)) else (Path(fx) / MEMO_NAME if fx else None)
            if mp is None or not mp.exists():
                raise MemoStale(f"没有图标比对缓存 {mp.name if mp else MEMO_NAME}")
            m = read_memo(mp)
            if m.get("fingerprint") == st.fingerprint:
                st.memo_entries = load_memo(m)
                st.memo_state = "ok"
            else:
                st.memo_state, st.needs_rerecord = "stale", True
                st.stale = stale_parts(m, st.fp_parts)
                st.icons_ok, st.icons_detail = icons_complete(man)
                if not st.icons_ok:
                    raise MemoStale(f"图标比对缓存过期且本机没有完整的 .cache/icons（指纹变了："
                                    f"{', '.join(st.stale) or '总指纹不同，组成的子哈希一样'}；{st.icons_detail}）")
                st.live = True
        st.icon_dir = Path(PR.ICON_CACHE)
        yield st
    finally:
        urllib.request.urlopen = O.urlopen
        LG._ake_candidates = O.cand
        FL.image = O.image
        PR.ICON_CACHE = saved["icon_dir"]
        PR.icon.cache_clear()
        LG._ake = saved["ake"]
        LG._AKE_CACHE.clear()
        LG._AKE_CACHE.update(saved["cache"])
        shutil.rmtree(empty, ignore_errors=True)
        _ACTIVE = None


# ------------------------------------------------------------------ 跑一个据点
def lkey(l: dict) -> list:
    """连线的比较键（item3f / simtools/snap.py 的口径）。第 2 步的比较器另用画布坐标 + 类型做键。"""
    a, b = l.get("a_dev") or {}, l.get("device") or {}
    return [l["layer"], l.get("a"), l.get("b"), a.get("name"), [round(x) for x in a.get("c", [])], b.get("name"),
            [round(x) for x in b.get("c", [])], l.get("dir")]


def _tmpdir(prefix: str) -> Path:
    t = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, t, True)
    return t


def _run_bd(bd: Path, archive: Path | None, base: str, led_p: Path, kind: str, ledger_dict: dict | None = None) -> dict:
    from bpa import design as D, flow as FL
    if kind == "reflow":
        (bd / "flow.json").unlink()
        FL.run_base(bd, ledger_dict if ledger_dict is not None else jload(led_p))
    elif kind != "stored":
        raise ValueError(kind)
    D.run_base(bd, archive, base, ledger=led_p)
    flow = jload(bd / "flow.json")
    return {"design": jload(bd / "design.json"), "links": [lkey(l) for l in flow["links"]], "flow": flow,
            "recipes_icon": (bd / "recipes_icon.json").exists()}


def run_case(fx: Path, base: str, kind: str, ledger_dict: dict | None = None) -> dict:
    """样本 fx 的一个据点跑 stored 或 reflow → {"design": design.json, "links": [lkey…], "flow": flow.json, "recipes_icon": bool}。
    fx/proj 复制到临时目录再跑（run_base 会往 bd 写 design.json / flow.json / jpg），跑完删掉（atexit 兜底）。
    stored：D.run_base(bd, fx/archive, base, ledger=底账)，用样本里冻结的 flow.json；
    reflow：先删副本里的 flow.json，FL.run_base(bd, 底账 dict) 现算物流连接，再 D.run_base。
    不经过 project.ledger_path / rerun_design（写死了 ROOT/debug/bpa、ROOT/data/blueprints）。
    有档案的样本跑完断言没写出 recipes_icon.json（档案路径放错时 run_base 不报错，悄悄改走机身图标推配方）。"""
    fx = Path(fx)
    man = load_manifest(fx)
    arch = fx / man["archive"] if man.get("archive") else None
    led_p = fx / man["bases"][base]["ledger"]
    tmp = _tmpdir("designsnap_case_")
    try:
        shutil.copytree(fx / "proj", tmp / "proj")
        out = _run_bd(tmp / "proj" / "base" / base, arch, base, led_p, kind, ledger_dict)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if arch is not None:
        assert not out["recipes_icon"], f"{base} {kind}：写出了 recipes_icon.json（档案路径不对，改走了机身图标推配方）"
    return out


def run_fixture(fx: Path, kinds=("stored", "reflow"), bases=None, log=None) -> dict:
    """样本里每个据点跑 kinds → {据点: {kind: run_case 的结果, "secs": {kind: 秒}}}。要在 offline() 里调。"""
    man = load_manifest(fx)
    out = {}
    for base in bases or man["bases"]:
        out[base] = {"secs": {}}
        for kind in kinds:
            t = time.time()
            out[base][kind] = run_case(fx, base, kind)
            out[base]["secs"][kind] = round(time.time() - t, 1)
            if log:
                log(f"  {base} {kind}：{len(out[base][kind]['design']['devices'])} 台，{out[base]['secs'][kind]} 秒")
    return out


def record_memo(fx: Path, reason: str, log=print) -> dict:
    """在真实 .cache/icons 上（禁网、先断言图标齐全）清空 LG._AKE_CACHE，跑完样本每个据点的 stored 和 reflow，
    把缓存连同指纹落成 fx/ake_memo.json.gz。"""
    from bpa import logistics as LG
    fx = Path(fx)
    t0 = time.time()
    with offline(fx, live=True) as st:
        log(f"录图标比对缓存：{st.icons_detail}；图标目录 {rel(st.icon_dir)}")
        LG._AKE_CACHE.clear()
        run_fixture(fx, log=log)
        assert st.net == 0, f"录缓存时有 {st.net} 次联网尝试：{st.net_ids[:5]}"
        # 每个不同的图标只真算一次；条数对不上说明缓存中途被清过（logistics.ake_candidates 超过 4000 条会清空）
        assert len(LG._AKE_CACHE) == st.miss, f"缓存 {len(LG._AKE_CACHE)} 条，真算 {st.miss} 次"
        entries = memo_entries_from_cache()
        fp = (st.fingerprint, st.fp_parts)
        misses = st.miss
    write_memo(fx / MEMO_NAME, entries, reason, fp)
    info = {"entries": len(entries), "computed": misses, "fingerprint": fp[0], "secs": round(time.time() - t0, 1),
            "bytes": (fx / MEMO_NAME).stat().st_size}
    log(f"缓存 {info['entries']} 条（真算 {misses} 次），指纹 {fp[0]}，{info['bytes'] / 1e6:.2f} MB，用时 {info['secs']} 秒")
    return info


# ------------------------------------------------------------------ 打包 / 比较（item3f 口径；完整比较器在第 2 步）
def pack_devs(res: dict) -> list[dict]:
    """和 simtools/snap.py 的 devs() 完全一样的取整口径。"""
    return [{"device": d["device"], "c": [int(d["c"][0]), int(d["c"][1])], "state": d["state"], "rate": round(float(d["rate"]), 4),
             "recipe": d.get("recipe"), "got": {k: round(float(v), 3) for k, v in (d.get("got") or {}).items()},
             "need": {k: round(float(v), 3) for k, v in (d.get("need") or {}).items()}} for d in res["devices"]]


def _pack_one(res: dict) -> dict:
    return {"devices": pack_devs(res), "states": dict(Counter(d["state"].split("（")[0] for d in res["devices"])),
            "pending": [p.get("key") for p in res.get("pending") or []], "reread": len(res.get("reread") or []),
            "recipe_fixes": len(res.get("recipe_fixes") or []), "gates": len(res.get("gates") or []), "ports": len(res.get("ports") or [])}


def pack_item3f(stored: dict, reflow: dict, links: list, flow_issues: list | None) -> dict:
    """一个据点 → item3f 那种格式：{stored, reflow, links（reflow 的 lkey）, issues（reflow 物流 issues 条数）}。"""
    return {"stored": _pack_one(stored), "reflow": _pack_one(reflow), "links": links, "issues": len(flow_issues or [])}


def cmp_item3f(ref: dict, cur: dict) -> tuple[int, list[str], dict]:
    """item3f 的一个据点 vs 现在（pack_item3f 的结果）→ (不同处数, 打印行, 统计)。
    逐台比 state / rate / recipe / got / need（键 = 设备名 + 取整画布坐标）、states、连线多重集合；
    pending / reread / recipe_fixes / gates / ports / issues 的个数也比（附带）。"""
    bad, lines, stat = 0, [], {}
    for kind in ("stored", "reflow"):
        A, B = ref[kind], cur[kind]
        da = {(x["device"], tuple(x["c"])): x for x in A["devices"]}
        db = {(x["device"], tuple(x["c"])): x for x in B["devices"]}
        diffs = []
        for k in sorted(set(da) | set(db), key=str):
            x, y = da.get(k), db.get(k)
            if x is None or y is None:
                diffs.append(f"{k[0]}{list(k[1])} 只在{'item3f' if y is None else '这次'}有")
                continue
            fs = [f for f in ("state", "rate", "recipe", "got", "need") if x[f] != y[f]]
            if fs:
                diffs.append(f"{k[0]}{list(k[1])}：" + "；".join(f"{f}: {x[f]} → {y[f]}" for f in fs)[:300])
        same_states = A["states"] == B["states"]
        extra = [f"{f} {A.get(f)!r} → {B.get(f)!r}" for f in ("pending", "reread", "recipe_fixes", "gates", "ports")
                 if f in A and A.get(f) != B.get(f)]
        stat[kind] = {"n_ref": len(A["devices"]), "n": len(B["devices"]), "diff": len(diffs), "states_same": same_states,
                      "states": B["states"], "extra": extra}
        lines.append(f"{kind}：item3f {len(A['devices'])} 台 / 这次 {len(B['devices'])} 台；逐台不同 {len(diffs)} 台；"
                     f"states 一样 {same_states}（{'、'.join(f'{s} {n}' for s, n in B['states'].items())}）"
                     + (f"；附带字段不同：{'；'.join(extra)}" if extra else "；pending / reread / recipe_fixes / gates / ports 个数一样"))
        lines += ["    " + s for s in diffs[:30]]
        bad += len(diffs) + (not same_states) + len(extra)
    ca = Counter(json.dumps(l, ensure_ascii=False) for l in ref["links"])
    cb = Counter(json.dumps(l, ensure_ascii=False) for l in cur["links"])
    same_links = ca == cb
    stat["links"] = {"n_ref": len(ref["links"]), "n": len(cur["links"]), "same": same_links}
    lines.append(f"reflow 连线：这次 {len(cur['links'])} 条，item3f {len(ref['links'])} 条，多重集合相同 {same_links}"
                 + ("" if same_links else f"（少 {sum((ca - cb).values())} 条、多 {sum((cb - ca).values())} 条）"))
    if "issues" in ref:
        si = ref["issues"] == cur["issues"]
        lines.append(f"物流 issues：这次 {cur['issues']} 条，item3f {ref['issues']} 条，一样 {si}")
        bad += not si
    bad += not same_links
    return bad, lines, stat


def _rowkey(r: dict) -> str:
    return f"{r.get('device')}@{r.get('x')},{r.get('y')}"


def cmp_exact(A, B, label: str) -> tuple[int, list[str]]:
    """两份结果逐字段**完全相等**（不设容差，浮点按 json 文本比，依据全文也比）→ (不同处数, 打印行)。"""
    ja, jb = jdump(A, None), jdump(B, None)
    if ja == jb:
        return 0, [f"{label}：完全相同"]
    if not (isinstance(A, dict) and isinstance(B, dict)):
        return 1, [f"{label}：不同（{len(A) if hasattr(A, '__len__') else A} / {len(B) if hasattr(B, '__len__') else B}）"]
    n, lines = 0, []
    for k in sorted(set(A) | set(B)):
        if jdump(A.get(k), None) == jdump(B.get(k), None):
            continue
        n += 1
        if k == "devices" and isinstance(A.get(k), list) and isinstance(B.get(k), list):
            ra = {_rowkey(r): r for r in A[k]}
            rb = {_rowkey(r): r for r in B[k]}
            rows = []
            for kk in sorted(set(ra) | set(rb)):
                x, y = ra.get(kk), rb.get(kk)
                if x is None or y is None:
                    rows.append(f"{kk} 只在{'A' if y is None else 'B'}")
                    continue
                fs = [f for f in sorted(set(x) | set(y)) if jdump(x.get(f), None) != jdump(y.get(f), None)]
                if fs:
                    rows.append(f"{kk}: " + "；".join(f"{f}: {jdump(x.get(f), None)[:100]} → {jdump(y.get(f), None)[:100]}" for f in fs))
            lines.append(f"{label}.devices 不同 {len(rows)} 台（行顺序一样 {[_rowkey(r) for r in A[k]] == [_rowkey(r) for r in B[k]]}）")
            lines += ["    " + s[:400] for s in rows[:20]]
        else:
            lines.append(f"{label}.{k} 不同：{jdump(A.get(k), None)[:200]}  →  {jdump(B.get(k), None)[:200]}")
    return n, lines


# ================================================================== 第 2 步：期望快照、比较器、实测判词、diff 打印
EXPECT_DIR = "expect"
CHANGELOG = "changelog.jsonl"           # 在 expect/ 下，每次 --init / --accept / --sync-truth 追加一行
TRUTH_NAME = "truth.json"
TOL_RATE = 0.001                         # 开工率：差 ≤ 0.001 算没变
TOL_FLOW = 0.01                          # 量（个/分）和其它数：差 ≤ 0.01 算没变
COORD_TOL = 0.5                          # 画布坐标：差超过 0.5 px 只提示
HARD = ("state", "rate", "recipe", "recipe_inferred", "got", "need", "available", "limit_by", "zero_margin", "feeds", "fuel", "loop",
        "env_required", "via_inferred", "if_main_blocked", "channels", "need_capped", "pool_src", "recipe_unknown", "can_make",
        "blocked_by_unknown", "candidates")   # 不在这里、也不是依据 / 坐标的新字段，一样按硬字段比
WORDING = ("basis",)                     # 依据全文；state 括号里的说明也归这一类（措辞）
NOT_COMPARED = ("device", "truth")       # device 已在键里；truth 是嵌入的实测标注，不是程序结果
ROW_RULES = {                            # 这些键只许出现在什么样的行上（原型踩过：所有行都写 channels: null）
    "channels": lambda r: bool(r.get("channels")), "need_capped": lambda r: bool(r.get("channels")),
    "pool_src": lambda r: r.get("device") in ("反应池", "扩容反应池") or bool(r.get("recipe_unknown")),
    "candidates": lambda r: r.get("device") in ("反应池", "扩容反应池") or bool(r.get("recipe_unknown")),
    "recipe_unknown": lambda r: bool(r.get("recipe_unknown")), "can_make": lambda r: bool(r.get("recipe_unknown")),
    "if_main_blocked": lambda r: str(r.get("state", "")).startswith("溢流位"),
}
ITEM_SECS = ("gates", "pending", "recipe_fixes", "ports", "issues", "inferred_links", "reread", "notes")
FLOW_SECS = ("flow_issues", "structures")   # 只在 reflow 段
KNOWN_TOP = {"devices", "summary", "tunnels", "iters", *ITEM_SECS}
ID_FIELDS = ("a", "b", "node", "gate", "nodes")   # 物流节点编号：只作行内字段打印，不参加比较
LAYER = {"belt": "带子", "pipe": "管子"}
STATE_CATS = ("满速", "间歇", "溢流位", "没接上", "配方未知", "没在用")
SYM_ORDER = {"✗": 0, "✓": 1, "↗": 2, "↘": 3, "·": 4}
REP_RATE = {"有料": 1.0, "备路": 0.0, "平时没料": 0.0, "时有时无": 0.5}   # 实测没给开工率时按类别取的代表值
GATE_MIN, GATE_FRAC = 5, 0.5             # 防退化的闸：同一种迁移 ≥5 台、迁移后和实测一致的不到一半
DIFF_MAX_LINES = 60                      # run_all 失败时只抓前 60 行「差异 」


def _r(v, nd=3):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, dict):
        return {k: _r(x, nd) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_r(x, nd) for x in v]
    return v


def _rc(c) -> str:
    try:
        return f"({round(c[0])},{round(c[1])})"
    except (TypeError, IndexError):
        return "(?)"


def short(v, n=90) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[:n] + "…"


# ------------------------------------------------------------------ 打包
def dev_key(d: dict, taken: set) -> str:
    """设备键 =「设备名@底账x,y」（和 fixes / confirm 同口径）；撞了加「#画布cx,cy」，再撞就报错（不按顺序编 #2，行序一变键就漂）。"""
    k = f"{d['device']}@{d['x']},{d['y']}"
    if k in taken:
        k2 = f"{k}#画布{round(d['c'][0])},{round(d['c'][1])}"
        if k2 in taken:
            raise ValueError(f"设备键撞了两次：{k2}（底账里同名同坐标、画布坐标也一样，快照分不开）")
        return k2
    return k


def pack_row(d: dict) -> dict:
    """设备行原样保留全部字段（去掉 x / y，已在键里）；rate 4 位小数，量和其它数 3 位，c 1 位。"""
    row = _r({f: v for f, v in d.items() if f not in ("x", "y")}, 3)
    row["rate"] = round(float(d.get("rate") or 0.0), 4)
    if isinstance(d.get("c"), (list, tuple)):
        row["c"] = [round(float(x), 1) for x in d["c"]]
    return row


def _node_index(flow: dict | None) -> dict:
    return {n.get("id"): n for n in ((flow or {}).get("nodes") or [])}


def _nref(nodes: dict, i) -> str | None:
    if i is None or isinstance(i, bool):
        return None
    n = nodes.get(i)
    return f"{n.get('type')}{_rc(n.get('c'))}" if n else f"节点{i}"


def _dref(dv: dict | None) -> str | None:
    return f"{dv.get('name')}{_rc(dv.get('c'))}" if dv else None


def canon_item(it, nodes: dict):
    """其它段（准入口、待确认、物流 issues、结构…）的一条：数取整；物流节点编号另加「类型(画布x,y)」的 *_ref 字段（比较用 ref，不用编号）。"""
    if not isinstance(it, dict):
        return _r(it)
    out = _r(dict(it))
    for f in ("a", "b", "node", "gate"):
        v = it.get(f)
        if isinstance(v, int) and not isinstance(v, bool):
            out[f + "_ref"] = _nref(nodes, v)
    if isinstance(it.get("nodes"), list):
        out["nodes_ref"] = [_nref(nodes, i) for i in it["nodes"]]
    return out


def link_rec(l: dict, nodes: dict) -> dict:
    """一条物流连线 → 比较用的记录：按物料流向写成 起点 → 终点（dir=b->a 的倒过来，从哪头追到的不算变化），
    每头 = 「设备名(画布x,y)」或「节点类型(画布x,y)」+ 接口边（设备边 a_dev_side / dev_side，节点边 a_side / b_side）+ 认法（how）+ 类型；
    节点编号只留在 lkey 里（行内字段，给 item3f 对照用，不参加比较）。"""
    ad, dv = l.get("a_dev") or {}, l.get("device") or {}
    ea = {"ref": _dref(ad) if ad else _nref(nodes, l.get("a")), "side": l.get("a_dev_side") or l.get("a_side"),
          "how": ad.get("how"), "type": l.get("a_end") or ("node" if l.get("a") is not None else None)}
    eb = {"ref": _dref(dv) if dv else _nref(nodes, l.get("b")), "side": l.get("dev_side") or l.get("b_side"),
          "how": dv.get("how"), "type": l.get("end")}
    d = l.get("dir")
    if d == "b->a":
        ea, eb = eb, ea
    rec = {"layer": l.get("layer"), "dir": "→" if d in ("a->b", "b->a") else d}
    for p, e in (("src", ea), ("dst", eb)):
        rec[p], rec[p + "_side"], rec[p + "_how"], rec[p + "_type"] = e["ref"], e["side"], e["how"], e["type"]
    rec["lkey"] = lkey(l)
    return rec


def _lcore(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "lkey"}


def pack_section(design: dict, flow: dict | None, kind: str) -> dict:
    """design.json（+ 它用的 flow.json）→ 快照段。reflow 段另带连线、物流 issues、structures。"""
    nodes = _node_index(flow)
    devs, taken = {}, set()
    for d in design.get("devices") or []:
        k = dev_key(d, taken)
        taken.add(k)
        devs[k] = pack_row(d)
    sec = {"devices": devs}
    for s in ITEM_SECS:
        sec[s] = [canon_item(x, nodes) for x in (design.get(s) or [])]
    sec["summary"] = _r(design.get("summary"))
    sec["tunnels"] = _r(design.get("tunnels"))
    sec["iters"] = design.get("iters")
    sec["extra"] = {k: _r(v) for k, v in design.items() if k not in KNOWN_TOP}
    sec["top_keys"] = sorted(design)
    sec["exact_sha1"] = sha1_bytes(jdump(design, None).encode("utf-8"))
    if kind == "reflow":
        sec["links"] = sorted((link_rec(l, nodes) for l in (flow or {}).get("links") or []),
                              key=lambda r: jdump(_lcore(r), None))
        sec["flow_issues"] = [canon_item(x, nodes) for x in (flow or {}).get("issues") or []]
        sec["structures"] = [canon_item(x, nodes) for x in (flow or {}).get("structures") or []]
    return sec


def item_key(sec: str, it) -> str:
    """其它段一条的键：画布坐标（取整）+ 类型 / 物品 / 设备名；物流节点编号不进键。"""
    if not isinstance(it, dict):
        return json.dumps(it, ensure_ascii=False, sort_keys=True)
    if sec == "pending":
        return f"{it.get('kind')}|{it.get('key')}"
    if sec == "recipe_fixes":
        return f"{it.get('device')}@{it.get('x')},{it.get('y')}"
    if sec == "gates":
        return f"准入口{_rc(it.get('c'))}"
    if sec == "inferred_links":
        return f"{LAYER.get(it.get('layer'), it.get('layer'))} {it.get('from')} → {it.get('to')}"
    parts = [str(it[f]) for f in ("kind", "layer", "device", "role") if it.get(f) is not None]
    parts += [_rc(it[f]) for f in ("c", "at") if isinstance(it.get(f), list) and len(it[f]) >= 2]
    parts += [str(it[f]) for f in ("a_ref", "b_ref", "node_ref", "gate_ref") if it.get(f)]
    if it.get("nodes_ref"):
        parts.append("/".join(map(str, it["nodes_ref"])))
    if parts:
        return "|".join(parts)
    return json.dumps({k: v for k, v in it.items() if k not in ID_FIELDS}, ensure_ascii=False, sort_keys=True)


def _strip_ids(it):
    return {k: v for k, v in it.items() if k not in ID_FIELDS} if isinstance(it, dict) else it


def keyed(sec: str, items) -> dict:
    """一段的条目 → {键: 条目}；同键的按内容（去掉节点编号）排序后加 #2、#3。"""
    groups = defaultdict(list)
    for it in items or []:
        groups[item_key(sec, it)].append(it)
    out = {}
    for k, its in groups.items():
        its = sorted(its, key=lambda x: jdump(_strip_ids(x), None))
        for n, it in enumerate(its, 1):
            out[k if n == 1 else f"{k}#{n}"] = it
    return out


# ------------------------------------------------------------------ 比较
def same(a, b, tol=TOL_FLOW) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k], tol) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y, tol) for x, y in zip(a, b))
    return a == b


def split_state(s) -> tuple[str, str]:
    """state → (类别前缀, 括号里的说明)：第一个「（」或「(」之前是类别（硬字段），之后是说明（措辞）。"""
    s = str(s or "")
    m = re.search(r"[（(]", s)
    return (s[:m.start()], s[m.start():]) if m else (s, "")


def scat(row: dict) -> str:
    """状态类别（迁移直方图用）：满速 / 间歇 / 溢流位 / 没接上 / 配方未知 / 没在用 / 没料。"""
    p = split_state((row or {}).get("state"))[0]
    for k in STATE_CATS:
        if p.startswith(k):
            return k
    return "没料"


def _field_diff(x, y) -> list:
    if isinstance(x, dict) and isinstance(y, dict):
        return [(f, x.get(f), y.get(f)) for f in sorted(set(x) | set(y)) if f not in ID_FIELDS and not same(x.get(f), y.get(f))]
    return [] if same(x, y) else [("值", x, y)]


def compare(A: dict, B: dict) -> dict:
    """A = 期望段，B = 现在算的段 → 结构化差异（打印、--accept、自测都用它）。"""
    d = {"hard": {}, "wording": {}, "coords": [], "new_keys": defaultdict(list), "bad_keys": defaultdict(list),
         "lost_keys": defaultdict(list), "added": [], "removed": [], "items": {}, "summary": [], "scalars": [], "top": [],
         "hit_cap": None, "hints": [], "links": None}
    da, db = A.get("devices") or {}, B.get("devices") or {}
    d["removed"] = sorted(da.keys() - db.keys())
    d["added"] = sorted(db.keys() - da.keys())
    for k in sorted(da.keys() & db.keys()):
        a, b = da[k], db[k]
        hard, word = [], []
        for f in sorted(set(a) | set(b)):
            if f in NOT_COMPARED:
                continue
            if f not in a:
                (d["bad_keys"] if f in ROW_RULES and not ROW_RULES[f](b) else d["new_keys"])[f].append(k)
                continue
            if f not in b:
                d["lost_keys"][f].append(k)
                continue
            x, y = a[f], b[f]
            if f == "c":
                if not same(x, y, COORD_TOL):
                    d["coords"].append((k, x, y))
            elif f == "state":
                px, sx = split_state(x)
                py, sy = split_state(y)
                if px != py:
                    hard.append(("state", x, y))
                elif sx != sy:
                    word.append(("state", x, y))
            elif f in WORDING:
                if x != y:
                    word.append((f, x, y))
            elif not same(x, y, TOL_RATE if f == "rate" else TOL_FLOW):
                hard.append((f, x, y))
        if hard:
            d["hard"][k] = hard
        if word:
            d["wording"][k] = word
    for s in ITEM_SECS + FLOW_SECS:
        if s not in A and s not in B:
            continue
        ka, kb = keyed(s, A.get(s)), keyed(s, B.get(s))
        add = [(k, kb[k]) for k in sorted(kb.keys() - ka.keys())]
        rem = [(k, ka[k]) for k in sorted(ka.keys() - kb.keys())]
        chg = [(k, fs) for k in sorted(ka.keys() & kb.keys()) if (fs := _field_diff(ka[k], kb[k]))]
        if add or rem or chg:
            d["items"][s] = {"added": add, "removed": rem, "changed": chg}
    sa, sb = A.get("summary") or {}, B.get("summary") or {}
    for f in sorted(set(sa) | set(sb)):
        x, y = sa.get(f), sb.get(f)
        if f == "states" and isinstance(x, dict) and isinstance(y, dict):
            y = {s: n for s, n in y.items() if s in x or n}   # 值为 0 的新状态键忽略
        if not same(x, y):
            d["summary"].append((f, x, sb.get(f)))
    for f in ("tunnels", "extra"):
        if not same(A.get(f), B.get(f)):
            d["scalars"].append((f, A.get(f), B.get(f)))
    ia, ib = A.get("iters") or {}, B.get("iters") or {}
    if ib.get("hit_cap"):
        d["hit_cap"] = ib
    elif not same(ia, ib):
        d["hints"].append(f"迭代轮数变了（只提示）：{short(ia)} → {short(ib)}")
    ta, tb = set(A.get("top_keys") or []), set(B.get("top_keys") or [])
    d["top"] = [("多了", k) for k in sorted(tb - ta)] + [("少了", k) for k in sorted(ta - tb)]
    if "links" in A or "links" in B:
        ca = Counter(jdump(_lcore(r), None) for r in A.get("links") or [])
        cb = Counter(jdump(_lcore(r), None) for r in B.get("links") or [])
        if ca != cb:
            rem = [json.loads(s) for s in sorted((ca - cb).elements())]
            add = [json.loads(s) for s in sorted((cb - ca).elements())]
            pool = defaultdict(list)
            for r in add:
                pool[(r.get("layer"), frozenset((str(r.get("src")), str(r.get("dst")))))].append(r)
            pairs, rem_left = [], []
            for r in rem:
                lst = pool.get((r.get("layer"), frozenset((str(r.get("src")), str(r.get("dst"))))))
                if lst:
                    pairs.append((r, lst.pop(0)))
                else:
                    rem_left.append(r)
            add_left = [r for lst in pool.values() for r in lst]
            d["links"] = {"removed": rem_left, "added": add_left, "changed": pairs,
                          "n_a": sum(ca.values()), "n_b": sum(cb.values())}
    if n_fail(d) == 0 and A.get("exact_sha1") and A.get("exact_sha1") != B.get("exact_sha1"):
        d["hints"].append("design.json 原文的 sha1 不同、打包结果却相同：数值在容差内浮动（只提示）")
    if d["coords"]:
        d["hints"].append(f"画布坐标变了 {len(d['coords'])} 台（差超过 {COORD_TOL} px，只提示），例 "
                          + "；".join(f"{k} {x}→{y}" for k, x, y in d["coords"][:3]))
    return d


def n_links(d: dict) -> int:
    L = d.get("links")
    return len(L["removed"]) + len(L["added"]) + len(L["changed"]) if L else 0


def n_items(d: dict) -> int:
    return sum(len(v["added"]) + len(v["removed"]) + len(v["changed"]) for v in d["items"].values())


def n_fail(d: dict) -> int:
    """不通过的处数（画布坐标、迭代轮数、sha1 只提示，不算）。"""
    return (len(d["hard"]) + len(d["wording"]) + len(d["added"]) + len(d["removed"])
            + sum(map(len, d["new_keys"].values())) + sum(map(len, d["bad_keys"].values())) + sum(map(len, d["lost_keys"].values()))
            + n_items(d) + len(d["summary"]) + len(d["scalars"]) + len(d["top"]) + (1 if d["hit_cap"] else 0) + n_links(d))


# ------------------------------------------------------------------ 实测标注：判定和判词
EMBED = ("cat", "role", "rate", "recipe", "env_required", "recipe_source", "derived_from_program_recipe", "depends_on",
         "recipe_dispute", "confidence", "note")
CAT_PROOF = {"有料": "有料", "备路": "平时不开工（备路 / 溢流位）", "平时没料": "平时没料", "时有时无": "时有时无"}


def load_truth(fx: Path) -> dict:
    """{据点: {设备键: 标注}}；没有 truth.json（别人的据点）→ {}。"""
    p = Path(fx) / TRUTH_NAME
    if not p.exists():
        return {}
    t = jload(p)
    return {k: v for k, v in t.items() if not k.startswith("_")}


def t_cat(t) -> str:
    return (t or {}).get("cat") or "未知"


def pclass(row: dict) -> str:
    """程序结果归类：rate > 0.005 → 在转；state 以「溢流位」开头 → 溢流位；其它（没料、没接上、配方未知）→ 没料。"""
    if float((row or {}).get("rate") or 0.0) > 0.005:
        return "在转"
    if str((row or {}).get("state") or "").startswith("溢流位"):
        return "溢流位"
    return "没料"


def verdict(t, row: dict) -> tuple[str, list]:
    """一台设备的程序结果对实测 → (一致 / 不一致 / 没有实测, [不一致种类])。口径照 snapplan/truth/annotate.py。"""
    c = t_cat(t)
    if c == "未知":
        return "没有实测", []
    pc, rate, tr = pclass(row), float(row.get("rate") or 0.0), t.get("rate")
    kinds = []
    if c == "有料":
        if pc != "在转":
            kinds.append("有没有料")
        elif tr is not None and abs(rate - tr) > 0.1:
            kinds.append("开工率")
    elif c == "备路":
        if pc == "在转":
            kinds.append("有没有料")
        elif pc == "没料":
            kinds.append("状态名")
    elif c == "平时没料":
        if pc == "在转":
            kinds.append("有没有料")
    elif c == "时有时无":
        if pc == "没料":
            kinds.append("有没有料")
        elif tr is not None and abs(rate - tr) > 0.1 and pc == "在转":
            kinds.append("开工率")
    rf = t.get("recipe")
    if rf and (rf not in (row.get("recipe") or "") or (t.get("env_required") and not row.get("env_required"))):
        kinds.append("配方")
    return ("不一致" if kinds else "一致"), kinds


def embed_truth(t, row: dict) -> dict | None:
    """嵌进期望快照设备行的精简实测副本（判定按这一行重算）。"""
    if not t:
        return None
    e = {f: t.get(f) for f in EMBED}
    e["user_confirmed"] = bool(t.get("user_confirmed"))
    e["verdict"], e["mismatch"] = verdict(t, row)
    return e


def _proof(t) -> str:
    c = t_cat(t)
    s = CAT_PROOF.get(c, c)
    if t.get("rate") is not None:
        s += f"、开工率 {t['rate']:.3g}"
    if t.get("recipe"):
        s += "、配方"
    return s


def _uncovered(t, fields: list) -> list:
    """变了的字段里实测管不到的。"""
    out = []
    for f in fields:
        if f == "state":
            continue
        if f == "rate" and t.get("rate") is not None:
            continue
        if f in ("recipe", "env_required") and t.get("recipe"):
            continue
        if f not in out:
            out.append(f)
    return out


def judge_change(t, old: dict, new: dict, fields: list | None = None, upstream_changed: bool = False) -> tuple[str, str]:
    """这台从 old 变成 new，对照实测 → (符号, 判词)。符号：✗ 改错了 / ✓ 改对了 / ↗ 近了 / ↘ 远了 / · 其它。"""
    fields = list(fields or [])
    if t_cat(t) == "未知":
        return "·", "要人看（没有实测）"
    vo, ko = verdict(t, old)
    vn, kn = verdict(t, new)
    po, pn = pclass(old), pclass(new)
    state_changed = scat(old) != scat(new) or abs(float(old.get("rate") or 0) - float(new.get("rate") or 0)) > TOL_RATE
    recipe_changed = (old.get("recipe") or "") != (new.get("recipe") or "")
    if t.get("recipe_dispute"):
        if state_changed:
            return "✗", "改错了（配方存疑的设备状态 / 开工率不许变）"
        if recipe_changed:
            return "·", "要人看（配方存疑：巡检面板和机身图标说法相反）"
    if t.get("depends_on") and upstream_changed:
        return "·", f"随上游变（依赖 {t['depends_on']}，那台也变了）"
    if t.get("derived_from_program_recipe") and recipe_changed:
        return "·", "要人看（实测开工率是按程序配方归到简报组推出来的，配方变了）"
    unc = _uncovered(t, fields)
    tail = f"（实测只证明{_proof(t)}" + (f"；{'、'.join(unc)} 没有实测）" if unc else "）")
    if vo == "一致" and vn == "不一致":
        return "✗", f"改错了（原来和实测一致；现在 {'/'.join(kn)} 不一致）"
    if vo == "不一致" and vn == "一致":
        if t_cat(t) in ("平时没料", "时有时无") and pn == "溢流位":
            return "·", f"变成溢流位，不算改对{tail}"
        if t_cat(t) == "备路" and set(ko) == {"状态名"}:
            return "✓", "改对了（角色按规则推定）"
        return "✓", "改对了"
    if vo == "一致" and vn == "一致":
        return "·", "仍一致" + tail
    # 仍不一致
    if t.get("role") == "主路" and pn == "溢流位" and po != "溢流位":
        return "↘", f"离实测远了（实测是主路，变成了溢流位；仍不一致 {'/'.join(kn)}）"
    if set(ko) == {"状态名"} and set(kn) == {"状态名"}:
        return "·", "仍不一致（只差状态名）" + tail
    tgt = t.get("rate")
    tgt = REP_RATE.get(t_cat(t)) if tgt is None else tgt
    ro, rn = float(old.get("rate") or 0), float(new.get("rate") or 0)
    if tgt is not None and abs(ro - rn) > TOL_RATE and abs(abs(rn - tgt) - abs(ro - tgt)) > 1e-9:
        if abs(rn - tgt) < abs(ro - tgt):
            return "↗", f"离实测近了（开工率 {ro:g}→{rn:g}，实测 {tgt:g}；仍不一致 {'/'.join(kn)}）"
        return "↘", f"离实测远了（开工率 {ro:g}→{rn:g}，实测 {tgt:g}；仍不一致 {'/'.join(kn)}）"
    if set(kn) < set(ko):
        return "↗", f"离实测近了（不一致 {'/'.join(ko)} → {'/'.join(kn)}）"
    if set(kn) > set(ko):
        return "↘", f"离实测远了（不一致 {'/'.join(ko)} → {'/'.join(kn)}）"
    return "·", f"仍不一致（{'/'.join(ko)}{'' if ko == kn else ' → ' + '/'.join(kn)}）" + tail


def truth_tag(t, row: dict) -> str:
    """设备行上的［实测…］标注（按期望里的那一行判）。"""
    if not t or t_cat(t) == "未知":
        return "［没有实测］"
    v, k = verdict(t, row)
    s = f"［实测{v}{'·' + '/'.join(k) if k else ''}：{t_cat(t)}"
    if t.get("rate") is not None:
        s += f" {t['rate']:.3g}"
    if t.get("role") == "主路":
        s += "（主路）"
    note = re.split(r"[；。]", t.get("note") or "")[0][:26]
    if note:
        s += f"；{note}"
    s += f"；可信 {t.get('confidence')}"
    if t.get("user_confirmed"):
        s += "；作者确认"
    return s + "］"


def truth_tally(rows: dict, truth: dict) -> dict:
    """一段（或几段合并）的设备行对实测 → 计数。"""
    c = Counter()
    kinds = Counter()
    for k, row in rows.items():
        t = truth.get(k)
        v, ks = verdict(t, row) if t else ("没有实测", [])
        c[v] += 1
        if v == "不一致" and set(ks) == {"状态名"}:
            c["只差状态名"] += 1
        kinds.update(ks)
    return {"一致": c["一致"], "不一致": c["不一致"], "只差状态名": c["只差状态名"], "没有实测": c["没有实测"], "kinds": dict(kinds)}


def tally_line(label: str, tl: dict) -> str:
    return f"实测对照 {label}：一致 {tl['一致']} / 不一致 {tl['不一致']}（只差状态名 {tl['只差状态名']}）/ 没有实测 {tl['没有实测']}"


# ------------------------------------------------------------------ 依据改法归并
_NUM = re.compile(r"\d+(?:\.\d+)?")


def templ(s: str) -> str:
    return _NUM.sub("#", s)


def edit_sig(a: list, b: list) -> tuple:
    """依据从 a 变成 b 的「改法」：成对替换的句子按字符级 diff 取被换掉 / 换上的片段，多出 / 少掉的句子取开头 12 个字；数字都换成 #。
    同一处措辞改动落在 25 台上 → 同一个签名 → 打印成一组。"""
    import difflib
    sig = []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        xa, xb = a[i1:i2], b[j1:j2]
        for k in range(max(len(xa), len(xb))):
            p, q = (xa[k] if k < len(xa) else None), (xb[k] if k < len(xb) else None)
            if p is None or q is None:
                sig.append(("多一句" if p is None else "少一句", templ(q or p)[:12]))
                continue
            cm = difflib.SequenceMatcher(a=p, b=q, autojunk=False)
            sig += [("改字", templ(p[c1:c2]), templ(q[d1:d2])) for o, c1, c2, d1, d2 in cm.get_opcodes() if o != "equal"]
    return tuple(sorted(set(sig)))


def _wsig(f, x, y) -> tuple:
    if f == "state":
        return tuple(("state",) + s for s in edit_sig([split_state(x)[1]], [split_state(y)[1]]))
    return tuple((f,) + s for s in edit_sig(list(x or []), list(y or [])))


def _sig_text(sig) -> str:
    parts = []
    for s in sig[:3]:
        if s[1] == "改字":
            parts.append(f"{s[0]}「{s[2]}」→「{s[3]}」")
        else:
            parts.append(f"{s[0]}{s[1]}「{s[2]}」")
    return "；".join(parts) + (f" 等 {len(sig)} 处" if len(sig) > 3 else "")


# ------------------------------------------------------------------ 期望文件
def expect_path(fx: Path, base: str) -> Path:
    return Path(fx) / EXPECT_DIR / f"{base}.json"


SAME = "同 stored"   # 期望文件里 reflow 段和 stored 段完全相同的设备行 / 段，写成这个（省一半体积），读的时候展开


def _expand(doc: dict) -> dict:
    S_, R_ = doc.get("stored") or {}, doc.get("reflow") or {}
    for k, row in list((R_.get("devices") or {}).items()):
        if row == SAME:
            R_["devices"][k] = json.loads(json.dumps(S_["devices"][k]))
    for f, v in list(R_.items()):
        if v == SAME:
            R_[f] = json.loads(json.dumps(S_[f]))
    return doc


def _fold(doc: dict) -> dict:
    doc = json.loads(json.dumps(doc))
    S_, R_ = doc["stored"], doc["reflow"]
    for k, row in R_["devices"].items():
        if row == S_["devices"].get(k):
            R_["devices"][k] = SAME
    for f in list(R_):
        if f != "devices" and f in S_ and R_[f] == S_[f]:
            R_[f] = SAME
    return doc


def dump_layered(o, depth: int = 0, maxd: int = 3) -> str:
    """期望文件的写法：键排序、不转义中文；缩进到第 3 层（文件 → 段 → devices / links / gates…），
    设备行、连线、条目各占一行（紧凑 JSON）—— 一台一行，git diff 按台看；体积比 indent=1 小一半。"""
    if depth >= maxd or not isinstance(o, (dict, list)) or not o:
        return json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ind = " " * (depth + 1)
    if isinstance(o, dict):
        body = ",\n".join(f"{ind}{json.dumps(k, ensure_ascii=False)}: {dump_layered(v, depth + 1, maxd)}" for k, v in sorted(o.items()))
        return "{\n" + body + "\n" + " " * depth + "}"
    return "[\n" + ",\n".join(ind + dump_layered(v, depth + 1, maxd) for v in o) + "\n" + " " * depth + "]"


def load_expect(fx: Path) -> dict:
    """{据点: 期望文件（「同 stored」已展开）}；没有 expect/ → {}。"""
    d = Path(fx) / EXPECT_DIR
    if not d.exists():
        return {}
    return {p.stem: _expand(jload(p)) for p in sorted(d.glob("*.json"))}


def last_changelog(fx: Path) -> dict | None:
    p = Path(fx) / EXPECT_DIR / CHANGELOG
    if not p.exists():
        return None
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def make_expect(fx: Path, base: str, packs: dict, truth: dict, reason: str) -> dict:
    """一个据点的期望文件：stored / reflow 两段，每台设备行嵌入实测副本（按这一段的这一行重判）。"""
    tb = truth.get(base) or {}
    doc = {"format": FORMAT, "base": base,
           "meta": {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "code": git_code(), "reason": reason,
                    "fixture_sha256": sha256(Path(fx) / MANIFEST), "repo_data_sha1": repo_data_sha1(), "env": env_versions()}}
    for kind in ("stored", "reflow"):
        sec = json.loads(jdump(packs[kind], None))
        for k, row in sec["devices"].items():
            e = embed_truth(tb.get(k), row)
            if e is not None:
                row["truth"] = e
        doc[kind] = sec
    return doc


def write_expect(fx: Path, base: str, doc: dict) -> Path:
    p = expect_path(fx, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_layered(_fold(doc)) + "\n", encoding="utf-8", newline="\n")
    return p


def append_changelog(fx: Path, entry: dict) -> None:
    p = Path(fx) / EXPECT_DIR / CHANGELOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def git_tracked_at_head(path: Path) -> bool | None:
    """这个文件在 git HEAD 里有没有。不在 git 仓库 / 没有 git → None。"""
    try:
        kw = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 30}
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"], **kw)
        if r.returncode or r.stdout.strip() != "true":
            return None
        relp = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
        r = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"HEAD:{relp}"], **kw)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ------------------------------------------------------------------ 跑一份样本、和期望比、出 diff
def recipe_texts() -> dict:
    """data/recipes.json 生成的配方文字 {机器名: [「A×1 + B×1 → C×1（2秒）」…]}（和 design._machine_recipes 同一写法）。
    truth.json 的 recipe 片段自检用：每个片段都必须能在它那种机器的配方文字里找到。"""
    rec = jload(ROOT / "data" / "recipes.json")
    names = jload(ROOT / "data" / "item_id_names.json")
    out = defaultdict(list)
    for r in rec:
        if not r.get("seconds"):
            continue
        ins = " + ".join(f"{names.get(k, k)}×{v:g}" for k, v in r["inputs"].items())
        outs = " + ".join(f"{names.get(k, k)}×{v:g}" for k, v in r["outputs"].items())
        out[r.get("machine_name")].append(f"{ins} → {outs}（{r['seconds']:g}秒）")
    return dict(out)


@dataclasses.dataclass
class SnapRun:
    fx: Path
    packs: dict          # {据点: {"stored": 段, "reflow": 段}}
    misses: dict         # {据点: {kind: 未命中次数}}
    secs: dict           # {据点: {kind: 秒}}
    st: OfflineState
    probs: list          # manifest 问题
    total_secs: float = 0.0
    memo_entries: list | None = None   # capture_memo=True 时：跑完以后 LG._AKE_CACHE 的条目（重录缓存用）


def run_snapshot(fx: Path, *, memo=True, live: bool = False, bases=None, log=None, capture_memo: bool = False) -> SnapRun:
    """离线跑样本每个据点的 stored 和 reflow，打包成快照段。memo / live 的意思同 offline()。
    MemoStale / IconsIncomplete 照常往外抛（测试里换成 skip / 失败）。"""
    fx = Path(fx)
    man = load_manifest(fx)
    probs = check_manifest(fx)
    packs, misses, secs = {}, {}, {}
    entries = None
    t0 = time.time()
    with offline(fx, memo=memo if not live else False, live=live) as st:
        for base in bases or man["bases"]:
            packs[base], misses[base], secs[base] = {}, {}, {}
            for kind in ("stored", "reflow"):
                m0, t = st.miss, time.time()
                r = run_case(fx, base, kind)
                misses[base][kind] = st.miss - m0
                secs[base][kind] = round(time.time() - t, 1)
                packs[base][kind] = pack_section(r["design"], r["flow"], kind)
                if log:
                    log(f"  {base} {kind}：{len(packs[base][kind]['devices'])} 台，{secs[base][kind]} 秒"
                        + (f"，缓存未命中 {misses[base][kind]} 次" if misses[base][kind] else ""))
        if capture_memo:
            entries = memo_entries_from_cache()
        stc = dataclasses.replace(st, net_ids=list(st.net_ids), miss_keys=list(st.miss_keys), stale=list(st.stale))
    return SnapRun(fx=fx, packs=packs, misses=misses, secs=secs, st=stc, probs=probs, total_secs=round(time.time() - t0, 1),
                   memo_entries=entries)


@dataclasses.dataclass
class SnapDiff:
    cases: list                     # [{base, kind, A, B, d, n, judg}]
    head: list                      # 「差异 」开头的总览（各段计数、迁移直方图、✗ 清单、--accept 要什么）
    details: dict                   # {(据点, kind): [行]}
    hints: list                     # 只提示、不算失败的
    n_total: int                    # 不通过处数合计
    changed_keys: list              # 「结论变了」的 据点·键（硬字段 / 新增 / 消失，stored 和 reflow 合并去重）→ --n
    crosses: list                   # ✗ 的 据点·键
    gated: list                     # 闸拦下的 据点·键
    required: list                  # --against-truth 必须逐个列出的 据点·键（✗ + 闸 + 消失）
    ticks: list                     # ✓ 的 据点·键
    wording_groups: int
    hist: dict                      # {(kind, 旧, 新): {...}}
    full_path: Path | None = None

    def fail_message(self, label: str) -> str:
        """AssertionError 用（≤300 字）：各类计数和前 2 台。"""
        parts = []
        for c in self.cases:
            if c["n"]:
                d = c["d"]
                parts.append(f"{c['base']}·{c['kind']} 结论 {len(d['hard'])} 措辞 {len(d['wording'])} 增减 {len(d['added'])}/{len(d['removed'])} "
                             f"其它 {c['n'] - len(d['hard']) - len(d['wording']) - len(d['added']) - len(d['removed'])}")
        ex = "、".join(self.changed_keys[:2])
        s = f"{label}：" + "；".join(parts) + (f"；例 {ex}" if ex else "") + (f"；全文 {self.full_path}" if self.full_path else "")
        return s[:300]


def _sec_head(c: dict) -> str:
    d, B = c["d"], c["B"]
    js = Counter(c["judg"][k][0] for k in d["hard"])
    L = d.get("links")
    return (f"差异 == {c['base']}·{c['kind']}：{len(B.get('devices') or {})} 台；结论变了 {len(d['hard'])} 台"
            + (f"（{' '.join(f'{s}{js[s]}' for s in SYM_ORDER if js[s])}）" if js else "")
            + f"，措辞变了 {len(d['wording'])} 台，新增 {len(d['added'])} / 消失 {len(d['removed'])}"
            + f"，行上键 {sum(map(len, d['new_keys'].values())) + sum(map(len, d['bad_keys'].values())) + sum(map(len, d['lost_keys'].values()))}"
            + f"，其它段 {n_items(d) + len(d['summary']) + len(d['scalars']) + len(d['top']) + (1 if d['hit_cap'] else 0)} 处"
            + (f"，连线 少 {len(L['removed'])} / 多 {len(L['added'])} / 变 {len(L['changed'])}（{L['n_a']}→{L['n_b']} 条）" if L else ""))


def link_text(r: dict) -> str:
    """「带子 固气转化机(670,750)[N] → 配件机(520,1023)[W]」（[边]；方向不定的用 —）。"""
    lay = LAYER.get(r.get("layer"), r.get("layer"))
    A = f"{r.get('src')}" + (f"[{r['src_side']}]" if r.get("src_side") else "")
    B = f"{r.get('dst')}" + (f"[{r['dst_side']}]" if r.get("dst_side") else "")
    if r.get("dir") == "→":
        return f"{lay} {A} → {B}"
    return f"{lay} {A} — {B}（方向{'不定' if r.get('dir') is None else ' ' + str(r.get('dir'))}）"


LINK_FIELD_CN = {"dir": "方向", "src": "起点", "dst": "终点", "src_side": "起点接口边", "dst_side": "终点接口边",
                 "src_how": "起点认法", "dst_how": "终点认法", "src_type": "起点类型", "dst_type": "终点类型"}


def section_lines(c: dict, truth_b: dict) -> list[str]:
    """一段（据点·stored / reflow）的逐台明细，都以「差异 」开头。"""
    base, kind, A, B, d, judg = c["base"], c["kind"], c["A"], c["B"], c["d"], c["judg"]
    lab = f"{base}·{kind}"
    out = []
    # 硬字段：同一「字段 旧→新」落在 ≥3 台上的归成一组，只打印一次
    grp = defaultdict(list)
    for k, chg in d["hard"].items():
        for f, x, y in chg:
            grp[(f, short(x, 70), short(y, 70))].append(k)
    big = sorted((g for g, ks in grp.items() if len(ks) >= 3), key=lambda g: (-len(grp[g]), g))
    gid = {g: i for i, g in enumerate(big, 1)}
    for g in big:
        ks = grp[g]
        out.append(f"差异 {lab} 改法#{gid[g]} {g[0]}  {g[1]} → {g[2]}：{len(ks)} 台（例 {'、'.join(ks[:3])}）")
    for k in sorted(d["hard"], key=lambda k: (SYM_ORDER.get(judg[k][0], 9), k)):
        a, b = A["devices"][k], B["devices"][k]
        sym, text = judg[k]
        refs = [f"#{gid[(f, short(x, 70), short(y, 70))]}" for f, x, y in d["hard"][k] if (f, short(x, 70), short(y, 70)) in gid]
        out.append(f"差异 {lab} {k} 画布{_rc(b.get('c'))} {truth_tag(truth_b.get(k), a)} {sym} {text}"
                   + (f"｜改法{'、'.join(refs)}" if refs else ""))
        for f, x, y in d["hard"][k]:
            if (f, short(x, 70), short(y, 70)) not in gid:
                out.append(f"差异     {f}  {short(x, 80)} → {short(y, 80)}")
    for k in d["removed"]:
        r = A["devices"][k]
        out.append(f"差异 {lab} 消失 {k} 画布{_rc(r.get('c'))}（原来 {r.get('state')} {r.get('rate')}）{truth_tag(truth_b.get(k), r)} "
                   f"✗ 设备消失（--accept 要 --against-truth）")
    for k in d["added"]:
        r = B["devices"][k]
        out.append(f"差异 {lab} 新增 {k} 画布{_rc(r.get('c'))} {r.get('state')} {r.get('rate')} {truth_tag(truth_b.get(k), r)} · 要人看")
    if d["wording"]:
        groups = defaultdict(list)
        for k, ws in d["wording"].items():
            groups[tuple(sorted(set(s for f, x, y in ws for s in _wsig(f, x, y))))].append(k)
        out.append(f"差异 {lab} 措辞变了 {len(d['wording'])} 台（其中结论也变了 {len(set(d['wording']) & set(d['hard']))} 台），"
                   f"依据改法 {len(groups)} 组")
        for sig, ks in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:12]:
            f, x, y = d["wording"][ks[0]][0]
            if f == "state":
                p, q = x, y
            else:
                xa, xb = list(x or []), list(y or [])
                p, q = next(((p, q) for p, q in zip(xa + [""] * len(xb), xb + [""] * len(xa)) if p != q), ("", ""))
            out.append(f"差异 {lab} 依据改法 ×{len(ks)} 台：{_sig_text(sig)}；例 {ks[0]}：{short(p, 60)} → {short(q, 60)}"
                       + (f"（还有 {'、'.join(ks[1:3])}{' 等' if len(ks) > 3 else ''}）" if len(ks) > 1 else ""))
    for name, lab2 in (("new_keys", "新键"), ("bad_keys", "新键出现在不该有的行上"), ("lost_keys", "少了键")):
        for f, ks in sorted(d[name].items()):
            out.append(f"差异 {lab} {lab2} {f}：{len(ks)}/{len(B.get('devices') or {})} 行，例 {'、'.join(ks[:3])}")
    for s, v in d["items"].items():
        for k, it in v["removed"]:
            out.append(f"差异 {lab} {s} 少了 {k}：{short(_strip_ids(it), 120)}")
        for k, it in v["added"]:
            out.append(f"差异 {lab} {s} 多了 {k}：{short(_strip_ids(it), 120)}")
        for k, fs in v["changed"]:
            out.append(f"差异 {lab} {s} 变了 {k}：" + "；".join(f"{f} {short(x, 60)} → {short(y, 60)}" for f, x, y in fs[:4])
                       + (f" 等 {len(fs)} 个字段" if len(fs) > 4 else ""))
    for f, x, y in d["summary"]:
        out.append(f"差异 {lab} summary.{f}  {short(x, 100)} → {short(y, 100)}")
    for f, x, y in d["scalars"]:
        out.append(f"差异 {lab} {f}  {short(x, 100)} → {short(y, 100)}")
    for what, k in d["top"]:
        out.append(f"差异 {lab} 顶层{what}键 {k}")
    if d["hit_cap"]:
        out.append(f"差异 {lab} iters.hit_cap 为真（内层跑满或外层没收敛）：{short(d['hit_cap'])}")
    L = d.get("links")
    if L:
        for r in L["removed"]:
            out.append(f"差异 {lab} 连线 {link_text(r)} 消失（认法 {r.get('src_how')} / {r.get('dst_how')}）")
        for r in L["added"]:
            out.append(f"差异 {lab} 连线 {link_text(r)} 新出现（认法 {r.get('src_how')} / {r.get('dst_how')}）")
        for r0, r1 in L["changed"]:
            fs = [f for f in LINK_FIELD_CN if r0.get(f) != r1.get(f)]
            out.append(f"差异 {lab} 连线 {link_text(r0)} 变了：" + "；".join(f"{LINK_FIELD_CN[f]} {r0.get(f)} → {r1.get(f)}" for f in fs))
    return out


def diff_fixture(name: str, run: SnapRun, expect: dict, truth: dict, *, write_full: bool = True) -> SnapDiff:
    """样本的这次结果 vs 期望 → SnapDiff（逐段比较、判词、迁移直方图、✗ 清单、--accept 要的名单、打印行）。"""
    cases = []
    for base in sorted(set(run.packs) | set(expect)):
        for kind in ("stored", "reflow"):
            A = (expect.get(base) or {}).get(kind)
            B = (run.packs.get(base) or {}).get(kind)
            if A is None or B is None:
                d = compare({"devices": {}}, {"devices": {}})
                d["top"] = [("少了", f"期望里没有 {base}·{kind}" if A is None else f"这次没算 {base}·{kind}")]
                cases.append({"base": base, "kind": kind, "A": A or {"devices": {}}, "B": B or {"devices": {}}, "d": d,
                              "n": 1, "judg": {}})
                continue
            d = compare(A, B)
            tb = truth.get(base) or {}
            judg = {}
            for k, chg in d["hard"].items():
                t = tb.get(k)
                up = bool(t and t.get("depends_on") and t["depends_on"] in d["hard"])
                judg[k] = judge_change(t, A["devices"][k], B["devices"][k], [f for f, _, _ in chg], up)
            cases.append({"base": base, "kind": kind, "A": A, "B": B, "d": d, "n": n_fail(d), "judg": judg})
    # 迁移直方图（按 kind 汇总三据点）、闸
    hist = {}
    for c in cases:
        tb = truth.get(c["base"]) or {}
        for k in c["d"]["hard"]:
            fa, fb = scat(c["A"]["devices"][k]), scat(c["B"]["devices"][k])
            if fa == fb:
                continue
            h = hist.setdefault((c["kind"], fa, fb), {"keys": [], "cats": Counter(), "ok": 0})
            t = tb.get(k)
            h["keys"].append(f"{c['base']}·{k}")
            h["cats"][t_cat(t) if t else "未知"] += 1
            if t and verdict(t, c["B"]["devices"][k])[0] == "一致":
                h["ok"] += 1
    gated = sorted({k for h in hist.values() if len(h["keys"]) >= GATE_MIN and h["ok"] < GATE_FRAC * len(h["keys"]) for k in h["keys"]})
    crosses = sorted({f"{c['base']}·{k}" for c in cases for k, j in c["judg"].items() if j[0] == "✗"})
    ticks = sorted({f"{c['base']}·{k}" for c in cases for k, j in c["judg"].items() if j[0] == "✓"})
    removed = sorted({f"{c['base']}·{k}" for c in cases for k in c["d"]["removed"]})
    changed = sorted({f"{c['base']}·{k}" for c in cases for k in list(c["d"]["hard"]) + c["d"]["added"] + c["d"]["removed"]})
    required = sorted(set(crosses) | set(gated) | set(removed))
    n_total = sum(c["n"] for c in cases)
    sigs = set()
    head, details, hints = [], {}, []
    for c in cases:
        for h in c["d"]["hints"]:
            hints.append(f"提示 {c['base']}·{c['kind']}：{h}")
        sigs |= {tuple(sorted(set(s for f, x, y in ws for s in _wsig(f, x, y)))) for ws in c["d"]["wording"].values()}
    wgroups = len(sigs)   # 依据改法组数：各段合并去重（同一处措辞改动落在几个据点、两段上只算 1 组）
    if n_total:
        head.append(f"差异 ==== {name}：不通过 {n_total} 处；结论变了 {len(changed)} 台（据点·键 去重），✗ {len(crosses)} 台，✓ {len(ticks)} 台，"
                    f"依据改法 {wgroups} 组")
        for c in cases:
            if c["n"]:
                head.append(_sec_head(c))
        for (kind, fa, fb), h in sorted(hist.items(), key=lambda kv: (kv[0][0] != "stored", -len(kv[1]["keys"]), kv[0])):
            n = len(h["keys"])
            hit = n >= GATE_MIN and h["ok"] < GATE_FRAC * n
            head.append(f"差异 迁移 {kind} {fa}→{fb} ×{n}（实测 " + "、".join(f"{k} {v}" for k, v in h["cats"].most_common())
                        + f"；迁移后和实测一致 {h['ok']} 台）" + ("  ⚠ 闸：--accept 要 --against-truth 逐个列出这些设备" if hit else ""))
        if crosses:
            head.append(f"差异 ✗ 改错了 {len(crosses)} 台：" + "、".join(crosses[:12]) + (" 等" if len(crosses) > 12 else ""))
        head.append(f"差异 --accept 要：--n {len(changed)}" + (f"；--against-truth 要逐个列出 {len(required)} 台（✗ {len(crosses)}、"
                                                                f"闸 {len(gated)}、消失 {len(removed)}）" if required else "；不用 --against-truth"))
        for c in cases:
            if c["n"]:
                details[(c["base"], c["kind"])] = section_lines(c, truth.get(c["base"]) or {})
    sd = SnapDiff(cases=cases, head=head, details=details, hints=hints, n_total=n_total, changed_keys=changed, crosses=crosses,
                  gated=gated, required=required, ticks=ticks, wording_groups=wgroups, hist=hist)
    if n_total and write_full:
        p = Path(tempfile.gettempdir()) / f"design_snap_diff_{name}_{time.strftime('%Y%m%d-%H%M%S')}.txt"
        body = head + [ln for c in cases for ln in details.get((c["base"], c["kind"]), [])] + hints
        if required:
            body.append(f"--against-truth 要逐个列出的 {len(required)} 台：" + ",".join(required))
        p.write_text("\n".join(body) + "\n", encoding="utf-8")
        sd.full_path = p
    return sd


def repo_data_changes(expect: dict) -> list[str]:
    """期望文件生成时的仓库数据哈希 vs 现在（结果变了时帮着找原因，只提示）。"""
    cur = repo_data_sha1()
    out = []
    for base, doc in expect.items():
        old = (doc.get("meta") or {}).get("repo_data_sha1") or {}
        ch = [k for k in sorted(set(old) | set(cur)) if old.get(k) != cur.get(k)]
        if ch:
            out.append(f"提示 {base}：生成期望以来仓库数据变了：{', '.join(ch)}")
    return out


# ------------------------------------------------------------------ 期望快照 vs item3f（第 2 步验收 (2)）
def expect_item3f(doc: dict) -> dict:
    """期望文件 → item3f 那种打包格式（pack_item3f 的口径）。"""
    def one(sec):
        devs = [{"device": r["device"], "c": [int(r["c"][0]), int(r["c"][1])], "state": r["state"], "rate": r["rate"],
                 "recipe": r.get("recipe"), "got": r.get("got") or {}, "need": r.get("need") or {}} for r in sec["devices"].values()]
        return {"devices": devs, "states": dict(Counter(r["state"].split("（")[0] for r in sec["devices"].values())),
                "pending": [p.get("key") for p in sec.get("pending") or []], "reread": len(sec.get("reread") or []),
                "recipe_fixes": len(sec.get("recipe_fixes") or []), "gates": len(sec.get("gates") or []),
                "ports": len(sec.get("ports") or [])}
    return {"stored": one(doc["stored"]), "reflow": one(doc["reflow"]), "links": [r["lkey"] for r in doc["reflow"].get("links") or []],
            "issues": len(doc["reflow"].get("flow_issues") or [])}


def align_c(ref: dict, cur: dict) -> int:
    """期望里 c 存 1 位小数，int(round(c,1)) 和 item3f 的 int(c) 可能差 1：在 item3f 里找不到时试 -1，找到就改成它的。→ 改了几台。"""
    n = 0
    for kind in ("stored", "reflow"):
        have = {(x["device"], tuple(x["c"])) for x in ref[kind]["devices"]}
        for x in cur[kind]["devices"]:
            if (x["device"], tuple(x["c"])) in have:
                continue
            for dx, dy in ((-1, 0), (0, -1), (-1, -1)):
                c2 = (x["c"][0] + dx, x["c"][1] + dy)
                if (x["device"], c2) in have:
                    x["c"] = list(c2)
                    n += 1
                    break
    return n


# ------------------------------------------------------------------ 命令行
def _print(*a):
    print(*a, flush=True)


def _check_offline(st: OfflineState, label: str) -> int:
    """一段离线运行之后：联网、未命中、图标目录是不是空临时目录。→ 问题数。"""
    empty_now = st.empty_dir is not None and st.empty_dir.exists() and not any(st.empty_dir.iterdir())
    is_empty_dir = st.empty_dir is not None and st.icon_dir == st.empty_dir
    _print(f"[{label}] 联网尝试 {st.net} 次，缓存未命中 {st.miss} 次；运行时 PR.ICON_CACHE 是空临时目录 {is_empty_dir and empty_now}"
           f"（memo {st.memo_state}，{st.memo_entries} 条）")
    bad = 0
    if st.net:
        _print(f"  联网尝试：{st.net_ids[:5]}")
        bad += 1
    if st.miss:
        _print(f"  未命中（{'memo 要重录' if st.icons_ok else '本机图标不齐，结果作废'}）：{st.miss_keys[:5]}")
        bad += 1
    if not (is_empty_dir and empty_now):
        bad += 1
    return bad


def cmd_expect_vs(a, fx: Path) -> int:
    """期望快照（expect/<据点>.json，不重算）对 item3f：逐台 state / rate / recipe / got / need、states、连线多重集合。"""
    expect = load_expect(fx)
    if not expect:
        _print(f"{rel(fx)} 没有 expect/")
        return 2
    ref_all = jload(Path(a.expect_vs))
    prefix = a.ref_prefix or SOURCES.get(a.name, {}).get("item3f") or a.name
    _print(f"== 期望快照对照 item3f（{prefix}·<据点>）")
    bad = 0
    tot = Counter()
    for base, doc in expect.items():
        key = f"{prefix}·{base}"
        if key not in ref_all:
            _print(f"-- {base}：item3f 里没有 {key}")
            bad += 1
            continue
        cur = expect_item3f(doc)
        moved = align_c(ref_all[key], cur)
        n, lines, stat = cmp_item3f(ref_all[key], cur)
        tot["stored"] += stat["stored"]["n"]
        tot["reflow"] += stat["reflow"]["n"]
        _print(f"-- {base}：{'0 不同' if n == 0 else f'不同 {n} 处'}（c 取整差 1 对齐 {moved} 台）")
        for s in lines:
            _print("   ", s)
        bad += n
    _print(f"合计 stored {tot['stored']} 台、reflow {tot['reflow']} 台；" + ("全部一致" if bad == 0 else f"有问题 {bad} 处"))
    return 0 if bad == 0 else 1


def cmd_verify(a) -> int:
    from bpa import design as D  # noqa: F401  （先导入，计时不算导入）
    fx = fixture_dir(a.name)
    if not (fx / MANIFEST).exists():
        _print(f"没有样本 {rel(fx)}")
        return 2
    if a.expect_vs:
        return cmd_expect_vs(a, fx)
    probs = check_manifest(fx)
    _print(f"样本 {rel(fx)}：manifest 核对 {'通过' if not probs else '有问题 ' + str(len(probs)) + ' 处'}")
    for s in probs[:20]:
        _print("  ", s)
    bad = len(probs)
    man = load_manifest(fx)
    t0 = time.time()
    try:
        with offline(fx, live=a.no_memo, memo=not a.no_memo) as st:
            _print(f"离线运行：指纹 {st.fingerprint}，memo {st.memo_state}（{st.memo_entries} 条）"
                   + (f"，现算：{st.icons_detail}" if st.live else ""))
            res = run_fixture(fx, log=_print)
            bad += _check_offline(st, "样本") if not a.no_memo else (1 if st.net else 0)
            for base in res:
                for kind in ("stored", "reflow"):
                    it = (res[base][kind]["design"].get("iters") or {})
                    if it.get("hit_cap"):
                        _print(f"  {base} {kind}：iters.hit_cap 为真")
                        bad += 1
            packs = {base: pack_item3f(r["stored"]["design"], r["reflow"]["design"], r["reflow"]["links"],
                                       r["reflow"]["flow"].get("issues")) for base, r in res.items()}
            if a.ref:
                ref_all = jload(Path(a.ref))
                prefix = a.ref_prefix or SOURCES.get(a.name, {}).get("item3f") or a.name
                _print(f"== 对照 item3f（{prefix}·<据点>）")
                for base, p in packs.items():
                    n, lines, _ = cmp_item3f(ref_all[f"{prefix}·{base}"], p)
                    _print(f"-- {base}：{'0 不同' if n == 0 else f'不同 {n} 处'}")
                    for s in lines:
                        _print("   ", s)
                    bad += n
            if a.exact_vs_project:
                bad += _exact_vs_project(a, fx, man, res, st)
            if a.placeholder_check:
                bad += _placeholder_check(a, fx, man, res, st)
            if a.dump:
                dump = {base: {"pack": packs[base],
                               "sha1": {k: sha1_bytes(jdump(res[base][k]["design"], None).encode("utf-8")) for k in ("stored", "reflow")}
                               | {"flow_reflow": sha1_bytes(jdump(res[base]["reflow"]["flow"], None).encode("utf-8"))}}
                        for base in res}
                Path(a.dump).write_text(jdump(dump), encoding="utf-8")
                _print(f"打包结果写到 {a.dump}（sha256 {sha256(Path(a.dump))[:16]}）")
    except MemoStale as e:
        _print(f"图标比对缓存不能用：{e}")
        return 2
    except IconsIncomplete as e:
        _print(str(e))
        return 2
    _print(f"用时 {time.time() - t0:.0f} 秒；" + ("全部一致" if bad == 0 else f"有问题 {bad} 处"))
    return 0 if bad == 0 else 1


def _exact_vs_project(a, fx: Path, man: dict, res: dict, st: OfflineState) -> int:
    """同一套代码、同一个离线环境，在 debug/ 下整份项目的副本（_testkit.copy_project）上跑，design.json / flow.json / 连线逐字段完全相等。"""
    from _testkit import Skip, copy_project, project_ledger
    src = SOURCES.get(a.name)
    if not src:
        _print("--exact-vs-project：不知道这个样本的来源项目")
        return 1
    try:
        proj = copy_project(Path(src["src"]).name)
    except Skip as e:
        _print(f"--exact-vs-project：{e}")
        return 1
    arch = ROOT / src["archive"]
    st.reset_counts()
    _print(f"== 整份项目副本（{src['src']}，档案 {src['archive']}）")
    bad = 0
    for base in man["bases"]:
        led = project_ledger(proj, base)
        if sha256(led) != man["files"][man["bases"][base]["ledger"]]["sha256"]:
            _print(f"  {base}：界面取的底账和样本冻结的那份内容不一样（样本取自 {man['bases'][base]['ledger_src']}）")
            bad += 1
        for kind in ("stored", "reflow"):
            t = time.time()
            p = _run_bd(proj / "base" / base, arch, base, led, kind)
            for what, A, B in (("design.json", p["design"], res[base][kind]["design"]), ("flow.json", p["flow"], res[base][kind]["flow"]),
                               ("连线", p["links"], res[base][kind]["links"])):
                n, lines = cmp_exact(A, B, f"{base} {kind} {what}")
                bad += n
                for s in lines:
                    _print("  ", s)
            _print(f"  （{base} {kind} 整份项目 {time.time() - t:.1f} 秒）")
    bad += _check_offline(st, "整份项目")
    return bad


def _placeholder_check(a, fx: Path, man: dict, res: dict, st: OfflineState) -> int:
    """样本复制一份，占位 mosaic.png 换回源项目的真图，再跑一遍，和占位图的结果逐字段完全相等。"""
    src = SOURCES.get(a.name)
    srcp = ROOT / src["src"] if src else None
    ph = {r: i for r, i in man["files"].items() if r.endswith("/mosaic.png")}
    if not srcp or not srcp.exists():
        _print("--placeholder-check：本机没有源项目，换不回真图")
        return 1
    tmp = _tmpdir("designsnap_realmosaic_")
    fx2 = tmp / fx.name
    shutil.copytree(fx, fx2, ignore=shutil.ignore_patterns("expect", "truth.json", "changelog.jsonl"))
    for r, info in ph.items():
        s = ROOT / info["src"]
        assert sha256(s) == info["src_sha256"], f"源 mosaic 变了：{info['src']}"
        shutil.copyfile(s, fx2 / r)
    _print(f"== 占位图核对：{len(ph)} 张 mosaic.png 换回真图再跑")
    st.reset_counts()
    res2 = run_fixture(fx2, log=_print)
    bad = 0
    for base in man["bases"]:
        for kind in ("stored", "reflow"):
            for what in ("design", "flow", "links"):
                n, lines = cmp_exact(res2[base][kind][what], res[base][kind][what], f"{base} {kind} {what}")
                bad += n
                if n:
                    for s in lines:
                        _print("  ", s)
    _print(f"  占位图 vs 真图：{'0 差异' if bad == 0 else f'{bad} 处不同'}")
    bad += _check_offline(st, "真图")
    shutil.rmtree(tmp, ignore_errors=True)
    return bad


def cmd_build(a) -> int:
    src = SOURCES.get(a.name) or {}
    sp = Path(a.src) if a.src else (ROOT / src["src"] if src else None)
    ar = Path(a.archive) if a.archive else (ROOT / src["archive"] if src.get("archive") else None)
    if sp is None or not sp.exists():
        _print(f"没有源项目（build-fixture 只在有 debug/ 的机器上用）：{rel(sp) if sp else a.name}")
        return 2
    out = SNAP / a.name
    had_expect = (out / "expect").exists()
    build_fixture(a.name, sp, ar, out, a.reason, log=_print)
    probs = check_manifest(out)
    _print("manifest 核对：" + ("通过" if not probs else "；".join(probs[:5])))
    if had_expect:
        _print("（已有 expect/：这里不重录 memo；之后必须 --rerecord-ake 再 --accept）")
    elif not a.no_memo:
        record_memo(out, a.reason, log=_print)
    return 0 if not probs else 1


def cmd_record(a) -> int:
    fx = fixture_dir(a.name)
    try:
        record_memo(fx, a.reason, log=_print)
    except IconsIncomplete as e:
        _print(str(e))
        return 2
    return 0


def cmd_inspect(a) -> int:
    """体积、文件数、manifest、memo、隐私 grep、拼合图和源图逐像素核对（有源项目时）。"""
    import numpy as np
    from bpa import vision as V
    fx = fixture_dir(a.name)
    files = sorted(p for p in fx.rglob("*") if p.is_file())
    tot = sum(p.stat().st_size for p in files)
    _print(f"{rel(fx)}：{len(files)} 个文件，{tot / 1e6:.2f} MB（{tot / 2 ** 20:.2f} MiB）")
    by = Counter(p.relative_to(fx).parts[0] for p in files)
    _print("  按顶层：" + "，".join(f"{k} {v}" for k, v in sorted(by.items())))
    bad = 0
    probs = check_manifest(fx)
    _print(f"manifest 核对：{'通过' if not probs else probs[:5]}（覆盖 {', '.join(covered_dirs(load_manifest(fx)))}，"
           f"列了 {len(load_manifest(fx)['files'])} 个文件）")
    bad += len(probs)
    mp = fx / MEMO_NAME
    if mp.exists():
        m = read_memo(mp)
        fp, parts = fingerprint()
        _print(f"memo：{len(m['entries'])} 条，{mp.stat().st_size / 1e6:.2f} MB，指纹 {m['fingerprint']}，"
               f"和现在{'一致' if m['fingerprint'] == fp else '不一致：' + ', '.join(stale_parts(m, parts))}")
    else:
        _print("memo：没有")
        bad += 1
    hits = []
    for p in files:
        if p.suffix.lower() in (".json", ".jsonl"):
            for mm in PRIVACY.finditer(p.read_text(encoding="utf-8")):
                hits.append(f"{p.relative_to(fx).as_posix()}: {mm.group(0)!r}")
    _print(f"隐私 grep（{PRIVACY_SHOWN}）：{len(hits)} 处" + (f"，例 {hits[:5]}" if hits else ""))
    bad += len(hits)
    man = load_manifest(fx)
    for r, info in man["files"].items():
        if r.endswith("composite.png"):
            s = ROOT / info["src"]
            if not s.exists():
                _print(f"  {r}：本机没有源图，只核 sha256")
                continue
            same = np.array_equal(V.rd(fx / r), V.rd(s))
            _print(f"  {r}：解码后和源图逐像素相同 {same}（{(fx / r).stat().st_size / 1e6:.2f} MB，源 {s.stat().st_size / 1e6:.2f} MB）")
            bad += not same
    if a.thumbs:
        out = Path(a.thumbs)
        out.mkdir(parents=True, exist_ok=True)
        import cv2
        for r in man["files"]:
            if r.endswith("composite.png"):
                im = V.rd(fx / r)
                sc = 1600 / max(im.shape[:2])
                V.wr(out / f"{Path(r).parent.name}.jpg", cv2.resize(im, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA), 85)
        _print(f"缩略图写到 {out}")
    return 0 if bad == 0 else 1


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="设计分析整份快照：样本、图标比对缓存、离线核对")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-fixture", help="从 debug/ 和档案重做样本（只重写 proj/、archive/、manifest.json）")
    b.add_argument("name")
    b.add_argument("--reason", required=True)
    b.add_argument("--src", default="")
    b.add_argument("--archive", default="")
    b.add_argument("--no-memo", action="store_true", help="不录图标比对缓存")
    r = sub.add_parser("record-memo", help="在 .cache/icons 上真算，重录 ake_memo.json.gz")
    r.add_argument("name")
    r.add_argument("--reason", required=True)
    v = sub.add_parser("verify", help="离线跑样本并核对")
    v.add_argument("name")
    v.add_argument("--ref", default="", help="item3f.json")
    v.add_argument("--expect-vs", default="", help="item3f.json：不重算，拿 expect/<据点>.json 和它逐台比")
    v.add_argument("--ref-prefix", default="", help="item3f 里的键前缀（皇帝产线 → 皇帝）")
    v.add_argument("--exact-vs-project", action="store_true")
    v.add_argument("--placeholder-check", action="store_true")
    v.add_argument("--dump", default="")
    v.add_argument("--no-memo", action="store_true", help="不装缓存，用 .cache/icons 真算（禁网，先断言图标齐全）")
    i = sub.add_parser("inspect", help="体积、文件数、manifest、memo、隐私、拼合图")
    i.add_argument("name")
    i.add_argument("--thumbs", default="", help="把拼合图缩小存到这个目录（看一眼有没有界面、UID）")
    a = ap.parse_args(argv)
    return {"build-fixture": cmd_build, "record-memo": cmd_record, "verify": cmd_verify, "inspect": cmd_inspect}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
