"""路径多样性分析 - Experiment 0（零改动）

目标：回答"每个时刻到底有多少条可用路径？它们质量差异多大？"

改动量：0 行代码。纯数据收集。

方法：利用已有的 Opera 路由表，遍历所有 time slice 和 src-dst pair，统计：
- 每对 ToR 平均可选路径数
- 路径跳数范围
- Edge-disjoint 比例
- 最短路径与最长路径的跳数差

预期产出：
- 路径多样性统计
- 路径跳数 CDF
- Edge-disjoint 率分布
- 判定：多路径收益是否显著

Usage:
    # 分析 Opera 拓扑的路径多样性
    python3 examples/Flare/path_diversity_analysis.py --nodes 16 --output results/path_diversity

    # 分析不同拓扑
    python3 examples/Flare/path_diversity_analysis.py --nodes 32 --nb-link 2 --output results/path_diversity_32

    # 生成可视化
    python3 examples/Flare/path_diversity_analysis.py --nodes 16 --output results/path_diversity --plot
"""

import argparse
import json
import csv
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple
from collections import defaultdict
from dataclasses import dataclass, asdict

from openoptics import OpticalTopo, OpticalRouting, Toolbox


@dataclass
class PathInfo:
    """单条路径信息"""
    ts: int
    src: int
    dst: int
    path_id: int
    num_hops: int
    edges: List[Tuple[int, int]]  # 边列表 [(node1, node2), ...]


@dataclass
class PathDiversityMetrics:
    """路径多样性指标"""
    # 基础统计
    total_paths: int
    total_src_dst_pairs: int
    total_time_slices: int

    # 路径数量
    avg_paths_per_pair: float
    min_paths_per_pair: int
    max_paths_per_pair: int

    # 跳数统计
    avg_hops: float
    min_hops: int
    max_hops: int
    hop_range: int  # max - min

    # 路径质量差异
    avg_hop_diff_within_pair: float  # 同一对 ToR 的路径跳数差异
    max_hop_diff_within_pair: int

    # Edge-disjoint 分析
    avg_edge_disjoint_rate: float  # 平均边不相交比例
    fully_disjoint_pair_ratio: float  # 完全边不相交的配对比例

    # 判定信号
    is_diverse_enough: int  # 路径多样性是否足够
    multipath_benefit_score: float  # 多路径收益评分 (0-1)


def extract_edges_from_path(path) -> List[Tuple[int, int]]:
    """从 Path 对象提取边列表"""
    edges = []

    for i, step in enumerate(path.steps):
        if step.step_type == "port":
            # 这是一个传输步骤
            if i > 0:
                prev_step = path.steps[i-1]
                if hasattr(prev_step, 'send_node') and hasattr(step, 'send_node'):
                    edge = tuple(sorted([prev_step.send_node, step.send_node]))
                    edges.append(edge)

    # 如果没有提取到边，尝试从 steps 直接构造
    if not edges and len(path.steps) > 0:
        current_node = path.src
        for step in path.steps:
            if hasattr(step, 'send_node') and step.send_node is not None:
                if step.send_node != current_node:
                    edge = tuple(sorted([current_node, step.send_node]))
                    edges.append(edge)
                    current_node = step.send_node

    return edges


def count_hops_from_path(path) -> int:
    """计算路径跳数（传输边的数量）"""
    hops = 0
    for step in path.steps:
        if step.step_type == "port":  # 传输步骤
            hops += 1

    # 如果没有 step_type，根据 steps 数量估算
    if hops == 0:
        # 简化估算：跳数 ≈ 有效 steps 数量
        hops = sum(1 for s in path.steps if hasattr(s, 'send_node') and s.send_node is not None)

    return hops


