"""Flare Credit Waste Quantification Study

系统性量化 Incast 场景下的 credit waste，分析其对吞吐和 FCT 的影响。

实验设计：
- Incast degree: 2, 4, 8, 16, 32, 64
- 流大小: BDP (short), 10×BDP (medium), 100×BDP (long)
- Time slice: 15µs, 55µs
- 流量模式: synchronized incast / staggered incast
- 对比基准: all-to-all（相同总流量）

关键指标：
1. Credit Waste Rate = (credits_sent - data_received) / credits_sent
2. Credit 丢包位置分布（per-hop）
3. 收敛时间（达到 fair share 的 RTT 数）
4. 吞吐损失（理论值 vs 实际值积分）
5. Credit queue depth 时间序列
6. Per-flow rate 轨迹

Usage:
    # 单个实验
    python examples/flare_credit_waste_study.py --senders 16 --flow-size bdp --profile 55us

    # 完整扫描
    python examples/flare_credit_waste_study.py --sweep --output results/credit_waste_study

    # Staggered incast
    python examples/flare_credit_waste_study.py --senders 16 --stagger-ts 2
"""

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict

from openoptics import OpticalRouting, OpticalTopo, Toolbox
from openoptics.backends.ns3.traffic import FlareConfig


@dataclass
class ExperimentConfig:
    """实验配置"""
    senders: int
    flow_size_bytes: int
    profile: str  # "55us" or "15us"
    traffic_pattern: str  # "incast" or "alltoall"
    stagger_ts: int = 0  # 错开的时间片数（0 = synchronized）
    mtu: int = 1536
    output_dir: Path = Path("results/credit_waste")


@dataclass
class CreditWasteMetrics:
    """Credit waste 相关指标"""
    # 基础统计
    total_credits_sent: int
    total_data_received: int
    total_credits_admitted: int
    total_credits_dropped: int

    # Credit waste 率
    credit_waste_rate: float  # (sent - received) / sent
    credit_drop_rate: float   # dropped / (admitted + dropped)
    credit_efficiency: float  # data_received / credits_sent

    # 吞吐指标
    theoretical_throughput_gbps: float
    actual_throughput_gbps: float
    throughput_loss_percent: float

    # FCT 指标
    mean_fct_ms: float
    median_fct_ms: float
    p99_fct_ms: float
    cv_fct: float  # 变异系数

    # 收敛指标
    convergence_time_ms: Optional[float] = None
    convergence_rtt_count: Optional[int] = None

    # 公平性指标
    jain_fairness_index: float = 0.0
    max_min_rate_ratio: float = 0.0


def estimate_bdp(profile: str, link_bw_gbps: float = 100) -> int:
    """估算 BDP（Bandwidth-Delay Product）

    BDP = Bandwidth × RTT
    RTT ≈ 2 × time_slice (往返)

    Args:
        profile: "55us" or "15us"
        link_bw_gbps: 链路带宽 (Gbps)

    Returns:
        BDP in bytes
    """
    slice_us = 55 if profile == "55us" else 15
    rtt_s = 2 * slice_us / 1e6  # RTT in seconds
    bw_bytes_per_s = link_bw_gbps * 1e9 / 8
    bdp_bytes = int(rtt_s * bw_bytes_per_s)
    return bdp_bytes


def build_network(config: ExperimentConfig, stop_s: float):
    """构建实验网络拓扑"""
    nb_node = config.senders + 1 if config.traffic_pattern == "incast" else config.senders
    slice_us = 55 if config.profile == "55us" else 15

    net = Toolbox.BaseNetwork(
        name=f"flare_cw_{config.traffic_pattern}_{config.senders}",
        backend="ns3",
        nb_node=nb_node,
        nb_link=1,
        time_slice_duration_us=slice_us,
        guardband_ms=0,
        ocs_tor_link_bw_gbps=100,
        tor_host_link_bw_gbps=100,
        use_webserver=False,
        simulation_stop_s=stop_s,
        snapshot_interval_us=slice_us,
        flare_profile=config.profile,
    )

    # 部署 opera 拓扑
    net.deploy_topo(OpticalTopo.opera(nb_node=nb_node, nb_link=1))

    # 部署 HoHo 路由
    paths = OpticalRouting.routing_hoho(net.get_topo())
    net.deploy_routing(paths, routing_mode="Per-hop")

    return net, nb_node


