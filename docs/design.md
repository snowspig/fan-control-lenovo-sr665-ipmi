# SR665 风扇降噪控制器 — 设计规格

- 日期：2026-11-25
- 状态：设计已获用户批准，待实施
- 机器：Lenovo ThinkSystem SR665 V1（2× EPYC 7532，Performance 双转子风扇，XCC 3.01）

---

## 1. 背景与目标

### 1.1 问题

这台 SR665 放在家里使用，但风扇噪音不可接受。实测确认：**噪音不是温度逼出来的，而是 BMC 的空闲转速下限**。

空闲基线（负载 `0.00`）：

| 指标 | 值 |
| --- | --- |
| 环境温度 | 25.0 °C |
| 排气温度 | 30.0 °C |
| CPU 1 / CPU 2 | 39 °C / 39 °C（DTS −51 ⇒ Tjmax = 90 °C） |
| DIMM 1 / 16 / 17 / 32 | 30 / 31 / 31 / 33 °C |
| 6 个风扇 | 前转子 8260–8860 RPM，后转子 8420–8880 RPM |
| System Air Flow | 72 CFM |
| Sys Fan Pwr | 35 W |

也就是说：在什么都不干的时候，XCC 仍把 6 个双转子（performance）风扇顶在 ~8.5k RPM。按 `55·log10(8500/4000) ≈ 18 dB` 粗估，把空闲转速压到 4k RPM 附近，人耳感知响度大致减半。

### 1.2 目标

| 项 | 目标 |
| --- | --- |
| 摆放 | 同一房间，需尽量安静 |
| 负载形态 | 大部分时间空闲，偶尔短时回测/编译（用户确认） |
| 空闲转速 | 3–4k RPM（Phase 0 实测后折算为占空比下限 `min_duty`） |
| 负载响应 | 温度上升时立即拉高，不设延迟 |
| 温控对象 | **CPU、内存(DIMM)、SSD(NVMe)** —— 其它部件不参与闭环（用户确认） |

### 1.3 非目标

- 不升级 XCC / BIOS 固件。
- 不改动 RAID、NVMe 上的任何数据，不做分区、分区表、阵列操作。
- 不做 per-fan 分区控制（SR665 的 6 个风扇是同一条 N+1 横向风道，分区无收益）。
- 不引入第三方 Python 依赖（服务器 Python 3.12.3，标准库足够）。
- 不新增 `NOPASSWD` sudo 规则，不以 root 身份常驻运行。

---

## 2. 已验证的环境事实

