# 在不同网络中用 Tailscale 控制 WiZ 灯 — 说明与实用指南

本 README 汇总了如何在树莓派与 WiZ 灯泡不在同一物理网络（例如树莓派连手机热点、灯泡连家中 Wi‑Fi）的情况下，通过 Tailscale 实现远程局域网控制的原理、常见拓扑、配置步骤、排查方法与安全建议，并给出可直接使用的示例命令与代理脚本。

---

## 结论摘要（一句话）
通过在灯泡所在局域网中运行一个 Tailscale 节点并启用子网路由（advertise‑routes），可以把家中局域网的设备在 Tailscale 上“暴露”出来，外部的树莓派通过 Tailscale 路由直接访问灯泡的私有 IP，从而实现本地协议的远程控制；如果不能或不想做子网路由，则可在家中运行一个“本地代理”，由代理在局域网内用本地发现/控制协议与灯泡交互，远端通过 Tailscale 调用代理接口。

---

## 1. 核心概念
- Tailscale 是基于 WireGuard 的点对点 VPN（overlay network）：每台设备获得一个虚拟 IP（通常 100.x.x.x）。
- 控制平面负责认证与密钥分发；数据优先点对点（NAT 穿透），穿透失败时走中继（DERP），流量始终端到端加密。
- 子网路由（subnet routes）：在家中某台设备上使用 `--advertise-routes=<LAN_CIDR>` 将该 LAN 广告到 Tailscale 管理面，管理员在控制台批准后，其他 Tailscale 节点可通过该广告节点访问对应的私有 IP。
- 局域网设备发现（如 mDNS / UDP 广播）默认不会穿越子网路由，需要代理或 mDNS 转发才能跨网发现。

---

## 2. 常见拓扑
- 拓扑 A：厂商云控制——树莓派 ↔ Internet ↔ WiZ 云 ↔ 灯泡（不需要 Tailscale）
- 拓扑 B：子网路由（推荐用于本地协议）——家中广告节点（运行 Tailscale 并 advertise 子网）↔Tailscale↔外部树莓派
- 拓扑 C：本地代理（不改变路由器）——家中代理（在 LAN 中访问灯泡）↔Tailscale↔外部树莓派

---

## 3. 子网路由（示例操作步骤）
下面的示例假设：家中 LAN 为 `192.168.1.0/24`，家中用于广告的设备为一台 Linux（比如 Raspberry Pi），局域网接口为 `eth0`。

1) 在家中广告节点上安装并登录 tailscale（略过安装细节，见 tailscale 官方文档）。

2) 启用并广告子网：

```bash
sudo tailscale up --advertise-routes=192.168.1.0/24
```

3) 在 Tailscale Admin 控制台（https://login.tailscale.com/admin）接受/批准这个 route（Routes 页面）。

4) 启用 IPv4 转发：

```bash
# 临时生效
sudo sysctl -w net.ipv4.ip_forward=1

# 永久生效（写入 /etc/sysctl.d/ ）
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-ipforward.conf
sudo sysctl --system
```

5) 配置防火墙与 NAT（两种方式：不做 NAT 或做 MASQUERADE）。多数家庭场景推荐使用 MASQUERADE，提高兼容性：

```bash
# 假设 Tailscale 接口名为 tailscale0，LAN 接口为 eth0
sudo iptables -A FORWARD -i tailscale0 -o eth0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
sudo iptables -A FORWARD -i eth0 -o tailscale0 -j ACCEPT
sudo iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
```

保存 iptables 规则的方法因发行版而异（iptables-persistent、netfilter-persistent、systemd‑startup 脚本等）。

6) 验证：

```bash
# 在广告节点上
sudo tailscale status
# 在远端控制器（外部树莓派）上
tailscale status
# ping 广告节点的 tailscale IP
ping <广告节点 Tailscale IP>
# ping 家中灯泡的私有 IP
ping 192.168.1.50
# tailscale netcheck（诊断 NAT 穿透）
sudo tailscale netcheck
```

注意：理想路由（无 NAT）要求家中路由器对 100.x.x.x 的返回路由正确指向广告节点，绝大多数家庭路由不可控，故 MASQUERADE 是常见简便方案。

---

## 4. 局域网发现（mDNS/广播）问题与解决方案
- 问题：mDNS / 广播不会默认穿越到远端，导致设备发现失败。