def compute_edge_disjoint_rate(paths: List[PathInfo]) -> float:
    """计算一组路径的边不相交比例"""
    if len(paths) <= 1:
        return 1.0

    # 收集所有边
    all_edges = set()
    for path in paths:
        all_edges.update(path.edges)

    if not all_edges:
        return 0.0

    # 计算每条路径有多少独占边
    unique_edge_count = 0
    for path in paths:
        path_edges = set(path.edges)
        # 检查这条路径的边有多少不被其他路径共享
        other_paths_edges = set()
        for other_path in paths:
            if other_path.path_id != path.path_id:
                other_paths_edges.update(other_path.edges)

        unique_edges = path_edges - other_paths_edges
        unique_edge_count += len(unique_edges)

    # 边不相交比例 = 独占边数 / 总边数
    total_edges = sum(len(p.edges) for p in paths)
    if total_edges == 0:
        return 0.0

    return unique_edge_count / total_edges


def analyze_path_diversity(nb_node: int, nb_link: int = 1,
                           time_slice_duration_us: int = 55) -> Tuple[List[PathInfo], PathDiversityMetrics]:
    """分析路径多样性"""

    print(f"Building Opera topology: {nb_node} nodes, {nb_link} links per ToR")

    # ==================== 创建网络 ====================
    net = Toolbox.BaseNetwork(
        name="opera_all_to_all_udp",
        backend="ns3",
        nb_node=nb_node,
        time_slice_duration_us=10_000,      # 时间片长度 10ms
        guardband_us=2,                 # 保护带 2us
        ocs_tor_link_bw_gbps=100,
        tor_host_link_bw_gbps=100,
        use_webserver=False,             # 启用仪表板
        simulation_stop_s=1.0,          # 仿真时长 1秒
    )

    # 构建拓扑
    slice_to_topo = OpticalTopo.opera(nb_node=nb_node, nb_link=nb_link)
    paths: List[Path] = []
    assert net.deploy_topo(slice_to_topo)

    _slice_to_topo = net.get_topo()
    nb_ts = len(_slice_to_topo)
    any_topo = next(iter(_slice_to_topo.values()))
    nodes = sorted(any_topo.nodes())

    print(f"Topology created: {nb_ts} time slices")
    print(f"Computing HoHo routing...")

    # 计算路由（使用 HoHo 获取所有路径）
    # paths = OpticalRouting.routing_hoho(net.get_topo())
    # net.deploy_routing(paths, routing_mode="Per-hop")
    for dst in nodes:
        for src in nodes:
            if src == dst:
                continue
            paths = paths + OpticalRouting.find_n_hop_path_node_pair(_slice_to_topo, src, dst, 50)
    
    print(f"Found {len(paths)} paths")
    print()

    # 提取路径信息
    path_records = []
    path_by_ts_pair = defaultdict(list)  # {(ts, src, dst): [PathInfo]}

    for path_id, path in enumerate(paths):
        # 提取边
        edges = extract_edges_from_path(path)
        num_hops = count_hops_from_path(path)

        # 如果跳数为 0，尝试用边数量估算
        if num_hops == 0 and edges:
            num_hops = len(edges)

        path_info = PathInfo(
            ts=path.arrival_ts,
            src=path.src,
            dst=path.dst,
            path_id=path_id,
            num_hops=num_hops,
            edges=edges
        )

        path_records.append(path_info)
        path_by_ts_pair[(path.arrival_ts, path.src, path.dst)].append(path_info)

    print(f"Extracted {len(path_records)} path records")
    print(f"Unique (ts, src, dst) pairs: {len(path_by_ts_pair)}")
    print()

    # 统计指标
    print("Computing metrics...")

    # 1. 路径数量统计
    paths_per_pair = [len(paths) for paths in path_by_ts_pair.values()]
    avg_paths_per_pair = np.mean(paths_per_pair)
    min_paths_per_pair = min(paths_per_pair) if paths_per_pair else 0
    max_paths_per_pair = max(paths_per_pair) if paths_per_pair else 0

    # 2. 跳数统计
    hop_counts = [p.num_hops for p in path_records if p.num_hops > 0]
    if hop_counts:
        avg_hops = np.mean(hop_counts)
        min_hops = min(hop_counts)
        max_hops = max(hop_counts)
        hop_range = max_hops - min_hops
    else:
        avg_hops = min_hops = max_hops = hop_range = 0

    # 3. 同一对 ToR 的路径跳数差异
    hop_diffs = []
    max_hop_diff = 0
    for (ts, src, dst), paths_list in path_by_ts_pair.items():
        if len(paths_list) > 1:
            hops_in_group = [p.num_hops for p in paths_list if p.num_hops > 0]
            if len(hops_in_group) > 1:
                hop_diff = max(hops_in_group) - min(hops_in_group)
                hop_diffs.append(hop_diff)
                max_hop_diff = max(max_hop_diff, hop_diff)

    avg_hop_diff = np.mean(hop_diffs) if hop_diffs else 0.0

    # 4. Edge-disjoint 分析
    edge_disjoint_rates = []
    fully_disjoint_count = 0

    for (ts, src, dst), paths_list in path_by_ts_pair.items():
        if len(paths_list) > 1:
            disjoint_rate = compute_edge_disjoint_rate(paths_list)
            edge_disjoint_rates.append(disjoint_rate)
            if disjoint_rate > 0.85:  # 85% 以上认为完全不相交
                fully_disjoint_count += 1

    avg_edge_disjoint_rate = np.mean(edge_disjoint_rates) if edge_disjoint_rates else 0.0
    fully_disjoint_pair_ratio = fully_disjoint_count / len(path_by_ts_pair) if path_by_ts_pair else 0.0

    # 5. 判定多路径收益
    # 判定标准：
    # - 平均路径数 >= 2.0
    # - 边不相交比例 >= 0.5
    # - 平均跳数差异 >= 1（有质量差异）

    benefit_score = 0.0
    is_diverse = False

    if avg_paths_per_pair >= 2.0:
        benefit_score += 0.4

    if avg_edge_disjoint_rate >= 0.5:
        benefit_score += 0.4

    if avg_hop_diff >= 0.5:
        benefit_score += 0.2

    is_diverse = (avg_paths_per_pair >= 1.5 and
                  avg_edge_disjoint_rate >= 0.4)

    metrics = PathDiversityMetrics(
        total_paths=len(path_records),
        total_src_dst_pairs=len(path_by_ts_pair),
        total_time_slices=nb_ts,
        avg_paths_per_pair=avg_paths_per_pair,
        min_paths_per_pair=min_paths_per_pair,
        max_paths_per_pair=max_paths_per_pair,
        avg_hops=avg_hops,
        min_hops=min_hops,
        max_hops=max_hops,
        hop_range=hop_range,
        avg_hop_diff_within_pair=avg_hop_diff,
        max_hop_diff_within_pair=max_hop_diff,
        avg_edge_disjoint_rate=avg_edge_disjoint_rate,
        fully_disjoint_pair_ratio=fully_disjoint_pair_ratio,
        is_diverse_enough=1 if is_diverse else 0,
        multipath_benefit_score=benefit_score
    )

    return path_records, metrics


