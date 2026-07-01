# Flare Credit Rate 与 Remaining Hops 修改说明

本文档记录本次针对 Flare 实现做的两处修改：

1. 接收端 credit rate 的动态调整。
2. `remaining_hops` 随交换机转发跳数递减。

## 修改文件

- `openoptics/backends/ns3/src/model/flare-host-app.h`
- `openoptics/backends/ns3/src/model/flare-host-app.cc`
- `openoptics/backends/ns3/src/model/openoptics-tor-app.h`
- `openoptics/backends/ns3/src/model/openoptics-tor-app.cc`
- `openoptics/backends/ns3/backend.py`

## 一、接收端 Credit Rate 调整

原实现里已经有 `targetCreditRate`、`AdjustCreditRate()` 和
`OnPathChange()`，但是 rate control 的反馈路径没有真正接上。
也就是说，receiver 发 credit 时会根据 `targetCreditRate` 判断发
regular credit 还是 tentative credit，但 `targetCreditRate` 本身
缺少可靠的更新来源。

本次修改后，receiver 端每个 flow 增加了 credit 发送时间记录：

```cpp
std::unordered_map<uint32_t, Time> creditSentAt;
```

该 map 记录每个 credit sequence 最近一次发送的时间。

receiver 周期性刷新 credit 时，会检查窗口内尚未收到 data 的 seq。
如果某个 seq 对应的 credit 已经发出超过一个 retransmission timeout，
但仍然没有收到对应 data，则认为该 credit 没有产生有效反馈，
作为一次 credit loss 信号传给 `AdjustCreditRate()`。

核心逻辑是：

- credit 发出时记录 `creditSentAt[seq] = Simulator::Now()`。
- 周期刷新时，如果某个未完成 seq 超时，则认为本周期存在 loss。
- 收到 data 后删除对应 seq 的 `creditSentAt` 记录。
- 收到 data 也会触发一次无 loss 的 rate 调整检查。

这样 receiver 端的 credit rate 就会真正根据反馈变化，而不是一直停留
在初始值。

## 二、`w_init` 生效

原来 `w_init` 虽然从配置传入了 `FlareHostApp`，但 flow 初始化时
`targetCreditRate` 仍然使用默认值 `1.0`。

本次修改后，sender flow 和 receiver flow 初始化时都会执行：

```cpp
f.targetCreditRate = ClampCreditRate(m_wInit);
```

同时 `SetDefaultConfig()` 中会对 `w_init` 和 `target_loss` 做边界保护：

```cpp
m_wInit = ClampCreditRate(w_init);
m_targetLoss = std::min(1.0, std::max(0.0, target_loss));
```

其中 `targetCreditRate` 被限制在：

```text
0.1 <= targetCreditRate <= 1.0
```

## 三、Rate 调整规则

`AdjustCreditRate()` 的调整方式保持原有设计，但现在会被真实反馈触发。

如果本周期存在 credit loss：

```cpp
targetCreditRate *= (1.0 - target_loss)
```

如果本周期没有 credit loss：

```cpp
targetCreditRate *= (1.0 + target_loss)
```

调整后统一通过 `ClampCreditRate()` 限制到 `[0.1, 1.0]`。

这意味着：

- 网络拥塞、credit 超时未换回 data 时，receiver 会降低 regular credit 比例。
- 网络顺畅、data 能正常返回时，receiver 会逐步提高 regular credit 比例。
- rate 降低后，更多 credit 会变成 tentative credit。

## 四、路径变化补偿修正

原来的 `OnPathChange()` 直接用：

```cpp
pow(0.5, lastPathLength - 1)
```

当 `lastPathLength` 还没有初始化，或者收到的 hop count 为 0 时，
可能出现不合理的概率计算。

本次修改增加了 `AdmissionProbabilityForHops()`：

```cpp
if (hops <= 1) {
    return 1.0;
}
return std::pow(0.5, hops - 1);
```

同时 `OnPathChange()` 会忽略 `new_path_length == 0` 的情况。

这样可以避免 `remaining_hops` 递减到 0 后，错误影响 path change
补偿逻辑。

## 五、Remaining Hops 随交换机跳数递减

原实现中，`remaining_hops` 主要由 host 在生成 Flare header 时写入。
包经过 ToR 转发后，这个值不会随着实际交换机跳数减少。

本次修改在 `TorApp` 中增加：

```cpp
void TorApp::DecrementFlareRemainingHops(Ptr<Packet> pkt_with_headers);
```

该函数会解析并恢复以下头部结构：

```text
OpenOpticsHeader
可选 OpenOpticsSourceRouteHeader
Ipv4Header
FlareHeader
```

如果包中存在合法 Flare header，并且：

```cpp
flare.GetRemainingHops() > 0
```

则在 ToR 实际从 uplink 发出包之前执行：

```cpp
flare.SetRemainingHops(flare.GetRemainingHops() - 1);
```

该逻辑接在 `DrainSlice()` 中，也就是包真正从 ToR 发往 uplink 的路径上。
因此，`remaining_hops` 现在反映的是包经过交换机后的剩余跳数，而不是
host 初始写入后保持不变。

## 六、Tentative Credit 也进入 Credit Admission

原来 ToR 判断 Flare credit 时只检查：

```cpp
flare.GetType() == FlareHeader::CREDIT
```

这样 tentative credit 没有进入同一套 credit admission 和 credit queue
accounting。

本次修改增加了统一判断函数：

```cpp
inline bool IsFlareCreditType(FlareHeader::PacketType type)
{
    return type == FlareHeader::CREDIT ||
           type == FlareHeader::TENTATIVE_CREDIT;
}
```

现在 regular credit 和 tentative credit 都会进入 ToR 的 credit admission
逻辑。

区别仍然保留在 `AdmitFlareCredit()` 内部：

- tentative credit 只在队列占用非常低时接纳。
- regular credit 在低于 congestion threshold 时直接接纳，拥塞时按
  `remaining_hops` 做概率接纳。

## 七、Stats 增加 Remaining Hops = 0 桶

由于 `remaining_hops` 现在会随 ToR 转发递减，目的端收到包时可能已经是 0。

因此 `Ns3Backend.flare_stats_for()` 的 `path_length_histogram` 增加了 0 桶：

```python
path_length_histogram={
    0: int(dst_app.GetPathLengthCount(flow_id, 0)),
    1: int(dst_app.GetPathLengthCount(flow_id, 1)),
    2: int(dst_app.GetPathLengthCount(flow_id, 2)),
    3: int(dst_app.GetPathLengthCount(flow_id, 3)),
}
```

这样后续分析时不会漏掉递减到 0 的包。

## 八、验证结果

已执行以下检查：

```bash
python -m unittest tests.test_ns3_traffic_generator.FlareTrafficGeneratorTests
python -m compileall -q openoptics/backends/ns3
git diff --check
```

结果均通过。

另外也尝试运行了 ns-3 Flare 集成 smoke test：

```bash
python -m unittest tests.test_ns3_flare.Ns3FlareIntegrationTests.test_single_flare_flow_completes_and_reports_counters
```

该测试在当前环境中被 skip，原因是当前 test runner 没有可用的 ns-3 环境。

