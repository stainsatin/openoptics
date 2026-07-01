#ifndef OPENOPTICS_FLARE_HOST_APP_H
#define OPENOPTICS_FLARE_HOST_APP_H

#include "ns3/application.h"
#include "ns3/event-id.h"
#include "ns3/ipv4-address.h"
#include "ns3/net-device.h"
#include "ns3/nstime.h"
#include "ns3/packet.h"
#include "ns3/ptr.h"
#include "ns3/random-variable-stream.h"
#include "ns3/socket.h"

#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>

namespace ns3
{
namespace openoptics
{

class FlareHostApp : public Application
{
  public:
    FlareHostApp();
    ~FlareHostApp() override;

    static TypeId GetTypeId();

    void SetNodeId(uint32_t node_id);
    void SetHostDevice(Ptr<NetDevice> device);
    void SetDefaultConfig(uint32_t credit_qsize_pkts,
                          uint32_t shaping_thresh_pkts,
                          uint32_t aeolus_thresh_pkts,
                          double w_init,
                          double target_loss,
                          uint32_t mtu_bytes,
                          double retransmission_timeout_s);

    void AddSenderFlow(uint32_t flow_id,
                       uint32_t dst_node,
                       const std::string& src_ip,
                       const std::string& dst_ip,
                       double start_s,
                       double stop_s,
                       uint32_t size_bytes,
                       uint32_t packet_size_bytes,
                       uint32_t port,
                       uint32_t path_id,
                       uint32_t initial_credit_pkts);

    void AddReceiverFlow(uint32_t flow_id,
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
                         uint32_t credit_path_hops);

    uint64_t GetDataPacketsSent(uint32_t flow_id) const;
    uint64_t GetDataPacketsReceived(uint32_t flow_id) const;
    uint64_t GetCreditsSent(uint32_t flow_id) const;
    uint64_t GetCreditsReceived(uint32_t flow_id) const;
    uint64_t GetRetransmissions(uint32_t flow_id) const;
    uint64_t GetTimeouts(uint32_t flow_id) const;
    uint64_t GetDuplicateData(uint32_t flow_id) const;
    uint64_t GetDuplicateCredits(uint32_t flow_id) const;
    uint64_t GetFlowCompletionTimeUs(uint32_t flow_id) const;
    uint64_t GetPathLengthCount(uint32_t flow_id, uint32_t remaining_hops) const;

  protected:
    void StartApplication() override;
    void StopApplication() override;

  private:
    struct FlowState
    {
        uint32_t flowId = 0;
        uint32_t peerNode = 0;
        Ipv4Address localIp;
        Ipv4Address peerIp;
        Time start;
        Time stop;
        uint32_t sizeBytes = 0;
        uint32_t packetSizeBytes = 1024;
        uint32_t port = 0;
        uint32_t pathId = 0;
        uint32_t initialCreditPkts = 1;
        uint32_t creditPathHops = 1;
        uint32_t totalPackets = 0;
        uint32_t nextSendSeq = 0;
        uint32_t nextCreditSeq = 0;
        Time firstDataRx;
        Time completionTime;
        bool done = false;
        std::unordered_set<uint32_t> creditSeqs;
        std::unordered_set<uint32_t> sentSeqs;
        std::unordered_set<uint32_t> receivedSeqs;
        std::unordered_map<uint32_t, uint32_t> pathLengthHistogram;
        EventId sendEvent;
        EventId creditRefreshEvent;
        uint64_t dataSent = 0;
        uint64_t dataReceived = 0;
        uint64_t creditsSent = 0;
        uint64_t creditsReceived = 0;
        uint64_t retransmissions = 0;
        uint64_t timeouts = 0;
        uint64_t duplicateData = 0;
        uint64_t duplicateCredits = 0;
        std::unordered_map<uint32_t, uint8_t> creditTimeSliceMap;  // seq -> time_slice
        std::unordered_map<uint32_t, Time> creditSentAt;           // receiver: seq -> last credit send

        // Rate control 状态
        double targetCreditRate = 1.0;      // 目标 credit rate (0-1)
        uint32_t lossCountThisSlice = 0;    // 当前时间片的 loss
        uint8_t lastPathLength = 0;         // 上一次的路径长度
        Time lastSliceChange;               // 上次时间片切换时间
    };

    void ReceiveFromHostDevice(Ptr<NetDevice> device,
                               Ptr<const Packet> packet,
                               uint16_t protocol,
                               const Address& src,
                               const Address& dst,
                               NetDevice::PacketType packetType);
    void ReceiveFromSocket(Ptr<Socket> socket);
    void HandleFlarePacket(Ptr<Packet> packet);

    void SendInitialCredits(uint32_t flow_id);
    void RefreshCredits(uint32_t flow_id);
    void SendEligibleData(uint32_t flow_id);
    void OnRetransmissionTimeout(uint32_t flow_id, uint32_t seq);
    void SendCredit(FlowState& flow, uint32_t seq);
    void SendData(FlowState& flow, uint32_t seq, bool retransmission);
    void SendControlDone(FlowState& flow);
    void SendFlarePacket(FlowState& flow,
                         uint32_t seq,
                         uint8_t packet_type,
                         uint8_t control_code,
                         uint32_t payload_bytes);
    void HandleCredit(uint32_t flow_id, uint32_t seq, uint32_t remaining_hops, uint8_t time_slice);
    void HandleData(uint32_t flow_id, uint32_t seq, uint32_t remaining_hops);
    void HandleControl(uint32_t flow_id, uint8_t control_code);

    void AdjustCreditRate(FlowState& flow, bool credit_dropped);
    void OnPathChange(FlowState& flow, uint8_t new_path_length);

    const FlowState* FindFlow(uint32_t flow_id) const;
    FlowState* FindSenderFlow(uint32_t flow_id);
    FlowState* FindReceiverFlow(uint32_t flow_id);
    uint32_t PacketPayloadBytes(const FlowState& flow, uint32_t seq) const;

    uint32_t m_nodeId;
    Ptr<NetDevice> m_hostDev;
    Ptr<Socket> m_recvSocket;
    uint32_t m_creditQsizePkts;
    uint32_t m_shapingThreshPkts;
    uint32_t m_aeolusThreshPkts;
    double m_wInit;
    double m_targetLoss;
    uint32_t m_mtuBytes;
    Time m_retransmissionTimeout;
    std::unordered_map<uint32_t, FlowState> m_senderFlows;
    std::unordered_map<uint32_t, FlowState> m_receiverFlows;
    Ptr<UniformRandomVariable> m_flareAdmissionRng;
};

} // namespace openoptics
} // namespace ns3

#endif // OPENOPTICS_FLARE_HOST_APP_H
