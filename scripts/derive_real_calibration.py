"""Derive configs/real_calibrated.json from public prices and published benchmark data.

Usage:  .venv/bin/python scripts/derive_real_calibration.py [--cache DIR] [--out PATH]

* Prices: transcribed from each vendor's public pricing page (see SOURCES; the
  date accessed is recorded). They are list prices, not negotiated contract prices.
* Hit / accuracy rates: computed here from two per-contact benchmark datasets
  published under CC BY 4.0 (downloaded at run time, not vendored into the repo):
  - Anymail Finder "Email Finder Benchmark 2026" (5,000 B2B contacts, 14 tools).
  - OrbiSearch "Email Finder Benchmark, August 2026" (288 contacts, 3 regions).
  Both are published by vendors that took part in their own test.
* Everything without a public source is written as an explicit ASSUMPTION.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wfo.sim.calibration import (
    ASSUMPTION,
    ProviderMoments,
    from_dict,
    implied_union_valid,
    logit_mod,
    solve_discoverable,
    solve_provider,
)
from wfo.sim.world import DEFAULT_SPECS, GUESS_FACTOR

ACCESSED = "2026-11-20"
AMF_CSV = "https://anymailfinder.com/email-finder-benchmark/email-finder-benchmark-data.csv"
ORBI_CSV = "https://orbisearch.com/research/email-finder-benchmark-2026.csv"
PROVIDERS = ("apollo", "dropcontact", "hunter", "prospeo")

SOURCES: dict[str, dict[str, str]] = {
    "S_hunter_pricing": {
        "url": "https://hunter.io/pricing",
        "accessed": ACCESSED,
        "publisher": "Hunter (vendor)",
        "quote": "Starter ... $49 /month billed monthly ($34 billed yearly, $408/yr) ... 2,000 "
        "credits per month (24,000 per year) ... Credit cost: Email found · 1 credit",
    },
    "S_hunter_api": {
        "url": "https://hunter.io/api-documentation/v2",
        "accessed": ACCESSED,
        "publisher": "Hunter (vendor)",
        "quote": "Email Finder: A verification is automatically performed on each email found. "
        "If no email can be found, no credit is charged.",
    },
    "S_apollo_pricing": {
        "url": "https://www.apollo.io/pricing",
        "accessed": ACCESSED,
        "publisher": "Apollo.io (vendor)",
        "quote": "Basic $49 Per seat per month, billed annually; 30,000 credits per seat per "
        "year, granted upfront (no monthly-billing price is shown on the page)",
    },
    "S_apollo_credits": {
        "url": "https://www.apollo.io/pricing/about-credits",
        "accessed": ACCESSED,
        "publisher": "Apollo.io (vendor)",
        "quote": "You're only charged credits for email addresses that Apollo can verify. "
        "(Knowledge base: one credit per verified email for a net-new contact.)",
    },
    "S_prospeo_pricing": {
        "url": "https://prospeo.io/pricing",
        "accessed": ACCESSED,
        "publisher": "Prospeo (vendor)",
        "quote": "Starter: Monthly Price $49, Yearly Price $441 ($37/mo); Monthly Credits 2000, "
        "Yearly Credits 24000. One credit gives you one verified business email.",
    },
    "S_prospeo_api": {
        "url": "https://prospeo.io/api-docs/enrich-person",
        "accessed": ACCESSED,
        "publisher": "Prospeo (vendor)",
        "quote": "Enriching a person record costs 1 credit per email found ... You won't be "
        "charged if no results are found.",
    },
    "S_dropcontact_pricing": {
        "url": "https://www.dropcontact.com/pricing",
        "accessed": ACCESSED,
        "publisher": "Dropcontact (vendor)",
        "quote": "Starter 500 credits ... 79 EUR /monthly ... Pay on success ... Native and "
        "exclusive email validation (Monthly / Yearly -20%)",
    },
    "S_ecb_fx": {
        "url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        "accessed": ACCESSED,
        "publisher": "European Central Bank",
        "quote": "Euro foreign exchange reference rate 2026-11-19: USD 1.0992",
    },
    "S_amf": {
        "url": "https://anymailfinder.com/email-finder-benchmark",
        "accessed": ACCESSED,
        "publisher": "Anymail Finder (vendor; a participant in its own benchmark, disclosed)",
        "quote": "14 tools tested on the same 5,000 B2B contacts (US, UK, France, Germany), every "
        "returned email verified by BounceBan, ZeroBounce and MillionVerifier; no live send. "
        f"Per-contact CSV (CC BY 4.0): {AMF_CSV}",
    },
    "S_orbi": {
        "url": "https://orbisearch.com/research/email-finder-benchmark",
        "accessed": ACCESSED,
        "publisher": "OrbiSearch (vendor; a participant in its own benchmark)",
        "quote": "7 email finders tested on the same 288 B2B contacts (NA 95 / EMEA 99 / APAC "
        f"94), every returned address re-verified live. Per-contact CSV (CC BY 4.0): {ORBI_CSV}",
    },
    "S_dropcontact_bench": {
        "url": "https://www.dropcontact.com/email-finder-benchmark",
        "accessed": ACCESSED,
        "publisher": "Dropcontact (vendor; funded and designed its own benchmark)",
        "quote": "15 tools, 20,000 contacts, live send counting hard bounces. Base = 17,132 "
        "contacts with >=1 email found by any tool. Dropcontact: 9,584 raw (55.9%), 9,402 "
        "usable (54.9%). Hunter: 6,665 raw (38.9%), 744 hard bounce (11.2%), 5,570 usable "
        "(32.5%). Apollo and Prospeo not tested. Used only to motivate the sensitivity sweep.",
    },
}


def _leaf(value: Any, source: str, note: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "source": source}
    if note:
        out["note"] = note
    return out


def _assume(value: Any, note: str) -> dict[str, Any]:
    return _leaf(value, ASSUMPTION, note)


def _fetch(url: str, cache: Path) -> list[dict[str, str]]:
    path = cache / url.rsplit("/", 1)[-1]
    if not path.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "wfo-calibration/0.1"})
        with urllib.request.urlopen(req, timeout=60) as r:
            path.write_bytes(r.read())
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _rate(rows: Iterable[dict[str, str]], pred: Any) -> tuple[float, int, int]:
    rs = list(rows)
    k = sum(1 for r in rs if pred(r))
    return k / len(rs), k, len(rs)


def _amf_country(domain: str) -> str:
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    return {"uk": "UK", "de": "DE", "fr": "FR"}.get(tld, "US")


def derive(cache: Path) -> dict[str, Any]:
    amf = _fetch(AMF_CSV, cache)
    orbi = _fetch(ORBI_CSV, cache)
    tools = [k[: -len("_result")] for k in amf[0] if k.endswith("_result")]

    observed: dict[str, Any] = {"amf_n_contacts": _leaf(len(amf), "S_amf")}
    moments: dict[str, ProviderMoments] = {}
    for p in PROVIDERS:
        col = f"{p}_result"
        f, kf, n = _rate(amf, lambda r, c=col: r[c] != "not_found")
        v, kv, _ = _rate(amf, lambda r, c=col: r[c] == "valid")
        moments[p] = ProviderMoments(f, v)
        observed[p] = {
            "found_rate": _leaf(f, "S_amf", f"{kf}/{n} contacts with any email returned"),
            "valid_rate": _leaf(v, "S_amf", f"{kv}/{n} contacts with a verified-valid email"),
        }
    u4, k4, n = _rate(amf, lambda r: any(r[f"{p}_result"] == "valid" for p in PROVIDERS))
    u14, k14, _ = _rate(amf, lambda r: any(r[f"{t}_result"] == "valid" for t in tools))
    observed["union4_valid_rate"] = _leaf(u4, "S_amf", f"{k4}/{n}: >=1 of the 4 providers valid")
    observed["union14_valid_rate"] = _leaf(u14, "S_amf", f"{k14}/{n}: >=1 of 14 tools valid")

    d = solve_discoverable(moments, u4)
    k = d + GUESS_FACTOR * (1 - d)
    sol = {p: solve_provider(m, d) for p, m in moments.items()}

    # Country effects. AMF: ccTLD .uk/.de/.fr -> UK/DE/FR, everything else -> US proxy.
    by_country: dict[str, list[dict[str, str]]] = {"US": [], "UK": [], "DE": [], "FR": []}
    for r in amf:
        by_country[_amf_country(r["domain"])].append(r)
    orbi_all = orbi
    orbi_apac = [r for r in orbi if r["region"] == "APAC"]
    observed["country_found_rate"] = {}
    providers: dict[str, Any] = {}
    defaults = {s.name: s for s in DEFAULT_SPECS}
    for p in PROVIDERS:
        h, a = sol[p]
        cmods: dict[str, Any] = {}
        obs_c: dict[str, Any] = {}
        for c, rows in by_country.items():
            fc, kc, nc = _rate(rows, lambda r, col=f"{p}_result": r[col] != "not_found")
            obs_c[c] = _leaf(fc, "S_amf", f"{kc}/{nc}")
            note = f"logit(h_{c}) - logit(h); h_{c} from found rate {kc}/{nc}" + (
                "; US = all non-.uk/.de/.fr domains (proxy)" if c == "US" else ""
            )
            cmods[c] = _leaf(round(logit_mod(min(fc / k, 0.99), h), 4), "S_amf", note)
        if p in orbi[0]:
            fa, ka, na = _rate(orbi_apac, lambda r, col=p: r[col] != "none")
            fo, ko, no = _rate(orbi_all, lambda r, col=p: r[col] != "none")
            obs_c["IN"] = _leaf(fa, "S_orbi", f"APAC {ka}/{na} (overall {ko}/{no})")
            cmods["IN"] = _leaf(
                round(logit_mod(fa / k, fo / k), 4),
                "S_orbi",
                f"APAC vs all-region found rate ({ka}/{na} vs {ko}/{no}); APAC used as a "
                "proxy for IN",
            )
        else:
            cmods["IN"] = _assume(0.0, f"{p} is not in the OrbiSearch test; no IN data")
        observed["country_found_rate"][p] = obs_c
        spec = defaults[p]
        price, charge = PRICES[p]
        providers[p] = {
            "price_usd": price,
            "price_usd_annual_billing": ANNUAL_PRICES[p],
            "charge_on_miss": charge,
            "base_hit": _leaf(
                round(h, 4),
                "S_amf",
                f"solved: found_rate / (d + {GUESS_FACTOR}*(1-d)) with d={d:.4f}",
            ),
            "accuracy": _leaf(
                round(a, 4), "S_amf", "solved so the model reproduces the benchmark valid rate"
            ),
            "country_mod": cmods,
            "size_mod": {
                s: _assume(0.0, "no public size-level benchmark found") for s in spec.size_mod
            },
            "seniority_mod": {
                s: _assume(0.0, "no public seniority-level benchmark found")
                for s in spec.seniority_mod
            },
            "latency_median_s": _assume(spec.latency_median_s, "illustrative repo default"),
            "latency_sigma": _assume(spec.latency_sigma, "illustrative repo default"),
            "error_rate": _assume(spec.error_rate, "illustrative repo default"),
            "rate_limit_rate": _assume(spec.rate_limit_rate, "illustrative repo default"),
        }

    implied = implied_union_valid(
        {p: s[0] for p, s in sol.items()}, {p: s[1] for p, s in sol.items()}, d
    )
    world = {
        "discoverable": _leaf(
            round(d, 4),
            "S_amf",
            "solved by bisection so the model's P(>=1 of 4 providers valid) equals the "
            f"benchmark's {u4:.4f}",
        ),
        "catch_all": _assume(
            0.0,
            "Benchmark 'valid' labels already adjudicate catch-all domains (BounceBan), so "
            "unresolvable catch-alls are netted into the valid rates instead of modelled apart",
        ),
        "segment_noise": _assume(
            0.35,
            "per-seed logit noise per segment x provider (repo default); stands in for "
            "size/seniority heterogeneity that has no public data; swept 0..0.7",
        ),
        "verify_cost_usd": _assume(
            0.0,
            "All four vendors bundle verification into the find credit (S_hunter_api, "
            "S_apollo_credits, S_prospeo_pricing, S_dropcontact_pricing); an external verifier "
            "would add cost",
        ),
        "country_weights": {
            c: _assume(w, "repo default mix; no public segment mix used")
            for c, w in zip(
                ("US", "UK", "DE", "FR", "IN"), (0.40, 0.15, 0.15, 0.10, 0.20), strict=True
            )
        },
        "size_weights": {
            s: _assume(w, "repo default mix")
            for s, w in zip(("1-50", "51-500", "501+"), (0.45, 0.35, 0.20), strict=True)
        },
        "seniority_weights": {
            s: _assume(w, "repo default mix")
            for s, w in zip(("ic", "manager", "exec"), (0.55, 0.30, 0.15), strict=True)
        },
    }
    raw: dict[str, Any] = {
        "label": "REAL-CALIBRATED SIMULATION: real list prices + published benchmark hit rates; "
        "outcomes are still simulated",
        "as_of": ACCESSED,
        "sources": SOURCES,
        "providers": providers,
        "world": world,
        "observed": observed,
        "fit_check": {
            "union4_valid_model": _leaf(
                round(implied, 4), "S_amf", "model-implied, should equal observed union4"
            ),
            "union14_valid_observed": observed["union14_valid_rate"],
        },
    }
    from_dict(raw)  # validates
    return raw


_FX = 1.0992
PRICES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "apollo": (
        _leaf(
            round(49 * 12 / 30_000, 6),
            "S_apollo_pricing",
            "Basic: $588/yr / 30,000 credits; "
            "1 credit per verified email (annual billing is the only price shown)",
        ),
        _leaf(False, "S_apollo_credits", "charged only for emails Apollo can verify"),
    ),
    "hunter": (
        _leaf(
            round(49 / 2_000, 6),
            "S_hunter_pricing",
            "Starter: $49/month / 2,000 credits; 1 credit per email found",
        ),
        _leaf(False, "S_hunter_api", "no credit charged if no email is found"),
    ),
    "prospeo": (
        _leaf(
            round(49 / 2_000, 6),
            "S_prospeo_pricing",
            "Starter: $49/month / 2,000 credits; 1 credit per email found",
        ),
        _leaf(False, "S_prospeo_api", "not charged if no results are found"),
    ),
    "dropcontact": (
        _leaf(
            round(79 / 500 * _FX, 6),
            "S_dropcontact_pricing",
            "Starter: EUR 79/month / 500 credits x EUR/USD 1.0992 (S_ecb_fx)",
        ),
        _leaf(False, "S_dropcontact_pricing", "'Pay on success'"),
    ),
}
ANNUAL_PRICES: dict[str, dict[str, Any]] = {
    "apollo": _leaf(round(49 * 12 / 30_000, 6), "S_apollo_pricing", "$588/yr / 30,000 credits"),
    "hunter": _leaf(round(408 / 24_000, 6), "S_hunter_pricing", "$408/yr / 24,000 credits"),
    "prospeo": _leaf(round(441 / 24_000, 6), "S_prospeo_pricing", "$441/yr / 24,000 credits"),
    "dropcontact": _leaf(
        round(79 * 0.8 / 500 * _FX, 6),
        "S_dropcontact_pricing",
        "EUR 79 x (1 - 20% yearly discount) / 500 credits x 1.0992 (S_ecb_fx)",
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", default=".wfo/calibration_cache")
    ap.add_argument("--out", default="configs/real_calibrated.json")
    args = ap.parse_args()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    raw = derive(cache)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, indent=1) + "\n")
    w = raw["world"]["discoverable"]["value"]
    print(f"wrote {out}: discoverable={w}")
    for p, spec in raw["providers"].items():
        print(
            f"  {p:12s} price=${spec['price_usd']['value']:.4f} hit={spec['base_hit']['value']:.3f}"
            f" acc={spec['accuracy']['value']:.3f}"
            f" country={ {c: m['value'] for c, m in spec['country_mod'].items()} }"
        )
    print("  fit:", {k: v["value"] for k, v in raw["fit_check"].items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
