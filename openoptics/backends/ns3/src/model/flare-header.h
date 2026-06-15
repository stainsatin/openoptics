// Copyright (c) Max-Planck-Gesellschaft zur Förderung der Wissenschaftens e.V.
//
// Flare transport metadata for the OpenOptics ns-3 backend.

#ifndef OPENOPTICS_FLARE_HEADER_H
#define OPENOPTICS_FLARE_HEADER_H

#include "ns3/header.h"

#include <cstdint>

namespace ns3
{
namespace openoptics
{

class FlareHeader : public Header
{
  public:
    enum PacketType : uint8_t
    {
        DATA = 1,
        CREDIT = 2,
        TENTATIVE_CREDIT = 3,
        NACK = 4,
        CONTROL = 5,
    };

    enum ControlCode : uint8_t
    {
        NONE = 0,
        FLOW_DONE = 1,
        UNSCHEDULED = 2,
    };

    FlareHeader();

    static TypeId GetTypeId();
    TypeId GetInstanceTypeId() const override;

    void SetType(PacketType type);
    PacketType GetType() const;
    void SetControlCode(ControlCode code);
    ControlCode GetControlCode() const;
    void SetFlowId(uint32_t flow_id);
    uint32_t GetFlowId() const;
    void SetSeq(uint32_t seq);
    uint32_t GetSeq() const;
    void SetCreditEpoch(uint32_t epoch);
    uint32_t GetCreditEpoch() const;
    void SetSrcNode(uint32_t node);
    uint32_t GetSrcNode() const;
    void SetDstNode(uint32_t node);
    uint32_t GetDstNode() const;
    void SetPathId(uint16_t path_id);
    uint16_t GetPathId() const;
    void SetRemainingHops(uint8_t hops);
    uint8_t GetRemainingHops() const;
    void SetTimeSlice(uint8_t ts);
    uint8_t GetTimeSlice() const;
    bool IsValid() const;

    uint32_t GetSerializedSize() const override;
    void Serialize(Buffer::Iterator start) const override;
    uint32_t Deserialize(Buffer::Iterator start) override;
    void Print(std::ostream& os) const override;

    static constexpr uint16_t kMagic = 0xF1A3;

  private:
    uint16_t m_magic;
    uint8_t m_type;
    uint8_t m_controlCode;
    uint32_t m_flowId;
    uint32_t m_seq;
    uint32_t m_creditEpoch;
    uint32_t m_srcNode;
    uint32_t m_dstNode;
    uint16_t m_pathId;
    uint8_t m_remainingHops;
    uint8_t m_timeSlice;
};

} // namespace openoptics
} // namespace ns3

#endif // OPENOPTICS_FLARE_HEADER_H
