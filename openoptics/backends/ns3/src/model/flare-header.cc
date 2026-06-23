#include "flare-header.h"

namespace ns3
{
namespace openoptics
{

NS_OBJECT_ENSURE_REGISTERED(FlareHeader);

TypeId
FlareHeader::GetTypeId()
{
    static TypeId tid = TypeId("ns3::openoptics::FlareHeader")
                            .SetParent<Header>()
                            .SetGroupName("OpenOptics")
                            .AddConstructor<FlareHeader>();
    return tid;
}

TypeId
FlareHeader::GetInstanceTypeId() const
{
    return GetTypeId();
}

FlareHeader::FlareHeader()
    : m_magic(kMagic),
      m_type(DATA),
      m_controlCode(NONE),
      m_flowId(0),
      m_seq(0),
      m_creditEpoch(0),
      m_srcNode(0),
      m_dstNode(0),
      m_pathId(0),
      m_remainingHops(0),
      m_totalHops(0),
      m_timeSlice(0)
{
}

void FlareHeader::SetType(PacketType type) { m_type = static_cast<uint8_t>(type); }
FlareHeader::PacketType FlareHeader::GetType() const { return static_cast<PacketType>(m_type); }
void FlareHeader::SetControlCode(ControlCode code) { m_controlCode = static_cast<uint8_t>(code); }
FlareHeader::ControlCode FlareHeader::GetControlCode() const { return static_cast<ControlCode>(m_controlCode); }
void FlareHeader::SetFlowId(uint32_t flow_id) { m_flowId = flow_id; }
uint32_t FlareHeader::GetFlowId() const { return m_flowId; }
void FlareHeader::SetSeq(uint32_t seq) { m_seq = seq; }
uint32_t FlareHeader::GetSeq() const { return m_seq; }
void FlareHeader::SetCreditEpoch(uint32_t epoch) { m_creditEpoch = epoch; }
uint32_t FlareHeader::GetCreditEpoch() const { return m_creditEpoch; }
void FlareHeader::SetSrcNode(uint32_t node) { m_srcNode = node; }
uint32_t FlareHeader::GetSrcNode() const { return m_srcNode; }
void FlareHeader::SetDstNode(uint32_t node) { m_dstNode = node; }
uint32_t FlareHeader::GetDstNode() const { return m_dstNode; }
void FlareHeader::SetPathId(uint16_t path_id) { m_pathId = path_id; }
uint16_t FlareHeader::GetPathId() const { return m_pathId; }
void FlareHeader::SetRemainingHops(uint8_t hops) { m_remainingHops = hops; }
uint8_t FlareHeader::GetRemainingHops() const { return m_remainingHops; }
void FlareHeader::SetTotalHops(uint8_t hops) { m_totalHops = hops; }
uint8_t FlareHeader::GetTotalHops() const { return m_totalHops; }
void FlareHeader::SetTimeSlice(uint8_t ts) { m_timeSlice = ts; }
uint8_t FlareHeader::GetTimeSlice() const { return m_timeSlice; }
bool FlareHeader::IsValid() const { return m_magic == kMagic; }

uint32_t
FlareHeader::GetSerializedSize() const
{
    return 29;
}

void
FlareHeader::Serialize(Buffer::Iterator start) const
{
    start.WriteHtonU16(m_magic);
    start.WriteU8(m_type);
    start.WriteU8(m_controlCode);
    start.WriteHtonU32(m_flowId);
    start.WriteHtonU32(m_seq);
    start.WriteHtonU32(m_creditEpoch);
    start.WriteHtonU32(m_srcNode);
    start.WriteHtonU32(m_dstNode);
    start.WriteHtonU16(m_pathId);
    start.WriteU8(m_remainingHops);
    start.WriteU8(m_totalHops);
    start.WriteU8(m_timeSlice);
}

uint32_t
FlareHeader::Deserialize(Buffer::Iterator start)
{
    m_magic = start.ReadNtohU16();
    m_type = start.ReadU8();
    m_controlCode = start.ReadU8();
    m_flowId = start.ReadNtohU32();
    m_seq = start.ReadNtohU32();
    m_creditEpoch = start.ReadNtohU32();
    m_srcNode = start.ReadNtohU32();
    m_dstNode = start.ReadNtohU32();
    m_pathId = start.ReadNtohU16();
    m_remainingHops = start.ReadU8();
    m_totalHops = start.ReadU8();
    m_timeSlice = start.ReadU8();
    return GetSerializedSize();
}

void
FlareHeader::Print(std::ostream& os) const
{
    os << "Flare type=" << static_cast<int>(m_type)
       << " flow=" << m_flowId
       << " seq=" << m_seq
       << " epoch=" << m_creditEpoch
       << " remaining_hops=" << static_cast<int>(m_remainingHops)
       << " time_slice=" << static_cast<int>(m_timeSlice);
}

} // namespace openoptics
} // namespace ns3
