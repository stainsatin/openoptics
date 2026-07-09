r"""Shared-uplink Flare credit-drop micro experiment.

Static topology:

    r0   r1   r2   r5
     \   |   |   /
          tor3 ---- tor4(sender)

Data flows are sender -> receiver:

    4 -> 0, 4 -> 1, 4 -> 2, 4 -> 5

Credits travel in the reverse direction and all share the same bottleneck:

    0 -> 3 -> 4
    1 -> 3 -> 4
    2 -> 3 -> 4
    5 -> 3 -> 4

The intended credit bottleneck is tor3 egress port 4 in slice 0.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


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


RECEIVERS = (0, 1, 2, 5)
AGG_TOR = 3
SENDER = 4
NB_NODES = 6
NB_LINKS = 5


@dataclass(frozen=True)
class FlowPlan:
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


def _topology() -> List[List[int]]:
    """Return one static slice.

    Circuit format is [time_slice, node1, node2, node1_port, node2_port].
    tor3 port4 is the shared credit egress toward sender tor4.
    """
    return [
        [0, 0, AGG_TOR, 0, 0],
        [0, 1, AGG_TOR, 0, 1],
        [0, 2, AGG_TOR, 0, 2],
        [0, 5, AGG_TOR, 0, 3],
        [0, AGG_TOR, SENDER, 4, 0],
    ]


def _directed_port_map(circuits: Sequence[Sequence[int]]) -> Dict[Tuple[int, int, int], int]:
    ports: Dict[Tuple[int, int, int], int] = {}
    for ts, node1, node2, port1, port2 in circuits:
        ts = int(ts)
        node1 = int(node1)
        node2 = int(node2)
        ports[(ts, node1, node2)] = int(port1)
        ports[(ts, node2, node1)] = int(port2)
    return ports


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
                        raise RuntimeError(f"missing directed port for {key}")
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


def _flow_plans(
    args: argparse.Namespace,
    nodes_by_slice: Mapping[Tuple[int, int, int], Tuple[int, ...]],
) -> List[FlowPlan]:
    plans: List[FlowPlan] = []
    replica_gap_s = args.replica_gap_us / 1e6
    for receiver in RECEIVERS:
        data_path = nodes_by_slice[(SENDER, receiver, 0)]
        credit_path = nodes_by_slice[(receiver, SENDER, 0)]
        for replica in range(args.flows_per_receiver):
            plans.append(
                FlowPlan(
                    sender=SENDER,
                    receiver=receiver,
                    replica=replica,
                    name=f"shared-uplink-s{SENDER}-r{receiver}-f{replica:03d}",
                    start_s=args.start + replica * replica_gap_s,
                    data_path_nodes=data_path,
                    credit_path_nodes=credit_path,
                )
            )
    plans.sort(key=lambda p: (p.start_s, p.receiver, p.replica))
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
        raise RuntimeError("path validation failed:\n  " + "\n  ".join(errors))


def _build_network(args: argparse.Namespace, flow_size: int):
    slice_us = _slice_us(args.profile)
    circuits = _topology()
    port_map = _directed_port_map(circuits)
    net = Toolbox.BaseNetwork(
        name="flare_shared_uplink_credit_drop",
        backend="ns3",
        nb_node=NB_NODES,
        nb_link=NB_LINKS,
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
    net.deploy_topo(circuits)
    paths, nodes_by_slice = _same_slice_paths(net.get_topo(), port_map)
    net.deploy_routing(paths, routing_mode="Per-hop")
    plans = _flow_plans(args, nodes_by_slice)
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


def _flow_row(plan: FlowPlan, stats: FlareStats) -> Dict[str, Any]:
    credit_loss = stats.credits_sent - stats.credits_received
    completed = _finite(stats.fct_s) and float(stats.fct_s) > 0
    return {
        "name": plan.name,
        "flow_id": stats.flow_id,
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
        "data_packets_sent": stats.data_packets_sent,
        "data_packets_received": stats.data_packets_received,
        "duplicate_credits": stats.duplicate_credits,
        "duplicate_data": stats.duplicate_data,
        "timeouts": stats.timeouts,
        "retransmissions": stats.retransmissions,
    }


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
                "role": (
                    "receiver_leaf" if int(tor_id) in RECEIVERS
                    else "shared_bottleneck" if int(tor_id) == AGG_TOR
                    else "sender" if int(tor_id) == SENDER
                    else "unused"
                ),
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


def _expected_pressure(
    args: argparse.Namespace,
    plans: Sequence[FlowPlan],
    flow_size: int,
) -> Dict[str, Any]:
    burst_per_flow = min(args.initial_credit, math.ceil(flow_size / args.mtu))
    by_edge: DefaultDict[str, int] = defaultdict(int)
    by_egress: DefaultDict[str, int] = defaultdict(int)
    port_map = _directed_port_map(_topology())
    for plan in plans:
        for src, dst in zip(plan.credit_path_nodes[:-1], plan.credit_path_nodes[1:]):
            by_edge[f"{src}->{dst}"] += burst_per_flow
            by_egress[f"tor{src}:port{port_map[(0, src, dst)]}"] += burst_per_flow
    return {
        "burst_per_flow": burst_per_flow,
        "initial_credit_burst_total": len(plans) * burst_per_flow,
        "by_credit_edge": dict(sorted(by_edge.items(), key=lambda item: (-item[1], item[0]))),
        "by_egress_port": dict(sorted(by_egress.items(), key=lambda item: (-item[1], item[0]))),
        "intended_bottleneck_egress": f"tor{AGG_TOR}:port4",
        "intended_bottleneck_offered_credits": by_egress.get(f"tor{AGG_TOR}:port4", 0),
    }


def _summary(
    args: argparse.Namespace,
    flow_size: int,
    flow_rows: Sequence[Dict[str, Any]],
    tor_rows: Sequence[Dict[str, Any]],
    plans: Sequence[FlowPlan],
) -> Dict[str, Any]:
    bottleneck = next((row for row in tor_rows if row["tor"] == AGG_TOR), {})
    total_credits_sent = sum(row["credits_sent"] for row in flow_rows)
    total_credits_received = sum(row["credits_received"] for row in flow_rows)
    data_received = sum(row["data_packets_received"] for row in flow_rows)
    fcts = [
        row["fct_s"] for row in flow_rows
        if _finite(row["fct_s"]) and float(row["fct_s"]) > 0
    ]
    return {
        "scenario": "shared-uplink-credit-drop",
        "topology": "0,1,2,5 -> tor3 -> tor4(sender), one static slice",
        "data_flows": "tor4 -> each receiver leaf",
        "credit_paths": sorted(
            set("->".join(str(node) for node in plan.credit_path_nodes) for plan in plans)
        ),
        "intended_bottleneck": f"tor{AGG_TOR} egress to tor{SENDER}, port 4, slice 0",
        "flow_count": len(flow_rows),
        "receiver_count": len(RECEIVERS),
        "flows_per_receiver": args.flows_per_receiver,
        "flow_size": flow_size,
        "flow_packets": math.ceil(flow_size / args.mtu),
        "mtu": args.mtu,
        "initial_credit": args.initial_credit,
        "flare_credit_qsize": args.flare_credit_qsize,
        "flare_congestion_threshold": args.flare_congestion_threshold,
        "expected_pressure": _expected_pressure(args, plans, flow_size),
        "total_credits_sent": total_credits_sent,
        "total_credits_received": total_credits_received,
        "total_credit_delivery_ratio": _safe_ratio(total_credits_received, total_credits_sent),
        "total_credit_loss_proxy": total_credits_sent - total_credits_received,
        "total_credit_tax": _safe_ratio(total_credits_sent, data_received),
        "total_tor_credit_admitted": sum(row["credit_admitted"] for row in tor_rows),
        "total_tor_credit_dropped": sum(row["credit_dropped"] for row in tor_rows),
        "total_tor_credit_wasted": sum(row["credit_wasted"] for row in tor_rows),
        "total_overflow_drops": sum(row["overflow_drops"] for row in tor_rows),
        "bottleneck_tor": bottleneck,
        "bottleneck_credit_dropped": bottleneck.get("credit_dropped"),
        "bottleneck_credit_admitted": bottleneck.get("credit_admitted"),
        "credit_drop_observed": sum(row["credit_dropped"] for row in tor_rows) > 0,
        "completed_flow_count": len(fcts),
    }


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _print_setup(args: argparse.Namespace, plans: Sequence[FlowPlan]) -> None:
    print("\n=== Shared Uplink Credit Drop Experiment ===")
    print("Topology: receiver leaves 0,1,2,5 -> tor3 -> tor4(sender)")
    print(f"Shared credit bottleneck: tor{AGG_TOR} -> tor{SENDER}, port 4, slice 0")
    print(
        f"flows={len(plans)}, flows_per_receiver={args.flows_per_receiver}, "
        f"initial_credit={args.initial_credit}, qsize={args.flare_credit_qsize}"
    )
    print("Credit paths:")
    for plan in plans:
        print(f"  {plan.name}: {'->'.join(str(node) for node in plan.credit_path_nodes)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--flows-per-receiver", type=int, default=1)
    parser.add_argument("--flow-size", type=int, default=0,
                        help="0 means initial_credit * mtu")
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=32)
    parser.add_argument("--start", type=float, default=0.000105)
    parser.add_argument("--replica-gap-us", type=float, default=0.0)
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
        default=Path("results/flare_shared_uplink_credit_drop.json"),
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
    flow_rows.sort(key=lambda row: (row["receiver"], row["replica"]))
    tor_rows = _tor_rows(installed)
    summary = _summary(args, flow_size, flow_rows, tor_rows, plans)

    payload = {
        "experiment": "flare_shared_uplink_credit_drop",
        "parameters": dict(vars(args), resolved_flow_size=flow_size),
        "topology": _topology(),
        "routes": {
            f"{src}->{dst}@{ts}": list(path)
            for (src, dst, ts), path in sorted(nodes_by_slice.items())
        },
        "summary": summary,
        "flows": flow_rows,
        "tors": tor_rows,
    }
    _write_json(args.output, payload)
    _write_csv(args.output.with_suffix(".flows.csv"), flow_rows)
    _write_csv(args.output.with_suffix(".tors.csv"), tor_rows)

    print("Summary:")
    print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
    print(f"\nWrote {args.output}")
    print(f"Wrote {args.output.with_suffix('.flows.csv')}")
    print(f"Wrote {args.output.with_suffix('.tors.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
