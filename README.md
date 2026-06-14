# ec2-i4i-capacity-grabber

抢占 i4i（存储优化型，Intel Ice Lake + Nitro SSD）EC2 容量的脚本，
用于 Prime Day 等峰值场景下的产能储备。**只走一条路线：On-Demand 容量预留（ODCR）。**

> **区域可配置**：默认 `us-east-1`，所有命令都支持 `--region <region>` 切换到任意区域
> （如 `--region us-west-2`、`--region ap-southeast-1`）。AZ、机型 offering 都会按所选区域自动发现。

| 脚本 | 策略 | 适用场景 |
|------|------|----------|
| `grab_odcr.py` | On-Demand 容量预留 ODCR（`CreateCapacityReservation`） | 预先把产能锁在你名下：即使没启实例、或实例停了，产能也不丢；代价是 active 预留按 On-Demand 价**持续计费**（无论是否填充）。配合客户 ASG 的 `capacity-reservations-first` 自动吸纳 |

> ⚠️ **ODCR 不会在容量池里插队**——它和普通 On-Demand 抢的是同一个池子，没有优先级。
> 它的价值是「抢到后即使实例停了也不还回去」，把产能锁住直到大促结束。

**相关文档**：[`扩大.md`](扩大.md) 抢占前置配额清单（最该先提 `L-1216C47A`） · [`SMOKE_TEST.md`](SMOKE_TEST.md) 两份端到端验证报告。

---

## 工作原理

`grab_odcr.py` 的核心思路：

1. **自动发现** 区域内所有可用 AZ、以及每个实例类型在哪些 AZ 真正被提供（跳过不可能的调用）。ODCR 创建预留**不需要子网**，所以扫**全部 AZ**；只有真正往预留里启实例时才需要对应 AZ 有子网。
2. **默认只预留 `i4i.16xlarge`（64 核）**：一台就是一大块核，凑够目标核数所需的预留数量和 API 调用最少。需要降级兜底时，用 `--types` 显式列出其他机型，脚本会**自动按 vCPU 从大到小排序**后逐个尝试（你不用关心写的顺序）。
3. **逐个抢**：每次只 `count=1`，抢到一个就累加 vCPU，直到达到 `--target-cores` 目标。
4. **以 AWS 实时持有为准（重启幂等）**：每轮 sweep 开头直接从 AWS 读「本脚本 tag 的预留各 AZ 已持有多少**核**」（不是预留条数），用它判断 per-AZ 上限和总目标到没到。所以进程崩溃 / 机器重启 / 打补丁后续跑，会**按各 AZ 真实持有只补差额**，绝不超抢、绝不把分布抢歪。
5. **智能处理**：
   - 没产能（`InsufficientInstanceCapacity` 等）→ 记一笔，换下一个 AZ/机型，不算失败。
   - 被限流（`Throttling`）→ 指数退避 + 抖动后重试同一目标。
   - 其他错误 → 视为致命，立即抛出。
6. **进度统计**：结束后打印实际预留了多少 vCPU、分布在哪些 AZ。

---

## 前置条件

1. **AWS 凭证**：通过环境变量、`~/.aws/credentials` 或 IAM 角色配置好，且有权限调用 EC2 ODCR API。在 EC2 上 24/7 跑时**推荐用 instance profile（IAM role）**，天然对齐账号、不用拷 access key。
2. **Python 3.8+** 和 `boto3`：

   ```bash
   pip install -r requirements.txt
   ```

3. **配额（关键！）**：真实抢 10000 核之前，必须先通过 TAM 把 i4i 的 On-Demand vCPU 配额提到 ≥ 目标值，
   否则会先撞配额上限（`VcpuLimitExceeded`）而不是产能上限。详见 [`扩大.md`](扩大.md)。

