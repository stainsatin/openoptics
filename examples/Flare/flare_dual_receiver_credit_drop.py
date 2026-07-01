"""
Topology:
    0
     \
      2 -- 3
     /
    1
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple


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
    credit_path_nodes: Tuple[int, ...]

    @property
    def credit_hops(self) -> int:
        return len(self.credit_path_nodes) - 1


def _slice_us(profile: str) -> int:
    return 55 if profile == "55us" else 15


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


def _safe_ratio(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


def _topology() -> List[List[int]]:
    """Return one static slice for 0--2--3 and 1--2--3.

    Circuit format: [time_slice, node1, node2, port1, port2].
    ``tor2`` uses three optical ports; the leaves use port 0.
    """
    return [
        [0, 0, 2, 0, 0],
        [0, 1, 2, 0, 1],
        [0, 2, 3, 2, 0],
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
                paths.append(OpticalPath(src=int(src), arrival_ts=int(ts),
                                         dst=int(dst), steps=steps))
                nodes_by_slice[(int(src), int(dst), int(ts))] = tuple(
                    int(node) for node in path_nodes
                )

    return paths, nodes_by_slice


def _flow_plans(flows_per_receiver: int) -> List[FlowPlan]:
    plans: List[FlowPlan] = []
    for replica in range(flows_per_receiver):
        suffix = "" if flows_per_receiver == 1 else f"-r{replica}"
        plans.append(
            FlowPlan(
                name=f"sender3-to-receiver0{suffix}",
                src=3,
                dst=0,
                credit_path_nodes=(0, 2, 3),
            )
        )
        plans.append(
            FlowPlan(
                name=f"sender3-to-receiver1{suffix}",
                src=3,
                dst=1,
                credit_path_nodes=(1, 2, 3),
            )
        )
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
                f"{plan.name}: expected credit path "
                f"{plan.credit_path_nodes}, got {actual}"
            )
    if errors:
        raise RuntimeError("credit path validation failed:\n  " + "\n  ".join(errors))


def _build_network(args: argparse.Namespace, flow_size: int):
    slice_us = _slice_us(args.profile)
    net = Toolbox.BaseNetwork(
        name="flare_dual_receiver_credit_drop",
        backend="ns3",
        nb_node=4,
        nb_link=3,
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
        flare_w_init=args.w_init,
        flare_target_loss=args.target_loss,
        flare_credit_qsize_pkts=args.flare_credit_qsize,
        flare_shaping_thresh_pkts=args.flare_shaping_thresh,
        flare_aeolus_thresh_pkts=args.flare_aeolus_thresh,
        flare_congestion_threshold_percent=args.flare_congestion_threshold,
        flare_tentative_threshold_percent=args.flare_tentative_threshold,
    )
    net.deploy_topo(_topology())
    paths, nodes_by_slice = _same_slice_paths(net.get_topo())
    plans = _flow_plans(args.flows_per_receiver)
    _validate_credit_paths(plans, nodes_by_slice)
    net.deploy_routing(paths, routing_mode="Per-hop")

    config = FlareConfig.from_profile(
        args.profile,
        credit_qsize_pkts=args.flare_credit_qsize,
        shaping_thresh_pkts=args.flare_shaping_thresh,
        aeolus_thresh_pkts=args.flare_aeolus_thresh,
        w_init=args.w_init,
        target_loss=args.target_loss,
        mtu_bytes=args.mtu,
        initial_credit_pkts=args.initial_credit,
        congestion_threshold_percent=args.flare_congestion_threshold,
        tentative_threshold_percent=args.flare_tentative_threshold,
    )
    gen = net.flare_traffic(config=config)
    for plan in plans:
        gen.flow(
            plan.src,
            plan.dst,
            flow_size,
            start_s=args.start,
            duration_s=args.duration,
            packet_size_bytes=args.mtu,
            name=plan.name,
        )
    return net, gen.install(), plans, nodes_by_slice


def _flow_row(plan: FlowPlan, stats: FlareStats) -> Dict[str, Any]:
    return {
        "name": plan.name,
        "src": stats.src,
        "dst": stats.dst,
        "credit_path": "->".join(str(node) for node in plan.credit_path_nodes),
        "credit_hops": plan.credit_hops,
        "fct_s": stats.fct_s,
        "credits_sent": stats.credits_sent,
        "credits_received": stats.credits_received,
        "credit_loss_proxy": stats.credits_sent - stats.credits_received,
        "data_packets_sent": stats.data_packets_sent,
        "data_packets_received": stats.data_packets_received,
        "duplicate_credits": stats.duplicate_credits,
        "duplicate_data": stats.duplicate_data,
        "retransmissions": stats.retransmissions,
        "timeouts": stats.timeouts,
    }


def _tor_counters(installed_flows) -> List[Dict[str, int]]:
    if not installed_flows:
        return []
    backend = getattr(installed_flows[0], "_backend", None)
    tor_apps = getattr(backend, "_tor_apps", {})
    rows = []
    for tor_id in sorted(tor_apps):
        app = tor_apps[tor_id]
        rows.append(
            {
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
            }
        )
    return rows


def _first_burst_model(args: argparse.Namespace, flow_size: int, flow_count: int) -> Dict[str, Any]:
    flow_packets = math.ceil(flow_size / args.mtu)
    burst_per_flow = min(args.initial_credit, flow_packets)
    burst_total = flow_count * burst_per_flow
    bottleneck_admitted = min(args.flare_credit_qsize, burst_total)
    bottleneck_dropped = max(0, burst_total - bottleneck_admitted)
    first_hop_admitted = burst_total
    return {
        "applies_cleanly": flow_packets <= args.initial_credit,
        "flow_packets": flow_packets,
        "burst_per_flow": burst_per_flow,
        "burst_total": burst_total,
        "expected_first_hop_admitted": first_hop_admitted,
        "expected_tor2_bottleneck_admitted": bottleneck_admitted,
        "expected_tor2_bottleneck_dropped": bottleneck_dropped,
        "expected_total_credit_admitted": first_hop_admitted + bottleneck_admitted,
        "assumptions": (
            "w_init=1, congestion_threshold=100, no refresh before stop, "
            "and all initial credits reach tor2 in one credit-admission epoch"
        ),
    }


def _summarize(
    args: argparse.Namespace,
    flow_size: int,
    flow_rows: List[Dict[str, Any]],
    tor_rows: List[Dict[str, int]],
) -> Dict[str, Any]:
    model = _first_burst_model(args, flow_size, len(flow_rows))
    tor2 = next((row for row in tor_rows if row["tor"] == 2), None)
    total_credit_admitted = sum(row["credit_admitted"] for row in tor_rows)
    total_credit_dropped = sum(row["credit_dropped"] for row in tor_rows)
    total_credit_wasted = sum(row["credit_wasted"] for row in tor_rows)
    total_credits_sent = sum(row["credits_sent"] for row in flow_rows)
    total_credits_received = sum(row["credits_received"] for row in flow_rows)
    tor2_dropped = tor2["credit_dropped"] if tor2 else 0
    expected_tor2_dropped = int(model["expected_tor2_bottleneck_dropped"])
    observed_expected_drop_ratio = _safe_ratio(tor2_dropped, expected_tor2_dropped)
    return {
        "scenario": "dual-receiver shared credit bottleneck",
        "topology": "0--2--3 and 1--2--3, one static slice",
        "data_flows": "3->0 and 3->1",
        "credit_paths": ["0->2->3", "1->2->3"],
        "intended_bottleneck": "tor2 egress to tor3, port 2, slice 0",
        "flow_count": len(flow_rows),
        "flow_size": flow_size,
        "mtu": args.mtu,
        "initial_credit": args.initial_credit,
        "flare_credit_qsize": args.flare_credit_qsize,
        "flare_congestion_threshold": args.flare_congestion_threshold,
        "w_init": args.w_init,
        "first_burst_model": model,
        "total_credits_sent": total_credits_sent,
        "total_credits_received": total_credits_received,
        "total_credit_loss_proxy": total_credits_sent - total_credits_received,
        "total_tor_credit_admitted": total_credit_admitted,
        "total_tor_credit_dropped": total_credit_dropped,
        "total_tor_credit_wasted": total_credit_wasted,
        "tor2_credit_admitted": tor2["credit_admitted"] if tor2 else None,
        "tor2_credit_dropped": tor2_dropped if tor2 else None,
        "tor2_drop_observed_expected_ratio": observed_expected_drop_ratio,
        "credit_drop_observed": total_credit_dropped > 0,
        "possible_over_accept_signal": (
            model["applies_cleanly"]
            and expected_tor2_dropped > 0
            and tor2_dropped < expected_tor2_dropped
        ),
        "verdict": (
            "tor2 dropped fewer credits than the first-burst queue model predicts."
            if (
                model["applies_cleanly"]
                and expected_tor2_dropped > 0
                and tor2_dropped < expected_tor2_dropped
            )
            else "tor2 credit drops are present and meet or exceed the first-burst queue model."
            if tor2_dropped > 0
            else "no tor2 credit drop was observed in this run."
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
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--flows-per-receiver", type=int, default=1)
    parser.add_argument("--flow-size", type=int, default=0,
                        help="0 means initial_credit * mtu, i.e. first-burst only")
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=8)
    parser.add_argument("--start", type=float, default=0.000105)
    parser.add_argument("--duration", type=float, default=0.00008)
    parser.add_argument("--stop", type=float, default=0.00018)
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    parser.add_argument("--host-delay-us", type=int, default=1)
    parser.add_argument("--ocs-delay-us", type=int, default=1)
    parser.add_argument("--guardband-us", type=int, default=0)
    parser.add_argument("--w-init", type=float, default=1.0)
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
        default=Path("results/flare_dual_receiver_credit_drop.json"),
    )
    args = parser.parse_args(argv)
    if args.flows_per_receiver <= 0:
        raise ValueError("--flows-per-receiver must be positive")
    if args.initial_credit <= 0:
        raise ValueError("--initial-credit must be positive")
    if args.flow_size < 0:
        raise ValueError("--flow-size must be non-negative")

    flow_size = args.flow_size or args.initial_credit * args.mtu
    net, installed, plans, nodes_by_slice = _build_network(args, flow_size)

    print("=== Dual Receiver Credit Drop Experiment ===")
    print("Topology: 0--2--3 and 1--2--3")
    print("Data flows: 3->0 and 3->1")
    print("Credit paths:")
    for plan in plans:
        print(f"  {plan.name}: {'->'.join(str(n) for n in plan.credit_path_nodes)}")
    print("Intended bottleneck: tor2 -> tor3, port 2, slice 0")

    net.start()

    flow_rows = []
    for plan, installed_flow in zip(plans, installed):
        stats = installed_flow.stats()
        if stats is not None:
            flow_rows.append(_flow_row(plan, stats))
    tor_rows = _tor_counters(installed)
    summary = _summarize(args, flow_size, flow_rows, tor_rows)
    parameters = dict(vars(args))
    parameters["resolved_flow_size"] = flow_size
    payload = {
        "experiment": "flare_dual_receiver_credit_drop",
        "parameters": parameters,
        "routes": {
            f"{src}->{dst}@{ts}": list(path)
            for (src, dst, ts), path in sorted(nodes_by_slice.items())
        },
        "summary": summary,
        "flows": flow_rows,
        "tors": tor_rows,
    }
    _write_json(args.output, payload)

    print("\nSummary:")
    print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
