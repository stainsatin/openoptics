Flare 在 OpenOptics ns-3 后端的完整复刻计划Summary目标是在 openoptics 现有 Opera/ns-3 仿真框架上，新增一套论文《Unlocking Superior Performance in Reconfigurable Data Center Networks with Credit-Based Transport》中的 Flare 传输栈，并复现论文的主要结论，而不是只做一个“像 credit-based transport 的原型”。
范围锁定为 ns-3 后端完整复刻：包含 Flare 协议机制、Opera 上的运行路径、关键基线、主要实验、参数配置、指标采集与结果校验。
实现应复用仓库现有的 OpticalTopo.opera、OpticalRouting.*、Ns3Backend、TorApp、OcsApp、FlowMonitor、dashboard，而不是重写一套新的网络栈。
Key Changes1. 先做一份“论文到代码”的实现规格冻结将论文中所有 Flare 机制整理成一份实现规格，明确哪些是 必须复刻、哪些是 仿真近似、哪些是 延期项。
必须纳入 v1 的机制：receiver-driven / credit-based 发送控制。
多瓶颈链路上的逐链路 credit shaping。
以 remaining hop count 为核心的 opportunistic credit admission / priority。
处理 credit waste 的机制。
处理 credit/data path mismatch 的机制。
处理 slow convergence / underutilization 的机制。
可靠性机制：数据重传、超时、重复包/重复 credit 处理、流结束判定。

参数直接锁定为论文默认 profile，至少提供两套：55us profile：credit_qsize=60 pkt、shaping_thresh=30 pkt、aeolus_thresh=40 pkt、w_init=1.0、target_loss=0.1。
15us profile：credit_qsize=16 pkt、shaping_thresh=8 pkt、aeolus_thresh=8 pkt、w_init=1.0、target_loss=0.1。

交付物是员工实现时必须遵守的协议规格文档，避免边写边猜论文语义。
2. 在 ns-3 C++ 模型层新增 Flare 主机协议实体在 openoptics/backends/ns3/src/model/ 中新增一套 Flare host-side ns-3 app，而不是把 Flare 塞进现有 UDP/TCP helper。
新增的核心类型应固定为：FlareHeader：区分 data / credit / nack / control 子类型，并携带 flow id、seq、credit epoch、path metadata、remaining hops 等字段。
FlareHostApp：同时承担 sender 和 receiver 逻辑。
FlareFlowState：维护发送窗口、已发未确认数据、credit budget、retransmission state、flow completion state。
FlareStats：除 FlowMonitor 外，单独记录 credits sent/admitted/dropped/wasted、retransmits、timeout count、path length histogram。

sender 逻辑固定为：仅在本 flow 持有有效 credit 时发数据。
credit 消耗与 MTU-sized data packet 一一对应，避免“按字节/按包”实现混乱。
所有重传也必须重新消耗有效 credit，不能绕过 shaping。

receiver 逻辑固定为：按论文机制生成 credit。
跟踪接收序号与缺口，触发 loss recovery。
对完成流停止继续发 credit，并安全回收状态。

3. 扩展 ToR 数据面模型以支持 Flare 的 credit shaping保留 OcsApp 基本不动；主要改造集中在 TorApp，因为现有 TorApp 已负责时隙感知、路径匹配、calendar queue 和 admission。
TorApp 需要新增三类 Flare 相关能力：区分 data 与 credit 的独立处理路径和计数器。
对 credit 包执行逐链路 shaping / admission，而不是只对数据包做当前的 byte-budget admission。
在 credit admission 时引入 remaining-hop-aware priority，让短剩余路径 credit 更容易通过。

为避免把当前 OpenOpticsHeader 语义搅乱，固定做法是：保留现有 OpenOpticsHeader 负责时隙/目的 ToR 路由。
Flare transport 元数据放入新的 FlareHeader。
ToR 先按 OpenOptics 路由，再根据 Flare 类型走 credit/data 专属逻辑。

TorApp 的 Flare 新状态必须包括：每个 uplink/slot 上 credit queue 的 occupancy。
credit admission/drop/waste counters。
tentative credit accounting。
与现有 m_linkFreeAt、m_cqBytesPerSlot 并行的 credit-side per-slot/per-uplink state。

