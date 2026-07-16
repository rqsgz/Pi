#!/usr/bin/env python3
"""NeuroLux AI 脑波分析上位机 — 终端运行，DeepSeek 分析 + QQ 邮箱发送

用法
----
    cd /home/pi/brain-light
    venv/bin/python scripts/ai_reporter.py

行为
----
每 500ms 从 localhost:5000 采集一帧脑波数据，攒满 10 帧后调用 DeepSeek
进行 AI 分析，结果通过 QQ 邮箱 SMTP 发送。

配置
----
编辑 config.yaml，填入:

    reporter:
      deepseek_api_key: "sk-..."       # DeepSeek API Key
      deepseek_model: "deepseek-chat"   # 模型名
      email_to: "yourname@qq.com"       # 接收报告的 QQ 邮箱
      email_password: "授权码"           # QQ SMTP 授权码 (不是 QQ 密码!)
      batch_size: 10
      poll_interval_s: 0.5

CTRL+C 退出。
"""

from __future__ import annotations

import json
import logging
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests

# ── paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# ── logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ai_reporter")


# ═══════════════════════════════════════════════════════════════════════
#  config
# ═══════════════════════════════════════════════════════════════════════


def load_config() -> dict:
    """Load reporter section from config.yaml, falling back to defaults."""
    defaults = {
        "deepseek_api_key": "",
        "deepseek_model": "deepseek-chat",
        "email_to": "",
        "email_from": "",
        "email_password": "",
        "email_smtp_host": "smtp.qq.com",
        "email_smtp_port": 465,
        "batch_size": 10,
        "poll_interval_s": 0.5,
    }
    if not CONFIG_PATH.exists():
        return defaults

    try:
        import yaml
    except ImportError:
        log.warning("PyYAML 未安装，使用默认配置")
        return defaults

    with open(CONFIG_PATH) as fh:
        cfg = yaml.safe_load(fh) or {}

    reporter = cfg.get("reporter", {})
    if not isinstance(reporter, dict):
        return defaults

    merged = dict(defaults)
    merged.update(reporter)
    # Auto-fill email_from when to is set but from is empty
    if merged["email_to"] and not merged["email_from"]:
        merged["email_from"] = merged["email_to"]
    return merged


# ═══════════════════════════════════════════════════════════════════════
#  data collector
# ═══════════════════════════════════════════════════════════════════════


