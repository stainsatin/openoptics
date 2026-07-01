from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List


SCRIPT_PATH = Path(__file__).resolve()
for root in (SCRIPT_PATH.parents[2], Path.cwd()):
    if (root / "openoptics").exists() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
        break

from openoptics import OpticalRouting, OpticalTopo, Toolbox
from openoptics.backends.ns3.traffic import FlareConfig, FlareStats


def _slice_us(profile: str) -> int:
    return 55 if profile == "55us" else 15


def _is_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _safe_ratio(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


def _rel_delta(a: float, b: float) -> float | None:
    scale = max(abs(a), abs(b))
    return None if scale == 0 else abs(a - b) / scale


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _build_network(args: argparse.Namespace, label: str, w_init: float):
    slice_us = _slice_us(args.profile)
    net = Toolbox.BaseNetwork(
        name=f"flare_rate_control_smoke_{label}",
        backend="ns3",
        nb_node=2,
        nb_link=1,
        time_slice_duration_us=slice_us,
        guardband_us=args.guardband_us,
        ocs_tor_link_bw_gbps=args.ocs_bw,
        tor_host_link_bw_gbps=args.host_bw,
        use_webserver=args.dashboard,
        simulation_stop_s=args.stop,
        snapshot_interval_us=max(1, slice_us),
        host_link_delay_us=args.host_delay_us,
        ocs_link_delay_us=args.ocs_delay_us,
        flare_profile=args.profile,
        flare_w_init=w_init,
        flare_target_loss=args.target_loss,
        flare_credit_qsize_pkts=args.flare_credit_qsize,
        flare_shaping_thresh_pkts=args.flare_shaping_thresh,
        flare_aeolus_thresh_pkts=args.flare_aeolus_thresh,
        flare_congestion_threshold_percent=args.flare_congestion_threshold,
        flare_tentative_threshold_percent=args.flare_tentative_threshold,
    )
    net.deploy_topo(OpticalTopo.round_robin(nb_node=2))
    net.deploy_routing(OpticalRouting.routing_direct(net.get_topo()), routing_mode="Per-hop")
    return net


def _flow_config(args: argparse.Namespace, w_init: float) -> FlareConfig:
    return FlareConfig.from_profile(
        args.profile,
        credit_qsize_pkts=args.flare_credit_qsize,
        shaping_thresh_pkts=args.flare_shaping_thresh,
        aeolus_thresh_pkts=args.flare_aeolus_thresh,
        w_init=w_init,
        target_loss=args.target_loss,
        mtu_bytes=args.mtu,
        initial_credit_pkts=args.initial_credit,
        congestion_threshold_percent=args.flare_congestion_threshold,
        tentative_threshold_percent=args.flare_tentative_threshold,
    )


def _run_case(args: argparse.Namespace, label: str, w_init: float) -> Dict[str, Any]:
    print(f"\n=== Running {label}: flare_w_init={w_init} ===", flush=True)
    net = _build_network(args, label, w_init)
    config = _flow_config(args, w_init)
    gen = net.flare_traffic(config=config)
    gen.flow(
        0,
        1,
        args.flow_size,
        start_s=args.start,
        duration_s=args.duration,
        packet_size_bytes=args.mtu,
        name=label,
    )
    installed = gen.install()
    net.start()

    stats = installed[0].stats()
    if stats is None:
        raise RuntimeError("Flare stats were not available after the ns-3 run")
    return _stats_row(args, label, w_init, stats)


def _stats_row(
    args: argparse.Namespace,
    label: str,
    w_init: float,
    stats: FlareStats,
) -> Dict[str, Any]:
    observed_duration = (
        float(stats.fct_s)
        if _is_finite(stats.fct_s) and float(stats.fct_s) > 0
        else max(1e-12, args.stop - args.start)
    )
    credit_send_rate_pps = stats.credits_sent / observed_duration
    data_send_rate_pps = stats.data_packets_sent / observed_duration
    return {
        "label": label,
        "backend_flare_w_init": w_init,
        "fixed_initial_credit_pkts": args.initial_credit,
        "flow_packets": math.ceil(args.flow_size / args.mtu),
        "fct_s": stats.fct_s,
        "observed_duration_s": observed_duration,
        "throughput_bps": stats.throughput_bps,
        "credits_sent": stats.credits_sent,
        "credits_received": stats.credits_received,
        "credits_admitted": stats.credits_admitted,
        "credits_dropped": stats.credits_dropped,
        "credits_wasted": stats.credits_wasted,
        "credit_send_rate_pps": credit_send_rate_pps,
        "credit_count_per_initial_window": _safe_ratio(
            stats.credits_sent, args.initial_credit
        ),
        "data_packets_sent": stats.data_packets_sent,
        "data_packets_received": stats.data_packets_received,
        "data_send_rate_pps": data_send_rate_pps,
        "data_received_per_credit_sent": _safe_ratio(
            stats.data_packets_received, stats.credits_sent
        ),
        "duplicate_credits": stats.duplicate_credits,
        "duplicate_data": stats.duplicate_data,
        "retransmissions": stats.retransmissions,
        "timeouts": stats.timeouts,
    }


def _compare(args: argparse.Namespace, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    high = next(row for row in rows if row["label"] == "high")
    low = next(row for row in rows if row["label"] == "low")

    credit_count_delta = _rel_delta(high["credits_sent"], low["credits_sent"])
    credit_rate_delta = _rel_delta(
        high["credit_send_rate_pps"], low["credit_send_rate_pps"]
    )
    data_rate_delta = _rel_delta(high["data_send_rate_pps"], low["data_send_rate_pps"])
    low_high_credit_rate_ratio = _safe_ratio(
        low["credit_send_rate_pps"], high["credit_send_rate_pps"]
    )
    low_high_credit_count_ratio = _safe_ratio(low["credits_sent"], high["credits_sent"])

    no_drop_confounds = all(
        row["credits_dropped"] == 0 and row["credits_wasted"] == 0 for row in rows
    )
    high_no_drop_confounds = (
        high["credits_dropped"] == 0 and high["credits_wasted"] == 0
    )
    regular_credit_unpaced_signal = (
        high_no_drop_confounds
        and high["credits_sent"] >= high["flow_packets"]
        and (
            high["credit_count_per_initial_window"] is not None
            and high["credit_count_per_initial_window"] >= args.burst_multiplier
        )
    )
    low_admitted_not_usable_fraction = _safe_ratio(
        low["credits_admitted"] - low["credits_received"],
        low["credits_admitted"],
    )
    low_tentative_not_rate_control_signal = (
        no_drop_confounds
        and low_admitted_not_usable_fraction is not None
        and low_admitted_not_usable_fraction >= args.tentative_gap_fraction
    )
    host_rate_control_missing_signal = regular_credit_unpaced_signal

    return {
        "topology": "2 ToRs, one direct optical link, one Flare flow 0->1",
        "control_variable": (
            "backend flare_w_init, with initial_credit_pkts held constant"
        ),
        "expected_if_w_init_were_send_rate_control": (
            "w_init should pace or cap usable credit emission, not just change credit type"
        ),
        "regular_credit_path_signal": (
            "all-regular run emits far more credits than the fixed initial window"
            if regular_credit_unpaced_signal
            else "all-regular run did not expose the expected burst/replenish signal"
        ),
        "low_w_init_signal": (
            "low w_init creates admitted credits that the sender does not count as usable"
            if low_tentative_not_rate_control_signal
            else "low w_init did not expose a large admitted-vs-usable credit gap"
        ),
        "no_drop_confounds": no_drop_confounds,
        "regular_credit_unpaced_signal": regular_credit_unpaced_signal,
        "low_tentative_not_rate_control_signal": low_tentative_not_rate_control_signal,
        "low_admitted_not_usable_fraction": low_admitted_not_usable_fraction,
        "credit_count_relative_delta": credit_count_delta,
        "credit_rate_relative_delta": credit_rate_delta,
        "data_rate_relative_delta": data_rate_delta,
        "low_high_credit_count_ratio": low_high_credit_count_ratio,
        "low_high_credit_rate_ratio": low_high_credit_rate_ratio,
        "tolerance": args.tolerance,
        "burst_multiplier": args.burst_multiplier,
        "tentative_gap_fraction": args.tentative_gap_fraction,
        "host_rate_control_missing_signal": host_rate_control_missing_signal,
        "no_host_rate_control_signal": host_rate_control_missing_signal,
        "verdict": (
            "Current Flare host logic lacks explicit credit pacing in the regular-credit path."
            if host_rate_control_missing_signal
            else "This run did not isolate a clean missing-host-rate-control signal."
        ),
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["55us", "15us"], default="15us")
    parser.add_argument("--high-w-init", type=float, default=1.0)
    parser.add_argument("--low-w-init", type=float, default=0.1)
    parser.add_argument("--target-loss", type=float, default=0.1)
    parser.add_argument("--flow-size", type=int, default=262_144)
    parser.add_argument("--mtu", type=int, default=1024)
    parser.add_argument("--initial-credit", type=int, default=8)
    parser.add_argument("--start", type=float, default=0.0001)
    parser.add_argument("--duration", type=float, default=0.001)
    parser.add_argument("--stop", type=float, default=0.002)
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    parser.add_argument("--host-delay-us", type=int, default=1)
    parser.add_argument("--ocs-delay-us", type=int, default=1)
    parser.add_argument("--guardband-us", type=int, default=0)
    parser.add_argument("--flare-credit-qsize", type=int, default=4096)
    parser.add_argument("--flare-shaping-thresh", type=int, default=4096)
    parser.add_argument("--flare-aeolus-thresh", type=int, default=4096)
    parser.add_argument("--flare-congestion-threshold", type=int, default=100)
    parser.add_argument("--flare-tentative-threshold", type=int, default=100)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--burst-multiplier", type=float, default=4.0)
    parser.add_argument("--tentative-gap-fraction", type=float, default=0.5)
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/flare_rate_control_smoke.json"),
    )
    parser.add_argument(
        "--assert-no-rate-control",
        action="store_true",
        help="exit with code 1 if the expected no-rate-control signal is absent",
    )
    args = parser.parse_args(argv)

    rows = [
        _run_case(args, "high", args.high_w_init),
        _run_case(args, "low", args.low_w_init),
    ]
    summary = _compare(args, rows)
    payload = {
        "experiment": "flare_rate_control_smoke",
        "parameters": {
            "profile": args.profile,
            "flow_size": args.flow_size,
            "mtu": args.mtu,
            "initial_credit": args.initial_credit,
            "high_w_init": args.high_w_init,
            "low_w_init": args.low_w_init,
            "duration": args.duration,
            "stop": args.stop,
        },
        "summary": summary,
        "runs": rows,
    }
    _write_json(args.output, payload)

    print("\n=== Flare Rate-Control Smoke Test Summary ===")
    print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
    print(f"\nWrote {args.output}")

    if args.assert_no_rate_control and not summary["no_host_rate_control_signal"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
