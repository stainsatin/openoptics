"""Credit Waste Study Visualization

分析和可视化 Credit Waste Study 的实验结果。

生成图表：
1. Credit Waste Rate vs Incast Degree (不同流大小/profile)
2. Incast vs All-to-All 对比 (credit waste, throughput loss)
3. Staggered vs Synchronized Incast 对比
4. Credit Efficiency 热图 (senders × flow_size)
5. Throughput Loss vs Credit Waste 散点图（相关性分析）
6. Convergence Analysis (如果有时间序列数据)

Usage:
    # 可视化单个实验结果
    python examples/plot_credit_waste_study.py --input results/credit_waste --experiment single

    # 可视化完整扫描结果
    python examples/plot_credit_waste_study.py --input results/credit_waste --experiment sweep

    # 生成判定报告
    python examples/plot_credit_waste_study.py --input results/credit_waste --report
"""

import argparse
import json
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
from typing import List, Dict, Any
from collections import defaultdict

# 设置绘图风格
plt.rcParams['figure.figsize'] = (10, 6)
plt.rcParams['font.size'] = 11
plt.rcParams['axes.labelsize'] = 12
plt.rcParams['axes.titlesize'] = 14
plt.rcParams['legend.fontsize'] = 10
plt.rcParams['figure.dpi'] = 100
plt.rcParams['savefig.dpi'] = 300
plt.rcParams['savefig.bbox'] = 'tight'

sns.set_palette("husl")


def load_sweep_results(input_dir: Path) -> List[Dict[str, Any]]:
    """加载完整扫描结果"""
    sweep_file = input_dir / "sweep_summary.json"
    if not sweep_file.exists():
        raise FileNotFoundError(f"Sweep summary not found: {sweep_file}")

    with open(sweep_file, 'r') as f:
        return json.load(f)


def plot_credit_waste_vs_senders(results: List[Dict], output_dir: Path):
    """图1: Credit Waste Rate vs Incast Degree"""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    for profile_idx, profile in enumerate(["55us", "15us"]):
        ax = axes[profile_idx]

        # 按流大小分组
        for flow_type in ["short", "medium", "long"]:
            incast_data = defaultdict(list)
            alltoall_data = defaultdict(list)

            for res in results:
                cfg = res["config"]
                met = res["metrics"]

                if cfg["profile"] != profile:
                    continue

                # 判断流大小类型（根据实际字节数）
                # 这里简化处理，实际应该根据 BDP 倍数判断
                flow_size = cfg["flow_size_bytes"]

                if cfg["traffic_pattern"] == "incast" and cfg["stagger_ts"] == 0:
                    incast_data[cfg["senders"]].append(met["credit_waste_rate"])
                elif cfg["traffic_pattern"] == "alltoall":
                    alltoall_data[cfg["senders"]].append(met["credit_waste_rate"])

            # 绘制 incast
            if incast_data:
                senders = sorted(incast_data.keys())
                waste_rates = [np.mean(incast_data[s]) * 100 for s in senders]
                ax.plot(senders, waste_rates, marker='o', linewidth=2,
                       label=f'Incast ({flow_type})', markersize=8)

            # 绘制 all-to-all 作为基线
            if alltoall_data:
                senders = sorted(alltoall_data.keys())
                waste_rates = [np.mean(alltoall_data[s]) * 100 for s in senders]
                ax.plot(senders, waste_rates, marker='s', linewidth=2,
                       linestyle='--', label=f'All-to-all ({flow_type})',
                       markersize=8, alpha=0.7)

        ax.set_xlabel("Number of Senders (Incast Degree)")
        ax.set_ylabel("Credit Waste Rate (%)")
        ax.set_title(f"Credit Waste vs Incast Degree ({profile})")
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.set_xscale('log', base=2)

    plt.tight_layout()
    output_file = output_dir / "credit_waste_vs_senders.png"
    plt.savefig(output_file)
    print(f"✓ Saved: {output_file}")
    plt.close()