### 最小 IAM 权限

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:CreateCapacityReservation",
      "ec2:DescribeCapacityReservations",
      "ec2:CancelCapacityReservation",
      "ec2:CreateTags"
    ],
    "Resource": "*"
  }]
}
```

> `CreateCapacityReservation` 带了 `TagSpecifications`，所以必须给 `ec2:CreateTags`，否则建预留会被拒。

---

## 安全机制

- **默认 dry-run**：不加 `--live` 时只做参数 / IAM 校验（`DryRun=True`），**不会真的建预留**。
- **打标签追踪**：所有预留都打 `purpose=primeday-i4i-grab` 标签，方便定位和清理。
- **一键清理**：自带 `--cancel-all` 拆除命令，防止资源泄漏 / 持续计费。

---

## 最重要的一个开关：`--live`

脚本**默认是演练模式（dry-run）**，只校验权限和参数、打印计划，**不会真的动任何资源、不花一分钱**。
只有当你加上 `--live`，才会真正去抢。先不加 `--live` 跑一遍看计划，确认无误再加 `--live` 实弹，这是最安全的用法。

```bash
python3 grab_odcr.py --target-cores 8           # 演练：只看计划，不建预留
python3 grab_odcr.py --target-cores 8 --live    # 实弹：真的建预留（⚠️ 立刻计费）
```

---

## `grab_odcr.py`（容量预留 ODCR 抢占）

**做什么**：创建容量预留，把产能锁在你名下。即使没启实例、或实例停了，产能也不会还回去。
**适合**：大促前预留产能、业务有 stop/restart 周期、或迁移窗口需要「停了也不丢产能」的场景。
**⚠️ 注意**：active 预留**立刻按 On-Demand 价持续计费**（无论里面有没有跑实例），用完务必 `--cancel-all` 释放。

### 常用命令

```bash
# 1) 演练，看会预留哪些机型（不建预留、不计费）
python3 grab_odcr.py --target-cores 8

# 2) 实弹预留（⚠️ 一加 --live 就立刻开始计费）
python3 grab_odcr.py --target-cores 10000 --live

# 3) 加计费保险：6 小时后预留自动过期释放，防止忘记取消
python3 grab_odcr.py --target-cores 10000 --live --end-hours 6

# 4) 随时查看当前持有哪些预留
python3 grab_odcr.py --list

