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



brain_client.py
目的
简单的同步 TCP 客户端，用于连接 BrainServer（默认 127.0.0.1：9527），接收以换行分割的 JSON 消息并在终端打印/可视化。
主要函数
parse_args（）：解析 CLI（--host， --port， --raw）。
main（）：建立 socket 连接、循环接收数据、按行解析 JSON、根据 type 做不同显示或回应 pong。
重要行为
将接收到的数据以 \n 为边界分行解析为 JSON。
支持类型：
brain_state：格式化显示 attention/meditation/alpha/power/eyes/signal（带条形图）。
light_status：打印灯状态（亮度、色温、label）。
ping：收到后回应 pong（{“type”：“pong”，“ts”：...}）。
--raw 参数打印完整 JSON。
输入/输出
输入：TCP JSON-line 消息。
输出：终端文本或发送 pong 到服务器。
注意/潜在问题
当前使用 blocking socket.recv;若服务器暂停发送，recv 会阻塞（直到断开或有数据）。
无自动重连；连接失败时脚本退出。
JSONDecodeError 被静默吞掉（便于持续运行但不利调试）。
示例
Python3 brain_client.py --host 192.168.1.100

main.py
目的
应用入口，串联整个 pipeline：TGAM 数据源 → 解析 → 处理 → 分类 → 映射 → 执行（WiZ/GPIO）→ 广播/仪表盘;支持 replay、record、calibrate。
主要类/函数
load_config（path）：加载 config.yaml（支持覆盖 DEFAULT_CONFIG），注意之前已经修复为 deepcopy。
BTSocketStream：用原生 AF_BLUETOOTH socket 封装的串流替代类（提供 in_waiting， read， write， close）。
NeuroLux：主 orchestrator，包含 setup（）， _open_source（）， _open_csv_writer（）， run（）， _run_async（）， _next_frame（）， _start_web（）， _shutdown（）。
run_calibrate（）：交互式 alpha 阈值校准（采集睁眼/闭眼 alpha 并写 config.yaml）。
parse_args（）：CLI。
关键常量/配置
DEFAULT_CONFIG：串口、Wiz、TCP、Web、Signal、Replay、logging。
config.signal 包含阈值和窗口（alpha_close_thresh 等）。
主要流程（高层）
setup（） 实例化 processor， blink_detector， classifier， mapper， gpio/wiz/tcp。
_open_source（） 根据 config.serial.port 选择串口或 bt： 格式使用 BTSocketStream，或回放。
_run_async（） 循环实现：读取 frames → processor.process → classifier.classify → mapper.map → 执行 WiZ/gpio → broadcast → update dashboard → 写 CSV。
包含自动重连、非阻塞读策略（in_waiting 或非阻塞 socket）、错误计数触发重连。
输入/输出
输入：串口/蓝牙字节流或 CSV 回放。
输出：WiZ UDP 命令、GPIO PWM、TCP 广播、Flask 仪表盘、CSV 录制。
关键实现细节
TGAM init bytes： 写 bytes（[0xAA， 0x00]） 触发设备输出。
poor_signal > 50 时强制 LED 关并跳过状态更新（质量门控）。
_pending_override 用于 Web 手工覆盖灯光。
注意/潜在问题
串口行为依赖 pyserial 版本与底层驱动（in_waiting/timeout 行为）。
WiZ 操作为 async（pywizlight），需网络连通性与正确 IP 或 discovery 成功。
Web/TCP 若公开到 0.0.0.0 要注意安全。
常用运行
Python3 main.py
Python3 main.py --replay data/session.csv
Python3 main.py --校准

