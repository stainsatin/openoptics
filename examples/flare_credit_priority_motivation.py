"""Fixed-chain Flare credit priority motivation experiment.

This script is intentionally small and deterministic.  It builds one optical
topology and one fixed workload so the only thing that changes between runs is
the order in which near/short-credit and far/long-credit flows issue credits.

Conceptual topology, using paper-style labels:

    1 -- 2 -- 3 -- 4

OpenOptics uses 0-based ToR ids, so the ns-3 topology is:

    0 -- 1 -- 2 -- 3

Credit travels from receiver to sender.  The fixed workload is:

    short-r0-near: data 1 -> 0, credit 0 -> 1
    long-r0-far:   data 3 -> 0, credit 0 -> 1 -> 2 -> 3
    short-r1-near: data 2 -> 1, credit 1 -> 2
    long-r1-far:   data 3 -> 1, credit 1 -> 2 -> 3

The intended bottleneck is tor1's uplink to tor2.  The short-r1-near credit
and both long credits contend there; if the long-credit flows are launched
first they consume bottleneck admission slots before the short-r1-near credit
arrives.  If short credits are launched first, the near flow can reserve some
of those slots before the long paths consume multiple downstream resources.

Recommended first run:

    PYTHONPATH=$PWD python3 examples/flare_credit_priority_motivation.py \
        --order long-first \
        --profile 15us \
        --flows-per-pair 16 \
        --initial-credit 8 \
        --flare-credit-qsize 8 \
        --flare-congestion-threshold 0 \
        --duration 0.00015 \
        --stop 0.00035 \
        --output results/manual_chain4_long_first

Then compare with ``--order short-first`` and the same other arguments.
Keep ``--stop`` short while validating the motivation experiment: Flare's host
model refreshes credits every retransmission timeout, so multi-second runs can
mostly measure repeated refresh loss rather than the first ordering decision.
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
from typing import DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCRIPT_PATH = Path(__file__).resolve()
CANDIDATE_ROOTS = [SCRIPT_PATH.parents[1], Path.cwd()]
for root in CANDIDATE_ROOTS:
    if (root / "openoptics").exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
        break

import networkx as nx

from openoptics import Toolbox
from openoptics.TimeFlowTable import Path as OpticalPath
from openoptics.TimeFlowTable import Step
from openoptics.backends.ns3.traffic import FlareConfig, FlareStats


@dataclass(frozen=True)
class FlowTemplate:
    name: str
    hop_class: str
    src: int
    dst: int
    credit_path_nodes: Tuple[int, ...]
    bottleneck_key: Optional[Tuple[int, int]]

    @property
    def credit_hops(self) -> int:
        return len(self.credit_path_nodes) - 1


@dataclass(frozen=True)
class FlowPlan:
    name: str
    group_name: str
    hop_class: str
    src: int
    dst: int
    start_s: float
    credit_path_nodes: Tuple[int, ...]
    bottleneck_key: Optional[Tuple[int, int]]
    credit_hops: int
    expected_credit_burst: int
    expected_bottleneck_admitted: int
    expected_bottleneck_dropped: int


def _slice_us(profile: str) -> int:
    return 55 if profile == "55us" else 15


def _chain4_topology() -> List[List[int]]:
    """Return one static optical slice: 0--1--2--3.

    Circuit format is [time_slice, node1, node2, port1, port2].  The middle
    ToRs need two optical ports, so the network must be built with nb_link=2.
    """
    return [
        [0, 0, 1, 0, 0],
        [0, 1, 2, 1, 0],
        [0, 2, 3, 1, 0],
    ]


def _flow_templates() -> List[FlowTemplate]:
    return [
        FlowTemplate(
            name="short-r0-near",
            hop_class="short",
            src=1,
            dst=0,
            credit_path_nodes=(0, 1),
            bottleneck_key=None,
        ),
        FlowTemplate(
            name="long-r0-far",
            hop_class="long",
            src=3,
            dst=0,
            credit_path_nodes=(0, 1, 2, 3),
            bottleneck_key=(1, 2),
        ),
        FlowTemplate(
            name="short-r1-near",
            hop_class="short",
            src=2,
            dst=1,
            credit_path_nodes=(1, 2),
            bottleneck_key=(1, 2),
        ),
        FlowTemplate(
            name="long-r1-far",
            hop_class="long",
            src=3,
            dst=1,
            credit_path_nodes=(1, 2, 3),
            bottleneck_key=(1, 2),
        ),
    ]


def _template_order(order: str) -> Dict[str, int]:
    if order == "long-first":
        return {"long": 0, "short": 1}
    if order == "short-first":
        return {"short": 0, "long": 1}
    if order == "simultaneous":
        return {"short": 0, "long": 0}
    raise ValueError(f"unknown order {order!r}")


def _same_slice_paths(
    slice_to_topo: Mapping[int, nx.Graph],
) -> Tuple[
    List[OpticalPath],
    Dict[Tuple[int, int, int], int],
    Dict[Tuple[int, int, int], Tuple[int, ...]],
]:
    """Build per-hop routing entries from same-slice shortest paths."""
    paths: List[OpticalPath] = []
    hop_by_slice: Dict[Tuple[int, int, int], int] = {}
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
                            cur_node=cur_node,
                            step_type="port",
                            send_port=int(edge["port1"]),
                            send_ts=ts,
                            send_node=next_node,
                        )
                    )
                if not steps:
                    continue

                paths.append(OpticalPath(src=src, arrival_ts=ts, dst=dst, steps=steps))
                key = (int(src), int(dst), int(ts))
                hop_by_slice[key] = len(steps)
                nodes_by_slice[key] = tuple(int(n) for n in path_nodes)

    return paths, hop_by_slice, nodes_by_slice


def _validate_credit_paths(
    templates: Sequence[FlowTemplate],
    nodes_by_slice: Mapping[Tuple[int, int, int], Tuple[int, ...]],
    start_slice: int,
) -> None:
    errors = []
    for tmpl in templates:
        credit_pair = (tmpl.dst, tmpl.src, start_slice)
        actual = nodes_by_slice.get(credit_pair)
        if actual != tmpl.credit_path_nodes:
            errors.append(
                f"{tmpl.name}: expected credit path {tmpl.credit_path_nodes}, got {actual}"
            )
    if errors:
        joined = "\n  ".join(errors)
        raise RuntimeError(f"manual chain routing mismatch:\n  {joined}")


def _build_expected_bottleneck_model(
    templates: Sequence[FlowTemplate],
    *,
    order: str,
    flows_per_pair: int,
    initial_credit: int,
    qsize: int,
    gap_us: float,
) -> Dict[str, Dict[str, int]]:
    """Model first-burst admission at the intended tor1->tor2 bottleneck.

    This deliberately mirrors the experiment's first burst, not the complete
    Flare closed loop.  It is useful as a sanity target for the ns-3 counters.
    """
    rank = _template_order(order)
    events = []
    for tmpl in templates:
        if tmpl.bottleneck_key is None:
            continue
        start_rank = rank[tmpl.hop_class]
        for replica in range(flows_per_pair):
            events.append(
                (
                    start_rank,
                    tmpl.name,
                    replica,
                    tmpl.hop_class,
                    tmpl.credit_hops,
                    initial_credit,
                )
            )
    events.sort(key=lambda item: (item[0], item[1], item[2]))

    admitted_total = 0
    by_group: Dict[str, Dict[str, int]] = {}
    occupancy = 0
    for _rank, name, _replica, hop_class, credit_hops, burst in events:
        row = by_group.setdefault(
            name,
            {
                "expected_credit_burst": 0,
                "expected_bottleneck_admitted": 0,
                "expected_bottleneck_dropped": 0,
                "expected_credit_hop_cost_sent": 0,
                "expected_credit_hop_cost_dropped": 0,
                "credit_hops": credit_hops,
                "hop_class": hop_class,
            },
        )
        for _ in range(burst):
            row["expected_credit_burst"] += 1
            row["expected_credit_hop_cost_sent"] += credit_hops
            if occupancy < qsize:
                occupancy += 1
                admitted_total += 1
                row["expected_bottleneck_admitted"] += 1
            else:
                row["expected_bottleneck_dropped"] += 1
                row["expected_credit_hop_cost_dropped"] += credit_hops

    return {
        "parameters": {
            "model": "first-credit-burst at tor1->tor2",
            "order": order,
            "flows_per_pair": flows_per_pair,
            "initial_credit": initial_credit,
            "bottleneck_qsize": qsize,
            "gap_us": gap_us,
            "note": (
                "This model intentionally ignores queue drain and refreshes; "
                "it is the expected first-burst ordering pressure."
            ),
        },
        "total": {
            "expected_bottleneck_admitted": admitted_total,
            "expected_bottleneck_dropped": sum(
                row["expected_bottleneck_dropped"] for row in by_group.values()
            ),
            "expected_credit_burst": sum(
                row["expected_credit_burst"] for row in by_group.values()
            ),
        },
        "by_group": by_group,
    }


def _make_flow_plans(args, expected_model: Mapping[str, object]) -> List[FlowPlan]:
    rank = _template_order(args.order)
    gap_s = args.gap_us / 1e6
    by_group = expected_model["by_group"]

    plans: List[FlowPlan] = []
    for tmpl in _flow_templates():
        group_expected = by_group.get(tmpl.name, {})
        for replica in range(args.flows_per_pair):
            class_start = args.start + rank[tmpl.hop_class] * gap_s
            start_s = class_start + replica * (args.replica_gap_us / 1e6)
            plans.append(
                FlowPlan(
                    name=f"{tmpl.name}-{replica:03d}",
                    group_name=tmpl.name,
                    hop_class=tmpl.hop_class,
                    src=tmpl.src,
                    dst=tmpl.dst,
                    start_s=start_s,
                    credit_path_nodes=tmpl.credit_path_nodes,
                    bottleneck_key=tmpl.bottleneck_key,
                    credit_hops=tmpl.credit_hops,
                    expected_credit_burst=int(
                        group_expected.get("expected_credit_burst", 0)
                    ),
                    expected_bottleneck_admitted=int(
                        group_expected.get("expected_bottleneck_admitted", 0)
                    ),
                    expected_bottleneck_dropped=int(
                        group_expected.get("expected_bottleneck_dropped", 0)
                    ),
                )
            )

    plans.sort(key=lambda p: (rank[p.hop_class], p.start_s, p.group_name, p.name))
    return plans


def _backend_kwargs(args) -> dict:
    kwargs = {}
    if args.flare_credit_qsize is not None:
        kwargs["flare_credit_qsize_pkts"] = args.flare_credit_qsize
    if args.flare_congestion_threshold is not None:
        kwargs["flare_congestion_threshold_percent"] = args.flare_congestion_threshold
    if args.flare_tentative_threshold is not None:
        kwargs["flare_tentative_threshold_percent"] = args.flare_tentative_threshold
    return kwargs


def _build_network(args):
    slice_us = _slice_us(args.profile)
    net = Toolbox.BaseNetwork(
        name=f"flare_credit_priority_chain4_{args.order}",
        backend="ns3",
        nb_node=4,
        nb_link=2,
        time_slice_duration_us=slice_us,
        guardband_ms=0,
        ocs_tor_link_bw_gbps=args.ocs_bw,
        tor_host_link_bw_gbps=args.host_bw,
        use_webserver=args.dashboard,
        simulation_stop_s=args.stop,
        snapshot_interval_us=slice_us,
        flare_profile=args.profile,
        **_backend_kwargs(args),
    )

    circuits = _chain4_topology()
    if args.save_topology is not None:
        _save_topology(args.save_topology, circuits, args)
        print(f"Saved topology to {args.save_topology}")

    net.deploy_topo(circuits)
    paths, hop_by_slice, nodes_by_slice = _same_slice_paths(net.get_topo())
    _validate_credit_paths(_flow_templates(), nodes_by_slice, start_slice=0)
    net.deploy_routing(paths, routing_mode="Per-hop", start_fresh=True)
    return net, hop_by_slice, nodes_by_slice


def _install_flows(net, plans: Sequence[FlowPlan], args):
    cfg = FlareConfig.from_profile(
        args.profile,
        mtu_bytes=args.mtu,
        initial_credit_pkts=args.initial_credit,
        retransmission_timeout_s=args.refresh_timeout_us / 1e6,
    )
    gen = net.flare_traffic(config=cfg)
    for plan in plans:
        gen.flow(
            plan.src,
            plan.dst,
            args.flow_size,
            start_s=plan.start_s,
            duration_s=args.duration,
            packet_size_bytes=args.mtu,
            name=plan.name,
        )
    return gen.install()


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _mean(values: Iterable[float]):
    vals = [v for v in values if _finite(v)]
    return statistics.mean(vals) if vals else None


def _safe_ratio(numerator: float, denominator: float):
    return None if denominator == 0 else numerator / denominator


def _stats_row(plan: FlowPlan, stats: FlareStats) -> dict:
    hist = dict(stats.path_length_histogram or {})
    credit_loss_proxy = stats.credits_sent - stats.credits_received
    return {
        "flow_id": stats.flow_id,
        "name": plan.name,
        "group_name": plan.group_name,
        "hop_class": plan.hop_class,
        "src": stats.src,
        "dst": stats.dst,
        "data_path": f"{stats.src}->{stats.dst}",
        "credit_path": "->".join(map(str, plan.credit_path_nodes)),
        "credit_hops": plan.credit_hops,
        "uses_bottleneck_tor1_to_tor2": plan.bottleneck_key == (1, 2),
        "start_s": plan.start_s,
        "fct_s": stats.fct_s,
        "throughput_bps": stats.throughput_bps,
        "data_packets_sent": stats.data_packets_sent,
        "data_packets_received": stats.data_packets_received,
        "credits_sent": stats.credits_sent,
        "credits_received": stats.credits_received,
        "credit_loss_proxy": credit_loss_proxy,
        "credit_delivery_ratio": _safe_ratio(stats.credits_received, stats.credits_sent),
        "credit_hop_cost_sent": stats.credits_sent * plan.credit_hops,
        "credit_hop_cost_received": stats.credits_received * plan.credit_hops,
        "credit_hop_loss_cost": credit_loss_proxy * plan.credit_hops,
        "retransmissions": stats.retransmissions,
        "timeouts": stats.timeouts,
        "duplicate_data": stats.duplicate_data,
        "duplicate_credits": stats.duplicate_credits,
        "path_len_0": hist.get(0, 0),
        "path_len_1": hist.get(1, 0),
        "path_len_2": hist.get(2, 0),
        "path_len_3": hist.get(3, 0),
    }


def _group_rows(rows: Sequence[dict]) -> Dict[str, dict]:
    grouped: DefaultDict[str, List[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["group_name"]].append(row)

    result = {}
    for group, subset in sorted(grouped.items()):
        result[group] = {
            "flow_count": len(subset),
            "hop_class": subset[0]["hop_class"],
            "credit_hops": subset[0]["credit_hops"],
            "uses_bottleneck_tor1_to_tor2": subset[0]["uses_bottleneck_tor1_to_tor2"],
            "completed_flow_count": sum(
                1 for r in subset if _finite(r["fct_s"]) and r["fct_s"] > 0
            ),
            "mean_fct_s": _mean(r["fct_s"] for r in subset),
            "total_data_packets_sent": sum(r["data_packets_sent"] for r in subset),
            "total_data_packets_received": sum(
                r["data_packets_received"] for r in subset
            ),
            "total_credits_sent": sum(r["credits_sent"] for r in subset),
            "total_credits_received": sum(r["credits_received"] for r in subset),
            "total_credit_loss_proxy": sum(r["credit_loss_proxy"] for r in subset),
            "mean_credit_delivery_ratio": _mean(
                r["credit_delivery_ratio"] for r in subset
            ),
            "total_credit_hop_cost_sent": sum(
                r["credit_hop_cost_sent"] for r in subset
            ),
            "total_credit_hop_cost_received": sum(
                r["credit_hop_cost_received"] for r in subset
            ),
            "total_credit_hop_loss_cost": sum(
                r["credit_hop_loss_cost"] for r in subset
            ),
        }
    return result


def _class_summary(rows: Sequence[dict], hop_class: str) -> dict:
    subset = [r for r in rows if r["hop_class"] == hop_class]
    return {
        "flow_count": len(subset),
        "completed_flow_count": sum(
            1 for r in subset if _finite(r["fct_s"]) and r["fct_s"] > 0
        ),
        "mean_fct_s": _mean(r["fct_s"] for r in subset),
        "total_data_packets_sent": sum(r["data_packets_sent"] for r in subset),
        "total_data_packets_received": sum(r["data_packets_received"] for r in subset),
        "total_credits_sent": sum(r["credits_sent"] for r in subset),
        "total_credits_received": sum(r["credits_received"] for r in subset),
        "total_credit_loss_proxy": sum(r["credit_loss_proxy"] for r in subset),
        "mean_credit_delivery_ratio": _mean(r["credit_delivery_ratio"] for r in subset),
        "total_credit_hop_cost_sent": sum(r["credit_hop_cost_sent"] for r in subset),
        "total_credit_hop_cost_received": sum(
            r["credit_hop_cost_received"] for r in subset
        ),
        "total_credit_hop_loss_cost": sum(r["credit_hop_loss_cost"] for r in subset),
    }


def _flow_count_warning(args, rows: Sequence[dict]) -> Optional[str]:
    if not rows:
        return None
    total_credits_sent = sum(r["credits_sent"] for r in rows)
    first_burst = len(rows) * args.initial_credit
    if total_credits_sent > first_burst * 2:
        return (
            "credits_sent is much larger than the initial burst.  The run is "
            "measuring refresh/retry behavior too; shorten --stop or increase "
            "--refresh-timeout-us if you want a first-burst-only comparison."
        )
    return None


def _summary(args, rows: Sequence[dict], first_stats: Optional[FlareStats]) -> dict:
    total_credits_sent = sum(r["credits_sent"] for r in rows)
    total_credits_received = sum(r["credits_received"] for r in rows)
    summary = {
        "scenario": "manual-chain4-credit-bottleneck",
        "topology": "0--1--2--3, corresponding to conceptual 1--2--3--4",
        "intended_bottleneck": "tor1 egress to tor2, port 1, slice 0",
        "order": args.order,
        "profile": args.profile,
        "nodes": 4,
        "links": 2,
        "flows_per_pair": args.flows_per_pair,
        "flow_count": len(rows),
        "start_s": args.start,
        "gap_us": args.gap_us,
        "replica_gap_us": args.replica_gap_us,
        "duration_s": args.duration,
        "stop_s": args.stop,
        "flow_size": args.flow_size,
        "mtu": args.mtu,
        "initial_credit": args.initial_credit,
        "refresh_timeout_us": args.refresh_timeout_us,
        "flare_credit_qsize": args.flare_credit_qsize,
        "flare_congestion_threshold": args.flare_congestion_threshold,
        "total_credits_sent": total_credits_sent,
        "total_credits_received": total_credits_received,
        "total_credit_loss_proxy": total_credits_sent - total_credits_received,
        "class_summary": {
            "short": _class_summary(rows, "short"),
            "long": _class_summary(rows, "long"),
        },
        "group_summary": _group_rows(rows),
    }
    warning = _flow_count_warning(args, rows)
    if warning:
        summary["warning"] = warning
    if first_stats is not None:
        summary.update(
            {
                "tor_credit_admitted": first_stats.credits_admitted,
                "tor_credit_dropped": first_stats.credits_dropped,
                "tor_credit_wasted": first_stats.credits_wasted,
            }
        )
    return summary


def _save_topology(path: Path, circuits: Sequence[Sequence[int]], args) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "openoptics-circuits-v1",
        "scenario": "manual-chain4-credit-bottleneck",
        "nodes": 4,
        "links": 2,
        "profile": args.profile,
        "circuits": [list(map(int, c)) for c in circuits],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _print_setup(
    args,
    plans: Sequence[FlowPlan],
    hop_by_slice: Mapping[Tuple[int, int, int], int],
    expected_model: Mapping[str, object],
) -> None:
    print("=== Flare Fixed-Chain Credit Priority Experiment ===")
    print(
        f"order={args.order}, profile={args.profile}, "
        f"flows_per_pair={args.flows_per_pair}, initial_credit={args.initial_credit}, "
        f"qsize={args.flare_credit_qsize}"
    )
    print("Topology: 0 -- 1 -- 2 -- 3  (conceptual: 1 -- 2 -- 3 -- 4)")
    print("Intended bottleneck: tor1 -> tor2, port 1, slice 0")
    print(
        "Routing hop distribution: "
        f"{dict(sorted(Counter(hop_by_slice.values()).items()))}"
    )
    print("Expected first-burst bottleneck model:")
    print(json.dumps(expected_model["total"], indent=2, sort_keys=True))
    print("Flow groups:")
    group_seen = set()
    for plan in plans:
        if plan.group_name in group_seen:
            continue
        group_seen.add(plan.group_name)
        bottleneck = "yes" if plan.bottleneck_key == (1, 2) else "no"
        print(
            f"  {plan.group_name}: data {plan.src}->{plan.dst}, "
            f"credit {'->'.join(map(str, plan.credit_path_nodes))}, "
            f"class={plan.hop_class}, credit_hops={plan.credit_hops}, "
            f"uses_bottleneck={bottleneck}"
        )
    print(f"Installed flows: {len(plans)}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--order",
        choices=["long-first", "short-first", "simultaneous"],
        default="long-first",
    )
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--flows-per-pair", type=int, default=16)
    parser.add_argument("--flow-size", type=int, default=65_536)
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=8)
    parser.add_argument("--start", type=float, default=0.0001)
    parser.add_argument(
        "--gap-us",
        type=float,
        default=5.0,
        help="start offset between the first and second credit class",
    )
    parser.add_argument(
        "--replica-gap-us",
        type=float,
        default=0.0,
        help="optional start gap between replicas in the same group",
    )
    parser.add_argument("--duration", type=float, default=0.00015)
    parser.add_argument("--stop", type=float, default=0.00035)
    parser.add_argument(
        "--refresh-timeout-us",
        type=float,
        default=200.0,
        help="Flare receiver credit refresh timeout",
    )
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    parser.add_argument("--flare-credit-qsize", type=int, default=8)
    parser.add_argument("--flare-congestion-threshold", type=int, default=0)
    parser.add_argument("--flare-tentative-threshold", type=int, default=100)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--save-topology", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("results/manual_chain4"))
    args = parser.parse_args(argv)

    if args.flows_per_pair <= 0:
        raise ValueError("--flows-per-pair must be positive")
    if args.initial_credit <= 0:
        raise ValueError("--initial-credit must be positive")
    if args.flare_credit_qsize <= 0:
        raise ValueError("--flare-credit-qsize must be positive")

    expected_model = _build_expected_bottleneck_model(
        _flow_templates(),
        order=args.order,
        flows_per_pair=args.flows_per_pair,
        initial_credit=args.initial_credit,
        qsize=args.flare_credit_qsize,
        gap_us=args.gap_us,
    )
    plans = _make_flow_plans(args, expected_model)
    net, hop_by_slice, _nodes_by_slice = _build_network(args)
    _print_setup(args, plans, hop_by_slice, expected_model)

    installed = _install_flows(net, plans, args)
    net.start()

    rows = []
    first_stats = None
    for plan, flow in zip(plans, installed):
        stats = flow.stats()
        if stats is None:
            continue
        if first_stats is None:
            first_stats = stats
        rows.append(_stats_row(plan, stats))

    payload = {
        "summary": _summary(args, rows, first_stats),
        "expected_first_burst_model": expected_model,
        "flows": rows,
    }

    out_dir = args.output
    _write_json(out_dir / "summary.json", payload["summary"])
    _write_json(out_dir / "expected_first_burst_model.json", expected_model)
    _write_json(out_dir / "flows.json", payload)
    _write_csv(out_dir / "flows.csv", rows)

    print("\nSummary:")
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    print(f"\nWrote {out_dir / 'summary.json'}")
    print(f"Wrote {out_dir / 'expected_first_burst_model.json'}")
    print(f"Wrote {out_dir / 'flows.csv'}")


if __name__ == "__main__":
    main()
