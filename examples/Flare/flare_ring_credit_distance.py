"""Ring-topology Flare credit-distance experiment.

The topology is an 8-ToR bidirectional ring.  Each ToR has one host, and every
ToR acts as both a sender and a receiver.  A case with distance k installs data
flows:

    n -> (n + k) mod 8

for every node n.  Credits travel in the reverse direction, so the credit path
length is k hops for k in {1, 2, 4}.  Running k=1, k=2, and k=4 separately gives
a clean view of how longer credit paths create more shared egress credit
pressure and drop.

Examples:

    PYTHONPATH=$PWD python examples/Flare/flare_ring_credit_distance.py \
        --distance all \
        --flows-per-pair 8 \
        --initial-credit 16 \
        --flare-credit-qsize 8 \
        --output results/flare_ring_credit_distance.json

    PYTHONPATH=$PWD python examples/Flare/flare_ring_credit_distance.py \
        --distance 4 \
        --flows-per-pair 8 \
        --initial-credit 16 \
        --flare-credit-qsize 8 \
        --output results/flare_ring_d4.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCRIPT_PATH = Path(__file__).resolve()
for root in (SCRIPT_PATH.parents[2], Path.cwd()):
    if (root / "openoptics").exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
        break

from openoptics import Toolbox
from openoptics.TimeFlowTable import Path as OpticalPath
from openoptics.TimeFlowTable import Step
from openoptics.backends.ns3.traffic import FlareConfig, FlareStats


RING_NODES = 8


@dataclass(frozen=True)
class FlowPlan:
    distance: int
    sender: int
    receiver: int
    replica: int
    name: str
    start_s: float
    data_path_nodes: Tuple[int, ...]
    credit_path_nodes: Tuple[int, ...]

    @property
    def credit_hops(self) -> int:
        return len(self.credit_path_nodes) - 1


def _slice_us(profile: str) -> int:
    return 55 if profile == "55us" else 15


def _topology(nodes: int = RING_NODES) -> List[List[int]]:
    """Return one static bidirectional ring slice.

    Port convention:
      port 0 -> clockwise neighbor
      port 1 -> counter-clockwise neighbor
    """
    return [
        [0, node, (node + 1) % nodes, 0, 1]
        for node in range(nodes)
    ]


def _port_map(nodes: int = RING_NODES) -> Dict[Tuple[int, int], int]:
    ports: Dict[Tuple[int, int], int] = {}
    for _, node, nxt, port_node, port_nxt in _topology(nodes):
        ports[(node, nxt)] = port_node
        ports[(nxt, node)] = port_nxt
    return ports


def _ring_path(
    src: int,
    dst: int,
    *,
    nodes: int = RING_NODES,
    tie_break: str = "clockwise",
) -> Tuple[int, ...]:
    if src == dst:
        return (int(src),)
    clockwise_hops = (dst - src) % nodes
    counter_hops = (src - dst) % nodes
    if clockwise_hops < counter_hops:
        step = 1
        hops = clockwise_hops
    elif counter_hops < clockwise_hops:
        step = -1
        hops = counter_hops
    else:
        step = 1 if tie_break == "clockwise" else -1
        hops = clockwise_hops
    path = [int(src)]
    cur = int(src)
    for _ in range(hops):
        cur = (cur + step) % nodes
        path.append(cur)
    return tuple(path)


def _same_slice_ring_paths(
    *,
    nodes: int = RING_NODES,
    tie_break: str = "clockwise",
) -> Tuple[List[OpticalPath], Dict[Tuple[int, int, int], Tuple[int, ...]]]:
    ports = _port_map(nodes)
    paths: List[OpticalPath] = []
    nodes_by_slice: Dict[Tuple[int, int, int], Tuple[int, ...]] = {}
    ts = 0
    for src in range(nodes):
        for dst in range(nodes):
            if src == dst:
                continue
            path_nodes = _ring_path(src, dst, nodes=nodes, tie_break=tie_break)
            steps: List[Step] = []
            for cur_node, next_node in zip(path_nodes[:-1], path_nodes[1:]):
                steps.append(
                    Step(
                        cur_node=int(cur_node),
                        step_type="port",
                        send_port=int(ports[(cur_node, next_node)]),
                        send_ts=ts,
                        send_node=int(next_node),
                    )
                )
            paths.append(
                OpticalPath(
                    src=int(src),
                    arrival_ts=ts,
                    dst=int(dst),
                    steps=steps,
                )
            )
            nodes_by_slice[(int(src), int(dst), ts)] = path_nodes
    return paths, nodes_by_slice


def _distances(value: str) -> List[int]:
    if value == "all":
        return [1, 2, 4]
    return [int(value)]


def _flow_plans(
    args: argparse.Namespace,
    distance: int,
    nodes_by_slice: Mapping[Tuple[int, int, int], Tuple[int, ...]],
) -> List[FlowPlan]:
    plans: List[FlowPlan] = []
    replica_gap_s = args.replica_gap_us / 1e6
    node_gap_s = args.node_gap_us / 1e6
    for sender in range(args.nodes):
        receiver = (sender + distance) % args.nodes
        data_path = nodes_by_slice[(sender, receiver, 0)]
        credit_path = nodes_by_slice[(receiver, sender, 0)]
        for replica in range(args.flows_per_pair):
            plans.append(
                FlowPlan(
                    distance=distance,
                    sender=sender,
                    receiver=receiver,
                    replica=replica,
                    name=f"d{distance}-s{sender}-r{receiver}-f{replica:03d}",
                    start_s=args.start + sender * node_gap_s + replica * replica_gap_s,
                    data_path_nodes=data_path,
                    credit_path_nodes=credit_path,
                )
            )
    plans.sort(key=lambda p: (p.start_s, p.sender, p.replica))
    return plans


def _validate_plans(
    plans: Sequence[FlowPlan],
    nodes_by_slice: Mapping[Tuple[int, int, int], Tuple[int, ...]],
) -> None:
    errors: List[str] = []
    for plan in plans:
        actual_data = nodes_by_slice.get((plan.sender, plan.receiver, 0))
        actual_credit = nodes_by_slice.get((plan.receiver, plan.sender, 0))
        if actual_data != plan.data_path_nodes:
            errors.append(
                f"{plan.name}: data path expected {plan.data_path_nodes}, got {actual_data}"
            )
        if actual_credit != plan.credit_path_nodes:
            errors.append(
                f"{plan.name}: credit path expected {plan.credit_path_nodes}, got {actual_credit}"
            )
    if errors:
        raise RuntimeError("ring path validation failed:\n  " + "\n  ".join(errors))


def _build_network(args: argparse.Namespace, distance: int, flow_size: int):
    slice_us = _slice_us(args.profile)
    net = Toolbox.BaseNetwork(
        name=f"flare_ring_credit_distance_d{distance}",
        backend="ns3",
        nb_node=args.nodes,
        nb_link=2,
        time_slice_duration_us=slice_us,
        guardband_us=args.guardband_us,
        ocs_tor_link_bw_gbps=args.ocs_bw,
        tor_host_link_bw_gbps=args.host_bw,
        use_webserver=args.dashboard,
        simulation_stop_s=args.stop,
        snapshot_interval_us=max(1, slice_us),
        host_link_delay_us=args.host_delay_us,
        ocs_link_delay_us=args.ocs_delay_us,
        flare_profile=args.profile,
        flare_w_init=1.0,
        flare_target_loss=args.target_loss,
        flare_credit_qsize_pkts=args.flare_credit_qsize,
        flare_shaping_thresh_pkts=args.flare_shaping_thresh,
        flare_aeolus_thresh_pkts=args.flare_aeolus_thresh,
        flare_congestion_threshold_percent=args.flare_congestion_threshold,
        flare_tentative_threshold_percent=args.flare_tentative_threshold,
    )
    net.deploy_topo(_topology(args.nodes))
    paths, nodes_by_slice = _same_slice_ring_paths(
        nodes=args.nodes,
        tie_break=args.tie_break,
    )
    net.deploy_routing(paths, routing_mode="Per-hop")
    plans = _flow_plans(args, distance, nodes_by_slice)
    _validate_plans(plans, nodes_by_slice)

    config = FlareConfig.from_profile(
        args.profile,
        credit_qsize_pkts=args.flare_credit_qsize,
        shaping_thresh_pkts=args.flare_shaping_thresh,
        aeolus_thresh_pkts=args.flare_aeolus_thresh,
        w_init=1.0,
        target_loss=args.target_loss,
        mtu_bytes=args.mtu,
        retransmission_timeout_s=args.refresh_timeout_us / 1e6,
        initial_credit_pkts=args.initial_credit,
        congestion_threshold_percent=args.flare_congestion_threshold,
        tentative_threshold_percent=args.flare_tentative_threshold,
    )
    gen = net.flare_traffic(config=config)
    for plan in plans:
        gen.flow(
            plan.sender,
            plan.receiver,
            flow_size,
            start_s=plan.start_s,
            duration_s=args.duration,
            packet_size_bytes=args.mtu,
            name=plan.name,
        )
    return net, gen.install(), plans, nodes_by_slice


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _safe_ratio(num: float, den: float) -> Optional[float]:
    return None if den == 0 else num / den


def _mean(values: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in values if _finite(v)]
    return statistics.mean(vals) if vals else None


def _percentile(values: Iterable[float], pct: float) -> Optional[float]:
    vals = sorted(float(v) for v in values if _finite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * pct
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return vals[int(idx)]
    weight = idx - lo
    return vals[lo] * (1.0 - weight) + vals[hi] * weight


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _call_int(app: Any, getter: str) -> int:
    if not hasattr(app, getter):
        return 0
    return int(getattr(app, getter)())


def _tor_rows(installed_flows, distance: int) -> List[Dict[str, Any]]:
    if not installed_flows:
        return []
    backend = getattr(installed_flows[0], "_backend", None)
    tor_apps = getattr(backend, "_tor_apps", {})
    rows: List[Dict[str, Any]] = []
    for tor_id in sorted(tor_apps):
        app = tor_apps[tor_id]
        rows.append(
            {
                "distance": distance,
                "tor": int(tor_id),
                "from_host": int(app.GetIngressFromHostCount()),
                "from_uplink": int(app.GetIngressFromUplinkCount()),
                "forwarded": int(app.GetForwardedCount()),
                "delivered": int(app.GetDeliveredToHostCount()),
                "drops": int(app.GetDropCount()),
                "overflow_drops": int(app.GetSliceOverflowDrops()),
                "credit_admitted": int(app.GetFlareCreditAdmitted()),
                "credit_dropped": int(app.GetFlareCreditDropped()),
                "credit_wasted": int(app.GetFlareCreditWasted()),
                "data_packets_seen": int(app.GetFlareCreditDataPackets()),
                "drop_forward_send_fail": _call_int(app, "GetDropForwardSendFail"),
                "drop_forward_cq": _call_int(app, "GetDropForwardCq"),
                "drop_adm_fail": _call_int(app, "GetDropAdmFail"),
                "drop_aeolus_unscheduled": _call_int(app, "GetDropAeolusUnscheduled"),
            }
        )
    return rows


def _flow_row(plan: FlowPlan, stats: FlareStats) -> Dict[str, Any]:
    credit_loss = stats.credits_sent - stats.credits_received
    completed = _finite(stats.fct_s) and float(stats.fct_s) > 0
    return {
        "distance": plan.distance,
        "flow_id": stats.flow_id,
        "name": plan.name,
        "sender": plan.sender,
        "receiver": plan.receiver,
        "replica": plan.replica,
        "start_s": plan.start_s,
        "data_path": "->".join(str(node) for node in plan.data_path_nodes),
        "credit_path": "->".join(str(node) for node in plan.credit_path_nodes),
        "credit_hops": plan.credit_hops,
        "fct_s": stats.fct_s,
        "completed": bool(completed),
        "goodput_bps": stats.throughput_bps,
        "credits_sent": stats.credits_sent,
        "credits_received": stats.credits_received,
        "credit_delivery_ratio": _safe_ratio(stats.credits_received, stats.credits_sent),
        "credit_loss_proxy": credit_loss,
        "credit_tax": _safe_ratio(stats.credits_sent, stats.data_packets_received),
        "lost_credit_hop_cost_proxy": credit_loss * plan.credit_hops,
        "credit_hop_cost_sent": stats.credits_sent * plan.credit_hops,
        "credit_hop_cost_received": stats.credits_received * plan.credit_hops,
        "data_packets_sent": stats.data_packets_sent,
        "data_packets_received": stats.data_packets_received,
        "duplicate_credits": stats.duplicate_credits,
        "duplicate_data": stats.duplicate_data,
        "timeouts": stats.timeouts,
        "retransmissions": stats.retransmissions,
    }


def _group_summary(rows: Iterable[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    grouped: DefaultDict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)

    out: Dict[str, Dict[str, Any]] = {}
    for value, subset in sorted(grouped.items(), key=lambda item: str(item[0])):
        credits_sent = sum(row["credits_sent"] for row in subset)
        credits_received = sum(row["credits_received"] for row in subset)
        fcts = [
            row["fct_s"] for row in subset
            if _finite(row["fct_s"]) and float(row["fct_s"]) > 0
        ]
        goodputs = [row["goodput_bps"] for row in subset if _finite(row["goodput_bps"])]
        data_received = sum(row["data_packets_received"] for row in subset)
        out[str(value)] = {
            "flow_count": len(subset),
            "completed_flow_count": len(fcts),
            "credits_sent": credits_sent,
            "credits_received": credits_received,
            "credit_delivery_ratio": _safe_ratio(credits_received, credits_sent),
            "credit_loss_proxy": credits_sent - credits_received,
            "credit_tax": _safe_ratio(credits_sent, data_received),
            "lost_credit_hop_cost_proxy": sum(
                row["lost_credit_hop_cost_proxy"] for row in subset
            ),
            "credit_hop_cost_sent": sum(row["credit_hop_cost_sent"] for row in subset),
            "credit_hop_cost_received": sum(
                row["credit_hop_cost_received"] for row in subset
            ),
            "mean_fct_s": _mean(fcts),
            "p95_fct_s": _percentile(fcts, 0.95),
            "mean_goodput_bps": _mean(goodputs),
            "data_packets_sent": sum(row["data_packets_sent"] for row in subset),
            "data_packets_received": data_received,
            "timeouts": sum(row["timeouts"] for row in subset),
            "retransmissions": sum(row["retransmissions"] for row in subset),
        }
    return out


def _expected_pressure(args: argparse.Namespace, plans: Sequence[FlowPlan], flow_size: int) -> Dict[str, Any]:
    ports = _port_map(args.nodes)
    burst_per_flow = min(args.initial_credit, math.ceil(flow_size / args.mtu))
    edge_counts: DefaultDict[str, int] = defaultdict(int)
    port_counts: DefaultDict[str, int] = defaultdict(int)
    for plan in plans:
        for src, dst in zip(plan.credit_path_nodes[:-1], plan.credit_path_nodes[1:]):
            edge_counts[f"{src}->{dst}"] += burst_per_flow
            port_counts[f"tor{src}:port{ports[(src, dst)]}"] += burst_per_flow
    return {
        "burst_per_flow": burst_per_flow,
        "initial_credit_burst_total": len(plans) * burst_per_flow,
        "by_credit_edge": dict(
            sorted(edge_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "by_egress_port": dict(
            sorted(port_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "note": (
            "For an all-to-distance ring case, each directed ring edge is used "
            "by roughly distance flow groups, so offered credit pressure scales "
            "with hop distance."
        ),
    }


def _summary(
    args: argparse.Namespace,
    distance: int,
    flow_size: int,
    flow_rows: List[Dict[str, Any]],
    tor_rows: List[Dict[str, Any]],
    plans: Sequence[FlowPlan],
) -> Dict[str, Any]:
    total_credits_sent = sum(row["credits_sent"] for row in flow_rows)
    total_credits_received = sum(row["credits_received"] for row in flow_rows)
    fcts = [
        row["fct_s"] for row in flow_rows
        if _finite(row["fct_s"]) and float(row["fct_s"]) > 0
    ]
    goodputs = [row["goodput_bps"] for row in flow_rows if _finite(row["goodput_bps"])]
    data_received = sum(row["data_packets_received"] for row in flow_rows)
    return {
        "scenario": "ring-credit-distance",
        "distance": distance,
        "nodes": args.nodes,
        "tie_break": args.tie_break,
        "flow_count": len(flow_rows),
        "flow_size": flow_size,
        "flow_packets": math.ceil(flow_size / args.mtu),
        "flows_per_pair": args.flows_per_pair,
        "initial_credit": args.initial_credit,
        "flare_credit_qsize": args.flare_credit_qsize,
        "flare_congestion_threshold": args.flare_congestion_threshold,
        "expected_pressure": _expected_pressure(args, plans, flow_size),
        "total_credits_sent": total_credits_sent,
        "total_credits_received": total_credits_received,
        "total_credit_delivery_ratio": _safe_ratio(
            total_credits_received, total_credits_sent
        ),
        "total_credit_loss_proxy": total_credits_sent - total_credits_received,
        "total_credit_tax": _safe_ratio(total_credits_sent, data_received),
        "total_lost_credit_hop_cost_proxy": sum(
            row["lost_credit_hop_cost_proxy"] for row in flow_rows
        ),
        "completed_flow_count": len(fcts),
        "mean_fct_s": _mean(fcts),
        "p95_fct_s": _percentile(fcts, 0.95),
        "mean_goodput_bps": _mean(goodputs),
        "data_packets_sent": sum(row["data_packets_sent"] for row in flow_rows),
        "data_packets_received": data_received,
        "total_tor_credit_admitted": sum(row["credit_admitted"] for row in tor_rows),
        "total_tor_credit_dropped": sum(row["credit_dropped"] for row in tor_rows),
        "total_tor_credit_wasted": sum(row["credit_wasted"] for row in tor_rows),
        "total_overflow_drops": sum(row["overflow_drops"] for row in tor_rows),
        "sender_summary": _group_summary(flow_rows, "sender"),
        "receiver_summary": _group_summary(flow_rows, "receiver"),
        "tor_summary": {str(row["tor"]): row for row in tor_rows},
    }


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _print_case_setup(args: argparse.Namespace, distance: int, plans: Sequence[FlowPlan]) -> None:
    print(f"\n=== Ring Credit Distance Experiment: distance={distance} ===")
    print(
        f"nodes={args.nodes}, profile={args.profile}, "
        f"flows_per_pair={args.flows_per_pair}, "
        f"initial_credit={args.initial_credit}, qsize={args.flare_credit_qsize}"
    )
    print("Topology: 0--1--2--3--4--5--6--7--0")
    print("Flow pattern: data n->n+distance, credit receiver->sender")
    for plan in plans[: min(args.nodes, len(plans))]:
        print(
            f"  {plan.name}: data {'->'.join(map(str, plan.data_path_nodes))}, "
            f"credit {'->'.join(map(str, plan.credit_path_nodes))}, "
            f"hops={plan.credit_hops}"
        )
    if len(plans) > args.nodes:
        print(f"  ... {len(plans) - args.nodes} more replicated flows")


def _run_case(args: argparse.Namespace, distance: int, flow_size: int) -> Dict[str, Any]:
    net, installed, plans, nodes_by_slice = _build_network(args, distance, flow_size)
    _print_case_setup(args, distance, plans)
    net.start()

    plans_by_name = {plan.name: plan for plan in plans}
    flow_rows: List[Dict[str, Any]] = []
    for installed_flow in installed:
        plan = plans_by_name.get(installed_flow.spec.name)
        if plan is None:
            raise RuntimeError(f"installed flow has no plan: {installed_flow.spec.name}")
        stats = installed_flow.stats()
        if isinstance(stats, FlareStats):
            flow_rows.append(_flow_row(plan, stats))
    flow_rows.sort(key=lambda row: (row["distance"], row["sender"], row["replica"]))
    tor_rows = _tor_rows(installed, distance)
    summary = _summary(args, distance, flow_size, flow_rows, tor_rows, plans)
    print("Summary:")
    print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
    return {
        "summary": summary,
        "flows": flow_rows,
        "tors": tor_rows,
        "routes": {
            f"{src}->{dst}@{ts}": list(path)
            for (src, dst, ts), path in sorted(nodes_by_slice.items())
        },
    }


def _combined_summary(cases: Mapping[str, Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    for key, case in sorted(cases.items(), key=lambda item: int(item[0])):
        s = case["summary"]
        rows.append(
            {
                "distance": int(key),
                "credit_delivery_ratio": s["total_credit_delivery_ratio"],
                "credit_loss_proxy": s["total_credit_loss_proxy"],
                "credit_tax": s["total_credit_tax"],
                "lost_credit_hop_cost_proxy": s["total_lost_credit_hop_cost_proxy"],
                "tor_credit_dropped": s["total_tor_credit_dropped"],
                "tor_credit_wasted": s["total_tor_credit_wasted"],
                "completed_flow_count": s["completed_flow_count"],
                "mean_fct_s": s["mean_fct_s"],
                "mean_goodput_bps": s["mean_goodput_bps"],
            }
        )
    return {"by_distance": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distance", choices=["1", "2", "4", "all"], default="all")
    parser.add_argument("--nodes", type=int, default=RING_NODES)
    parser.add_argument(
        "--tie-break",
        choices=["clockwise", "counterclockwise"],
        default="clockwise",
        help="direction used when both ring paths have equal length, e.g. distance 4",
    )
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--flows-per-pair", type=int, default=8)
    parser.add_argument("--flow-size", type=int, default=0,
                        help="0 means initial_credit * mtu")
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=16)
    parser.add_argument("--start", type=float, default=0.000105)
    parser.add_argument("--replica-gap-us", type=float, default=0.0)
    parser.add_argument("--node-gap-us", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=0.0001)
    parser.add_argument("--stop", type=float, default=0.00035)
    parser.add_argument("--refresh-timeout-us", type=float, default=1000.0)
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    parser.add_argument("--host-delay-us", type=int, default=1)
    parser.add_argument("--ocs-delay-us", type=int, default=1)
    parser.add_argument("--guardband-us", type=int, default=0)
    parser.add_argument("--target-loss", type=float, default=0.1)
    parser.add_argument("--flare-credit-qsize", type=int, default=8)
    parser.add_argument("--flare-shaping-thresh", type=int, default=4096)
    parser.add_argument("--flare-aeolus-thresh", type=int, default=4096)
    parser.add_argument("--flare-congestion-threshold", type=int, default=100)
    parser.add_argument("--flare-tentative-threshold", type=int, default=100)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/flare_ring_credit_distance.json"),
    )
    args = parser.parse_args(argv)
    if args.nodes != RING_NODES:
        raise ValueError("this experiment currently expects --nodes 8")
    if args.flows_per_pair <= 0:
        raise ValueError("--flows-per-pair must be positive")
    if args.initial_credit <= 0:
        raise ValueError("--initial-credit must be positive")
    if args.flow_size < 0:
        raise ValueError("--flow-size must be non-negative")

    flow_size = args.flow_size or args.initial_credit * args.mtu
    cases: Dict[str, Dict[str, Any]] = {}
    all_flow_rows: List[Dict[str, Any]] = []
    all_tor_rows: List[Dict[str, Any]] = []
    for distance in _distances(args.distance):
        case = _run_case(args, distance, flow_size)
        cases[str(distance)] = case
        all_flow_rows.extend(case["flows"])
        all_tor_rows.extend(case["tors"])

    payload = {
        "experiment": "flare_ring_credit_distance",
        "parameters": dict(vars(args), resolved_flow_size=flow_size),
        "topology": _topology(args.nodes),
        "summary": _combined_summary(cases),
        "cases": cases,
    }
    _write_json(args.output, payload)
    _write_csv(args.output.with_suffix(".flows.csv"), all_flow_rows)
    _write_csv(args.output.with_suffix(".tors.csv"), all_tor_rows)
    print(f"\nWrote {args.output}")
    print(f"Wrote {args.output.with_suffix('.flows.csv')}")
    print(f"Wrote {args.output.with_suffix('.tors.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
