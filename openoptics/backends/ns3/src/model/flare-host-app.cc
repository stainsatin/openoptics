#include "flare-host-app.h"

#include "flare-header.h"

#include "ns3/ipv4-header.h"
#include "ns3/ipv4-raw-socket-factory.h"
#include "ns3/log.h"
#include "ns3/node.h"
#include "ns3/packet.h"
#include "ns3/simulator.h"
#include "ns3/uinteger.h"

#include <algorithm>

namespace ns3
{
namespace openoptics
{

NS_LOG_COMPONENT_DEFINE("OpenOpticsFlareHost");
NS_OBJECT_ENSURE_REGISTERED(FlareHostApp);

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
      m_retransmissionTimeout(MicroSeconds(200))
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
    m_wInit = w_init;
    m_targetLoss = target_loss;
    m_mtuBytes = mtu_bytes;
    m_retransmissionTimeout = Seconds(retransmission_timeout_s);
}

namespace {
uint32_t
CeilDiv(uint32_t a, uint32_t b)
{
    return (a + b - 1) / b;
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
                              uint32_t initial_credit_pkts)
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
    f.totalPackets = CeilDiv(size_bytes, f.packetSizeBytes);
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
        HandleCredit(flare.GetFlowId(), flare.GetSeq(), flare.GetRemainingHops());
        break;
    case FlareHeader::DATA:
        HandleData(flare.GetFlowId(), flare.GetSeq(), flare.GetRemainingHops());
        break;
    case FlareHeader::CONTROL:
        HandleControl(flare.GetFlowId(), flare.GetControlCode());
        break;
    case FlareHeader::NACK:
        HandleCredit(flare.GetFlowId(), flare.GetSeq(), flare.GetRemainingHops());
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
        SendCredit(*flow, flow->nextCreditSeq++);
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
    for (uint32_t seq = 0; seq < window_end; ++seq)
    {
        if (flow->receivedSeqs.find(seq) == flow->receivedSeqs.end())
        {
            SendCredit(*flow, seq);
        }
    }

    flow->creditRefreshEvent =
        Simulator::Schedule(m_retransmissionTimeout,
                            &FlareHostApp::RefreshCredits, this, flow_id);
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
    ++flow.creditsSent;
    SendFlarePacket(flow, seq, FlareHeader::CREDIT, FlareHeader::NONE, 0);
}

void
FlareHostApp::SendData(FlowState& flow, uint32_t seq, bool retransmission)
{
    if (retransmission)
    {
        ++flow.retransmissions;
    }
    ++flow.dataSent;
    flow.sentSeqs.insert(seq);
    SendFlarePacket(flow, seq, FlareHeader::DATA, FlareHeader::NONE,
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
    flare.SetRemainingHops(1);
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
FlareHostApp::HandleCredit(uint32_t flow_id, uint32_t seq, uint32_t remaining_hops)
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
    const uint32_t next_credit = seq + flow->initialCreditPkts;
    if (next_credit < flow->totalPackets)
    {
        SendCredit(*flow, next_credit);
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
