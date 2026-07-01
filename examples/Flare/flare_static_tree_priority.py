"""Static tree experiment for Flare credit hop-priority.

This experiment keeps the optical topology static and creates many receiver
flows with different reverse-credit path lengths.  The sender is ToR 9.

Topology:

    r0   r1
      \\ /
       6 -- 7 -- 8 -- 9(sender)
        / \\   /|\\
        r2  r3 r4 r5 r8

Credit paths:

    h=4: 0/1 -> 6 -> 7 -> 8 -> 9
    h=3: 2/3 -> 7 -> 8 -> 9
    h=2: 4/5 -> 8 -> 9
    h=1: 8   -> 9

The important point is not just raw drop count.  Hop-priority may intentionally
drop long credits earlier, which can reduce late-stage drops and wasted
credit-hop work while improving short-path delivery.
"""

from __future__ import annotations

import argparse
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


SENDER = 9
RECEIVERS = (0, 1, 2, 3, 4, 5, 8)
STAGE_TORS = {
    6: "far-stage 6->7",
    7: "mid-stage 7->8",
    8: "final-stage 8->9",
}


@dataclass(frozen=True)
class FlowPlan:
    name: str
    src: int
    dst: int
    start_s: float
    credit_path_nodes: Tuple[int, ...]

    @property
    def credit_hops(self) -> int:
        return len(self.credit_path_nodes) - 1

    @property
    def hop_class(self) -> str:
        if self.credit_hops <= 1:
            return "very-short"
        if self.credit_hops == 2:
            return "short"
        if self.credit_hops == 3:
            return "medium"
        return "long"


def _slice_us(profile: str) -> int:
    return 55 if profile == "55us" else 15


def _safe_ratio(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


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


def _topology() -> List[List[int]]:
    """Return one static slice for the receiver tree.

    Circuit format: [time_slice, node1, node2, port1, port2].
    ``nb_link`` must be at least 4 because ToR 7 has degree 4.
    """
    return [
        [0, 0, 6, 0, 0],
        [0, 1, 6, 0, 1],
        [0, 6, 7, 2, 0],
        [0, 2, 7, 0, 1],
        [0, 3, 7, 0, 2],
        [0, 7, 8, 3, 0],
        [0, 4, 8, 0, 1],
        [0, 5, 8, 0, 2],
        [0, 8, 9, 3, 0],
    ]


def _same_slice_paths(
    slice_to_topo: Mapping[int, nx.Graph],
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
                    edge = graph[cur_node][next_node]
                    steps.append(
                        Step(
                            cur_node=int(cur_node),
                            step_type="port",
                            send_port=int(edge["port1"]),
                            send_ts=int(ts),
                            send_node=int(next_node),
                        )
                    )
                if not steps:
                    continue
                paths.append(
                    OpticalPath(src=int(src), arrival_ts=int(ts),
                                dst=int(dst), steps=steps)
                )
                nodes_by_slice[(int(src), int(dst), int(ts))] = tuple(
                    int(node) for node in path_nodes
                )
    return paths, nodes_by_slice


def _rank_for_order(credit_hops: int, order: str) -> int:
    if order == "long-first":
        return 4 - credit_hops
    if order == "short-first":
        return credit_hops - 1
    if order == "simultaneous":
        return 0
    raise ValueError(f"unknown order {order!r}")


def _flow_plans(
    args: argparse.Namespace,
    nodes_by_slice: Mapping[Tuple[int, int, int], Tuple[int, ...]],
) -> List[FlowPlan]:
    plans: List[FlowPlan] = []
    for receiver in RECEIVERS:
        credit_path = nodes_by_slice.get((receiver, SENDER, 0))
        if not credit_path:
            raise RuntimeError(f"missing credit path {receiver}->{SENDER}@0")
        credit_hops = len(credit_path) - 1
        rank = _rank_for_order(credit_hops, args.order)
        base_start = args.start + rank * args.gap_us / 1e6
        for replica in range(args.flows_per_receiver):
            start = base_start + replica * args.replica_gap_us / 1e6
            plans.append(
                FlowPlan(
                    name=f"h{credit_hops}-r{receiver}-f{replica}",
                    src=SENDER,
                    dst=receiver,
                    start_s=start,
                    credit_path_nodes=credit_path,
                )
            )
    plans.sort(key=lambda p: (p.start_s, -p.credit_hops, p.dst, p.name))
    return plans


def _validate_credit_paths(
    plans: Sequence[FlowPlan],
    nodes_by_slice: Mapping[Tuple[int, int, int], Tuple[int, ...]],
) -> None:
    errors = []
    for plan in plans:
        actual = nodes_by_slice.get((plan.dst, plan.src, 0))
        if actual != plan.credit_path_nodes:
            errors.append(
                f"{plan.name}: expected {plan.credit_path_nodes}, got {actual}"
            )
    if errors:
        raise RuntimeError("credit path validation failed:\n  " + "\n  ".join(errors))


def _build_network(args: argparse.Namespace, policy: str, flow_size: int):
    slice_us = _slice_us(args.profile)
    congestion_threshold = (
        args.priority_congestion_threshold
        if policy == "hop-priority"
        else args.baseline_congestion_threshold
    )
    net = Toolbox.BaseNetwork(
        name=f"flare_static_tree_priority_{policy}_{args.order}",
        backend="ns3",
        nb_node=10,
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
        flare_congestion_threshold_percent=congestion_threshold,
        flare_tentative_threshold_percent=100,
    )
    net.deploy_topo(_topology())
    paths, nodes_by_slice = _same_slice_paths(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")
    plans = _flow_plans(args, nodes_by_slice)
    _validate_credit_paths(plans, nodes_by_slice)

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
        congestion_threshold_percent=congestion_threshold,
        tentative_threshold_percent=100,
    )
    gen = net.flare_traffic(config=config)
    for plan in plans:
        gen.flow(
            plan.src,
            plan.dst,
            flow_size,
            start_s=plan.start_s,
            duration_s=args.duration,
            packet_size_bytes=args.mtu,
            name=plan.name,
        )
    return net, gen.install(), plans, nodes_by_slice


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
                "credit_stage": STAGE_TORS.get(int(tor_id), "leaf/sender"),
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
            }
        )
    return rows


