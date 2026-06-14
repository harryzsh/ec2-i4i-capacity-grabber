# Smoke Test 验证报告

本文件汇总 `grab_odcr.py` 抢预留机制的两份端到端验证报告，从 [README](README.md) 拆出独立成篇。

- **Smoke Test 1**：用 `t3.micro` 验证「open 预留 + ASG `capacity-reservations-first` → 实例自动落进预留」这套**机制链路**。
- **Smoke Test 2**：用真 `i4i.16xlarge` 6 台验证**现货能抢到 + ASG 拉起 + 3+3 AZ 均衡分布**。

---

## Smoke Test 1：t3.micro 验证机制

为了在真金白银抢 i4i 之前，先确认「**open 预留 + ASG `capacity-reservations-first` → 实例自动落进预留**」这套机制确实成立，跑了一次端到端 smoke test。

### 关键设计决策：用 t3.micro，不用 i4i.16xlarge

| 维度 | 说明 |
|------|------|
| **验证目标** | 验的是 ASG 吃预留的**机制**，不是 i4i 容量本身 |
| **为什么换机型** | `capacity-reservations-first` 的匹配逻辑**与机型无关**——对 t3.micro 成立，对 i4i.16xlarge 必然同样成立 |
| **成本** | t3.micro 2 台 + 2 个预留跑几分钟，全程 **< $0.05**；i4i.16xlarge 2 台约 $11/小时，且本来就可能抢不到容量，反而干扰结论 |
| **容量** | t3.micro 容量稳拿，不会卡在「抢不到」上，确保测的是机制而非运气 |

> ⚠️ 注意区分：本测试证明的是**机制链路**。Prime Day 真跑 i4i 时，能不能**抢到** i4i 预留是另一回事（取决于 AWS 池子有无货，靠 `grab_odcr.py --watch` 死等解决）。但只要预留抢到了，ASG 一定能把它吃进去——这一点已实锤。

### 测试步骤（实际执行）

环境：`us-east-1`，账户 `<ACCOUNT_ID>`，默认 VPC（`vpc-02f8...52d0`，原本只有 1c 一个子网）。所有资源打 `purpose=primeday-smoke-test` 标签，便于一键拆除。

1. **建子网**：默认 VPC 在 1b/1d 没子网，临时各建一个（`172.31.16.0/20`@1b、`172.31.32.0/20`@1d）。
2. **建 open 预留**：在 1b、1d 各建 1 个 `t3.micro` 容量预留，`platform=Linux/UNIX`、`tenancy=default`、`instance-match-criteria=open`（与生产 ODCR 同模式）。
3. **建启动模板**：AL2023 AMI（x86_64）、`t3.micro`、默认 SG。
4. **建 ASG**：横跨两个子网，`min=0 / max=2 / desired=0`，`CapacityReservationPreference=capacity-reservations-first`。
5. **记录基线**：两个预留 `Available=1`、未使用。
6. **触发扩容**：`set-desired-capacity --desired-capacity 2`，等约 90 秒实例起来。
7. **双向取证**（见下）。
8. **拆除**：删 ASG（force，连带终止实例）→ 取消两个预留（停计费）→ 删启动模板 → 删游离 ENI → 删两个子网。

### 结果：✅ 通过

两侧证据对得上，机制确认无误：

| 视角 | us-east-1b | us-east-1d |
|------|-----------|-----------|
| 实例的 `CapacityReservationId` | → `cr-…aa28` ✓ | → `cr-…459` ✓ |
| 预留 `Available`（扩容前 → 后） | 1 → **0** | 1 → **0** |

- **实例侧**：两台实例的 `CapacityReservationId` 字段直接写着对应 AZ 的预留 ID——实例自己记录了它落进了哪个预留。
- **预留侧**：两个预留的可用槽位同步从 1 掉到 **0**，证明槽位被实例占满。
- > 小注：CLI 表格里 `UsedInstanceCount` 字段渲染成 `null`/`None` 是 API 的显示怪癖，但 `Total(1) − Available(0) = 1` 在数学上就是「被占用 1 个」，结论不受影响。