# 5) 释放全部预留、停止计费（正规 i4i 供给到位后执行）
python3 grab_odcr.py --cancel-all --live
```

### 全部参数

| 参数 | 作用 | 默认值 | 示例 |
|------|------|--------|------|
| `--region R` | 在哪个 AWS 区域抢 | `us-east-1` | `--region us-west-2` |
| `--target-cores N` | 持有够 N 个 vCPU 就停下（按 AWS 实时核数判断，重启幂等） | `8` | `--target-cores 10000` |
| `--per-az-cores N` | **均衡模式**：每个 AZ 各封顶 N 个 vCPU。上限按该 AZ **AWS 实时持有核数**判断，某 AZ 凑满就跳过、继续抢其余 AZ，让预留在各 AZ 间均匀分布（对齐 ASG 的 50/50 调度）。**进程重启后按各 AZ 真实持有只补差额，不会抢歪。** 单独设此项、`--target-cores` 留默认时，总目标自动 = `N × AZ数` | 关闭（不均衡） | `--per-az-cores 5000` |
| `--types ...` | 机型列表，脚本**自动按 vCPU 从大到小排序**（顺序随便写）。不传 = 只预留 `i4i.16xlarge` | 只 `i4i.16xlarge` | `--types i4i.16xlarge i4i.8xlarge` |
| `--azs ...` | 锁定只在这些 AZ 预留（写 AZ 名字）。不传 = 区域内所有 AZ | 所有 AZ | `--azs us-east-1c us-east-1d` |
| `--end-hours N` | N 小时后预留**自动过期释放**（计费保险，防止忘关） | 不过期，直到手动取消 | `--end-hours 6` |
| `--live` | 真正建预留。**不加 = 只演练不建**。⚠️ 加了立刻计费 | 关闭（演练） | `--live` |
| `--watch` | 24×7 死等模式：循环重扫，每轮重读 AWS 真实持有，直到够 `--target-cores` 才停 | 关闭（扫一遍就退出） | `--watch` |
| `--interval N` | `--watch` 模式下每轮之间隔几秒 | `60` | `--interval 30` |
| `--cancel-all` | 取消所有预留、**停止计费**（清理用） | 关闭 | `--cancel-all --live` |
| `--list` | 只查看当前持有的预留，不增不删 | 关闭 | `--list` |
| `-h` / `--help` | 显示帮助 | — | `--help` |

> 脚本默认用的是「即时预留」，**无最低承诺，可随时取消**。
> 若要预订未来某天（如 0703）的产能，需要带 `commitmentDuration`，那要和供给侧另行协商。

---

## 机型策略

**默认只预留 `i4i.16xlarge`（64 核）**——一台一大块核，凑够目标所需的预留数最少。
需要降级兜底时，用 `--types` 列出多个机型即可，脚本会**自动按 vCPU 从大到小排序**后逐个尝试（你写的顺序无所谓，未知机型会被自动忽略并告警）。
内置的 vCPU 对照（用 `--types` 自定义时参考）：

| 机型 | vCPU | 内存 | 本地 NVMe SSD |
|------|------|------|----------|
| `i4i.large` | 2 | 16 GiB | 1 × 468 GB |
| `i4i.xlarge` | 4 | 32 GiB | 1 × 937 GB |
| `i4i.2xlarge` | 8 | 64 GiB | 1 × 1,875 GB |
| `i4i.4xlarge` | 16 | 128 GiB | 1 × 3,750 GB |
| `i4i.8xlarge` | 32 | 256 GiB | 2 × 3,750 GB |
| `i4i.16xlarge` | 64 | 512 GiB | 4 × 3,750 GB |
| `i4i.32xlarge` | 128 | 1,024 GiB | 8 × 3,750 GB |

---

## Prime Day 实战剧本

### ⭐ 本次 Prime Day 标准命令（10000 vCPU 总数，两 AZ 均衡）

> 目标是 **10000 vCPU 总数**（不是 10000 台），均匀铺在 `us-east-1b` / `us-east-1d` 两个 AZ。
> 每 AZ 各封顶 `10000 ÷ 2 = 5000` vCPU；i4i.16xlarge = 64 vCPU/台，即每 AZ 约 78 台、合计约 156 台。
> 用 `--per-az-cores` 设单 AZ 上限，脚本会**自动把总目标算成 `5000 × 2 = 10000`**，你只需填一个数。

```bash
# 第 0 步：先演练（不加 --live），确认 AZ、机型、每 AZ 上限、总目标都对
python3 grab_odcr.py \
  --azs us-east-1b us-east-1d \
  --per-az-cores 5000

# 第 1 步：实弹 24×7 死等，两 AZ 均衡抢满 10000 vCPU（⚠️ 一加 --live 立刻计费）
python3 grab_odcr.py \
  --azs us-east-1b us-east-1d \
  --per-az-cores 5000 \
  --live --watch --interval 30
