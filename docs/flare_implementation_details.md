# Flare Transport Protocol 实现原理

本文档详细说明 Flare 论文中各个设计点在 OpenOptics ns-3 backend 中的具体实现。

## 目录

- [1. 系统架构](#1-系统架构)
- [2. Phase 1: Credit-Data Path Symmetry](#2-phase-1-credit-data-path-symmetry)
- [3. Phase 2: Probabilistic Credit Admission](#3-phase-2-probabilistic-credit-admission)
- [4. Phase 3: Credit Rate Control](#4-phase-3-credit-rate-control)
- [5. Phase 4: Optimizations](#5-phase-4-optimizations)
- [6. 数据结构](#6-数据结构)
- [7. 参数配置](#7-参数配置)

---

## 1. 系统架构

### 1.1 组件架构

```
┌─────────────────────────────────────────────────────────────┐
│                     Flare 端到端流程                         │
└─────────────────────────────────────────────────────────────┘

Sender Host                ToR Switch              Receiver Host
┌──────────┐              ┌──────────┐              ┌──────────┐
│ FlareHost│              │  TorApp  │              │ FlareHost│
│   App    │              │          │              │   App    │
│          │              │          │              │          │
│ ┌──────┐ │   DATA pkt   │ ┌──────┐ │              │ ┌──────┐ │
│ │Sender│ │─────────────>│ │Admit │ │─────────────>│ │Recv  │ │
│ │Flow  │ │              │ │Check │ │              │ │Flow  │ │
│ │State │ │<─────────────│ │      │ │<─────────────│ │State │ │
│ └──────┘ │ CREDIT pkt   │ │Queue │ │ CREDIT pkt   │ └──────┘ │
│          │              │ └──────┘ │              │          │
│ Rate Ctl │              │Prob Admit│              │Credit Gen│
└──────────┘              └──────────┘              └──────────┘
```

### 1.2 核心文件

| 文件 | 职责 |
|------|------|
| `flare-header.h/cc` | Flare 包头定义（PacketType, ControlCode） |
| `flare-host-app.h/cc` | Host 端逻辑（发送/接收、速率控制） |
| `openoptics-tor-app.h/cc` | ToR 端逻辑（接纳控制、队列管理） |
| `traffic.py` | Python 层配置接口 |
| `backend.py` | ns-3 后端集成 |

---

## 2. Phase 1: Credit-Data Path Symmetry

### 2.1 论文设计

**问题**: Credit 和 data 走不同路径导致 credit 浪费

**解决方案**: 
- ToR 在 credit 入口处戳 time_slice
- Receiver 记录每个 credit 的 time_slice
- Data 包继承对应 credit 的 time_slice

### 2.2 实现细节

#### 2.2.1 FlareHeader 扩展

**文件**: `flare-header.h`

```cpp
class FlareHeader : public Header
{
  private:
    uint8_t m_timeSlice;  // 新增字段：时间片标识
    
  public:
    void SetTimeSlice(uint8_t ts);
    uint8_t GetTimeSlice() const;
};
```

**序列化**:
- `Serialize()`: 将 `m_timeSlice` 写入包头
- `Deserialize()`: 从包头读取 `m_timeSlice`
- 包头大小增加 1 字节

#### 2.2.2 ToR 入口戳时间片

**文件**: `openoptics-tor-app.cc`

```cpp
void TorApp::HandleDownlinkPacket(Ptr<Packet> packet, ...)
{
    FlareHeader flare;
    packet->PeekHeader(flare);
    
    // 只对 CREDIT 包戳时间片
    if (flare.GetType() == FlareHeader::CREDIT ||
        flare.GetType() == FlareHeader::TENTATIVE_CREDIT)
    {
        uint32_t current_slice = CurrentSlice();  // 获取当前时间片
        
        // 移除旧包头，修改后重新添加
        packet->RemoveHeader(flare);
        flare.SetTimeSlice(static_cast<uint8_t>(current_slice));
        packet->AddHeader(flare);
    }
}
```

#### 2.2.3 Receiver 记录 credit 时间片

**文件**: `flare-host-app.cc`

```cpp
void FlareHostApp::HandleCredit(uint32_t flow_id, uint32_t seq,
                                uint8_t time_slice)
{
    FlowState* flow = FindSenderFlow(flow_id);
    
    // 记录这个 seq 的 credit 到达时的时间片
    flow->creditTimeSliceMap[seq] = time_slice;
    
    // 将 seq 加入可发送队列
    flow->creditSeqs.insert(seq);
}
```

**数据结构**:
```cpp
std::unordered_map<uint32_t, uint8_t> creditTimeSliceMap;  // seq -> time_slice
```

#### 2.2.4 Sender 继承时间片到 data 包

**文件**: `flare-host-app.cc`

```cpp
void FlareHostApp::SendFlarePacket(FlowState& flow, uint32_t seq,
                                   uint8_t packet_type, ...)
{
    FlareHeader flare;
    
    // DATA 包继承对应 credit 的时间片
    if (packet_type == FlareHeader::DATA)
    {
        auto it = flow.creditTimeSliceMap.find(seq);
        if (it != flow.creditTimeSliceMap.end())
        {
            flare.SetTimeSlice(it->second);  // 继承
        }
        else
        {
            flare.SetTimeSlice(0);  // 默认值（首次发送可能没有 credit）
        }
    }
    else  // CREDIT 包
    {
        // ToR 会在入口处设置，这里填 0
        flare.SetTimeSlice(0);
    }
    
    packet->AddHeader(flare);
    // 发送...
}
```

**工作流程**:
```
1. Receiver 发送 credit(seq=10)，time_slice=0
2. ToR 入口：credit.time_slice = 3（当前时间片）
3. Sender 收到 credit(seq=10, time_slice=3)
4. Sender 记录：creditTimeSliceMap[10] = 3
5. Sender 发送 data(seq=10)：查表，设置 time_slice=3
6. Data 包按时间片 3 的路径转发
```

### 2.3 效果

- **Path Symmetry**: Credit 和 data 走相同时间片的路径
- **Credit Efficiency**: 减少因路径不匹配导致的 credit 浪费
- **开销**: 每个包头增加 1 字节

### 2.4 问题

- *只记录了时间戳能否保证按时间戳对应的时间片的路径发送*

---

## 3. Phase 2: Probabilistic Credit Admission

### 3.1 论文设计

**问题**: ToR credit 队列拥塞导致 credit 丢失

**解决方案**: 
- 队列空闲时：全部接纳
- 队列拥塞时：基于 remaining_hops 概率接纳
- 概率公式：`P(h) = (1/2)^(h-1)`

### 3.2 实现细节

#### 3.2.1 接纳概率预计算

**文件**: `openoptics-tor-app.cc`

```cpp
class TorApp : public Application
{
  private:
    std::vector<double> m_admissionProbTable;  // 索引=hops, 值=概率
    
    void InitAdmissionProbTable()
    {
        m_admissionProbTable.resize(9, 0.0);  // 支持 1-8 hops
        for (uint8_t h = 1; h <= 8; ++h)
        {
            m_admissionProbTable[h] = std::pow(0.5, static_cast<double>(h - 1));
        }
        // m_admissionProbTable[1] = 1.0
        // m_admissionProbTable[2] = 0.5
        // m_admissionProbTable[3] = 0.25
        // ...
    }
};
```

#### 3.2.2 核心接纳逻辑

**文件**: `openoptics-tor-app.cc`

```cpp
bool TorApp::AdmitFlareCredit(uint32_t send_ts, uint32_t send_port,
                               const FlareHeader& flare)
{
    uint32_t& occupancy = m_flareCreditPktsPerSlot[send_ts][send_port];
    
    // 1. 始终拒绝超过队列容量的包
    if (occupancy >= m_flareCreditQsizePkts)
    {
        ++m_flareCreditDropped;
        return false;
    }
    
    // 2. 低于拥塞阈值：全部接纳
    const uint32_t congestion_thresh =
        m_flareCreditQsizePkts * m_flareCongestionThresholdPercent / 100;
    
    if (occupancy < congestion_thresh)
    {
        ++occupancy;
        ++m_flareCreditAdmitted;
        return true;
    }
    
    // 3. 超过拥塞阈值：概率接纳
    const uint8_t hops = flare.GetRemainingHops();
    const double admit_prob = GetAdmissionProb(hops);  // 查表
    const double rand_val = m_flareAdmissionRng->GetValue();  // [0,1]
    
    if (rand_val <= admit_prob)
    {
        ++occupancy;
        ++m_flareCreditAdmitted;
        return true;
    }
    else
    {
        ++m_flareCreditDropped;
        return false;
    }
}
```

**工作示例**:

假设：
- `m_flareCreditQsizePkts = 60`
- `m_flareCongestionThresholdPercent = 50` → threshold = 30

| 当前占用 | Hops | P(h) | 随机数 | 结果 |
|---------|------|------|--------|------|
| 20      | 3    | 0.25 | -      | **Accept** (< 30) |
| 35      | 1    | 1.0  | 0.3    | **Accept** (0.3 ≤ 1.0) |
| 35      | 2    | 0.5  | 0.7    | **Drop** (0.7 > 0.5) |
| 35      | 3    | 0.25 | 0.2    | **Accept** (0.2 ≤ 0.25) |
| 35      | 3    | 0.25 | 0.9    | **Drop** (0.9 > 0.25) |
| 62      | 1    | 1.0  | -      | **Drop** (≥ 60) |

### 3.3 效果

- **优先短路径**: 1-hop 流量 100% 通过，3-hop 流量只有 25% 通过
- **减少拥塞**: 在拥塞时主动丢弃长路径 credit，保护短路径
- **参数可调**: `congestion_threshold_percent` 控制何时启用概率接纳

---

## 4. Phase 3: Credit Rate Control

### 4.1 论文设计

**问题**: Per-flow controller 需要动态调整 credit 发送速率

**解决方案**:
- 每个 flow 维护 `targetCreditRate` (0-1)
- Regular credit vs Tentative credit
- 基于 loss 的 AIMD 速率控制
- 路径变化时补偿

### 4.2 实现细节

#### 4.2.1 FlowState 扩展

**文件**: `flare-host-app.h`

```cpp
struct FlowState
{
    // Rate control 状态
    double targetCreditRate = 1.0;      // 目标 credit rate (0-1)
    uint32_t lossCountThisSlice = 0;    // 当前周期的 loss 计数
    uint8_t lastPathLength = 0;         // 上一次的路径长度
    Time lastSliceChange;               // 上次调整时间
};
```

#### 4.2.2 Tentative Credit 决策

**文件**: `flare-host-app.cc`

```cpp
void FlareHostApp::SendCredit(FlowState& flow, uint32_t seq)
{
    // 生成 [0, 1] 随机数
    double rand_val = m_flareAdmissionRng->GetValue();
    
    // 根据 targetCreditRate 决定类型
    bool is_tentative = (rand_val > flow.targetCreditRate);
    
    uint8_t pkt_type = is_tentative
        ? FlareHeader::TENTATIVE_CREDIT
        : FlareHeader::CREDIT;
    
    SendFlarePacket(flow, seq, pkt_type, ...);
}
```

**逻辑**:
```
targetCreditRate = 1.0 → 所有 credit 都是 regular
targetCreditRate = 0.5 → 50% regular, 50% tentative
targetCreditRate = 0.1 → 10% regular, 90% tentative
```

#### 4.2.3 Tentative Credit 接纳

**文件**: `openoptics-tor-app.cc`

```cpp
bool TorApp::AdmitFlareCredit(uint32_t send_ts, uint32_t send_port,
                               const FlareHeader& flare)
{
    uint32_t& occupancy = m_flareCreditPktsPerSlot[send_ts][send_port];
    
    const bool is_tentative =
        (flare.GetType() == FlareHeader::TENTATIVE_CREDIT);
    
    // Tentative credit 只在队列极低时接纳
    if (is_tentative)
    {
        const uint32_t tentative_thresh =
            m_flareCreditQsizePkts * m_flareTentativeThresholdPercent / 100;
        
        if (occupancy >= tentative_thresh)
        {
            ++m_flareCreditDropped;
            return false;  // 拒绝
        }
        // 通过
        ++occupancy;
        ++m_flareCreditAdmitted;
        return true;
    }
    
    // Regular credit 走正常接纳逻辑（概率接纳）
    // ...
}
```

**接纳条件对比**:

| Credit 类型 | 接纳条件 |
|------------|---------|
| Regular | 占用 < 50%: 全接纳<br>占用 ≥ 50%: 概率接纳 P(h) |
| Tentative | **仅当占用 < 25% 接纳** |

#### 4.2.4 AIMD 速率控制

**文件**: `flare-host-app.cc`

```cpp
void FlareHostApp::AdjustCreditRate(FlowState& flow, bool credit_dropped)
{
    // 1. 累计 loss
    if (credit_dropped) {
        ++flow.lossCountThisSlice;
    }
    
    // 2. 每个 RTO 周期调整一次
    Time now = Simulator::Now();
    if (now - flow.lastSliceChange > m_retransmissionTimeout)
    {
        // 3. AIMD 调整
        if (flow.lossCountThisSlice > 0)
        {
            // 乘性减
            flow.targetCreditRate *= (1.0 - m_targetLoss);
        }
        else
        {
            // 乘性增（限制上界 1.0）
            flow.targetCreditRate = std::min(1.0,
                flow.targetCreditRate * (1.0 + m_targetLoss));
        }
        
        // 4. 重置计数
        flow.lossCountThisSlice = 0;
        flow.lastSliceChange = now;
    }
}
```

**AIMD 行为示例** (m_targetLoss = 0.1):

```
周期 | Loss | 操作 | Rate
-----|------|------|------
  0  |  -   |  初始 | 1.000
  1  |  0   | ×1.1 | 1.000 (capped)
  2  |  2   | ×0.9 | 0.900
  3  |  0   | ×1.1 | 0.990
  4  |  1   | ×0.9 | 0.891
  5  |  3   | ×0.9 | 0.802
  6  |  0   | ×1.1 | 0.882
```

#### 4.2.5 路径变化补偿

**文件**: `flare-host-app.cc`

```cpp
void FlareHostApp::OnPathChange(FlowState& flow, uint8_t new_path_length)
{
    // 计算接纳概率变化
    double old_prob = std::pow(0.5, flow.lastPathLength - 1.0);
    double new_prob = std::pow(0.5, new_path_length - 1.0);
    double delta = new_prob - old_prob;
    
    // 补偿 targetCreditRate
    flow.targetCreditRate = std::min(1.0,
        std::max(0.1, flow.targetCreditRate + delta));
    
    flow.lastPathLength = new_path_length;
}
```

**补偿示例**:

| 路径变化 | old_prob | new_prob | delta | Rate 调整 |
|---------|----------|----------|-------|----------|
| 2→1 hops | 0.5 | 1.0 | +0.5 | rate + 0.5 (路径变好) |
| 1→2 hops | 1.0 | 0.5 | -0.5 | rate - 0.5 (路径变差) |
| 2→3 hops | 0.5 | 0.25 | -0.25 | rate - 0.25 |
| 3→2 hops | 0.25 | 0.5 | +0.25 | rate + 0.25 |

**原理**: 路径长度改变 → 接纳概率改变 → 需要调整发送速率来适应

### 4.3 效果

- **动态适应**: 根据网络拥塞自动调整速率
- **Tentative 探测**: 低速率时用 tentative credit 探测
- **路径感知**: 路径切换时主动补偿

---

## 5. Phase 4: Optimizations

### 5.1 Hop Jittering

#### 5.1.1 论文设计

**问题**: 固定 hop count 声明导致不公平

**解决方案**: 
- 短流：减少声明的 hop count
- 长流：增加声明的 hop count
- 50% 概率应用

#### 5.1.2 实现

**文件**: `flare-host-app.cc`

```cpp
void FlareHostApp::SendFlarePacket(FlowState& flow, uint32_t seq,
                                   uint8_t packet_type, ...)
{
    uint8_t actual_hops = 1;  // 默认
    
    // 仅对 CREDIT 包应用 jittering
    if (packet_type == FlareHeader::CREDIT ||
        packet_type == FlareHeader::TENTATIVE_CREDIT)
    {
        // 估算 BDP
        uint64_t bdp_estimate = flow.initialCreditPkts * flow.packetSizeBytes;
        uint64_t delivered_bytes = flow.receivedSeqs.size() * flow.packetSizeBytes;
        double jitter_ratio = delivered_bytes / bdp_estimate;
        
        // 从 histogram 获取最常见的 hop count
        for (const auto& kv : flow.pathLengthHistogram)
        {
            if (kv.second > max_count)
                actual_hops = kv.first;
        }
        
        // 50% 概率 jittering
        if (rand_val < 0.5)
        {
            if (jitter_ratio < 4.0 && actual_hops > 1)
                actual_hops -= 1;  // 短流减 1
            else if (jitter_ratio > 16.0 && actual_hops < 8)
                actual_hops += 1;  // 长流加 1
        }
    }
    
    flare.SetRemainingHops(actual_hops);
}
```

**效果**: 短流获得更高优先级（更小 hop count → 更高接纳概率）

### 5.2 Fast Start (Aeolus Integration)

#### 5.2.1 论文设计

**问题**: 小流量初始需要等待 credit 往返

**解决方案**: 第一个 BDP 的数据作为 UNSCHEDULED 发送

#### 5.2.2 实现 - UNSCHEDULED 标记

**文件**: `flare-header.h`

```cpp
enum ControlCode : uint8_t
{
    NONE = 0,
    FLOW_DONE = 1,
    UNSCHEDULED = 2,  // 新增
};
```

**文件**: `flare-host-app.cc`

```cpp
void FlareHostApp::SendData(FlowState& flow, uint32_t seq, bool retransmission)
{
    // 估算 BDP
    uint64_t bdp_estimate = flow.initialCreditPkts * flow.packetSizeBytes;
    uint64_t sent_bytes = seq * flow.packetSizeBytes;
    
    // 第一个 BDP 内的数据标记为 UNSCHEDULED
    bool is_unscheduled = (sent_bytes < bdp_estimate);
    
    uint8_t control_code = is_unscheduled
        ? FlareHeader::UNSCHEDULED
        : FlareHeader::NONE;
    
    SendFlarePacket(flow, seq, FlareHeader::DATA, control_code, ...);
}
```

#### 5.2.3 实现 - Aeolus 主动丢弃

**文件**: `openoptics-tor-app.cc`

```cpp
void TorApp::DrainSlice(uint32_t slice)
{
    for (uint32_t uplink = 0; uplink < m_cq.size(); ++uplink)
    {
        while (m_cq[uplink].Peek(slice, &pkt, &cookie))
        {
            FlareHeader flare;
            PeekFlareHeader(pkt, &flare);
            
            // Aeolus: 主动丢弃 UNSCHEDULED 包
            if (flare.GetType() == FlareHeader::DATA &&
                flare.GetControlCode() == FlareHeader::UNSCHEDULED)
            {
                uint64_t data_queue_bytes =
                    m_cqBytesPerSlot[slice][uplink];
                uint64_t aeolus_thresh_bytes =
                    m_flareAeolusThreshPkts * 1024;
                
                if (data_queue_bytes > aeolus_thresh_bytes)
                {
                    m_cq[uplink].Dequeue(slice, &pkt, &cookie);
                    ++m_dropAeolusUnscheduled;
                    continue;  // 丢弃
                }
            }
            
            // 正常处理...
        }
    }
}
```

**工作原理**:
```
1. 小流启动：发送第一个 BDP 的 UNSCHEDULED 数据
2. 如果网络空闲：UNSCHEDULED 包正常通过
3. 如果网络拥塞：ToR 主动丢弃 UNSCHEDULED 包
4. 丢弃后：流退回到 credit-based 传输
```

### 5.3 效果

- **Hop Jittering**: 短流优先
- **Fast Start**: 小流快速启动
- **Aeolus**: 拥塞时保护 scheduled 流量

---

## 6. 数据结构

### 6.1 FlowState (Host)

```cpp
struct FlowState
{
    // 基础信息
    uint32_t flowId;
    uint32_t peerNode;
    Ipv4Address localIp, peerIp;
    uint64_t sizeBytes;
    uint32_t packetSizeBytes;
    
    // 发送/接收状态
    std::unordered_set<uint32_t> sentSeqs;
    std::unordered_set<uint32_t> receivedSeqs;
    std::unordered_set<uint32_t> creditSeqs;  // 可用 credit
    
    // Phase 1: Path Symmetry
    std::unordered_map<uint32_t, uint8_t> creditTimeSliceMap;
    
    // Phase 3: Rate Control
    double targetCreditRate;
    uint32_t lossCountThisSlice;
    uint8_t lastPathLength;
    Time lastSliceChange;
    
    // Phase 4: Hop Jittering
    std::unordered_map<uint32_t, uint32_t> pathLengthHistogram;
};
```

### 6.2 TorApp (Switch)

```cpp
class TorApp : public Application
{
  private:
    // Phase 2: Probabilistic Admission
    std::vector<double> m_admissionProbTable;     // P(h) = (1/2)^(h-1)
    Ptr<UniformRandomVariable> m_flareAdmissionRng;
    uint32_t m_flareCreditQsizePkts;              // Credit 队列大小
    uint32_t m_flareCongestionThresholdPercent;   // 拥塞阈值 (%)
    
    // Phase 3: Tentative Credit
    uint32_t m_flareTentativeThresholdPercent;    // Tentative 阈值 (%)
    
    // Phase 4: Aeolus
    uint32_t m_flareAeolusThreshPkts;             // Aeolus 丢弃阈值
    
    // 统计
    uint64_t m_flareCreditAdmitted;
    uint64_t m_flareCreditDropped;
    uint64_t m_dropAeolusUnscheduled;
};
```

---

## 7. 参数配置

### 7.1 Python 配置接口

**文件**: `traffic.py`

```python
@dataclass(frozen=True)
class FlareConfig:
    profile: str = "55us"
    credit_qsize_pkts: Optional[int] = None
    shaping_thresh_pkts: Optional[int] = None
    aeolus_thresh_pkts: Optional[int] = None
    w_init: Optional[float] = None
    target_loss: Optional[float] = None
    mtu_bytes: int = 1024
    retransmission_timeout_s: float = 0.0002
    initial_credit_pkts: Optional[int] = None
    congestion_threshold_percent: int = 50      # Phase 2
    tentative_threshold_percent: int = 25       # Phase 3
```

### 7.2 Profile 预设

**55us Profile** (Opera, 55μs time slice):
```python
{
    "credit_qsize_pkts": 60,
    "shaping_thresh_pkts": 30,
    "aeolus_thresh_pkts": 40,
    "w_init": 1.0,
    "target_loss": 0.1,
    "initial_credit_pkts": 15,
}
```

**15us Profile** (Sirius, 15μs time slice):
```python
{
    "credit_qsize_pkts": 80,
    "shaping_thresh_pkts": 40,
    "aeolus_thresh_pkts": 50,
    "w_init": 1.0,
    "target_loss": 0.1,
    "initial_credit_pkts": 20,
}
```

### 7.3 关键参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `credit_qsize_pkts` | 60 | Credit 队列大小（包数） |
| `congestion_threshold_percent` | 50 | 概率接纳触发阈值（队列占用 %） |
| `tentative_threshold_percent` | 25 | Tentative credit 接纳阈值（队列占用 %） |
| `aeolus_thresh_pkts` | 40 | Aeolus 主动丢弃阈值（data 队列） |
| `w_init` | 1.0 | 初始 credit rate（100%） |
| `target_loss` | 0.1 | AIMD 调整步长（10%） |
| `initial_credit_pkts` | 15 | 初始 BDP 估算（credit 数量） |
| `retransmission_timeout_s` | 0.0002 | RTO 超时时间（200μs） |

---

## 9. 验证与测试

### 9.1 单元测试

**文件**：`tests/test_ns3_flare_phases.py`

#### 9.1.1 测试覆盖

| 测试用例 | 验证目标 | 通过标准 |
|---------|---------|---------|
| `test_phase1_path_symmetry` | Credit 和 data 走相同路径 | time_slice 字段正确传递 |
| `test_phase2_probabilistic_admission` | Hop-aware 概率接纳 | 1-hop 通过率 > 3-hop 通过率 |
| `test_phase2_congestion_threshold` | 拥塞阈值触发 | 低占用全接纳，高占用概率接纳 |
| `test_phase3_rate_control` | AIMD 速率调整 | Loss 时减速，无 loss 时加速 |
| `test_phase3_tentative_credit` | Tentative credit 区分 | Tentative 只在低占用接纳 |
| `test_phase4_hop_jittering` | Hop count 调整 | 短流减 hop，长流加 hop |
| `test_phase4_fast_start` | Unscheduled 标记 | 第一个 BDP 标记为 UNSCHEDULED |
| `test_phase4_aeolus_dropping` | 主动丢弃 | Data 队列拥塞时丢弃 UNSCHEDULED |
| `test_integration_all_phases` | 端到端集成 | 所有机制同时工作 |

#### 9.1.2 运行测试

```bash
# 全部测试
python -m pytest tests/test_ns3_flare_phases.py -v

# 生成覆盖率报告
python -m pytest tests/test_ns3_flare_phases.py --cov=openoptics.backends.ns3 --cov-report=html
```

### 9.2 微基准测试

**文件**：`examples/ns3_flare_microbench.py`

#### 9.2.1 测试场景

1. **Single Flow**: 验证基础传输正确性
2. **2-to-1 Incast**: 验证 credit 调度
3. **Varying Path Lengths**: 验证 hop-aware 行为
4. **Rate Adaptation**: 验证速率控制
5. **Fast Start**: 验证快速启动
6. **Congestion Response**: 验证拥塞响应

### 9.3 回归测试

确保新功能不破坏现有功能：

```bash
# 运行所有 Flare 相关测试
python -m pytest tests/test_ns3_flare*.py -v

# 运行完整 ns-3 测试套件
python -m pytest tests/test_ns3*.py -v
```

---

## 10. 总结

### 10.1 实现完成度

| Phase | 功能 | 实现状态 | 关键文件 |
|-------|------|---------|----------|
| **Phase 1** | Credit-Data Path Symmetry | ✅ 100% | flare-header.h/cc<br>flare-host-app.cc<br>openoptics-tor-app.cc |
| **Phase 2** | Probabilistic Credit Admission | ✅ 100% | openoptics-tor-app.cc:762-789 |
| **Phase 3** | Credit Rate Control | ✅ 100% | flare-host-app.cc:AdjustCreditRate<br>flare-host-app.cc:OnPathChange |
| **Phase 3** | Tentative Credits | ✅ 100% | flare-host-app.cc:SendCredit<br>openoptics-tor-app.cc:AdmitFlareCredit |
| **Phase 4** | Hop Jittering | ✅ 100% | flare-host-app.cc:SendFlarePacket |
| **Phase 4** | Fast Start / Aeolus | ✅ 100% | flare-host-app.cc:SendData<br>openoptics-tor-app.cc:DrainSlice |

### 10.2 关键数据结构映射

| 论文概念 | 实现位置 | 说明 |
|---------|---------|------|
| time_slice | FlareHeader::m_timeSlice | 1 字节，uint8_t |
| remaining_hops | FlareHeader::m_remainingHops | 用于概率接纳 |
| PacketType::TENTATIVE | FlareHeader::TENTATIVE_CREDIT | 值 = 2 |
| ControlCode::UNSCHEDULED | FlareHeader::UNSCHEDULED | 值 = 2 |
| targetCreditRate | FlowState::targetCreditRate | 0.0 - 1.0 |
| P(h) table | TorApp::m_admissionProbTable | 预计算 |

### 10.3 配置参数速查

```python
FlareConfig(
    profile="55us",                         # 55us or 15us
    congestion_threshold_percent=50,        # Phase 2: 概率接纳触发
    tentative_threshold_percent=25,         # Phase 3: Tentative 阈值
    credit_qsize_pkts=60,                   # Credit 队列大小
    aeolus_thresh_pkts=40,                  # Phase 4: Aeolus 丢弃阈值
    w_init=1.0,                             # 初始 rate = 100%
    target_loss=0.1,                        # AIMD 步长 = 10%
    initial_credit_pkts=15,                 # 初始 BDP 估算
    retransmission_timeout_s=0.0002,        # RTO = 200μs
)
```
