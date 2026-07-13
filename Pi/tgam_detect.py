#!/usr/bin/env python3
"""TGAM 检测与诊断工具 — 扫描串口、连接 TGAM、显示实时脑电数据。

用法:
    python3 tgam_detect.py                # 自动检测
    python3 tgam_detect.py --port /dev/ttyAMA0   # 指定端口
    python3 tgam_detect.py --scan-only    # 仅扫描，不连接
    python3 tgam_detect.py --raw          # 显示原始十六进制数据

支持:
    - 有线 UART: /dev/ttyAMA0, /dev/serial1, /dev/ttyS0
    - 蓝牙 SPP:  /dev/rfcomm0
    - USB 转串口: /dev/ttyUSB*

Author: NeuroLux / buckfpga.uk
"""

from __future__ import annotations

import argparse
import glob
import os
import signal
import sys
import time
from typing import Optional

# ── 切换到项目根目录以支持相对导入 ──────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("❌ 缺少 pyserial，请运行: pip3 install pyserial")
    sys.exit(1)

from tgam.parser import ThinkGearParser

# ── 常量 ────────────────────────────────────────────────────────────────────
TGAM_BAUD = 57600
DEFAULT_TIMEOUT = 0.5   # 读取超时 (秒)

# 常见 TGAM 串口路径
COMMON_PORTS = [
    "/dev/ttyAMA0",    # Pi 硬件 UART (PL011)
    "/dev/serial1",    # → ttyAMA0
    "/dev/ttyS0",      # Pi mini UART
    "/dev/rfcomm0",    # 蓝牙 SPP
    "/dev/ttyUSB0",    # USB-TTL 模块
    "/dev/ttyUSB1",
    "/dev/ttyACM0",    # USB CDC/ACM
]

# ANSI 颜色
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
RESET = "\033[0m"
CLEAR_SCREEN = "\033[2J\033[H"


def scan_ports() -> list[str]:
    """扫描所有可能的 TGAM 串口，返回存在的端口列表。"""
    found = []

    # 1. 检查常用端口是否存在
    for port in COMMON_PORTS:
        if os.path.exists(port):
            found.append(port)

    # 2. 扫描所有 ttyUSB* 和 ttyACM*
    for pattern in ["/dev/ttyUSB*", "/dev/ttyACM*"]:
        for port in sorted(glob.glob(pattern)):
            if port not in found:
                found.append(port)

    # 3. 使用 pyserial 的 list_ports (如果有 USB 设备)
    try:
        for info in serial.tools.list_ports.comports():
            if info.device not in found:
                found.append(info.device)
    except Exception:
        pass

    return found


def probe_port(port: str) -> Optional[str]:
    """尝试在指定端口检测 TGAM 同步字节 (0xAA 0xAA)。

    返回描述字符串，或 None 表示未检测到。
    """
    try:
        ser = serial.Serial(port, baudrate=TGAM_BAUD, timeout=0.3)
    except Exception as e:
        return None

    # 尝试读取最多 512 字节，寻找 0xAA 0xAA 同步字
    buf = bytearray()
    start = time.time()
    try:
        while (time.time() - start) < 1.5 and len(buf) < 512:
            chunk = ser.read(64)
            if chunk:
                buf.extend(chunk)
                if b"\xaa\xaa" in buf:
                    idx = buf.index(b"\xaa\xaa")
                    ser.close()
                    return (
                        f"✅ TGAM 同步字 0xAA 0xAA 检测到! "
                        f"(偏移 {idx} 字节, 共读 {len(buf)} 字节)"
                    )
    except Exception:
        pass
    finally:
        try:
            ser.close()
        except Exception:
            pass

    if len(buf) > 0:
        return f"⚠️  端口可打开，但未检测到 TGAM 同步字 (读到 {len(buf)} 字节)"
    else:
        return "⚠️  端口可打开但无数据"


def build_bar(value: int, width: int = 20, max_val: int = 100) -> str:
    """绘制彩色进度条。"""
    if value is None or value < 0:
        return "─" * width
    filled = min(int(value / max_val * width), width)
    if value > 60:
        color = GREEN
    elif value > 30:
        color = YELLOW
    else:
        color = CYAN
    return f"{color}{'█' * filled}{'░' * (width - filled)}{RESET}"


def format_signal_quality(val: int) -> str:
    """格式化信号质量 (0=完美, 200=无接触)。"""
    if val == 0:
        return f"{GREEN}完美 ({val}){RESET}"
    elif val < 50:
        return f"{GREEN}良好 ({val}){RESET}"
    elif val < 100:
        return f"{YELLOW}一般 ({val}){RESET}"
    elif val < 200:
        return f"{RED}差 ({val}){RESET}  ⚠️ 请调整电极"
    else:
        return f"{RED}无接触 ({val}){RESET}  ❌"


