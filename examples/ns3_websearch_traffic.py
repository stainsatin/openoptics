"""ns-3 websearch workload on an Opera topology.

Generates a Poisson-arrival, websearch-CDF flow-size distribution workload
that mirrors the logic in generate_traffic.m, but drives it through the
openoptics TCP traffic builder instead of writing an htsim trace file.

Traffic model (same as generate_traffic.m):
  - Inter-arrival times: exponential with rate lambda_network
  - Flow sizes: sampled from the websearch CDF (embedded below)
  - Source/destination: uniform random over all inter-rack host pairs
  - Only inter-rack flows (src_rack != dst_rack)

Usage::

    python examples/ns3_websearch_traffic.py [--load 0.1] [--nodes 8]
                                              [--links 2]
                                              [--hosts-per-tor 6]
                                              [--duration 1.0]
                                              [--seed 42]
                                              [--routing {direct,vlb,hoho}]
                                              [--max-flows N]
"""

from __future__ import annotations

import argparse
import math
import random
import sys

from openoptics import Toolbox, OpticalTopo, OpticalRouting


# ---------------------------------------------------------------------------
# Websearch flow-size CDF (source: websearch.csv)
# Each entry is (flow_size_bytes, cdf_value).
# The first entry has cdf=0 and serves as the lower-bound bin only.
# ---------------------------------------------------------------------------

_WEBSEARCH_CDF: list[tuple[int, float]] = [
    (      4_000, 0.000000000),
    (      5_971, 0.077049180),
    (      8_722, 0.152459016),
    (     18_614, 0.195081967),
    (     27_563, 0.300000000),
    (     44_871, 0.427868852),
    (     77_113, 0.532786885),
    (    193_593, 0.601639344),
    (    620_119, 0.696721311),
    (  1_933_313, 0.801639344),
    (  3_147_330, 0.860655738),
    (  4_853_634, 0.903278689),
    (  6_901_099, 0.942622951),
    (  9_812_271, 0.972131148),
    ( 15_759_080, 0.985245902),
    ( 28_589_215, 1.000000000),
]

_WEBSEARCH_SIZES = [s for s, _ in _WEBSEARCH_CDF]
_WEBSEARCH_CDFS  = [c for _, c in _WEBSEARCH_CDF]


# ---------------------------------------------------------------------------
# Websearch CDF helpers
# ---------------------------------------------------------------------------

def _sample_flow_size(sizes: list[float], cdfs: list[float], u: float) -> int:
    """Inverse-CDF sample: return flow size in bytes for uniform draw u in (0,1].

    Skips leading entries whose cdf==0 (zero-probability bin lower bound).
    """
    for i, c in enumerate(cdfs):
        if c > 0 and c >= u:
            return int(sizes[i])
    return int(sizes[-1])


def _avg_flow_size(sizes: list[float], cdfs: list[float]) -> float:
    """E[X] via the empirical PMF derived from CDF differences.

    P(size_i) = CDF[i] - CDF[i-1].  The first bin (cdf=0) contributes zero
    probability so it does not affect the mean.
    """
    avg = 0.0
    for i in range(1, len(sizes)):
        p = cdfs[i] - cdfs[i - 1]
        avg += sizes[i] * p
    return avg


# ---------------------------------------------------------------------------
# Flow schedule generation (mirrors generate_traffic.m)
# ---------------------------------------------------------------------------