| 项目 | 值 | 来源 |
| --- | --- | --- |
| 整机 | Lenovo ThinkSystem SR665 V1，2U，主板 `7D2VCTO1WW` | `/sys/class/dmi/id/product_name` |
| BIOS | `D8E116E-2.01`（2021-03-04） | `/sys/class/dmi/id/bios_version` |
| CPU | 2× AMD EPYC 7532（32C/64T each），503 GiB RAM + 8 GiB swap | `lscpu` / `free` |
| 内存 | 4×128 GB RDIMM，插在槽 **1 / 16 / 17 / 32**；其余槽温度 `na` 属正常（空槽） | `ipmitool sensor list` |
| 存储 | 8× Samsung 990 PRO 1TB（HighPoint SSD7540，`51:00.0`）→ `nvme0n1`…`nvme7n1` | `lsblk` / sysfs |
| 系统盘 | Broadcom MegaRAID SAS3508（`81:00.0`）上的 HDD 阵列 | `lspci` |
| GPU | **无**，仅 Matrox 板载 VGA | `lspci` |
| BMC | Lenovo XCC，IPMI 2.0，固件 3.01，Product ID `0x566` | `ipmitool mc info` |
| 带内 IPMI | `/dev/ipmi0`（`ipmi_ssif` + `ipmi_si`），当前 `crw------- root root` | `ls -l /dev/ipmi0` |
| BMC 网口 | 独立管理网口（与主机内网不同网段）。本方案只走带内 `/dev/ipmi0`，不依赖管理口地址 | `ipmitool lan print` |
| 内核传感器 | `k10temp` ×2（Tctl = `temp1_input`）；**NVMe 的 hwmon 芯片已随 HighPoint 驱动消失**（盘被 `hptnvme` 接管，`/dev/nvmeX` 字符设备也不存在） | sysfs |
| IPMI 传感器寻址（实测） | 风扇前转子 `0x41–0x46`、后转子 `0x49–0x4e`，**LUN 0**；DIMM n = `0x50+n`，**LUN 1**；同号不同 LUN 是**不同传感器**（LUN 0 读 0x51 会返回别的传感器甚至 0xcb） | `ipmitool -v sdr type Fan/Temperature` |
| IPMI 换算系数（实测） | 前转子 **M=80**（8240=103×80、8560=107×80）；后转子 **M=74**（8880=120×74、8510=115×74）；温度 M=1 | 配对 `sensor list` 与 raw 读数拟合 |
| 读取耗时（实测） | 单条 raw 读 0.08 s；16 条 ≈ 1.3–3.5 s；`ipmitool sensor list` 走 ~300 条 SDR 记录，**7.7 s**（不可用于 5 s 闭环） | 计时 |
| NVMe 温度（HighPoint） | `hptraidconf -u RAID -p hpt query devices 1/E1/n` 返回 `Temperature(C)` 与完整 SMART；单条 ~3 s，**hptsvr 串行处理**，8 条一轮 ≈ 24 s；hptsvr 监听 **TCP 0.0.0.0:7402** | 实测 |
| 990 PRO 临界温度 | 复合温度阈值 ≈ 84.85 °C（hwmon `crit` = 84850；`max` 读出的 65261850 是 SMART 超时字段被误读，**不是温度阈值**） | sysfs |
| 风扇 tach 下限 | IPMI 记录的 lower-critical = 960 RPM | `ipmitool sensor list` |
| SEL | 无风扇故障事件；历史有 PSU 2 AC lost（已恢复，现双 PSU 正常）；BMC 时钟未同步 | `ipmitool sel elist` |

### 2.1 与上游仓库的差异（决定了不能照抄）

