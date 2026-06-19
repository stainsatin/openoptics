"""Run a small Flare microbenchmark sweep and aggregate results.

This wraps ``examples/ns3_flare_microbench.py`` so a regression run can cover
both paper profiles and several mechanism scenarios without hand-launching
each command.

Example:

    python examples/ns3_flare_sweep.py --out-dir results/flare_sweep
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path


DEFAULT_SCENARIOS = [
    "single",
    "incast",
    "credit-pressure",
    "dual-bottleneck",
    "slice-switch",
]
DEFAULT_PROFILES = ["55us", "15us"]


def _run_one(args, scenario: str, profile: str) -> dict:
    json_path = args.out_dir / f"{scenario}_{profile}.json"
    csv_path = args.out_dir / f"{scenario}_{profile}.csv"
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("ns3_flare_microbench.py")),
        "--scenario", scenario,
        "--profile", profile,
        "--nodes", str(args.nodes),
        "--links", str(args.links),
        "--flow-size", str(args.flow_size),
        "--flows", str(args.flows),
        "--fan-in", str(args.fan_in),
        "--duration", str(args.duration),
        "--stop", str(args.stop),
        "--ocs-bw", str(args.ocs_bw),
        "--host-bw", str(args.host_bw),
        "--output", str(json_path),
        "--csv", str(csv_path),
    ]
    if args.topology != "auto":
        cmd.extend(["--topology", args.topology])
    if args.routing != "auto":
        cmd.extend(["--routing", args.routing])
    if args.initial_credit:
        cmd.extend(["--initial-credit", str(args.initial_credit)])

    print("running:", " ".join(cmd))
    completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
    if completed.stdout:
        print(completed.stdout.strip())
    if completed.stderr:
        print(completed.stderr.strip(), file=sys.stderr)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    summary = dict(payload["summary"])
    summary["json_path"] = str(json_path)
    summary["csv_path"] = str(csv_path)
    return summary


def _write_aggregate(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", nargs="+", default=DEFAULT_SCENARIOS)
    parser.add_argument("--profiles", nargs="+", default=DEFAULT_PROFILES,
                        choices=DEFAULT_PROFILES)
    parser.add_argument("--out-dir", type=Path, default=Path("flare_sweep"))
    parser.add_argument("--topology", choices=["auto", "round-robin", "opera"],
                        default="auto")
    parser.add_argument("--routing", choices=["auto", "direct", "hoho"],
                        default="auto")
    parser.add_argument("--nodes", type=int, default=4)
    parser.add_argument("--links", type=int, default=1)
    parser.add_argument("--flow-size", type=int, default=64_000)
    parser.add_argument("--flows", type=int, default=8)
    parser.add_argument("--fan-in", type=int, default=3)
    parser.add_argument("--initial-credit", type=int, default=0)
    parser.add_argument("--duration", type=float, default=0.02)
    parser.add_argument("--stop", type=float, default=0.05)
    parser.add_argument("--ocs-bw", type=float, default=100.0)
    parser.add_argument("--host-bw", type=float, default=100.0)
    args = parser.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for scenario in args.scenarios:
        for profile in args.profiles:
            rows.append(_run_one(args, scenario, profile))

    aggregate_json = args.out_dir / "aggregate.json"
    aggregate_csv = args.out_dir / "aggregate.csv"
    aggregate_json.write_text(
        json.dumps({"runs": rows}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_aggregate(aggregate_csv, rows)
    print(f"wrote {aggregate_json}")
    print(f"wrote {aggregate_csv}")


if __name__ == "__main__":
    main()
