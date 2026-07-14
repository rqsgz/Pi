# NeuroLux 🧠💡

![build](https://img.shields.io/badge/build-unknown-lightgrey.svg) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![license](https://img.shields.io/badge/license-MIT-lightgrey.svg)

将脑电（TGAM/ThinkGear）信号映射为有意义的灯光行为（例如：睁眼专注 → 冷色高亮；闭眼放松 → 暖色变暗），并提供从采集到可视化的完整工具链。

---

## 目录
- [概览](#概览)
- [特性](#特性)
- [快速开始](#快速开始)
- [主要模块说明](#主要模块说明)
- [配置（config.yaml）](#配置configyaml)
- [调试与常见问题](#调试与常见问题)
- [开发与贡献](#开发与贡献)
- [许可证 & 联系方式](#许可证--联系方式)

---

## 概览
NeuroLux 把来自 ThinkGear（TGAM）设备的脑电数据通过解析 → 信号处理 → 分类 → 映射 → 执行（WiZ / GPIO）→ 广播 / 仪表盘 的流水线，实时驱动灯光并提供诊断工具与回放能力。目标是低延迟、可配置且便于调试的端到端管道。

---

## 特性 ✅
- 实时采集：蓝牙 RFCOMM（bt:MAC:channel 或 /dev/rfcommX）与串口（/dev/ttyUSB*、/dev/ttyAMA0）。
- ThinkGear 协议解析（tgam.parser）。
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

## 开发与贡献 💡
- 代码风格：遵循项目内 linters（如 .pre-commit、flake8/eslint）。
- 测试：`pytest`（如有 test/）。
- 提交准则：清晰的 commit message，PR 请包含变更描述与必要测试/截图。
- 若需我帮你把 README 直接提交到仓库，请提供仓库地址与分支偏好，我可以创建 PR 或直接更新（视权限）。

---

## 许可证 & 联系方式 📄
- 许可证：MIT（详见 LICENSE）。
- 维护者：你的名字或团队
- Issues：通过 GitHub Issues 报告 bug/需求
