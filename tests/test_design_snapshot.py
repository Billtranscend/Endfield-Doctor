"""设计分析整份快照回归（审计改进第 5 项）：锁住皇帝产线三据点 175 台设备的整份设计分析结果（stored + reflow 端到端）。

快照是「防改坏」的基准，不是标准答案：和实测不一致的 51 台照样按现值锁住；diff 里每台都带实测标注（truth.json）和判词
（✗ 改错了 / ✓ 改对了 / ↗ 近了 / ↘ 远了 / · 仍一致 · 仍不一致 · 要人看）。有意的改动走 --accept，要用户认可改动以后再执行。

样本（tests/data/design_snap/皇帝产线/，说明见 tests/_designsnap.py 开头）：
  proj/、archive/、manifest.json   冻结的样本输入（11-13 的 flow.json、底账、档案配方表、准入口限速）
  ake_memo.json.gz                 图标比对缓存（带指纹；指纹不对一律不用）
  truth.json                       实测标注（手工维护，--accept 不改写）
  expect/<据点>.json               期望快照：stored、reflow 两段，每台设备行嵌入实测副本；expect/changelog.jsonl 每次接受追加一行
别人的据点：tests/data/private/他人_武陵城/（.gitignore 里，没得到测试者同意前不进仓库；BPA_PRIVATE_FIXTURES 可改到别处），本机没有就跳过。

跑的时候（_designsnap.offline）：图标目录指到空临时目录、禁止联网、装图标比对缓存、FL.image 换成空操作；在临时副本上跑。
  stored = 用样本里存的 flow.json 跑设计分析；reflow = 删掉 flow.json，现算物流连接再跑设计分析（同时保护追线）。

图标比对缓存的策略（amendments 第 2 节）：
  - 装之前先核指纹。不一致、本机图标齐全 → 整套改成用 .cache/icons 现算、照常比期望，test_memo 判失败「请 --rerecord-ake」；
    不一致、本机图标不齐 → 跳过（「图标比对缓存过期且本机没有完整的 .cache/icons」）。任何情况下都不拿过期的缓存跑。
  - 运行中未命中：图标齐全 → 现算、照常出 diff，这一条测试失败、标「memo 要重录」；图标不齐 → 直接失败，写明哪几条没命中。

命令行（不跑测试）：
  python tests/test_design_snapshot.py --accept "原因" --n N [--against-truth 据点·键1,据点·键2,…] [--fixture 名字]
        重算、打印 diff；N = 结论变了的台数（stored 和 reflow 合计，按 据点·键 去重）；出现 ✗ 改错了、设备消失、
        防退化的闸（同一种状态迁移 ≥5 台、迁移后和实测一致的不到一半）时，--against-truth 必须逐个列出这些设备，和实际名单不符就拒绝；
        原因至少 10 个字、不能和上一条相同；manifest 不对、hit_cap、联网、本机图标不齐时的缓存未命中，一律拒绝。
        接受以后写回 expect（嵌入的实测按新结果重判），changelog 追加一行；本机图标齐全且缓存要重录时一并重录。
        只在用户认可改动以后执行；工作流和代理不得自行接受。
  python tests/test_design_snapshot.py --init "原因" [--fixture 名字]   生成首个基线（expect 已经存在、或 git HEAD 里有这份 expect 时拒绝）
  python tests/test_design_snapshot.py --sync-truth ["原因"]            改了 truth.json 以后只刷新嵌入的实测副本；程序结果有任何差异就拒绝
  python tests/test_design_snapshot.py --show 应龙关 固气转化机@692,760   这台的期望、现在的结果和实测标注
  python tests/test_design_snapshot.py --no-memo                       不用缓存，用 .cache/icons 全量真算（约 140 秒；发布前跑一次）
  python tests/test_design_snapshot.py --rerecord-ake "原因"            要 .cache/icons：清空缓存真算、写新缓存和指纹，再用新缓存回放一遍，
                                                                        和真算逐字段相同才落盘（不和 expect 比）
  python tests/test_design_snapshot.py --audit-truth                   按 expect.stored 重判，逐台核对标注当天的判定（--init 之后核一次）
样本重做：python tests/_designsnap.py build-fixture 皇帝产线 --reason "…"（要有 debug/），之后必须 --rerecord-ake 再 --accept。
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
for _p in (str(TESTS), str(ROOT / "src" / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _designsnap as S  # noqa: E402
from _testkit import Skip, run, skip  # noqa: E402

PUBLIC = "皇帝产线"
PRIVATE_NAME = "他人_武陵城"
BASES = ("景玉谷", "应龙关", "武陵城")
SKIP_PRIVATE = f"本机没有 tests/data/private/{PRIVATE_NAME}（测试者的数据，没得到同意前不进仓库）"
CATS = ("有料", "备路", "平时没料", "时有时无", "未知")
ROLES = ("主路", "备路", "未知")


def _fixture(name: str) -> Path:
    if name == PUBLIC:
        return S.SNAP / name
    if name == PRIVATE_NAME:
        return S.PRIVATE / name
    return S.fixture_dir(name)


# ------------------------------------------------------------------ 一个进程里每份样本只算一次
_RUNS: dict = {}


class _Ctx:
    def __init__(self, name, fx, run, diff, expect, truth):
        self.name, self.fx, self.run, self.diff, self.expect, self.truth = name, fx, run, diff, expect, truth


def _get(name: str) -> _Ctx:
    if name in _RUNS:
        v = _RUNS[name]
        if isinstance(v, BaseException):
            raise v
        return v
    try:
        v = _compute(name)
    except (Skip, AssertionError) as e:
        _RUNS[name] = e
        raise
    _RUNS[name] = v
    return v


def _compute(name: str) -> _Ctx:
    fx = _fixture(name)
    expect = S.load_expect(fx)
    if not expect:
        raise AssertionError(f"{name}：没有期望快照 expect/（首个基线用 --init 生成）")
    truth = S.load_truth(fx)
    print(f"{name}：离线跑 stored + reflow …", flush=True)
    try:
        run = S.run_snapshot(fx, log=lambda s: print(s, flush=True))
    except S.MemoStale as e:
        raise Skip(f"图标比对缓存过期且本机没有完整的 .cache/icons（{e}）")
    st = run.st
    print(f"{name}：用时 {run.total_secs} 秒；图标比对缓存 {st.memo_state}（{st.memo_entries} 条）"
          + ("，用 .cache/icons 现算" if st.live else "") + f"；联网尝试 {st.net} 次，缓存未命中 {st.miss} 次", flush=True)
    diff = S.diff_fixture(name, run, expect, truth)
    for h in S.repo_data_changes(expect) + diff.hints:
        print(h)
    if diff.n_total:
        for ln in diff.head:
            print(ln)
        if diff.full_path:
            print(f"diff 全文：{diff.full_path}")
    return _Ctx(name, fx, run, diff, expect, truth)


def _check(name: str, base: str, kind: str):
    ctx = _get(name)
    st, run_ = ctx.run.st, ctx.run
    probs = []
    if run_.probs:
        probs.append(f"样本 manifest 有问题：{'；'.join(run_.probs[:3])}")
    if st.net:
        probs.append(f"联网尝试 {st.net} 次（{st.net_ids[:3]}）")
    m = run_.misses.get(base, {}).get(kind, 0)
    if m and st.memo_state == "ok":
        probs.append(f"图标比对缓存未命中 {m} 次：" + ("本机图标齐全，已用 .cache/icons 现算，memo 要重录（--rerecord-ake）"
                                               if st.live else f"本机图标不齐，结果作废（没命中：{st.miss_keys[:3]}）"))
    elif st.memo_state == "stale":
        print(f"{base}·{kind}：图标比对缓存过期（{', '.join(st.stale)}），这次用 .cache/icons 现算；test_memo 判失败")
    c = next((c for c in ctx.diff.cases if c["base"] == base and c["kind"] == kind), None)
    assert c is not None, f"期望或这次结果里没有 {base}·{kind}"
    if c["n"]:
        for ln in ctx.diff.details.get((base, kind), []):
            print(ln)
        d = c["d"]
        js = Counter(c["judg"][k][0] for k in d["hard"])
        ex = "、".join(sorted(d["hard"], key=lambda k: (S.SYM_ORDER.get(c["judg"][k][0], 9), k))[:2]) or "、".join((d["added"] + d["removed"])[:2])
        probs.insert(0, f"{base}·{kind} 不通过 {c['n']} 处：结论变了 {len(d['hard'])} 台（{' '.join(f'{s}{js[s]}' for s in S.SYM_ORDER if js[s])}），"
                        f"措辞 {len(d['wording'])} 台，新增 {len(d['added'])} / 消失 {len(d['removed'])}；例 {ex}"
                        + (f"；全文 {ctx.diff.full_path}" if ctx.diff.full_path else ""))
    assert not probs, "；".join(probs)[:300]
    B = c["B"]
    print(f"{base}·{kind}：{len(B['devices'])} 台，和期望 0 差异（{run_.secs.get(base, {}).get(kind)} 秒）"
          + (f"；连线 {len(B.get('links') or [])} 条" if kind == "reflow" else ""))


# ------------------------------------------------------------------ 测试
def test_fixture_integrity():
    """manifest 的 sha256 全对、没有多余 / 缺的文件、有档案的据点都有配方表；expect 三份都在、format=1、据点名对得上。
    仓库数据（data/recipes.json、resource/bpa/…）和生成期望时不同：只打印提示（结果变了时帮着找原因）。"""
    fx = _fixture(PUBLIC)
    probs = S.check_manifest(fx)
    man = S.load_manifest(fx)
    expect = S.load_expect(fx)
    for base in man["bases"]:
        doc = expect.get(base)
        if doc is None:
            probs.append(f"缺 expect/{base}.json（首个基线用 --init 生成）")
        elif doc.get("format") != S.FORMAT or doc.get("base") != base or not {"stored", "reflow"} <= set(doc):
            probs.append(f"expect/{base}.json 格式不对")
    for h in S.repo_data_changes(expect):
        print(h)
    print(f"样本 {PUBLIC}：manifest 列了 {len(man['files'])} 个输入文件，期望 {len(expect)} 份")
    assert not probs, "；".join(probs[:5])


def test_memo():
    """图标比对缓存：指纹一致；本机图标齐全时，按键哈希固定抽 4 条用真模板现算，逐位相同（发现 OpenCV 数值漂移、打分改了）。
    指纹不一致：图标齐全 → 失败「请 --rerecord-ake」；图标不齐 → 跳过。抽查禁网，先断言图标齐全。"""
    import numpy as np
    fx = _fixture(PUBLIC)
    m = S.read_memo(fx / S.MEMO_NAME)
    fp, parts = S.fingerprint()
    print(f"图标比对缓存 {len(m['entries'])} 条，指纹 {m['fingerprint']}，现在 {fp}")
    O = S._originals()
    with S.offline(fx, memo=False) as st:
        ok, detail = S.icons_complete(S.load_manifest(fx))
        if m.get("fingerprint") != fp:
            stale = ", ".join(S.stale_parts(m, parts)) or "总指纹不同"
            if not ok:
                skip(f"图标比对缓存过期且本机没有完整的 .cache/icons（指纹变了：{stale}；{detail}）")
            raise AssertionError(f"图标比对缓存过期（指纹变了：{stale}）：请 --rerecord-ake \"原因\"")
        if not ok:
            print(f"本机图标不齐（{detail}），不抽查现算；指纹一致")
            return
        picks = sorted(m["entries"], key=lambda e: S.sha1_bytes(e["bytes_b64"].encode("ascii") + str(e["top"]).encode()))[:4]
        bad = []
        for e in picks:
            icon = np.frombuffer(base64.b64decode(e["bytes_b64"]), dtype=np.dtype(e["dtype"])).reshape(e["shape"])
            res = O.cand(icon, e["top"])
            a = json.dumps(res, ensure_ascii=False, sort_keys=True, default=float)
            b = json.dumps(e["res"], ensure_ascii=False, sort_keys=True)
            if a != b:
                bad.append(f"{e['shape']} top {e['top']}：缓存 {b[:80]} / 现算 {a[:80]}")
        assert st.net == 0, f"抽查时联网尝试 {st.net} 次"
    print(f"抽查 4 条现算（{detail}）：{'逐位相同' if not bad else '不同 ' + str(len(bad)) + ' 条'}")
    assert not bad, "图标比对缓存和现算不同（打分改了 / 换了 OpenCV？）：请 --rerecord-ake；" + "；".join(bad[:2])


def test_comparator_selftest():
    """比较器埋坑自测：该报的要报、不该报的不报；判词和防退化的闸。"""
    import copy
    fx = _fixture(PUBLIC)
    expect = S.load_expect(fx)
    truth = S.load_truth(fx)
    assert "应龙关" in expect, "没有 expect/应龙关.json"
    A, R = expect["应龙关"]["stored"], expect["应龙关"]["reflow"]
    tb = truth.get("应龙关") or {}
    probs = []

    def cp(x):
        return copy.deepcopy(x)

    def check(label, a, b, want, pred=None):
        d = S.compare(a, b)
        n = S.n_fail(d)
        if (n > 0) != want or (pred is not None and not pred(d)):
            probs.append(f"{label}（不通过 {n} 处）")
        return d

    devs = A["devices"]
    k_run = next(k for k, r in devs.items() if r["rate"] > 0.05)
    k_full = next(k for k, r in devs.items() if r["state"] == "满速")
    check("原样", A, cp(A), False)
    check("reflow 原样", R, cp(R), False)
    B = cp(A); B["devices"][k_run]["rate"] = round(B["devices"][k_run]["rate"] + 0.01, 4)
    check("rate +0.01 要报", A, B, True, lambda d: [f for f, _, _ in d["hard"].get(k_run, [])] == ["rate"])
    B = cp(A); B["devices"][k_run]["rate"] = B["devices"][k_run]["rate"] + 0.0005
    check("rate +0.0005 不报", A, B, False)
    B = cp(A); B["devices"][k_full]["state"] = "没料"
    check("state 改了要报", A, B, True, lambda d: [f for f, _, _ in d["hard"].get(k_full, [])] == ["state"])
    B = cp(A); B["devices"][k_full]["state"] = "满速（测试说明）"
    check("state 只改括号里的说明：归措辞、不算结论变了", A, B, True, lambda d: not d["hard"] and k_full in d["wording"])
    # 一处措辞改动落在多台上：先在 5 台的依据里各加一句带不同数字的「卡在：…」，再把「卡在」改成「缺料」（不依赖期望里现在的措辞）
    A2 = cp(A)
    wk = list(A2["devices"])[:5]
    for n, k in enumerate(wk):
        A2["devices"][k]["basis"] = A2["devices"][k]["basis"] + [f"自测 卡在：测试料（能送到 {n}/30）"]
    B = cp(A2)
    for k in wk:
        B["devices"][k]["basis"] = [s.replace("自测 卡在", "自测 缺料") for s in B["devices"][k]["basis"]]
    d = check("一处依据措辞（卡在→缺料）：只报依据变了", A2, B, True, lambda d: not d["hard"] and sorted(d["wording"]) == sorted(wk))
    sig = {tuple(sorted(set(s for f, x, y in ws for s in S._wsig(f, x, y)))) for ws in d["wording"].values()}
    if len(sig) != 1:
        probs.append(f"依据措辞改法应归成 1 组（5 台），实际 {len(sig)} 组")
    k_fill = next(k for k, r in devs.items() if r["device"] == "灌装机")
    B = cp(A); B["devices"][k_fill]["channels"] = None
    check("灌装机行多了 channels: null → 不该有的行", A, B, True, lambda d: d["bad_keys"].get("channels") == [k_fill])
    B = cp(A)
    for r in B["devices"].values():
        r["new_field"] = 1
    check("所有行多一个新键：归并成一组", A, B, True, lambda d: len(d["new_keys"]) == 1 and len(d["new_keys"]["new_field"]) == len(devs))
    B = cp(A); del B["devices"][k_run]
    check("删一台 → 消失", A, B, True, lambda d: d["removed"] == [k_run])
    B = cp(A); B["summary"]["states"]["测试新状态"] = 0
    check("summary 多一个值为 0 的状态键：不报", A, B, False)
    B = cp(A); B["iters"] = dict(B.get("iters") or {}, hit_cap=True)
    check("hit_cap=True → 失败", A, B, True, lambda d: bool(d["hit_cap"]))
    B = cp(A)
    for g in B["gates"]:
        g["gate"] = g["gate"] + 1000
    for it in B["issues"]:
        for f in ("a", "b"):
            if isinstance(it.get(f), int):
                it[f] += 1000
    check("物流节点重新编号（准入口、issues）：不报", A, B, False)
    B = cp(A)
    for k in [k for k, r in B["devices"].items() if r.get("loop") is False][:4]:
        B["devices"][k]["loop"] = True      # 同一「字段 旧→新」落在 4 台上（一个常数改动）
    d = S.compare(A, B)
    lines = S.section_lines({"base": "应龙关", "kind": "stored", "A": A, "B": B, "d": d,
                             "judg": {k: S.judge_change(tb.get(k), A["devices"][k], B["devices"][k]) for k in d["hard"]}}, tb)
    if sum(1 for ln in lines if "改法#" in ln and "：4 台" in ln) != 1:
        probs.append("同一「字段 旧→新」落在 4 台上应归成 1 个改法组")
    # reflow：连线
    B = cp(R); B["links"] = B["links"][1:]
    d = check("连线少一条要报", R, B, True, lambda d: d["links"] and len(d["links"]["removed"]) == 1)
    if d["links"] and not S.link_text(d["links"]["removed"][0]).startswith(("带子 ", "管子 ")):
        probs.append("连线 diff 要打印成人话（带子 / 管子 A → B）")
    B = cp(R)
    for r in B["links"]:
        r["lkey"] = [x + 1000 if isinstance(x, int) and not isinstance(x, bool) else x for x in r["lkey"]]
    for it in B["flow_issues"] + B["structures"]:
        for f in ("a", "b", "node", "gate"):
            if isinstance(it.get(f), int) and not isinstance(it.get(f), bool):
                it[f] += 1000
    check("连线 / 物流 issues / structures 节点重新编号：不报", R, B, False)
    B = cp(R)
    i = next(i for i, r in enumerate(B["links"]) if r.get("dst_side"))
    B["links"][i]["dst_side"] = {"N": "S", "S": "N", "E": "W", "W": "E"}.get(B["links"][i]["dst_side"], "N")
    check("连线接口边变了（lkey 不变）：报「变了」", R, B, True, lambda d: d["links"] and len(d["links"]["changed"]) == 1)
    B = cp(R); B["structures"] = B["structures"][1:]
    check("structures 少一条要报", R, B, True, lambda d: "structures" in d["items"])
    # 判词（新旧两行都按需要现造，不依赖期望里现在的状态）
    idle, ovf = {"state": "没料", "rate": 0.0}, {"state": "溢流位/平时空闲", "rate": 0.0}
    k1, k2 = "固气转化机@692,760", "固气转化机@594,819"   # 都是实测备路（赫铜溢流位）
    if k1 in devs and k1 in tb:
        j = S.judge_change(tb[k1], dict(devs[k1], **idle), dict(devs[k1], **ovf), ["state"])
        if j[0] != "✓" or "角色按规则推定" not in j[1]:
            probs.append(f"{k1} 没料→溢流位 应判弱 ✓（角色按规则推定），实际 {j}")
    if k2 in devs and k2 in tb:
        j = S.judge_change(tb[k2], dict(devs[k2], **ovf), dict(devs[k2], **idle), ["state"])
        if j[0] != "✗":
            probs.append(f"{k2} 溢流位→没料 应判 ✗，实际 {j}")
    k3 = "固气转化机@653,404"   # 主路（灼铜链），实测有料
    if k3 in devs and k3 in tb:
        j = S.judge_change(tb[k3], dict(devs[k3], **idle), dict(devs[k3], **ovf), ["state"])
        if j[0] != "↘":
            probs.append(f"{k3}（主路）没料→溢流位 应判 ↘，实际 {j}")
    # 防退化的闸：零开工一律判成溢流位（评审 G1）。现造：实测有料的 8 台在「期望」里是没料、在「这次」里是溢流位，另 2 台备路同样改
    yes = [k for k, t in tb.items() if t.get("cat") == "有料" and k in devs][:8]
    bak = [k for k, t in tb.items() if t.get("cat") == "备路" and k in devs][:2]
    A3, B = cp(A), cp(A)
    for k in yes + bak:
        A3["devices"][k].update(idle)
        B["devices"][k].update(ovf)
    run_ = S.SnapRun(fx=fx, packs={"应龙关": {"stored": B, "reflow": cp(R)}}, misses={}, secs={}, st=S.OfflineState(), probs=[])
    doc3 = dict(expect["应龙关"], stored=A3)
    sd = S.diff_fixture("自测", run_, {"应龙关": doc3}, truth, write_full=False)
    if sorted(sd.gated) != sorted(f"应龙关·{k}" for k in yes + bak) or not set(sd.gated) <= set(sd.required) or sd.crosses:
        probs.append(f"零开工一律判溢流位：闸应拦下这 {len(yes + bak)} 台（gated {len(sd.gated)}、✗ {len(sd.crosses)}）")
    if not any(ln.startswith("差异 迁移 stored 没料→溢流位") and "闸" in ln for ln in sd.head):
        probs.append("迁移直方图里应有「没料→溢流位」并标闸")
    # 反过来：迁移后和实测一致的占多数（备路 8 台 + 有料 2 台），同样 ≥5 台也不拦
    bak8 = [k for k, t in tb.items() if t.get("cat") == "备路" and k in devs][:8]
    if len(bak8) >= 5:
        A4, B4 = cp(A), cp(A)
        for k in bak8 + yes[:2]:
            A4["devices"][k].update(idle)
            B4["devices"][k].update(ovf)
        run4 = S.SnapRun(fx=fx, packs={"应龙关": {"stored": B4, "reflow": cp(R)}}, misses={}, secs={}, st=S.OfflineState(), probs=[])
        sd4 = S.diff_fixture("自测", run4, {"应龙关": dict(expect["应龙关"], stored=A4)}, truth, write_full=False)
        if sd4.gated:
            probs.append(f"迁移后多数和实测一致时闸不该拦（拦了 {len(sd4.gated)} 台）")
    print(f"比较器自测：{'全部符合' if not probs else f'{len(probs)} 处不符'}（零开工判溢流位：迁移 {len(sd.gated)} 台被闸拦下）")
    assert not probs, "；".join(probs)[:300]


def test_truth_keys():
    """实测标注：truth 的键都在 expect 里（键写错就失败）；expect 里没有 truth 的只打印「没有实测」；
    嵌入副本和 truth.json 同步（按 expect 这一行重判）；recipe 片段都能在 data/recipes.json 生成的配方文字里找到；
    打印「自标注以来改对了 n 台 / 改错了 m 台」。（175/175 等于标注当天的判定只在 --audit-truth 里核）"""
    fx = _fixture(PUBLIC)
    truth, expect = S.load_truth(fx), S.load_expect(fx)
    assert expect, "没有 expect/（首个基线用 --init 生成）"
    probs = []
    for base, tb in truth.items():
        for k, t in tb.items():
            if t.get("cat") not in CATS or t.get("role") not in ROLES:
                probs.append(f"{base}·{k}：cat / role 不合法（{t.get('cat')} / {t.get('role')}）")
        doc = expect.get(base)
        if doc is None:
            probs.append(f"truth 里有 {base}，expect 里没有")
            continue
        for kind in ("stored", "reflow"):
            miss = sorted(set(tb) - set(doc[kind]["devices"]))
            if miss:
                probs.append(f"truth 的键不在 expect {base}·{kind} 里 {len(miss)} 个：{'、'.join(miss[:3])}")
    no = [f"{b}·{k}" for b, doc in expect.items() for k in doc["stored"]["devices"] if k not in (truth.get(b) or {})]
    if no:
        print(f"没有实测（expect 里有、truth 里没有）{len(no)} 台：{'、'.join(no[:5])}")
    unsync = []
    for b, doc in expect.items():
        for kind in ("stored", "reflow"):
            for k, row in doc[kind]["devices"].items():
                if row.get("truth") != S.embed_truth((truth.get(b) or {}).get(k), row):
                    unsync.append(f"{b}·{kind}·{k}")
    if unsync:
        probs.append(f"嵌入的实测副本和 truth.json 不同步 {len(unsync)} 处（例 {'、'.join(unsync[:2])}）：改了 truth.json 以后跑 --sync-truth")
    texts = S.recipe_texts()
    for b, tb in truth.items():
        for k, t in tb.items():
            if t.get("recipe") and not any(t["recipe"] in x for x in texts.get(k.split("@")[0], [])):
                probs.append(f"{b}·{k} 的 recipe 片段「{t['recipe']}」在 data/recipes.json 的{k.split('@')[0]}配方里找不到")
    fixed, broke = [], []
    tot = Counter()
    for b, tb in truth.items():
        rows = (expect.get(b) or {}).get("stored", {}).get("devices", {})
        tl = S.truth_tally(rows, tb)
        for f in ("一致", "不一致", "只差状态名", "没有实测"):
            tot[f] += tl[f]
        for k, t in tb.items():
            if k in rows:
                v = S.verdict(t, rows[k])[0]
                if t.get("verdict_at_annotation") == "不一致" and v == "一致":
                    fixed.append(f"{b}·{k}")
                if t.get("verdict_at_annotation") == "一致" and v == "不一致":
                    broke.append(f"{b}·{k}")
    print(S.tally_line("expect.stored", tot))
    print(f"自标注以来改对了 {len(fixed)} 台 / 改错了 {len(broke)} 台（按 expect.stored 重判 vs 标注当天）"
          + (f"：改对 {'、'.join(fixed[:3])}" if fixed else "") + (f"；改错 {'、'.join(broke[:3])}" if broke else ""))
    assert not probs, "；".join(probs)[:300]


def test_stored_景玉谷():
    _check(PUBLIC, "景玉谷", "stored")


def test_stored_应龙关():
    _check(PUBLIC, "应龙关", "stored")


def test_stored_武陵城():
    _check(PUBLIC, "武陵城", "stored")


def test_reflow_景玉谷():
    _check(PUBLIC, "景玉谷", "reflow")


def test_reflow_应龙关():
    _check(PUBLIC, "应龙关", "reflow")


def test_reflow_武陵城():
    _check(PUBLIC, "武陵城", "reflow")


def test_truth_report():
    """每次都打印「实测对照」：这次的结果逐台对实测（三据点合计 + 各据点）。只打印，不决定过不过。"""
    ctx = _get(PUBLIC)
    for kind in ("stored", "reflow"):
        tot = Counter()
        per = []
        for base in BASES:
            B = (ctx.run.packs.get(base) or {}).get(kind)
            if B is None:
                continue
            tl = S.truth_tally(B["devices"], ctx.truth.get(base) or {})
            for f in ("一致", "不一致", "只差状态名", "没有实测"):
                tot[f] += tl[f]
            per.append(f"{base} {tl['一致']}/{tl['不一致']}/{tl['没有实测']}")
        print(S.tally_line(kind, tot) + f"　（{'，'.join(per)}）")


def test_private_他人_武陵城():
    """别人的武陵城（本机私有样本）：stored 和 reflow，同一套比较器；没有档案，走机身图标推配方（允许写出 recipes_icon.json），
    但缓存未命中、联网都必须是 0。目录不在就跳过。"""
    fx = _fixture(PRIVATE_NAME)
    if not (fx / S.MANIFEST).exists():
        skip(SKIP_PRIVATE)
    ctx = _get(PRIVATE_NAME)
    st, run_ = ctx.run.st, ctx.run
    probs = []
    if run_.probs:
        probs.append(f"私有样本 manifest 有问题：{'；'.join(run_.probs[:3])}")
    if st.net:
        probs.append(f"联网尝试 {st.net} 次")
    if st.memo_state == "stale":
        probs.append(f"图标比对缓存过期（{', '.join(st.stale)}）：请 --rerecord-ake \"原因\" --fixture {PRIVATE_NAME}")
    elif st.miss:
        probs.append(f"图标比对缓存未命中 {st.miss} 次：" + ("已现算，memo 要重录" if st.live else f"本机图标不齐，结果作废（{st.miss_keys[:3]}）"))
    for c in ctx.diff.cases:
        if c["n"]:
            for ln in ctx.diff.details.get((c["base"], c["kind"]), []):
                print(ln)
            probs.append(f"{c['base']}·{c['kind']} 不通过 {c['n']} 处")
    for base, kinds in run_.packs.items():
        for kind, B in kinds.items():
            w = ((B.get("summary") or {}).get("warnings")) or []
            miss_pair = [n for n in B.get("notes") or [] if isinstance(n, dict) and n.get("kind") == "暗管配对缺失"]
            print(f"{PRIVATE_NAME} {base}·{kind}：{len(B['devices'])} 台，{run_.secs[base][kind]} 秒"
                  + (f"，连线 {len(B.get('links') or [])} 条" if kind == "reflow" else "")
                  + f"；summary.warnings {len(w)} 条，其中「暗管配对缺失」{sum(1 for n in miss_pair if n.get('msg') in w)} 条（现状：没有 bp/）")
    if ctx.diff.full_path:
        probs.append(f"全文 {ctx.diff.full_path}")
    assert not probs, "；".join(probs)[:300]


def test_private_ignored():
    """守卫：公开样本里没有和私有样本 sha256 相同的文件；在 git 仓库里时 tests/data/private/ 被忽略、没有被跟踪的文件。
    没有 git、或者不在仓库里（git rev-parse 不成功）→ 跳过 git 那部分。"""
    priv = _fixture(PRIVATE_NAME)
    if priv.exists():
        ph = {S.sha256(p) for p in priv.rglob("*") if p.is_file() and p.stat().st_size}
        pub = {S.sha256(p): p for p in S.SNAP.rglob("*") if p.is_file() and p.stat().st_size}
        same = [S.rel(pub[h]) for h in ph & set(pub)]
        assert not same, f"公开样本里有和私有样本相同的文件：{same[:3]}"
        print(f"公开样本 {len(pub)} 个文件、私有样本 {len(ph)} 个文件：没有相同的 sha256")
    kw = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 60, "cwd": str(ROOT)}
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], **kw)
    except (OSError, subprocess.SubprocessError):
        skip("机器上没有 git，不查 tests/data/private 的忽略状态")
    if r.returncode != 0 or r.stdout.strip() != "true":
        skip(f"不在 git 仓库里（git rev-parse 返回 {r.returncode}），不查 tests/data/private 的忽略状态")
    ci = subprocess.run(["git", "check-ignore", "-q", "tests/data/private/x"], **kw)
    ls = subprocess.run(["git", "ls-files", "tests/data/private"], **kw)
    assert ci.returncode == 0, f"tests/data/private/ 没被 .gitignore 忽略（git check-ignore 返回 {ci.returncode}）"
    assert not ls.stdout.strip(), f"tests/data/private 下有被跟踪的文件：{ls.stdout.split()[:3]}"
    print("tests/data/private/ 被忽略，没有被跟踪的文件")


TESTS_ALL = [test_fixture_integrity, test_memo, test_comparator_selftest, test_truth_keys,
             test_stored_景玉谷, test_stored_应龙关, test_stored_武陵城, test_reflow_景玉谷, test_reflow_应龙关, test_reflow_武陵城,
             test_truth_report, test_private_他人_武陵城, test_private_ignored]


# ------------------------------------------------------------------ 命令行
def _refuse(msg: str) -> int:
    print(f"拒绝：{msg}", flush=True)
    return 2


def _run_or_refuse(fx: Path, **kw):
    try:
        return S.run_snapshot(fx, log=lambda s: print(s, flush=True), **kw), None
    except S.MemoStale as e:
        return None, f"图标比对缓存不能用：{e}"
    except S.IconsIncomplete as e:
        return None, str(e)


def _run_problems(run_: S.SnapRun) -> list[str]:
    st = run_.st
    probs = []
    if run_.probs:
        probs.append(f"样本 manifest 有问题：{'；'.join(run_.probs[:3])}")
    if st.net:
        probs.append(f"联网尝试 {st.net} 次")
    if st.miss and not st.live:
        probs.append(f"缓存未命中 {st.miss} 次，本机图标不齐（{st.miss_keys[:3]}）")
    for base, kinds in run_.packs.items():
        for kind, B in kinds.items():
            if (B.get("iters") or {}).get("hit_cap"):
                probs.append(f"{base}·{kind} iters.hit_cap 为真")
    return probs


def _print_diff(sd: S.SnapDiff):
    for ln in sd.head:
        print(ln)
    for c in sd.cases:
        for ln in sd.details.get((c["base"], c["kind"]), []):
            print(ln)
    for h in sd.hints:
        print(h)
    if sd.required:
        print(f"--against-truth 要逐个列出的 {len(sd.required)} 台（✗ 改错了、设备消失、防退化的闸）：" + ",".join(sd.required))
    if sd.full_path:
        print(f"diff 全文：{sd.full_path}")


KEY_RE = re.compile(r"[^,，;；\s]+?@-?\d+,-?\d+(?:#画布-?\d+,-?\d+)?")


def _norm_keys(text: str, valid: list[str]) -> tuple[set, list]:
    """--against-truth 的名单 → 规范成「据点·键」；只写键、而且三据点里唯一时也认。→ (集合, 认不出的)。
    设备键里本身有逗号（「固气转化机@692,760」），所以按「名字@x,y」整段认，键之间用逗号、分号或空格隔开都行。"""
    out, bad = set(), []
    text = text or ""
    toks = [m.group(0) for m in KEY_RE.finditer(text)]
    rest = KEY_RE.sub(" ", text)
    bad += [x for x in re.split(r"[,，;；\s]+", rest) if x.strip()]
    for s in toks:
        if s in valid:
            out.add(s)
            continue
        cand = [v for v in valid if v.split("·", 1)[1] == s]
        if len(cand) == 1:
            out.add(cand[0])
        else:
            bad.append(s)
    return out, bad


def cmd_init(fx: Path, name: str, reason: str) -> int:
    if len((reason or "").strip()) < 10:
        return _refuse("--init 要写原因，至少 10 个字")
    man = S.load_manifest(fx)
    for base in man["bases"]:
        p = S.expect_path(fx, base)
        if p.exists():
            return _refuse(f"{S.rel(p)} 已经存在：--init 只生成首个基线，改动走 --accept")
        tracked = S.git_tracked_at_head(p)
        if tracked:
            return _refuse(f"git HEAD 里有 {S.rel(p)}：--init 只生成首个基线，改动走 --accept")
        if tracked is None and (fx / S.EXPECT_DIR).exists() and any((fx / S.EXPECT_DIR).glob("*.json")):
            return _refuse("不在 git 仓库里，而 expect/ 已经有文件：--init 只在 expect 不存在时可用")
    run_, err = _run_or_refuse(fx)
    if err:
        return _refuse(err)
    probs = _run_problems(run_)
    if run_.st.needs_rerecord:
        probs.append("图标比对缓存要重录（指纹不一致或运行中未命中）：先 --rerecord-ake")
    if probs:
        return _refuse("；".join(probs))
    truth = S.load_truth(fx)
    counts = {}
    for base in man["bases"]:
        doc = S.make_expect(fx, base, run_.packs[base], truth, reason)
        p = S.write_expect(fx, base, doc)
        counts[base] = {k: len(doc[k]["devices"]) for k in ("stored", "reflow")}
        print(f"写 {S.rel(p)}：stored {counts[base]['stored']} 台、reflow {counts[base]['reflow']} 台，"
              f"连线 {len(doc['reflow'].get('links') or [])} 条，{p.stat().st_size / 1e3:.0f} KB")
    S.append_changelog(fx, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "init", "reason": reason, "code": S.git_code(),
                            "n": sum(c["stored"] for c in counts.values()), "devices": counts,
                            "fixture_sha256": S.sha256(fx / S.MANIFEST)})
    for kind in ("stored", "reflow"):
        tot = Counter()
        for base in man["bases"]:
            tl = S.truth_tally(run_.packs[base][kind]["devices"], truth.get(base) or {})
            for f in ("一致", "不一致", "只差状态名", "没有实测"):
                tot[f] += tl[f]
        print(S.tally_line(kind, tot))
    print(f"首个基线已写（{name}），changelog 追加一行。接着跑 --audit-truth 核对标注当天的判定。")
    return 0


def cmd_accept(fx: Path, name: str, reason: str, n: int | None, against: str | None) -> int:
    reason = (reason or "").strip()
    if len(reason) < 10:
        return _refuse("原因至少 10 个字")
    last = S.last_changelog(fx)
    if last and (last.get("reason") or "").strip() == reason:
        return _refuse("原因和 changelog 上一条相同")
    expect = S.load_expect(fx)
    if not expect:
        return _refuse("没有 expect/（首个基线用 --init）")
    run_, err = _run_or_refuse(fx)
    if err:
        return _refuse(err)
    truth = S.load_truth(fx)
    sd = S.diff_fixture(name, run_, expect, truth)
    _print_diff(sd)
    probs = _run_problems(run_)
    if probs:
        return _refuse("；".join(probs))
    if not sd.n_total:
        print("和期望 0 差异，不用接受")
        return 0
    if n is None or n != len(sd.changed_keys):
        return _refuse(f"--n 必须等于结论变了的台数（stored 和 reflow 合计，按 据点·键 去重）；你给的是 {n}")
    valid = sorted({f"{c['base']}·{k}" for c in sd.cases for k in list(c["A"]["devices"]) + list(c["B"]["devices"])})
    got, bad = _norm_keys(against or "", valid)
    if bad:
        return _refuse(f"--against-truth 里有认不出的键：{bad[:5]}")
    if set(sd.required) != got:
        miss, extra = sorted(set(sd.required) - got), sorted(got - set(sd.required))
        if sd.required and against is None:
            return _refuse(f"有 ✗ 改错了 / 设备消失 / 防退化的闸拦下的设备 {len(sd.required)} 台：必须加 --against-truth 逐个列出（见上面的 diff）")
        return _refuse(f"--against-truth 名单和实际不符：少了 {len(miss)} 台、多了 {len(extra)} 台（例 少 {miss[:2]}，多 {extra[:2]}）")
    for base in run_.packs:
        S.write_expect(fx, base, S.make_expect(fx, base, run_.packs[base], truth, reason))
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "accept", "reason": reason, "code": S.git_code(), "n": len(sd.changed_keys),
             "hard": {f"{c['base']}·{c['kind']}": sorted(c["d"]["hard"]) for c in sd.cases if c["d"]["hard"]},
             "added": sorted(f"{c['base']}·{c['kind']}·{k}" for c in sd.cases for k in c["d"]["added"]),
             "removed": sorted(f"{c['base']}·{c['kind']}·{k}" for c in sd.cases for k in c["d"]["removed"]),
             "wording_groups": sd.wording_groups, "ticks": sd.ticks, "crosses": sd.crosses, "gated": sd.gated,
             "fixture_sha256": S.sha256(fx / S.MANIFEST)}
    S.append_changelog(fx, entry)
    if run_.st.needs_rerecord and run_.st.icons_ok:
        print("图标比对缓存要重录，本机图标齐全：一并重录")
        S.record_memo(fx, reason, log=lambda s: print(s, flush=True))
    print(f"已接受：写回 expect（{len(run_.packs)} 份），changelog 追加一行。改对了 {len(sd.ticks)} 台、改错了 {len(sd.crosses)} 台："
          f"请在设计文档里记一笔。")
    return 0


def cmd_sync_truth(fx: Path, name: str, reason: str) -> int:
    expect = S.load_expect(fx)
    if not expect:
        return _refuse("没有 expect/")
    run_, err = _run_or_refuse(fx)
    if err:
        return _refuse(err)
    truth = S.load_truth(fx)
    sd = S.diff_fixture(name, run_, expect, truth)
    probs = _run_problems(run_)
    if sd.n_total or probs:
        _print_diff(sd)
        return _refuse("程序结果和期望有差异（或运行有问题）：--sync-truth 只刷新实测标注，先处理差异" + (f"；{'；'.join(probs)}" if probs else ""))
    changed = 0
    dirty = []
    for base, doc in expect.items():
        tb = truth.get(base) or {}
        n0 = changed
        for kind in ("stored", "reflow"):
            for k, row in doc[kind]["devices"].items():
                e = S.embed_truth(tb.get(k), row)
                if row.get("truth") != e:
                    changed += 1
                    if e is None:
                        row.pop("truth", None)
                    else:
                        row["truth"] = e
        if changed > n0:
            dirty.append(base)
    if not changed:
        print("嵌入的实测副本已经和 truth.json 同步，不用写")
        return 0
    for base in dirty:
        S.write_expect(fx, base, expect[base])
    S.append_changelog(fx, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "sync-truth", "reason": reason, "code": S.git_code(),
                            "n": 0, "rows_changed": changed})
    print(f"嵌入的实测副本刷新了 {changed} 行")
    return 0


def cmd_show(fx: Path, base: str, key: str) -> int:
    expect = S.load_expect(fx)
    truth = (S.load_truth(fx).get(base) or {}).get(key)
    doc = expect.get(base)
    if not doc:
        return _refuse(f"没有 expect/{base}.json")
    run_, err = _run_or_refuse(fx, bases=[base])
    if err:
        return _refuse(err)
    print(f"== {base} · {key}")
    print("实测：" + (json.dumps({k: truth.get(k) for k in ("cat", "role", "rate", "recipe", "env_required", "recipe_source",
                                                       "derived_from_program_recipe", "depends_on", "recipe_dispute",
                                                       "confidence", "note")}, ensure_ascii=False) if truth else "没有"))
    for kind in ("stored", "reflow"):
        a = doc[kind]["devices"].get(key)
        b = run_.packs[base][kind]["devices"].get(key)
        print(f"-- {kind}")
        print("   期望：" + (S.jdump({k: v for k, v in a.items() if k != "truth"}, None) if a else "没有这台"))
        print("   现在：" + (S.jdump(b, None) if b else "没有这台"))
        if a and b:
            d = S.compare({"devices": {key: a}}, {"devices": {key: b}})
            chg = d["hard"].get(key, [])
            print(f"   {S.truth_tag(truth, a)} " + ("期望里的判定：" + S.jdump(a.get("truth"), None) if a.get("truth") else ""))
            if chg:
                print("   " + " ".join(S.judge_change(truth, a, b, [f for f, _, _ in chg])) + "：" + "；".join(f"{f} {S.short(x, 60)} → {S.short(y, 60)}" for f, x, y in chg))
            else:
                print("   结论没变" + ("（措辞变了）" if key in d["wording"] else ""))
    return 0


def cmd_no_memo(fx: Path, name: str) -> int:
    t = time.time()
    run_, err = _run_or_refuse(fx, live=True)
    if err:
        return _refuse(err)
    sd = S.diff_fixture(name, run_, S.load_expect(fx), S.load_truth(fx))
    _print_diff(sd)
    probs = _run_problems(run_)
    print(f"--no-memo 全量真算（{run_.st.icons_detail}）：用时 {time.time() - t:.0f} 秒，联网尝试 {run_.st.net} 次，"
          f"和期望 {'0 差异' if not sd.n_total else f'不通过 {sd.n_total} 处'}" + (f"；{'；'.join(probs)}" if probs else ""))
    return 0 if not sd.n_total and not probs else 1


def cmd_rerecord(fx: Path, name: str, reason: str) -> int:
    import shutil
    import tempfile
    if len((reason or "").strip()) < 10:
        return _refuse("原因至少 10 个字")
    t = time.time()
    live, err = _run_or_refuse(fx, live=True, capture_memo=True)
    if err:
        return _refuse(err)
    if live.st.net:
        return _refuse(f"真算时联网尝试 {live.st.net} 次")
    if len(live.memo_entries) != live.st.miss:
        return _refuse(f"缓存 {len(live.memo_entries)} 条、真算 {live.st.miss} 次，对不上（缓存中途被清过？）")
    tmp = Path(tempfile.mkdtemp(prefix="designsnap_memo_")) / S.MEMO_NAME
    S.write_memo(tmp, live.memo_entries, reason, (live.st.fingerprint, live.st.fp_parts))
    rep, err = _run_or_refuse(fx, memo=tmp)
    if err:
        return _refuse(err)
    bad = []
    if rep.st.miss or rep.st.net:
        bad.append(f"回放时未命中 {rep.st.miss} 次、联网 {rep.st.net} 次")
    for base in live.packs:
        for kind in ("stored", "reflow"):
            if S.jdump(live.packs[base][kind], None) != S.jdump(rep.packs[base][kind], None):
                n, lines = S.cmp_exact(live.packs[base][kind], rep.packs[base][kind], f"{base} {kind}")
                bad.append(f"{base}·{kind} 回放和真算不同 {n} 处")
                for s in lines[:10]:
                    print("  ", s)
    if bad:
        return _refuse("新缓存回放和不用缓存现算不一样，不落盘：" + "；".join(bad))
    shutil.copyfile(tmp, fx / S.MEMO_NAME)
    shutil.rmtree(tmp.parent, ignore_errors=True)
    print(f"重录图标比对缓存：{len(live.memo_entries)} 条，指纹 {live.st.fingerprint}；回放 vs 真算逐字段相同；用时 {time.time() - t:.0f} 秒")
    sd = S.diff_fixture(name, rep, S.load_expect(fx), S.load_truth(fx), write_full=False)
    print(f"（和期望比：{'0 差异' if not sd.n_total else f'不通过 {sd.n_total} 处，结果本来就该变时接着 --accept'}）")
    return 0


def cmd_audit(fx: Path) -> int:
    """按 expect.stored 重判，逐台核对标注当天的判定（verdict_at_annotation / mismatch_at_annotation）；另列 reflow 和 stored 判定不同的设备。"""
    expect, truth = S.load_expect(fx), S.load_truth(fx)
    if not truth:
        print("这份样本没有 truth.json")
        return 0
    bad, n, kinds = [], 0, Counter()
    for base in BASES if all(b in truth for b in BASES) else sorted(truth):
        rows = expect[base]["stored"]["devices"]
        rrows = expect[base]["reflow"]["devices"]
        tl = S.truth_tally(rows, truth[base])
        kinds.update(tl["kinds"])
        print(f"{base}：一致 {tl['一致']} / 不一致 {tl['不一致']}（只差状态名 {tl['只差状态名']}）/ 没有实测 {tl['没有实测']}")
        for k, t in sorted(truth[base].items()):
            if k not in rows:
                bad.append(f"{base}·{k} expect.stored 里没有")
                continue
            n += 1
            v, ks = S.verdict(t, rows[k])
            if v != t.get("verdict_at_annotation") or ks != t.get("mismatch_at_annotation"):
                bad.append(f"{base}·{k}：重判 {v}{ks}，标注当天 {t.get('verdict_at_annotation')}{t.get('mismatch_at_annotation')}")
            if k in rrows:
                vr, kr = S.verdict(t, rrows[k])
                if (vr, kr) != (v, ks):
                    print(f"  reflow 和 stored 判定不同：{base}·{k} stored {v}{ks} → reflow {vr}{kr}"
                          f"（{rows[k]['state']} {rows[k]['rate']} → {rrows[k]['state']} {rrows[k]['rate']}）")
    hard = sum(1 for b in truth for k, t in truth[b].items() if k in expect[b]["stored"]["devices"]
               and set(S.verdict(t, expect[b]["stored"]["devices"][k])[1]) - {"状态名"}
               and not (set(S.verdict(t, expect[b]["stored"]["devices"][k])[1]) == {"配方"} and t.get("recipe_source") == "数据侧"))
    print(f"不一致种类（一台可以多种）：{dict(kinds)}；去掉只差状态名和数据侧配方后的硬不一致 {hard} 台")
    print(f"逐台等于标注当天的判定：{n - len(bad)}/{n}")
    for s in bad[:30]:
        print("  ", s)
    return 0 if not bad else 1


def cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="设计分析整份快照：接受改动、生成基线、核对实测标注、重录图标比对缓存")
    ap.add_argument("--fixture", default=PUBLIC, help=f"样本名（默认 {PUBLIC}；私有的 {PRIVATE_NAME}）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", metavar="原因")
    g.add_argument("--init", metavar="原因")
    g.add_argument("--sync-truth", nargs="?", const="同步实测标注（truth.json 改了）", metavar="原因")
    g.add_argument("--show", nargs=2, metavar=("据点", "键"))
    g.add_argument("--no-memo", action="store_true")
    g.add_argument("--rerecord-ake", metavar="原因")
    g.add_argument("--audit-truth", action="store_true")
    ap.add_argument("--n", type=int, default=None, help="--accept：结论变了的台数")
    ap.add_argument("--against-truth", default=None, metavar="据点·键1,据点·键2,…")
    a = ap.parse_args(argv)
    fx = _fixture(a.fixture)
    if not (fx / S.MANIFEST).exists():
        return _refuse(f"没有样本 {S.rel(fx)}")
    from bpa import design as _D  # noqa: F401  （先导入，计时不算导入）
    if a.init is not None:
        return cmd_init(fx, a.fixture, a.init)
    if a.accept is not None:
        return cmd_accept(fx, a.fixture, a.accept, a.n, a.against_truth)
    if a.sync_truth is not None:
        return cmd_sync_truth(fx, a.fixture, a.sync_truth)
    if a.show:
        return cmd_show(fx, a.show[0], a.show[1])
    if a.no_memo:
        return cmd_no_memo(fx, a.fixture)
    if a.rerecord_ake is not None:
        return cmd_rerecord(fx, a.fixture, a.rerecord_ake)
    if a.audit_truth:
        return cmd_audit(fx)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    sys.exit(run(TESTS_ALL))