### 清理结果：零残留

拆除后扫描 5 类资源（实例 / 预留 / 启动模板 / ASG / 子网），**全部为空**，无任何持续计费资源遗留。子网首次删除时因实例刚终止、ENI 未释放报 `DependencyViolation`，等 ENI 被 AWS 自动回收后重删成功——这是正常的资源释放时序。

---

## Smoke Test 2：真 i4i.16xlarge 端到端实测

第一份报告用 t3.micro 验证了**机制**。这一份是**真金白银的 i4i.16xlarge 实测**——直接抢真机型、用 ASG 拉起、看 AZ 分布对不对。

### 实验设计

| 项 | 值 |
|----|----|
| 机型 | **i4i.16xlarge**（64 vCPU、512 GiB / 台） |
| 规模 | **6 台 = 384 vCPU**，两 AZ 均衡 **3 + 3** |
| AZ | `us-east-1b` / `us-east-1d` |
| 单价 | $5.491/h·台（us-east-1 官方价） |
| 验证目标 | ① i4i 现货能否抢到 ② ASG 能否拉起 ③ **6 台 AZ 分布是否 3+3 均衡** ④ 每台是否落进对应 AZ 的预留 |
| 实际耗时/成本 | 计费窗口 ~3.1 分钟，总花费 **≈ $1.70** |
| 环境 | 账户 `<ACCOUNT_ID>`，默认 VPC `vpc-02f8425260c9c52d0`，所有资源打 `purpose=primeday-smoke2` 标签 |

> ⚠️ **ODCR 一创建就按台计费**（不管实例起没起）。所以流程上先把不花钱的资源（子网、启动模板）全搭好，**最后**才建预留，验完立刻拆，把计费窗口压到最短。

### 完整命令流水（实际跑的，原样保留）

变量约定（实测取值）：`AMI=ami-0521cb2d60cfbb1a6`(AL2023 x86_64)、`SG=sg-0380d51a3f9beb58c`、`SUB_1B=subnet-08a5399b09024d72c`、`SUB_1D=subnet-0e02f5af0269387ca`、`LT=lt-05aa1876395b2a26e`、`CR_1B=cr-0cbbf7a21e7a9ddbf`、`CR_1D=cr-037eb8095c0f22881`。

#### 第 0 步：环境侦察（不花钱）
```bash
# 账户 / 默认 VPC / 现有子网 / AL2023 x86_64 AMI / 默认 SG
aws sts get-caller-identity --query Account --output text --region us-east-1
aws ec2 describe-vpcs --region us-east-1 --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text
aws ssm get-parameter --region us-east-1 \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameter.Value' --output text
aws ec2 describe-security-groups --region us-east-1 \
  --filters Name=vpc-id,Values=$VPC Name=group-name,Values=default \
  --query 'SecurityGroups[0].GroupId' --output text
```

#### 第 1 步：建 1b/1d 子网（默认 VPC 原本只有 1c，不花钱）
```bash
aws ec2 create-subnet --region us-east-1 --vpc-id $VPC \
  --cidr-block 172.31.48.0/20 --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=purpose,Value=primeday-smoke2}]'
aws ec2 create-subnet --region us-east-1 --vpc-id $VPC \
  --cidr-block 172.31.64.0/20 --availability-zone us-east-1d \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=purpose,Value=primeday-smoke2}]'
```

#### 第 2 步：建 i4i 启动模板（不花钱）
```bash
aws ec2 create-launch-template --region us-east-1 \
  --launch-template-name primeday-smoke2-i4i \
  --tag-specifications 'ResourceType=launch-template,Tags=[{Key=purpose,Value=primeday-smoke2}]' \
  --launch-template-data '{"ImageId":"'$AMI'","InstanceType":"i4i.16xlarge","SecurityGroupIds":["'$SG'"],"TagSpecifications":[{"ResourceType":"instance","Tags":[{"Key":"purpose","Value":"primeday-smoke2"}]}]}'
```