def display_live(ser: serial.Serial, show_raw: bool = False):
    """实时显示 TGAM 数据。"""
    parser = ThinkGearParser()
    running = True

    # 统计
    frame_count = 0
    start_time = time.time()
    last_values: dict = {}

    def on_sigint(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, on_sigint)

    print(CLEAR_SCREEN, end="")
    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║          🧠 NeuroLux TGAM 实时监测                      ║{RESET}")
    print(f"{BOLD}{CYAN}║          按 Ctrl+C 退出                                  ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════╝{RESET}")
    print()

    while running:
        try:
            raw = ser.read(256)
        except serial.SerialException as e:
            print(f"\n{RED}串口错误: {e}{RESET}")
            break

        if show_raw and raw:
            print(f"{MAGENTA}[RAW] {raw.hex()}{RESET}")

        frames = parser.feed(raw) if raw else []

        for frame in frames:
            frame_count += 1

            # 更新 last_values
            if frame.poor_signal is not None:
                last_values["poor_signal"] = frame.poor_signal
            if frame.attention is not None:
                last_values["attention"] = frame.attention
            if frame.meditation is not None:
                last_values["meditation"] = frame.meditation
            if frame.raw_wave is not None:
                last_values["raw_wave"] = frame.raw_wave
            if frame.eeg_power:
                last_values["eeg_power"] = frame.eeg_power
            if not frame.checksum_ok:
                last_values["bad_chk"] = last_values.get("bad_chk", 0) + 1

        # ── 更新显示 ──────────────────────────────────────────────────
        elapsed = time.time() - start_time
        fps = frame_count / elapsed if elapsed > 0 else 0

        # 光标回到顶部
        print(f"\033[4H", end="")

        # 状态行
        print(f"{BOLD}📡 串口: {ser.port} | 波特率: {TGAM_BAUD} | "
              f"运行: {elapsed:.0f}s | 帧数: {frame_count} | "
              f"速率: {fps:.1f} fps{RESET}    ")
        print()

        # 信号质量
        sq = last_values.get("poor_signal", "?")
        if isinstance(sq, int):
            print(f"  🔌 信号质量: {format_signal_quality(sq)}")
        else:
            print(f"  🔌 信号质量: {YELLOW}等待数据...{RESET}")

        print()

        # 注意力 / 放松度
        att = last_values.get("attention")
        med = last_values.get("meditation")
        att_str = f"{att:3d} {build_bar(att)}" if att is not None else f"{'?':>3} {'─'*20}"
        med_str = f"{med:3d} {build_bar(med)}" if med is not None else f"{'?':>3} {'─'*20}"
        print(f"  🎯 注意力 (Attention):   {att_str}")
        print(f"  🧘 放松度 (Meditation):  {med_str}")
        print()

        # EEG 频段
        eeg = last_values.get("eeg_power", {})
        if eeg:
            print(f"  {BOLD}📊 EEG 频段功率:{RESET}")
            bands = [
                ("δ  Delta",       "delta",      CYAN),
                ("θ  Theta",       "theta",      CYAN),
                ("α  Low-Alpha",   "low_alpha",  GREEN),
                ("α  High-Alpha",  "high_alpha", GREEN),
                ("β  Low-Beta",    "low_beta",   YELLOW),
                ("β  High-Beta",   "high_beta",  YELLOW),
                ("γ  Low-Gamma",   "low_gamma",  MAGENTA),
                ("γ  Mid-Gamma",   "mid_gamma",  MAGENTA),
            ]
            for label, key, color in bands:
                val = eeg.get(key, 0)
                # 对数刻度条
                if val > 0:
                    import math
                    bar_w = max(0, min(20, int(math.log10(val + 1) * 4)))
                else:
                    bar_w = 0
                bar = f"{color}{'█' * bar_w}{'░' * (20 - bar_w)}{RESET}"
                print(f"     {label:<14s} {val:>10,d}  {bar}")
        else:
            print(f"  {BOLD}📊 EEG 频段功率: {YELLOW}等待数据...{RESET}")
            print()

        # 校验和统计
        stats = parser.stats
        if stats["total_packets"] > 0:
            pass_rate = stats["checksum_pass_rate"] * 100
            chk_color = GREEN if pass_rate > 95 else (YELLOW if pass_rate > 80 else RED)
            print(f"\n  ✅ 校验通过率: {chk_color}{pass_rate:.1f}%{RESET} "
                  f"({stats['total_packets'] - stats['bad_checksum']}/{stats['total_packets']})")

        # 每 0.1 秒刷新一次
        time.sleep(0.1)

    ser.close()
    print(f"\n\n{BOLD}👋 已断开 TGAM。共接收 {frame_count} 帧。{RESET}")


