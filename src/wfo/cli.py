"""``wfo`` command line: simulate | report | run | generate | serve."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from wfo.config import Settings
from wfo.models import Contact
from wfo.routing import STRATEGIES

CSV_FIELDS = (
    "id",
    "first_name",
    "last_name",
    "company_domain",
    "country",
    "employees",
    "seniority",
)


def _cmd_simulate(args: argparse.Namespace) -> int:
    from wfo.sim.charts import render_all
    from wfo.sim.harness import SimConfig, simulate
    from wfo.sim.report import headline, render_report

    cfg = SimConfig(
        n_contacts=args.contacts,
        seeds=tuple(range(1, args.seeds + 1)),
        value_per_verified=args.value,
        budget_usd=args.budget,
        fault_scenario=not args.no_faults,
        workers=args.workers,
        world_config=args.world,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = simulate(cfg)
    (out / "results.json").write_text(json.dumps(results, indent=1))
    render_all(results, out)
    report = render_report(results, out)
    print(json.dumps(headline(results), indent=2))
    print(f"wrote {out / 'results.json'}, charts and {report}")
    return 0


def _cmd_sensitivity(args: argparse.Namespace) -> int:
    from wfo.sim.calibration import load
    from wfo.sim.charts import sensitivity_chart
    from wfo.sim.harness import SimConfig
    from wfo.sim.report import render_report
    from wfo.sim.sensitivity import default_variants, run_sweep

    providers = sorted(s.name for s in load(args.world).specs)
    variants = default_variants(providers, args.scale)
    if args.only:
        keep = set(args.only.split(","))
        variants = [v for v in variants if v.key in keep]
    cfg = SimConfig(
        n_contacts=args.contacts,
        seeds=tuple(range(1, args.seeds + 1)),
        value_per_verified=args.value,
        workers=args.workers,
        world_config=args.world,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sweep = run_sweep(cfg, variants)
    (out / "sensitivity.json").write_text(json.dumps(sweep, indent=1))
    sensitivity_chart(sweep, out / "sensitivity.png")
    results_path = out / "results.json"
    if results_path.exists():
        render_report(json.loads(results_path.read_text()), out)
    for row in sweep["variants"]:
        d = row["thompson_vs_static"]["cpv_reduction_pct"]
        n = row["thompson_vs_static"]["net_value_delta_usd"]
        print(
            f"{row['key']:22s} cpv -{d['mean']:.1f}% ± {d['ci95']:.1f}  "
            f"net/contact {n['mean']:+.4f} ± {n['ci95']:.4f}"
        )
    print(f"wrote {out / 'sensitivity.json'} and {out / 'sensitivity.png'}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from wfo.sim.charts import render_all
    from wfo.sim.report import render_report

    results = json.loads(Path(args.results).read_text())
    out = Path(args.out) if args.out else Path(args.results).parent
    render_all(results, out)
    print(f"wrote {render_report(results, out)}")
    return 0


def read_contacts(path: Path) -> list[Contact]:
    with path.open(newline="") as f:
        return [Contact.from_dict(row) for row in csv.DictReader(f)]


def _cmd_generate(args: argparse.Namespace) -> int:
    from wfo.sim.world import SyntheticWorld

    contacts = SyntheticWorld(args.seed).generate_contacts(args.n)
    with Path(args.out).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for c in contacts:
            w.writerow(c.to_dict())
    print(f"wrote {len(contacts)} synthetic contacts to {args.out}")
    return 0


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from wfo.service import build_service

    settings = Settings.from_env()
    settings = replace(settings, db_path=args.db, mode=args.mode, concurrency=args.concurrency)
    if args.budget is not None:
        settings = replace(settings, budget_usd=args.budget)
    service = build_service(settings)
    try:
        contacts = read_contacts(Path(args.input))
        batch_id, created = service.submit(
            contacts, args.strategy, idempotency_key=args.idempotency_key
        )
        await service.worker_pool().run_until_drained()
        rows = service.queue.results(batch_id)
        done = [r["result"] for r in rows if r["result"]]
        verified = sum(1 for r in done if r["verification"] == "valid")
        spend = sum(float(r["cost"]) for r in done)
        return {
            "batch_id": batch_id,
            "created": created,
            "contacts": len(rows),
            "verified": verified,
            "spend_usd": round(spend, 4),
            "cost_per_verified": round(spend / verified, 4) if verified else None,
            "ledger_spent_usd": service.ledger.spent_usd,
            "mode": settings.mode,
            "rows": rows,
        }
    finally:
        await service.aclose()


def write_results(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "contact_id",
                "status",
                "email",
                "verification",
                "provider",
                "stop_reason",
                "cost_usd",
                "providers_called",
            ]
        )
        for r in rows:
            res = r["result"] or {}
            w.writerow(
                [
                    r["job_key"],
                    r["status"],
                    res.get("email"),
                    res.get("verification"),
                    res.get("provider"),
                    res.get("stop_reason"),
                    res.get("cost"),
                    "|".join(c["provider"] for c in res.get("calls", [])),
                ]
            )


def _cmd_run(args: argparse.Namespace) -> int:
    summary = asyncio.run(_run(args))
    write_results(Path(args.output), summary.pop("rows"))
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from wfo.api import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wfo", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate", help="run the strategy comparison harness (simulation)")
    s.add_argument("--contacts", type=int, default=20_000)
    s.add_argument("--seeds", type=int, default=5)
    s.add_argument("--value", type=float, default=0.50, help="USD value of a verified email")
    s.add_argument("--budget", type=float, default=None)
    s.add_argument("--workers", type=int, default=2)
    s.add_argument("--no-faults", action="store_true", help="skip the outage scenario")
    s.add_argument("--out", default="docs/results")
    s.add_argument(
        "--world", default=None, help="calibration JSON (e.g. configs/real_calibrated.json)"
    )
    s.set_defaults(func=_cmd_simulate)

    sw = sub.add_parser("sensitivity", help="hit-rate / heterogeneity sweep on a calibrated world")
    sw.add_argument("--world", default="configs/real_calibrated.json")
    sw.add_argument("--contacts", type=int, default=10_000)
    sw.add_argument("--seeds", type=int, default=3)
    sw.add_argument("--value", type=float, default=0.50)
    sw.add_argument("--workers", type=int, default=2)
    sw.add_argument("--scale", type=float, default=0.30, help="hit-rate perturbation, e.g. 0.3")
    sw.add_argument("--only", default=None, help="comma-separated variant keys")
    sw.add_argument("--out", default="docs/results_real_calibrated")
    sw.set_defaults(func=_cmd_sensitivity)

    r = sub.add_parser("report", help="re-render charts + markdown from results.json")
    r.add_argument("--results", default="docs/results/results.json")
    r.add_argument("--out", default=None)
    r.set_defaults(func=_cmd_report)

    g = sub.add_parser("generate", help="write synthetic contacts CSV")
    g.add_argument("--n", type=int, default=1000)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--out", default="contacts.csv")
    g.set_defaults(func=_cmd_generate)

    run = sub.add_parser("run", help="enrich a CSV through the durable queue + worker pool")
    run.add_argument("--input", required=True)
    run.add_argument("--output", default="enriched.csv")
    run.add_argument("--strategy", choices=STRATEGIES, default="thompson")
    run.add_argument("--mode", choices=("simulated", "live"), default="simulated")
    run.add_argument("--db", default=".wfo/wfo.db")
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--budget", type=float, default=None)
    run.add_argument("--idempotency-key", default=None)
    run.set_defaults(func=_cmd_run)

    sv = sub.add_parser("serve", help="start the FastAPI server with workers")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=_cmd_serve)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
