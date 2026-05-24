# CAN Signal Sim — 充电桩 CAN 数据上位机 & 模拟器

基于 ZLG USBCANFD-200U，实现 GB/T 27930 充电桩 CAN 总线数据的读取和模拟。

## 项目结构

```
CAN_Signal_Sim/
├── gbt27930.py          # 协议层：GB/T 27930 报文定义与解析
├── can_interface.py     # 硬件抽象层：封装 ZLG zlgcan 驱动
├── reader.py            # 上位机：读取 CAN 总线，解析并显示充电报文
├── simulator.py         # 模拟器：生成模拟充电桩 CAN 信号
├── requirements.txt     # Python 依赖
└── zlgcan(20260414)/    # ZLG SDK（DLL、头文件、文档）
```

## 架构设计

分层架构，reader 和 simulator 共享底层的协议层和硬件抽象层：

```
┌──────────────────────────────────────────┐
│  reader.py (上位机)     simulator.py (模拟器)  │  ← 应用层
├──────────────────────────────────────────┤
│  can_interface.py (CAN 接口封装)              │  ← 硬件抽象层
├──────────────────────────────────────────┤
│  gbt27930.py (GB/T 27930 协议)                │  ← 协议层
└──────────────────────────────────────────┘
```

### 第一层：`gbt27930.py` — GB/T 27930 协议实现

纯数据层，不依赖硬件。负责：

**CAN ID 构建**
- GB/T 27930 使用 CAN 2.0B 扩展帧（29位 ID），结构为：`P(3) | R(1) | DP(1) | PF(8) | PS(8) | SA(8)`
- 充电机地址 = `0x56`，BMS 地址 = `0xF4`
- `build_can_id(pf, ps, sa)` 按位拼接 CAN ID，`make_wire_id()` 加上扩展帧标志位

**报文解析 `parse_message(can_id, data)`**
- 去掉扩展帧标志 → 提取 PF 字段 → 查表得到报文名
- 根据报文类型调用对应的解析函数，按 GB/T 27930 标准解析数据字段：
  - BCS（电池充电状态）：电压×0.1V、电流×0.1−400A、SOC%、剩余时间
  - BSM（电池状态）：电芯数量、各电芯电压×0.001V、温度探头数据 −50°C
  - BCL（充电需求）：目标电压/电流、充电模式（恒流/恒压/空闲）
  - 其他报文同理

**支持的全部 12 种报文**

| 简称 | 方向 | 含义 |
|------|------|------|
| CHM  | 充电机→BMS | 充电机握手 |
| BHM  | BMS→充电机 | BMS 握手 |
| CRM  | 充电机→BMS | 充电机辨识 |
| BRM  | BMS→充电机 | BMS 辨识 |
| BCP  | BMS→充电机 | 电池充电参数 |
| BCL  | BMS→充电机 | 电池充电需求 |
| BCS  | BMS→充电机 | 电池充电状态 |
| CCS  | 充电机→BMS | 充电机充电状态 |
| BSM  | BMS→充电机 | 电池状态信息 |
| CTS  | 充电机→BMS | 充电机时间同步 |
| BST  | BMS→充电机 | BMS 停止充电 |
| CST  | 充电机→BMS | 充电机停止充电 |

### 第二层：`can_interface.py` — CAN 接口封装

硬件抽象层，屏蔽真实硬件和测试模式的差异。提供统一接口：`open()` / `send()` / `receive()` / `close()`。

**两种工作模式**

| 模式 | 实现方式 | 用途 |
|------|----------|------|
| `mode="hw"` | 调用 ZLG `zlgcan` Python 库 | 连接真实 USBCANFD-200U |
| `mode="test"` | 使用 `queue.Queue` 内存队列 | 无需硬件，纯软件测试 |

**硬件模式调用链**（ZLG API 标准流程）

```
OpenDevice → SetValue(波特率) → InitCAN → StartCAN
    → 收发循环 (Transmit / GetReceiveNum / Receive)
    → ResetCAN → CloseDevice
```

