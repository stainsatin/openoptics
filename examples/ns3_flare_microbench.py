"""Run small Flare ns-3 microbenchmarks and export JSON/CSV results.

The goal is not to reproduce every paper figure in one script. This is the
next layer above unit tests: quick, repeatable Flare scenarios that exercise
single-flow completion, incast pressure, dual-bottleneck pressure, credit
queue pressure, slice switching, path mismatch, and hop-count diversity under
the existing OpenOptics ns-3 harness.

Examples:

    python examples/ns3_flare_microbench.py --scenario single --profile 55us
    python examples/ns3_flare_microbench.py --scenario incast --profile 15us \
        --output results/flare_incast.json --csv results/flare_incast.csv
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable

from openoptics import OpticalRouting, OpticalTopo, Toolbox
from openoptics.backends.ns3.traffic import FlareConfig, FlareStats


_SCENARIOS = (
    "single",
    "incast",
    "credit-pressure",
    "dual-bottleneck",
    "path-mismatch",
    "slice-switch",
    "hop-count",
)


def _slice_us(profile: str) -> int:
    return 55 if profile == "55us" else 15


def _effective_topology(args) -> str:
    if args.topology != "auto":
        return args.topology
    if args.scenario in {"path-mismatch", "hop-count"}:
        return "opera"
    return "round-robin"


def _effective_routing(args) -> str:
    if args.routing != "auto":
        return args.routing
    if args.scenario in {"path-mismatch", "hop-count"}:
        return "hoho"
    return "direct"


def _build_network(args):
    slice_us = _slice_us(args.profile)
    topology = _effective_topology(args)
    routing = _effective_routing(args)
    net = Toolbox.BaseNetwork(
        name=f"ns3_flare_{args.scenario}_{args.profile}",
        backend="ns3",
        nb_node=args.nodes,
        nb_link=args.links,
        time_slice_duration_us=slice_us,
        guardband_ms=0,
        ocs_tor_link_bw_gbps=args.ocs_bw,
        tor_host_link_bw_gbps=args.host_bw,
        use_webserver=args.dashboard,
        simulation_stop_s=args.stop,
        snapshot_interval_us=max(1, slice_us),
        flare_profile=args.profile,
    )
    if topology == "opera":
        topo = OpticalTopo.opera(nb_node=args.nodes, nb_link=args.links)
    else:
        topo = OpticalTopo.round_robin(nb_node=args.nodes)
    net.deploy_topo(topo)

    if routing == "hoho":
        paths = OpticalRouting.routing_hoho(net.get_topo())
    else:
        paths = OpticalRouting.routing_direct(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")
    return net


def _install_flows(net, args):
    cfg = FlareConfig.from_profile(
        args.profile,
        mtu_bytes=args.mtu,
        initial_credit_pkts=args.initial_credit if args.initial_credit else None,
    )
    gen = net.flare_traffic(config=cfg)
    if args.scenario == "single":
        gen.flow(0, min(1, args.nodes - 1), args.flow_size,
                 start_s=args.start, duration_s=args.duration,
                 packet_size_bytes=args.mtu)
    elif args.scenario == "incast":
        dst = args.nodes - 1
        sources = range(max(0, dst - args.fan_in), dst)
        gen.many_to_one(sources, dst=dst, size_bytes=args.flow_size,
                        start_s=args.start, duration_s=args.duration,
                        packet_size_bytes=args.mtu)
    elif args.scenario == "credit-pressure":
        dst = min(1, args.nodes - 1)
        for i in range(args.flows):
            gen.flow(0, dst, args.flow_size, start_s=args.start,
                     duration_s=args.duration, packet_size_bytes=args.mtu,
                     name=f"pressure-{i}")
    elif args.scenario == "dual-bottleneck":
        if args.nodes < 4:
            raise ValueError("dual-bottleneck requires --nodes >= 4")
        pairs = [(0, 2), (1, 3)]
        for i in range(args.flows):
            src, dst = pairs[i % len(pairs)]
            gen.flow(src, dst, args.flow_size, start_s=args.start,
                     duration_s=args.duration, packet_size_bytes=args.mtu,
                     name=f"dual-{i}-{src}-{dst}")
    elif args.scenario == "path-mismatch":
        if args.nodes < 4:
            raise ValueError("path-mismatch requires --nodes >= 4")
        # Stagger flows across several slice boundaries on an Opera/HoHo
        # default. This stresses credits that were admitted under one
        # slice/path but whose data may arrive after the topology changes.
        slice_s = _slice_us(args.profile) / 1e6
        pairs = [(0, args.nodes // 2), (args.nodes // 2, 1), (1, args.nodes - 1)]
        for i in range(args.flows):
            src, dst = pairs[i % len(pairs)]
            gen.flow(src, dst, args.flow_size,
                     start_s=args.start + (i % 4) * slice_s * 0.75,
                     duration_s=args.duration,
                     packet_size_bytes=args.mtu,
                     name=f"mismatch-{i}-{src}-{dst}")
    elif args.scenario == "slice-switch":
        if args.nodes < 2:
            raise ValueError("slice-switch requires --nodes >= 2")
        # Launch around the active slice boundary to expose late-rollover
        # and credit refresh behavior.
        slice_s = _slice_us(args.profile) / 1e6
        offsets = (0.05, 0.85, 1.05, 1.85)
        for i in range(args.flows):
            gen.flow(0, min(1, args.nodes - 1), args.flow_size,
                     start_s=args.start + offsets[i % len(offsets)] * slice_s,
                     duration_s=args.duration,
                     packet_size_bytes=args.mtu,
                     name=f"slice-{i}")
    elif args.scenario == "hop-count":
        if args.nodes < 4:
            raise ValueError("hop-count requires --nodes >= 4")
        # Use a mix of near/far pairs. With Opera/HoHo defaults this tends
        # to exercise direct and relayed paths without hard-coding topology
        # internals into the workload generator.
        pairs = [
            (0, 1),
            (0, args.nodes // 2),
            (1, args.nodes - 1),
            (args.nodes // 2, args.nodes - 1),
        ]
        for i, (src, dst) in enumerate(pairs[:args.flows]):
            if src != dst:
                gen.flow(src, dst, args.flow_size,
                         start_s=args.start + i * _slice_us(args.profile) / 1e6,
                         duration_s=args.duration,
                         packet_size_bytes=args.mtu,
                         name=f"hop-{i}-{src}-{dst}")
    else:
        raise ValueError(f"unknown scenario {args.scenario!r}")
    return gen.install()


def _stats_to_row(stats: FlareStats) -> dict:
    hist = dict(stats.path_length_histogram or {})
    return {
        "flow_id": stats.flow_id,
        "src": stats.src,
        "dst": stats.dst,
        "fct_s": stats.fct_s,
        "throughput_bps": stats.throughput_bps,
        "data_packets_sent": stats.data_packets_sent,
        "data_packets_received": stats.data_packets_received,
        "credits_sent": stats.credits_sent,
        "credits_received": stats.credits_received,
        "credits_admitted": stats.credits_admitted,
        "credits_dropped": stats.credits_dropped,
        "credits_wasted": stats.credits_wasted,
        "retransmissions": stats.retransmissions,
        "timeouts": stats.timeouts,
        "duplicate_data": stats.duplicate_data,
        "duplicate_credits": stats.duplicate_credits,
        "path_len_1": hist.get(1, 0),
        "path_len_2": hist.get(2, 0),
        "path_len_3": hist.get(3, 0),
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[dict]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def _finite(values):
    return [v for v in values if isinstance(v, (int, float)) and v == v]


def _percentile(values, pct: float):
    values = sorted(_finite(values))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * pct
    lo = int(rank)
    hi = min(lo + 1, len(values) - 1)
    weight = rank - lo
    return values[lo] * (1.0 - weight) + values[hi] * weight


def _safe_ratio(num: float, den: float):
    return None if den == 0 else num / den


def _summarize_rows(args, rows: list[dict]) -> dict:
    total_credits_sent = sum(r["credits_sent"] for r in rows)
    total_credits_admitted = sum(r["credits_admitted"] for r in rows)
    total_credits_dropped = sum(r["credits_dropped"] for r in rows)
    total_credits_wasted = sum(r["credits_wasted"] for r in rows)
    completed_rows = [
        r for r in rows
        if r["fct_s"] == r["fct_s"] and r["fct_s"] > 0
    ]
    fcts = [r["fct_s"] for r in completed_rows]
    return {
        "scenario": args.scenario,
        "profile": args.profile,
        "topology": _effective_topology(args),
        "routing": _effective_routing(args),
        "nodes": args.nodes,
        "links": args.links,
        "flow_count": len(rows),
        "completed_flow_count": len(completed_rows),
        "completion_fraction": _safe_ratio(len(completed_rows), len(rows)),
        "fct_median_s": _percentile(fcts, 0.50),
        "fct_p95_s": _percentile(fcts, 0.95),
        "fct_p99_s": _percentile(fcts, 0.99),
        "throughput_sum_bps": sum(_finite([r["throughput_bps"] for r in rows])),
        "total_data_packets_sent": sum(r["data_packets_sent"] for r in rows),
        "total_data_packets_received": sum(r["data_packets_received"] for r in rows),
        "total_credits_sent": total_credits_sent,
        "total_credits_received": sum(r["credits_received"] for r in rows),
        "total_credits_admitted": total_credits_admitted,
        "total_credits_dropped": total_credits_dropped,
        "total_credits_wasted": total_credits_wasted,
        "credit_drop_fraction": _safe_ratio(total_credits_dropped, total_credits_sent),
        "credit_waste_fraction": _safe_ratio(total_credits_wasted, total_credits_admitted),
        "total_retransmissions": sum(r["retransmissions"] for r in rows),
        "total_timeouts": sum(r["timeouts"] for r in rows),
        "total_duplicate_data": sum(r["duplicate_data"] for r in rows),
        "total_duplicate_credits": sum(r["duplicate_credits"] for r in rows),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=_SCENARIOS, default="single")
    parser.add_argument("--profile", choices=["55us", "15us"], default="55us")
    parser.add_argument("--topology", choices=["auto", "round-robin", "opera"],
                        default="auto")
    parser.add_argument("--routing", choices=["auto", "direct", "hoho"],
                        default="auto")
    parser.add_argument("--nodes", type=int, default=4)
    parser.add_argument("--links", type=int, default=1)
    parser.add_argument("--flow-size", type=int, default=64_000)
    parser.add_argument("--flows", type=int, default=8,
                        help="number of flows for credit-pressure")
    parser.add_argument("--fan-in", type=int, default=3,
                        help="number of sources for incast")
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=0)
    parser.add_argument("--start", type=float, default=0.0001)
    parser.add_argument("--duration", type=float, default=0.02)
    parser.add_argument("--stop", type=float, default=0.05)
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("flare_microbench.json"))
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args(argv)

    net = _build_network(args)
    installed = _install_flows(net, args)
    net.start()

    rows = []
    for flow in installed:
        stats = flow.stats()
        if stats is not None:
            rows.append(_stats_to_row(stats))

    summary = _summarize_rows(args, rows)
    payload = {"summary": summary, "flows": rows}
    _write_json(args.output, payload)
    if args.csv is not None:
        _write_csv(args.csv, rows)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
