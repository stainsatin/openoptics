"""Flare Paper Experiments Reproduction

Reproduces key experiments from the Flare paper:
"Unlocking Superior Performance in Reconfigurable Data Center Networks
with Credit-Based Transport" (SIGCOMM'25)

Experiments:
1. Microbenchmark - Basic performance validation
2. Incast - Many-to-one traffic pattern
3. All-to-all - Full mesh traffic pattern
4. Throughput vs Flow Size - Performance across flow sizes
5. FCT vs Load - Completion time under varying loads

Usage:
    # Run all experiments
    python examples/flare_paper_experiments.py --all --output results/paper_repro

    # Run specific experiment
    python examples/flare_paper_experiments.py --experiment microbench --output results/
    python examples/flare_paper_experiments.py --experiment incast --senders 16
    python examples/flare_paper_experiments.py --experiment alltoall --nodes 8
"""

import argparse
import csv
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional

from openoptics import OpticalRouting, OpticalTopo, Toolbox
from openoptics.backends.ns3.traffic import FlareConfig


@dataclass
class ExperimentResult:
    """Result from a single experiment run."""
    name: str
    config: Dict[str, Any]
    metrics: Dict[str, Any]
    flows: List[Dict[str, Any]]


# ============================================================================
# Experiment 1: Microbenchmark (Basic Performance Validation)
# ============================================================================

