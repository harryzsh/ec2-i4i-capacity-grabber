# ec2-i4i-capacity-grabber

一个脚本，24×7 死等抢 **i4i** EC2 容量，给 Prime Day 等大促囤产能。
抢的是 **On-Demand 容量预留（ODCR）**：产能锁在你名下，实例停了也不丢，配合客户 ASG 自动吸纳。

```bash
# 演练（不花钱，先看计划）→ 实弹（⚠️ 立刻计费）
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000 --live --watch --interval 30
```

> 上面这条就是本次 Prime Day 的标准命令：两个 AZ 各抢 5000 vCPU，合计 **10000 vCPU**（≈156 台 i4i.16xlarge）。

---

## 30 秒了解

- **目标**：抢 10000 vCPU 的 i4i，均匀铺在 `us-east-1b` / `us-east-1d` 两个 AZ。
- **怎么抢**：每次建一个 `i4i.16xlarge`（64 核）的 open 预留，累加到目标；产能是间歇放出来的，所以 `--watch` 死等。
- **怎么用上**：客户 EKS 的 ASG 设 `capacity-reservations-first`，扩容时实例自动落进预留。**脚本不碰客户 ASG**。
- **三件事必须先做对**（否则白忙）：
  1. **抢预留的账号 = EKS 集群账号**（open 预留只在同账号内匹配）。
  2. **vCPU 配额提到 ≥ 12000**（默认才 5，是头号阻塞，要提前开工单）→ 见 [`扩大.md`](扩大.md)。
  3. **客户 ASG 配好** → 见 [`客户配置.md`](客户配置.md)。

---

## 三步上手

**1. 装**

```bash
pip install -r requirements.txt   # 只需 boto3，Python 3.8+
```

凭证用环境变量 / `~/.aws/credentials` / IAM role 都行。在 EC2 上长跑推荐 **instance profile**。

**2. 演练**（默认就是 dry-run，不加 `--live` 不花一分钱）

```bash
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000
```

看打印的计划：抢哪些 AZ、机型、每 AZ 上限、总目标对不对。

**演练这一趟到底做了什么**——它把「能不能跑通」全验一遍，唯独不真建、不花钱：

- ✅ **真连 AWS**：用你的凭证调只读 API（列 AZ、查 `i4i.16xlarge` 在哪些 AZ 有供货），所以**凭证错、IAM 权限不够、账号/区域不对会当场报错**。
- ✅ **真算计划**：`--per-az-cores 5000` × 2 AZ → 总目标 10000，打印出来给你核对。
- ✅ **真试建预留、但带 `DryRun` 标志**：对每个机型/AZ 调一次创建预留的 API，AWS 只校验「参数合法吗、你有没有权限建」然后拒绝真正创建，脚本据此打印 `[dry-run] would reserve ...`。**所以连"有没有权限建预留"都验到了**。
- ❌ **不建任何预留、不计费、不写台账**（`grabs.jsonl` 只记真实抢占，保持干净）。

> 一句话：演练 = 把权限、账号、AZ/机型供货、计划数字全验一遍，**就差没真按下扣费键**。看着没问题，再加 `--live`。

**3. 实弹**（加 `--live`，⚠️ 预留一建立刻按 On-Demand 价计费）

```bash
python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000 --live --watch --interval 30
```

挂在 systemd 里 24×7 跑（见下）。抢够了它自己停。

**用完一定要释放，停止计费：**

```bash
python3 grab_odcr.py --cancel-all --live
```

---

## 常用命令

```bash
python3 grab_odcr.py --list                          # 看当前抢了多少（含 per-AZ + 总计汇总）
python3 grab_odcr.py --target-cores 10000 --live --watch --interval 30   # 按总核数抢（不分 AZ）
python3 grab_odcr.py ... --live --end-hours 6        # 计费保险：6 小时后预留自动过期
python3 grab_odcr.py --cancel-all --live             # 释放全部、停止计费
```

### 关键参数

| 参数 | 作用 |
|------|------|
| `--live` | **真正建预留**。不加 = 只演练、不花钱 |
| `--watch --interval 30` | 24×7 死等，每 30 秒重扫一次（产能是间歇放出来的，必须死等） |
| `--per-az-cores N` | 每个 AZ 各封顶 N 核，均衡铺货（对齐 ASG 50/50）。**客户每 AZ 一个 ASG 时务必用** |
| `--target-cores N` | 总共抢够 N 核就停。单设 `--per-az-cores` 时总目标自动 = N × AZ 数 |
| `--azs ...` | 锁定 AZ，如 `--azs us-east-1b us-east-1d` |
| `--region R` | 区域，默认 `us-east-1` |
| `--cancel-all` | 取消全部预留、停止计费 |
| `--list` | 只看不动（列每条预留 + per-AZ/总计核数汇总） |

完整参数 `python3 grab_odcr.py --help`。