def save_results(path_records: List[PathInfo],
                metrics: PathDiversityMetrics,
                output_dir: Path):
    """保存分析结果"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 保存汇总指标
    summary_file = output_dir / "summary.json"
    with open(summary_file, 'w') as f:
        json.dump(asdict(metrics), f, indent=2)

    print(f"✓ Summary saved: {summary_file}")

    # 保存详细路径数据
    paths_file = output_dir / "paths.csv"
    with open(paths_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'path_id', 'ts', 'src', 'dst', 'num_hops', 'num_edges', 'edges'
        ])
        writer.writeheader()

        for p in path_records:
            writer.writerow({
                'path_id': p.path_id,
                'ts': p.ts,
                'src': p.src,
                'dst': p.dst,
                'num_hops': p.num_hops,
                'num_edges': len(p.edges),
                'edges': str(p.edges)
            })

    print(f"✓ Path details saved: {paths_file}")

    # 打印关键结果
    print("\n" + "="*70)
    print("Path Diversity Analysis Results")
    print("="*70)
    print(f"Total Paths:              {metrics.total_paths}")
    print(f"Unique (ts,src,dst) Pairs: {metrics.total_src_dst_pairs}")
    print(f"Time Slices:              {metrics.total_time_slices}")
    print()
    print(f"Avg Paths per Pair:       {metrics.avg_paths_per_pair:.2f}")
    print(f"Path Count Range:         [{metrics.min_paths_per_pair}, {metrics.max_paths_per_pair}]")
    print()
    print(f"Avg Hops:                 {metrics.avg_hops:.2f}")
    print(f"Hop Range:                [{metrics.min_hops}, {metrics.max_hops}] (range={metrics.hop_range})")
    print(f"Avg Hop Diff (within pair): {metrics.avg_hop_diff_within_pair:.2f}")
    print(f"Max Hop Diff (within pair): {metrics.max_hop_diff_within_pair}")
    print()
    print(f"Avg Edge-Disjoint Rate:   {metrics.avg_edge_disjoint_rate:.2%}")
    print(f"Fully Disjoint Pair Ratio: {metrics.fully_disjoint_pair_ratio:.2%}")
    print()
    print(f"Multipath Benefit Score:  {metrics.multipath_benefit_score:.2f} / 1.0")
    print(f"Is Diverse Enough:        {'✓ YES' if metrics.is_diverse_enough else '✗ NO'}")
    print("="*70)
    print()

    # 判定
    print("Judgment:")
    if metrics.avg_paths_per_pair < 1.5:
        print("  ⚠️  EARLY EXIT SIGNAL: Average < 1.5 paths per pair")
        print("      → Multipath收益可能不足")

    if metrics.avg_edge_disjoint_rate < 0.4:
        print("  ⚠️  EARLY EXIT SIGNAL: Edge-disjoint rate < 40%")
        print("      → 路径间竞争同一瓶颈，收益有限")

    if metrics.avg_hop_diff_within_pair < 0.3:
        print("  ⚠️  EARLY EXIT SIGNAL: Hop差异 < 0.3")
        print("      → 路径质量趋同，收益可能有限")

    if metrics.is_diverse_enough:
        print("  ✓ PROCEED: 路径多样性足够，值得进一步实验")
    else:
        print("  ✗ STOP: 路径多样性不足，多路径收益可能有限")

    print()


def plot_results(path_records: List[PathInfo],
                metrics: PathDiversityMetrics,
                output_dir: Path):
    """生成可视化"""

    print("Generating plots...")

    fig, axes = plt.subplots(2, 2, figsize=(14, 12))

    # 图1: 路径数量分布
    ax1 = axes[0, 0]
    path_by_pair = defaultdict(int)
    for p in path_records:
        path_by_pair[(p.ts, p.src, p.dst)] += 1

    path_counts = list(path_by_pair.values())
    ax1.hist(path_counts, bins=range(0, max(path_counts)+2),
            alpha=0.7, edgecolor='black')
    ax1.axvline(metrics.avg_paths_per_pair, color='red', linestyle='--',
               label=f'Mean: {metrics.avg_paths_per_pair:.2f}')
    ax1.set_xlabel("Number of Paths per (ts, src, dst)")
    ax1.set_ylabel("Frequency")
    ax1.set_title("Path Count Distribution")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # 图2: 路径跳数 CDF
    ax2 = axes[0, 1]
    hop_counts = [p.num_hops for p in path_records if p.num_hops > 0]
    if hop_counts:
        sorted_hops = sorted(hop_counts)
        cdf = np.arange(1, len(sorted_hops) + 1) / len(sorted_hops)
        ax2.plot(sorted_hops, cdf, linewidth=2, marker='o', markersize=4)
        ax2.set_xlabel("Number of Hops")
        ax2.set_ylabel("CDF")
        ax2.set_title("Path Hop Count CDF")
        ax2.grid(True, alpha=0.3)

        # 标注 90% 分位点
        p90_idx = int(len(sorted_hops) * 0.9)
        if p90_idx < len(sorted_hops):
            p90_hops = sorted_hops[p90_idx]
            ax2.axvline(p90_hops, color='red', linestyle='--',
                       label=f'P90: {p90_hops} hops')
            ax2.legend()

    # 图3: Hop 差异分布
    ax3 = axes[1, 0]
    hop_diffs = []
    path_by_pair_dict = defaultdict(list)
    for p in path_records:
        path_by_pair_dict[(p.ts, p.src, p.dst)].append(p)

    for paths_list in path_by_pair_dict.values():
        if len(paths_list) > 1:
            hops = [p.num_hops for p in paths_list if p.num_hops > 0]
            if len(hops) > 1:
                hop_diffs.append(max(hops) - min(hops))

    if hop_diffs:
        ax3.hist(hop_diffs, bins=range(0, max(hop_diffs)+2),
                alpha=0.7, edgecolor='black')
        ax3.axvline(metrics.avg_hop_diff_within_pair, color='red',
                   linestyle='--', label=f'Mean: {metrics.avg_hop_diff_within_pair:.2f}')
        ax3.set_xlabel("Hop Count Difference (within same src-dst pair)")
        ax3.set_ylabel("Frequency")
        ax3.set_title("Path Quality Diversity (Hop Difference)")
        ax3.legend()
        ax3.grid(True, alpha=0.3)

    # 图4: Edge-Disjoint 率分布
    ax4 = axes[1, 1]
    disjoint_rates = []
    for paths_list in path_by_pair_dict.values():
        if len(paths_list) > 1:
            rate = compute_edge_disjoint_rate(paths_list)
            disjoint_rates.append(rate)

    if disjoint_rates:
        ax4.hist(disjoint_rates, bins=20, alpha=0.7, edgecolor='black')
        ax4.axvline(metrics.avg_edge_disjoint_rate, color='red',
                   linestyle='--', label=f'Mean: {metrics.avg_edge_disjoint_rate:.2%}')
        ax4.axvline(0.85, color='green', linestyle='--', alpha=0.5,
                   label='Fully Disjoint Threshold (85%)')
        ax4.set_xlabel("Edge-Disjoint Rate")
        ax4.set_ylabel("Frequency")
        ax4.set_title("Edge-Disjoint Rate Distribution")
        ax4.legend()
        ax4.grid(True, alpha=0.3)

    plt.tight_layout()
    output_file = output_dir / "path_diversity_analysis.png"
    plt.savefig(output_file)
    print(f"✓ Plot saved: {output_file}")
    plt.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--nodes", type=int, default=16,
                       help="Number of nodes in Opera topology")
    parser.add_argument("--nb-link", type=int, default=1,
                       help="Number of links per ToR")
    parser.add_argument("--time-slice", type=int, default=55,
                       help="Time slice duration (us)")
    parser.add_argument("--output", type=Path, default=Path("results/path_diversity"),
                       help="Output directory")
    parser.add_argument("--plot", action="store_true",
                       help="Generate plots")

    args = parser.parse_args(argv)

    print("="*70)
    print("Path Diversity Analysis - Experiment 0")
    print("="*70)
    print(f"Nodes: {args.nodes}")
    print(f"Links per ToR: {args.nb_link}")
    print(f"Time slice: {args.time_slice} us")
    print(f"Output: {args.output}")
    print()

    # 分析路径多样性
    path_records, metrics = analyze_path_diversity(
        args.nodes, args.nb_link, args.time_slice
    )

    # 保存结果
    save_results(path_records, metrics, args.output)

    # 生成可视化
    if args.plot:
        plot_results(path_records, metrics, args.output)

    print("\nAnalysis complete!")


if __name__ == "__main__":
    main()