def fetch_state() -> Optional[dict]:
    """GET /api/state from the local brain-light server."""
    try:
        resp = requests.get("http://127.0.0.1:5000/api/state", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def collect_batch(batch_size: int, interval_s: float) -> list[dict]:
    """Poll until we have `batch_size` frames with valid data.

    Skips frames where alpha_power, attention, and meditation are all 0
    (TGAM disconnected or no signal).
    """
    log.info("开始采集 %d 帧脑波数据 (间隔 %.1fs)…", batch_size, interval_s)
    batch: list[dict] = []
    attempts = 0
    max_attempts = batch_size * 20  # timeout after ~10 s at 0.5 s interval

    while len(batch) < batch_size and attempts < max_attempts:
        attempts += 1
        snap = fetch_state()
        if snap is None:
            log.warning("无法连接 brain-light 服务，1 秒后重试…")
            time.sleep(1)
            continue

        # Skip empty frames (no TGAM data)
        if snap.get("alpha_power", 0) == 0 and snap.get("attention", 0) == 0:
            time.sleep(interval_s)
            continue

        frame = {
            "ts":           time.time(),
            "attention":    round(snap.get("attention", 0), 1),
            "meditation":   round(snap.get("meditation", 0), 1),
            "alpha_power":  round(snap.get("alpha_power", 0), 1),
            "eyes_state":   snap.get("eyes_state", "open"),
            "poor_signal":  snap.get("poor_signal", 0),
            "blink_count":  snap.get("blink_count", 0),
        }
        batch.append(frame)

        # Progress display
        bar = "█" * len(batch) + "░" * (batch_size - len(batch))
        print(f"\r  [{bar}] {len(batch)}/{batch_size}", end="", flush=True)
        time.sleep(interval_s)

    print()
    if len(batch) < batch_size:
        log.warning("采集超时: 只拿到 %d/%d 帧", len(batch), batch_size)

    return batch


# ═══════════════════════════════════════════════════════════════════════
#  DeepSeek AI analysis
# ═══════════════════════════════════════════════════════════════════════

ANALYSIS_PROMPT = """你是脑电波(EEG)分析专家。以下是 TGAM 脑电传感器连续 {n} 帧数据。

**α 波阈值**: <40 睁眼 | 40-55 过渡 | >55 闭眼

**数据列**: 时间 | 专注度 | 冥想度 | α功率 | 眼睛状态 | 信号质量

```
{data_table}
```

请用中文给出简洁分析(200字内):
1. 专注/冥想趋势
2. α波与眼睛状态是否一致
3. 如有异常(信号差/频繁眨眼)请提示
4. 一条简短建议

直接输出文本，不要 markdown。"""


def build_data_table(batch: list[dict]) -> str:
    """Format batch as a compact text table."""
    header = f"{'时间':>8s} | {'专注':>4s} | {'冥想':>4s} | {'α功率':>6s} | {'眼睛':^7s} | {'信号':>4s}"
    rows = []
    for f in batch:
        ts = time.strftime("%H:%M:%S", time.localtime(f["ts"]))
        rows.append(
            f"{ts:>8s} | {f['attention']:4.0f} | {f['meditation']:4.0f} "
            f"| {f['alpha_power']:6.0f} | {f['eyes_state']:^7s} | {f['poor_signal']:4d}"
        )
    return header + "\n" + "\n".join(rows)


def deepseek_analyze(batch: list[dict], api_key: str, model: str) -> Optional[str]:
    """Send batch to DeepSeek API, return analysis text."""
    data_table = build_data_table(batch)
    prompt = ANALYSIS_PROMPT.format(n=len(batch), data_table=data_table)

    log.info("发送 %d 帧到 DeepSeek (%s)…", len(batch), model)

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是专业的脑电波分析师，用中文简洁回答。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 500,
                "temperature": 0.7,
            },
            timeout=60,
        )

        if resp.status_code == 200:
            body = resp.json()
            text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            if text.strip():
                return text.strip()
            log.error("DeepSeek 返回空内容")
        else:
            log.error("DeepSeek API %d: %s", resp.status_code, resp.text[:300])
    except requests.exceptions.Timeout:
        log.error("DeepSeek API 超时 (60s)")
    except Exception as exc:
        log.error("DeepSeek 异常: %s", exc)

    return None


# ═══════════════════════════════════════════════════════════════════════
#  QQ email
# ═══════════════════════════════════════════════════════════════════════

EMAIL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"></head>
<body style="font-family:'PingFang SC','Microsoft YaHei',sans-serif;
      background:#0b1120;color:#e0e8f8;padding:24px;">
  <div style="max-width:520px;margin:0 auto;">
    <h2 style="color:#5b7eff;margin:0 0 4px;">🧠 NeuroLux · AI 脑波分析报告</h2>
    <p style="color:#7b8bb0;font-size:13px;margin:0 0 20px;">{timestamp}</p>
    <div style="background:#131c33;border:1px solid #1e3460;
         border-radius:12px;padding:14px 16px;margin-bottom:12px;">
      <p style="font-size:14px;line-height:1.7;margin:0;">
        {analysis}
      </p>
    </div>
    <div style="background:#131c33;border:1px solid #1e3460;
         border-radius:10px;padding:10px 14px;font-size:11px;color:#7b8bb0;">
      <b>数据摘要</b>: {n_frames} 帧 · α 均值 {alpha_mean:.0f} ·
      专注均值 {attn_mean:.0f} · 冥想均值 {med_mean:.0f}
    </div>
    <hr style="border-color:#1e3460;margin:20px 0 0;">
    <p style="color:#7b8bb0;font-size:11px;text-align:center;">
      NeuroLux 驭光 · AI 自动分析 · {time_str}
    </p>
  </div>