#### 第 3 步：建 i4i ODCR（⚠️ 此刻开始计费）
```bash
# 每 AZ 各建 1 个 open 预留，instance-count=3
aws ec2 create-capacity-reservation --region us-east-1 \
  --instance-type i4i.16xlarge --instance-platform Linux/UNIX \
  --availability-zone us-east-1b --instance-count 3 --tenancy default \
  --instance-match-criteria open --end-date-type unlimited \
  --tag-specifications 'ResourceType=capacity-reservation,Tags=[{Key=purpose,Value=primeday-smoke2}]'
aws ec2 create-capacity-reservation --region us-east-1 \
  --instance-type i4i.16xlarge --instance-platform Linux/UNIX \
  --availability-zone us-east-1d --instance-count 3 --tenancy default \
  --instance-match-criteria open --end-date-type unlimited \
  --tag-specifications 'ResourceType=capacity-reservation,Tags=[{Key=purpose,Value=primeday-smoke2}]'
```
**核实预留全抢到**（关键节点：抢不全就停下报告现货情况）：
```bash
aws ec2 describe-capacity-reservations --region us-east-1 \
  --filters Name=tag:purpose,Values=primeday-smoke2 Name=state,Values=active \
  --query 'CapacityReservations[].[CapacityReservationId,AvailabilityZone,TotalInstanceCount,AvailableInstanceCount,State]' \
  --output table
```
```
+-----------------------+-------------+----+----+----------+
|  cr-037eb8095c0f22881 |  us-east-1d |  3 |  3 |  active  |   ← 1d 3/3 ✓
|  cr-0cbbf7a21e7a9ddbf |  us-east-1b |  3 |  3 |  active  |   ← 1b 3/3 ✓
+-----------------------+-------------+----+----+----------+
```
✅ **6 台 i4i.16xlarge 一次全抢到**——证明本次 us-east-1 的 i4i 现货充足。

#### 第 4 步：起 ASG（核心 —— 怎么拉起）
```bash
aws autoscaling create-auto-scaling-group --region us-east-1 \
  --auto-scaling-group-name primeday-smoke2-asg \
  --launch-template "LaunchTemplateId=$LT,Version=\$Latest" \
  --min-size 0 --max-size 6 --desired-capacity 6 \
  --vpc-zone-identifier "$SUB_1B,$SUB_1D" \
  --capacity-reservation-specification "CapacityReservationPreference=capacity-reservations-first" \
  --tags "Key=purpose,Value=primeday-smoke2,PropagateAtLaunch=true"
```
要点：
- **`--desired-capacity 6`** 一次拉满，ASG 自己往两 AZ 均衡放。
- **`--vpc-zone-identifier "$SUB_1B,$SUB_1D"`** 横跨两 AZ 子网——这是 ASG 能 3+3 分布的前提。
- **`CapacityReservationPreference=capacity-reservations-first`** 设在 ASG 上——让实例优先吃 open 预留。
- 不指定具体预留 ID，靠属性匹配（机型/平台/AZ/租户）自动落入。

#### 第 5 步：核实（双向取证 —— 怎么核实）
等约 90 秒实例 running，然后**两个方向**对证：

**(a) 实例侧** —— 看每台落在哪个 AZ、落进哪个预留：
```bash
aws ec2 describe-instances --region us-east-1 \
  --filters Name=tag:purpose,Values=primeday-smoke2 Name=instance-state-name,Values=pending,running \
  --query 'Reservations[].Instances[].[InstanceId,Placement.AvailabilityZone,State.Name,CapacityReservationId]' \
  --output table
```
```
+----------------------+-------------+----------+------------------------+
|  i-03bedfa1a9cd0d3aa |  us-east-1d |  running |  cr-037eb8095c0f22881  |
|  i-0aac963125478a5c2 |  us-east-1d |  running |  cr-037eb8095c0f22881  |
|  i-00fcc38325042f08d |  us-east-1d |  running |  cr-037eb8095c0f22881  |
|  i-0ffb18a55a627faf3 |  us-east-1b |  running |  cr-0cbbf7a21e7a9ddbf  |
|  i-0ee18df9de7670407 |  us-east-1b |  running |  cr-0cbbf7a21e7a9ddbf  |
|  i-09039679d99f6096d |  us-east-1b |  running |  cr-0cbbf7a21e7a9ddbf  |
+----------------------+-------------+----------+------------------------+
```
**AZ 分布计数**：
```bash
aws ec2 describe-instances --region us-east-1 \
  --filters Name=tag:purpose,Values=primeday-smoke2 Name=instance-state-name,Values=pending,running \
  --query 'Reservations[].Instances[].Placement.AvailabilityZone' --output text \
  | tr '\t' '\n' | sort | uniq -c
#       3 us-east-1b
#       3 us-east-1d
```

