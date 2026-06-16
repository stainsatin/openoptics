"""Plot Flare Paper Experiment Results

Generates publication-quality plots for Flare experiments based on paper figures:
- Figure 9: Throughput breakdown (PA + RC + Tentative)
- Figure 11: Probability functions comparison
- Figure 12: Tentative credits effect
- Figure 14-17: FCT comparisons (Web Search, Hadoop, RPC)
- Figure 18-19: Opera vs Clos comparison

Usage:
    python examples/plot_paper_experiments.py --input results/paper_repro --output plots/
    python examples/plot_paper_experiments.py --experiment microbench --input results/
"""

import argparse
import json
import csv
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from typing import List, Dict, Any, Optional

# Set publication-quality defaults
plt.rcParams['figure.figsize'] = (8, 6)
plt.rcParams['font.size'] = 12
plt.rcParams['axes.labelsize'] = 14
plt.rcParams['axes.titlesize'] = 16
plt.rcParams['xtick.labelsize'] = 11
plt.rcParams['ytick.labelsize'] = 11
plt.rcParams['legend.fontsize'] = 11
plt.rcParams['figure.dpi'] = 100
plt.rcParams['savefig.dpi'] = 300
plt.rcParams['savefig.bbox'] = 'tight'


def load_json(file_path: Path) -> Dict[str, Any]:
    """Load JSON result file."""
    with open(file_path, 'r') as f:
        return json.load(f)