```

- **`--per-az-cores 5000`**：每个 AZ 各到 5000 vCPU 就停抢、跳过去抢另一个 AZ，预留天然均衡（对齐 ASG 的 50/50 调度），不会单边堆出空转浪费。客户是「每 AZ 一个 ASG」时**更要用它**——让预留按各 AZ 的 ASG 目标精确铺，两边数量对齐。
- **`--watch --interval 30`**：产能是间歇放出来的，每 30 秒重扫一次、死等攒满，每轮按 AWS 实时持有只补差额。
- **不加 `--end-hours`**：Prime Day 要长期持有产能，不能让预留中途自动过期；用完手动 `--cancel-all` 释放。
- 长期跑请交给 **systemd**（见下方「24×7 在 EC2 上跑」），断了自动拉起、机器重启自启、重启续抢不超额。

> 想加计费保险（比如怕忘关）可附 `--end-hours 12`，但要确保大于你实际持有时长，否则预留会提前释放。
> 想允许降级兜底（16xl 抢不到就往下），加 `--types i4i.16xlarge i4i.8xlarge i4i.4xlarge`（脚本自动按 vCPU 从大到小排序）。

### 完整步骤

1. 让 TAM 把 i4i On-Demand vCPU 配额提到 ≥ 10000（建议 12000，含回落余量），否则会先撞配额（`VcpuLimitExceeded`）而不是产能上限。详见 [`扩大.md`](扩大.md)。
2. 确认目标账号在 `us-east-1b` / `us-east-1d` 有子网（ODCR 建预留不需要子网，但真正往预留里启实例时需要）。
3. 跑上面的 ⭐ 标准命令（先演练后实弹）。
4. 持续盯进度：
   ```bash
   python3 grab_odcr.py --list                 # 当前持有哪些预留
   tail -f logs/grab_odcr.log                   # 实时流水，看各 AZ 分布
   wc -l logs/grabs.jsonl                        # 已抢到多少笔
   ```
5. 等正规 i4i 供给到位后，释放预留、停止计费：
   ```bash
   python3 grab_odcr.py --cancel-all --live
   ```

---

## 24×7 在 EC2 上跑（生产部署）

`--watch` 已经是死循环，要 24/7 无人值守只需让它**断了能自动拉起、机器重启能自启**。推荐用一台小 EC2 + systemd。

### 为什么是小 EC2 + instance profile

- **账号必须 = EKS 集群账号**（open 预留只在同账号内匹配）。直接在该账号开机器、用 **instance profile** 拿凭证，天然对齐账号、不碰 access key。
- 脚本就是「调 API → sleep → 再调」，几乎不耗资源，**`t3.micro` 足够**。（它自己也占 2 vCPU 配额，和抢占目标无关。）
- 上线前先 `tmux` 跑一次 dry-run 肉眼确认计划，再交给 systemd 长跑。

### systemd unit

`/etc/systemd/system/grab-odcr.service`：

```ini
[Unit]
Description=i4i ODCR capacity grabber (24x7 watch)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/ec2-i4i-capacity-grabber
ExecStart=/usr/bin/python3 grab_odcr.py \
  --azs us-east-1b us-east-1d \
  --per-az-cores 5000 \
  --live --watch --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grab-odcr      # 立刻起 + 开机自启
journalctl -u grab-odcr -f                 # 看实时日志
```

> **为什么 `Restart=always` 在这里是安全的**：脚本的 per-AZ / 总量上限都按 **AWS 实时持有核数**判断（每轮重读），不靠进程内存累加。所以无论崩溃、重启、补丁，拉起后都会按各 AZ 真实持有**只补差额**——不会超抢、不会抢歪分布。抢满目标后 watch 循环自然停在那里空转重扫（不再建预留），大促结束 `systemctl stop` + `--cancel-all` 收尾即可。

### 收尾

```bash
sudo systemctl stop grab-odcr              # 先停 watcher
sudo systemctl disable grab-odcr           # 取消开机自启
python3 grab_odcr.py --cancel-all --live   # 释放全部预留、停止计费（最关键的止血）
```

---

## ASG 接预留配置（客户侧必读）

脚本只负责**把预留抢到手**。预留抢到后，能不能被你的 Auto Scaling Group（ASG）自动「吃掉」、扩容时实例真正落进预留，**取决于客户侧的 ASG 配置**。这一节是开 ASG 前客户必须确认的事项。

### 核心开关：`capacity-reservations-first`

在 ASG 上设置 Capacity Reservation 偏好（**设在 ASG 上，不是启动模板上**）：

```bash
aws autoscaling create-auto-scaling-group \
  --auto-scaling-group-name <你的ASG名> \
  --launch-template "LaunchTemplateId=<LT-ID>,Version=\$Latest" \
  --vpc-zone-identifier "<1b子网>,<1d子网>" \
  --capacity-reservation-specification "CapacityReservationPreference=capacity-reservations-first" \
  ...