def exp_microbench(args) -> ExperimentResult:
    """Single flow throughput test.

    Validates basic Flare functionality:
    - Credit-data exchange works correctly
    - Achieves near-line-rate throughput
    - Low packet loss
    """
    print("\n=== Experiment 1: Microbenchmark ===")
    print(f"Flow size: {args.flow_size} bytes, Profile: {args.profile}")

    # Build simple 2-node topology
    net = Toolbox.BaseNetwork(
        name="flare_microbench",
        backend="ns3",
        nb_node=2,
        nb_link=1,
        time_slice_duration_us=55 if args.profile == "55us" else 15,
        guardband_ms=0,  # No guardband for Flare experiments
        simulation_stop_s=args.stop,
        flare_profile=args.profile,
        use_webserver=False,
    )

    # Use Opera topology as in the paper
    net.deploy_topo(OpticalTopo.opera(nb_node=2, nb_link=1))
    paths = OpticalRouting.routing_hoho(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")

    # Install single flow
    flare_config = FlareConfig.from_profile(args.profile, mtu_bytes=args.mtu)
    gen = net.flare_traffic(config=flare_config)
    gen.flow(
        src=0, dst=1,
        size_bytes=args.flow_size,
        start_s=0.001,
        packet_size_bytes=args.mtu,
    )
    flows = gen.install()

    print("Running simulation...")
    net.start()

    # Extract metrics
    stats = flows[0].stats()
    throughput_gbps = (stats.data_packets_received * args.mtu * 8) / stats.fct_s / 1e9 if stats.fct_s > 0 else 0

    result_metrics = {
        "fct_ms": stats.fct_s * 1000,
        "throughput_gbps": throughput_gbps,
        "packets_sent": stats.data_packets_sent,
        "packets_received": stats.data_packets_received,
        "packet_loss_rate": (stats.data_packets_sent - stats.data_packets_received) / stats.data_packets_sent if stats.data_packets_sent > 0 else 0,
        "credit_efficiency": stats.data_packets_received / stats.credits_sent if stats.credits_sent > 0 else 0,
        "retransmissions": stats.retransmissions,
    }

    print(f"FCT: {result_metrics['fct_ms']:.2f} ms")
    print(f"Throughput: {result_metrics['throughput_gbps']:.2f} Gbps")
    print(f"Packet loss: {result_metrics['packet_loss_rate']:.4f}")

    return ExperimentResult(
        name="microbench",
        config={"flow_size": args.flow_size, "profile": args.profile},
        metrics=result_metrics,
        flows=[_stats_to_dict(stats)],
    )


# ============================================================================
# Experiment 2: Incast (Many-to-One)
# ============================================================================

def exp_incast(args) -> ExperimentResult:
    """Incast traffic pattern: N senders → 1 receiver.

    Tests:
    - Congestion control under bottleneck
    - Credit admission fairness
    - Queue management
    """
    print(f"\n=== Experiment 2: Incast ({args.senders}-to-1) ===")
    print(f"Flow size: {args.flow_size} bytes, Profile: {args.profile}")

    nb_node = args.senders + 1
    slice_us = 55 if args.profile == "55us" else 15

    net = Toolbox.BaseNetwork(
        name=f"flare_incast_{args.senders}to1",
        backend="ns3",
        nb_node=nb_node,
        nb_link=1,
        time_slice_duration_us=slice_us,
        guardband_ms=0,  # No guardband for Flare experiments
        simulation_stop_s=args.stop,
        flare_profile=args.profile,
        use_webserver=False,
    )

    # Use Opera topology as in the paper
    net.deploy_topo(OpticalTopo.opera(nb_node=nb_node, nb_link=1))
    paths = OpticalRouting.routing_hoho(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")

    # Install incast flows
    receiver = nb_node - 1
    senders = list(range(args.senders))

    flare_config = FlareConfig.from_profile(args.profile, mtu_bytes=args.mtu)
    gen = net.flare_traffic(config=flare_config)
    gen.many_to_one(
        senders, dst=receiver,
        size_bytes=args.flow_size,
        start_s=0.001,
        packet_size_bytes=args.mtu,
    )
    flows = gen.install()

    print("Running simulation...")
    net.start()

    # Analyze results
    completed = [f.stats() for f in flows if f.stats().fct_s > 0]
    if not completed:
        print("⚠️  Warning: No flows completed!")
        return ExperimentResult(
            name="incast",
            config={"senders": args.senders, "flow_size": args.flow_size},
            metrics={"error": "no completed flows"},
            flows=[],
        )

    fcts = [s.fct_s * 1000 for s in completed]
    throughputs = [(s.data_packets_received * args.mtu * 8) / s.fct_s / 1e9 for s in completed if s.fct_s > 0]

    result_metrics = {
        "completed_flows": len(completed),
        "total_flows": len(flows),
        "mean_fct_ms": statistics.mean(fcts),
        "median_fct_ms": statistics.median(fcts),
        "p99_fct_ms": _percentile(fcts, 99),
        "mean_throughput_gbps": statistics.mean(throughputs) if throughputs else 0,
        "total_throughput_gbps": sum(throughputs),
    }

    print(f"Completed: {result_metrics['completed_flows']}/{result_metrics['total_flows']}")
    print(f"Mean FCT: {result_metrics['mean_fct_ms']:.2f} ms")
    print(f"P99 FCT: {result_metrics['p99_fct_ms']:.2f} ms")

    return ExperimentResult(
        name="incast",
        config={"senders": args.senders, "flow_size": args.flow_size},
        metrics=result_metrics,
        flows=[_stats_to_dict(s) for s in completed],
    )


# ============================================================================
# Experiment 3: All-to-All
# ============================================================================

def exp_alltoall(args) -> ExperimentResult:
    """All-to-all traffic pattern.

    Tests:
    - Performance under uniform load
    - Path diversity utilization
    - Fairness across all node pairs
    """
    print(f"\n=== Experiment 3: All-to-All ({args.nodes} nodes) ===")
    print(f"Flow size: {args.flow_size} bytes, Profile: {args.profile}")

    slice_us = 55 if args.profile == "55us" else 15

    net = Toolbox.BaseNetwork(
        name=f"flare_alltoall_{args.nodes}",
        backend="ns3",
        nb_node=args.nodes,
        nb_link=2,  # More links for all-to-all
        time_slice_duration_us=slice_us,
        guardband_ms=0,  # No guardband for Flare experiments
        simulation_stop_s=args.stop,
        flare_profile=args.profile,
        use_webserver=False,
    )

    # Use Opera topology as in the paper (with more links for better connectivity)
    net.deploy_topo(OpticalTopo.opera(nb_node=args.nodes, nb_link=2))
    paths = OpticalRouting.routing_hoho(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")

    # Install all-to-all flows
    flare_config = FlareConfig.from_profile(args.profile, mtu_bytes=args.mtu)
    gen = net.flare_traffic(config=flare_config)
    gen.all_to_all(
        size_bytes=args.flow_size,
        start_s=0.001,
        packet_size_bytes=args.mtu,
        include_self=False,
    )
    flows = gen.install()

    print(f"Total flows: {len(flows)} ({args.nodes} × {args.nodes - 1})")
    print("Running simulation...")
    net.start()

    # Analyze results
    completed = [f.stats() for f in flows if f.stats().fct_s > 0]
    if not completed:
        print("⚠️  Warning: No flows completed!")
        return ExperimentResult(
            name="alltoall",
            config={"nodes": args.nodes, "flow_size": args.flow_size},
            metrics={"error": "no completed flows"},
            flows=[],
        )

    fcts = [s.fct_s * 1000 for s in completed]
    result_metrics = {
        "completed_flows": len(completed),
        "total_flows": len(flows),
        "mean_fct_ms": statistics.mean(fcts),
        "median_fct_ms": statistics.median(fcts),
        "p50_fct_ms": _percentile(fcts, 50),
        "p99_fct_ms": _percentile(fcts, 99),
        "cv_fct": statistics.stdev(fcts) / statistics.mean(fcts) if len(fcts) > 1 else 0,
    }

    print(f"Completed: {result_metrics['completed_flows']}/{result_metrics['total_flows']}")
    print(f"Mean FCT: {result_metrics['mean_fct_ms']:.2f} ms")
    print(f"Median FCT: {result_metrics['median_fct_ms']:.2f} ms")
    print(f"P99 FCT: {result_metrics['p99_fct_ms']:.2f} ms")

    return ExperimentResult(
        name="alltoall",
        config={"nodes": args.nodes, "flow_size": args.flow_size},
        metrics=result_metrics,
        flows=[_stats_to_dict(s) for s in completed],
    )


# ============================================================================
# Experiment 4: Throughput vs Flow Size
# ============================================================================

def exp_throughput_vs_size(args) -> ExperimentResult:
    """Measure throughput across different flow sizes.

    Tests:
    - Performance for short vs long flows
    - Fast start effectiveness
    - Credit window adaptation
    """
    print("\n=== Experiment 4: Throughput vs Flow Size ===")

    # Test different flow sizes
    flow_sizes = [
        16 * 1024,      # 16 KB (short)
        64 * 1024,      # 64 KB
        256 * 1024,     # 256 KB
        1024 * 1024,    # 1 MB
        4 * 1024 * 1024,  # 4 MB (long)
    ]

    results = []
    for size_bytes in flow_sizes:
        print(f"\nTesting flow size: {size_bytes / 1024:.0f} KB")

        # Adjust simulation time based on flow size
        stop_s = max(0.1, size_bytes / (100 * 1e9 / 8) * 100)  # Conservative estimate

        net = Toolbox.BaseNetwork(
            name=f"flare_throughput_{size_bytes}",
            backend="ns3",
            nb_node=2,
            nb_link=1,
            time_slice_duration_us=55 if args.profile == "55us" else 15,
            guardband_ms=0,  # No guardband for Flare experiments
            simulation_stop_s=stop_s,
            flare_profile=args.profile,
            use_webserver=False,
        )

        # Use Opera topology as in the paper
        net.deploy_topo(OpticalTopo.opera(nb_node=2, nb_link=1))
        paths = OpticalRouting.routing_hoho(net.get_topo())
        net.deploy_routing(paths, routing_mode="Per-hop")

        flare_config = FlareConfig.from_profile(args.profile, mtu_bytes=args.mtu)
        gen = net.flare_traffic(config=flare_config)
        gen.flow(src=0, dst=1, size_bytes=size_bytes, start_s=0.001, packet_size_bytes=args.mtu)
        flows = gen.install()

        net.start()

        stats = flows[0].stats()
        throughput_gbps = (stats.data_packets_received * args.mtu * 8) / stats.fct_s / 1e9 if stats.fct_s > 0 else 0

        result = {
            "flow_size_kb": size_bytes / 1024,
            "fct_ms": stats.fct_s * 1000,
            "throughput_gbps": throughput_gbps,
            "link_utilization": throughput_gbps / 100.0,  # Assuming 100 Gbps link
        }
        results.append(result)

        print(f"  FCT: {result['fct_ms']:.2f} ms, Throughput: {result['throughput_gbps']:.2f} Gbps")

    # Aggregate metrics
    aggregate_metrics = {
        "flow_sizes_tested": len(flow_sizes),
        "mean_throughput_gbps": statistics.mean([r["throughput_gbps"] for r in results]),
        "mean_utilization": statistics.mean([r["link_utilization"] for r in results]),
    }

    return ExperimentResult(
        name="throughput_vs_size",
        config={"profile": args.profile},
        metrics=aggregate_metrics,
        flows=results,
    )


# ============================================================================
# Experiment 5: FCT vs Load
# ============================================================================

def exp_fct_vs_load(args) -> ExperimentResult:
    """Measure FCT under varying network loads.

    Tests:
    - Performance degradation under load
    - Queue buildup behavior
    - Rate control effectiveness
    """
    print("\n=== Experiment 5: FCT vs Load ===")

    # Test different loads (number of concurrent flows)
    loads = [2, 4, 8, 16]

    results = []
    for num_flows in loads:
        print(f"\nTesting load: {num_flows} concurrent flows")

        nb_node = num_flows + 1

        # Adjust simulation time based on load
        stop_s = max(0.2, num_flows * 0.05)

        net = Toolbox.BaseNetwork(
            name=f"flare_load_{num_flows}",
            backend="ns3",
            nb_node=nb_node,
            nb_link=1,
            time_slice_duration_us=55 if args.profile == "55us" else 15,
            guardband_ms=0,  # No guardband for Flare experiments
            simulation_stop_s=stop_s,
            flare_profile=args.profile,
            use_webserver=False,
        )

        # Use Opera topology as in the paper
        net.deploy_topo(OpticalTopo.opera(nb_node=nb_node, nb_link=1))
        paths = OpticalRouting.routing_hoho(net.get_topo())
        net.deploy_routing(paths, routing_mode="Per-hop")

        # Install incast flows
        receiver = nb_node - 1
        senders = list(range(num_flows))

        flare_config = FlareConfig.from_profile(args.profile, mtu_bytes=args.mtu)
        gen = net.flare_traffic(config=flare_config)
        gen.many_to_one(
            senders, dst=receiver,
            size_bytes=args.flow_size,
            start_s=0.001,
            packet_size_bytes=args.mtu,
        )
        flows = gen.install()

        net.start()

        # Analyze
        completed = [f.stats() for f in flows if f.stats().fct_s > 0]
        if completed:
            fcts = [s.fct_s * 1000 for s in completed]
            result = {
                "load_num_flows": num_flows,
                "completed": len(completed),
                "mean_fct_ms": statistics.mean(fcts),
                "median_fct_ms": statistics.median(fcts),
                "p99_fct_ms": _percentile(fcts, 99),
            }
        else:
            result = {
                "load_num_flows": num_flows,
                "completed": 0,
                "mean_fct_ms": 0,
                "median_fct_ms": 0,
                "p99_fct_ms": 0,
            }

        results.append(result)
        print(f"  Mean FCT: {result['mean_fct_ms']:.2f} ms, Completed: {result['completed']}/{num_flows}")

    # Aggregate
    aggregate_metrics = {
        "loads_tested": loads,
        "results": results,
    }

    return ExperimentResult(
        name="fct_vs_load",
        config={"flow_size": args.flow_size},
        metrics=aggregate_metrics,
        flows=results,
    )


# ============================================================================
# Helper Functions
# ============================================================================

def _stats_to_dict(stats) -> Dict[str, Any]:
    """Convert FlareStats to dictionary."""
    return {
        "flow_id": stats.flow_id,
        "src": stats.src,
        "dst": stats.dst,
        "fct_s": stats.fct_s,
        "credits_sent": stats.credits_sent,
        "data_packets_received": stats.data_packets_received,
        "retransmissions": stats.retransmissions,
    }


def _percentile(data: List[float], p: float) -> float:
    """Calculate percentile."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    index = int(len(sorted_data) * p / 100.0)
    return sorted_data[min(index, len(sorted_data) - 1)]


def save_results(output_dir: Path, result: ExperimentResult):
    """Save experiment results to JSON and CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save summary JSON
    summary_file = output_dir / f"{result.name}_summary.json"
    summary = {
        "experiment": result.name,
        "config": result.config,
        "metrics": result.metrics,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary saved to {summary_file}")

    # Save flow details CSV
    if result.flows:
        csv_file = output_dir / f"{result.name}_flows.csv"
        fieldnames = list(result.flows[0].keys())
        with open(csv_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(result.flows)
        print(f"Flow details saved to {csv_file}")


# ============================================================================
# Main
# ============================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Experiment selection
    parser.add_argument("--experiment", choices=["microbench", "incast", "alltoall", "throughput", "load", "all"],
                       default="all", help="Experiment to run")
    parser.add_argument("--all", action="store_true", help="Run all experiments")

    # Common parameters
    parser.add_argument("--profile", choices=["55us", "15us"], default="55us",
                       help="Flare profile")
    parser.add_argument("--mtu", type=int, default=1536,
                       help="MTU size in bytes (default: 1536 as in paper)")
    parser.add_argument("--flow-size", type=int, default=65536,
                       help="Flow size in bytes (default: 64KB)")
    parser.add_argument("--stop", type=float, default=0.5,
                       help="Simulation stop time in seconds")
    parser.add_argument("--output", type=Path, default=Path("results/paper_repro"),
                       help="Output directory for results")

    # Experiment-specific parameters
    parser.add_argument("--senders", type=int, default=16,
                       help="Number of senders for incast experiment")
    parser.add_argument("--nodes", type=int, default=8,
                       help="Number of nodes for all-to-all experiment")

    args = parser.parse_args(argv)

    # Determine which experiments to run
    experiments = []
    if args.all or args.experiment == "all":
        experiments = ["microbench", "incast", "alltoall", "throughput", "load"]
    else:
        experiments = [args.experiment]

    print("=" * 70)
    print("Flare Paper Experiments Reproduction")
    print("=" * 70)
    print(f"Experiments: {', '.join(experiments)}")
    print(f"Output directory: {args.output}")
    print()

    # Run experiments
    results = []
    for exp_name in experiments:
        try:
            if exp_name == "microbench":
                result = exp_microbench(args)
            elif exp_name == "incast":
                result = exp_incast(args)
            elif exp_name == "alltoall":
                result = exp_alltoall(args)
            elif exp_name == "throughput":
                result = exp_throughput_vs_size(args)
            elif exp_name == "load":
                result = exp_fct_vs_load(args)
            else:
                continue

            results.append(result)
            save_results(args.output, result)

        except Exception as e:
            print(f"⚠️  Error in {exp_name}: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 70)
    print("All experiments completed!")
    print(f"Results saved to: {args.output}")
    print("=" * 70)


if __name__ == "__main__":
    main()
