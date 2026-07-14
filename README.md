项目概览
NeuroLux 的目标是把脑电信号转换为有意义的灯光行为（例如：睁眼专注 → 冷色高亮;闭眼放松 → 暖色变暗），并提供完整的工具链：

实时采集：支持蓝牙 RFCOMM（bt：MAC：channel 或 /dev/rfcommX）与串口（/dev/ttyUSB*、/dev/ttyAMA0）。
解析器：ThinkGear 协议解析（tgam.parser）。
信号处理：平滑、alpha 能量提取（signal.processor）。
分类器：基于 α-blocking 与 attention的 Schmitt-trigger 状态机（signal.classifier）。
执行：WiZ 智能灯（UDP/pyez）与本地 GPIO PWM（light.gpio_led）。
可视化：Flask 仪表盘 + SSE（web.dashboard）。
网络广播：TCP JSON-lines broadcast（network.tcp_server），提供 brain_client.py 客户端接入。
诊断与工具:tgam_detect.py、tgam_read.py、replayer（离线回放）。




主要模块说明
main.py：应用入口。初始化组件，运行主循环（读取帧 → 处理 → 映射 → 执行 → 广播），支持 replay、record、calibrate。
brain_client.py：TCP 客户端，用于连接 BrainServer 并在终端显示实时脑态/灯光消息。
tgam_detect.py：串口/蓝牙检测与实时诊断工具（终端界面，显示 attention / meditation / EEG bands / checksum）。
tgam_read.py：原始蓝牙字节 hexdump 工具（快速验证蓝牙连接）。
tgam/parser.py：ThinkGear 协议解析器（帧边界、校验、payload 解码）。
tgam/replayer.py：CSV 回放器与 CSV 写入助手（write_frame_row）。
signal/processor.py：平滑与 alpha-power 提取（EMA + biquad）。
signal/classifier.py：基于 alpha / attention的 Schmitt-trigger 状态机（防抖、冷却、长闭检测）。
signal/blink_detector.py：从 raw_wave 检测眨眼事件（单、双眨）。
light/gpio_led.py：树莓派 GPIO PWM 驱动（RPi.GPIO），包含 fade/pulse 等。
light/wiz_controller.py：WiZ 灯控制封装（pywizlight）。
light/mapper.py：将分类结果映射为具体的 wifi 灯命令（亮度/色温/过渡）。
network/tcp_server.py：异步 TCP 广播服务器（JSON line 协议）。
web/dashboard.py：Flask 仪表盘与 SSE（实时状态、历史、手动覆盖）。



配置（config.yaml）
示例位于 。主要项：config.example.yaml

serial.port： 串口路径或蓝牙格式 bt：MAC：channel（例如 bt：04：22：12：02：0D：C0：1）
serial.baudrate / timeout：TGAM 默认 57600
wiz.ip / port / timeout：WiZ 灯的 IP（可为空，程序会 discover）
tcp.host / port：TCP 广播监听地址
web.host / port：Flask 仪表盘监听地址（建议本地 127.0.0.1 或在内网）
信号。*：attn_window，alpha_window，门槛alpha_open/关闭阈值，confirm_time，冷却时间，sleep_time
replay.*：回放速度与是否循环
logging.level：DEBUG/INFO/WARNING




调试与诊断（常见问题）
无法打开串口
检查权限： groups 应包含 ;或使用 sudo 临时测试dialout
检查设备： ls -la /dev/rfcomm0 或 /dev/ttyUSB0
未检测到 TGAM 同步字（0xAA 0xAA）
检查 TGAM 是否上电、接线是否正确，电极是否接触
试用 tgam_read.py 或 tgam_detect.py ---仅扫描
程序卡死或高 CPU
检查是否在非阻塞读取模式下 busy-wait（可通过 top 观察）
将 logging.level 设为 DEBUG 以查看日志
WiZ 灯控制失败
确认 WiZ 灯在同一网段并已连接;检查 discover/配置的 IP
在 WiZController 中查看异常日志（网络超时）
Web 仪表盘无法访问
检查 web.host 配置（默认示例改为 127.0.0.1，改为 0.0.0.0 可 LAN 访问）
检查防火墙或路由