# 已有 ASG 改配置：
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <你的ASG名> \
  --capacity-reservation-specification "CapacityReservationPreference=capacity-reservations-first"
```

| 偏好值 | 行为 | 适用 |
|--------|------|------|
| `capacity-reservations-first` | **优先**用匹配的 open 预留，预留用光了**自动回落到普通 On-Demand**（软兜底，不会扩容失败） | ⭐ Prime Day 推荐：有预留就吃，没预留也不挂 |
| `capacity-reservations-only` | 只用预留，没预留就**扩容失败** | 太硬，不推荐（预留不够时实例起不来） |
| `none` / `default` | 不主动用预留 | 不适用 |

> 用 `capacity-reservations-first` + `open` 模式预留：ASG **不需要**指定具体预留 ID，任何属性匹配的实例都会自动掉进预留。这就是为什么 `grab_odcr.py` 默认建 `open` 预留——客户 ASG 零改动即可吸纳。
>
> ⚠️ 用了 `capacity-reservations-first` 的软兜底，回落的普通 On-Demand 实例**占用同一个 `L-1216C47A` vCPU 配额**。所以配额要在目标核数之上留 20% 余量（本案 9,984 → 12,000），否则预留占满后回落实例会撞配额起不来。详见 [`扩大.md`](扩大.md)。

### 实例要落进预留，4 个属性必须 100% 对齐

预留和 ASG 启动的实例，下面 4 项**少一项对不上，实例就掉不进预留**（会静默走普通 On-Demand）：

| 属性 | 必须一致 | 本次配置 |
|------|----------|----------|
| **机型** instance type | 预留机型 = LT 机型 | `i4i.16xlarge` |
| **平台** platform (OS) | 预留 platform = 实例 OS | `Linux/UNIX`（AL2023） |
| **可用区** AZ | 预留 AZ ∈ ASG 子网所在 AZ | `us-east-1b` / `us-east-1d` |
| **租户** tenancy | 预留 tenancy = 实例 tenancy | `default`（共享） |

### 客户开 ASG 前 checklist

- [ ] ASG 设了 `CapacityReservationPreference=capacity-reservations-first`
- [ ] ASG 的 `--vpc-zone-identifier` 横跨预留所在的两个 AZ（1b + 1d），且每个 AZ 都有可用子网
- [ ] 启动模板的机型 = 预留机型（`i4i.16xlarge`）
- [ ] 启动模板的 AMI 架构 = 机型架构（i4i 是 x86_64，别配成 ARM AMI）
- [ ] 启动模板平台 = `Linux/UNIX`，tenancy = `default`
- [ ] 预留是 `open` 模式（`grab_odcr.py` 默认就是，无需 ASG 指定预留 ID）

### ⚠️ 常见坑

- **属性对不上 = 双倍计费**：实例掉不进预留时不会报错，而是静默起一台普通 On-Demand。结果你**同时付**空转预留的钱 + On-Demand 实例的钱。开 ASG 前务必核对上面 4 个属性。
- **AMI 架构不匹配**：i4i 是 Intel x86_64，启动模板若配了 ARM/Graviton 的 AMI，实例根本起不来。
- **预留用了 `targeted` 而非 `open`**：targeted 模式要求实例显式指定预留 ID，ASG 不会自动吸纳。本脚本默认 `open`，不要改成 targeted。
- **ASG 50/50 调度 vs 预留分布**：ASG 默认往实例数少的 AZ 放，最终≈50/50。所以预留也要 50/50 均衡铺（用 `--per-az-cores`），否则多的那边预留空转、少的那边缺口落到 On-Demand。

---

## EKS 自建节点组（self-managed node group）专项

> 本节针对客户实际架构：**EKS 自建节点组**，**自管 auto scaler**（非 Kubernetes Cluster Autoscaler），**每个 AZ 一个 ASG**。
> 这套架构天然规避了原本 6 个坑里的 2 个（AZRebalance、跨 AZ 调度）。EC2 层抢货机制不变（已端到端验证，见 [`SMOKE_TEST.md`](SMOKE_TEST.md)），剩下的 Kubernetes 层风险**收敛到一条**：自管 scaler 在低负载时把节点缩下去，把抢到的 i4i 还回 AWS。

### 本脚本的定位：只抢预留，绝不碰 ASG（写死）

`grab_odcr.py` **从代码层面就只调用 3 个 EC2 ODCR API**，全项目扫描确认无任何 `autoscaling` 调用：

| 脚本动作 | 调用的 AWS API |
|----------|----------------|
| 抢预留 | `create_capacity_reservation` |
| `--list` / gate 读实时核数 | `describe_capacity_reservations` |
| `--cancel-all` 释放 | `cancel_capacity_reservation` |

> ✅ 客户已有的 EKS 节点组（ASG / 启动模板 / 节点）**脚本一律不读、不改、不删**。抢到的是 `open` 模式预留，客户节点组只要设了 `capacity-reservations-first` 就会自动 open-match 吸纳，两边各管各的、互不干涉。

### 生产运行方式

```bash
# 大促 24×7 抢：两个 AZ 各封顶 5000 vCPU（=78×i4i.16xlarge/AZ，共 10000 vCPU）
python3 grab_odcr.py \
  --azs us-east-1b us-east-1d \
  --per-az-cores 5000 \
  --live --watch --interval 30