</body>
</html>"""


def send_email(cfg: dict, analysis_text: str, batch: list[dict]) -> bool:
    """Send the analysis report via QQ SMTP."""
    to_addr = cfg["email_to"]
    password = cfg["email_password"]
    from_addr = cfg["email_from"]
    host = cfg["email_smtp_host"]
    port = cfg["email_smtp_port"]

    if not to_addr or not password:
        log.warning("邮箱未配置，跳过发送")
        return False

    # Build stats
    n = len(batch)
    alphas = [f["alpha_power"] for f in batch]
    attns = [f["attention"] for f in batch]
    meds = [f["meditation"] for f in batch]

    now = time.localtime()
    now_str = time.strftime("%Y-%m-%d %H:%M", now)
    time_str = time.strftime("%H:%M:%S", now)

    html = EMAIL_HTML.format(
        timestamp=now_str,
        time_str=time_str,
        analysis=analysis_text.replace("\n", "<br>"),
        n_frames=n,
        alpha_mean=sum(alphas) / n,
        attn_mean=sum(attns) / n,
        med_mean=sum(meds) / n,
    )
    subject = f"🧠 NeuroLux 脑波分析 — {now_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        log.info("✅ 邮件已发送 → %s", to_addr)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("❌ SMTP 认证失败 — 请检查 QQ 邮箱授权码")
    except Exception as exc:
        log.error("❌ 邮件发送失败: %s", exc)
    return False


# ═══════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    cfg = load_config()

    api_key = cfg["deepseek_api_key"]
    model = cfg["deepseek_model"]
    batch_size = int(cfg["batch_size"])
    interval_s = float(cfg["poll_interval_s"])

    if not api_key:
        log.error("❌ 未配置 DeepSeek API Key")
        log.error("   请在 config.yaml 的 reporter.deepseek_api_key 填入你的 key")
        sys.exit(1)

    print("""
╔══════════════════════════════════════════════╗
║   🧠 NeuroLux AI 脑波分析上位机             ║
╠══════════════════════════════════════════════╣
║                                              ║
║   🤖 AI:   DeepSeek ({model:<20s}) ║
║   📊 批次:  {batch_size} 帧/组                           ║
║   📧 邮箱:  {email:<30s} ║
║                                              ║
║   CTRL+C 退出                                 ║
╚══════════════════════════════════════════════╝
""".format(
        model=model[:20],
        batch_size=batch_size,
        email=cfg["email_to"] or "(未配置)",
    ))

    round_num = 0

    try:
        while True:
            round_num += 1
            print(f"\n{'='*50}")
            print(f"  📊 第 {round_num} 轮 — 采集 {batch_size} 帧数据")
            print(f"{'='*50}")

            # 1. Collect batch
            batch = collect_batch(batch_size, interval_s)
            if len(batch) < 3:
                log.warning("数据太少，跳过本轮")
                continue

            # 2. AI analysis
            print("  🤖 DeepSeek 分析中…")
            analysis = deepseek_analyze(batch, api_key, model)
            if analysis is None:
                log.warning("AI 分析失败，跳过邮件发送")
                continue

            print(f"\n{'─'*50}")
            print("  📋 AI 分析结果:")
            print(f"{'─'*50}")
            # Word-wrap the analysis text for terminal display
            for line in analysis.split("\n"):
                while len(line) > 56:
                    print(f"  {line[:56]}")
                    line = line[56:]
                print(f"  {line}")
            print(f"{'─'*50}")

            # 3. Email
            if cfg["email_to"] and cfg["email_password"]:
                print("  📧 发送邮件…")
                send_email(cfg, analysis, batch)
            else:
                log.info("📧 邮箱未配置，跳过发送")

            print(f"\n  ✅ 第 {round_num} 轮完成。等待下一轮…")

    except KeyboardInterrupt:
        print(f"\n\n👋 共完成 {round_num} 轮分析，再见！")


if __name__ == "__main__":
    main()
