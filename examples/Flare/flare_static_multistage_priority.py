"""Static multistage experiment for Flare path-length credit priority.

The static topology is a chain:

    receiver0 -- receiver1 -- receiver2 -- receiver3 -- receiver4 -- sender5

Data flows all go from sender5 to the receivers.  Credits travel in the
opposite direction, so receiver credits have path lengths from 1 to 5 hops.
Every longer credit traverses more bottleneck stages before reaching sender5:

    h=1: 4 -> 5
    h=2: 3 -> 4 -> 5
    h=3: 2 -> 3 -> 4 -> 5
    h=4: 1 -> 2 -> 3 -> 4 -> 5
    h=5: 0 -> 1 -> 2 -> 3 -> 4 -> 5

By default the workload starts long-hop receivers first.  This is deliberately
adversarial: without hop-priority, long credits can consume shared downstream
credit queue slots before short-hop credits arrive.  With Flare hop priority
enabled, congested ToRs apply P(h) = (1/2)^(h-1), which should protect short
credits and reduce wasted admitted credit-hop work.

Example:

    PYTHONPATH=$PWD python3 examples/Flare/flare_static_multistage_priority.py \
        --profile 15us \
        --flows-per-receiver 4 \
        --initial-credit 4 \
        --flare-credit-qsize 16 \
        --policy both \
        --output results/flare_static_multistage_priority.json
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
        if self.credit_hops <= 2:
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


def _chain_topology() -> List[List[int]]:
    """Return one static chain slice for nodes 0--1--2--3--4--5."""
    return [
        [0, 0, 1, 0, 0],
        [0, 1, 2, 1, 0],
        [0, 2, 3, 1, 0],
        [0, 3, 4, 1, 0],
        [0, 4, 5, 1, 0],
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
        return 5 - credit_hops
    if order == "short-first":
        return credit_hops - 1
    if order == "simultaneous":
        return 0
    raise ValueError(f"unknown order {order!r}")


def _flow_plans(args: argparse.Namespace, nodes_by_slice) -> List[FlowPlan]:
    plans: List[FlowPlan] = []
    sender = 5
    for receiver in range(5):
        credit_path = nodes_by_slice.get((receiver, sender, 0))
        if not credit_path:
            raise RuntimeError(f"missing credit path {receiver}->{sender}@0")
        credit_hops = len(credit_path) - 1
        rank = _rank_for_order(credit_hops, args.order)
        base_start = args.start + rank * args.gap_us / 1e6
        for replica in range(args.flows_per_receiver):
            start = base_start + replica * args.replica_gap_us / 1e6
            plans.append(
                FlowPlan(
                    name=f"h{credit_hops}-r{receiver}-f{replica}",
                    src=sender,
                    dst=receiver,
                    start_s=start,
                    credit_path_nodes=credit_path,
                )
            )
    return plans


def _build_network(args: argparse.Namespace, policy: str, flow_size: int):
    slice_us = _slice_us(args.profile)
    congestion_threshold = 0 if policy == "hop-priority" else 100
    net = Toolbox.BaseNetwork(
        name=f"flare_static_multistage_{policy}_{args.order}",
        backend="ns3",
        nb_node=6,
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
        flare_congestion_threshold_percent=congestion_threshold,
        flare_tentative_threshold_percent=100,
    )
    net.deploy_topo(_chain_topology())
    paths, nodes_by_slice = _same_slice_paths(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")
    plans = _flow_plans(args, nodes_by_slice)

    config = FlareConfig.from_profile(
        args.profile,
        credit_qsize_pkts=args.flare_credit_qsize,
        shaping_thresh_pkts=args.flare_shaping_thresh,
        aeolus_thresh_pkts=args.flare_aeolus_thresh,
        w_init=1.0,
        target_loss=args.target_loss,
        mtu_bytes=args.mtu,
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


def _tor_rows(installed_flows) -> List[Dict[str, int]]:
    if not installed_flows:
        return []
    backend = getattr(installed_flows[0], "_backend", None)
    tor_apps = getattr(backend, "_tor_apps", {})
    rows: List[Dict[str, int]] = []
    for tor_id in sorted(tor_apps):
        app = tor_apps[tor_id]
        rows.append(
            {
                "tor": int(tor_id),
                "credit_stage": f"{tor_id}->{tor_id + 1}" if tor_id < 5 else "sender",
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
        data_received = sum(row["data_packets_received"] for row in subset)
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
            "data_packets_received": data_received,
        }
    return out


def _summarize_case(
    args: argparse.Namespace,
    policy: str,
    flow_size: int,
    flow_rows: List[Dict[str, Any]],
    tor_rows: List[Dict[str, int]],
) -> Dict[str, Any]:
    total_credits_sent = sum(row["credits_sent"] for row in flow_rows)
    total_credits_received = sum(row["credits_received"] for row in flow_rows)
    total_credit_hop_cost_received = sum(
        row["credit_hop_cost_received"] for row in flow_rows
    )
    total_tor_credit_admitted = sum(row["credit_admitted"] for row in tor_rows)
    total_tor_credit_dropped = sum(row["credit_dropped"] for row in tor_rows)
    useful_admitted_hop_work = total_credit_hop_cost_received
    wasted_admitted_hop_work = max(
        0,
        total_tor_credit_admitted - useful_admitted_hop_work,
    )
    return {
        "policy": policy,
        "order": args.order,
        "flow_count": len(flow_rows),
        "flow_size": flow_size,
        "flow_packets": math.ceil(flow_size / args.mtu),
        "initial_credit": args.initial_credit,
        "flare_credit_qsize": args.flare_credit_qsize,
        "congestion_threshold": 0 if policy == "hop-priority" else 100,
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
        "total_tor_credit_dropped": total_tor_credit_dropped,
        "total_tor_credit_wasted": sum(row["credit_wasted"] for row in tor_rows),
        "total_overflow_drops": sum(row["overflow_drops"] for row in tor_rows),
        "useful_admitted_credit_hop_work": useful_admitted_hop_work,
        "wasted_admitted_credit_hop_work": wasted_admitted_hop_work,
        "wasted_admitted_credit_hop_fraction": _safe_ratio(
            wasted_admitted_hop_work, total_tor_credit_admitted
        ),
        "by_class": _group_summary(flow_rows, "hop_class"),
        "by_credit_hops": _group_summary(flow_rows, "credit_hops"),
    }


def _run_case(args: argparse.Namespace, policy: str, flow_size: int) -> Dict[str, Any]:
    print(f"\n=== Running policy={policy}, order={args.order} ===", flush=True)
    net, installed, plans, nodes_by_slice = _build_network(args, policy, flow_size)
    print("Topology: 0--1--2--3--4--5, sender=5")
    print("Receivers: 0,1,2,3,4")
    print("Credit paths:")
    for plan in plans[:5]:
        print(
            f"  {plan.name}: {'->'.join(str(n) for n in plan.credit_path_nodes)} "
            f"(h={plan.credit_hops}, class={plan.hop_class})"
        )
    if len(plans) > 5:
        print(f"  ... {len(plans) - 5} more flows")

    net.start()

    flow_rows: List[Dict[str, Any]] = []
    for plan, installed_flow in zip(plans, installed):
        stats = installed_flow.stats()
        if stats is not None:
            flow_rows.append(_flow_row(plan, stats))
    tor_rows = _tor_rows(installed)
    summary = _summarize_case(args, policy, flow_size, flow_rows, tor_rows)
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

    base_short = get_class(base, "short", "credit_delivery_ratio")
    prio_short = get_class(prio, "short", "credit_delivery_ratio")
    base_long = get_class(base, "long", "credit_delivery_ratio")
    prio_long = get_class(prio, "long", "credit_delivery_ratio")
    base_waste = base["wasted_admitted_credit_hop_work"]
    prio_waste = prio["wasted_admitted_credit_hop_work"]
    return {
        "short_credit_delivery_delta": (
            None if base_short is None or prio_short is None else prio_short - base_short
        ),
        "long_credit_delivery_delta": (
            None if base_long is None or prio_long is None else prio_long - base_long
        ),
        "raw_credit_drop_delta": (
            prio["total_tor_credit_dropped"] - base["total_tor_credit_dropped"]
        ),
        "wasted_admitted_credit_hop_work_delta": prio_waste - base_waste,
        "wasted_admitted_credit_hop_work_reduction": (
            None if base_waste == 0 else (base_waste - prio_waste) / base_waste
        ),
        "interpretation": (
            "Hop priority helped short-path credit delivery and reduced wasted "
            "admitted credit-hop work."
            if (
                base_short is not None
                and prio_short is not None
                and prio_short >= base_short
                and prio_waste <= base_waste
            )
            else "Inspect by_class and per-ToR stage drops; this parameter set did not cleanly show both effects."
        ),
        "note": (
            "Raw drop count can increase under hop priority because long credits "
            "are intentionally filtered earlier; the main target is lower late "
            "drop / lower wasted credit-hop work plus better short-path delivery."
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
        help="no-priority uses qsize only; hop-priority applies P(h) at congestion",
    )
    parser.add_argument(
        "--order",
        choices=["long-first", "short-first", "simultaneous"],
        default="long-first",
    )
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--flows-per-receiver", type=int, default=4)
    parser.add_argument("--flow-size", type=int, default=0,
                        help="0 means initial_credit * mtu")
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=4)
    parser.add_argument("--start", type=float, default=0.000105)
    parser.add_argument("--gap-us", type=float, default=3.0)
    parser.add_argument("--replica-gap-us", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=0.00008)
    parser.add_argument("--stop", type=float, default=0.00019)
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    parser.add_argument("--host-delay-us", type=int, default=1)
    parser.add_argument("--ocs-delay-us", type=int, default=1)
    parser.add_argument("--guardband-us", type=int, default=0)
    parser.add_argument("--target-loss", type=float, default=0.1)
    parser.add_argument("--flare-credit-qsize", type=int, default=16)
    parser.add_argument("--flare-shaping-thresh", type=int, default=4096)
    parser.add_argument("--flare-aeolus-thresh", type=int, default=4096)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/flare_static_multistage_priority.json"),
    )
    args = parser.parse_args(argv)
    if args.flows_per_receiver <= 0:
        raise ValueError("--flows-per-receiver must be positive")
    if args.initial_credit <= 0:
        raise ValueError("--initial-credit must be positive")
    if args.flow_size < 0:
        raise ValueError("--flow-size must be non-negative")

    flow_size = args.flow_size or args.initial_credit * args.mtu
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
        "experiment": "flare_static_multistage_priority",
        "parameters": {
            **{key: _json_safe(value) for key, value in vars(args).items()},
            "resolved_flow_size": flow_size,
        },
        "design": {
            "topology": "0--1--2--3--4--5 static chain",
            "sender": 5,
            "receivers": [0, 1, 2, 3, 4],
            "credit_hops": {
                "receiver4": 1,
                "receiver3": 2,
                "receiver2": 3,
                "receiver1": 4,
                "receiver0": 5,
            },
            "bottlenecks": ["0->1", "1->2", "2->3", "3->4", "4->5"],
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
