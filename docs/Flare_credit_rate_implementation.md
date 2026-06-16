# Flare 速率控制实现详解

  1. 核心数据结构（FlowState）

  每个 flow 维护以下速率控制状态：

  ```C
  struct FlowState {
      // Rate control 状态
      double targetCreditRate = 1.0;      // 目标 credit rate (0-1)
      uint32_t lossCountThisSlice = 0;    // 当前时间片的 loss 计数
      uint8_t lastPathLength = 0;         // 上一次的路径长度
      Time lastSliceChange;               // 上次时间片切换时间
  };
  ```

  2. 三个核心机制

  机制 1: SendCredit() - Tentative Credit 决策

  ```C
  void FlareHostApp::SendCredit(FlowState& flow, uint32_t seq)
  {
      // 生成随机数 [0, 1]
      double rand_val = m_flareAdmissionRng->GetValue();

      // 根据 targetCreditRate 决定是否发送 tentative credit
      bool is_tentative = (rand_val > flow.targetCreditRate);

      uint8_t pkt_type = is_tentative
          ? FlareHeader::TENTATIVE_CREDIT  // Tentative
          : FlareHeader::CREDIT;           // Regular

      SendFlarePacket(flow, seq, pkt_type, ...);
  }
  ```

  工作原理：
  - targetCreditRate = 1.0: 所有 credit 都是 regular（rand_val 永远 ≤ 1.0）
  - targetCreditRate = 0.5: 50% regular, 50% tentative
  - targetCreditRate = 0.1: 10% regular, 90% tentative

  机制 2: AdjustCreditRate() - 基于 Loss 的速率调整

  ```C
  void FlareHostApp::AdjustCreditRate(FlowState& flow, bool credit_dropped)
  {
      // 1. 记录 credit 是否被丢弃
      if (credit_dropped) {
          ++flow.lossCountThisSlice;
      }

      // 2. 检查是否到达时间片边界（每个 RTO 周期检查一次）
      Time now = Simulator::Now();
      if (now - flow.lastSliceChange > m_retransmissionTimeout)
      {
          // 3. 根据 loss 调整 rate
          if (flow.lossCountThisSlice > 0)
          {
              // 有 loss: 降速
              flow.targetCreditRate *= (1.0 - m_targetLoss);
          }
          else
          {
              // 无 loss: 加速
              flow.targetCreditRate = std::min(1.0,
                  flow.targetCreditRate * (1.0 + m_targetLoss));
          }

          // 4. 重置计数器，进入下一个周期
          flow.lossCountThisSlice = 0;
          flow.lastSliceChange = now;
      }
  }
  ```

  AIMD 速率控制：
  loss > 0:  rate = rate × (1 - target_loss)     [乘性减]
  loss = 0:  rate = rate × (1 + target_loss)     [乘性增]
                    但 rate ≤ 1.0

  示例（target_loss = 0.1）：
  初始: rate = 1.0
  周期1: loss=0 → rate = 1.0 × 1.1 = 1.0 (capped)
  周期2: loss=2 → rate = 1.0 × 0.9 = 0.9
  周期3: loss=0 → rate = 0.9 × 1.1 = 0.99
  周期4: loss=1 → rate = 0.99 × 0.9 = 0.891

  机制 3: OnPathChange() - 路径变化补偿

  ```C
  void FlareHostApp::OnPathChange(FlowState& flow, uint8_t new_path_length)
  {
      // 1. 计算旧路径和新路径的接纳概率
      // Flare 概率接纳：P(h) = (1/2)^(h-1)
      double old_prob = std::pow(0.5, flow.lastPathLength - 1.0);
      double new_prob = std::pow(0.5, new_path_length - 1.0);
      double delta = new_prob - old_prob;

      // 2. 按概率差值补偿 targetCreditRate
      flow.targetCreditRate = std::min(1.0,
          std::max(0.1, flow.targetCreditRate + delta));

      flow.lastPathLength = new_path_length;
  }
  ```

  补偿原理：
  路径从 2 hops → 3 hops:
    old_prob = (1/2)^(2-1) = 0.5
    new_prob = (1/2)^(3-1) = 0.25
    delta = -0.25
    rate = rate - 0.25  [降低 rate 因为新路径更容易被丢]

  路径从 3 hops → 1 hop:
    old_prob = 0.25
    new_prob = 1.0
    delta = +0.75
    rate = rate + 0.75  [提高 rate 因为新路径更容易通过]

  3. 完整工作流程

  ```text
                ┌─────────────────┐
                │  Receiver 端    │
                │ SendCredit()    │
                └────────┬────────┘
                         │
                         ↓
             根据 targetCreditRate 决策
                         │
            ┌────────────┴────────────┐
            ↓                         ↓
      Regular Credit          Tentative Credit
      (rand ≤ rate)           (rand > rate)
            │                         │
            └────────────┬────────────┘
                         │
                         ↓
                在 ToR 上概率接纳
                         │
            ┌────────────┴────────────┐
            ↓                         ↓
         Admitted                  Dropped
            │                         │
            ↓                         ↓
      到达 Sender             触发 AdjustCreditRate
                                      │
                                      ↓
                          lossCountThisSlice++
                                      │
                         ┌────────────┴────────────┐
                         ↓                         ↓
              每个 RTO 周期检查              检测路径变化
                         │                         │
              ┌──────────┴──────────┐             ↓
              ↓                     ↓         OnPathChange()
        loss > 0              loss = 0            │
              │                     │             ↓
        rate × 0.9            rate × 1.1    rate += delta
              │                     │             │
              └──────────┬──────────┘             │
                         │                        │
                         └────────────┬───────────┘
                                      │
                                      ↓
                            下一轮 SendCredit

  ```
  4. 关键设计点

  为什么用 Tentative Credit？

  Regular credit 和 tentative credit 的区别：

  ┌──────────┬───────────────────────────────────────────────┬────────────────────────────┐
  │          │                Regular Credit                 │      Tentative Credit      │
  ├──────────┼───────────────────────────────────────────────┼────────────────────────────┤
  │ 接纳条件 │ 队列 < 50% → 全接纳队列 ≥ 50% → 概率接纳 P(h) │ 仅当队列 < 25% 接纳        │
  ├──────────┼───────────────────────────────────────────────┼────────────────────────────┤
  │ 目的     │ 正常流量传输                                  │ 探测网络容量，避免过度占用 │
  └──────────┴───────────────────────────────────────────────┴────────────────────────────┘

  速率控制逻辑：
  targetCreditRate = 1.0  → 100% regular, 0% tentative
  targetCreditRate = 0.5  → 50% regular, 50% tentative
  targetCreditRate = 0.1  → 10% regular, 90% tentative

  当网络拥塞时：
  1. Regular credit 被丢弃 → lossCountThisSlice++
  2. AdjustCreditRate() 降低 targetCreditRate
  3. 更多 credit 变成 tentative
  4. Tentative credit 只在队列空闲时接纳
  5. 减少对网络的压力

  周期性调整

  `if (now - flow.lastSliceChange > m_retransmissionTimeout)`

  - 周期: m_retransmissionTimeout（默认 200μs）
  - 为什么: 太快调整会震荡，太慢反应迟钝
  - 类似 TCP: TCP 每个 RTT 调整窗口

  上下界保护
  
  ```C
  flow.targetCreditRate = std::min(1.0,        // 上界: 1.0
      std::max(0.1, targetCreditRate));        // 下界: 0.1
  ```

  - 上界 1.0: 不能超过 100% regular credit
  - 下界 0.1: 至少保持 10% regular credit，避免完全饿死

  5. 与 TCP 的对比

  ┌──────────┬───────────────────┬─────────────────────────────────────┐
  │          │     TCP AIMD      │         Flare Rate Control          │
  ├──────────┼───────────────────┼─────────────────────────────────────┤
  │ 控制对象 │ cwnd（拥塞窗口）  │ targetCreditRate（credit 类型比例） │
  ├──────────┼───────────────────┼─────────────────────────────────────┤
  │ 增加策略 │ cwnd += 1 (AI)    │ rate × 1.1 (MI)                     │
  ├──────────┼───────────────────┼─────────────────────────────────────┤
  │ 减少策略 │ cwnd /= 2 (MD)    │ rate × 0.9 (MD)                     │
  ├──────────┼───────────────────┼─────────────────────────────────────┤
  │ 反馈信号 │ Packet loss / ACK │ Credit dropped                      │
  ├──────────┼───────────────────┼─────────────────────────────────────┤
  │ 调整周期 │ 每个 RTT          │ 每个 RTO (200μs)                    │
  ├──────────┼───────────────────┼─────────────────────────────────────┤
  │ 特殊机制 │ Fast retransmit   │ Tentative credit + 路径补偿         │
  └──────────┴───────────────────┴─────────────────────────────────────┘