def plot_incast_vs_alltoall_comparison(results: List[Dict], output_dir: Path):
    """图2: Incast vs All-to-All 多维对比"""
    fig, axes = plt.subplots(2, 2, figsize=(14, 12))

    metrics_to_plot = [
        ("credit_waste_rate", "Credit Waste Rate", "%", 100),
        ("throughput_loss_percent", "Throughput Loss", "%", 1),
        ("mean_fct_ms", "Mean FCT", "ms", 1),
        ("jain_fairness_index", "Jain Fairness Index", "", 1),
    ]

    for idx, (metric_key, metric_name, unit, scale) in enumerate(metrics_to_plot):
        ax = axes[idx // 2, idx % 2]

        for profile in ["55us", "15us"]:
            incast_by_senders = defaultdict(list)
            alltoall_by_senders = defaultdict(list)

            for res in results:
                cfg = res["config"]
                met = res["metrics"]

                if cfg["profile"] != profile:
                    continue

                if cfg["traffic_pattern"] == "incast" and cfg["stagger_ts"] == 0:
                    incast_by_senders[cfg["senders"]].append(met[metric_key] * scale)
                elif cfg["traffic_pattern"] == "alltoall":
                    alltoall_by_senders[cfg["senders"]].append(met[metric_key] * scale)

            # 绘制对比
            if incast_by_senders:
                senders = sorted(incast_by_senders.keys())
                values = [np.mean(incast_by_senders[s]) for s in senders]
                ax.plot(senders, values, marker='o', linewidth=2,
                       label=f'Incast ({profile})', markersize=8)

            if alltoall_by_senders:
                senders = sorted(alltoall_by_senders.keys())
                values = [np.mean(alltoall_by_senders[s]) for s in senders]
                ax.plot(senders, values, marker='s', linewidth=2,
                       linestyle='--', label=f'All-to-all ({profile})',
                       markersize=8, alpha=0.7)

        ax.set_xlabel("Number of Senders")
        ax.set_ylabel(f"{metric_name} ({unit})" if unit else metric_name)
        ax.set_title(f"{metric_name}: Incast vs All-to-All")
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.set_xscale('log', base=2)

    plt.tight_layout()
    output_file = output_dir / "incast_vs_alltoall_comparison.png"
    plt.savefig(output_file)
    print(f"✓ Saved: {output_file}")
    plt.close()


def plot_staggered_effect(results: List[Dict], output_dir: Path):
    """图3: Staggered vs Synchronized Incast"""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    metrics = [
        ("credit_waste_rate", "Credit Waste Rate (%)", 100),
        ("throughput_loss_percent", "Throughput Loss (%)", 1),
    ]

    for idx, (metric_key, ylabel, scale) in enumerate(metrics):
        ax = axes[idx]

        for profile in ["55us", "15us"]:
            sync_data = defaultdict(list)
            stagger2_data = defaultdict(list)
            stagger3_data = defaultdict(list)

            for res in results:
                cfg = res["config"]
                met = res["metrics"]

                if cfg["profile"] != profile or cfg["traffic_pattern"] != "incast":
                    continue

                senders = cfg["senders"]
                value = met[metric_key] * scale

                if cfg["stagger_ts"] == 0:
                    sync_data[senders].append(value)
                elif cfg["stagger_ts"] == 2:
                    stagger2_data[senders].append(value)
                elif cfg["stagger_ts"] == 3:
                    stagger3_data[senders].append(value)

            # 绘制对比
            if sync_data:
                senders = sorted(sync_data.keys())
                values = [np.mean(sync_data[s]) for s in senders]
                ax.plot(senders, values, marker='o', linewidth=2,
                       label=f'Synchronized ({profile})', markersize=8)

            if stagger2_data:
                senders = sorted(stagger2_data.keys())
                values = [np.mean(stagger2_data[s]) for s in senders]
                ax.plot(senders, values, marker='s', linewidth=2,
                       label=f'Stagger 2 TS ({profile})', markersize=8)

            if stagger3_data:
                senders = sorted(stagger3_data.keys())
                values = [np.mean(stagger3_data[s]) for s in senders]
                ax.plot(senders, values, marker='^', linewidth=2,
                       label=f'Stagger 3 TS ({profile})', markersize=8)

        ax.set_xlabel("Number of Senders")
        ax.set_ylabel(ylabel)
        ax.set_title(f"Staggered Incast Effect: {ylabel}")
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.set_xscale('log', base=2)

    plt.tight_layout()
    output_file = output_dir / "staggered_effect.png"
    plt.savefig(output_file)
    print(f"✓ Saved: {output_file}")
    plt.close()


def plot_efficiency_heatmap(results: List[Dict], output_dir: Path):
    """图4: Credit Efficiency 热图"""
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))

    for profile_idx, profile in enumerate(["55us", "15us"]):
        ax = axes[profile_idx]

        # 收集数据：senders × flow_size
        senders_list = sorted(set(r["config"]["senders"] for r in results
                                 if r["config"]["profile"] == profile
                                 and r["config"]["traffic_pattern"] == "incast"))

        flow_sizes = []
        efficiency_matrix = []

        # 按流大小分组
        flow_size_groups = defaultdict(lambda: defaultdict(list))
        for res in results:
            cfg = res["config"]
            met = res["metrics"]

            if cfg["profile"] != profile or cfg["traffic_pattern"] != "incast":
                continue

            flow_size_groups[cfg["flow_size_bytes"]][cfg["senders"]].append(
                met["credit_efficiency"]
            )

        # 构建矩阵
        sorted_sizes = sorted(flow_size_groups.keys())
        flow_sizes = [f"{s//1024}KB" if s < 1024*1024 else f"{s//1024//1024}MB"
                     for s in sorted_sizes]

        for size_bytes in sorted_sizes:
            row = []
            for senders in senders_list:
                if senders in flow_size_groups[size_bytes]:
                    row.append(np.mean(flow_size_groups[size_bytes][senders]))
                else:
                    row.append(np.nan)
            efficiency_matrix.append(row)

        if efficiency_matrix:
            im = ax.imshow(efficiency_matrix, cmap='RdYlGn', aspect='auto',
                          vmin=0.5, vmax=1.0)
            ax.set_xticks(range(len(senders_list)))
            ax.set_xticklabels(senders_list)
            ax.set_yticks(range(len(flow_sizes)))
            ax.set_yticklabels(flow_sizes)
            ax.set_xlabel("Number of Senders")
            ax.set_ylabel("Flow Size")
            ax.set_title(f"Credit Efficiency Heatmap ({profile})")

            # 添加数值标签
            for i in range(len(flow_sizes)):
                for j in range(len(senders_list)):
                    if not np.isnan(efficiency_matrix[i][j]):
                        text = ax.text(j, i, f"{efficiency_matrix[i][j]:.2f}",
                                     ha="center", va="center", color="black",
                                     fontsize=9)

            plt.colorbar(im, ax=ax, label="Credit Efficiency")

    plt.tight_layout()
    output_file = output_dir / "efficiency_heatmap.png"
    plt.savefig(output_file)
    print(f"✓ Saved: {output_file}")
    plt.close()


