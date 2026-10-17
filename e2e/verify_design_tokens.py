#!/usr/bin/env python3
"""ui-rebuild · 设计 Token 静态断言（母版 mobile_final(2).html 原值对齐检查）。

检查目标：ui-rebuild/web/css/styles.css 中 CSS 变量必须等于母版原值；
同时检查 index.html 五页结构与关键产品结构存在性。不检查运行时状态，
不访问网络。用法: python3 e2e/verify_design_tokens.py [--web ui-rebuild/web]

退出码: 0 = 全部断言通过。
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 母版 Design Token 原值（README_UI_REBUILD.md + mobile_final(2).html :root）
MASTER_TOKENS = {
    "--navy": "#112a49",
    "--navy2": "#17385f",
    "--blue": "#3c72df",
    "--blue2": "#5a88e8",
    "--lime": "#c8ef63",
    "--canvas": "#edf2f7",
    "--card": "#fff",
    "--ink": "#17283d",
    "--muted": "#75849a",
    "--line": "#dce4ee",
    "--shadow": "0 14px 34px rgba(28,54,88,.10)",
}

# 母版结构断言（存在性；id 允许出现多次引用，至少一次在真实 DOM 里）
STRUCTURE_CHECKS = [
    # (名称, 正则, 期望≥次数)
    ("手机壳舞台 demo-stage", r'class="demo-stage"', 1),
    ("手机壳 phone 393px", r'class="phone"', 1),
    ("状态栏", r'class="status"', 1),
    ("深蓝 Hero", r'class="hero"', 1),
    ("TODAY'S TRAINING kicker", r"TODAY'S TRAINING", 1),
    ("白色卡片", r'class="card', 1),
    ("录音豆 bean-button", r'class="bean-button', 1),
    ("LED 指示灯 bean-led", r'bean-led', 1),
    ("蓝色主按钮 primary", r'class="primary"', 1),
    ("训练上下文 context-card", r'context-card', 1),
    ("SET 组次标签 setLabel", r'id="setLabel"', 1),
    ("live-card 波形 wave", r'id="wave"', 1),
    ("saved-result 指标", r'id="savedResult"', 1),
    ("训练事件列表 event-list", r'class="event-list"', 1),
    ("复盘大数字 summary-grid", r'summary-grid', 1),
    ("中央 Bean 底部导航 nav-center", r'nav-center', 1),
    ("五页视图 view-start/active/facts/pending/review",
     r'id="view-(start|active|facts|pending|review)"', 5),
    ("演示信息折叠区 details.demo-info", r'class="demo-info"', 1),
    ("演示语音折叠区 details.demo-panel", r'class="demo-panel"', 1),
    ("业务锚点 fitcore-demo-data", r'id="fitcore-demo-data"', 1),
    ("上传进度锚点 upload-progress", r'id="upload-progress"', 1),
    ("作业列表锚点 job-list", r'id="job-list"', 1),
    ("待确认反馈 resolve-result", r'id="resolve-result"', 1),
    ("纠错暂不可用标注", r"暂不可用", 1),
    ("viewport-guard（静态检查锚点）", r'id="viewport-guard"', 1),
]

# 禁止回归的工程调试风 Token（黑底 / 整页绿）
FORBIDDEN = ["#0d1117", "#0D1117", "#4dd0a1", "#4DD0A1"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default=str(ROOT / "web"))
    args = ap.parse_args()
    web = Path(args.web)
    css_path = web / "css" / "styles.css"
    html_path = web / "index.html"
    checks, ok = [], True

    def check(name, cond, detail=""):
        nonlocal ok
        checks.append(name)
        tag = "OK " if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{tag}] {name} {detail}")

    if not css_path.is_file():
        check("css_exists", False, f"missing {css_path}")
        sys.exit(1)
    css = css_path.read_text(encoding="utf-8")

    # 1) Token 值 = 母版原值
    for var, value in MASTER_TOKENS.items():
        m = re.search(
            r"{var}\s*:\s*([^;}}\n]+)".format(var=re.escape(var)), css)
        actual = (m.group(1).strip() if m else None)
        check(f"token_{var}", actual == value, f"actual={actual!r} expected={value!r}")

    # 2) 母版结构存在性（CSS 类名检查 css + html，DOM id 检查 html）
    html = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
    haystack = css + "\n" + html
    for name, pat, min_count in STRUCTURE_CHECKS:
        n = len(re.findall(pat, haystack))
        check(f"structure_{name}", n >= min_count, f"count={n} expected>={min_count}")

    # 3) 禁止回归工程调试风颜色（只检查声明值，跳过注释/文档说明）
    css_decls = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for bad in FORBIDDEN:
        n = len(re.findall(re.escape(bad), css_decls, re.IGNORECASE))
        check(f"forbidden_color_{bad}", n == 0, f"count={n}（工程调试风黑底/整页绿禁止回归）")

    # 4) 业务脚本保持引用（Adapters / v1client / app.js 均未脱离）
    for s in ("js/httpAdapter.js", "js/v1client.js", "js/app.js", "js/ui-adapter.js"):
        check(f"script_{s}", s in html, f"in index.html={s in html}")

    print(f"\nVERIFY_DESIGN_TOKENS: {'ALL_OK' if ok else 'HAS_FAILURES'} {len(checks)} checks")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