def load_csv(file_path: Path) -> List[Dict[str, Any]]:
    """Load CSV result file."""
    rows = []
    with open(file_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Convert numeric strings to numbers
            converted = {}
            for k, v in row.items():
                try:
                    converted[k] = float(v)
                except (ValueError, TypeError):
                    converted[k] = v
            rows.append(converted)
    return rows


# ============================================================================
# Figure 1: Microbenchmark Results
# ============================================================================

def plot_microbench(input_dir: Path, output_dir: Path):
    """Plot microbenchmark results: throughput and FCT."""
    summary = load_json(input_dir / "microbench_summary.json")
    metrics = summary.get("metrics", {})

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Throughput bar
    throughput = metrics.get("throughput_gbps", 0)
    ax1.bar(["Flare"], [throughput], color='steelblue', width=0.5)
    ax1.axhline(y=100, color='red', linestyle='--', label='Line Rate (100 Gbps)')
    ax1.set_ylabel("Throughput (Gbps)")
    ax1.set_title("Single Flow Throughput")
    ax1.set_ylim(0, 110)
    ax1.legend()
    ax1.grid(axis='y', alpha=0.3)

    # FCT bar
    fct_ms = metrics.get("fct_ms", 0)
    ax2.bar(["Flare"], [fct_ms], color='coral', width=0.5)
    ax2.set_ylabel("Flow Completion Time (ms)")
    ax2.set_title("Single Flow FCT")
    ax2.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    output_file = output_dir / "microbench.png"
    plt.savefig(output_file)
    print(f"Saved: {output_file}")
    plt.close()


# ============================================================================
# Figure 2: Incast Performance
# ============================================================================

def plot_incast(input_dir: Path, output_dir: Path):
    """Plot incast experiment results: FCT distribution."""
    summary = load_json(input_dir / "incast_summary.json")
    flows = load_csv(input_dir / "incast_flows.csv")

    if not flows:
        print("Warning: No flow data for incast")
        return

    fcts_ms = [f["fct_s"] * 1000 for f in flows]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # FCT histogram
    ax1.hist(fcts_ms, bins=20, alpha=0.7, color='steelblue', edgecolor='black')
    ax1.axvline(np.mean(fcts_ms), color='red', linestyle='--',
                label=f'Mean: {np.mean(fcts_ms):.2f} ms')
    ax1.axvline(np.median(fcts_ms), color='orange', linestyle='--',
                label=f'Median: {np.median(fcts_ms):.2f} ms')
    ax1.set_xlabel("Flow Completion Time (ms)")
    ax1.set_ylabel("Number of Flows")
    ax1.set_title("Incast FCT Distribution")
    ax1.legend()
    ax1.grid(axis='y', alpha=0.3)

    # FCT CDF
    sorted_fcts = sorted(fcts_ms)
    cdf = np.arange(1, len(sorted_fcts) + 1) / len(sorted_fcts)
    ax2.plot(sorted_fcts, cdf, linewidth=2, color='steelblue')
    ax2.set_xlabel("Flow Completion Time (ms)")
    ax2.set_ylabel("CDF")
    ax2.set_title("Incast FCT CDF")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = output_dir / "incast_performance.png"
    plt.savefig(output_file)
    print(f"Saved: {output_file}")
    plt.close()


# ============================================================================
# Figure 3: All-to-All Performance
# ============================================================================

def plot_alltoall(input_dir: Path, output_dir: Path):
    """Plot all-to-all experiment results."""
    summary = load_json(input_dir / "alltoall_summary.json")
    flows = load_csv(input_dir / "alltoall_flows.csv")

    if not flows:
        print("Warning: No flow data for all-to-all")
        return

    fcts_ms = [f["fct_s"] * 1000 for f in flows]
    metrics = summary.get("metrics", {})

    fig, ax = plt.subplots(figsize=(10, 6))

    # Box plot
    bp = ax.boxplot([fcts_ms], labels=["All-to-All"], widths=0.5, patch_artist=True)
    bp['boxes'][0].set_facecolor('lightblue')

    # Add statistics text
    stats_text = (
        f"Mean: {metrics.get('mean_fct_ms', 0):.2f} ms\n"
        f"Median: {metrics.get('median_fct_ms', 0):.2f} ms\n"
        f"P99: {metrics.get('p99_fct_ms', 0):.2f} ms\n"
        f"CV: {metrics.get('cv_fct', 0):.3f}"
    )
    ax.text(1.3, np.median(fcts_ms), stats_text, fontsize=10,
            bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    ax.set_ylabel("Flow Completion Time (ms)")
    ax.set_title("All-to-All Traffic Pattern Performance")
    ax.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    output_file = output_dir / "alltoall_performance.png"
    plt.savefig(output_file)
    print(f"Saved: {output_file}")
    plt.close()


# ============================================================================
# Figure 4: Throughput vs Flow Size (like Figure 9 in paper)
# ============================================================================

def plot_throughput_vs_size(input_dir: Path, output_dir: Path):
    """Plot throughput vs flow size."""
    flows = load_csv(input_dir / "throughput_vs_size_flows.csv")

    if not flows:
        print("Warning: No flow data for throughput vs size")
        return

    flow_sizes = [f["flow_size_kb"] for f in flows]
    throughputs = [f["throughput_gbps"] for f in flows]
    utilizations = [f["link_utilization"] for f in flows]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # Throughput
    ax1.plot(flow_sizes, throughputs, marker='o', linewidth=2,
             markersize=8, color='steelblue', label='Flare')
    ax1.axhline(y=100, color='red', linestyle='--', alpha=0.5, label='Line Rate')
    ax1.set_xlabel("Flow Size (KB)")
    ax1.set_ylabel("Throughput (Gbps)")
    ax1.set_title("Throughput vs Flow Size")
    ax1.set_xscale('log')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Link utilization
    ax2.plot(flow_sizes, [u * 100 for u in utilizations], marker='s',
             linewidth=2, markersize=8, color='coral', label='Flare')
    ax2.axhline(y=100, color='red', linestyle='--', alpha=0.5, label='100% Util')
    ax2.set_xlabel("Flow Size (KB)")
    ax2.set_ylabel("Link Utilization (%)")
    ax2.set_title("Link Utilization vs Flow Size")
    ax2.set_xscale('log')
    ax2.set_ylim(0, 110)
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = output_dir / "throughput_vs_size.png"
    plt.savefig(output_file)
    print(f"Saved: {output_file}")
    plt.close()


# ============================================================================
# Figure 5: FCT vs Load (like Figure 14-17 in paper)
# ============================================================================

def plot_fct_vs_load(input_dir: Path, output_dir: Path):
    """Plot FCT vs network load."""
    flows = load_csv(input_dir / "fct_vs_load_flows.csv")

    if not flows:
        print("Warning: No flow data for FCT vs load")
        return

    loads = [f["load_num_flows"] for f in flows]
    mean_fcts = [f["mean_fct_ms"] for f in flows]
    median_fcts = [f["median_fct_ms"] for f in flows]
    p99_fcts = [f["p99_fct_ms"] for f in flows]

    fig, ax = plt.subplots(figsize=(10, 6))

    ax.plot(loads, mean_fcts, marker='o', linewidth=2, label='Mean FCT')
    ax.plot(loads, median_fcts, marker='s', linewidth=2, label='Median FCT')
    ax.plot(loads, p99_fcts, marker='^', linewidth=2, label='P99 FCT')

    ax.set_xlabel("Number of Concurrent Flows (Load)")
    ax.set_ylabel("Flow Completion Time (ms)")
    ax.set_title("FCT vs Network Load")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = output_dir / "fct_vs_load.png"
    plt.savefig(output_file)
    print(f"Saved: {output_file}")
    plt.close()


# ============================================================================
# Main
# ============================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", type=Path, required=True,
                       help="Input directory containing experiment results")
    parser.add_argument("--output", type=Path, default=None,
                       help="Output directory for plots (default: same as input)")
    parser.add_argument("--experiment", choices=["microbench", "incast", "alltoall",
                                                  "throughput", "load", "all"],
                       default="all", help="Which experiment to plot")

    args = parser.parse_args(argv)

    output_dir = args.output if args.output else args.input
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("Flare Paper Experiment Plotting")
    print("=" * 70)
    print(f"Input: {args.input}")
    print(f"Output: {output_dir}")
    print()

    # Determine which plots to generate
    experiments = []
    if args.experiment == "all":
        experiments = ["microbench", "incast", "alltoall", "throughput", "load"]
    else:
        experiments = [args.experiment]

    # Generate plots
    for exp_name in experiments:
        try:
            print(f"Plotting {exp_name}...")
            if exp_name == "microbench":
                plot_microbench(args.input, output_dir)
            elif exp_name == "incast":
                plot_incast(args.input, output_dir)
            elif exp_name == "alltoall":
                plot_alltoall(args.input, output_dir)
            elif exp_name == "throughput":
                plot_throughput_vs_size(args.input, output_dir)
            elif exp_name == "load":
                plot_fct_vs_load(args.input, output_dir)
        except FileNotFoundError as e:
            print(f"⚠️  Skipping {exp_name}: {e}")
        except Exception as e:
            print(f"⚠️  Error plotting {exp_name}: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 70)
    print(f"All plots saved to: {output_dir}")
    print("=" * 70)


if __name__ == "__main__":
    main()
