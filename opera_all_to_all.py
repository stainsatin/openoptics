"""Opera拓扑 All-to-All 流量模拟

在Opera拓扑上模拟All-to-All流量模式，每个节点向所有其他节点发送TCP流量。
支持多种路由算法对比：direct, hoho, vlb等。

改进：
- 添加随机流启动时间，避免所有流同时启动导致的段错误
- 添加FCT（Flow Completion Time）统计
- 支持限制最大流数量用于测试
"""

from __future__ import annotations

import argparse
import random

from openoptics import Toolbox, OpticalTopo, OpticalRouting


if __name__ == "__main__":
    # ==================== 命令行参数 ====================
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--nodes", type=int, default=54,
                        help="节点数量 (default: 54)")
    parser.add_argument("--links", type=int, default=6,
                        help="每个节点的上行链路数 (default: 6)")
    parser.add_argument("--flow-size", type=int, default=500_000,
                        help="每个流的大小（字节） (default: 500000)")
    parser.add_argument("--duration", type=float, default=1.0,
                        help="仿真时长（秒） (default: 1.0)")
    parser.add_argument("--start-window", type=float, default=0.15,
                        help="流启动时间窗口（秒），流将在[0.05, 0.05+window]内随机启动 (default: 0.15)")
    parser.add_argument("--routing", choices=["direct", "vlb", "hoho", "ksp"],
                        default="hoho",
                        help="路由算法 (default: hoho)")
    parser.add_argument("--max-flows", type=int, default=0,
                        help="最大流数量限制，0表示无限制 (default: 0)")
    parser.add_argument("--seed", type=int, default=42,
                        help="随机数种子 (default: 42)")
    parser.add_argument("--backend", choices=["Mininet", "ns3"],
                        default="ns3",
                        help="后端选择 (default: ns3)")
    args = parser.parse_args()

    # ==================== 网络配置 ====================
    nb_node = args.nodes
    nb_link = args.links
    backend = args.backend

    random.seed(args.seed)

    # ==================== 创建网络 ====================
    if backend == "Mininet":
        net = Toolbox.BaseNetwork(
            name="opera_all_to_all_mininet",
            backend="Mininet",
            nb_node=nb_node,
            nb_link=nb_link,
            time_slice_duration_ms=64,  # 时间片长度 64ms
            use_webserver=True,         # 启用仪表板
        )
    else:  # ns3
        net = Toolbox.BaseNetwork(
            name="opera_all_to_all_ns3",
            backend="ns3",
            nb_node=nb_node,
            nb_link=nb_link,
            time_slice_duration_us=50,      # 时间片长度 50us
            guardband_us=2,                 # 保护带 2us
            ocs_tor_link_bw_gbps=100,       # OCS-ToR链路带宽 100Gbps
            tor_host_link_bw_gbps=1,        # ToR-Host链路带宽 1Gbps
            use_webserver=True,             # 启用仪表板
            simulation_stop_s=args.duration,  # 仿真时长
        )

    # ==================== 部署拓扑 ====================
    print(f"生成Opera拓扑: {nb_node}节点, {nb_link}链路...")
    circuits = OpticalTopo.opera(nb_node=nb_node, nb_link=nb_link)
    assert net.deploy_topo(circuits)
    print(f"拓扑部署成功! 时间片数量: {net.nb_time_slices}")

    # ==================== 部署路由 ====================
    # 可选路由算法:
    # 1. routing_direct - 直连路由（仅在直连时发送）
    # 2. routing_hoho - HoHo路由（最短路径，支持多跳）
    # 3. routing_ksp - 每个时间片的最短路径
    # 4. routing_vlb - Valiant负载均衡（随机中间节点）

    routing_algorithm = args.routing
    routing_mode = "Per-hop"    # 可选: "Per-hop" 或 "Source"

    print(f"计算路由: {routing_algorithm} ({routing_mode})...")
    if routing_algorithm == "direct":
        paths = OpticalRouting.routing_direct(net.get_topo())
    elif routing_algorithm == "hoho":
        paths = OpticalRouting.routing_hoho(net.get_topo())
    elif routing_algorithm == "ksp":
        paths = OpticalRouting.routing_ksp(net.get_topo())
    elif routing_algorithm == "vlb":
        paths = OpticalRouting.routing_vlb(net.get_topo(), net.tor_ocs_ports)
        routing_mode = "Source"  # VLB需要源路由
    else:
        raise ValueError(f"未知路由算法: {routing_algorithm}")

    net.deploy_routing(paths, routing_mode=routing_mode)
    print(f"路由部署成功! 路径数量: {len(paths)}")

    # ==================== 配置流量 ====================
    if backend == "ns3":
        print("配置All-to-All TCP流量...")

        # All-to-All流量参数
        flow_size_bytes = args.flow_size
        tcp_chunk_size = 1448          # TCP分段大小
        start_time_base = 0.05         # 基础启动时间
        start_window = args.start_window  # 启动时间窗口
        stop_time_s = args.duration - 0.1  # 流量停止时间

        # 生成All-to-All流量矩阵，添加随机启动时间
        tcp_gen = net.tcp_traffic()
        flow_count = 0
        flow_specs = []  # 保存流规格用于统计

        print(f"生成流启动时间（随机分布在 [{start_time_base:.2f}, {start_time_base + start_window:.2f}] 秒内）...")

        for src in range(nb_node):
            for dst in range(nb_node):
                if src != dst:  # 不向自己发送
                    # 为每个流分配随机启动时间，避免同时启动
                    start_s = start_time_base + random.uniform(0, start_window)

                    tcp_gen.bulk(
                        src=src,
                        dst=dst,
                        size_bytes=flow_size_bytes,
                        chunk_size_bytes=tcp_chunk_size,
                        start_s=start_s,
                        stop_s=stop_time_s,
                        name=f"h{src}->h{dst}",
                    )
                    flow_specs.append((src, dst, start_s))
                    flow_count += 1

                    # 如果设置了最大流数量限制
                    if args.max_flows > 0 and flow_count >= args.max_flows:
                        break
            if args.max_flows > 0 and flow_count >= args.max_flows:
                break

        print(f"配置完成! 总流量数: {flow_count}")
        print(f"每个流大小: {flow_size_bytes/1000:.1f}KB")
        print(f"总流量: {flow_count * flow_size_bytes / 1e6:.1f}MB")
        if args.max_flows > 0:
            print(f"（已限制最大流数量为 {args.max_flows}）")

        # 安装流量并运行仿真
        print("\n安装TCP流量...")
        installed = tcp_gen.install()
        print(f"已安装 {len(installed)} 个流")

        print("\n开始仿真...")
        print("提示: FCT统计将在仿真结束后自动显示")
        print("提示: 设置环境变量 OPENOPTICS_NS3_NO_PAUSE=1 可跳过按回车")
        net.start()

    else:  # Mininet
        print("\n" + "="*70)
        print("Mininet模式 - 交互式测试")
        print("="*70)
        print("\n可用命令:")
        print("  h0 ping h1              - 主机间ping测试")
        print("  h0 iperf -s             - 在h0启动iperf服务器")
        print("  h1 iperf -c 10.0.0.0 -t 10  - 从h1向h0发送10秒流量")
        print("  get_num_queued_packets  - 查看队列深度")
        print("  get_packet_loss_ctr     - 查看丢包统计")
        print("  exit                    - 退出")
        print("\n仪表板地址: http://localhost:8001")
        print("="*70 + "\n")

        net.start()
