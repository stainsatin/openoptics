r"""Receiver-local short/long Flare credit priority experiment.

This experiment uses a static topology supplied by the user.  Each selected
receiver owns one short-credit flow and one long-credit flow, with different
senders.  The only controlled variable is the order in which each receiver
issues its short and long credit bursts.

Topology, one static slice:

        tor0     tor1
           \     /
tor10 -- tor6 -- tor7 -- tor8 -- tor9 -- tor11
          |       |       |       |
         tor2    tor3    tor4    tor5
                  |
                 tor12

Credit paths:

    receiver12 short: 12 -> 3
    receiver12 long:  12 -> 3 -> 7 -> 8 -> 4

    receiver10 short: 10 -> 6 -> 2
    receiver10 long:  10 -> 6 -> 7 -> 3 -> 12

    receiver11 short: 11 -> 9
    receiver11 long:  11 -> 9 -> 8 -> 7

    receiver2 short:  2 -> 6
    receiver2 long:   2 -> 6 -> 7 -> 1

Example:

    PYTHONPATH=$PWD python examples/Flare/flare_receiver_pair_priority.py \
        --order long-first \
        --profile 15us \
        --flows-per-pair 8 \
        --initial-credit 16 \
        --flare-credit-qsize 8 \
        --gap-us 0 \
        --stop 0.00035 \
        --output results/receiver_pair_priority_long_first.json

Run the same command with ``--order short-first`` and compare the JSON
summaries.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Sequence, Tuple


SCRIPT_PATH = Path(__file__).resolve()
for root in (SCRIPT_PATH.parents[2], Path.cwd()):
    if (root / "openoptics").exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
        break

import networkx as nx

from openoptics import Toolbox
from openoptics.TimeFlowTable import Path as OpticalPath
from openoptics.TimeFlowTable import Step
from openoptics.backends.ns3.traffic import FlareConfig, FlareStats


STAGE_TORS = {
    2: "receiver2 first-hop / sender-short",
    3: "receiver12 short sender / branch to 7 or 12",
    6: "tor6 hub",
    7: "tor7 hub",
    8: "tor8 hub",
    9: "receiver11 first-hop / sender-short",
    10: "receiver10 first-hop",
    11: "receiver11 first-hop",
    12: "receiver12 first-hop / sender-long10",
}


@dataclass(frozen=True)
class FlowTemplate:
    receiver: int
    sender: int
    priority_class: str
    name: str
    credit_path_nodes: Tuple[int, ...]

    @property
    def credit_hops(self) -> int:
        return len(self.credit_path_nodes) - 1


@dataclass(frozen=True)
class FlowPlan:
    receiver: int
    sender: int
    priority_class: str
    group_name: str
    name: str
    start_s: float
    priority_rank: int
    credit_path_nodes: Tuple[int, ...]
    credit_hops: int


def _slice_us(profile: str) -> int:
    return 55 if profile == "55us" else 15


def _topology() -> List[List[int]]:
    """Return one static slice for the user-designed topology.

    Circuit format: [time_slice, node1, node2, port1, port2].
    ``nb_link`` must be at least 4 because tor6 and tor7 have degree 4.
    """
    return [
        [0, 10, 6, 0, 0],
        [0, 6, 7, 1, 0],
        [0, 7, 8, 1, 0],
        [0, 8, 9, 1, 0],
        [0, 9, 11, 1, 0],
        [0, 6, 0, 2, 0],
        [0, 7, 1, 2, 0],
        [0, 6, 2, 3, 0],
        [0, 7, 3, 3, 0],
        [0, 8, 4, 2, 0],
        [0, 9, 5, 2, 0],
        [0, 3, 12, 1, 0],
    ]


def _flow_templates() -> List[FlowTemplate]:
    return [
        FlowTemplate(
            receiver=12,
            sender=3,
            priority_class="short",
            name="r12-short-s3",
            credit_path_nodes=(12, 3),
        ),
        FlowTemplate(
            receiver=12,
            sender=4,
            priority_class="long",
            name="r12-long-s4",
            credit_path_nodes=(12, 3, 7, 8, 4),
        ),
        FlowTemplate(
            receiver=10,
            sender=2,
            priority_class="short",
            name="r10-short-s2",
            credit_path_nodes=(10, 6, 2),
        ),
        FlowTemplate(
            receiver=10,
            sender=12,
            priority_class="long",
            name="r10-long-s12",
            credit_path_nodes=(10, 6, 7, 3, 12),
        ),
        FlowTemplate(
            receiver=11,
            sender=9,
            priority_class="short",
            name="r11-short-s9",
            credit_path_nodes=(11, 9),
        ),
        FlowTemplate(
            receiver=11,
            sender=7,
            priority_class="long",
            name="r11-long-s7",
            credit_path_nodes=(11, 9, 8, 7),
        ),
        FlowTemplate(
            receiver=2,
            sender=6,
            priority_class="short",
            name="r2-short-s6",
            credit_path_nodes=(2, 6),
        ),
        FlowTemplate(
            receiver=2,
            sender=1,
            priority_class="long",
            name="r2-long-s1",
            credit_path_nodes=(2, 6, 7, 1),
        ),
    ]


def _rank_for_order(priority_class: str, order: str) -> int:
    if order == "long-first":
        return 0 if priority_class == "long" else 1
    if order == "short-first":
        return 0 if priority_class == "short" else 1
    if order == "simultaneous":
        return 0
    raise ValueError(f"unknown order {order!r}")


def _directed_port_map(circuits: Sequence[Sequence[int]]) -> Dict[Tuple[int, int, int], int]:
    port_map: Dict[Tuple[int, int, int], int] = {}
    for circuit in circuits:
        if len(circuit) < 5:
            raise ValueError(f"invalid circuit entry: {circuit!r}")
        ts, node1, node2, port1, port2 = (int(value) for value in circuit[:5])
        port_map[(ts, node1, node2)] = port1
        port_map[(ts, node2, node1)] = port2
    return port_map


def _same_slice_paths(
    slice_to_topo: Mapping[int, nx.Graph],
    port_map: Mapping[Tuple[int, int, int], int],
) -> Tuple[List[OpticalPath], Dict[Tuple[int, int, int], Tuple[int, ...]]]:
    paths: List[OpticalPath] = []
    nodes_by_slice: Dict[Tuple[int, int, int], Tuple[int, ...]] = {}
    for ts in sorted(slice_to_topo.keys()):
        graph = slice_to_topo[ts]
        for src in sorted(graph.nodes()):
            for dst in sorted(graph.nodes()):
                if src == dst:
                    continue
                try:
                    path_nodes = nx.shortest_path(graph, src, dst)
                except nx.NetworkXNoPath:
                    continue
                steps: List[Step] = []
                for cur_node, next_node in zip(path_nodes[:-1], path_nodes[1:]):
                    key = (int(ts), int(cur_node), int(next_node))
                    if key not in port_map:
                        raise RuntimeError(f"missing directed port for edge {key}")
                    steps.append(
                        Step(
                            cur_node=int(cur_node),
                            step_type="port",
                            send_port=int(port_map[key]),
                            send_ts=int(ts),
                            send_node=int(next_node),
                        )
                    )
                if not steps:
                    continue
                paths.append(
                    OpticalPath(
                        src=int(src),
                        arrival_ts=int(ts),
                        dst=int(dst),
                        steps=steps,
                    )
                )
                nodes_by_slice[(int(src), int(dst), int(ts))] = tuple(
                    int(node) for node in path_nodes
                )
    return paths, nodes_by_slice


def _validate_credit_paths(
    templates: Sequence[FlowTemplate],
    nodes_by_slice: Mapping[Tuple[int, int, int], Tuple[int, ...]],
) -> None:
    errors: List[str] = []
    for tmpl in templates:
        actual = nodes_by_slice.get((tmpl.receiver, tmpl.sender, 0))
        if actual != tmpl.credit_path_nodes:
            errors.append(
                f"{tmpl.name}: expected {tmpl.credit_path_nodes}, got {actual}"
            )
    if errors:
        raise RuntimeError("credit path validation failed:\n  " + "\n  ".join(errors))


def _make_flow_plans(args: argparse.Namespace) -> List[FlowPlan]:
    plans: List[FlowPlan] = []
    gap_s = args.gap_us / 1e6
    replica_gap_s = args.replica_gap_us / 1e6
    receiver_gap_s = args.receiver_gap_us / 1e6
    receiver_order = {
        receiver: index
        for index, receiver in enumerate(
            sorted({tmpl.receiver for tmpl in _flow_templates()})
        )
    }
    for tmpl in _flow_templates():
        rank = _rank_for_order(tmpl.priority_class, args.order)
        receiver_start = args.start + receiver_order[tmpl.receiver] * receiver_gap_s
        class_start = receiver_start + rank * gap_s
        for replica in range(args.flows_per_pair):
            start = class_start + replica * replica_gap_s
            plans.append(
                FlowPlan(
                    receiver=tmpl.receiver,
                    sender=tmpl.sender,
                    priority_class=tmpl.priority_class,
                    group_name=tmpl.name,
                    name=f"{tmpl.name}-f{replica:03d}",
                    start_s=start,
                    priority_rank=rank,
                    credit_path_nodes=tmpl.credit_path_nodes,
                    credit_hops=tmpl.credit_hops,
                )
            )
    template_order = {
        (tmpl.receiver, tmpl.priority_class, tmpl.name): idx
        for idx, tmpl in enumerate(_flow_templates())
    }
    plans.sort(
        key=lambda p: (
            p.start_s,
            p.receiver,
            p.priority_rank,
            template_order[(p.receiver, p.priority_class, p.group_name)],
            p.name,
        )
    )
    return plans


def _build_network(args: argparse.Namespace, flow_size: int):
    slice_us = _slice_us(args.profile)
    net = Toolbox.BaseNetwork(
        name=f"flare_receiver_pair_priority_{args.order}",
        backend="ns3",
        nb_node=13,
        nb_link=4,
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
    circuits = _topology()
    port_map = _directed_port_map(circuits)
    net.deploy_topo(circuits)
    paths, nodes_by_slice = _same_slice_paths(net.get_topo(), port_map)
    _validate_credit_paths(_flow_templates(), nodes_by_slice)
    net.deploy_routing(paths, routing_mode="Per-hop")

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
    plans = _make_flow_plans(args)
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


def _safe_ratio(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


def _call_int(obj: Any, name: str, default: int = 0) -> int:
    method = getattr(obj, name, None)
    if method is None:
        return default
    return int(method())


def _mean(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values if _finite(v)]
    return statistics.mean(vals) if vals else None


def _percentile(values: Iterable[float], pct: float) -> float | None:
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


def _tor_rows(installed_flows) -> List[Dict[str, Any]]:
    if not installed_flows:
        return []
    backend = getattr(installed_flows[0], "_backend", None)
    tor_apps = getattr(backend, "_tor_apps", {})
    rows: List[Dict[str, Any]] = []
    for tor_id in sorted(tor_apps):
        app = tor_apps[tor_id]
        rows.append(
            {
                "tor": int(tor_id),
                "stage": STAGE_TORS.get(int(tor_id), "leaf/sender"),
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
    lost_credit_hop_cost = credit_loss * plan.credit_hops
    completed = _finite(stats.fct_s) and float(stats.fct_s) > 0
    return {
        "flow_id": stats.flow_id,
        "name": plan.name,
        "group_name": plan.group_name,
        "receiver": plan.receiver,
        "sender": plan.sender,
        "priority_class": plan.priority_class,
        "start_s": plan.start_s,
        "priority_rank": plan.priority_rank,
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
        "lost_credit_hop_cost_proxy": lost_credit_hop_cost,
        "credit_tax_proxy": lost_credit_hop_cost,
        "credit_hop_cost_sent": stats.credits_sent * plan.credit_hops,
        "credit_hop_cost_received": stats.credits_received * plan.credit_hops,
        "data_packets_sent": stats.data_packets_sent,
        "data_packets_received": stats.data_packets_received,
        "duplicate_credits": stats.duplicate_credits,
        "duplicate_data": stats.duplicate_data,
        "timeouts": stats.timeouts,
        "retransmissions": stats.retransmissions,
    }


def _group_summary(
    rows: Iterable[Dict[str, Any]],
    key: str,
) -> Dict[str, Dict[str, Any]]:
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
        lost_credit_hop_cost = sum(
            row["lost_credit_hop_cost_proxy"] for row in subset
        )
        out[str(value)] = {
            "flow_count": len(subset),
            "completed_flow_count": len(fcts),
            "credits_sent": credits_sent,
            "credits_received": credits_received,
            "credit_delivery_ratio": _safe_ratio(credits_received, credits_sent),
            "credit_loss_proxy": credits_sent - credits_received,
            "credit_tax": _safe_ratio(credits_sent, data_received),
            "lost_credit_hop_cost_proxy": lost_credit_hop_cost,
            "credit_tax_proxy": lost_credit_hop_cost,
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


def _expected_first_burst_pressure(
    args: argparse.Namespace,
    plans: Sequence[FlowPlan],
    flow_size: int,
) -> Dict[str, Any]:
    by_receiver: DefaultDict[int, Dict[str, int]] = defaultdict(
        lambda: {"short": 0, "long": 0, "total": 0}
    )
    by_edge: DefaultDict[str, Dict[str, int]] = defaultdict(
        lambda: {"short": 0, "long": 0, "total": 0}
    )
    shared_first_hops: DefaultDict[str, Dict[str, int]] = defaultdict(
        lambda: {"short": 0, "long": 0, "total": 0}
    )

    burst_per_flow = min(args.initial_credit, math.ceil(flow_size / args.mtu))
    for plan in plans:
        burst = burst_per_flow
        by_receiver[plan.receiver][plan.priority_class] += burst
        by_receiver[plan.receiver]["total"] += burst

        path_edges = list(zip(plan.credit_path_nodes[:-1], plan.credit_path_nodes[1:]))
        for src, dst in path_edges:
            key = f"{src}->{dst}"
            by_edge[key][plan.priority_class] += burst
            by_edge[key]["total"] += burst
        if path_edges:
            src, dst = path_edges[0]
            key = f"{src}->{dst}"
            shared_first_hops[key][plan.priority_class] += burst
            shared_first_hops[key]["total"] += burst

    return {
        "burst_per_flow": burst_per_flow,
        "initial_credit_burst_total": len(plans) * burst_per_flow,
        "by_receiver": {
            str(receiver): counts
            for receiver, counts in sorted(by_receiver.items())
        },
        "by_credit_edge": {
            edge: counts
            for edge, counts in sorted(
                by_edge.items(),
                key=lambda item: (-item[1]["total"], item[0]),
            )
        },
        "shared_receiver_first_hops": {
            edge: counts
            for edge, counts in sorted(shared_first_hops.items())
            if counts["short"] > 0 and counts["long"] > 0
        },
        "note": (
            "This is offered initial-credit pressure. Actual drops depend on "
            "ns-3 event order, link service release, and multi-hop propagation."
        ),
    }


def _summary(
    args: argparse.Namespace,
    flow_size: int,
    flow_rows: List[Dict[str, Any]],
    tor_rows: List[Dict[str, Any]],
    plans: Sequence[FlowPlan],
) -> Dict[str, Any]:
    total_credits_sent = sum(row["credits_sent"] for row in flow_rows)
    total_credits_received = sum(row["credits_received"] for row in flow_rows)
    total_data_received = sum(row["data_packets_received"] for row in flow_rows)
    total_lost_credit_hop_cost = sum(
        row["lost_credit_hop_cost_proxy"] for row in flow_rows
    )
    class_summary = _group_summary(flow_rows, "priority_class")
    short = class_summary.get("short", {})
    long = class_summary.get("long", {})
    first_burst = len(plans) * min(args.initial_credit, math.ceil(flow_size / args.mtu))
    warning = None
    if first_burst > 0 and total_credits_sent > first_burst * 1.5:
        warning = (
            "credits_sent is well above the initial burst; increase "
            "--refresh-timeout-us or reduce --stop if you want a first-burst-only view."
        )
    return {
        "scenario": "receiver-local-short-long-credit-priority",
        "order": args.order,
        "priority_model": (
            "Each receiver has one short and one long credit class. "
            "The order changes receiver-side initial-credit scheduling; "
            "gap_us=0 keeps both classes in the same burst and relies on "
            "installation/event order to represent priority."
        ),
        "topology": "user static tor10-6-7-8-9-11 with branches to 0,1,2,3,4,5,12",
        "flow_count": len(flow_rows),
        "flow_size": flow_size,
        "flow_packets": math.ceil(flow_size / args.mtu),
        "flows_per_pair": args.flows_per_pair,
        "initial_credit": args.initial_credit,
        "flare_credit_qsize": args.flare_credit_qsize,
        "flare_congestion_threshold": args.flare_congestion_threshold,
        "gap_us": args.gap_us,
        "replica_gap_us": args.replica_gap_us,
        "receiver_gap_us": args.receiver_gap_us,
        "expected_first_burst_pressure": _expected_first_burst_pressure(
            args, plans, flow_size
        ),
        "total_credits_sent": total_credits_sent,
        "total_credits_received": total_credits_received,
        "total_credit_delivery_ratio": _safe_ratio(
            total_credits_received, total_credits_sent
        ),
        "total_credit_loss_proxy": total_credits_sent - total_credits_received,
        "total_credit_tax": _safe_ratio(total_credits_sent, total_data_received),
        "total_lost_credit_hop_cost_proxy": total_lost_credit_hop_cost,
        "total_credit_tax_proxy": total_lost_credit_hop_cost,
        "total_tor_credit_admitted": sum(row["credit_admitted"] for row in tor_rows),
        "total_tor_credit_dropped": sum(row["credit_dropped"] for row in tor_rows),
        "total_tor_credit_wasted": sum(row["credit_wasted"] for row in tor_rows),
        "class_summary": class_summary,
        "receiver_summary": _group_summary(flow_rows, "receiver"),
        "group_summary": _group_summary(flow_rows, "group_name"),
        "tor_summary": {
            str(row["tor"]): row for row in tor_rows
        },
        "short_minus_long_credit_delivery": (
            None
            if short.get("credit_delivery_ratio") is None
            or long.get("credit_delivery_ratio") is None
            else short["credit_delivery_ratio"] - long["credit_delivery_ratio"]
        ),
        "short_minus_long_credit_tax": (
            None
            if short.get("credit_tax") is None or long.get("credit_tax") is None
            else short["credit_tax"] - long["credit_tax"]
        ),
        "short_minus_long_mean_fct_s": (
            None
            if short.get("mean_fct_s") is None or long.get("mean_fct_s") is None
            else short["mean_fct_s"] - long["mean_fct_s"]
        ),
        "short_minus_long_mean_goodput_bps": (
            None
            if short.get("mean_goodput_bps") is None
            or long.get("mean_goodput_bps") is None
            else short["mean_goodput_bps"] - long["mean_goodput_bps"]
        ),
        "warning": warning,
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


def _print_setup(args: argparse.Namespace, plans: Sequence[FlowPlan]) -> None:
    print("=== Receiver Pair Short/Long Credit Priority Experiment ===")
    print(
        f"order={args.order}, profile={args.profile}, "
        f"flows_per_pair={args.flows_per_pair}, "
        f"initial_credit={args.initial_credit}, qsize={args.flare_credit_qsize}"
    )
    print("Topology: tor10--tor6--tor7--tor8--tor9--tor11 plus branches")
    print("Receiver flow groups:")
    seen = set()
    for plan in plans:
        if plan.group_name in seen:
            continue
        seen.add(plan.group_name)
        print(
            f"  {plan.group_name}: data {plan.sender}->{plan.receiver}, "
            f"credit {'->'.join(str(n) for n in plan.credit_path_nodes)}, "
            f"class={plan.priority_class}, hops={plan.credit_hops}"
        )
    print(f"Installed flows: {len(plans)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--order",
        choices=["long-first", "short-first", "simultaneous"],
        default="long-first",
    )
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--flows-per-pair", type=int, default=8)
    parser.add_argument("--flow-size", type=int, default=0,
                        help="0 means initial_credit * mtu")
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=16)
    parser.add_argument("--start", type=float, default=0.000105)
    parser.add_argument("--gap-us", type=float, default=0.0)
    parser.add_argument("--replica-gap-us", type=float, default=0.0)
    parser.add_argument("--receiver-gap-us", type=float, default=0.0)
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
        default=Path("results/flare_receiver_pair_priority.json"),
    )
    args = parser.parse_args(argv)
    if args.flows_per_pair <= 0:
        raise ValueError("--flows-per-pair must be positive")
    if args.initial_credit <= 0:
        raise ValueError("--initial-credit must be positive")
    if args.flow_size < 0:
        raise ValueError("--flow-size must be non-negative")

    flow_size = args.flow_size or args.initial_credit * args.mtu
    net, installed, plans, nodes_by_slice = _build_network(args, flow_size)
    _print_setup(args, plans)
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
    flow_rows.sort(key=lambda row: (row["start_s"], row["receiver"], row["priority_rank"], row["name"]))

    tor_rows = _tor_rows(installed)
    payload = {
        "experiment": "flare_receiver_pair_priority",
        "parameters": dict(vars(args), resolved_flow_size=flow_size),
        "credit_paths": {
            tmpl.name: list(tmpl.credit_path_nodes) for tmpl in _flow_templates()
        },
        "routes": {
            f"{src}->{dst}@{ts}": list(path)
            for (src, dst, ts), path in sorted(nodes_by_slice.items())
        },
        "summary": _summary(args, flow_size, flow_rows, tor_rows, plans),
        "flows": flow_rows,
        "tors": tor_rows,
    }
    _write_json(args.output, payload)
    _write_csv(args.output.with_suffix(".flows.csv"), flow_rows)
    _write_csv(args.output.with_suffix(".tors.csv"), tor_rows)

    print("\nSummary:")
    print(json.dumps(_json_safe(payload["summary"]), indent=2, sort_keys=True))
    print(f"\nWrote {args.output}")
    print(f"Wrote {args.output.with_suffix('.flows.csv')}")
    print(f"Wrote {args.output.with_suffix('.tors.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
