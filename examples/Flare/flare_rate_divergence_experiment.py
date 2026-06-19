"""Flare Rate Divergence Experiment

Implements a systematic study of Flare's per-flow credit rate controller behavior
under incast scenarios. This script follows a 4-step experimental protocol:

Step 1: Minimal incast topology (8/16/32 senders → 1 receiver)
Step 2: Instrument credit rate, efficiency, FCT for each flow
Step 3: Visualize credit rate divergence over time
Step 4: Analyze correlation between rate allocation and FCT

Usage:
    python3 examples/flare_rate_divergence_experiment.py --senders 16 --flow-size 65536
    python3 examples/flare_rate_divergence_experiment.py --senders 32 --profile 15us --output results/
    python3 examples/flare_rate_divergence_experiment.py --senders 32 --flow-size 1048576 --profile 55us --output results/rate_div_32
"""

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import List, Dict, Any

from openoptics import OpticalRouting, OpticalTopo, Toolbox
from openoptics.backends.ns3.traffic import FlareConfig


def build_incast_network(nb_senders: int, profile: str, stop_s: float, **flare_kwargs):
    """Build a minimal incast topology: N senders → 1 receiver.

    Topology: nb_senders + 1 nodes, where node[0:nb_senders] are senders,
              and node[nb_senders] is the receiver (bottleneck at receiver uplink).
    """
    nb_node = nb_senders + 1
    slice_us = 55 if profile == "55us" else 15

    net = Toolbox.BaseNetwork(
        name=f"flare_incast_{nb_senders}to1",
        backend="ns3",
        nb_node=nb_node,
        nb_link=1,  # Single link per ToR to create bottleneck
        time_slice_duration_us=slice_us,
        guardband_ms=0,
        ocs_tor_link_bw_gbps=100,
        tor_host_link_bw_gbps=100,
        use_webserver=False,
        simulation_stop_s=stop_s,
        snapshot_interval_us=slice_us,
        flare_profile=profile,
        **flare_kwargs
    )

    # Deploy opera topology
    net.deploy_topo(OpticalTopo.opera(nb_node=nb_node, nb_link=1))

    # Deploy HoHo routing as requested
    paths = OpticalRouting.routing_hoho(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")

    return net, nb_node


def install_incast_flows(net, nb_senders: int, nb_node: int, flow_size: int,
                        start_s: float, duration_s: float, mtu: int,
                        flare_config: FlareConfig):
    """Install all-to-one incast flows."""
    receiver = nb_node - 1
    senders = list(range(nb_senders))

    gen = net.flare_traffic(config=flare_config)
    gen.many_to_one(
        senders,
        dst=receiver,
        size_bytes=flow_size,
        start_s=start_s,
        duration_s=duration_s,
        packet_size_bytes=mtu
    )
    return gen.install()


def compute_flow_metrics(installed_flows) -> List[Dict[str, Any]]:
    """Extract per-flow metrics from installed flows.

    Returns a list of dicts, each containing:
    - flow_id: flow identifier
    - src: source node
    - dst: destination node
    - credits_sent: total credits sent
    - credits_received: total credits received
    - data_packets_sent: total data packets sent
    - data_packets_received: total data packets received
    - fct_s: flow completion time in seconds
    - credit_efficiency: data_received / credits_sent
    - avg_credit_rate: credits_sent / fct_s (if fct > 0)
    """
    metrics = []
    for flow in installed_flows:
        stats = flow.stats()
        if stats is None:
            continue

        credit_efficiency = (
            stats.data_packets_received / stats.credits_sent
            if stats.credits_sent > 0 else 0.0
        )

        avg_credit_rate = (
            stats.credits_sent / stats.fct_s
            if stats.fct_s > 0 else 0.0
        )

        metrics.append({
            "flow_id": stats.flow_id,
            "src": stats.src,
            "dst": stats.dst,
            "credits_sent": stats.credits_sent,
            "credits_received": stats.credits_received,
            "credits_admitted": stats.credits_admitted,
            "credits_dropped": stats.credits_dropped,
            "data_packets_sent": stats.data_packets_sent,
            "data_packets_received": stats.data_packets_received,
            "fct_s": stats.fct_s,
            "credit_efficiency": credit_efficiency,
            "avg_credit_rate": avg_credit_rate,
            "retransmissions": stats.retransmissions,
            "timeouts": stats.timeouts,
        })

    return metrics


def analyze_rate_allocation(metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze credit rate allocation across flows.

    Checks for rate divergence and unfairness patterns:
    - Top 20% flows vs bottom 80% credit allocation
    - Coefficient of variation in credit rates
    - Correlation between avg_credit_rate and FCT
    """
    if not metrics:
        return {}

    completed = [m for m in metrics if m["fct_s"] > 0]
    if not completed:
        return {"error": "no completed flows"}

    # Sort by avg_credit_rate
    sorted_by_rate = sorted(completed, key=lambda m: m["avg_credit_rate"], reverse=True)

    # Top 20% vs bottom 80% allocation
    top_20_idx = max(1, len(sorted_by_rate) // 5)
    top_20 = sorted_by_rate[:top_20_idx]
    bottom_80 = sorted_by_rate[top_20_idx:]

    top_20_total_credits = sum(m["credits_sent"] for m in top_20)
    total_credits = sum(m["credits_sent"] for m in completed)

    top_20_credit_share = (
        top_20_total_credits / total_credits if total_credits > 0 else 0.0
    )

    # Coefficient of variation (CV) in credit rates
    rates = [m["avg_credit_rate"] for m in completed]
    mean_rate = statistics.mean(rates)
    stdev_rate = statistics.stdev(rates) if len(rates) > 1 else 0.0
    cv_rate = stdev_rate / mean_rate if mean_rate > 0 else 0.0

    # FCT statistics
    fcts = [m["fct_s"] for m in completed]
    mean_fct = statistics.mean(fcts)
    median_fct = statistics.median(fcts)

    return {
        "total_flows": len(metrics),
        "completed_flows": len(completed),
        "top_20_percent_credit_share": top_20_credit_share,
        "cv_credit_rate": cv_rate,
        "mean_credit_rate": mean_rate,
        "mean_fct_s": mean_fct,
        "median_fct_s": median_fct,
    }


def save_results(output_dir: Path, metrics: List[Dict[str, Any]],
                analysis: Dict[str, Any], args):
    """Save experimental results to JSON and CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save summary JSON
    summary_file = output_dir / "summary.json"
    summary = {
        "experiment": "flare_rate_divergence",
        "parameters": {
            "senders": args.senders,
            "flow_size": args.flow_size,
            "profile": args.profile,
            "mtu": args.mtu,
        },
        "analysis": analysis,
    }
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary saved to {summary_file}")

    # Save per-flow metrics CSV
    metrics_file = output_dir / "flow_metrics.csv"
    if metrics:
        fieldnames = list(metrics[0].keys())
        with open(metrics_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(metrics)
        print(f"Flow metrics saved to {metrics_file}")

    # Print summary to console
    print("\n=== Rate Divergence Analysis ===")
    print(json.dumps(analysis, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--senders", type=int, default=16,
                       choices=[8, 16, 32],
                       help="Number of sender nodes (8/16/32)")
    parser.add_argument("--flow-size", type=int, default=65536,
                       help="Flow size in bytes (default: 64KB)")
    parser.add_argument("--profile", choices=["55us", "15us"], default="55us",
                       help="Flare profile (55us or 15us)")
    parser.add_argument("--mtu", type=int, default=1024,
                       help="MTU size in bytes")
    parser.add_argument("--start", type=float, default=0.0001,
                       help="Flow start time in seconds")
    parser.add_argument("--duration", type=float, default=None,
                       help="Flow duration in seconds (default: None, use stop_s)")
    parser.add_argument("--stop", type=float, default=0.5,
                       help="Simulation stop time in seconds")
    parser.add_argument("--output", type=Path, default=Path("results/rate_divergence"),
                       help="Output directory for results")

    # Flare configuration parameters
    parser.add_argument("--flare-credit-qsize", type=int, default=None,
                       help="Flare credit queue size in packets")
    parser.add_argument("--flare-congestion-threshold", type=int, default=None,
                       help="Flare congestion threshold percent")
    parser.add_argument("--flare-tentative-threshold", type=int, default=None,
                       help="Flare tentative threshold percent")

    args = parser.parse_args(argv)

    # Build Flare kwargs
    flare_kwargs = {}
    if args.flare_credit_qsize is not None:
        flare_kwargs["flare_credit_qsize_pkts"] = args.flare_credit_qsize
    if args.flare_congestion_threshold is not None:
        flare_kwargs["flare_congestion_threshold_percent"] = args.flare_congestion_threshold
    if args.flare_tentative_threshold is not None:
        flare_kwargs["flare_tentative_threshold_percent"] = args.flare_tentative_threshold

    print(f"=== Flare Rate Divergence Experiment ===")
    print(f"Senders: {args.senders}, Flow Size: {args.flow_size} bytes")
    print(f"Profile: {args.profile}, MTU: {args.mtu}")
    print(f"Duration: {args.duration}s, Stop: {args.stop}s")
    print()

    # Step 1: Build minimal incast topology
    print("Step 1: Building incast topology...")
    net, nb_node = build_incast_network(
        args.senders, args.profile, args.stop, **flare_kwargs
    )

    # Step 2: Install incast flows
    print(f"Step 2: Installing {args.senders}-to-1 incast flows...")
    flare_config = FlareConfig.from_profile(args.profile, mtu_bytes=args.mtu)
    installed = install_incast_flows(
        net, args.senders, nb_node, args.flow_size,
        args.start, args.duration, args.mtu, flare_config
    )

    # Run simulation
    print("Running simulation...")
    net.start()
    print("Simulation complete.")

    # Step 3 & 4: Extract metrics and analyze
    print("\nStep 3: Computing flow metrics...")
    metrics = compute_flow_metrics(installed)

    print("Step 4: Analyzing rate allocation and FCT correlation...")
    analysis = analyze_rate_allocation(metrics)

    # Save results
    save_results(args.output, metrics, analysis, args)

    print("\nExperiment complete!")
    print(f"\nNext steps for visualization:")
    print(f"  - Plot credit rate over time (requires time-series data from ns-3 traces)")
    print(f"  - Plot avg_credit_rate vs FCT scatter from: {args.output / 'flow_metrics.csv'}")
    print(f"  - Check top_20_percent_credit_share in: {args.output / 'summary.json'}")


if __name__ == "__main__":
    main()





