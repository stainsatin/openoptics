"""Opera拓扑 All-to-All UDP流量模拟

在Opera拓扑上模拟All-to-All流量模式，每个节点向所有其他节点发送UDP流量。
使用UDP避免ns-3.44的TCP SACK段错误问题。
"""

from openoptics import Toolbox, OpticalTopo, OpticalRouting
import matplotlib
matplotlib.use('Agg')

if __name__ == "__main__":
    # ==================== 网络配置 ====================
    nb_node = 54          # 节点数量
    nb_link = 6          # 每个节点的上行链路数
    backend = "ns3"      # 使用ns-3后端

    # ==================== 创建网络 ====================
    net = Toolbox.BaseNetwork(
        name="opera_all_to_all_udp",
        backend="ns3",
        nb_node=nb_node,
        time_slice_duration_us=10_000,      # 时间片长度 10ms
        guardband_us=2,                 # 保护带 2us
        ocs_tor_link_bw_gbps=100,        # OCS-ToR链路带宽 10Gbps (降低容量)
        tor_host_link_bw_gbps=100,       # ToR-Host链路带宽 10Gbps (降低容量)
        use_webserver=True,             # 启用仪表板
        simulation_stop_s=1.0,          # 仿真时长 1秒
    )

    # ==================== 部署拓扑 ====================
    print(f"生成Opera拓扑: {nb_node}节点, {nb_link}链路...")
    circuits = OpticalTopo.opera(nb_node=nb_node, nb_link=nb_link)
    assert net.deploy_topo(circuits)
    print(f"拓扑部署成功! 时间片数量: {net.nb_time_slices}")

    # ==================== 部署路由 ====================
    routing_algorithm = "vlb"  # 可选: "direct", "hoho", "ksp", "vlb"
    routing_mode = "Source"    # 可选: "Per-hop" 或 "Source"

    print(f"计算路由: {routing_algorithm} ({routing_mode})...")
    if routing_algorithm == "direct":
        paths = OpticalRouting.routing_direct(net.get_topo())
    elif routing_algorithm == "hoho":
        paths = OpticalRouting.routing_hoho(net.get_topo())
    elif routing_algorithm == "ksp":
        paths = OpticalRouting.routing_ksp(net.get_topo())
    elif routing_algorithm == "vlb":
        paths = OpticalRouting.routing_vlb(net.get_topo(), net.tor_ocs_ports)
    else:
        raise ValueError(f"未知路由算法: {routing_algorithm}")

    net.deploy_routing(paths, routing_mode=routing_mode)
    print(f"路由部署成功! 路径数量: {len(paths)}")

    # ==================== 配置UDP流量 ====================
    print("配置All-to-All UDP流量...")

    # All-to-All流量参数（参考all2all_gen.py）
    flow_size_bytes = 4 * 1024 * 1024  # 4MB，与all2all_gen.py一致
    start_time_s = 0.05                # 所有流同时开始
    packet_size_bytes = 1400           # UDP包大小

    # 生成All-to-All流量矩阵（所有流同时启动，无速率限制）
    udp_gen = net.udp_traffic()
    flow_count = 0

    print(f"生成 {nb_node}x{nb_node-1} = {nb_node*(nb_node-1)} 条同时启动的流...")

    for src in range(nb_node):
        for dst in range(nb_node):
            if src != dst:  # 不向自己发送
                # 使用flow()而不是onoff()，让流以最大速率发送
                udp_gen.flow(
                    src=src,
                    dst=dst,
                    size_bytes=flow_size_bytes,
                    packet_size_bytes=packet_size_bytes,
                    start_s=start_time_s,
                    # 不指定rate，让其以链路最大速率发送
                    name=f"flow_{src}_to_{dst}"
                )
                flow_count += 1

    print(f"配置完成! 总流量数: {flow_count} ({nb_node}x{nb_node-1})")
    print(f"每个流大小: {flow_size_bytes/(1024*1024):.1f}MB")
    print(f"总流量: {flow_count * flow_size_bytes / 1e6:.1f}MB")
    print(f"所有流同时在 t={start_time_s}s 启动")

    # 安装流量并运行仿真
    installed = udp_gen.install()
    print("\n开始仿真...")
    net.start()

    # ==================== 统计结果 ====================
    import statistics

    print("\n" + "="*70)
    print("All-to-All 流量统计分析")
    print("="*70)

    # 收集所有流的统计数据
    flow_stats = []
    total_tx_bytes = 0
    total_rx_bytes = 0
    total_tx_packets = 0
    total_rx_packets = 0
    fct_list = []
    delay_list = []
    throughput_list = []
    completed_flows = 0
    incomplete_flows = 0

    for inst in installed:
        stats = inst.stats()
        if stats is not None:
            src = inst.spec.src
            dst = inst.spec.dst

            total_tx_bytes += stats.tx_bytes
            total_rx_bytes += stats.rx_bytes
            total_tx_packets += stats.tx_packets
            total_rx_packets += stats.rx_packets

            # FCT (Flow Completion Time)
            fct_ms = stats.fct_s * 1000 if stats.fct_s else 0
            if fct_ms > 0:
                fct_list.append(fct_ms)
                completed_flows += 1
            else:
                incomplete_flows += 1

            # 延迟
            if stats.delay_avg_s and stats.delay_avg_s > 0:
                delay_list.append(stats.delay_avg_s * 1000)

            # 吞吐量
            throughput_mbps = stats.throughput_bps / 1e6 if stats.throughput_bps else 0
            if throughput_mbps > 0:
                throughput_list.append(throughput_mbps)

            flow_stats.append({
                'src': src,
                'dst': dst,
                'fct_ms': fct_ms,
                'tx_bytes': stats.tx_bytes,
                'rx_bytes': stats.rx_bytes,
                'tx_packets': stats.tx_packets,
                'rx_packets': stats.rx_packets,
                'throughput_mbps': throughput_mbps,
                'delay_avg_ms': stats.delay_avg_s * 1000 if stats.delay_avg_s else 0,
            })

    # ==================== 总体统计 ====================
    print(f"\n【总体统计】")
    print(f"  流量总数: {flow_count}")
    print(f"  完成流数: {completed_flows}")
    print(f"  未完成流: {incomplete_flows}")
    print(f"  发送数据: {total_tx_bytes / 1e6:.2f} MB ({total_tx_packets} 包)")
    print(f"  接收数据: {total_rx_bytes / 1e6:.2f} MB ({total_rx_packets} 包)")

    if total_tx_packets > 0:
        packet_loss = total_tx_packets - total_rx_packets
        loss_rate = (packet_loss / total_tx_packets) * 100
        print(f"  丢包统计: {packet_loss} 包 ({loss_rate:.2f}%)")
        print(f"  投递率: {(total_rx_bytes / total_tx_bytes) * 100:.2f}%")

    # ==================== FCT统计 ====================
    if fct_list:
        print(f"\n【流完成时间 (FCT) 统计】")
        print(f"  平均 FCT: {statistics.mean(fct_list):.2f} ms")
        print(f"  中位 FCT: {statistics.median(fct_list):.2f} ms")
        print(f"  最小 FCT: {min(fct_list):.2f} ms")
        print(f"  最大 FCT: {max(fct_list):.2f} ms")
        print(f"  标准差: {statistics.stdev(fct_list):.2f} ms" if len(fct_list) > 1 else "  标准差: N/A")

        # 百分位数
        sorted_fct = sorted(fct_list)
        p50 = sorted_fct[int(len(sorted_fct) * 0.50)]
        p95 = sorted_fct[int(len(sorted_fct) * 0.95)]
        p99 = sorted_fct[int(len(sorted_fct) * 0.99)]
        print(f"  P50: {p50:.2f} ms")
        print(f"  P95: {p95:.2f} ms")
        print(f"  P99: {p99:.2f} ms")

    # ==================== 延迟统计 ====================
    if delay_list:
        print(f"\n【端到端延迟统计】")
        print(f"  平均延迟: {statistics.mean(delay_list):.2f} ms")
        print(f"  中位延迟: {statistics.median(delay_list):.2f} ms")
        print(f"  最小延迟: {min(delay_list):.2f} ms")
        print(f"  最大延迟: {max(delay_list):.2f} ms")

    # ==================== 吞吐量统计 ====================
    if throughput_list:
        print(f"\n【吞吐量统计】")
        print(f"  平均吞吐量: {statistics.mean(throughput_list):.2f} Mbps/流")
        print(f"  总吞吐量: {sum(throughput_list):.2f} Mbps")
        print(f"  最小吞吐量: {min(throughput_list):.2f} Mbps")
        print(f"  最大吞吐量: {max(throughput_list):.2f} Mbps")

    # ==================== 按源节点分组统计 ====================
    print(f"\n【按源节点分组的FCT统计】")
    print(f"{'源节点':<8} {'流数':<8} {'平均FCT(ms)':<15} {'中位FCT(ms)':<15} {'最大FCT(ms)':<15}")
    print("-" * 70)

    for src in range(nb_node):
        src_flows = [f for f in flow_stats if f['src'] == src and f['fct_ms'] > 0]
        if src_flows:
            src_fcts = [f['fct_ms'] for f in src_flows]
            print(f"Node {src:<3} {len(src_flows):<8} {statistics.mean(src_fcts):<15.2f} "
                  f"{statistics.median(src_fcts):<15.2f} {max(src_fcts):<15.2f}")

    # ==================== 最慢和最快的流 ====================
    if flow_stats:
        print(f"\n【最慢的5条流】")
        print(f"{'流':<12} {'FCT(ms)':<12} {'吞吐量(Mbps)':<15} {'丢包率(%)':<12}")
        print("-" * 60)

        sorted_by_fct = sorted([f for f in flow_stats if f['fct_ms'] > 0],
                               key=lambda x: x['fct_ms'], reverse=True)
        for f in sorted_by_fct[:5]:
            loss_rate = ((f['tx_packets'] - f['rx_packets']) / f['tx_packets'] * 100) if f['tx_packets'] > 0 else 0
            print(f"{f['src']}->{f['dst']:<8} {f['fct_ms']:<12.2f} {f['throughput_mbps']:<15.2f} {loss_rate:<12.2f}")

        print(f"\n【最快的5条流】")
        print(f"{'流':<12} {'FCT(ms)':<12} {'吞吐量(Mbps)':<15} {'丢包率(%)':<12}")
        print("-" * 60)

        for f in sorted_by_fct[-5:]:
            loss_rate = ((f['tx_packets'] - f['rx_packets']) / f['tx_packets'] * 100) if f['tx_packets'] > 0 else 0
            print(f"{f['src']}->{f['dst']:<8} {f['fct_ms']:<12.2f} {f['throughput_mbps']:<15.2f} {loss_rate:<12.2f}")

    print("\n" + "="*70)
    print(f"\n仪表板地址: http://localhost:8001")