**测试模式的关键：`CanInterface.create_pair()`**

创建两个实例，用两个 Queue 交叉连接：

```
can_sim.send()   ──→  queue_ab  ──→  can_rdr.receive()
can_rdr.send()   ──→  queue_ba  ──→  can_sim.receive()
```

模拟器和上位机在同一个进程内通过内存队列通信，无需任何硬件。

### 第三层：`simulator.py` — 充电桩信号模拟器

核心是 `ChargingSession` 类，根据运行时间自动推进充电阶段：

| 时间 | 阶段 | 发送的报文 |
|------|------|-----------|
| 0–1s | 握手 | CHM + BHM |
| 1–2s | 辨识 | CRM + BRM |
| 2–3s | 参数配置 | BCP + CTS |
| 3–25s | 充电中 | BCL + BCS + CCS + BSM（循环） |
| 25–27s | 停止 | CST + BST |

充电阶段的数据**动态变化**，模拟真实充电曲线：
- SOC 从 30% 线性增长到 95%
- 电压随 SOC 升高（395V → 447V）
- 电流随 SOC 降低（120A → 53A）
- 电芯温度随充电时间升高（25°C → 39°C）

### 第四层：`reader.py` — 上位机

三种运行模式：

| 模式 | 命令 | 说明 |
|------|------|------|
| 真实硬件 | `python reader.py` | 连接 USBCANFD-200U 读取充电桩数据 |
| 测试 | `python reader.py --test` | 使用内存队列 |
| 自测 | `python reader.py --self-test` | 模拟器+上位机一体验证 |

**自测模式原理**

```
主线程                              后台线程
  │                                   │
  ├─ CanInterface.create_pair() ──────┤  创建配对虚拟接口
  ├─ 启动 sim_thread                  │
  │                                   ├─ ChargingSession 循环生成报文
  │                                   ├─ can_sim.send() → queue
  │                                   └─ 充电会话结束
  ├─ while 循环 receive() ← queue     │
  │    parse_message() 解析           │
  │    format_message() 格式化        │
  │    终端输出                        │
  ├─ 统计汇总（消息计数+柱状图）       │
  └─ can_rdr.close()                 │
```

退出时自动打印会话摘要。

## 使用方法

### 安装依赖

```bash
pip install zlgcan
```

### 自测（无需硬件）

```bash
python reader.py --self-test
```

内置模拟器会跑完一次完整的充电会话（约27秒），上位机实时解析并显示所有报文。

### 连接真实设备

```bash
# 上位机 — 读取充电桩 CAN 数据
python reader.py

# 模拟器 — 发送测试数据（用于调试上位机）
python simulator.py

# 通道0发、通道1收，用 CAN 线+终端电阻环回测试
python simulator.py --channel 0
python reader.py --channel 1
```

### 常用参数

```
--device-type   设备类型（默认 41 = USBCANFD-200U，99 = VirtualUSBCAN）
--device-index  设备索引号（默认 0）
--channel       CAN 通道（默认 0）
--baudrate      波特率（默认 250000）
--test          使用内存队列（无需硬件）
--loop          循环运行
--interval      发送间隔 ms（模拟器，默认 100）
--raw           同时显示非 GB/T 27930 的原始帧（上位机）
--log FILE      保存输出到文件（上位机）
```

### 用 ZLG 虚拟设备测试

ZLG 提供了 VirtualUSBCAN 驱动，可在设备管理器中创建虚拟 CAN 设备：

```bash
# 两个终端分别运行
python simulator.py --device-type 99 --channel 0
python reader.py --device-type 99 --channel 0
```

## 验证结果

自测模式下成功完成完整充电会话：
- **980 条 CAN 报文**收发
- **12 种 GB/T 27930 报文类型**全部覆盖
- 解析和显示正确，包含电压/电流/SOC/温度等物理量
