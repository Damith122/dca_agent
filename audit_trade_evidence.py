"""Read-only audit of logged round trips, excluding simulated/unknown exits.

No backtest or profitability prediction: changing execution invalidates a
counterfactual made by simply deleting historical losers. Funding is not in
these trade rows and must be reconciled separately before claiming net edge.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path


def read_trades(paths, since=None):
    rows, seen, excluded = [], set(), Counter()
    for path in paths:
        for line in Path(path).read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            try:
                oid = int(row["exit_order_id"])
            except (KeyError, TypeError, ValueError):
                excluded["unknown_exit_id"] += 1; continue
            if oid <= 0:
                excluded["simulated_exit_id"] += 1; continue
            ts = datetime.strptime(row["close_time"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
            if since and ts < since:
                excluded["before_cutoff"] += 1; continue
            key = (row["symbol"], oid)
            if key in seen:
                excluded["duplicate_exit_id"] += 1; continue
            seen.add(key)
            for field in ("gross_pnl_usdt", "fees_usdt", "net_pnl_usdt"):
                if not math.isfinite(float(row[field])):
                    raise ValueError(f"Non-finite {field} in {path}")
            if not math.isclose(float(row["gross_pnl_usdt"]) - float(row["fees_usdt"]),
                                float(row["net_pnl_usdt"]), abs_tol=0.00001):
                raise ValueError(f"PnL does not reconcile in {path}, exit {oid}")
            rows.append(row)
    return sorted(rows, key=lambda r: r["close_time"]), dict(excluded)


def summarize(rows):
    wins = [float(r["net_pnl_usdt"]) for r in rows if float(r["net_pnl_usdt"]) > 0]
    losses = [float(r["net_pnl_usdt"]) for r in rows if float(r["net_pnl_usdt"]) < 0]
    net = sum(float(r["net_pnl_usdt"]) for r in rows)
    return {"trades": len(rows), "wins": len(wins), "losses": len(losses),
            "gross_usdt": sum(float(r["gross_pnl_usdt"]) for r in rows),
            "fees_usdt": sum(float(r["fees_usdt"]) for r in rows), "net_usdt": net,
            "mean_net_usdt": net / len(rows) if rows else None,
            "profit_factor": sum(wins) / -sum(losses) if losses else None,
            "first_close": rows[0]["close_time"] if rows else None,
            "last_close": rows[-1]["close_time"] if rows else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--since", help="UTC ISO timestamp, e.g. 2026-08-31T14:54:00Z")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since else None
    if since is not None and since.tzinfo is None:
        parser.error("--since requires an explicit timezone")
    rows, excluded = read_trades(args.paths, since)
    groups = defaultdict(list)
    for row in rows:
        groups[row["exit_reason"]].append(row)
    print(json.dumps({"scope": "positive exchange-exit IDs; logged commissions included, funding excluded",
                      "excluded": excluded, "total": summarize(rows),
                      "by_exit": {k: summarize(v) for k, v in sorted(groups.items())},
                      "profitability_proven": False}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
