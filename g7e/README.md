# ec2-g7e-capacity-grabber (count-based)

一个脚本，24×7 死等抢 **g7e.48xlarge** EC2 容量。抢的是 **On-Demand 容量预留（ODCR）**：产能锁在你名下，实例停了也不丢。

> 这是仓库根目录 i4i grabber 的 **按台数（instance count）** 版本兄弟脚本。i4i 那个按 **vCPU 核数** 抢；这个**只抢一种机型 `g7e.48xlarge`，按台数抢**——每一个单位就是“一台 g7e.48xlarge”。

```bash
# 演练（不花钱，先看计划）→ 实弹（⚠️ 立刻计费）
python3 grab_g7e_odcr.py --azs us-east-1b us-east-1d --per-az-count 2
python3 grab_g7e_odcr.py --azs us-east-1b us-east-1d --per-az-count 2 --live --watch --interval 30
```

> 上面这条就是标准命令：两个 AZ 各抢 2 台，合计 **4 台 g7e.48xlarge**。

## 30 秒了解
* **目标**：抢 N 台 g7e.48xlarge，可均匀铺在多个 AZ 上。
* **怎么抢**：每次建一个 `count=1` 的 open 预留，累加到目标；产能是间歇放出来的，所以 `--watch` 死等。
* **为什么按台数**：g7e.48xlarge 只有一种尺寸（192 vCPU/台），按“台”计量比按核数直观——`--per-az-count 2` 就是每个 AZ 各 2 台。
* **两件事必须先做对**（否则白忙）：
  1. **G/VT vCPU 配额提到 ≥ 192 × 台数**（默认常常是 0，是头号阻塞，提前提 `L-DB2E81BA`）→ 见下方「配额」。
  2. **ODCR ≠ 一定抢得到**：它和普通 On-Demand 抢同一个池子，没优先级；价值只在“抢到后停了也不还回去”。

## 三步上手

**1. 装**

```bash
pip install -r requirements.txt   # 只需 boto3，Python 3.8+
```

凭证用环境变量 / `~/.aws/credentials` / IAM role 都行。在 EC2 上长跑推荐 **instance profile**。

**2. 演练**（默认就是 dry-run，不加 `--live` 不花一分钱）

```bash
python3 grab_g7e_odcr.py --azs us-east-1b us-east-1d --per-az-count 2
```

演练会**真连 AWS**：列 AZ、查 `g7e.48xlarge` 在哪些 AZ 有供货、对每个 AZ 调一次带 `DryRun` 的创建预留 API（AWS 只校验参数和权限然后拒绝真正创建）。所以**凭证错、IAM 权限不够、机型在该 AZ 没供货都会当场暴露**，但**不建任何预留、不计费、不写台账**。

> 注意：不加 `--watch` 时，演练/实弹都只做 **一轮 sweep**（每个 AZ 最多抢 1 台）。要把每个 AZ 填满到 `--per-az-count`，必须加 `--watch`（靠多轮累加）。

**3. 实弹**（加 `--live`，⚠️ 预留一建立刻按 On-Demand 价计费）

```bash
python3 grab_g7e_odcr.py --azs us-east-1b us-east-1d --per-az-count 2 --live --watch --interval 30
```

挂在 systemd 里 24×7 跑（见下）。抢够了它自己停。

**用完一定要释放，停止计费：**

```bash
python3 grab_g7e_odcr.py --cancel-all --live
```

## 常用命令

```bash
python3 grab_g7e_odcr.py --list                         # 看当前抢了多少台（含 per-AZ + 总计，自动带目标进度）
python3 grab_g7e_odcr.py --target-count 4 --live --watch --interval 30   # 按总台数抢（不分 AZ 均衡）
python3 grab_g7e_odcr.py ... --live --end-hours 6        # 计费保险：6 小时后预留自动过期
python3 grab_g7e_odcr.py --cancel-all --live             # 释放全部、停止计费
# Windows PowerShell 没有 watch：直接用脚本自带的 --watch；要外部刷新可循环调 --list
```

### 关键参数

