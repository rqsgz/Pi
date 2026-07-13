#!/usr/bin/env python3
"""TGAM raw data reader — quick verification tool.

Uses native Bluetooth socket (not rfcomm) as configured in config.yaml.
Also works with the BTSocketStream class from main.py if available,
otherwise falls back to plain socket connect.

Usage:  python3 tgam_read.py
        python3 tgam_read.py bt:04:22:12:02:0D:C0:1
"""
import socket
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_bt_port(port_str: str) -> tuple[str, int]:
    """Parse 'bt:MAC:channel' into (mac, channel)."""
    raw = port_str[3:]
    parts = raw.split(":")
    if len(parts) >= 7 and parts[-1].isdigit():
        mac = ":".join(parts[:6])
        channel = int(parts[-1])
    else:
        mac = ":".join(parts[:6])
        channel = 1
    return mac, channel


def main():
    # Determine port — CLI arg > config default
    if len(sys.argv) > 1:
        port_str = sys.argv[1]
    else:
        port_str = "bt:04:22:12:02:0D:C0:1"

    if not port_str.startswith("bt:"):
        print("Usage: python3 tgam_read.py bt:<MAC>:<channel>")
        print("Example: python3 tgam_read.py bt:04:22:12:02:0D:C0:1")
        sys.exit(1)

    mac, channel = parse_bt_port(port_str)
    print(f"Connecting to TGAM {mac} channel {channel}...")

    sock = None
    try:
        sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
        )
        sock.settimeout(3)
        sock.connect((mac, channel))
        print("✅ Connected! Reading brain data...\n")

        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(256)
                if chunk:
                    buf.extend(chunk)
                    # Print hex in 16-byte rows
                    while len(buf) >= 16:
                        line = buf[:16]
                        hex_str = " ".join(f"{b:02X}" for b in line)
                        # Highlight AA AA sync markers
                        print(hex_str)
                        del buf[:16]
            except BlockingIOError:
                time.sleep(0.01)
            except socket.timeout:
                pass

    except socket.error as e:
        print(f"❌ Bluetooth socket error: {e}")
        print("  Make sure TGAM is powered on and paired (ACL connected)")
    except KeyboardInterrupt:
        print("\n程序退出")
    finally:
        if sock:
            sock.close()


if __name__ == "__main__":
    main()