> **重启幂等**：上限按「AWS 实时持有的核数」判断（不是进程内存），所以进程崩溃 / 机器重启后续跑，会按各 AZ 真实持有**只补差额**，不超抢、不抢歪。配 systemd `Restart=always` 安全。

---

## 24×7 在 EC2 上跑（systemd）

开一台 `t3.micro`（脚本几乎不耗资源），用 instance profile 拿凭证，丢给 systemd 长跑——断了自动拉起、机器重启自启。这样大促期间脚本**一直挂着死等**，你不用反复敲命令，只需偶尔看进度（见下方「监控」）。

**1. 新建 systemd 服务文件**（把下面整段复制到终端执行，会创建 `/etc/systemd/system/grab-odcr.service`）：

```bash
sudo tee /etc/systemd/system/grab-odcr.service > /dev/null <<'EOF'
[Unit]
Description=i4i ODCR capacity grabber
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/ec2-i4i-capacity-grabber
ExecStart=/usr/bin/python3 grab_odcr.py --azs us-east-1b us-east-1d --per-az-cores 5000 --live --watch --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

> 改命令参数就编辑这个文件的 `ExecStart=` 行；改完 `sudo systemctl daemon-reload && sudo systemctl restart grab-odcr`。
> `WorkingDirectory` / `User` 按你实际放脚本的路径和用户改。

**2. 启动 + 开机自启**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grab-odcr      # 起 + 开机自启
sudo systemctl status grab-odcr            # 看是否 running
```

**3. 大促结束，收尾**

```bash
sudo systemctl stop grab-odcr              # 先停 watcher
sudo systemctl disable grab-odcr           # 取消开机自启
python3 grab_odcr.py --cancel-all --live   # 释放全部预留、停止计费（最关键的止血）
```

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

---

## 监控：看抢了多少

跑起来后，有三个地方看进度，各管一件事：

**① 看「目前总共抢了多少核、每个 AZ 多少」——最权威，直接问 AWS：**

```bash
python3 grab_odcr.py --list
```

输出 = 每条预留 + 一个汇总块：

```
cr-0aaa...  i4i.16xlarge  us-east-1b  active  count=78  tag=primeday-i4i-grab
cr-0bbb...  i4i.16xlarge  us-east-1d  active  count=78  tag=primeday-i4i-grab
--- summary (tag=primeday-i4i-grab) ---
  us-east-1b    4992 vCPU  (78 x i4i.16xlarge)
  us-east-1d    4992 vCPU  (78 x i4i.16xlarge)
  TOTAL         9984 vCPU  across 2 AZ(s)
```

> 按**核数**算（不是预留条数），只统计本脚本 tag 的预留。这就是「抢到哪了」的标准答案。

**② 看「脚本此刻在干啥」——实时流水：**

```bash
journalctl -u grab-odcr -f          # systemd 跑的看这个
tail -f logs/grab_odcr.log          # 直接跑的看这个（脚本目录下）
```

每轮打印扫了哪些 AZ、抢到/没抢到、限流退避、`have X/10000 vCPU` 进度。

**③ 看「抢占明细台账」——对账用：**

```bash
wc -l logs/grabs.jsonl              # 一共抢到多少笔（每抢到一个一行）
cat  logs/grabs.jsonl               # 每行一条 JSON：时间戳/机型/AZ/累计 vCPU
```

> dry-run 不写台账，`grabs.jsonl` 只记真实抢占，干净可审计。

---

## 要知道的几件事

- **ODCR 不插队**：它和普通 On-Demand 抢同一个池子，没优先级。价值只在「抢到后停了也不还回去」。
- **能不能抢到是另一回事**：配额提够 ≠ 立刻抢到 10000 核。产能靠 AWS 间歇放出，靠 `--watch` 死等攒。
- **默认只抢 `i4i.16xlarge`（64 核）**：一台一大块，调用最少。要降级兜底加 `--types i4i.16xlarge i4i.8xlarge ...`（脚本自动按大到小排序）。
- **日志**：`logs/grab_odcr.log`（人读流水，自动轮转）、`logs/grabs.jsonl`（每抢到一个追加一行 JSON，对账用）。dry-run 不写台账。
- **成本**：`i4i.16xlarge` ≈ $5.491/小时·台（us-east-1）。实测 6 台跑 3 分钟约 $1.70。

---

## 仓库文件

| 文件 | 用途 |
|------|------|
| `grab_odcr.py` | 抢占脚本（唯一） |
| `common.py` | 共享工具：AZ 发现、退避重试、核数计数、日志台账 |
| `test_grab_odcr.py` | 单元测试（mock boto3，验重启幂等逻辑），`python3 -m unittest test_grab_odcr` |
| [`扩大.md`](扩大.md) | **抢前必读**：配额清单，最该先提 `L-1216C47A` |
| [`客户配置.md`](客户配置.md) | **客户必读**：ASG / EKS 怎么配才能吃进预留 |
| [`SMOKE_TEST.md`](SMOKE_TEST.md) | 两份端到端验证报告（机制 + 真 i4i 分布，已通过） |