def _flow_row(plan: FlowPlan, stats: FlareStats) -> Dict[str, Any]:
    return {
        "name": plan.name,
        "hop_class": plan.hop_class,
        "credit_hops": plan.credit_hops,
        "src": stats.src,
        "dst": stats.dst,
        "start_s": plan.start_s,
        "credit_path": "->".join(str(node) for node in plan.credit_path_nodes),
        "fct_s": stats.fct_s,
        "completed": bool(_finite(stats.fct_s) and float(stats.fct_s) > 0),
        "credits_sent": stats.credits_sent,
        "credits_received": stats.credits_received,
        "credit_loss_proxy": stats.credits_sent - stats.credits_received,
        "credit_hop_cost_sent": stats.credits_sent * plan.credit_hops,
        "credit_hop_cost_received": stats.credits_received * plan.credit_hops,
        "credit_hop_loss_cost": (
            (stats.credits_sent - stats.credits_received) * plan.credit_hops
        ),
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
        out[str(value)] = {
            "flow_count": len(subset),
            "completed_flow_count": len(fcts),
            "mean_fct_s": statistics.mean(fcts) if fcts else None,
            "credits_sent": credits_sent,
            "credits_received": credits_received,
            "credit_delivery_ratio": _safe_ratio(credits_received, credits_sent),
            "credit_loss_proxy": credits_sent - credits_received,
            "credit_hop_cost_sent": sum(row["credit_hop_cost_sent"] for row in subset),
            "credit_hop_cost_received": sum(
                row["credit_hop_cost_received"] for row in subset
            ),
            "credit_hop_loss_cost": sum(
                row["credit_hop_loss_cost"] for row in subset
            ),
            "data_packets_sent": sum(row["data_packets_sent"] for row in subset),
            "data_packets_received": sum(row["data_packets_received"] for row in subset),
        }
    return out


def _stage_summary(tor_rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for tor_id, stage_name in STAGE_TORS.items():
        row = next((item for item in tor_rows if item["tor"] == tor_id), None)
        if row is None:
            continue
        out[stage_name] = {
            "tor": tor_id,
            "credit_admitted": row["credit_admitted"],
            "credit_dropped": row["credit_dropped"],
            "credit_wasted": row["credit_wasted"],
            "overflow_drops": row["overflow_drops"],
            "all_drops": row["drops"],
        }
    return out


def _expected_pressure(args: argparse.Namespace, plans: Sequence[FlowPlan]) -> Dict[str, Any]:
    by_stage = {stage: 0 for stage in STAGE_TORS.values()}
    by_hops: DefaultDict[int, int] = defaultdict(int)
    for plan in plans:
        burst = args.initial_credit
        by_hops[plan.credit_hops] += burst
        path_edges = list(zip(plan.credit_path_nodes[:-1], plan.credit_path_nodes[1:]))
        if (6, 7) in path_edges:
            by_stage["far-stage 6->7"] += burst
        if (7, 8) in path_edges:
            by_stage["mid-stage 7->8"] += burst
        if (8, 9) in path_edges:
            by_stage["final-stage 8->9"] += burst
    return {
        "initial_credit_burst_total": len(plans) * args.initial_credit,
        "by_credit_hops": {str(k): v for k, v in sorted(by_hops.items())},
        "by_stage": by_stage,
        "note": (
            "This is offered first-burst pressure, not an exact queue model; "
            "per-slice drains and multi-hop propagation still occur in ns-3."
        ),
    }


def _summarize_case(
    args: argparse.Namespace,
    policy: str,
    flow_size: int,
    flow_rows: List[Dict[str, Any]],
    tor_rows: List[Dict[str, Any]],
    plans: Sequence[FlowPlan],
) -> Dict[str, Any]:
    total_credits_sent = sum(row["credits_sent"] for row in flow_rows)
    total_credits_received = sum(row["credits_received"] for row in flow_rows)
    total_credit_hop_cost_received = sum(
        row["credit_hop_cost_received"] for row in flow_rows
    )
    total_tor_credit_admitted = sum(row["credit_admitted"] for row in tor_rows)
    useful_admitted_hop_work = total_credit_hop_cost_received
    wasted_admitted_hop_work = max(
        0,
        total_tor_credit_admitted - useful_admitted_hop_work,
    )
    warning = None
    first_burst = len(flow_rows) * args.initial_credit
    if total_credits_sent > first_burst * 2:
        warning = (
            "credits_sent greatly exceeds the first burst; shorten --stop or "
            "increase --refresh-timeout-us to focus on first-burst behavior."
        )
    return {
        "policy": policy,
        "order": args.order,
        "flow_count": len(flow_rows),
        "flow_size": flow_size,
        "flow_packets": math.ceil(flow_size / args.mtu),
        "initial_credit": args.initial_credit,
        "flare_credit_qsize": args.flare_credit_qsize,
        "congestion_threshold": (
            args.priority_congestion_threshold
            if policy == "hop-priority"
            else args.baseline_congestion_threshold
        ),
        "expected_pressure": _expected_pressure(args, plans),
        "total_credits_sent": total_credits_sent,
        "total_credits_received": total_credits_received,
        "total_credit_delivery_ratio": _safe_ratio(
            total_credits_received, total_credits_sent
        ),
        "total_credit_loss_proxy": total_credits_sent - total_credits_received,
        "total_credit_hop_loss_cost": sum(
            row["credit_hop_loss_cost"] for row in flow_rows
        ),
        "total_tor_credit_admitted": total_tor_credit_admitted,
        "total_tor_credit_dropped": sum(row["credit_dropped"] for row in tor_rows),
        "total_tor_credit_wasted": sum(row["credit_wasted"] for row in tor_rows),
        "total_overflow_drops": sum(row["overflow_drops"] for row in tor_rows),
        "useful_admitted_credit_hop_work": useful_admitted_hop_work,
        "wasted_admitted_credit_hop_work": wasted_admitted_hop_work,
        "wasted_admitted_credit_hop_fraction": _safe_ratio(
            wasted_admitted_hop_work, total_tor_credit_admitted
        ),
        "by_class": _group_summary(flow_rows, "hop_class"),
        "by_credit_hops": _group_summary(flow_rows, "credit_hops"),
        "by_stage": _stage_summary(tor_rows),
        "warning": warning,
    }


def _run_case(args: argparse.Namespace, policy: str, flow_size: int) -> Dict[str, Any]:
    print(f"\n=== Running policy={policy}, order={args.order} ===", flush=True)
    net, installed, plans, nodes_by_slice = _build_network(args, policy, flow_size)
    print("Topology: receivers 0/1 -> 6 -> 7 -> 8 -> 9(sender)")
    print("          receivers 2/3 -> 7 -> 8 -> 9")
    print("          receivers 4/5 -> 8 -> 9; receiver 8 -> 9")
    print("Credit classes:")
    seen = set()
    for plan in plans:
        key = (plan.credit_hops, plan.dst)
        if key in seen:
            continue
        seen.add(key)
        print(
            f"  dst={plan.dst}: {'->'.join(map(str, plan.credit_path_nodes))} "
            f"(h={plan.credit_hops}, class={plan.hop_class})"
        )

    net.start()

    flow_rows: List[Dict[str, Any]] = []
    for plan, installed_flow in zip(plans, installed):
        stats = installed_flow.stats()
        if stats is not None:
            flow_rows.append(_flow_row(plan, stats))
    tor_rows = _tor_rows(installed)
    summary = _summarize_case(args, policy, flow_size, flow_rows, tor_rows, plans)
    return {
        "summary": summary,
        "flows": flow_rows,
        "tors": tor_rows,
        "routes": {
            f"{src}->{dst}@{ts}": list(path)
            for (src, dst, ts), path in sorted(nodes_by_slice.items())
        },
    }


def _compare_cases(cases: Mapping[str, Dict[str, Any]]) -> Dict[str, Any]:
    if "no-priority" not in cases or "hop-priority" not in cases:
        return {}
    base = cases["no-priority"]["summary"]
    prio = cases["hop-priority"]["summary"]

    def get_class(summary, class_name, field):
        return summary.get("by_class", {}).get(class_name, {}).get(field)

    def get_stage(summary, stage_name, field):
        return summary.get("by_stage", {}).get(stage_name, {}).get(field)

    short_names = ("very-short", "short")
    short_base_sent = sum(
        get_class(base, cls, "credits_sent") or 0 for cls in short_names
    )
    short_prio_sent = sum(
        get_class(prio, cls, "credits_sent") or 0 for cls in short_names
    )
    short_base_recv = sum(
        get_class(base, cls, "credits_received") or 0 for cls in short_names
    )
    short_prio_recv = sum(
        get_class(prio, cls, "credits_received") or 0 for cls in short_names
    )
    base_short_ratio = _safe_ratio(short_base_recv, short_base_sent)
    prio_short_ratio = _safe_ratio(short_prio_recv, short_prio_sent)
    base_waste = base["wasted_admitted_credit_hop_work"]
    prio_waste = prio["wasted_admitted_credit_hop_work"]
    base_late_drop = get_stage(base, "final-stage 8->9", "credit_dropped") or 0
    prio_late_drop = get_stage(prio, "final-stage 8->9", "credit_dropped") or 0

    short_delta = (
        None if base_short_ratio is None or prio_short_ratio is None
        else prio_short_ratio - base_short_ratio
    )
    waste_reduction = (
        None if base_waste == 0 else (base_waste - prio_waste) / base_waste
    )
    late_drop_reduction = (
        None if base_late_drop == 0
        else (base_late_drop - prio_late_drop) / base_late_drop
    )

    intended_effect = (
        short_delta is not None
        and short_delta > 0
        and (
            (waste_reduction is not None and waste_reduction > 0)
            or (late_drop_reduction is not None and late_drop_reduction > 0)
        )
    )
    return {
        "short_credit_delivery_delta": short_delta,
        "short_credits_received_delta": short_prio_recv - short_base_recv,
        "raw_credit_drop_delta": (
            prio["total_tor_credit_dropped"] - base["total_tor_credit_dropped"]
        ),
        "final_stage_credit_drop_delta": prio_late_drop - base_late_drop,
        "final_stage_credit_drop_reduction": late_drop_reduction,
        "wasted_admitted_credit_hop_work_delta": prio_waste - base_waste,
        "wasted_admitted_credit_hop_work_reduction": waste_reduction,
        "intended_effect_observed": intended_effect,
        "interpretation": (
            "Hop priority improved short-path credit delivery and reduced "
            "late-stage drop or wasted admitted credit-hop work."
            if intended_effect
            else "This parameter set did not cleanly expose the intended effect; "
                 "try slightly larger qsize or a smaller long-first gap."
        ),
        "metric_note": (
            "Raw credit drops can rise under priority because long credits may "
            "be filtered earlier. Focus on short delivery, final-stage drops, "
            "and credit-hop waste."
        ),
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        choices=["both", "no-priority", "hop-priority"],
        default="both",
        help="no-priority uses qsize only; hop-priority applies P(h)",
    )
    parser.add_argument(
        "--order",
        choices=["long-first", "short-first", "simultaneous"],
        default="long-first",
    )
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--flows-per-receiver", type=int, default=8)
    parser.add_argument("--flow-size", type=int, default=0,
                        help="0 means initial_credit * mtu")
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=4)
    parser.add_argument("--start", type=float, default=0.000005)
    parser.add_argument("--gap-us", type=float, default=2.0)
    parser.add_argument("--replica-gap-us", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=0.00011)
    parser.add_argument("--stop", type=float, default=0.00018)
    parser.add_argument("--refresh-timeout-us", type=float, default=200.0)
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    parser.add_argument("--host-delay-us", type=int, default=1)
    parser.add_argument("--ocs-delay-us", type=int, default=1)
    parser.add_argument("--guardband-us", type=int, default=0)
    parser.add_argument("--target-loss", type=float, default=0.1)
    parser.add_argument("--flare-credit-qsize", type=int, default=16)
    parser.add_argument("--flare-shaping-thresh", type=int, default=4096)
    parser.add_argument("--flare-aeolus-thresh", type=int, default=4096)
    parser.add_argument("--baseline-congestion-threshold", type=int, default=100)
    parser.add_argument("--priority-congestion-threshold", type=int, default=0)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/flare_static_tree_priority.json"),
    )
    args = parser.parse_args(argv)

    if args.flows_per_receiver <= 0:
        raise ValueError("--flows-per-receiver must be positive")
    if args.initial_credit <= 0:
        raise ValueError("--initial-credit must be positive")
    if args.flare_credit_qsize <= 0:
        raise ValueError("--flare-credit-qsize must be positive")
    if args.flow_size < 0:
        raise ValueError("--flow-size must be non-negative")

    flow_size = args.flow_size or args.initial_credit * args.mtu
    slice_us = _slice_us(args.profile)
    planned_max_start_us = args.start * 1e6 + max(
        0.0,
        _rank_for_order(1, args.order) * args.gap_us,
        _rank_for_order(2, args.order) * args.gap_us,
        _rank_for_order(3, args.order) * args.gap_us,
        _rank_for_order(4, args.order) * args.gap_us,
    ) + max(0, args.flows_per_receiver - 1) * args.replica_gap_us
    if planned_max_start_us >= slice_us:
        raise ValueError(
            f"all flow start times must stay within slice 0 for this static "
            f"topology; got max start {planned_max_start_us:.3f}us with "
            f"slice duration {slice_us}us. Reduce --start/--gap-us/"
            f"--replica-gap-us or use a larger custom topology."
        )
    policies = (
        ["no-priority", "hop-priority"]
        if args.policy == "both"
        else [args.policy]
    )
    cases = {
        policy: _run_case(args, policy, flow_size)
        for policy in policies
    }
    comparison = _compare_cases(cases)
    payload = {
        "experiment": "flare_static_tree_priority",
        "parameters": {
            **{key: _json_safe(value) for key, value in vars(args).items()},
            "resolved_flow_size": flow_size,
        },
        "design": {
            "topology": "static receiver tree feeding sender 9",
            "sender": SENDER,
            "receivers": list(RECEIVERS),
            "trunk": "6--7--8--9",
            "credit_paths": {
                "receiver0/1": "0/1->6->7->8->9, h=4",
                "receiver2/3": "2/3->7->8->9, h=3",
                "receiver4/5": "4/5->8->9, h=2",
                "receiver8": "8->9, h=1",
            },
            "bottleneck_stages": list(STAGE_TORS.values()),
        },
        "comparison": comparison,
        "cases": cases,
    }
    _write_json(args.output, payload)

    print("\nComparison:")
    print(json.dumps(_json_safe(comparison), indent=2, sort_keys=True))
    for policy, case in cases.items():
        print(f"\nSummary for {policy}:")
        print(json.dumps(_json_safe(case["summary"]), indent=2, sort_keys=True))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
