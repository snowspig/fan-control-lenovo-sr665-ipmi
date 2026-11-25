# sr665-fanctl — Lenovo ThinkSystem SR665 静音风扇控制器

用 IPMI 闭环控制 **Lenovo ThinkSystem SR665（V1）** 的 6 个风扇：
空闲时把转速从 BMC 默认的 **~8,900 RPM 压到 ~3,000–4,300 RPM**（原厂策略在机器什么都不干时也全速吹），
CPU / 内存 / NVMe 一升温就自动拉高。所有故障都倒向"更吵"，绝不倒向"更静"。

- 仅 Python 标准库（3.11+），零第三方依赖
- 单区控制（SR665 的 6 个风扇是同一条 N+1 风道，分区没有收益）
- 每次启动自动交叉校验传感器读取路径，读错就拒绝启动
- 崩溃 / 停止时自动把风扇交还 BMC（systemd `ExecStopPost`）

> **⚠️ 先读免责声明**：Lenovo 明确表示手动风扇命令"不受支持/不推荐，
> 可能造成内部元件热损坏"（见[参考](#参考)）。本项目在 1 台 SR665 V1 上实测验证并长期运行，
> 但**不对你的硬件负责**。动手前请完整读完本文档，尤其是[首次使用](#首次使用必做-phase-0)。

English version: [README.en.md](README.en.md)

## 目录

- [为什么写这个](#为什么写这个)
- [工作原理](#工作原理)
- [实测数据](#实测数据这台-sr665-v1)
- [安装](#安装)
- [首次使用必做 Phase 0](#首次使用必做-phase-0)
- [配置](#配置)
- [命令](#命令)
- [安全设计](#安全设计)
- [已知限制](#已知限制)
- [踩过的坑](#踩过的坑)
- [参考](#参考)
- [许可](#许可)

## 为什么写这个

这台 SR665 放在家里。实测（负载 0.00、CPU 41 °C、环境 25 °C）时，XCC 仍把 6 个
Performance 双转子风扇顶在 **8,560–8,880 RPM**（SDR 里的"标称转速"）——噪音完全是
BMC 的空闲策略造成的，不是散热需求。

现成方案都不适用：

- 上游 [`fyr233/fan-control-sr655`](https://github.com/fyr233/fan-control-sr655)
  面向**带 AMD GPU 的 SR655 V1**：风扇映射按 GPU 温度分区，本机没有 GPU；
  它用的 OEM 命令 `raw 0x3c 0x30 …` 在 SR665 的 XCC 上被直接拒绝（`rsp=0xc1`）；
  写完不回读转速；且仓库无 LICENSE。
- XCC 的 Web 界面没有风扇转速设置。

所以重写了一个针对 SR665 的小型闭环控制器，并在真机上完成了全部标定
（[docs/probe-results.md](docs/probe-results.md)）。

## 工作原理

```text
  ┌────────────────── 每 5 秒 ─────────────────┐
  │ sysfs hwmon:  CPU1/CPU2 Tctl (k10temp)        │
  │ IPMI raw 读:  风扇前/后转子 ×12、DIMM ×4      │
  │               (每条一条 IPMI 消息，~0.08 s)     │
  │ 后台线程:     NVMe ×8 温度 (hptraidconf)      │
  └───────────────────┬─────────────────────────┘
                      ▼
   CPU / DIMM / NVMe 三条曲线各自求值 → 取最大
   → 温度滞回 2 °C → 下降限速（上升不限）
                      ▼
   ipmitool raw 0x3a 0x07 0xff <速度字节> 0x01
                      ▼
   回读 12 个转子转速 ──► 校验写入是否真的生效
```

关键规则：

| 规则 | 值 |
| --- | --- |
| 速度字节 0 永不发送 | 那是"交还 BMC 自动"的模式 |
| 硬件下限 `hard_floor_duty` | 任何路径不得低于它（本机实测字节 8 ≈ 1,600 RPM） |
| 紧急全速（字节 255 ≈ 18,400 RPM） | CPU ≥ 85 °C / DIMM ≥ 85 °C / NVMe ≥ 78 °C |
| 传感器连续 3 个周期读不到 | 全速（感知不到温度就按最坏情况处理） |
| 转速低于预期值一半，连续 2 个周期 | 全速 + ERROR（风扇停转或命令被忽略） |
| IPMI 写入失败 | 重试 1 次，仍失败退出，由 systemd 重启 |

## 实测数据（这台 SR665 V1）

**风扇命令**（上游 SR655 的 `0x3c 0x30` 在本机被拒 `0xc1`）：

```bash
ipmitool raw 0x3a 0x07 0xff <速度字节> 0x01   # 固定转速
ipmitool raw 0x3a 0x07 0xff 0x00 0x00         # 交还 BMC 自动控制
```

**速度字节 → 转速**（Performance 双转子风扇；⚠️ 字节不是 RPM 的百分比，低端被明显抬高）：

| 字节 | 8 | 15 | 20 | 30 | 50 | 100 | 127 | 192 | 255 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 最低转子 RPM | 1600 | 1920 | 2240 | 3040 | 4400 | 8000 | 10000 | 14080 | 18400 |

**效果**：

| 场景 | 原厂策略 | 本控制器 |
| --- | --- | --- |
| 空闲（凉机，CPU 47–49 °C） | 8,560–8,880 RPM | **2,960–3,040 RPM**，NVMe 39–41 °C |
| 空闲（机箱热平衡后，CPU 58–60 °C） | 同上 | **3,700–4,300 RPM**（控制器按曲线微调） |
| 128 线程满载 | — | CPU 稳定 81–83 °C @ 14,000–17,000 RPM；DIMM/NVMe 全程清凉 |
| 崩溃（SIGKILL） | — | 立即交还 BMC → 3 秒后自动重启 → 约 1 分钟回到空闲转速 |

满载 2×200 W 本来就需要这个风量——想让满载也安静只能限制 CPU 功耗（BIOS 的 cTDP/PPT）。

## 安装

在 **Ubuntu 24.04**（内核 6.8，`ipmitool` 1.8.19，Python 3.12）上开发并验证。

```bash
# 服务器上
sudo apt install -y ipmitool

# 把仓库放到 /opt/fan-control
git clone https://github.com/snowspig/fan-control-lenovo-sr665-ipmi.git /tmp/fan-control
sudo cp -a /tmp/fan-control /opt/fan-control
sudo chown -R "$USER":"$USER" /opt/fan-control
rm -rf /tmp/fan-control

# 一次性安装（幂等）：fanctl 服务用户、ipmi 组、udev 规则、systemd 单元
sudo bash /opt/fan-control/deploy/install.sh

# 让你的登录用户也能免 sudo 使用 fanctl（重新登录生效）
sudo usermod -aG ipmi "$USER"
```

安装**不会**启用服务——必须先完成 Phase 0。

权限模型：服务以专用的 `fanctl` 用户（`nologin`）运行，靠 udev 规则把
`/dev/ipmi0` 授权给 `ipmi` 组。**不需要 root，也没有任何 sudoers 条目。**

## 首次使用必做 Phase 0

传感器编号、LUN、换算系数、命令字节标度都是**固件相关**的——本文档里的数值
只对"这台" SR665 V1（XCC 3.01）成立。换机器或升级 XCC 固件后必须重新标定，
否则轻则读错温度，重则把风扇调到失速区。

机器空闲时，按顺序执行（每条都等约 20 秒让风扇稳定再读数）：

```bash
# 1) 传感器交叉校验：快速 raw 路径与 SDR 慢路径逐点一致才继续
fanctl verify

# 2) 只读查看：三类温度 + 6 个风扇转速
fanctl status

# 3) 逐档试探，记录每档的转速（命令作用于全部风扇，等 20 秒）
fanctl set 60 && sleep 20 && fanctl status
fanctl set 40 && sleep 20 && fanctl status
fanctl set 20 && sleep 20 && fanctl status   # …继续往下直到转速 < 1500 RPM

# 4) 找到"前后转子都 ≥ 1500 RPM"的最低字节 → hard_floor_duty

# 5) 交还 BMC（随时可用，也是回退手段）
fanctl auto
```

把 `hard_floor_duty`、`expected_rpm`（字节→转速表）、`min_duty`（空闲目标）
填进 `/etc/fanctl/config.toml`，然后：

```bash
sudo systemctl enable --now sr665-fanctl
journalctl -u sr665-fanctl -f     # 应看到 duty 100 -> 84 -> ... -> 30
```

## 配置

`/etc/fanctl/config.toml`（模板：[deploy/config.toml](deploy/config.toml)，改完重启服务生效）：

| 参数 | 本机值 | 含义 |
| --- | --- | --- |
| `min_duty` | 30 | 空闲目标（≈3,040 RPM），静音/风量的取舍 |
| `hard_floor_duty` | 8 | 硬件下限（≈1,600 RPM），Phase 0 实测 |
| `safe_duty` | 100 | 服务启动的初始值（≈8,000 RPM） |
| `cpu_curve` | 55/65/75/85 °C → 30/60/100/255 | CPU Tctl 曲线 |
| `dimm_curve` | 70/78/85 °C → 30/60/255 | DIMM 曲线（JEDEC 上限 85 °C） |
| `nvme_curve` | 55/65/72/78 °C → 30/60/100/255 | NVMe 曲线（990 PRO crit ≈ 85 °C） |
| `max_fall_per_cycle` | 16 | 每 5 秒最多降 16 字节（上升不限速） |
| `emergency_c` | cpu 85 / dimm 85 / nvme 78 | 触发字节 255 的阈值 |
| `[tach]` / `[dimm]` / `[nvme]` | 见模板 | 传感器编号、LUN、换算系数 |

更新流程：改版本库里的 `deploy/config.toml` → 复制到 `/etc/fanctl/` → 重启服务。

调参提示：如果空闲时 CPU 温度正好落在曲线第一个拐点附近，控制器会在两档之间
小幅"找平衡"（本机约每 3–5 分钟在字节 38–44 间切换，幅度约 400 RPM）。想更安静就把
`cpu_curve` 的第一个拐点抬高几度（例如 55 → 62 °C），代价是空闲温度更高一些。

## 命令

| 命令 | 作用 |
| --- | --- |
| `fanctl status` | 三类温度、6 风扇转速、曲线建议字节（只读） |
| `fanctl status --nvme` | 额外查询 8 块 NVMe 的 SMART 温度（每块约 3 秒） |
| `fanctl set <byte>` | 手动设定速度字节（钳位到 `hard_floor_duty`） |
| `fanctl auto` | 交还 BMC 自动控制（幂等，回退手段） |
| `fanctl verify` | 传感器交叉校验（`run` 启动时也会自动做） |
| `fanctl run --dry-run --once` | 打印将下发的命令，不碰硬件 |
| `fanctl run` | 常驻闭环（systemd 就是跑这个） |

## 安全设计

- **故障方向恒定**：任何故障（传感器失效、命令被拒、进程崩溃）的结果都是"更快"，不是"更慢"。
- **启动闸门**：先用慢路径（`ipmitool sensor list`，~8 s）取权威读数，再与快速 raw 路径
  逐点比对（风扇容差 max(200 RPM, 5%)，温度 3 °C）。不一致等 20 秒重试 2 次
  （容忍风扇爬坡的瞬态），仍不一致就拒绝启动——读错 LUN 会静默拿到**别的传感器**的值
  （实测：同一编号在 LUN 0 读出的是无关读数，甚至 −40 °C）。
- **转速回读**：每周期用上一轮的转速校验写入；真正的停转（转速 0）会被抓到。
  最高速时 BMC 偶尔把 tach 报成"不可用"，缺读数不算故障。
- **交还 BMC** 是安全的驻留态：BMC 自己的策略（~8.9k RPM）比任何手动值都保守。
  服务停止/崩溃时 `ExecStopPost` 自动执行。
- 最小权限：`fanctl` 用户 + `ipmi` 组 + udev 规则，无 sudoers，systemd 加固
  （`NoNewPrivileges`、`ProtectSystem=strict`、`DevicePolicy=closed` 等）。

## 已知限制

- **NVMe 温度有约 30 秒的滞后**：装了 HighPoint 驱动后内核不再暴露 NVMe 温度，
  只能通过 `hptsvr`（`hptraidconf`）逐块查询，一块约 3 秒、串行。后台线程一轮
  8 块约 24–32 秒，控制环只读"新鲜"值（超过 90 秒的读数作废）。
- **HighPoint / MegaRAID / HDD 的温度不在闭环内**。本机这样没问题（纯 CPU 负载时它们都凉），
  但如果你的机器装了 GPU 或满配 HDD，请自行评估。
- **整机挂死时控制器也停**（用户态程序的固有局限）。风扇停在最后一次下发的字节。
  可选缓解：IPMI watchdog 让 BMC 在挂死时复位主机（本项目未做）。
- **升级 XCC 固件后必须重跑 Phase 0**（命令、字节标度、传感器布局都可能变）。

## 踩过的坑

移植或改写时最容易栽进去的几件事（全部实测踩过）：

1. **`hptraidconf` 绝不能从 stdin 喂命令**：交互模式在 EOF 处死循环刷屏，
   15 分钟写了 2.4 GB 到 `/tmp`。只允许 argv 单条模式 + 硬超时 + 输出大小上限。
2. **`ipmitool sensor list` 要走约 8 秒**（遍历约 300 条 SDR 记录）。闭环里用
   raw `Get Sensor Reading`（`ipmitool -l <lun> raw 0x04 0x2d <编号>`，一条消息 ~0.08 s）。
3. **同一传感器编号在不同 LUN 是不同传感器**：本机 DIMM 在 LUN 1；在 LUN 0 读同一个
   编号会拿到无关读数。所以传感器身份 =（编号, LUN），且启动时交叉校验。
4. **风扇换速要 15–20 秒才稳定**：写完立即读转速会得到中间值；任何"两次读数对比"的
   校验都必须等稳态，否则误报。
5. **速度字节不是"最大转速的百分比"**：它只是发给 BMC 的请求值（0–255），实际 RPM
   由风扇特性决定。本机 ≈ 1000 + 68 × 字节；别拿别人的"百分比"直接套。
6. **上游仓库的 OEM 命令是机型专用的**：SR655 V1 的 `0x3c 0x30` 在 SR665 V1 上是
   `0xc1 Invalid command`。同一厂商不同型号都不通用，务必实测。

## 参考

以下引用内容均已逐字核对：

- **Lenovo ThinkSystem SR665 产品手册**（Lenovo Press,
  [lp1269.pdf](https://lenovopress.lenovo.com/lp1269.pdf)）——"up to six hot-swap
  redundant fans"；"Standard fans (single-rotor 17K RPM) or Performance fans
  (dual-rotor 18K RPM)"（本机 18,400 RPM 满速与双转子 Performance 风扇一致）。
- **XCP-ng 论坛：High Fan Speed Issue on Lenovo ThinkSystem Servers**
  （[第 5 页](https://xcp-ng.org/forum/topic/8548/high-fan-speed-issue-on-lenovo-thinksystem-servers/55?page=5)）
  ——`raw 0x3a 0x07 0xff <字节> 0x01` 的公开出处（用户 Riven 从 Lenovo 获得的信息，
  在 SR635 **V3** 上演示；帖内转述 Lenovo 警告："NOT SUPPORTED/RECOMMENDED -
  and could cause thermal damage to internal components"）。本仓库的贡献：
  在 **SR665 V1**（XCC 3.01）上实测验证该命令、测出字节→RPM 标定表、发现
  模式字节 `0x00` 可直接交还 BMC 自动（帖中认为需要重启 XCC）。
- **[fyr233/fan-control-sr655](https://github.com/fyr233/fan-control-sr655)**——
  思路来源（SR655 V1 + GPU 场景）；本仓库是其思路的 SR665 重写，命令、传感器布局、
  安全机制均不同。

## 许可

[MIT](LICENSE)。本工具与 Lenovo 无关，不受 Lenovo 支持或背书。