| 参数 | 作用 |
|------|------|
| `--live` | **真正建预留**。不加 = 只演练、不花钱 |
| `--watch --interval 30` | 24×7 死等，每 30 秒重扫一次（产能间歇放出，必须死等，也是把每个 AZ 填满的唯一方式） |
| `--per-az-count N` | 每个 AZ 各封顶 **N 台**，均衡铺货。设了它就**不用再写 `--target-count`**：总目标自动 = `N × AZ数`（如 `2 × 2 = 4`），启动日志会打印 `balanced mode: per-az cap 2 x 2 AZ -> target 4 instances` 确认 |
| `--target-count N` | 总共抢够 **N 台**就停。**字面默认 1**（占位）；只要设了 `--per-az-count` 且没手动改它，就会被自动覆盖成 `N × AZ数`。⚠️ 两个都写且对不上时，**以 `--target-count` 为硬总闸** |
| `--azs ...` | 锁定 AZ，如 `--azs us-east-1b us-east-1d`（默认：region 内全部可用 AZ） |
| `--region R` | 区域，默认 `us-east-1` |
| `--end-hours N` | N 小时后预留自动过期（计费保险） |
| `--cancel-all` | 取消全部本脚本 tag 的预留、停止计费 |
| `--list` | 只看不动（列每条预留 + per-AZ/总计台数汇总，自动从台账读目标显示进度） |

完整参数 `python3 grab_g7e_odcr.py --help`。

> **重启幂等**：上限按「AWS 实时持有的台数」判断（不是进程内存），所以进程崩溃 / 机器重启后续跑，会按各 AZ 真实持有**只补差额**，不超抢、不抢歪。配 systemd `Restart=always` 安全。
>
> **count-based 精确停**：每次 `+1` 台、抢前先判 gate，所以**精确停在目标台数，不会超抢**（这点比按核数的 i4i 版更干净，那个可能因机型粒度小幅超过核数上限）。

## 配额（抢前必读）

g7e 属于 **G 系列**，提的是 **`Running On-Demand G and VT instances`** 配额（`L-DB2E81BA`），单位是 **vCPU**，不是台数。每台 g7e.48xlarge = **192 vCPU**，要抢 N 台就把配额提到 **≥ 192 × N**。

```bash
# 查当前 G/VT 配额
aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --region us-east-1
# 申请提到 768 vCPU（≈4 台）
aws service-quotas request-service-quota-increase --service-code ec2 --quota-code L-DB2E81BA --desired-value 768 --region us-east-1
```

> **配额批了 ≠ 立刻抢到**：配额只决定“允许你起多少”，真正能不能起还要看那个 AZ 有没有货——所以才要 `--watch` 死等。

## 24×7 在 EC2 上跑（systemd）

开一台 `t3.micro`（脚本几乎不耗资源），用 instance profile 拿凭证，丢给 systemd 长跑——断了自动拉起、机器重启自启。

**1. 新建 systemd 服务文件**（创建 `/etc/systemd/system/grab-g7e-odcr.service`）：

```bash
sudo tee /etc/systemd/system/grab-g7e-odcr.service > /dev/null <<'EOF'
[Unit]
Description=g7e.48xlarge ODCR capacity grabber (count-based)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/ec2-i4i-capacity-grabber/g7e
ExecStart=/usr/bin/python3 grab_g7e_odcr.py --azs us-east-1b us-east-1d --per-az-count 2 --live --watch --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

> 改命令参数就编辑 `ExecStart=` 行；改完 `sudo systemctl daemon-reload && sudo systemctl restart grab-g7e-odcr`。`WorkingDirectory` / `User` 按你实际路径和用户改。这里没写 `--target-count` 是对的：`--per-az-count 2` 会让总目标自动 = 4。

**2. 启动 + 开机自启**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grab-g7e-odcr
sudo systemctl status grab-g7e-odcr
```

**3. 大促/任务结束 —— 先停、再释放（这一步才停计费）**

```bash
# (1) 先停 watcher（停止继续抢；已抢到的预留仍在、仍计费）
sudo systemctl stop grab-g7e-odcr
sudo systemctl disable grab-g7e-odcr
# (2) 确认无误后，再释放全部预留 —— 这一步才真正停止计费
python3 grab_g7e_odcr.py --cancel-all --live
```