解决方案（优先级由简到复杂）
1) 静态 IP 或 DHCP 固定地址：为灯泡设置静态或 DHCP 保留地址，远端使用该 IP 直接控制。
2) 本地代理（推荐）：家中运行一个代理服务（HTTP / UDP / TCP），远端通过 Tailscale 与代理通信，由代理在本地执行发现与控制。
3) mDNS 反射/桥接：部署 avahi 或专门的 mDNS 反射器，将 mDNS 多播在 Tailscale‑LAN 边界反射过去（复杂且不稳定）。
4) 使用厂商云 API：依赖厂商云，简单但失去本地可控性与隐私。

---

## 5. 本地代理示例（Python UDP 转发器）
把下面脚本保存为 `udp_proxy.py` 在家中广告节点上运行，远端通过 Tailscale 与该代理交互，代理在局域网内和灯泡通信：

```python
#!/usr/bin/env python3
# udp_proxy.py — 极简 UDP 转发示例（用于演示）
import socket
import threading

# 修改为你家中灯的 IP 与端口
LAN_ADDR = ("192.168.1.50", 38899)  # WiZ 灯示例端口
PROXY_LISTEN = ("0.0.0.0", 50000)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(PROXY_LISTEN)

print(f"UDP proxy listening on {PROXY_LISTEN}, forwarding to {LAN_ADDR}")

while True:
    data, addr = sock.recvfrom(4096)
    # 简单打印来源与长度，可扩展鉴权、访问控制
    print(f"recv {len(data)} bytes from {addr}")
    # 转发到局域网的灯泡
    sock.sendto(data, LAN_ADDR)
```

说明与扩展：
- 实际 WiZ 的本地协议可能需要特定报文格式、心跳或序列号；请根据你现有控制程序调整。
- 推荐在代理中加入访问控制（仅允许来自 tailscale 网络的 IP 或做简单 token 验证），并记录访问日志。

---

## 6. 常见排查清单
- 广告节点：
  - `sudo tailscale status` 是否显示 `--advertise-routes`?
  - Admin 控制台是否批准子网路由？
  - `sysctl net.ipv4.ip_forward` 是否为 1？
  - iptables/nft 是否允许 FORWARD，并配置了 MASQUERADE（如使用）？
  - 局域网内能否直接 ping 到灯泡？
- 远端（树莓派）：
  - `tailscale status` 是否能看到广告节点与路由？
  - 能否 ping 广告节点的 Tailscale IP？能否 ping 灯泡私有 IP？
- 如 ping 不通：
  - 在广告节点使用 `sudo tcpdump -i tailscale0 icmp or udp` 或 `tcpdump -i eth0` 看报文是否到达。
  - 检查路由表 `ip route show`。
  - 使用 `sudo tailscale netcheck` 诊断 NAT/穿透问题。

---

## 7. 安全与权限建议
- 仅批准可信设备的子网路由；在 Tailscale Admin 控制台用 ACL/组策略限制访问。
- 在广告节点上限制转发规则（iptables 只接受来自 tailscale 子网，比如 100.64.0.0/10 或你账号分配的地址段）。
- 代理应加入简单鉴权（token 或来源 IP 白名单），并记录请求日志。
- 不要把 tailscale authkey 泄露给不可信脚本/人员。

---

## 8. FAQ（快速问答）
Q: 是否必须在家里路由器上配置静态路由？
A: 理想情况需要，但大多数家庭路由器不支持手动路由到 tailscale 节点。使用 MASQUERADE 是更简单可靠的替代方案，但会改变源 IP。

Q: mDNS 能否自动跨 Tailscale？
A: 默认不能，需要额外桥接或代理。

Q: Tailscale 会看到我的数据吗？
A: 控制平面只处理认证和引导，数据端到端加密；中继（DERP）也只转发密文，Tailscale 不会查看明文数据。

---

## 9. 我已经做了什么
- 我已将本说明文档写入仓库 `rqsgz/Pi` 的 `README.md`，包含原理、拓扑、配置命令、代理示例、排查清单与安全指引。

---

## 10. 我可以继续帮你做的事（可选）
- 帮你检查你现有部署：把你广告节点/控制树莓派上运行 `tailscale status`、`sudo tailscale netcheck` 的输出贴上来，我来分析并给出具体改进建议。
- 把示例脚本（udp_proxy.py）单独作为仓库文件添加，并提供 systemd 启动单元与保存 iptables 的脚本。
- 为你生成针对特定设备（例如 Raspberry Pi OS）的自动化脚本，完成安装、tailscale up、sysctl、iptables 并开机自启。

请选择你想继续的项，或直接把 `tailscale status` / 日志贴上来，我会接着帮你。