def main():
    parser = argparse.ArgumentParser(
        description="TGAM (ThinkGear AM) 检测与诊断工具 — NeuroLux"
    )
    parser.add_argument(
        "--port", "-p",
        help=f"指定串口路径 (默认自动检测)",
    )
    parser.add_argument(
        "--scan-only", "-s",
        action="store_true",
        help="仅扫描可用串口，不连接",
    )
    parser.add_argument(
        "--raw", "-r",
        action="store_true",
        help="同时显示原始十六进制数据",
    )
    parser.add_argument(
        "--baud", "-b",
        type=int,
        default=TGAM_BAUD,
        help=f"波特率 (默认 {TGAM_BAUD})",
    )
    args = parser.parse_args()

    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║       🧠 TGAM 检测工具 v2.0 — NeuroLux 驭光             ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════╝{RESET}")
    print()

    # ── 扫描阶段 ──────────────────────────────────────────────────────
    print(f"{BOLD}🔍 正在扫描串口...{RESET}")
    ports = scan_ports()

    if not ports:
        print(f"{RED}❌ 未找到任何可用串口！{RESET}")
        print()
        print("请检查:")
        print("  1. TGAM 模块是否正确连接到 Pi 的 GPIO 引脚?")
        print("     TGAM TX → Pi RX (GPIO15 / pin 10)")
        print("     TGAM GND → Pi GND (pin 6)")
        print("     TGAM VCC → Pi 3.3V (pin 1)")
        print("  2. 是否启用了 Pi 的硬件 UART?")
        print("     运行: sudo raspi-config → Interface Options → Serial Port")
        print("     - 关闭串口登录 shell (NO)")
        print("     - 启用串口硬件 (YES)")
        print("  3. 如果是蓝牙 TGAM, 是否已配对并绑定 rfcomm?")
        return 1

    print(f"  找到 {len(ports)} 个端口:")
    for port in ports:
        desc = ""
        if os.path.islink(port):
            desc = f" → {os.readlink(port)}"
        print(f"    📍 {port}{desc}")
    print()

    if args.scan_only:
        print(f"{BOLD}🔬 正在探测 TGAM 信号...{RESET}")
        for port in ports:
            result = probe_port(port)
            status = result if result else f"{RED}❌ 无法打开{RESET}"
            print(f"  {port}: {status}")
        print()
        return 0

    # ── 连接阶段 ──────────────────────────────────────────────────────
    target = args.port
    if not target:
        # 自动选择：优先 ttyAMA0/serial1，然后 rfcomm0
        for preferred in ["/dev/ttyAMA0", "/dev/serial1"]:
            if preferred in ports:
                target = preferred
                break
        if not target:
            target = ports[0]

    print(f"{BOLD}🔗 连接到: {target} @ {args.baud} bps{RESET}")

    try:
        ser = serial.Serial(target, baudrate=args.baud, timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        print(f"{RED}❌ 无法打开串口 {target}: {e}{RESET}")
        print()
        print("权限问题？请检查:")
        print(f"  $ groups   (确保包含 'dialout')")
        print(f"  $ ls -la {target}")
        return 1

    print(f"{GREEN}✅ 串口已打开!{RESET}")
    print()
    print(f"{YELLOW}⏳ 等待 TGAM 数据... (需 TGAM 上电并连接电极){RESET}")
    print(f"{YELLOW}   如果长时间无数据，请检查:{RESET}")
    print(f"{YELLOW}   1. TGAM 供电是否正常 (3.3V)?{RESET}")
    print(f"{YELLOW}   2. TX/RX 接线是否正确?{RESET}")
    print(f"{YELLOW}   3. 波特率是否匹配 (默认 57600)?{RESET}")
    print()

    # ── 等待第一个同步字 ──────────────────────────────────────────────
    buf = bytearray()
    sync_found = False
    wait_start = time.time()
    print("  正在扫描同步字节 0xAA 0xAA...", end="", flush=True)

    while (time.time() - wait_start) < 5.0:
        chunk = ser.read(64)
        if chunk:
            buf.extend(chunk)
            if b"\xaa\xaa" in buf:
                sync_found = True
                # 将缓冲的未读数据喂回 (模拟)
                break
        print(".", end="", flush=True)

    print()

    if sync_found:
        idx = buf.index(b"\xaa\xaa")
        print(f"{GREEN}✅ 检测到 TGAM 同步字节! (偏移 {idx}, 缓冲 {len(buf)} 字节){RESET}")
        # 把缓冲数据喂给 parser
    else:
        print(f"{YELLOW}⚠️  未在缓冲中找到同步字节，但仍尝试解析...{RESET}")
        print(f"{YELLOW}   已缓冲 {len(buf)} 字节数据{RESET}")

    print()
    input(f"{BOLD}按 Enter 开始实时监测...{RESET}")
    print()

    # ── 把已缓冲的数据预喂给 display_live ────────────────────────────
    # display_live 会从头开始读，所以这里重新打开串口
    ser.close()
    try:
        ser = serial.Serial(target, baudrate=args.baud, timeout=DEFAULT_TIMEOUT)
    except Exception:
        pass

    display_live(ser, show_raw=args.raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