> ⚠️ **`stop` 和 `cancel-all` 是两码事**：`stop` 只停“继续抢”，**已抢到的预留还在、还在计费**；只有 `cancel-all` 才真正释放、停计费。顺序不能反：先 `stop` 防止边释放边又抢回来，再 `cancel-all`。

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

> `Resource: "*"` 是因为创建容量预留时资源 ARN 尚不存在；如需更严格可结合条件键（如 `aws:RequestedRegion`、tag 条件）收紧。

## 监控：看抢了多少

三个出口，各回答一个不同的问题——平时只用第 ①个就够：

| 想知道 | 用哪个 | 数据来自 |
|--------|--------|----------|
| ① **现在总共抢了多少台、每个 AZ 多少**（最常看） | `python3 grab_g7e_odcr.py --list` | 实时问 AWS |
| ② **脚本此刻在干啥、为啥没抢到**（排查用） | `journalctl -u grab-g7e-odcr -f` 或 `tail -f logs/grab_g7e_odcr.log` | 脚本运行流水 |
| ③ **每一笔抢占的明细**（事后对账用） | 读 `logs/grabs.jsonl` 台账 | 脚本写的本地文件 |

### ① 抢到多少 —— 最权威，直接问 AWS

```bash
python3 grab_g7e_odcr.py --list
```

输出 = 每条预留 + 一个汇总块（按**台数**算、只统计本脚本 tag `purpose=g7e-grab` 的预留）。summary 自动带进度：`--list` 会从 `grabs.jsonl` 读出上次抢的目标，显示 `已抢 / 目标` + `FULL/short`：

```
cr-0aaa...  g7e.48xlarge   us-east-1b   active    count=2 free tag=g7e-grab
cr-0bbb...  g7e.48xlarge   us-east-1d   active    count=2 free tag=g7e-grab
--- summary (tag=g7e-grab) ---
  us-east-1b      2 / 2 g7e.48xlarge [FULL]
  us-east-1d      2 / 2 g7e.48xlarge [FULL]
  TOTAL           4 / 4 instances across 2 AZ(s) [FULL]
  USED            0 / 2 reservations USED (have an instance running)
```

### ② 脚本此刻在干啥 —— 运行日志

```bash
journalctl -u grab-g7e-odcr -f          # 用 systemd 跑的，看这个
tail -f logs/grab_g7e_odcr.log          # 直接 / tmux 跑的，看这个（脚本目录下）
```

### ③ 抢占明细台账 —— 对账用

`logs/grabs.jsonl` 是机器可读的流水账：每真正抢到一个预留，就追加一行 JSON（dry-run 不写）。一行长这样（`total_count` = 抢到这笔时的累计台数）：

```json
{"ts":"2026-06-16T07:12:16Z","via":"odcr","instance_type":"g7e.48xlarge","az":"us-east-1b","region":"us-east-1","count":1,"total_count":1,"target_count":4,"per_az_count":2,"per_az_total":1}
```

## 要知道的几件事
* **ODCR 不插队**：和普通 On-Demand 抢同一个池子，没优先级。价值只在“抢到后停了也不还回去”。
* **Capacity Blocks 不覆盖 G 系列**：G7e 不能用 Capacity Blocks for ML（那是 P/Trn 训练卡的），所以 ODCR 是这里的正解。
* **只抢 g7e.48xlarge 一种机型**：无尺寸回落（按需求设计）。
* **成本**：ODCR 一旦 active 就按 On-Demand 价计费，无论有没有实例占用。用完务必 `--cancel-all --live`。
* **测试**：`python3 -m unittest test_common test_grab_g7e_odcr`（mock boto3，无 AWS、无成本）。

## 文件

| 文件 | 用途 |
|------|------|
| `grab_g7e_odcr.py` | 抢占脚本（count-based，唯一入口） |
| `common.py` | 共享工具：AZ 发现、供货检查、退避重试、台数台账、日志 |
| `test_common.py` / `test_grab_g7e_odcr.py` | 单元测试（mock boto3，无 AWS）：`python3 -m unittest test_common test_grab_g7e_odcr` |
| `requirements.txt` | 依赖（仅 boto3） |
