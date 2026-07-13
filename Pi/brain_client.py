#!/usr/bin/env python3
"""TCP brain-wave test client — connect to BrainServer and display live data.

Usage
-----
    python3 brain_client.py --host 192.168.1.100
    python3 brain_client.py --host 192.168.1.100 --raw   # print raw JSON
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="NeuroLux TCP brain-wave client"
    )
    ap.add_argument("--host", default="127.0.0.1", help="BrainServer IP")
    ap.add_argument("--port", type=int, default=9527, help="BrainServer port")
    ap.add_argument("--raw", action="store_true", help="Print raw JSON")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30)

    try:
        sock.connect((args.host, args.port))
        print(f"✅ Connected to {args.host}:{args.port}")
        print("   Waiting for brain data…  (Ctrl+C to quit)\n")
    except Exception as exc:
        print(f"❌ Cannot connect to {args.host}:{args.port} — {exc}")
        sys.exit(1)

    buf = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                print("Connection closed by server.")
                break

            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                mtype = msg.get("type", "?")

                if args.raw:
                    print(json.dumps(msg, ensure_ascii=False))
                elif mtype == "brain_state":
                    # Pretty-print brain state
                    alpha = msg.get("alpha_power", 0)
                    bar = "█" * min(int(alpha / 10), 30)
                    print(
                        f"\r🧠 Attn:{msg.get('attention',0):5.1f}  "
                        f"Med:{msg.get('meditation',0):5.1f}  "
                        f"α:{alpha:7.1f}  {bar:<30s}  "
                        f"👁 {msg.get('eyes_state','?'):>12s}  "
                        f"Sig:{msg.get('poor_signal',0):>3d}",
                        end="",
                        flush=True,
                    )
                elif mtype == "light_status":
                    print(
                        f"\n💡 {msg.get('state_label','?')}  "
                        f"bri={msg.get('brightness','?')}%  "
                        f"temp={msg.get('color_temp','?')}K"
                    )
                elif mtype == "ping":
                    # Respond with pong
                    pong = json.dumps({"type": "pong", "ts": msg.get("ts", "")})
                    sock.sendall((pong + "\n").encode())
    except KeyboardInterrupt:
        print("\n👋 Disconnected.")
    except Exception as exc:
        print(f"\n⚠️  Error: {exc}")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