# 随时查看已抢到的预留
python3 grab_odcr.py --list

# 大促结束，一键释放所有预留（停止计费）
python3 grab_odcr.py --cancel-all --live
```

抢到的预留即 `open` 模式，客户节点组的 ASG 自动吸纳，脚本无需也不会触碰 ASG。生产建议挂 systemd 长跑，见上方「24×7 在 EC2 上跑」。

### ✅ 客户架构已规避的两个坑

| 原坑 | 为什么本架构下不成立 |
|------|----------------------|
| **AZRebalance**（单 ASG 跨多 AZ 时会搬实例丢容量）| AZRebalance 只在**单 ASG 横跨多 AZ**时触发。客户**每 AZ 一个 ASG**（单 AZ），组内没有可均衡对象 → 不触发。前提：每个 ASG 只挂**单 AZ 的子网**（务必确认，见下方 checklist）|
| **跨 AZ 单节点组**（CA 无法精确控制 AZ 落点）| 客户已经是**每 AZ 一个 ASG**，正好对齐脚本 `--per-az-cores` 的均衡抢货设计 → 已解决 |

> 注：客户用**自管 auto scaler**，不是 Kubernetes Cluster Autoscaler，所以原「CA 缩容 / CA 不占满预留」两个坑也不以 CA 形态出现——但风险换了个马甲，见下。

### 🔴 真正剩下的核心坑：自管 scaler 低负载缩容

抢到 i4i 不等于守得住。客户自管 scaler 若有「节点利用率低 → 缩容」逻辑，会在 pod 没填满时主动终止节点，把容量还回 AWS，高峰**可能抢不回**。

| 后果 | 防护手段（二选一，推荐都做）|
|------|------|
| scaler 判定低负载，缩掉抢到的预留节点 | **① 大促窗口把每个 ASG 的 `min = desired = 该 AZ 预留数` 钉死**，关掉向下缩容逻辑——这是最干净的做法，坑直接消除 |
| 预留抢到了但没 pod 调度，节点不起，ODCR 空转全额计费 | **② 大促前主动预热**：直接拉高 `min/desired` 强制起节点占满预留，别等 scaler |

```bash
# 钉死下限（每个单 AZ ASG 各做一次，N = 该 AZ 预留数）
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name <该AZ的ASG名> \
  --min-size <N> --desired-capacity <N> --max-size <N>