不允许把 Flare 简化为“只在 host 侧 pacing、交换普通 UDP control packet”；这样无法复刻论文的多瓶颈 shaping 和 credit waste 现象。
4. 在 Python backend/API 层暴露一套明确的 Flare 接口在 openoptics/backends/ns3/backend.py 和 openoptics/backends/ns3/traffic.py 上新增独立接口，不复用 udp_traffic() / tcp_traffic() 名字。
对外接口固定为：FlareConfig dataclass：承载论文参数与 profile。
BaseNetwork.flare_traffic(config=..., **defaults)：返回 Flare workload builder。
flare_traffic().flow(src, dst, size_bytes, start_s, ...)：安装 Flare 流。
InstalledFlareTraffic.stats()：返回 FCT、throughput、retrans、credit waste、credit loss 等统计。

Ns3Backend.accepted_kwargs() 中新增但仅限 Flare 模式有效的参数：flare_profile
flare_credit_qsize_pkts
flare_shaping_thresh_pkts
flare_aeolus_thresh_pkts
flare_w_init
flare_target_loss

现有 udp_traffic() / tcp_traffic() 保持不变，用于基线复现。
5. 基线与实验框架按论文主结果完整补齐主实验范围固定包含：Flare
NDP
ExpressPass
TDTCP
Bolt
similar-cost Clos 对照

如果仓库当前没有这些协议的现成 ns-3 实现，要求统一在同一套 OpenOptics/ns-3 harness 下补齐“论文所需最小可比模型”，重点是行为一致，不追求另起大型独立模块。
评测脚本必须分成两层：microbench：单/双瓶颈、credit waste、path mismatch、slice 切换、不同 hop count。
paper-scale：Opera 大拓扑、55us/15us slice、论文负载与 trace 场景。

结果输出统一写成 CSV/JSON，至少包含：throughput
median/p95/p99 FCT
queue occupancy
credit admitted/dropped/wasted
retransmission count
path-length usage
host uplink utilization

Appendix 中的 HbH 基线不放入主里程碑，作为第二阶段可选扩展；主里程碑先复现正文核心对比。
6. 测试、验证与结果回归必须先于“论文图表美化”单元测试新增覆盖：FlareHeader 序列化/反序列化。
sender/receiver 状态机。
credit 消耗、重复 credit、超时重传。
remaining-hop-aware admission 是否真的偏向短路径。
tentative credit / credit waste 相关计数器。
credit/data path mismatch 时不会错误放行或错误释放 credit。

集成测试新增覆盖：4-node/8-node Opera 拓扑下的单流、多流、incast、adversarial traffic。
15us 与 55us 两套参数 profile。
source/per-hop routing 与 Flare 的组合约束。

回归门槛固定为：Flare 功能测试全绿。
主要基线在同一 harness 下可重复运行。
同一随机种子下结果稳定。
至少能复现论文中“Flare 明显优于 NDP / ExpressPass / TDTCP / Bolt，并在某些场景优于 similar-cost Clos”的趋势；若绝对数值有偏差，需要输出偏差归因文档。

Public APIs / Interfaces新增 FlareConfig Python 配置类型。
新增 BaseNetwork.flare_traffic() 用户入口。
新增 InstalledFlareTraffic.stats() 返回的 Flare 专属统计结构。
在 ns-3 C++ 侧新增 FlareHeader、FlareHostApp、FlareFlowState 与对应 TypeId 注册。
TorApp 新增 Flare credit/data counters 与查询接口，供测试和 dashboard 使用。
Test Plan协议正确性：单流从 credit 发放到数据完成，全程无死锁、无 credit 泄漏、无提前结束。
丢 credit、丢 data、超时重传、重复包、流完成后的迟到包都能正确处理。

机制正确性：多瓶颈场景下 credit shaping 生效，未被 admission 的 credit 不得隐式授权发送。
不同 remaining hop 的竞争场景里，短剩余路径获得更高 admission 概率。
topology/path 切换时 credit/data path mismatch 不会导致状态错配。

论文趋势复现：55us 和 15us 两套参数下，Flare 相对 NDP / ExpressPass / TDTCP / Bolt 的优势方向与论文一致。
至少一个 similar-cost Clos 对照场景中验证 Flare+Opera 的吞吐趋势。

工程可维护性：所有 Flare 测试进入 tests/test_ns3_* 套件。
所有图表脚本都可由原始 JSON/CSV 一键重跑。

Assumptions本轮只做 ns-3 仿真复刻，不做论文里的 DPDK/Tofino testbed 实装。
维持仓库当前 ns3 后端的 一 ToR 一 host 约束，不扩展多 host per ToR。
拓扑与路由直接复用现有 Opera 生成器和 OpenOptics 路由框架，不重做控制面。
主里程碑必须覆盖正文核心实验；附录 HbH 与更深度的图表复刻放在第二阶段。
如果论文使用的真实 trace 无法直接取得，则采用同类公开 trace 或按论文负载模型重建，并在结果说明中显式标注。