def plot_waste_vs_loss_scatter(results: List[Dict], output_dir: Path):
    """图5: Credit Waste vs Throughput Loss 相关性分析"""
    fig, ax = plt.subplots(figsize=(10, 8))

    for pattern in ["incast", "alltoall"]:
        for profile in ["55us", "15us"]:
            waste_rates = []
            throughput_losses = []

            for res in results:
                cfg = res["config"]
                met = res["metrics"]

                if cfg["profile"] != profile or cfg["traffic_pattern"] != pattern:
                    continue

                waste_rates.append(met["credit_waste_rate"] * 100)
                throughput_losses.append(met["throughput_loss_percent"])

            if waste_rates:
                marker = 'o' if pattern == "incast" else 's'
                label = f"{pattern.capitalize()} ({profile})"
                ax.scatter(waste_rates, throughput_losses, marker=marker,
                          s=100, alpha=0.6, label=label)

    # 添加趋势线
    all_waste = []
    all_loss = []
    for res in results:
        met = res["metrics"]
        all_waste.append(met["credit_waste_rate"] * 100)
        all_loss.append(met["throughput_loss_percent"])

    if len(all_waste) > 2:
        z = np.polyfit(all_waste, all_loss, 1)
        p = np.poly1d(z)
        x_line = np.linspace(min(all_waste), max(all_waste), 100)
        ax.plot(x_line, p(x_line), "r--", alpha=0.5, linewidth=2,
               label=f"Trend: y={z[0]:.2f}x+{z[1]:.2f}")

        # 计算相关系数
        corr = np.corrcoef(all_waste, all_loss)[0, 1]
        ax.text(0.05, 0.95, f"Correlation: {corr:.3f}",
               transform=ax.transAxes, fontsize=12,
               verticalalignment='top',
               bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    ax.set_xlabel("Credit Waste Rate (%)")
    ax.set_ylabel("Throughput Loss (%)")
    ax.set_title("Correlation: Credit Waste vs Throughput Loss")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = output_dir / "waste_vs_loss_correlation.png"
    plt.savefig(output_file)
    print(f"✓ Saved: {output_file}")
    plt.close()


def generate_judgment_report(results: List[Dict], output_dir: Path):
    """生成判定报告"""
    report_file = output_dir / "judgment_report.txt"

    with open(report_file, 'w') as f:
        f.write("="*70 + "\n")
        f.write("CREDIT WASTE STUDY - JUDGMENT REPORT\n")
        f.write("="*70 + "\n\n")

        # 判定标准 1: CW_incast / CW_all-to-all > 2×
        f.write("Criterion 1: Credit Waste Ratio (Incast / All-to-all)\n")
        f.write("-"*70 + "\n")

        for profile in ["55us", "15us"]:
            incast_wastes = []
            alltoall_wastes = []

            for res in results:
                cfg = res["config"]
                met = res["metrics"]

                if cfg["profile"] != profile:
                    continue

                if cfg["traffic_pattern"] == "incast" and cfg["stagger_ts"] == 0:
                    incast_wastes.append(met["credit_waste_rate"])
                elif cfg["traffic_pattern"] == "alltoall":
                    alltoall_wastes.append(met["credit_waste_rate"])

            if incast_wastes and alltoall_wastes:
                avg_incast = np.mean(incast_wastes) * 100
                avg_alltoall = np.mean(alltoall_wastes) * 100
                ratio = avg_incast / avg_alltoall if avg_alltoall > 0 else float('inf')

                f.write(f"  Profile {profile}:\n")
                f.write(f"    Incast avg waste:     {avg_incast:.2f}%\n")
                f.write(f"    All-to-all avg waste: {avg_alltoall:.2f}%\n")
                f.write(f"    Ratio:                {ratio:.2f}x\n")
                f.write(f"    Verdict:              {'FAIL (>2x)' if ratio > 2.0 else 'PASS'}\n\n")

        # 判定标准 2: 吞吐损失 > 15%
        f.write("\nCriterion 2: Throughput Loss > 15%\n")
        f.write("-"*70 + "\n")

        for pattern in ["incast", "alltoall"]:
            losses = []
            for res in results:
                if res["config"]["traffic_pattern"] == pattern:
                    losses.append(res["metrics"]["throughput_loss_percent"])

            if losses:
                avg_loss = np.mean(losses)
                max_loss = max(losses)

                f.write(f"  {pattern.capitalize()}:\n")
                f.write(f"    Average loss: {avg_loss:.2f}%\n")
                f.write(f"    Max loss:     {max_loss:.2f}%\n")
                f.write(f"    Verdict:      {'FAIL (>15%)' if avg_loss > 15.0 else 'PASS'}\n\n")

        # 判定标准 3: Staggered incast 仍然劣化
        f.write("\nCriterion 3: Staggered Incast Still Degraded\n")
        f.write("-"*70 + "\n")

        sync_wastes = []
        stagger_wastes = []

        for res in results:
            cfg = res["config"]
            met = res["metrics"]

            if cfg["traffic_pattern"] != "incast":
                continue

            if cfg["stagger_ts"] == 0:
                sync_wastes.append(met["credit_waste_rate"] * 100)
            elif cfg["stagger_ts"] > 0:
                stagger_wastes.append(met["credit_waste_rate"] * 100)

        if sync_wastes and stagger_wastes:
            avg_sync = np.mean(sync_wastes)
            avg_stagger = np.mean(stagger_wastes)
            improvement = (avg_sync - avg_stagger) / avg_sync * 100

            f.write(f"  Synchronized avg waste: {avg_sync:.2f}%\n")
            f.write(f"  Staggered avg waste:    {avg_stagger:.2f}%\n")
            f.write(f"  Improvement:            {improvement:.2f}%\n")
            f.write(f"  Verdict:                {'FAIL (still degraded)' if avg_stagger > 5.0 else 'PASS'}\n\n")

        # 汇总
        f.write("\n" + "="*70 + "\n")
        f.write("SUMMARY\n")
        f.write("="*70 + "\n")
        f.write(f"Total experiments run: {len(results)}\n")
        f.write(f"Report generated: {output_dir}\n")

    print(f"\n✓ Judgment report saved: {report_file}")

    # 打印到控制台
    with open(report_file, 'r') as f:
        print("\n" + f.read())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", type=Path, required=True,
                       help="Input directory with experiment results")
    parser.add_argument("--output", type=Path, default=None,
                       help="Output directory for plots (default: same as input)")
    parser.add_argument("--experiment", choices=["single", "sweep"], default="sweep",
                       help="Type of experiment to visualize")
    parser.add_argument("--report", action="store_true",
                       help="Generate judgment report")

    args = parser.parse_args(argv)

    output_dir = args.output if args.output else args.input
    output_dir.mkdir(parents=True, exist_ok=True)

    print("="*70)
    print("Credit Waste Study - Visualization")
    print("="*70)

    if args.experiment == "sweep":
        print("\nLoading sweep results...")
        results = load_sweep_results(args.input)
        print(f"✓ Loaded {len(results)} experiment results\n")

        print("Generating plots...")
        plot_credit_waste_vs_senders(results, output_dir)
        plot_incast_vs_alltoall_comparison(results, output_dir)
        plot_staggered_effect(results, output_dir)
        plot_efficiency_heatmap(results, output_dir)
        plot_waste_vs_loss_scatter(results, output_dir)

        if args.report:
            print("\nGenerating judgment report...")
            generate_judgment_report(results, output_dir)

    print("\n" + "="*70)
    print(f"All visualizations saved to: {output_dir}")
    print("="*70)


if __name__ == "__main__":
    main()