tgam_detect.py
目的
串口/设备扫描与 TGAM 同步字（0xAA 0xAA）检测;提供交互式终端实时监测（注意力/冥想/脑电波段/校验和速率）。
主要函数
scan_ports（）：检查常见端口、glob /dev/ttyUSB*， /dev/ttyACM*、并结合 serial.tools.list_ports。
probe_port（port）：尝试打开串口并在短时间内查找 0xAA 0xAA。
display_live（ser， show_raw）：核心终端 UI 循环，读取 raw bytes、parser.feed、聚合 last_values 并打印。
main（）：CLI，处理 scan-only、选择端口/打开串口、等待同步字后进入 display_live。
关键常量
TGAM_BAUD = 57600;DEFAULT_TIMEOUT = 0.5。
输入/输出
输入：串口字节流。
输出：终端界面（彩色），raw hex（可选）。
实现要点
使用 ThinkGearParser 解析帧并统计 parser.stats（checksum）。
终端采用 ANSI 控制实现“刷新”，用 \033[4H 回到画面某行重写（注意终端高度限制）。
注意
在 probe_port 未检测到同步字可能是因为设备未上电或波特不匹配。
对低终端高度或不同终端行为，光标定位可能有问题;可考虑用 curses。
示例
Python3 tgam_detect.py
Python 3 tgam_detect.py --仅扫描

tgam_read.py
目的
直接用蓝牙 RFCOMM socket 做原始 hexdump（不依赖 rfcomm 绑定），用于快速验证蓝牙连接和查看 0xAA 0xAA 等。
主要函数
parse_bt_port（port_str）：将 bt：MAC：channel 拆解为 （mac， channel）。
main（）：建立 AF_BLUETOOTH socket 连接，循环 recv 并以 16 字节行打印 hex。
输入/输出
输入：socket 蓝牙字节流。
输出：hex dump 行，每 16 字节一行。
注意
依赖系统支持 AF_BLUETOOTH（Linux/BlueZ）。
未高亮 0xAA 0xAA（注释提到但实现是简单 hexdump）。
未打印剩余不足 16 字节的尾部（退出前可能丢失）。
示例
Python3 tgam_read.py bt：04：22：12：02：0D：C0：1
init.py （tgam 包）
目的
包声明，导出 Parser 和 replayer 子包（简短）。
内容
all = [“解析器”，“重播者”]
作用
便于 from tgam import parser， replayer 使用。
gpio_led.py
目的
用于在树莓派上通过 RPi.GPIO 提供简单 PWM LED 驱动（模拟或实际硬件）。
主要类/方法
班级GpioLed：
init（pin=17， pwm_freq=100， simulate=False）：初始化 RPi.GPIO PWM 或进入 simulate 模式。
set（brightness）：0-100，设置占空比并记录当前亮度。
on（）， off（）， pulse（）， fade_to（target， duration， steps）：便捷效果。
cleanup（）：停止 PWM 并 cleanup pin。
重要实现点
初始会短暂 blink（100%，10%，50%）作为指示。
simulate=True 时仅记录日志，不访问硬件（便于开发机测试）。
注意
RPi.GPIO 在非 Pi 环境可能不可用，会自动退回 simulate 模式。
对高功率负载请使用 MOSFET 驱动，不要直接用 GPIO。

mapper.py
目的
把分类器输出（EyesState + attention）映射为具体 LightCommand（brightness， color_temp， transition_s， state_label）。
主要内容
data类LightCommand
默认映射表 _MAPPING_TABLE（open_focused， open_relaxed， transition， closed ， long_closed）
类光照映射器：
map（result） → LightCommand（根据 EyesState 和 注意阈值）
should_update（new_cmd） → bool（避免重复下发）
apply（result， wiz_ctrl， gpio_led=None）：直接执行映射（同步 WiZ/gpio），并更新 _last_cmd
输入/输出
输入：ClassifierResult
输出：LightCommand（以及对设备的实际调用）
注意
apply（） 中对 WiZ/gpio 的调用使用 try/except，不会抛出到调用者。
attn_focus_thresh 默认 60。

wiz_controller.py
目的
封装 pywizlight，用于发现与控制 WiZ 智能灯（async API）。
主要类/方法
dataclass WiZState：灯的快照信息（ip， mac， brightness， color_temp 等）。
WiZController：
发现（暂停）
_get_bulb（）
_call（coro， label） 包装超时
turn_on，turn_off，set_brightness，set_color_temp，set_scene，set_state，fade_to
get_state（） → WiZState
close（）， close_sync（）
重要实现点
fade_to 转换 duration_s到 WiZ speed（_transition_s_to_speed）。
使用 asyncio.wait_for 来限制超时。
注意
需要 pywizlight 依赖并在同一 LAN。
get_state 使用 bulb.updateState（），接口来自 pywizlight。

tcp_server.py
目的
异步 TCP server（asyncio），广播 brain_state / light_status JSON-line 给多个客户端，并维护心跳（ping/pong）。
主要函数/类
build_brain_state（...）、build_light_status（...）、build_ping（）、build_pong（）
班级BrainServer：
start（）， stop（）
广播（数据）、broadcast_brain_state（...）、broadcast_light_status（...）
_handle_client（reader， writer）：处理客户端入站（接收 pong）
_broadcast_worker（）：从队列取消息并发给所有 client
_heartbeat_loop（）：定期发送 ping & 杀掉超时未 pong 的 client
参数
ping_interval（默认 5 s），client_timeout（默认 15 s）
注意
Broadcast 使用 asyncio。Queue（maxsize=256），队列满时丢帧并记录警告。
_clients 使用 writer as key，保存 last_pong 时间戳。
使用
在 main 中创建并 await server.start（），并周期性调用 broadcast_brain_state。
blink_detector.py
目的
从 raw_wave 原始采样（512 Hz）检测眼眨（blink）事件，提供单/双眨检测能力。
主要类/函数
dataclass BlinkEvent（时间戳、振幅、duration_ms、is_double）
类别BlinkDetector：
feed（raw_wave： int， timest： float） → 可选[BlinkEvent]
reset（）、基线属性、blink_count属性
实现要点
用 rolling window 计算 baseline mean/std（Welford-like 简化），用 sigma_thresh（默认 3σ）作为检测阈值。
在 spike 完成（降到半阈值）时 finalize，并基于宽度（min/max width）判定是否为 blink。
double-blink：两次 blink 时间间隔小于 double_window_s（默认 1s）视为 double。
注意
参数（window_ms， sigma_thresh， min_width_ms， max_width_ms）可调以适配不同噪声环境。
对低质量/高噪声环境可能误报。

classifier.py
目的
基于平滑后特征（alpha_power， attention）实现 Schmitt-trigger 风格的眼睛开/闭分类，并提供防抖、冷却与长闭（sleep）检测。
主要类/函数
enum EyesState（开、闭、过渡、LONG_CLOSED）
dataclass ClassifierResult（时间戳、状态、alpha_power、注意、冥想、closed_duration_s）
类状态分类器：
分类（平滑）→分类器结果
_commit_state（new_state，现在，alpha_power） 内部切换
判定规则（默认）
正常（非倒置）：
关闭，如果阿尔法和注意力> alpha_close< attn_close
如果阿尔法或注意，开门时< alpha_open> attn_open
否则 过渡
inverted 支持 alpha 在闭眼时下降的设备
新状态必须持续 confirm_time 秒且 cooldown 后才能 commit
closed_duration 超过 sleep_time → LONG_CLOSED
注意
参数（alpha_close/open， attn thresholds， confirm_time， cooldown， sleep_time）在 config.signal 中配置。
这个 classifier 是决定灯光体验的核心，阈值对用户差异敏感，建议 run_calibrate 校准 alpha 阈值。


processor.py
目的
对原始 Frames 做平滑与 alpha 能量估算，输出 SmoothedFrame（attention/meditation 的滑动平均，alpha 的 EMA + 补偿）。
主要类/函数
dataclass SmoothedFrame（时间戳、 poor_signal、注意、冥想、 raw_wave、alpha_power、eeg_bands）
类信号处理器：
process（frame， raw_wave_samples=无） → SmoothedFrame
reset（）， is_stale属性
_compute_alpha_from_raw（samples） 用 biquad bandpass （10Hz） + RMS 做 alpha proxy（当没有 eeg_power 字段时）
实现要点
attention/meditation 用 SMA（滑动窗口）。
alpha 用 EMA（_alpha_smooth），并对 outlier 使用权重调整（避免单帧巨变导致 EMA 跳动过度）。
若 frame.eeg_power 提供 low_alpha/high_alpha，则直接用它们之和作为 alpha_instant;否则尝试从 raw_wave_samples 估算。
处理 saturated ADC 值（|x|>=2000）时跳过。
注意
EMA 参数 alpha_window 可调，影响响应速度与平滑度。
_compute_alpha_from_raw 相对粗糙但在缺乏 EEG 功率时有用。


parser.py
目的
实现 ThinkGear（二进制）协议的按字节状态机解析，将原始字节流转为 BrainFrame（包含 attention， meditation， raw_wave， eeg_power 等）。
主要常量/结构
SYNC_BYTE = 0xAA;MAX_PAYLOAD = 172
_CODE_MAP：code→（name， vlen， struct fmt），包含 0x02 poor_signal、0x04 attention、0x05 meditation、0x80 raw_wave（2B）、0x83 eeg_power（24B： 8×3B）
EEG_BANDS 列表顺序。
状态 enum：WAIT_SYNC1、WAIT_SYNC2、WAIT_PLEN、READ_PAYLOAD、VERIFY。
dataclass BrainFrame（时间戳、poor_signal、注意、冥想、raw_wave、eeg_power、checksum_ok、raw_payload）
主要类/函数
类 ThinkGearParser：
feed（数据：字节） →列表[BrainFrame]
_consume（byte） 内部按字节消费并在帧完成时返回 BrainFrame
_decode（payload， checksum_ok） → BrainFrame：解析 payload 内的 code|vlen|data 序列并 carry-forward 上次值（带 TTL）
统计属性：{total_packets， bad_checksum， checksum_pass_rate}
重置（）
实现要点
校验和：chk == （~sum（有效载荷） & 0xFF）
解析 0x83（EEG power）为 8 个 3-byte big-endian unsigned。
carry-forward：若某些值在当前 payload 未出现，则用最近值补入，但带 TTL（默认 5s）避免永久填充陈旧值。
注意
对未知 code 会跳过对应 vlen。
parser.feed 逐字节解析，适合流式输入（串口 / 蓝牙）。
checksum 失败会统计并仍返回 frame，但 frame.checksum_ok 标志为 False。


replayer.py
目的
从 CSV 会话文件重放为 BrainFrame（便于在无头戴设备时调试 pipeline），并提供异步迭代接口与 CSV 写入辅助。
主要类/函数
Replayer（path， speed=1.0， loop=False）：
iter/next 实现同步迭代（以 CSV ts 字段控制 inter-frame timing，speed 可加速/减速或 speed=0 最快）
async_iter（） 返回 异步生成器
_row_to_frame（行） → BrainFrame（把列转 int）
write_frame_row（作者，框架）：把 BrainFrame 写入 csv.DictWriter（用于录制）
CSV 格式
标题：TS、注意、冥想、poor_signal、raw_wave、Delta、Theta、low_alpha、high_alpha、low_beta、high_beta、low_gamma、mid_gamma
注意
ts 字段用于重放间隔;若不合理，speed=0 以最快速度回放。
Replayer 会在 loop=True 时把行追加回尾实现循环。


dashboard.py
目的
Flask web dashboard + SSE（Server-Sent Events）供实时显示 brain/light 状态，并提供 API 供手动 override。
主要类/函数
LightSnapshot 数据类
DashboardState：线程安全（Lock）共享状态（attention， meditation， alpha_power， poor_signal， eyes_state， blink_count， eeg_bands， light snapshot， history）
update_brain（...）， update_light（...）， snapshot（）， history（n）
create_app（州）：Flask app factory，提供 route：
获取 / → templates/index.html
获取 /api/state 的快照 JSON →
获取 /api/history？n=200 → 历史
获取 /api/light → light snapshot
POST /api/light/set → 手动覆盖（设置 _pending_override）
获取 /api/stream → SSE 事件流（轮询 state.snapshot）
模块级_pending_override用于主循环检测并执行手动 light override。
实现要点
SSE generator 使用 time.sleep（0.15） 频率推送（约 6-7Hz）。
state.update_brain 中对数据做简单校验（注意/药物界限，alpha range filter）。