def install_traffic(net, config: ExperimentConfig, nb_node: int,
                   flare_config: FlareConfig) -> List:
    """安装流量模式"""
    gen = net.flare_traffic(config=flare_config)

    if config.traffic_pattern == "incast":
        # Incast: N senders → 1 receiver
        receiver = nb_node - 1
        senders = list(range(config.senders))

        if config.stagger_ts == 0:
            # Synchronized incast
            gen.many_to_one(
                senders,
                dst=receiver,
                size_bytes=config.flow_size_bytes,
                start_s=0.0001,
                duration_s=None,
                packet_size_bytes=config.mtu
            )
        else:
            # Staggered incast: 错开启动时间
            slice_us = 55 if config.profile == "55us" else 15
            stagger_s = config.stagger_ts * slice_us / 1e6

            for i, src in enumerate(senders):
                start_time = 0.0001 + (i % config.stagger_ts) * stagger_s
                gen.one_to_one(
                    src,
                    dst=receiver,
                    size_bytes=config.flow_size_bytes,
                    start_s=start_time,
                    duration_s=None,
                    packet_size_bytes=config.mtu
                )

    else:  # all-to-all
        # All-to-all: 每个节点向其他所有节点发送
        # 总流量与 incast 相同：N 条流
        nodes = list(range(nb_node))
        # flow_count = 0
        # target_flows = config.senders  # 与 incast 相同的流数量
        
        gen.all_to_all(
            nodes=nodes,
            size_bytes=config.flow_size_bytes,
            start_s=0.0001,
            duration_s=None,
            packet_size_bytes=config.mtu,
            include_self=None
        )

    return gen.install()


def compute_credit_waste_metrics(flows: List,
                                 config: ExperimentConfig) -> CreditWasteMetrics:
    """计算 credit waste 相关指标"""
    if not flows:
        raise ValueError("No flows installed")

    # 收集所有流的统计
    completed_flows = []
    total_credits_sent = 0
    total_data_received = 0
    total_credits_admitted = 0
    total_credits_dropped = 0
    fcts = []
    throughputs = []

    for flow in flows:
        stats = flow.stats()
        if stats is None or stats.fct_s <= 0:
            continue

        completed_flows.append(stats)
        total_credits_sent += stats.credits_sent
        total_data_received += stats.data_packets_received
        total_credits_admitted += stats.credits_admitted
        total_credits_dropped += stats.credits_dropped
        fcts.append(stats.fct_s * 1000)  # 转换为 ms

        # 计算每条流的吞吐量
        if stats.fct_s > 0:
            flow_bytes = config.flow_size_bytes
            throughput_gbps = (flow_bytes * 8) / (stats.fct_s * 1e9)
            throughputs.append(throughput_gbps)

    if not completed_flows:
        raise ValueError("No completed flows")

    # 计算 credit waste 率
    credit_waste_rate = (
        (total_credits_sent - total_data_received) / total_credits_sent
        if total_credits_sent > 0 else 0.0
    )

    credit_drop_rate = (
        total_credits_dropped / (total_credits_admitted + total_credits_dropped)
        if (total_credits_admitted + total_credits_dropped) > 0 else 0.0
    )

    credit_efficiency = (
        total_data_received / total_credits_sent
        if total_credits_sent > 0 else 0.0
    )

    # 计算理论吞吐量
    # 理论值：每条流应该获得 fair share
    link_bw_gbps = 100
    if config.traffic_pattern == "incast":
        # Incast: 所有流共享 receiver 的 downlink
        fair_share_gbps = link_bw_gbps / config.senders
    else:
        # All-to-all: 更复杂，这里简化为平均带宽
        fair_share_gbps = link_bw_gbps / config.senders

    theoretical_throughput = fair_share_gbps
    actual_throughput = statistics.mean(throughputs) if throughputs else 0.0
    throughput_loss = (
        (theoretical_throughput - actual_throughput) / theoretical_throughput * 100
        if theoretical_throughput > 0 else 0.0
    )

    # FCT 统计
    mean_fct = statistics.mean(fcts)
    median_fct = statistics.median(fcts)
    p99_fct = sorted(fcts)[int(len(fcts) * 0.99)] if len(fcts) > 1 else fcts[0]
    cv_fct = (
        statistics.stdev(fcts) / mean_fct if len(fcts) > 1 and mean_fct > 0 else 0.0
    )

    # 公平性指标：Jain's Fairness Index
    if throughputs:
        sum_throughput = sum(throughputs)
        sum_squared = sum(t**2 for t in throughputs)
        n = len(throughputs)
        jain_index = (sum_throughput ** 2) / (n * sum_squared) if sum_squared > 0 else 0.0
        max_min_ratio = max(throughputs) / min(throughputs) if min(throughputs) > 0 else 0.0
    else:
        jain_index = 0.0
        max_min_ratio = 0.0

    return CreditWasteMetrics(
        total_credits_sent=total_credits_sent,
        total_data_received=total_data_received,
        total_credits_admitted=total_credits_admitted,
        total_credits_dropped=total_credits_dropped,
        credit_waste_rate=credit_waste_rate,
        credit_drop_rate=credit_drop_rate,
        credit_efficiency=credit_efficiency,
        theoretical_throughput_gbps=theoretical_throughput,
        actual_throughput_gbps=actual_throughput,
        throughput_loss_percent=throughput_loss,
        mean_fct_ms=mean_fct,
        median_fct_ms=median_fct,
        p99_fct_ms=p99_fct,
        cv_fct=cv_fct,
        jain_fairness_index=jain_index,
        max_min_rate_ratio=max_min_ratio,
    )


