"""Compare receiver-pair short/long Flare credit priority results.

Example:

    python examples/Flare/compare_receiver_pair_priority.py \
        results/receiver_pair_priority_long_first.json \
        results/receiver_pair_priority_short_first.json \
        results/receiver_pair_priority_simultaneous.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List


def _get(summary: Dict[str, Any], path: Iterable[str]) -> Any:
    value: Any = summary
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "-"
        if abs(value) >= 1e6:
            return f"{value:.3e}"
        return f"{value:.6g}"
    return str(value)


def _delta(value: Any, base: Any) -> Any:
    if value is None or base is None:
        return None
    return value - base


def _row(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary", {})
    return {
        "file": str(path),
        "order": summary.get("order"),
        "drop": summary.get("total_tor_credit_dropped"),
        "waste": summary.get("total_tor_credit_wasted"),
        "delivery": summary.get("total_credit_delivery_ratio"),
        "short_delivery": _get(summary, ("class_summary", "short", "credit_delivery_ratio")),
        "long_delivery": _get(summary, ("class_summary", "long", "credit_delivery_ratio")),
        "short_fct": _get(summary, ("class_summary", "short", "mean_fct_s")),
        "long_fct": _get(summary, ("class_summary", "long", "mean_fct_s")),
        "short_goodput": _get(summary, ("class_summary", "short", "mean_goodput_bps")),
        "long_goodput": _get(summary, ("class_summary", "long", "mean_goodput_bps")),
        "short_minus_long_delivery": summary.get("short_minus_long_credit_delivery"),
        "short_minus_long_fct": summary.get("short_minus_long_mean_fct_s"),
        "short_minus_long_goodput": summary.get("short_minus_long_mean_goodput_bps"),
    }


def _print_table(rows: List[Dict[str, Any]]) -> None:
    columns = [
        "order",
        "drop",
        "waste",
        "delivery",
        "short_delivery",
        "long_delivery",
        "short_fct",
        "long_fct",
        "short_goodput",
        "long_goodput",
    ]
    formatted = [{key: _fmt(row.get(key)) for key in columns} for row in rows]
    widths = {
        key: max(len(key), *(len(row[key]) for row in formatted))
        for key in columns
    }
    print("  ".join(key.ljust(widths[key]) for key in columns))
    print("  ".join("-" * widths[key] for key in columns))
    for row in formatted:
        print("  ".join(row[key].ljust(widths[key]) for key in columns))

    if len(rows) >= 2:
        base = rows[0]
        print(f"\nDeltas relative to {base['order']}:")
        for row in rows[1:]:
            print(
                f"  {row['order']}: "
                f"drop={_fmt(_delta(row['drop'], base['drop']))}, "
                f"waste={_fmt(_delta(row['waste'], base['waste']))}, "
                f"short_delivery={_fmt(_delta(row['short_delivery'], base['short_delivery']))}, "
                f"long_delivery={_fmt(_delta(row['long_delivery'], base['long_delivery']))}"
            )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args(argv)
    rows = [_row(path) for path in args.results]
    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