**(b) 预留侧** —— 看可用槽位被占满（3 → 0）：
```bash
aws ec2 describe-capacity-reservations --region us-east-1 \
  --filters Name=tag:purpose,Values=primeday-smoke2 Name=state,Values=active \
  --query 'CapacityReservations[].[CapacityReservationId,AvailabilityZone,TotalInstanceCount,AvailableInstanceCount]' \
  --output table
```
```
+-----------------------+--------------+----+----+
|  cr-037eb8095c0f22881 |  us-east-1d  |  3 |  0 |   ← Available 3→0
|  cr-0cbbf7a21e7a9ddbf |  us-east-1b  |  3 |  0 |   ← Available 3→0
+-----------------------+--------------+----+----+
```

#### 第 6 步：拆除（停止计费）
```bash
# 1. 删 ASG（force 连带终止 6 台实例）
aws autoscaling delete-auto-scaling-group --region us-east-1 \
  --auto-scaling-group-name primeday-smoke2-asg --force-delete
sleep 60                                  # 等实例终止、ENI 释放
# 2. 取消两个预留（停止按台计费 —— 最关键的止血动作）
aws ec2 cancel-capacity-reservation --region us-east-1 --capacity-reservation-id $CR_1B
aws ec2 cancel-capacity-reservation --region us-east-1 --capacity-reservation-id $CR_1D
# 3. 删启动模板
aws ec2 delete-launch-template --region us-east-1 --launch-template-id $LT
# 4. 删两个子网（实例终止快、ENI 已释放，本次首删即成）
aws ec2 delete-subnet --region us-east-1 --subnet-id $SUB_1B
aws ec2 delete-subnet --region us-east-1 --subnet-id $SUB_1D
```
**零残留扫描**（实例/预留/启动模板/ASG/子网/游离 ENI 六项全空）：
```bash
aws ec2 describe-instances --region us-east-1 --filters Name=tag:purpose,Values=primeday-smoke2 \
  Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down --query 'Reservations[].Instances[].InstanceId' --output text
aws ec2 describe-capacity-reservations --region us-east-1 --filters Name=tag:purpose,Values=primeday-smoke2 \
  Name=state,Values=active,pending --query 'CapacityReservations[].CapacityReservationId' --output text
# ...启动模板 / ASG / 子网 / ENI 同理，全部返回空
```

### 结论

| 验证项 | 结果 |
|--------|------|
| **i4i.16xlarge 现货能否抢到** | ✅ 6 台一次全抢到（1b 3/3、1d 3/3），本次 us-east-1 现货充足 |
| **ASG 能否拉起** | ✅ 6 台全部 `running` |
| **AZ 分布是否 3+3 均衡** | ✅ **完美 3 + 3**（1b 3 台、1d 3 台），ASG 自动均衡 |
| **每台是否落进对应 AZ 预留** | ✅ 全部命中：1b 3 台→`cr-…ddbf`，1d 3 台→`cr-…2881`；预留 Available 同步 3→0 |
| **成本控制** | ✅ 计费窗口 ~3.1 分钟，总花费 **≈ $1.70** |
| **零残留** | ✅ 六类资源扫描全空，无持续计费遗留 |

**一句话**：真 i4i.16xlarge 上，`grab_odcr.py` 抢预留 + ASG `capacity-reservations-first` + 跨两 AZ 子网 = 实例**自动 3+3 均衡落进预留**，机制与分布双双验证通过。Prime Day 把规模从 6 台放大到 ~156 台（10000 vCPU）即可，命令结构完全一致。
