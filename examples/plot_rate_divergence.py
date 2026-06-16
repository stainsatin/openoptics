"""Visualize Flare Rate Divergence Experimental Results

Generates plots for the rate divergence experiment:
1. Credit rate vs FCT scatter plot (allocation fairness)
2. Credit efficiency distribution
3. Flow completion time CDF

Usage:
    python3 examples/plot_rate_divergence.py --input results/rate_divergence
    python3 examples/plot_rate_divergence.py --input results/rate_divergence --output plots/
"""

import argparse
import csv
import json
from pathlib import Path
from typing import List, Dict, Any

try:
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:
    print("Error: matplotlib and numpy are required for plotting")
    print("Install with: pip install matplotlib numpy")
    exit(1)


def load_metrics(metrics_file: Path) -> List[Dict[str, Any]]:
    """Load per-flow metrics from CSV."""
    metrics = []
    with open(metrics_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Convert numeric fields
            metrics.append({
                "flow_id": int(row["flow_id"]),
                "src": int(row["src"]),
                "dst": int(row["dst"]),
                "credits_sent": int(row["credits_sent"]),
                "data_packets_received": int(row["data_packets_received"]),
                "fct_s": float(row["fct_s"]),
                "credit_efficiency": float(row["credit_efficiency"]),
                "avg_credit_rate": float(row["avg_credit_rate"]),
            })
    return metrics


def plot_credit_rate_vs_fct(metrics: List[Dict[str, Any]], output_file: Path):
    """Plot scatter: avg_credit_rate vs FCT.

    This reveals allocation unfairness: if some flows get high credit rates
    but don't achieve proportionally lower FCTs, the rate controller is
    creating divergence without benefit.
    """
    completed = [m for m in metrics if m["fct_s"] > 0]
    if not completed:
        print("Warning: No completed flows to plot")
        return

    rates = [m["avg_credit_rate"] for m in completed]
    fcts = [m["fct_s"] * 1000 for m in completed]  # Convert to ms

    plt.figure(figsize=(8, 6))
    plt.scatter(rates, fcts, alpha=0.6, s=50)
    plt.xlabel("Average Credit Rate (credits/s)", fontsize=12)
    plt.ylabel("Flow Completion Time (ms)", fontsize=12)
    plt.title("Credit Rate Allocation vs FCT", fontsize=14, fontweight="bold")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_file, dpi=300)
    print(f"Saved: {output_file}")
    plt.close()


def plot_credit_efficiency(metrics: List[Dict[str, Any]], output_file: Path):
    """Plot histogram of credit efficiency (data_received / credits_sent).

    Low efficiency indicates credit waste; high variance indicates
    per-flow divergence in path quality or admission rates.
    """
    efficiencies = [m["credit_efficiency"] for m in metrics if m["credits_sent"] > 0]
    if not efficiencies:
        print("Warning: No efficiency data to plot")
        return

    plt.figure(figsize=(8, 6))
    plt.hist(efficiencies, bins=20, alpha=0.7, edgecolor="black")
    plt.xlabel("Credit Efficiency (data/credit)", fontsize=12)
    plt.ylabel("Number of Flows", fontsize=12)
    plt.title("Credit Efficiency Distribution", fontsize=14, fontweight="bold")
    plt.axvline(np.mean(efficiencies), color="red", linestyle="--",
               label=f"Mean: {np.mean(efficiencies):.2f}")
    plt.legend()
    plt.grid(True, alpha=0.3, axis="y")
    plt.tight_layout()
    plt.savefig(output_file, dpi=300)
    print(f"Saved: {output_file}")
    plt.close()


def plot_fct_cdf(metrics: List[Dict[str, Any]], output_file: Path):
    """Plot CDF of flow completion times."""
    completed = [m for m in metrics if m["fct_s"] > 0]
    if not completed:
        print("Warning: No completed flows to plot")
        return

    fcts = sorted([m["fct_s"] * 1000 for m in completed])  # Convert to ms
    cdf = np.arange(1, len(fcts) + 1) / len(fcts)

    plt.figure(figsize=(8, 6))
    plt.plot(fcts, cdf, linewidth=2)
    plt.xlabel("Flow Completion Time (ms)", fontsize=12)
    plt.ylabel("CDF", fontsize=12)
    plt.title("Flow Completion Time CDF", fontsize=14, fontweight="bold")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_file, dpi=300)
    print(f"Saved: {output_file}")
    plt.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", type=Path, required=True,
                       help="Input directory containing flow_metrics.csv")
    parser.add_argument("--output", type=Path, default=None,
                       help="Output directory for plots (default: same as input)")

    args = parser.parse_args(argv)

    metrics_file = args.input / "flow_metrics.csv"
    if not metrics_file.exists():
        print(f"Error: {metrics_file} not found")
        print(f"Run the experiment first: python examples/flare_rate_divergence_experiment.py")
        return 1

    output_dir = args.output if args.output else args.input
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading metrics...")
    metrics = load_metrics(metrics_file)
    print(f"Loaded {len(metrics)} flows")

    print("\nGenerating plots...")
    plot_credit_rate_vs_fct(metrics, output_dir / "rate_vs_fct.png")
    plot_credit_efficiency(metrics, output_dir / "credit_efficiency.png")
    plot_fct_cdf(metrics, output_dir / "fct_cdf.png")

    print(f"\nAll plots saved to: {output_dir}")
    print("\nInterpretation Guide:")
    print("  1. rate_vs_fct.png: Check if high credit rates correlate with low FCT")
    print("     - Strong correlation = fair allocation")
    print("     - Weak correlation = rate divergence without benefit")
    print("  2. credit_efficiency.png: Check variance in efficiency")
    print("     - High variance = unequal path quality or admission")
    print("  3. fct_cdf.png: Overall flow performance")


if __name__ == "__main__":
    main()
