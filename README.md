# NeuroLux 🧠💡

![build](https://img.shields.io/badge/build-unknown-lightgrey.svg) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![license](https://img.shields.io/badge/license-MIT-lightgrey.svg)

将脑电（TGAM/ThinkGear）信号映射为有意义的灯光行为（例如：睁眼专注 → 冷色高亮；闭眼放松 → 暖色变暗），并提供从采集到可视化的完整工具链��[...] 

---

## 目录
- [概览](#概览)
- [特性](#特性)
- [快速开始](#快速开始)
- [主要模块说明](#主要模块说明)
- [配置（config.yaml）](#配置configyaml)
- [调试与常见问题](#调试与常见问题)

---

## 概览
NeuroLux 把来自 ThinkGear（TGAM）设备的脑电数据通过解析 → 信号处理 → 分类 → 映射 → 执行（WiZ / GPIO）→ 广播 / 仪表盘 的流水线，实时驱动灯光并[...]

---

## 特性 ✅
- 实时采集：蓝牙 RFCOMM（bt:MAC:channel 或 /dev/rfcommX）与串口（/dev/ttyUSB*、/dev/ttyAMA0）。
- ThinkGear 协议解析（tgam.parser��。
- 信号处理：平滑、alpha 能量提取（signal.processor）。
- 分类器：基于 α-blocking 与 attention 的 Schmitt-trigger 状态机（signal.classifier）。
- 灯控执行：WiZ (UDP/pywizlight) 与本地 GPIO PWM（light.gpio_led）。
- 可视化：Flask 仪表盘 + SSE（web.dashboard）。
- 网络广播：异步 TCP JSON-lines 服务供客户端订阅（network.tcp_server）。
- 诊断工具：tgam_detect.py、tgam_read.py、replayer（离线回放/录制）。

---

## 快速开始 🚀
1. 克隆仓库：
   ```bash
   git clone https://github.com/rqsgz/Pi.git
   cd Pi
   ```
2. 安装依赖（示例）：
   ```bash
   python -m pip install -r requirements.txt
   ```
3. 运行（默认从串口/蓝牙读取）：
   ```bash
   python3 main.py
   ```
4. 回放 CSV 会话：
   ```bash
   python3 main.py --replay data/session.csv
   ```
5. 客户端查看广播（默认 127.0.0.1:9527）：
   ```bash
   python3 brain_client.py --host 127.0.0.1 --port 9527
   ```

---

## 主要模块说明（简洁版） 🧩

- main.py  
  应用入口。初始化组件并运行主循环：读取帧 → 处理 → 分类 → 映射 → 执行 → 广播。支持 --replay、--record、--calibrate。

- brain_client.py  
  简单同步 TCP 客户端，连接 BrainServer（默认 127.0.0.1:9527），按行解析 JSON 并在终端显示（支持 --raw）。注意：当前为 blocking socket、无自动重连。

- tgam_detect.py  
  串口/设备扫描与 TGAM 同步字（0xAA 0xAA）检测。提供终端实时监测 attention/meditation/EEG bands/校验等信息。

- tgam_read.py  
  原始蓝牙 RFCOMM hexdump（直接用 AF_BLUETOOTH socket），用于快速验证蓝牙连接与同步字。

- tgam/ (包)
  - parser.py：ThinkGear 按字节状态机解析器，输出 BrainFrame（包含 attention、meditation、raw_wave、eeg_power 等），实现 carry-forward、checksum 统计等。
  - replayer.py：CSV 回放器与写入助手（write_frame_row），支持 speed、loop。

- signal/  
  - processor.py：平滑 (SMA/EMA) 与 alpha-power 估算（若无 eeg_power 则从 raw_wave 用 biquad + RMS 近似）。
  - classifier.py：基于 alpha & attention 的 Schmitt-trigger 状态机（防抖、冷却、长闭检测）。
  - blink_detector.py：从 raw_wave 检测单/双眨事件（基于 rolling baseline + sigma threshold）。

- light/  
  - gpio_led.py：树莓派 GPIO PWM 驱动（RPi.GPIO），支持 simulate 模式、fade/pulse、cleanup 等。
  - wiz_controller.py：pywizlight 封装（async），发现与控制 WiZ 灯，timeout/异常处理。
  - mapper.py：把分类结果映射为 LightCommand（brightness、color_temp、transition 等），并调用 WiZ/GPIO。

- network/  
  - tcp_server.py：asyncio 异步 TCP 广播（JSON-lines），支持多客户端、心跳 ping/pong、队列限流。

- web/  
  - dashboard.py：Flask + SSE 仪表盘，提供实时流、历史快照与手动覆盖 API（/api/light/set）。

---

## 配置（config.example.yaml） ⚙️
配置位于 config.example.yaml（将其复制为 config.yaml 并编辑）。关键字段摘要：

- serial:
  - port: 串口路径或蓝牙格式 `bt:MAC:channel`（例如 `bt:04:22:12:02:0D:C0:1`）
  - baudrate / timeout：TGAM 默认 57600
- wiz:
  - ip / port / timeout：WiZ 灯 IP（可空：程序可使用 discover）
- tcp:
  - host / port：TCP 广播监听地址（默认本地)
- web:
  - host / port：Flask 仪表盘监听地址（建议开发用 127.0.0.1；0.0.0.0 允许内网访问）
- signal:
  - attn_window, alpha_window
  - alpha_open/alpha_close 阈值
  - attn_open/attn_close 阈值
  - confirm_time, cooldown, sleep_time
- replay:
  - speed, loop
- logging:
  - level: DEBUG / INFO / WARNING

建议先用 run_calibrate 交互校准 alpha/attention 阈值以适配不同用户与设备。

---

## 调试与常见问题 🩺

- 无法打开串口：
  - 检查权限：用户是否在 dialout 组？或用 sudo 临时测试。
  - 检查设备：`ls -la /dev/rfcomm0` 或 `/dev/ttyUSB0`。

- 未检测到 TGAM 同步字（0xAA 0xAA）：
  - 检查设备上电、接线、电极接触。
  - 用 `tgam_read.py` 或 `tgam_detect.py --scan` 验证。

- 程序卡死或高 CPU：
  - 检查是否 busy-wait（非阻塞读取时轮询），可用 top/htop 监控。
  - 将 logging.level 设为 DEBUG 以获得更多信息。

- WiZ 灯控制失败：
  - 确认灯与主机在同一网段，或在配置中设置正确 IP。
  - 检查 discover/pywizlight 的异常日志（网络超时）。

- Web 仪表盘无法访问：
  - 检查 web.host（默认 127.0.0.1）；改为 0.0.0.0 可在 LAN 访问。
  - 检查防火墙规则与路由。

---

## 运行示例命令
- 启动（真机）：
  ```bash
  python3 main.py
  ```
- 回放 session.csv：
  ```bash
  python3 main.py --replay data/session.csv
  ```
- 交互校准 alpha 阈值：
  ```bash
  python3 main.py --calibrate
  ```
- 设备检测：
  ```bash
  python3 tgam_detect.py
  ```
- 原始蓝牙 hexdump：
  ```bash
  python3 tgam_read.py bt:04:22:12:02:0D:C0:1
  ```

---

## Tailscale + WiZ 控制指南

本节为附加内容：在树莓派与 WiZ 灯泡不在同一物理网络（例如树莓派连手机热点、灯泡连家中 Wi‑Fi）时，如何用 Tailscale 实现远程局域网控制的原[...]

### 结论摘要（一句话）
通过在灯泡所在局域网中运行一个 Tailscale 节点并启用子网路由（advertise‑routes），可以把家中局域网的设备在 Tailscale 上“暴露”出来，外部的树��[...]

### 原理要点
- Tailscale 使用 WireGuard，为每台设备分配虚拟 IP（通常 100.x.x.x），控制平面做身份与密钥分发，数据优先点对点，穿透失败使用 DERP 中继。
- 子网路由允许在某台设备上广告整个 LAN（--advertise-routes），管理员批准后其它 tailscale 节点能路由到该子网。
- mDNS/广播等发现协议默认不会穿越子网路由；需要使用静态 IP、本地代理或 mDNS 转发器来解决。

### 子网路由示例（快速步骤）
1) 在家中广告节点运行：
```bash
sudo tailscale up --advertise-routes=192.168.1.0/24
```
2) 在 Admin 控制台批准该路由。
3) 启用 IP 转发并添加 NAT（MASQUERADE）以提高兼容性：
```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -A FORWARD -i tailscale0 -o eth0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
sudo iptables -A FORWARD -i eth0 -o tailscale0 -j ACCEPT
sudo iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
```
4) 在远端用 `tailscale status`、`ping <内网灯 IP>` 验证连通。

### 本地代理示例（UDP 转发器）
把脚本保存为 `udp_proxy.py` 并在家中广告节点运行：
```python
#!/usr/bin/env python3
import socket

LAN_ADDR = ("192.168.1.50", 38899)
PROXY_LISTEN = ("0.0.0.0", 50000)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(PROXY_LISTEN)
print(f"UDP proxy listening on {PROXY_LISTEN}, forwarding to {LAN_ADDR}")
while True:
    data, addr = sock.recvfrom(4096)
    print(f"recv {len(data)} bytes from {addr}")
    sock.sendto(data, LAN_ADDR)
```

建议为代理添加鉴权与访问日志。

### 常见排查与安全建议
- 检查 `tailscale status`、Admin 控制台的 route 是否被接受。
- 确认 `sysctl net.ipv4.ip_forward` 为 1，iptables/nft 有正确 FORWARD 与 NAT 规则。
- 使用 `sudo tcpdump -i tailscale0`/`-i eth0` 排查流量是否到达。
- 只批准可信设备，代理加入 token 或 IP 白名单，保存并限制 iptables 规则。

---

## AI 分析 · 发送邮箱 🧠✉️

### 简短结论 ✅
- 项目用途：树莓派 + TGAM 采集 EEG，按 10 帧为一组调用 DeepSeek 做中文分析，再通过 QQ SMTP 把报告以 HTML 邮件发出；流程完整、可运行。

### 总体架构与数据流 🧭
- 🔌 采集：`scripts/ai_reporter.py` 周期（`poll_interval_s`）向 `http://127.0.0.1:5000/api/state` 请求单帧（`fetch_state`）。
- 🧹 过滤：跳过空帧（TGAM 无信号），累计到 `batch_size`（默认 10）（`collect_batch`）。
- 🤖 分析：把批次格式化为表格，构造 prompt 调用 DeepSeek（`deepseek_analyze`）。
- ✉️ 发送：把模型输出嵌入 HTML 模板，通过 QQ `SMTP_SSL` 发送（`send_email`）。
- ♻️ 运行：`main` 中循环执行，`CTRL+C` 退出。

### 关键函数（快速索引） 🔎
- `load_config()`：读取 `config.yaml` 的 `reporter` 配置。
- `collect_batch()`：采集并显示带进度的批次。
- `build_data_table()`：生成发送给 DeepSeek 的纯文本表格（保持不带图标以保证模型行为一致）。
- `deepseek_analyze()`：调用 DeepSeek API。
- `send_email()`：通过 QQ SMTP 发 HTML 邮件。

### 潜在问题与建议（按优先级） ⚠️
- 🔴 高优先级
  - DeepSeek 请求缺少重试/退避：遇到临时网络或限流会丢失整轮结果，建议实现 2–3 次指数退避重试。
  - 明文存放密钥/授权码：`config.yaml` 中包含敏感信息，建议改为环境变量或系统密钥管理（并设置文件权限 `600`）。
- 🟠 中优先级
  - `fetch_state()` 异常被吞（`except` 直接忽略）：建议至少 `log.debug` 异常信息以便排查。
  - `collect_batch()` 超时逻辑（`max_attempts`）不直观：建议改为基于 wall‑clock 的 `timeout_s` 配置。
  - 文档与实现不一致（跳过空帧条件）：统一判断逻辑（`attention`/`meditation`/`alpha_power` 三者均为 0 时跳过）。
  - 日志与监控不足：建议输出到文件或配合 `systemd`/Prometheus 监控关键指标（延迟、失败率、邮件发送成功率）。
- 🟢 低优先级
  - 邮件缺少纯文本备用 `text/plain` 部分：建议同时附带 `text/plain` 提升兼容性。
  - 邮件可附带 CSV/JSON 附件，便于人工审查与回溯。

### 推荐改进清单（行动项） 🛠️
1. 必做：实现 DeepSeek 重试（遇 5xx/网络错误重试），并用环境变量管理密钥。 
2. 应做：引入 `timeout_s` 替代 `max_attempts`；在 `send_email()` 中添加 `text/plain` part 并可选附 CSV。 
3. 可选：写入本地 SQLite/CSV 作为历史与断网重发缓冲；增加 Prometheus 导出接口或 `/health` 端点；添加 systemd unit 以守护进程形式运行。

### 运维与安全建议 🔒
- 不要将 API Key 或邮箱授权码提交到仓库；建议使用环境变量（例如 `DEEPSEEK_API_KEY`、`EMAIL_PASSWORD`）或设备本地密钥管理。
- 使用 `systemd` 管理脚本：`Restart=on-failure`，并把日志交给 `journal` 或文件轮替。
- 若 EEG 数据包含敏感信息，邮件发送前考虑脱敏或提示隐私风险。

---

## Dashboard：SSE / 线程安全 / 输入校验 总结与建议 ✨

下面是针对 `web/dashboard.py`（及相似文件）整理后的清晰美观总结，包含原理、问题点、改进建议与操作清单，方便直接阅读或粘贴入 README：

### 概览 🧭
客户端（浏览器 / PWA） ⇄ Flask REST / SSE
        ↑                          ↓
        │                          │
   [SSE 推送]                 [控制请求]
        ↑                          ↓
     DashboardState  ←──  主循环（TGAM 采集 + 灯控执行）
        ↑
   历史环形缓冲（deque） & 配置（STATE_COLORS）

### 关键模块与职责（图标化）
- 🧠 DashboardState（共享状态）
  - 保存: attention / meditation / alpha_power / poor_signal / eyes_state / blink_count / eeg_bands / last_update
  - 灯状态: LightSnapshot（brightness / color_temp / rgb / is_on / state_label）
  - 历史: deque 环形缓冲（最大长度可配置）
  - 建议: 把 pending override 纳入此对象并用 lock 保护，���免竞态。

- 🌐 Flask API（web 接口）
  - GET /api/state：当前快照
  - GET /api/history?n=：最近 N 帧
  - GET /api/stream：SSE 实时流
  - POST /api/light/set、/api/presets/apply、/api/rgb/apply：手动或预设灯控
  - 管理: /api/states-config（动态修改映射并持久化）

- 🔁 SSE（Server-Sent Events）
  - 作用: 把最新状态实时推送到前端
  - 建议: 降低推送频率到 0.5–1s 或仅在状态变化时推送 + 心跳；使用 stream_with_context；发送 id、event、retry 字段。

- 💾 配置持久化（config.yaml）
  - 通过 PyYAML 保存 STATE_COLORS；若未安装 PyYAML，应优雅提示并继续运行。

- 🛠 PWA（manifest + sw）
  - 提供离线缓存与安装能力；sw.js 采用 stale-while-revalidate 策略，API 请求不缓存（合理）。

### 主要问题与建议（优先级标注）
- ⚠️ 线程竞态（高）
  - 问题: 当前 `_pending_override` 为模块级 dict，API 并发写入与主循环读取时无锁保护。
  - 建议: 将其移入 `DashboardState`，提供 `queue_override()` 与 `pop_override()`，并用 `self._lock` 保护。

- ⚠️ SSE 频率不一致（中/高）
  - 问题: 注释写 500 ms（2 Hz），实现 `time.sleep(0.15)` ≈ 150 ms（6–7 Hz）。
  - 建议: 统一为 0.5–1s 或仅在状态变化时推送，添加心跳间隔（例如 5s）。

- 🛡 安全性（高）
  - 问题: 无鉴权的 POST 接口允许任意人控制灯（若在公网）。
  - 建议: 加入简单 token 验证或仅允许内网访问；对管理接口做速率限制并记录审计日志。

- ✅ 输入验证（中）
  - 建议: 对 brightness (0–100)、rgb（三元组，0–255）与 color_temp 做严格检查；避免 `get_json(force=True)` 在非 JSON 请求时抛异常。

- ℹ️ 数据格式（中）
  - 建议: 在 `snapshot()` 返回 `last_update`（数值秒或毫秒整数）；`history()` 中保留数值型 `ts`（并额外提供格式化字符串，如有需要）。

### 可直接采纳的代码片段（示例）
- 把 pending override 移入 `DashboardState`：
```python
# DashboardState.__init__
self._pending_override = {"pending": False, "brightness": None, "color_temp": None, "rgb": None}

def queue_override(self, brightness=None, color_temp=None, rgb=None):
    with self._lock:
        self._pending_override.update({"brightness": brightness, "color_temp": color_temp, "rgb": rgb, "pending": True})

def pop_override(self):
    with self._lock:
        p = dict(self._pending_override)
        self._pending_override = {"pending": False, "brightness": None, "color_temp": None, "rgb": None}
    return p
```

- SSE 改进思路（简化）：
```python
from flask import stream_with_context

@app.route('/api/stream')
def api_stream():
    @stream_with_context
    def event_stream():
        last_ts = None
        heartbeat = 5.0
        while True:
            snap = state.snapshot()
            if snap.get('last_update') != last_ts:
                last_ts = snap.get('last_update')
                yield f"id: {int(last_ts*1000)}\n"
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
            else:
                yield ":\n\n"  # comment-style heartbeat
                time.sleep(heartbeat)
            time.sleep(0.1)
    return Response(event_stream(), mimetype='text/event-stream', headers={...})
```

### 优先级行动清单（短）
1. 必做：把 `_pending_override` 迁入 `DashboardState` 并加锁；在 API 中改为 `state.queue_override(...)`。
2. 必做：在 `snapshot()` 中返回 `last_update`；将 `history()` 的 `ts` 保留数值格式。
3. 高优先级：在公开部署前为 POST 接口加入最小鉴权或仅允许内网访问。
4. 中优先级：把 SSE 改为“变更触发 + 心跳”，使用 `stream_with_context`。
5. 中优先级：统一输入验证（brightness、rgb、color_temp），并改用 `get_json(silent=True)`。

---

如果你希望，我可以：
- 把上面的补丁直接推到代码（我已经准备好变更），或
- 仅把这份说明保留在 README（已添加），方便后续人工合并。

（我已将这段“Dashboard：SSE / 线程安全 / 输入校验 总结与建议”内容追加到 README。若需要我现在把代码改动写入仓库文件，我也可以在一个独立分支上提交并创建 PR。）