```

> ✅ **这是现在唯一需要跟客户在 K8s 层敲死的事**：大促期间能否把两个 ASG 的 `min/desired` 钉到预留数、关掉向下缩容。

### 🟡 两个「配置不对就用不上预留」的坑

| 坑 | 说明 | 正确做法 |
|----|------|----------|
| **实例类型没锁死** | 启动模板配多机型，open 预留四要素匹配不上，静默走普通 On-Demand | 启动模板**只配 `i4i.16xlarge`**，别配多实例类型 |
| **大促期间滚动更新 / 改 LT 版本** | 改启动模板版本会 recycle 所有节点，抢不到货时可能丢容量 | 配置**冻结**到大促结束，期间禁止改 LT、禁止滚动更新 |

### EKS 自建节点组 checklist（抢货前）

EC2 层硬阻塞（错一个整个方案归零，**最优先**）：

- [ ] **抢预留的账号 = EKS 集群账号**（open 预留只在同账号内匹配；验证在 `476114114317`）
- [ ] **该账号 On-Demand Standard vCPU 配额已提到 ≥10000**（156 台 = 9984 vCPU，建议提到 12000 含回落余量；默认配额远不够 → 提早开 quota 工单，**头号硬阻塞**，详见 [`扩大.md`](扩大.md)）
- [ ] **两个 ASG 的子网确实在 1b + 1d**（不在则改抢对应 AZ）
- [ ] 启动模板只锁 `i4i.16xlarge`、`Linux/UNIX`、`default` 租户、x86_64 AMI

客户侧配置：

- [ ] 每个 ASG 设了 `CapacityReservationPreference=capacity-reservations-first`
- [ ] 每个 ASG 只挂**单 AZ 子网**（确认 AZRebalance 确实不触发；可顺手 `suspend-processes AZRebalance` 防配置漂移，零成本）
- [ ] **大促窗口把每个 ASG `min/desired` 钉到该 AZ 预留数，关掉自管 scaler 的向下缩容**
- [ ] 实例打了 `kubernetes.io/cluster/<集群名>: owned` 标签（否则节点不入集群）
- [ ] 关键 pod 配了 PodDisruptionBudget（进一步防误驱逐）
- [ ] 配置已冻结，大促期间不改 LT、不滚动更新

> **一句话总结**：客户这套「自管 scaler + 每 AZ 一个 ASG」天然规避了 AZRebalance 和跨 AZ 调度两个坑；K8s 层只剩一件事——**大促期间钉死 `min/desired`、别向下缩容**。真正的拦路虎在 EC2 层：**账号对齐 + vCPU 配额**。本脚本只负责抢预留，对客户 ASG 零触碰。

---

## 验证报告

抢货机制已端到端实测通过，两份报告整理在 **[`SMOKE_TEST.md`](SMOKE_TEST.md)**：

1. **Smoke Test 1（t3.micro 验机制）**：证明「open 预留 + ASG `capacity-reservations-first` → 实例自动落进预留」这套链路成立。机制与机型无关，对 t3.micro 成立即对 i4i.16xlarge 成立。全程 < $0.05。
2. **Smoke Test 2（真 i4i.16xlarge 验分布）**：6 台 i4i.16xlarge 一次全抢到、ASG 拉起、**完美 3+3 两 AZ 均衡**、每台落进对应 AZ 预留（Available 3→0）。计费窗口 ~3.1 分钟，≈ $1.70，零残留。

> 两份都验的是「抢到预留后能不能用上」；**能不能抢到** i4i 现货是另一回事（取决于 AWS 池子，靠 `--watch` 死等）。Prime Day 把规模从 6 台放大到 ~156 台即可，命令结构完全一致。

---

## 24×7 死等模式（`--watch`）

产能不是一直有的，AWS 会**间歇性**地把回收的 i4i 放回池子——可能凌晨某几分钟突然有一批，几秒后又被别人抢光。
单次扫描很可能空手而归。`--watch` 让脚本**循环重扫**，每隔 `--interval` 秒再扫一遍（每轮重读 AWS 真实持有），直到抢够 `--target-cores` 才停，这才是真正能把产能攒起来的用法。

```bash
# 每 30 秒重扫一次，死等到凑够 10000 vCPU；配合 --end-hours 做计费保险
python3 grab_odcr.py --target-cores 10000 --live --watch --interval 30 --end-hours 6
```

- 每轮按 AWS 实时持有只补差额，不会重复抢；进程重启也按各 AZ 真实持有续抢，不超额。
- `Ctrl-C` 随时安全退出，已抢到的资源不受影响（已记在日志和台账里）。
- 长期跑请交给 **systemd**（见上方「24×7 在 EC2 上跑」），它解决自动重启 + 开机自启。

---

## 日志与抢占台账

脚本会把每一轮运行**全程记录到日志**，无需任何开关、零外部依赖，全部落在本地 `logs/` 目录：

| 文件 | 内容 | 格式 |
|------|------|------|
| `logs/grab_odcr.log` | 人读的完整运行流水：每轮扫了哪些 AZ/机型、抢到/没抢到、限流退避等 | 文本，自动轮转（单文件 5 MB，保留 5 份，绝不撑爆磁盘） |
| `logs/grabs.jsonl` | **抢占台账**：每真正抢到一个预留就追加一行 JSON，方便事后统计、对账、喂给其他工具 | JSON Lines（一行一条） |

`grabs.jsonl` 每行的字段：

```json
{"ts": "2026-06-13T07:12:16Z", "via": "odcr", "instance_type": "i4i.16xlarge",
 "az": "us-east-1b", "region": "us-east-1", "vcpu": 64, "total_vcpu": 64, "target_vcpu": 10000}
