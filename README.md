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