参考仓库：[`fyr233/fan-control-sr655`](https://github.com/fyr233/fan-control-sr655)（作者声明仅适配 SR655 V1，README 自带"有导致硬件永久损坏的风险"警告，**无 LICENSE 文件**）。

上游实际行为（读 `main.py` 确认）：

- 命令：`ipmitool raw 0x3c 0x30 0x00 <fan_id> <duty>`，`fan_id` 取 `0x01`–`0x06`；退出时对 `fan_id = 0x00` 下发 50%（即作者在 SR655 V1 上把 fan 0 当"全部风扇"用）。
- 曲线最低 5%（≤40 °C），无滞回、无限速，每 5 s 按瞬时温度逐风扇重设。
- 写入失败只打印；传感器读取失败只打印后继续睡——**没有任何倒向安全的动作**。
- 没有回读 tach，命令被 BMC 忽略时无感知。
- **没有"交还 BMC 自动"的命令**。
- 推荐做法是 `NOPASSWD: /usr/bin/ipmitool`（等于给用户整条 ipmitool 的免密权限）。

| 维度 | 上游 | 本机 | 后果 |
| --- | --- | --- | --- |
| 机型 | SR655 V1 | **SR665 V1** | OEM 命令语义需实测确认 |
| 传感器 | `amdgpu`（GPU 温度）为主 | **无 GPU** | 照抄会让风扇 1–3 按"没有 GPU"降速，CPU/SSD 位缺风 |
| 温度来源 | `sensors -j` 文本输出 | 直接读 sysfs hwmon + IPMI | 少一层依赖，不受 `sensors` 格式影响 |
| 内存温度 | 不监控 | DIMM 进闭环 | 用户要求 |
| 反馈 | 无 | **回读 tach 校验** | 上游最大的隐患 |

**决策：重写而非 fork。** 上游配置写死、缺失反馈校验与兜底，且无许可证（衍生再分发在法律上不干净）。重写一个仅标准库、核心逻辑约 300 行的小包。

---

## 3. 架构

一个独立的小型控制层，不改固件、不碰数据盘。

```
  ┌────────────────── 每 5 秒 ──────────────────┐
  │ sysfs hwmon:   CPU1/CPU2 Tctl (k10temp)        │
  │ IPMI raw 读:   风扇前/后转子 ×12、DIMM ×4      │
  │                (每条一条 IPMI 消息，约 0.08 s)    │
  │ 后台线程:      NVMe ×8 温度 (hptraidconf, ~3 s/块)│
  └───────────────────┬─────────────────────────┘
                      ▼
   三条曲线各自求值 → 取 max → 滞回 → 下降限速 → 目标占空比
                      ▼
     ipmitool raw 0x3c 0x30 0x00 <fan_id> <duty>
                      ▼
     回读 Fan N Front/Rear Tach ──► 校验命令是否真的生效
```

**启动闸门**：服务启动时先用慢路径（`sensor list`，7.7 s）取一份权威读数，再与快速 raw 路径逐点比对（风扇容差 max(200 RPM, 5%)，温度容差 3 °C）。任何不一致都**拒绝启动**——因为读错 LUN 或换算系数会静默返回别的传感器的值（例如 DIMM 温度 −40 °C），这是温控闭环里最危险的错误。

**但闸门必须容忍"风扇正在爬坡"**：两次采样相隔约 10 s，而风扇换速需要 15–20 s 才稳定。崩溃后 `ExecStopPost` 会把风扇交还 BMC，重启后的第一次校验正好落在爬坡窗口，会产生**误报**（实测：fan 3–6 的 raw 8880 vs reference 7680）。所以校验不一致时等待 20 s 重试，连续 3 次不一致才拒绝启动（`VERIFY_RETRIES = 2`）。

**DIMM 槽位发现**：启动时按 `base_number + n` 逐槽试读（空槽返回 0xcb），把应答的槽位记下来，之后每周期只读这些槽。

### 3.1 分层与边界

| 层 | 模块 | 职责 | 不得越界 |
| --- | --- | --- | --- |
| 读 | `sensors.py` | 从 sysfs 读 CPU Tctl；把 IPMI/HighPoint 读数整理成 `Temps` | 不做决策、不写硬件 |
| 读 | `ipmi.py` | 封装 `ipmitool`：写占空比、raw 读传感器、慢路径参考读 | 不做决策 |
| 读 | `hpt.py` | 封装 `hptraidconf`：读 NVMe SMART 温度 | 不做决策 |
| 读 | `nvme.py` | 后台线程逐块轮询 NVMe（hptsvr 串行，~3 s/块），只暴露新鲜读数 | 不阻塞控制环 |
| 决策 | `curve.py` | **纯函数**：温度 → 占空比；滞回、限速、tach 校验 | 不碰 I/O，可完全单测 |
| 编排 | `controller.py` | 一个循环：读 → 校验上一轮 → 判 → 写 → 记日志 | 不含业务规则细节 |
| 校验 | `verify.py` | 快速 raw 路径 vs 慢速 SDR 路径的逐点交叉校验 | 只读、不决策 |
| 配置 | `config.py` | dataclass 默认值 + `/etc/fanctl/config.toml` 覆盖 | 无副作用 |

每个周期的调用顺序：`ipmi.read_sensors()`（一次 `sensor list`，取 DIMM 温度 + tach）→ `sensors.read_temps()` → 用本次读到的 tach 校验**上一周期**写入的占空比 → `decide()` → `ipmi.set_duty()`。这样每个周期只有一次读、一次写；tach 校验滞后一个周期（5 s），对安全判断没有影响。`fanctl status` 会打印单次 `sensor list` 的耗时；若日后超过约 3 s，再改用 `sdr type Fan` + `sdr type Temperature` 两条窄命令。

### 3.2 阶段与闸门

| 阶段 | 内容 | 闸门（不满足即停） |
| --- | --- | --- |
| **Phase 0 探针** | 空闲时对单个风扇试 OEM 命令，测出 duty→RPM 表、硬件下限、fan 0 语义、覆盖能否被 BMC 重置清除 | 命令被固件拒绝，或 BMC 重置清不掉覆盖 ⇒ 停止，与用户重新评估 |
| **Phase 1 控制器** | 闭环 + 兜底 + tach 校验 + systemd 单元 | 单元测试全绿 |
| **Phase 2 调参** | 按耳朵与温度调 `config.toml`；空载 30 min + 5 min 负载验证 | 空闲达标且三类温度不越线 |

---

## 4. 组件

包名 `sr665_fanctl`，Python 3.12，**仅标准库**。

### 4.1 模块

| 模块 | 关键接口 | 说明 |
| --- | --- | --- |
| `sensors.py` | `read_temps(hwmon_root: Path, ipmi: IpmiSensors \| None) -> Temps`<br>`read_socket_temps(hwmon_root) -> tuple[float \| None, float \| None]`<br>`read_nvme_temps(hwmon_root) -> list[float]` | `Temps(cpu1, cpu2, nvme_max, nvme_count, dimm_max, dimm_count)`。CPU/NVMe 读 sysfs，DIMM 取自 `ipmi`；`ipmi=None`（读取失败）时 DIMM 记为不可读 |
| `ipmi.py` | `IpmiClient.set_duty(fan_id: int, duty: int) -> None`<br>`IpmiClient.read_sensors() -> IpmiSensors` | `IpmiSensors(tach: dict[int, tuple[int, int]], dimm_temps: dict[int, float])`；每次子进程调用 `timeout=10` |
| `curve.py` | `duty_for_temp(t: float, curve: tuple[tuple[float, int], ...]) -> int`<br>`decide(state: State, temps: Temps, cfg: Config) -> Decision` | `Decision(duty: int, reason: str)`；纯函数 |
| `controller.py` | `Controller.run_cycle() -> Decision`<br>`Controller.run_forever() -> None` | 循环与故障处理（§6） |
| `config.py` | `Config`（dataclass）+ `load_config(path: Path \| None) -> Config` | 曲线点、阈值、限值、周期；加载时校验 |
| `__main__.py` | CLI 入口 | 见 4.2 |

### 4.2 CLI

| 命令 | 用途 | 改变硬件状态 |
| --- | --- | --- |
| `fanctl status` | 打印三类温度、6 个风扇 tach、当前曲线建议占空比 | 否 |
| `fanctl set <duty>` | 写速度字节（1..255）；钳位到 `[hard_floor_duty, 255]`。**没有单风扇模式**：这台 XCC 只接受 `0xff`（全部风扇）形态 | **是** |
| `fanctl auto` | 交还 BMC 自动控制（执行 `raw 0x3a 0x07 0xff 0x00 0x00`，Phase 0 已验证） | **是** |
| `fanctl run [--dry-run] [--once] [--config PATH]` | 常驻循环；`--dry-run` 只打印将要执行的命令 | 是（`--dry-run` 否） |

### 4.3 传感器发现规则（关键实现约束）

- **按 hwmon `name` 与设备路径发现，绝不按 hwmon 序号索引**。`hwmon8` 是本次启动的产物，重启后可能变成 `hwmon2`；按序号读会静默读错设备。
- CPU：`/sys/class/hwmon/hwmon*/name` == `k10temp` → 读 `temp1_input`（Tctl）。要求恰好找到 2 个；否则按传感器故障处理（§6）。两路按 PCI 地址排序（本机 `0000:00:18.3`、`0000:00:19.3`），保证 cpu1/cpu2 不随 hwmon 序号变化。
- NVMe：**实测（2026-11-25，装了 HighPoint 驱动后）** 盘被 `hptnvme` 接管，内核不再注册 `nvme` hwmon 芯片（`/sys/class/nvme` 为空）。因此 NVMe 温度改从 `hptsvr` 取（见 `hpt.py`/`nvme.py`）；`sensors.py` 仍保留 `name == "nvme"` 的 sysfs 发现，以便将来换回内核 `nvme` 驱动时自动生效。
- DIMM：`ipmitool sensor list` 中名字匹配 `DIMM <N> Temp` 且读数不是 `na` 的行。空槽 `na` 属正常，不算故障。
- **基线在启动时学习**：首次成功读取时记下 NVMe 数量（本机 8）与 DIMM 可读条数（本机 4），之后与之比较。

### 4.4 权限模型（最小权限，不使用 root / sudoers）

| 组件 | 做法 |
| --- | --- |
| 设备访问 | udev 规则 `KERNEL=="ipmi*", MODE="0660", GROUP="ipmi"`；新建系统组 `ipmi`、系统用户 `fanctl`（`nologin`，主组 `ipmi`） |
| 服务身份 | systemd `User=fanctl`、`Group=ipmi` |
| 加固 | `NoNewPrivileges=true`、`ProtectSystem=strict`、`ProtectHome=true`、`PrivateTmp=true`、`DevicePolicy=closed` + `DeviceAllow=/dev/ipmi0 rw`、`CapabilityBoundingSet=`（清空） |
| 开发期便利 | 探针阶段执行一次 `sudo usermod -aG ipmi <user>`（重新登录生效），使登录用户可免 sudo 运行 `fanctl` |
| 明确不做 | 不给登录用户配 `NOPASSWD`；服务不持有任何 root 能力 |

---

## 5. 控制策略

### 5.1 输入与合成

三类部件舒适区间不同，所以**不是**对"最热温度"套一条曲线，而是 CPU / DIMM / NVMe 各用自己的曲线求值，目标占空比 = 三者最大值。

### 5.2 曲线（暂定值，Phase 2 在 `config.toml` 内调整）

| 传感器 | 取值 | 拐点（温度 °C → 占空比 %） | 依据 |
| --- | --- | --- | --- |
| CPU Tctl | `max(cpu1, cpu2)` | 55→`min_duty`，65→45，75→75，85→100 | Tjmax 90 °C，留 5 °C 缓冲 |
| DIMM | 已插条数的最大值 | 70→`min_duty`，78→50，85→100 | DDR4 RDIMM JEDEC 上限 85 °C |
| NVMe 复合温度 | 8 块的最大值 | 55→`min_duty`，65→50，72→80，78→100 | 驱动 crit 84.85 °C |

拐点定在 55 °C 的原因：降速后空闲温度本身会升到 45–50 °C，拐点若低于该值，控制器会自行把转速顶回去，达不到降噪目的。

### 5.3 两个下限（含义不同，不能合并）

| 参数 | 含义 | 取值 |
| --- | --- | --- |
| `hard_floor_duty` | **硬件下限**：Phase 0 实测的"前后转子都 ≥ 1500 RPM 的最低速度字节"。任何路径都不得低于它 | **8**（约 1600 RPM） |
| `min_duty` | **策略下限**：为了让空闲转速落在 3–4k RPM 而做的静音/风量取舍 | **30**（约 3040 RPM） |

**占空比标度**：控制器内部用的"占空比"就是发给 BMC 的**速度字节**（1..255，0 保留给"交还自动"），实测映射见 `docs/probe-results.md`。不是 0–100 的百分比——`0x64`(100) 只有约 8000 RPM，真正满速是 `0xff`(255) ≈ 18400 RPM。

`load_config()` 校验 `hard_floor_duty ≤ min_duty ≤ safe_duty ≤ 255`，不满足直接报错退出。

### 5.4 滞回与限速

| 参数 | 值 | 精确定义 |
| --- | --- | --- |
| 温度滞回 | 2 °C | 每条曲线记住上次求值温度 `t_prev`；若 `abs(t − t_prev) < 2` 且未触发紧急，沿用该曲线上次的占空比，否则重算 |
| 上升 | 不限速 | 新目标高于当前 ⇒ 本周期立即生效 |
| 下降 | 每周期最多 8 点 | 新目标低于当前 ⇒ 本周期只降 8 点，剩余留待后续周期（100→30 约 45 s） |
| 启动值 | `safe_duty`（100） | 服务启动后的起点，再按限速逐周期下降。崩溃重启循环时风扇因此保持在 100 字节（约 8000 RPM）以上 |
| 禁用值 | — | 绝不发送 `0`（那是"交还自动"），绝不发送低于 `hard_floor_duty` 的值 |

### 5.5 紧急全速

任一条件成立 ⇒ 本周期直接 255（≈18400 RPM，BMC 能达到的最高速），记 WARNING，忽略滞回与限速：CPU Tctl ≥ 85 °C，或 DIMM ≥ 85 °C，或 NVMe ≥ 78 °C。

**tach 缺失不算故障**：在最高速时 BMC 偶尔把 tach 报成不可用（0xFF），风扇其实在转；真正的停转会报 0，会被转速下限判定抓到。缺读数只记 WARNING。

---

## 6. 故障处理

**总原则：所有故障都倒向"更吵"，绝不倒向"更静"。**

| 故障 | 检测 | 行为 |
| --- | --- | --- |
| 某类温度（CPU / DIMM / NVMe 任一）单周期读不到 | 该类无有效读数 | 该类按"未知"处理：本周期不因其降速；目标取其余类别结果与上周期占空比的较大值；记 WARNING |
| 某类温度连续 3 个周期读不到 | 计数 | 100%（感知不到温度就全速） |
| DIMM 可读条数低于启动基线 | 比较基线 | 记 WARNING，继续用剩余条数参与曲线 |
| NVMe 数量低于启动基线 | 比较基线 | 记 WARNING（掉盘），不作为升温处理 |
| IPMI 读取失败（非零退出 / 超时） | 退出码、`TimeoutExpired` | 视同 DIMM 与 tach 读不到，走上面两条计数规则 |
| NVMe 轮询失败（hptsvr 停止 / 超时 / 空输出） | `HptError`，`NvmeRefresher.last_error` | 该块旧读数在 `stale_s`（90 s）后过期 ⇒ 数量下降，记 WARNING；全部过期 ⇒ NVMe 类不可读 ⇒ 连续 3 周期后 100% |
| 启动交叉校验不一致 | `verify.conversion_problems()` 非空 | **拒绝启动**，逐条打印 MISMATCH；`--skip-verify` 仅用于调试 |
| IPMI 写入失败 | 退出码、非零 completion code | 立即重试 1 次；仍失败 ⇒ 以非零码退出，由 systemd 重启 |
| tach 校验不过：任一风扇 `min(前, 后)` 低于 `max(预期 RPM × 50%, 1500)`，连续 2 个周期 | 对照 duty→RPM 表 | 重发命令；仍不过 ⇒ 100% + ERROR 日志（风扇停转或命令被忽略） |
| 进程崩溃 / 被 stop | systemd | `Restart=always`、`RestartSec=3`；`ExecStopPost` 把风扇置 50% |

### 6.1 duty→RPM 预期表

Phase 0 实测填充（`config.toml` 的 `expected_rpm`，线性插值）。表为空时，校验退化为仅判断 `min(前, 后) ≥ 1500 RPM`。

### 6.2 交还 BMC 自动控制

**已验证**：`raw 0x3a 0x07 0xff 0x00 0x00`（模式字节 `0x00`）让 BMC 立刻恢复自己的自动策略（实测回到 8880–9546 RPM）。`fanctl auto` 执行的就是它；systemd 的 `ExecStopPost` 在服务停止/崩溃时也会执行它。

---

## 7. 部署

| 项 | 位置 |
| --- | --- |
| 开发 | 本仓库（独立 git 仓库） |
| 服务器代码 | `/opt/fan-control`（属主为登录用户，免 sudo 可更新） |
| 配置 | `/etc/fanctl/config.toml`（`install.sh` 首次生成，之后手工调） |
| 入口 | `/usr/local/bin/fanctl`（`install.sh` 生成的小包装脚本） |
| systemd 单元 | `/etc/systemd/system/sr665-fanctl.service` |
| udev 规则 | `/etc/udev/rules.d/99-ipmi-group.rules` |

### 7.1 首次部署流程

```text
1. 本机：`scp -r` 仓库到 `<user>@<server>:/opt/fan-control`
2. 服务器：sudo bash /opt/fan-control/deploy/install.sh   # 建组/用户/udev/单元，幂等
3. 服务器：sudo usermod -aG ipmi <user>                     # 重新登录生效
4. fanctl status                                          # 只读冒烟
5. Phase 0 探针（见 §9）
6. sudo systemctl enable --now sr665-fanctl               # Phase 0 通过后才执行
```

### 7.2 日常更新流程

本机改代码 → 单元测试 → `scp` 到 `/opt/fan-control` → `sudo systemctl restart sr665-fanctl`。

暂不建 GitHub 仓库（属于对外发布，需用户单独确认）；跑通后再议。

### 7.3 回退

```bash
sudo systemctl disable --now sr665-fanctl   # ExecStopPost 把控制权交还 BMC
sudo ipmitool mc reset cold                  # 或 fanctl auto（若 Phase 0 验证出路径）
```

---

## 8. 测试

### 8.1 单元测试（本机 pytest，Windows）

| 测试对象 | 覆盖点 |
| --- | --- |
| `curve.py` | 分段线性插值与端点钳位；三条曲线取 max；滞回（<2 °C 沿用旧值）；下降限速（每周期 ≤4）；上升不限速；紧急阈值绕过滞回与限速；启动值 50 |
| `sensors.py` | 假 sysfs 树（`tmp_path`）：按 `name` 找到 `k10temp`；hwmon 序号打乱后仍正确；k10temp 只有 1 个 ⇒ 故障；NVMe 缺失/减少；DIMM `na` 视为空槽；基线学习 |
| `ipmi.py` | 用服务器真实输出做样本（`tests/fixtures/ipmitool_sensor_list.txt`，逐字拷贝自 `~/ipmi_sensors.txt`）解析 DIMM 温度与前后转子 tach；命令行构造；非零退出 ⇒ `IpmiError`；超时 ⇒ `IpmiError` |
| `controller.py` | 传感器失败：保持 → 3 周期后 100%；写入失败重试 1 次后退出；tach 校验 2 周期不过 ⇒ 100%；钳位到 `hard_floor_duty` |
| 端到端 | 假 sysfs + 假 `ipmitool`，喂温度序列，断言下发的占空比序列 |

**绿灯规则**：引用测试运行前先确认 `module.__file__` 指向被改的那份副本，并在报告中说明测试覆盖到的行为。

### 8.2 服务器验收

1. `fanctl status` 只读冒烟：三类温度能读出、6 个风扇 tach 齐全。
2. `fanctl run --dry-run --once`：打印将执行的命令，不下发。
3. Phase 0 探针（§9）。
4. 服务常驻：空闲 10 min，记录占空比与 RPM，与目标 3–4k RPM 比对。
5. 负载验证：`stress-ng --cpu 128 --timeout 5m` 期间记录 CPU/DIMM/NVMe 温度，确认不越 85/85/78。
6. 故障注入：`kill` 进程，确认风扇回到 50% 且服务自动重启。

---

## 9. Phase 0 探针规程

前置：机器空闲（负载 < 0.2）。**每一步只动一个风扇，每步观察 ≤ 60 s；任一温度 > 60 °C 或出现异常噪声/异味，立即 `fanctl set 100 --fan 1` 并中止。**

| 步 | 操作 | 记录 | 判据 |
| --- | --- | --- | --- |
| 0 | `fanctl verify` | 交叉校验结果 | 必须输出 `sensor paths agree`；否则先解决问题再继续 |
| 0b | `fanctl status --nvme` | 6 个风扇前后转子基线 RPM、三类温度（含 8 块 NVMe） | 与 §1.1 基线同量级；8 块盘温度合理（本机 33–39 °C） |
| 1 | `fanctl set 40 --fan 1` | completion code、Fan 1 前后转子 RPM、其余 5 个风扇变化 | 返回码 0 且 Fan 1 转速下降 ⇒ 单风扇命令有效 |
| 1b | 若步 1 被拒：`fanctl set 50`（fan 0 = 全部，上游语义） | 同上 | 6 个风扇同步变化 ⇒ fan 0 语义有效；仍被拒 ⇒ **No-Go** |
| 2 | Fan 1 逐档 `set 30 / 25 / 20 / 15 / 10` | 每档前后转子 RPM | 前后转子都 ≥ 1500 RPM 的最低档 ⇒ `hard_floor_duty`；得到 duty→RPM 表 |
| 3 | `fanctl set 50 --fan 1` | RPM 是否回到基线附近 | 验证占空比刻度为 0–100%，并得出 BMC 空闲策略的等效占空比 |
| 4 | 用户执行 `sudo ipmitool mc reset cold` | BMC 恢复后 Fan 1 的 RPM 是否回到与其余风扇一致的自动值 | 回到自动 ⇒ `fanctl auto` 的实现就是它，**Go**；清不掉 ⇒ **No-Go**，改用整机断电恢复后重新评估 |
| 5 | 记录到 `docs/probe-results.md` | duty→RPM 表、`hard_floor_duty`、等效占空比、恢复路径 | Phase 1 配置的来源 |

---

## 10. 残余风险（已知并接受）

1. **内核挂死时控制器停止调节**，风扇停在最后一次下发的占空比。这是用户态控制器的固有局限；BMC 是否在覆盖生效期间接管未知。缓解：可选硬件 watchdog（`ipmi_watchdog`）让 BMC 在挂死时复位主机——v1 不做，记为后续项。
2. **NVMe 温度的新鲜度有限**：hptsvr 串行处理查询，一块盘 ~3 s，8 块一轮 ~24–32 s，因此任何一块盘的读数最多约 32–90 s 旧。990 PRO 的固件自身在 ~82 °C 会降速保护，这条滞后只影响风扇提前量，不影响 SSD 自身保护。
3. **NVMe 温度源依赖 hptsvr 服务**：hptsvr 停止/重启期间 NVMe 不可读，`stale_s` 过后控制器按"感知不到温度 ⇒ 全速"处理（更吵，但安全）。Phase 0 需确认 `fanctl` 用户（非 root）能调用 `hptraidconf`（它走 TCP localhost:7402，不依赖设备权限）。
4. **HighPoint 卡 / MegaRAID 卡 / HDD 无温度传感器**，不在闭环内（用户确认只需关注 CPU/内存/SSD）。缓解：`min_duty` 保留基础风量；Phase 2 空载 30 min 后人工测温一次。
3. **覆盖的持久性未知**：若覆盖跨主机重启持久，POST 阶段风扇会停留在最后一次占空比（正常停止为 50%，异常断电可能是低值）。POST 期间 CPU 无负载，风险低。
4. **日后升级 XCC 固件可能改变 OEM 命令语义** ⇒ 升级后必须重跑 Phase 0。

---

## 11. 待 Phase 0 填实的数值

| 项 | 暂定值 | 来源 |
| --- | --- | --- |
| `hard_floor_duty` | 未测（探针前 `config.toml` 不允许启动服务） | Phase 0 步骤 2 |
| duty→RPM 期望表 | 未测 | Phase 0 步骤 3 |
| BMC 空闲策略等效占空比 | 未测 | Phase 0 步骤 3 |
| 恢复自动的路径 | **已验证**：`raw 0x3a 0x07 0xff 0x00 0x00`（模式字节 0x00）让 BMC 立刻恢复自动策略 | Phase 0 |
| 风扇调速命令 | **已验证**：`raw 0x3a 0x07 0xff <速度字节> <模式>`；上游的 `0x3c 0x30` 被拒（0xc1） | Phase 0 |
| 速度字节标度 | **已实测**：字节 8≈1600 RPM、30≈3040、100≈8000、255≈18400；低端被抬高，故用实测表 | Phase 0 |
| fan 0 语义 | 未验证 | Phase 0 步骤 2b/4 |
| `min_duty` | 25 | Phase 2 |
| 曲线拐点 | §5.2 | Phase 2 |

**已实测、可直接使用的常量**：风扇前转子 M=80、后转子 M=74（LUN 0，编号 0x41–0x46 / 0x49–0x4e）；DIMM M=1（LUN 1，编号 0x50+n）；NVMe 来自 `hptraidconf query devices`（~3 s/块）。这些写进 `/etc/fanctl/config.toml` 的 `[tach]`、`[dimm]`、`[nvme]`。