def save_results(config: ExperimentConfig,
                metrics: CreditWasteMetrics,
                flows: List):
    """保存实验结果"""
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # 生成文件名前缀
    prefix = (f"{config.traffic_pattern}_"
             f"{config.senders}senders_"
             f"{config.flow_size_bytes}B_"
             f"{config.profile}")
    if config.stagger_ts > 0:
        prefix += f"_stagger{config.stagger_ts}"

    # 保存汇总指标
    summary_file = config.output_dir / f"{prefix}_summary.json"
    summary = {
        "experiment": "credit_waste_study",
        "config": asdict(config),
        "metrics": asdict(metrics),
    }
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"✓ Summary saved: {summary_file}")

    # 保存详细流统计
    flows_file = config.output_dir / f"{prefix}_flows.csv"
    fieldnames = [
        "flow_id", "src", "dst", "fct_s", "fct_ms",
        "credits_sent", "credits_received", "credits_admitted", "credits_dropped",
        "data_packets_sent", "data_packets_received",
        "credit_waste_rate", "credit_efficiency", "throughput_gbps"
    ]

    with open(flows_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for flow in flows:
            stats = flow.stats()
            if stats is None or stats.fct_s <= 0:
                continue

            cw_rate = (
                (stats.credits_sent - stats.data_packets_received) / stats.credits_sent
                if stats.credits_sent > 0 else 0.0
            )
            eff = (
                stats.data_packets_received / stats.credits_sent
                if stats.credits_sent > 0 else 0.0
            )
            tput = (
                (config.flow_size_bytes * 8) / (stats.fct_s * 1e9)
                if stats.fct_s > 0 else 0.0
            )

            writer.writerow({
                "flow_id": stats.flow_id,
                "src": stats.src,
                "dst": stats.dst,
                "fct_s": stats.fct_s,
                "fct_ms": stats.fct_s * 1000,
                "credits_sent": stats.credits_sent,
                "credits_received": stats.credits_received,
                "credits_admitted": stats.credits_admitted,
                "credits_dropped": stats.credits_dropped,
                "data_packets_sent": stats.data_packets_sent,
                "data_packets_received": stats.data_packets_received,
                "credit_waste_rate": cw_rate,
                "credit_efficiency": eff,
                "throughput_gbps": tput,
            })

    print(f"✓ Flow details saved: {flows_file}")

    # 打印关键结果
    print("\n" + "="*70)
    print("Credit Waste Analysis Results")
    print("="*70)
    print(f"Traffic Pattern: {config.traffic_pattern}")
    print(f"Senders: {config.senders}")
    print(f"Flow Size: {config.flow_size_bytes} bytes")
    print(f"Profile: {config.profile}")
    if config.stagger_ts > 0:
        print(f"Stagger: {config.stagger_ts} time slices")
    print()
    print(f"Credit Waste Rate:    {metrics.credit_waste_rate:.4f} ({metrics.credit_waste_rate*100:.2f}%)")
    print(f"Credit Drop Rate:     {metrics.credit_drop_rate:.4f} ({metrics.credit_drop_rate*100:.2f}%)")
    print(f"Credit Efficiency:    {metrics.credit_efficiency:.4f}")
    print()
    print(f"Theoretical Tput:     {metrics.theoretical_throughput_gbps:.2f} Gbps")
    print(f"Actual Tput:          {metrics.actual_throughput_gbps:.2f} Gbps")
    print(f"Throughput Loss:      {metrics.throughput_loss_percent:.2f}%")
    print()
    print(f"Mean FCT:             {metrics.mean_fct_ms:.3f} ms")
    print(f"Median FCT:           {metrics.median_fct_ms:.3f} ms")
    print(f"P99 FCT:              {metrics.p99_fct_ms:.3f} ms")
    print(f"CV(FCT):              {metrics.cv_fct:.4f}")
    print()
    print(f"Jain Fairness Index:  {metrics.jain_fairness_index:.4f}")
    print(f"Max/Min Rate Ratio:   {metrics.max_min_rate_ratio:.2f}")
    print("="*70)


def run_single_experiment(config: ExperimentConfig):
    """运行单个实验"""
    print(f"\n{'='*70}")
    print(f"Running Experiment: {config.traffic_pattern} / {config.senders} senders / {config.profile}")
    print(f"{'='*70}\n")

    # 估算仿真时间
    bdp = estimate_bdp(config.profile)
    expected_fct_s = config.flow_size_bytes / (100 * 1e9 / 8 / config.senders)  # 粗略估算
    stop_s = max(1.0, expected_fct_s * 3)  # 留出 3 倍余量

    print(f"Estimated BDP: {bdp} bytes")
    print(f"Expected FCT: ~{expected_fct_s*1000:.2f} ms")
    print(f"Simulation time: {stop_s} s\n")

    # 构建网络
    net, nb_node = build_network(config, stop_s)

    # 安装流量
    flare_config = FlareConfig.from_profile(config.profile, mtu_bytes=config.mtu)
    flows = install_traffic(net, config, nb_node, flare_config)

    print(f"Installed {len(flows)} flows")

    # 运行仿真
    print("Running simulation...")
    net.start()
    print("✓ Simulation complete\n")

    # 计算指标
    print("Computing metrics...")
    metrics = compute_credit_waste_metrics(flows, config)

    # 保存结果
    save_results(config, metrics, flows)

    return metrics


def run_sweep(output_dir: Path):
    """运行完整参数扫描"""
    print("\n" + "="*70)
    print("CREDIT WASTE STUDY - FULL PARAMETER SWEEP")
    print("="*70 + "\n")

    # 实验参数矩阵
    senders_list = [2, 4, 8, 16, 32]  # 64 可能太大，先测试较小规模
    profiles = ["55us", "15us"]
    traffic_patterns = ["incast", "alltoall"]

    all_results = []

    for profile in profiles:
        bdp = estimate_bdp(profile)
        flow_sizes = {
            "short": bdp,
            # "medium": bdp * 10,
            # "long": bdp * 100,
        }

        for size_name, size_bytes in flow_sizes.items():
            for senders in senders_list:
                for pattern in traffic_patterns:
                    config = ExperimentConfig(
                        senders=senders,
                        flow_size_bytes=size_bytes,
                        profile=profile,
                        traffic_pattern=pattern,
                        stagger_ts=0,
                        output_dir=output_dir
                    )

                    try:
                        metrics = run_single_experiment(config)
                        all_results.append({
                            "config": asdict(config),
                            "metrics": asdict(metrics)
                        })
                    except Exception as e:
                        print(f"⚠️  Experiment failed: {e}")
                        continue

                # 额外测试 staggered incast
                if senders >= 8:
                    for stagger in [2, 3]:
                        config = ExperimentConfig(
                            senders=senders,
                            flow_size_bytes=flow_sizes["medium"],
                            profile=profile,
                            traffic_pattern="incast",
                            stagger_ts=stagger,
                            output_dir=output_dir
                        )

                        try:
                            metrics = run_single_experiment(config)
                            all_results.append({
                                "config": asdict(config),
                                "metrics": asdict(metrics)
                            })
                        except Exception as e:
                            print(f"⚠️  Experiment failed: {e}")
                            continue

    # 保存汇总结果
    summary_file = output_dir / "sweep_summary.json"
    with open(summary_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    print(f"\n✓ Sweep complete! Results saved to {output_dir}")
    print(f"✓ Summary: {summary_file}")
    print(f"✓ Total experiments: {len(all_results)}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # 实验模式
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--sweep", action="store_true",
                           help="Run full parameter sweep")
    mode_group.add_argument("--senders", type=int,
                           help="Number of senders (single experiment)")

    # 单实验参数
    parser.add_argument("--flow-size", type=str, default="bdp",
                       help="Flow size: 'bdp', '10bdp', '100bdp', or bytes")
    parser.add_argument("--profile", choices=["55us", "15us"], default="55us",
                       help="Flare profile")
    parser.add_argument("--pattern", choices=["incast", "alltoall"], default="incast",
                       help="Traffic pattern")
    parser.add_argument("--stagger-ts", type=int, default=0,
                       help="Stagger incast by N time slices (0 = synchronized)")
    parser.add_argument("--mtu", type=int, default=1536,
                       help="MTU size in bytes")
    parser.add_argument("--output", type=Path, default=Path("results/credit_waste"),
                       help="Output directory")

    args = parser.parse_args(argv)

    if args.sweep:
        run_sweep(args.output)
    else:
        # 解析流大小
        bdp = estimate_bdp(args.profile)
        if args.flow_size == "bdp":
            flow_size_bytes = bdp
        elif args.flow_size == "10bdp":
            flow_size_bytes = bdp * 10
        elif args.flow_size == "100bdp":
            flow_size_bytes = bdp * 100
        else:
            flow_size_bytes = int(args.flow_size)

        config = ExperimentConfig(
            senders=args.senders,
            flow_size_bytes=flow_size_bytes,
            profile=args.profile,
            traffic_pattern=args.pattern,
            stagger_ts=args.stagger_ts,
            mtu=args.mtu,
            output_dir=args.output
        )

        run_single_experiment(config)


if __name__ == "__main__":
    main()
