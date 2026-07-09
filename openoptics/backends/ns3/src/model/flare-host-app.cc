#include "flare-host-app.h"

#include "flare-header.h"

#include "ns3/ipv4-header.h"
#include "ns3/ipv4-raw-socket-factory.h"
#include "ns3/log.h"
#include "ns3/node.h"
#include "ns3/packet.h"
#include "ns3/simulator.h"
#include "ns3/uinteger.h"
#include "ns3/double.h"

#include <algorithm>
#include <cmath>

namespace ns3
{
namespace openoptics
{

NS_LOG_COMPONENT_DEFINE("OpenOpticsFlareHost");
NS_OBJECT_ENSURE_REGISTERED(FlareHostApp);

namespace {
double ClampCreditRate(double rate);
} // namespace

TypeId
FlareHostApp::GetTypeId()
{
    static TypeId tid = TypeId("ns3::openoptics::FlareHostApp")
                            .SetParent<Application>()
                            .SetGroupName("OpenOptics")
                            .AddConstructor<FlareHostApp>();
    return tid;
}

FlareHostApp::FlareHostApp()
    : m_nodeId(0),
      m_creditQsizePkts(60),
      m_shapingThreshPkts(30),
      m_aeolusThreshPkts(40),
      m_wInit(1.0),
      m_targetLoss(0.1),
      m_mtuBytes(1024),
      m_retransmissionTimeout(MicroSeconds(200)),
      m_hostLinkRateBps(0),
      m_nextCreditSendTime(Seconds(0))
{
}

FlareHostApp::~FlareHostApp() = default;

void FlareHostApp::SetNodeId(uint32_t node_id) { m_nodeId = node_id; }

void
FlareHostApp::SetHostDevice(Ptr<NetDevice> device)
{
    NS_ASSERT_MSG(GetNode() != nullptr,
                  "SetHostDevice called before app was added to a node");
    m_hostDev = device;
    GetNode()->RegisterProtocolHandler(
        MakeCallback(&FlareHostApp::ReceiveFromHostDevice, this),
        /*protocol=*/0,
        device,
        /*promiscuous=*/true);
}

void
FlareHostApp::SetDefaultConfig(uint32_t credit_qsize_pkts,
                               uint32_t shaping_thresh_pkts,
                               uint32_t aeolus_thresh_pkts,
                               double w_init,
                               double target_loss,
                               uint32_t mtu_bytes,
                               double retransmission_timeout_s)
{
    m_creditQsizePkts = credit_qsize_pkts;
    m_shapingThreshPkts = shaping_thresh_pkts;
    m_aeolusThreshPkts = aeolus_thresh_pkts;
    m_wInit = ClampCreditRate(w_init);
    m_targetLoss = std::min(1.0, std::max(0.0, target_loss));
    m_mtuBytes = mtu_bytes;
    m_retransmissionTimeout = Seconds(retransmission_timeout_s);
}

void
FlareHostApp::SetHostLinkRateBps(uint64_t bps)
{
    m_hostLinkRateBps = bps;
}

namespace {
uint32_t
CeilDiv(uint32_t a, uint32_t b)
{
    return (a + b - 1) / b;
}

double
ClampCreditRate(double rate)
{
    return std::min(1.0, std::max(0.1, rate));
}

double
AdmissionProbabilityForHops(uint8_t hops)
{
    if (hops <= 1)
    {
        return 1.0;
    }
    return std::pow(0.5, static_cast<double>(hops) - 1.0);
}
} // namespace

void
FlareHostApp::AddSenderFlow(uint32_t flow_id,
                            uint32_t dst_node,
                            const std::string& src_ip,
                            const std::string& dst_ip,
                            double start_s,
                            double stop_s,
                            uint32_t size_bytes,
                            uint32_t packet_size_bytes,
                            uint32_t port,
                            uint32_t path_id,
                            uint32_t initial_credit_pkts)
{
    FlowState f;
    f.flowId = flow_id;
    f.peerNode = dst_node;
    f.localIp = Ipv4Address(src_ip.c_str());
    f.peerIp = Ipv4Address(dst_ip.c_str());
    f.start = Seconds(start_s);
    f.stop = Seconds(stop_s);
    f.sizeBytes = size_bytes;
    f.packetSizeBytes = packet_size_bytes ? packet_size_bytes : m_mtuBytes;
    f.port = port;
    f.pathId = path_id;
    f.initialCreditPkts = std::max(1u, initial_credit_pkts);
    f.totalPackets = CeilDiv(size_bytes, f.packetSizeBytes);
    f.targetCreditRate = ClampCreditRate(m_wInit);
    f.lastSliceChange = f.start;
    m_senderFlows[flow_id] = f;
    Simulator::Schedule(f.start, &FlareHostApp::SendEligibleData, this, flow_id);
}

void
FlareHostApp::AddReceiverFlow(uint32_t flow_id,
                              uint32_t src_node,
                              const std::string& local_ip,
                              const std::string& peer_ip,
                              double start_s,
                              double stop_s,
                              uint32_t size_bytes,
                              uint32_t packet_size_bytes,
                              uint32_t port,
                              uint32_t path_id,
                              uint32_t initial_credit_pkts,
                              uint32_t credit_path_hops)
{
    FlowState f;
    f.flowId = flow_id;
    f.peerNode = src_node;
    f.localIp = Ipv4Address(local_ip.c_str());
    f.peerIp = Ipv4Address(peer_ip.c_str());
    f.start = Seconds(start_s);
    f.stop = Seconds(stop_s);
    f.sizeBytes = size_bytes;
    f.packetSizeBytes = packet_size_bytes ? packet_size_bytes : m_mtuBytes;
    f.port = port;
    f.pathId = path_id;
    f.initialCreditPkts = std::max(1u, initial_credit_pkts);
    f.creditPathHops = std::max(1u, credit_path_hops);
    f.totalPackets = CeilDiv(size_bytes, f.packetSizeBytes);
    f.targetCreditRate = ClampCreditRate(m_wInit);
    f.lastSliceChange = f.start;
    m_receiverFlows[flow_id] = f;
    Simulator::Schedule(f.start, &FlareHostApp::SendInitialCredits, this, flow_id);
}

void
FlareHostApp::StartApplication()
{
    if (!m_recvSocket)
    {
        m_recvSocket = Socket::CreateSocket(
            GetNode(), Ipv4RawSocketFactory::GetTypeId());
        m_recvSocket->SetAttribute("Protocol", UintegerValue(253));
        m_recvSocket->Bind();
        m_recvSocket->SetRecvCallback(
            MakeCallback(&FlareHostApp::ReceiveFromSocket, this));
    }
}

void
FlareHostApp::StopApplication()
{
    if (m_recvSocket)
    {
        m_recvSocket->Close();
        m_recvSocket = nullptr;
    }
    for (auto& kv : m_senderFlows)
    {
        if (kv.second.sendEvent.IsPending())
        {
            Simulator::Cancel(kv.second.sendEvent);
        }
    }
    for (auto& kv : m_receiverFlows)
    {
        if (kv.second.creditRefreshEvent.IsPending())
        {
            Simulator::Cancel(kv.second.creditRefreshEvent);
        }
    }
    if (m_creditPacingEvent.IsPending())
    {
        Simulator::Cancel(m_creditPacingEvent);
    }
}

void
FlareHostApp::ReceiveFromHostDevice(Ptr<NetDevice> /*device*/,
                                    Ptr<const Packet> packet,
                                    uint16_t protocol,
                                    const Address& /*src*/,
                                    const Address& /*dst*/,
                                    NetDevice::PacketType /*packetType*/)
{
    if (protocol != 0x0800)
    {
        return;
    }
    Ptr<Packet> pkt = packet->Copy();
    HandleFlarePacket(pkt);
}

void
FlareHostApp::ReceiveFromSocket(Ptr<Socket> socket)
{
    Ptr<Packet> pkt;
    Address from;
    while ((pkt = socket->RecvFrom(from)))
    {
        HandleFlarePacket(pkt);
    }
}

void
FlareHostApp::HandleFlarePacket(Ptr<Packet> pkt)
{
    Ipv4Header ip;
    if (pkt->GetSize() >= ip.GetSerializedSize())
    {
        Ptr<Packet> with_ip = pkt->Copy();
        Ipv4Header maybe_ip;
        with_ip->RemoveHeader(maybe_ip);
        if (maybe_ip.GetProtocol() == 253)
        {
            pkt = with_ip;
        }
    }

    FlareHeader flare;
    if (pkt->GetSize() < flare.GetSerializedSize())
    {
        return;
    }
    pkt->RemoveHeader(flare);
    if (!flare.IsValid())
    {
        return;
    }

    switch (flare.GetType())
    {
    case FlareHeader::CREDIT:
        HandleCredit(flare.GetFlowId(), flare.GetSeq(), flare.GetRemainingHops(), flare.GetTimeSlice());
        break;
    case FlareHeader::DATA:
        HandleData(flare.GetFlowId(), flare.GetSeq(), flare.GetRemainingHops());
        break;
    case FlareHeader::CONTROL:
        HandleControl(flare.GetFlowId(), flare.GetControlCode());
        break;
    case FlareHeader::NACK:
        HandleCredit(flare.GetFlowId(), flare.GetSeq(), flare.GetRemainingHops(), flare.GetTimeSlice());
        break;
    default:
        break;
    }
}

void
FlareHostApp::SendInitialCredits(uint32_t flow_id)
{
    FlowState* flow = FindReceiverFlow(flow_id);
    if (!flow || flow->done)
    {
        return;
    }
    const uint32_t limit = std::min(flow->initialCreditPkts, flow->totalPackets);
    while (flow->nextCreditSeq < limit)
    {
        EnqueueCredit(*flow, flow->nextCreditSeq++);
    }
    if (!flow->creditRefreshEvent.IsPending())
    {
        flow->creditRefreshEvent =
            Simulator::Schedule(m_retransmissionTimeout,
                                &FlareHostApp::RefreshCredits, this, flow_id);
    }
}

void
FlareHostApp::RefreshCredits(uint32_t flow_id)
{
    FlowState* flow = FindReceiverFlow(flow_id);
    if (!flow || flow->done || Simulator::Now() > flow->stop)
    {
        return;
    }

    // Credit packets can be wasted by slice/path mismatch before they
    // reach the sender. Periodically refresh credits for the current
    // receiver window so the flow converges when a later slice admits
    // the reverse-path credit.
    const uint32_t delivered = static_cast<uint32_t>(flow->receivedSeqs.size());
    const uint32_t window_end =
        std::min(flow->totalPackets, delivered + flow->initialCreditPkts);

    bool credit_loss = false;
    const Time now = Simulator::Now();
    for (uint32_t seq = 0; seq < window_end; ++seq)
    {
        if (flow->receivedSeqs.find(seq) != flow->receivedSeqs.end())
        {
            continue;
        }
        auto sent_it = flow->creditSentAt.find(seq);
        if (sent_it != flow->creditSentAt.end() &&
            now - sent_it->second >= m_retransmissionTimeout)
        {
            credit_loss = true;
            break;
        }
    }
    AdjustCreditRate(*flow, credit_loss);

    for (uint32_t seq = 0; seq < window_end; ++seq)
    {
        if (flow->receivedSeqs.find(seq) == flow->receivedSeqs.end())
        {
            EnqueueCredit(*flow, seq);
        }
    }

    flow->creditRefreshEvent =
        Simulator::Schedule(m_retransmissionTimeout,
                            &FlareHostApp::RefreshCredits, this, flow_id);
}

void
FlareHostApp::EnqueueCredit(FlowState& flow, uint32_t seq)
{
    if (flow.done || Simulator::Now() > flow.stop ||
        seq >= flow.totalPackets ||
        flow.receivedSeqs.find(seq) != flow.receivedSeqs.end())
    {
        return;
    }

    const uint64_t key = CreditKey(flow.flowId, seq);
    if (!m_pendingCreditKeys.insert(key).second)
    {
        return;
    }
    m_pendingCredits.emplace_back(flow.flowId, seq);
    ScheduleCreditPacer();
}

void
FlareHostApp::ScheduleCreditPacer()
{
    if (m_pendingCredits.empty() || m_creditPacingEvent.IsPending())
    {
        return;
    }
    const Time now = Simulator::Now();
    const Time delay =
        (m_nextCreditSendTime > now) ? (m_nextCreditSendTime - now) : Time(0);
    m_creditPacingEvent =
        Simulator::Schedule(delay, &FlareHostApp::RunCreditPacer, this);
}

void
FlareHostApp::RunCreditPacer()
{
    m_creditPacingEvent = EventId();
    if (m_pendingCredits.empty())
    {
        return;
    }

    const auto item = m_pendingCredits.front();
    m_pendingCredits.pop_front();
    m_pendingCreditKeys.erase(CreditKey(item.first, item.second));

    FlowState* flow = FindReceiverFlow(item.first);
    if (flow && !flow->done && Simulator::Now() <= flow->stop &&
        item.second < flow->totalPackets &&
        flow->receivedSeqs.find(item.second) == flow->receivedSeqs.end())
    {
        SendCredit(*flow, item.second);
        m_nextCreditSendTime = Simulator::Now() + CreditPaceInterval(*flow);
    }
    else
    {
        m_nextCreditSendTime = Simulator::Now();
    }

    ScheduleCreditPacer();
}

void
FlareHostApp::SendEligibleData(uint32_t flow_id)
{
    FlowState* flow = FindSenderFlow(flow_id);
    if (!flow || flow->done || Simulator::Now() > flow->stop)
    {
        return;
    }
    bool sent_any = false;
    while (flow->nextSendSeq < flow->totalPackets &&
           flow->creditSeqs.erase(flow->nextSendSeq) > 0)
    {
        SendData(*flow, flow->nextSendSeq, false);
        ++flow->nextSendSeq;
        sent_any = true;
    }
    if (!sent_any && !flow->done)
    {
        flow->sendEvent =
            Simulator::Schedule(MicroSeconds(10),
                                &FlareHostApp::SendEligibleData, this, flow_id);
    }
}

void
FlareHostApp::OnRetransmissionTimeout(uint32_t flow_id, uint32_t seq)
{
    FlowState* flow = FindSenderFlow(flow_id);
    if (!flow || flow->done || Simulator::Now() > flow->stop)
    {
        return;
    }
    if (flow->sentSeqs.find(seq) == flow->sentSeqs.end())
    {
        return;
    }
    ++flow->timeouts;
    if (flow->creditSeqs.erase(seq) > 0)
    {
        SendData(*flow, seq, true);
    }
    else
    {
        // A retransmission must consume fresh credit; ask the event loop
        // to try again after more receiver credits arrive.
        flow->sendEvent =
            Simulator::Schedule(MicroSeconds(10),
                                &FlareHostApp::SendEligibleData, this, flow_id);
    }
}

void
FlareHostApp::SendCredit(FlowState& flow, uint32_t seq)
{
    // 判断是 regular 还是 tentative
    if (!m_flareAdmissionRng)
    {
        m_flareAdmissionRng = CreateObject<UniformRandomVariable>();
        m_flareAdmissionRng->SetAttribute("Min", DoubleValue(0.0));
        m_flareAdmissionRng->SetAttribute("Max", DoubleValue(1.0));
    }
    double rand_val = m_flareAdmissionRng->GetValue();
    bool is_tentative = (rand_val > flow.targetCreditRate);

    uint8_t pkt_type = is_tentative
        ? FlareHeader::TENTATIVE_CREDIT
        : FlareHeader::CREDIT;

    ++flow.creditsSent;
    SendFlarePacket(flow, seq, pkt_type, FlareHeader::NONE, 0);
    flow.creditSentAt[seq] = Simulator::Now();
}

void
FlareHostApp::SendData(FlowState& flow, uint32_t seq, bool retransmission)
{
    if (retransmission)
    {
        ++flow.retransmissions;
    }

    // Fast Start：第一个 BDP 的数据标记为 UNSCHEDULED
    uint64_t bdp_estimate = static_cast<uint64_t>(flow.initialCreditPkts) *
                            static_cast<uint64_t>(flow.packetSizeBytes);
    uint64_t sent_bytes = static_cast<uint64_t>(seq) * static_cast<uint64_t>(flow.packetSizeBytes);
    bool is_unscheduled = (sent_bytes < bdp_estimate);

    uint8_t control_code = is_unscheduled
        ? FlareHeader::UNSCHEDULED
        : FlareHeader::NONE;

    ++flow.dataSent;
    flow.sentSeqs.insert(seq);
    SendFlarePacket(flow, seq, FlareHeader::DATA, control_code,
                    PacketPayloadBytes(flow, seq));
    Simulator::Schedule(m_retransmissionTimeout,
                        &FlareHostApp::OnRetransmissionTimeout, this,
                        flow.flowId, seq);
}

void
FlareHostApp::SendControlDone(FlowState& flow)
{
    SendFlarePacket(flow, 0, FlareHeader::CONTROL, FlareHeader::FLOW_DONE, 0);
}

void
FlareHostApp::SendFlarePacket(FlowState& flow,
                              uint32_t seq,
                              uint8_t packet_type,
                              uint8_t control_code,
                              uint32_t payload_bytes)
{
    if (!m_hostDev)
    {
        return;
    }
    Ptr<Packet> pkt = Create<Packet>(payload_bytes);
    FlareHeader flare;
    flare.SetType(static_cast<FlareHeader::PacketType>(packet_type));
    flare.SetControlCode(static_cast<FlareHeader::ControlCode>(control_code));
    flare.SetFlowId(flow.flowId);
    flare.SetSeq(seq);
    flare.SetCreditEpoch(seq);
    flare.SetSrcNode(m_nodeId);
    flare.SetDstNode(flow.peerNode);
    flare.SetPathId(static_cast<uint16_t>(flow.pathId));

    uint8_t actual_hops = static_cast<uint8_t>(std::min(flow.creditPathHops, 255u));
    if (packet_type == FlareHeader::CREDIT ||
        packet_type == FlareHeader::TENTATIVE_CREDIT)
    {
        // Credit admission depends on the reverse-path distance configured
        // when the receiver flow was installed.  Do not infer it from the
        // data-path histogram: data packets reach the receiver with their
        // remaining_hops already decremented, which would collapse long
        // credit paths into apparent one-hop credits.
        uint64_t bdp_estimate = static_cast<uint64_t>(flow.initialCreditPkts) *
                                static_cast<uint64_t>(flow.packetSizeBytes);
        uint64_t delivered_bytes = flow.receivedSeqs.size() * flow.packetSizeBytes;
        double jitter_ratio = bdp_estimate > 0
            ? static_cast<double>(delivered_bytes) / static_cast<double>(bdp_estimate)
            : 0.0;

        if (actual_hops == 0) actual_hops = 1;  // 保底

        // 50% 概率应用 jittering
        if (!m_flareAdmissionRng)
        {
            m_flareAdmissionRng = CreateObject<UniformRandomVariable>();
            m_flareAdmissionRng->SetAttribute("Min", DoubleValue(0.0));
            m_flareAdmissionRng->SetAttribute("Max", DoubleValue(1.0));
        }
        double rand_val = m_flareAdmissionRng->GetValue();
        if (rand_val < 0.5)
        {
            if (jitter_ratio < 4.0 && actual_hops > 1)
            {
                actual_hops -= 1;  // 短流减 1
            }
            else if (jitter_ratio > 16.0 && actual_hops < 8)
            {
                actual_hops += 1;  // 长流加 1
            }
        }
    }
    flare.SetRemainingHops(actual_hops);
    flare.SetTotalHops(actual_hops);

    // 设置时间片：DATA 包继承对应 credit 的时间片
    if (packet_type == FlareHeader::DATA)
    {
        auto it = flow.creditTimeSliceMap.find(seq);
        if (it != flow.creditTimeSliceMap.end())
        {
            flare.SetTimeSlice(it->second);
        }
        else
        {
            flare.SetTimeSlice(0);  // 默认值
        }
    }
    else
    {
        // CREDIT 包的时间片将由 ToR 在入口处设置
        flare.SetTimeSlice(0);
    }

    pkt->AddHeader(flare);

    Ipv4Header ip;
    ip.SetSource(flow.localIp);
    ip.SetDestination(flow.peerIp);
    ip.SetProtocol(253);
    ip.SetPayloadSize(pkt->GetSize());
    ip.SetTtl(64);
    pkt->AddHeader(ip);
    m_hostDev->Send(pkt, m_hostDev->GetBroadcast(), 0x0800);
}

void
FlareHostApp::HandleCredit(uint32_t flow_id, uint32_t seq, uint32_t remaining_hops, uint8_t time_slice)
{
    FlowState* flow = FindSenderFlow(flow_id);
    if (!flow || flow->done)
    {
        return;
    }
    if (!flow->creditSeqs.insert(seq).second)
    {
        ++flow->duplicateCredits;
    }
    ++flow->creditsReceived;
    ++flow->pathLengthHistogram[remaining_hops];

    // 记录这个序列号对应的时间片
    flow->creditTimeSliceMap[seq] = time_slice;

    SendEligibleData(flow_id);
}

void
FlareHostApp::HandleData(uint32_t flow_id, uint32_t seq, uint32_t remaining_hops)
{
    FlowState* flow = FindReceiverFlow(flow_id);
    if (!flow || flow->done)
    {
        return;
    }
    if (flow->receivedSeqs.empty())
    {
        flow->firstDataRx = Simulator::Now();
    }
    if (!flow->receivedSeqs.insert(seq).second)
    {
        ++flow->duplicateData;
        return;
    }
    ++flow->dataReceived;
    ++flow->pathLengthHistogram[remaining_hops];
    OnPathChange(*flow, static_cast<uint8_t>(remaining_hops));
    flow->creditSentAt.erase(seq);
    AdjustCreditRate(*flow, false);
    const uint32_t next_credit = seq + flow->initialCreditPkts;
    if (next_credit < flow->totalPackets)
    {
        EnqueueCredit(*flow, next_credit);
    }
    if (flow->receivedSeqs.size() >= flow->totalPackets)
    {
        flow->done = true;
        flow->completionTime = Simulator::Now() - flow->start;
        if (flow->creditRefreshEvent.IsPending())
        {
            Simulator::Cancel(flow->creditRefreshEvent);
        }
        SendControlDone(*flow);
    }
}

void
FlareHostApp::HandleControl(uint32_t flow_id, uint8_t control_code)
{
    FlowState* flow = FindSenderFlow(flow_id);
    if (!flow)
    {
        return;
    }
    if (control_code == FlareHeader::FLOW_DONE)
    {
        flow->done = true;
        flow->completionTime = Simulator::Now() - flow->start;
    }
}

const FlareHostApp::FlowState*
FlareHostApp::FindFlow(uint32_t flow_id) const
{
    auto sit = m_senderFlows.find(flow_id);
    if (sit != m_senderFlows.end())
    {
        return &sit->second;
    }
    auto rit = m_receiverFlows.find(flow_id);
    if (rit != m_receiverFlows.end())
    {
        return &rit->second;
    }
    return nullptr;
}

FlareHostApp::FlowState*
FlareHostApp::FindSenderFlow(uint32_t flow_id)
{
    auto it = m_senderFlows.find(flow_id);
    return it == m_senderFlows.end() ? nullptr : &it->second;
}

FlareHostApp::FlowState*
FlareHostApp::FindReceiverFlow(uint32_t flow_id)
{
    auto it = m_receiverFlows.find(flow_id);
    return it == m_receiverFlows.end() ? nullptr : &it->second;
}

uint32_t
FlareHostApp::PacketPayloadBytes(const FlowState& flow, uint32_t seq) const
{
    const uint32_t sent = seq * flow.packetSizeBytes;
    if (sent >= flow.sizeBytes)
    {
        return 0;
    }
    return std::min(flow.packetSizeBytes, flow.sizeBytes - sent);
}

Time
FlareHostApp::CreditPaceInterval(const FlowState& flow) const
{
    if (m_hostLinkRateBps == 0)
    {
        return Time(0);
    }
    const uint64_t data_bytes =
        static_cast<uint64_t>(std::max(1u, flow.packetSizeBytes));
    const uint64_t ns =
        (data_bytes * 8ULL * 1000000000ULL + m_hostLinkRateBps - 1ULL) /
        m_hostLinkRateBps;
    return NanoSeconds(ns);
}

uint64_t
FlareHostApp::CreditKey(uint32_t flow_id, uint32_t seq)
{
    return (static_cast<uint64_t>(flow_id) << 32) | seq;
}

void
FlareHostApp::AdjustCreditRate(FlowState& flow, bool credit_dropped)
{
    if (credit_dropped)
    {
        ++flow.lossCountThisSlice;
    }

    // 检测时间片边界（简化：每个 retransmission timeout 周期）
    Time now = Simulator::Now();
    if (now - flow.lastSliceChange >= m_retransmissionTimeout)
    {
        // 根据 loss 调整 rate
        if (flow.lossCountThisSlice > 0)
        {
            flow.targetCreditRate = ClampCreditRate(
                flow.targetCreditRate * (1.0 - m_targetLoss));  // 减速
        }
        else
        {
            flow.targetCreditRate = ClampCreditRate(
                flow.targetCreditRate * (1.0 + m_targetLoss));  // 加速
        }

        // 重置计数
        flow.lossCountThisSlice = 0;
        flow.lastSliceChange = now;
    }
}

void
FlareHostApp::OnPathChange(FlowState& flow, uint8_t new_path_length)
{
    if (new_path_length == 0)
    {
        return;
    }
    if (flow.lastPathLength == 0)
    {
        flow.lastPathLength = new_path_length;
        return;
    }

    // 路径变化时的 rate 补偿
    double old_prob = AdmissionProbabilityForHops(flow.lastPathLength);
    double new_prob = AdmissionProbabilityForHops(new_path_length);
    double delta = new_prob - old_prob;

    flow.targetCreditRate = ClampCreditRate(flow.targetCreditRate + delta);

    flow.lastPathLength = new_path_length;
}

uint64_t FlareHostApp::GetDataPacketsSent(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->dataSent : 0;
}
uint64_t FlareHostApp::GetDataPacketsReceived(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->dataReceived : 0;
}
uint64_t FlareHostApp::GetCreditsSent(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->creditsSent : 0;
}
uint64_t FlareHostApp::GetCreditsReceived(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->creditsReceived : 0;
}
uint64_t FlareHostApp::GetRetransmissions(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->retransmissions : 0;
}
uint64_t FlareHostApp::GetTimeouts(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->timeouts : 0;
}
uint64_t FlareHostApp::GetDuplicateData(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->duplicateData : 0;
}
uint64_t FlareHostApp::GetDuplicateCredits(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    return f ? f->duplicateCredits : 0;
}
uint64_t FlareHostApp::GetFlowCompletionTimeUs(uint32_t flow_id) const
{
    const FlowState* f = FindFlow(flow_id);
    if (!f || !f->done)
    {
        return 0;
    }
    return static_cast<uint64_t>(f->completionTime.GetMicroSeconds());
}
uint64_t FlareHostApp::GetPathLengthCount(uint32_t flow_id, uint32_t remaining_hops) const
{
    const FlowState* f = FindFlow(flow_id);
    if (!f)
    {
        return 0;
    }
    auto it = f->pathLengthHistogram.find(remaining_hops);
    return it == f->pathLengthHistogram.end() ? 0 : it->second;
}

} // namespace openoptics
} // namespace ns3
