"""灰度包图文说明（2026-11-10）：从上手录屏里按字幕时间抽截图，拼成一个单文件 HTML（图片内嵌，离线能看）。

用法：python build\\make_guide.py <录制目录> <输出 html> [--video <视频文件名>]
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import cv2

# (字幕里能认出这一步的关键词, 字幕后几秒抽图, 图注)
SHOTS = [
    ("打开后先看「总览」", 2.5, "总览：左下角绿点 = 连上了游戏；当前蓝图是「1.6皇帝产线」"),
    ("每个功能旁边都有「?」", 2.0, "功能旁的「?」：鼠标放上去看一句话说明，点一下跳到「帮助」页"),
    ("选「合并进", 1.5, "导入作者分析包时选「合并进『1.6皇帝产线』」"),
    ("合并好了", 1.0, "合并完成：右下角提示「已合并进：1.6皇帝产线」"),
    ("先看「排查树」", 1.0, "快速诊断结果：上面一排数字，下面是排查树，全是 ✓ 就是正常（截图是旧版；0.10.6 起最上面还有一句总结论）"),
    ("往下是缺矿", 3.0, "往下：缺矿 / 缺水口 / 缺气口，以及简报层面的疑点"),
    ("巡检中", 8.0, "深入巡检进行中：右边是进度和日志，期间别碰键鼠"),
    ("「报告」页", 2.5, "报告页：左边历史列表，右边详情"),
    ("详情：问题", 6.0, "报告详情：问题 / 待确认 / 正常分组，每条有依据和建议"),
]

CSS = """
:root{--bg:#0f1113;--panel:#1b1f23;--line:#2d3338;--text:#e6e6e3;--muted:#8a9299;--accent:#ffd400;--bad:#ff4d4d;--warn:#ffb020;--ok:#7ad67a}
@media (prefers-color-scheme: light){:root:not([data-theme=dark]){--bg:#f6f6f3;--panel:#fff;--line:#ddd;--text:#1d1f21;--muted:#5d646b;--accent:#b38f00}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.75 "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:26px;letter-spacing:2px;margin:0 0 4px}h1 small{font-size:14px;color:var(--muted);font-weight:400;margin-left:8px}
.stripe{height:6px;margin:14px 0 24px;background:repeating-linear-gradient(135deg,var(--accent) 0 10px,transparent 10px 20px);opacity:.6}
h2{font-size:19px;margin:34px 0 10px;padding-left:10px;border-left:3px solid var(--accent)}
h3{font-size:16px;margin:22px 0 6px}
.card{background:var(--panel);border:1px solid var(--line);padding:14px 18px;margin:12px 0}
.tip{border-left:3px solid var(--accent)}.warn{border-left:3px solid var(--warn)}
ol,ul{padding-left:22px}li{margin:4px 0}
figure{margin:14px 0}figure img{width:100%;border:1px solid var(--line);display:block}figcaption{color:var(--muted);font-size:13px;margin-top:6px}
code,kbd{font-family:Consolas,monospace;background:rgba(127,127,127,.15);padding:1px 6px}
table{border-collapse:collapse;width:100%}td,th{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:500}
.b{color:var(--bad)}.w{color:var(--warn)}.g{color:var(--ok)}
a{color:var(--accent)}
"""


def grab(cap, fps, t):
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
    ok, f = cap.read()
    return f if ok else None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("out")
    ap.add_argument("--video", default="上手视频.mp4")
    a = ap.parse_args(argv)
    folder = Path(a.folder)
    meta = json.loads((folder / "events.json").read_text(encoding="utf-8"))
    cap = cv2.VideoCapture(str(folder / "raw.mp4"))
    imgs = {}
    for key, dt, cap_text in SHOTS:
        ev = next((e for e in meta["events"] if key in e["text"]), None)
        if ev is None:
            continue
        f = grab(cap, meta["fps"], ev["t"] + dt)
        if f is None:
            continue
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 82])
        imgs[key] = (f"data:image/jpeg;base64,{base64.b64encode(buf.tobytes()).decode()}", cap_text)

    def fig(key):
        if key not in imgs:
            return ""
        src, c = imgs[key]
        return f'<figure><img src="{src}" alt="{c}"><figcaption>{c}</figcaption></figure>'

    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>基建诊断使用说明</title><style>{CSS}</style></head><body><main>
<h1>终末地基建诊断 · 使用说明<small>0.10.8 灰度测试版 · 1.6 皇帝产线</small></h1>
<div class="stripe"></div>
<div class="card tip">先看视频：包里的 <a href="{a.video}">{a.video}</a>（约 2 分钟，有字幕），下面是同样的步骤配图。
程序里每个功能旁边的 <b>「?」</b>（悬停看一句话，点一下看详细）和左栏最下面的<b>「帮助」</b>页，有更细的说明。</div>

<h2>它能做什么</h2>
<ul>
<li><b>快速诊断</b>（约 2 分钟）：自己按 F10 读简报，对照蓝图基准，按群里的人工排查顺序（排查树）告诉你哪一环掉了、该去哪个分区看哪类设备。</li>
<li><b>定向巡检</b>：排查树有 ✗ 时一键去那几个分区，近景看设备有没有停、管子 / 传送带和以前比少没少、断水会影响谁。</li>
<li><b>深入巡检</b>（全基地 30–40 分钟）：逐分区进俯瞰扫描、点开面板核对，和简报合起来下结论，问题设备配定位图。</li>
</ul>
<p>只看游戏画面、发键鼠，不读游戏内存、不改游戏文件。巡检时只点开面板读数，<b>不按开关、不收取、不换物品</b>。</p>

<h2>包里有什么</h2>
<table><tr><th>文件</th><th>做什么</th></tr>
<tr><td><code>EndfieldFactoryDoctor-0.10.8.zip</code></td><td>程序本体，解压到任意目录（路径里别有空格）</td></tr>
<tr><td><code>皇帝产线1.6-作者分析包.bpa.zip</code></td><td>作者的底账、配方表、准入口限速、管网拼合图。<b>不要解压</b>，在程序里导入</td></tr>
<tr><td><code>{a.video}</code></td><td>上手视频</td></tr></table>

<h2>使用前准备</h2>
<ol>
<li>基地按 1.6 皇帝产线的共享蓝图摆好（三个据点：景玉谷、应龙关、武陵城）。</li>
<li>游戏：画面要 <b>16:9</b>，简体中文，<b>不要最小化</b>，关掉 Windows 的 HDR。16:9 屏幕用「全屏」或「无边框窗口」最稳；窗口模式选比屏幕小一档的 16:9（1080p 屏选 1600×900，1440p 屏选 1920×1080，4K 屏选 2560×1440），选和屏幕一样大的会被标题栏挤扁。比例不对时程序会直接提示你的屏幕该怎么设、不操作游戏。</li>
<li>右键 <code>EndfieldFactoryDoctor.exe</code> →「以管理员身份运行」（会弹 UAC）。没有管理员权限，游戏收不到程序的按键。</li>
</ol>
{fig("打开后先看「总览」")}{fig("每个功能旁边都有「?」")}

<h2>第一次：导入作者分析包</h2>
<ol><li>左栏「蓝图」→ 右上「导入 zip」→ 选 <code>皇帝产线1.6-作者分析包.bpa.zip</code>。</li>
<li>弹窗里选 <b>「合并进『1.6皇帝产线』」</b>（不要选新建）。约半分钟，右下角提示「已合并进」就好了。</li></ol>
{fig("选「合并进")}{fig("合并好了")}
<div class="card tip">合并只做一次。之后换电脑或重装程序再导一次就行；导两次也不会重复。</div>

<h2>日常：快速诊断</h2>
<ol><li>游戏停在<b>大世界</b>（不是菜单、对话、加载中）。</li>
<li>左栏「快速诊断」→「开始快速诊断」。程序自己按 F10、翻页、读完关掉，约 2 分钟，期间别碰键鼠。</li>
<li>先看结果最上面那句<b>总结论</b>：写着「产线在正常运转」就是没事。再看<b>排查树</b>：全是 <span class="g">✓</span> 就是正常；有 <span class="b">✗</span> 的那一步会写「去哪」「看什么」，点 <b>「定向巡检这几处」</b>直接去查。</li></ol>
{fig("先看「排查树」")}{fig("往下是缺矿")}
<div class="card warn">「待确认」是简报层面的疑点，<b>平时也可能有 1–2 条</b>（比如刚断过水、刚改过线，产量还没回到基准）。排查树全 ✓ 时不用管；连着几次都在、或者排查树也报 ✗，再去看。</div>

<h2>可选：深入巡检</h2>
<p>「深入巡检」→ 勾要看的分区 →「开始深入巡检」。程序会传送、进俯瞰、逐屏扫描、点开面板，全基地约 30–40 分钟，<b>期间别碰电脑</b>。适合：快速诊断报了问题但看不出是哪台，或者大改过基地之后。</p>
{fig("巡检中")}

<h2>看报告</h2>
<p>「报告」页左边是历史列表（绿字「正常」= 没有要看的，红字 = 要看 / 问题的条数，黄字 = 待确认数），右边是详情。每条结论有<b>依据</b>和<b>建议</b>；有问题的设备配定位图，点缩略图看大图，照图去找设备（报告里的坐标是程序内部用的，别按坐标找）。</p>
{fig("「报告」页")}{fig("详情：问题")}

<h2>要知道的几件事</h2>
<ul>
<li><b>近景结构基线</b>：定向巡检第一次看到每台设备时，会把它周围的管子 / 传送带存成基线，以后拿来比「少了哪段」。所以<b>第一次定向巡检要在基地正常时跑</b>。基地改过布局后，在「深入巡检」页「结构基线」里点「重设」。</li>
<li><b>参考简报是作者基地的</b>。你的产线和作者的有出入（多摆少摆了设备）时，在「蓝图」页点「更新基准」，趁基地正常时抓一份自己的。</li>
<li>目前只针对 1.6 皇帝产线调过；别的蓝图能用，但判断没那么准。</li>
<li>自动操作游戏可能违反用户协议，风险自负。</li>
</ul>

<h2>反馈</h2>
<p>判断错了、看不懂、报错：左栏「设置」→「打开数据目录」，把出问题那次的报告 <code>app_report_快速_*.json</code>（快速诊断）或 <code>app_report_深度_*.json</code>（深度/定向巡检），连同同一时间的进度日志 <code>app_log_*.log</code>、简报 <code>brief_*.json</code>、<code>shots</code> 截图文件夹（如果有）一起发到群里，说一下游戏里实际是什么情况。传送、进俯瞰这类操作失败时，再带上 <code>maa\\debug</code> 文件夹。</p>
</main></body></html>"""
    Path(a.out).write_text(html, encoding="utf-8")
    print(a.out, len(imgs), "张图", round(len(html) / 1024 / 1024, 1), "MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