def generate_websearch_flows(
    *,
    nb_node: int,
    nb_host_per_tor: int,
    load_frac: float,
    total_time_s: float,
    link_rate_gbps: float,
    sizes: list[float],
    cdfs: list[float],
    rng: random.Random,
    max_flows: int = 0,
) -> list[tuple[int, int, int, float]]:
    """Return list of (src_host, dst_host, size_bytes, start_s).

    Host IDs are 0-based globals: host_id = rack_id * nb_host_per_tor + local.
    Arrivals follow a Poisson process at rate lambda_network; flow sizes are
    drawn from the websearch CDF; src/dst pairs are uniform over inter-rack
    pairs (same distribution as generate_traffic.m).

    Args:
        max_flows: if > 0, stop after generating this many flows (safety cap).
    """
    nb_host = nb_node * nb_host_per_tor
    link_rate_Bps = link_rate_gbps * 1e9 / 8.0   # bytes per second

    avg_size_B = _avg_flow_size(_WEBSEARCH_SIZES, _WEBSEARCH_CDFS)
    lambda_host_max = link_rate_Bps / avg_size_B  # flows/s per host at 100% load
    lambda_host = load_frac * lambda_host_max
    lambda_net = nb_host * lambda_host            # flows/s for whole network

    # Pre-build list of all inter-rack (src, dst) host-index pairs
    pairs: list[tuple[int, int]] = []
    for src in range(nb_host):
        src_rack = src // nb_host_per_tor
        for dst in range(nb_host):
            if dst // nb_host_per_tor != src_rack:
                pairs.append((src, dst))
    nb_pairs = len(pairs)

    flows: list[tuple[int, int, int, float]] = []
    t = 0.0
    while t < total_time_s:
        # Exponential inter-arrival: -ln(1-U) / lambda, U ~ Uniform(0,1)
        # Use (1 - rng.random()) to avoid log(0)
        t += -math.log(1.0 - rng.random()) / lambda_net
        if t >= total_time_s:
            break
        size_B = _sample_flow_size(sizes, cdfs, rng.random())
        src, dst = pairs[rng.randint(0, nb_pairs - 1)]
        flows.append((src, dst, size_B, t))
        if max_flows > 0 and len(flows) >= max_flows:
            break

    return flows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--load", type=float, default=0.1,
                        help="Fraction of host-link capacity offered (default: 0.1)")
    parser.add_argument("--nodes", type=int, default=8,
                        help="Number of ToR nodes (default: 8)")
    parser.add_argument("--links", type=int, default=2,
                        help="Number of uplinks per ToR / optical degree (default: 2)")
    parser.add_argument("--hosts-per-tor", type=int, default=6,
                        help="Hosts per ToR switch (default: 6)")
    parser.add_argument("--duration", type=float, default=1.0,
                        help="Simulation duration in seconds (default: 1.0)")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for reproducibility (default: 42)")
    parser.add_argument("--routing", choices=["direct", "vlb", "hoho"],
                        default="hoho",
                        help="Routing algorithm (default: hoho)")
    parser.add_argument("--max-flows", type=int, default=0,
                        help="Safety cap on number of installed flows; 0 = no cap (default: 0)")
    parser.add_argument("--ocs-bw", type=float, default=100.0,
                        help="OCS-ToR link bandwidth in Gbps (default: 100)")
    parser.add_argument("--host-bw", type=float, default=10.0,
                        help="ToR-host link bandwidth in Gbps (default: 10)")
    parser.add_argument("--slice-us", type=int, default=10_000,
                        help="Time-slice duration in µs (default: 10000)")
    args = parser.parse_args(argv)

    # --- Generate flow schedule ---
    rng = random.Random(args.seed)
    flows = generate_websearch_flows(
        nb_node=args.nodes,
        nb_host_per_tor=args.hosts_per_tor,
        load_frac=args.load,
        total_time_s=args.duration,
        link_rate_gbps=args.host_bw,
        sizes=_WEBSEARCH_SIZES,
        cdfs=_WEBSEARCH_CDFS,
        rng=rng,
        max_flows=args.max_flows,
    )

    if not flows:
        print("No flows generated — load may be too low or duration too short.")
        sys.exit(0)

    nb_host = args.nodes * args.hosts_per_tor
    avg_size_B = _avg_flow_size(_WEBSEARCH_SIZES, _WEBSEARCH_CDFS)
    total_bytes = sum(f[2] for f in flows)
    actual_load = total_bytes / (args.duration * nb_host * args.host_bw * 1e9 / 8.0)
    print(
        f"Flow schedule: {len(flows)} flows, "
        f"avg websearch size {avg_size_B / 1e3:.1f} KB, "
        f"target load {args.load:.3f}, actual load {actual_load:.3f}"
    )

    # --- Build network ---
    net = Toolbox.BaseNetwork(
        name=f"ns3_websearch_{args.routing}",
        backend="ns3",
        nb_node=args.nodes,
        nb_link=args.links,
        nb_host_per_tor=1,   # ns3 backend supports exactly 1 host per ToR
        time_slice_duration_us=args.slice_us,
        guardband_ms=0,
        ocs_tor_link_bw_gbps=args.ocs_bw,
        tor_host_link_bw_gbps=args.host_bw,
        use_webserver=True,
        simulation_stop_s=args.duration,
    )

    circuits = OpticalTopo.opera(nb_node=args.nodes, nb_link=args.links)
    assert net.deploy_topo(circuits)

    if args.routing == "direct":
        paths = OpticalRouting.routing_direct(net.get_topo())
        assert net.deploy_routing(paths, routing_mode="Per-hop")
    elif args.routing == "vlb":
        paths = OpticalRouting.routing_vlb(net.get_topo(), net.tor_ocs_ports)
        assert net.deploy_routing(paths, routing_mode="Source")
    else:  # hoho
        paths = OpticalRouting.routing_hoho(net.get_topo())
        assert net.deploy_routing(paths, routing_mode="Per-hop")

    # --- Install TCP flows ---
    # openoptics identifies endpoints by ToR index, not individual host index.
    # Multiple hosts on the same ToR sharing one TCP bulk sender is a known
    # simplification: the host-level fanout is not modelled at ToR granularity.
    def host_to_tor(h: int) -> int:
        return h // args.hosts_per_tor

    tcp = net.tcp_traffic()
    installed_count = 0
    for i, (src_h, dst_h, size_b, start_s) in enumerate(flows):
        src_tor = host_to_tor(src_h)
        dst_tor = host_to_tor(dst_h)
        if src_tor == dst_tor:
            # guard: pairs list already excludes intra-rack, should not happen
            continue
        # stop_s: let the flow run until it finishes or simulation ends
        stop_s = args.duration
        tcp.bulk(
            src_tor, dst_tor,
            size_bytes=size_b,
            chunk_size_bytes=1448,
            start_s=start_s,
            stop_s=stop_s,
            name=f"ws_{i}",
        )
        installed_count += 1

    print(f"Installing {installed_count} TCP flows…")
    installed = tcp.install()

    print("\nStarting simulation...")
    print("Note: FCT statistics will be displayed automatically after simulation")
    print("Note: Set OPENOPTICS_NS3_NO_PAUSE=1 to skip 'Press Enter' prompt")
    net.start()


if __name__ == "__main__":
    main()