```

| 字段 | 含义 |
|------|------|
| `ts` | 抢到时刻（UTC ISO8601） |
| `via` | 路线：`odcr` |
| `instance_type` | 抢到的机型 |
| `az` | 落在哪个可用区 |
| `region` | 区域 |
| `vcpu` | 这一个预留贡献的 vCPU |
| `total_vcpu` | 累计已抢到的 vCPU |
| `target_vcpu` | 本次目标 vCPU |

> **演练（dry-run）不写台账**——`grabs.jsonl` 里只会有真正计费的抢占记录，干净可审计。
> 想看当前进度：`tail -f logs/grab_odcr.log`；想统计抢到多少个预留：`wc -l logs/grabs.jsonl`。

---

## 文件结构

```
.
├── common.py          # 共享工具：AZ 发现、机型 offering 探测、退避重试、vCPU 计数、错误分类、日志与台账
├── grab_odcr.py       # ODCR 预留抢占脚本（唯一抢占脚本）
├── 扩大.md            # 抢占前置配额清单（最该先提 L-1216C47A）
├── SMOKE_TEST.md      # 两份端到端验证报告（t3.micro 验机制 + 真 i4i.16xlarge 验分布）
├── logs/              # 运行日志（自动轮转）+ 抢占台账 grabs.jsonl（首次运行自动生成）
├── requirements.txt   # 依赖（boto3）
└── README.md
```

## 成本参考

- `i4i.16xlarge` = **$5.491/小时**（us-east-1 参考价，On-Demand，按秒计费，最低 60 秒），64 vCPU。价格随区域不同，以 AWS Pricing API 实时为准。
- 实弹验证已通过：Smoke Test 2 真起了 6 台 i4i.16xlarge，计费窗口 ~3.1 分钟，总花费 ≈ $1.70（详见 [`SMOKE_TEST.md`](SMOKE_TEST.md)）。
- `--region` 已在 us-east-1 / us-west-2 验证可正常发现各自的 AZ 与机型 offering。
