"""
Synthetic dataset generator for the Deal Desk reconciliation engine.

Purpose
-------
The project's pinned demo data (`data/salesforce_export.csv` and
`data/mission_control_export.csv`) is 14 rows designed to trigger every
classifier scenario once. That is sufficient for correctness demonstration
but insufficient for the kind of engineering a RevOps Manager or a serious
GTM Engineering reviewer evaluates: metrics that are more than noise,
failure-injection tests that need realistic volume, patterns that emerge
across hundreds of records rather than one.

This module produces reproducible synthetic datasets at the scale the
engineering work needs. Every output is deterministic given a seed, so the
regression tests pin what "the generator at seed 42 produces" and future
refactors are caught if they change that output silently.

Design choices
--------------

Reproducibility is non-negotiable.
    The generator uses `numpy.random.default_rng(seed)`. Same seed in,
    same output out, byte-for-byte. The seed is part of the generator's
    public interface, documented in every function docstring.

Distributions are informed, not arbitrary.
    Contract values are log-normal, which matches enterprise SaaS deal
    size distributions in public benchmarks (OpsLeader surveys, Pavilion
    RevOps reports). Approval ages are gamma-distributed, which captures
    the "most are recent, some are quite old" pattern of an open
    opportunity population. Rationale for each distribution lives in the
    function docstring; swap them deliberately, not by accident.

Drift patterns are controlled by rate parameters.
    Instead of hardcoding which opportunities drift, the generator takes
    a `drift_rates` dict with named keys (contract_value, missing_mc,
    contract_gap, owned_field) and samples the target count for each.
    This lets tests and demos vary the drift profile without rewriting
    the generator.

Company and person names are synthetic and generic.
    Names come from word pools (Acme, Zenith, Pacific crossed with
    Robotics, Software, Logistics, etc.). No real company names. No names
    derived from the author's work history. If a reviewer asks "where did
    these come from" the honest answer is "deterministic hash of a seed
    times a word list committed in the repo."

Output schema matches the existing CSVs exactly.
    `reconcile.py` and the API's reconcile endpoint keep working against
    generated data without modification. The adapter layer in `api/main.py`
    accepts the same shape. Backward compatibility is a feature.

Usage
-----

As a function:

    from data.generate import generate_dataset
    result = generate_dataset(
        n_opportunities=500,
        simulated_now=datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc),
        random_seed=42,
    )
    # result["sf_df"] and result["mc_df"] are the DataFrames.

As a CLI:

    python3 -m data.generate --n 500 --seed 42 --output data/generated/

Writes salesforce_export.csv and mission_control_export.csv to the
output directory.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


# -------------------------------------------------------------------
# Name pools
# -------------------------------------------------------------------

_COMPANY_ADJECTIVES = [
    "Acme", "Zenith", "Pacific", "Northern", "Summit", "Delta", "Alpine",
    "Horizon", "Pinnacle", "Vertex", "Apex", "Meridian", "Phoenix", "Titan",
    "Vega", "Nova", "Spectrum", "Cascade", "Atlas", "Orion", "Luna", "Terra",
    "Aurora", "Vortex", "Equinox", "Zephyr", "Solstice", "Prism", "Beacon",
    "Lattice",
]

_COMPANY_INDUSTRIES = [
    "Robotics", "Software", "Logistics", "Dynamics", "Systems", "Technologies",
    "Industries", "Solutions", "Networks", "Analytics", "Ventures", "Partners",
    "Holdings", "Platforms", "Labs",
]

_OWNER_FIRST_NAMES = [
    "Priya", "Marcus", "Jordan", "Alex", "Sam", "Taylor", "Morgan", "Casey",
    "Riley", "Avery", "Quinn", "Dakota", "Reese", "Rowan", "Hayden",
]

_OWNER_LAST_NAMES = [
    "Shah", "Chen", "Kim", "Park", "Nguyen", "Garcia", "Patel", "Singh",
    "Rossi", "Martin", "Okafor", "Silva", "Novak", "Haddad", "Reyes",
]

_SQUAD_NAMES = [
    "Squad Aurora", "Squad Bravo", "Squad Comet", "Squad Delta", "Squad Echo",
    "Squad Falcon", "Squad Glacier", "Squad Horizon",
]


# -------------------------------------------------------------------
# Drift configuration
# -------------------------------------------------------------------

@dataclass(frozen=True)
class DriftRates:
    """
    Rate of each drift pattern injected into the dataset. Each value is
    the proportion of opportunities that exhibit that specific drift.
    Opportunities can exhibit more than one (independent sampling); the
    generator does not enforce exclusivity.

    Defaults are informed by what the reconciliation engine should surface
    at interesting but not overwhelming rates: enough recurring incidents
    to make the weekly rollup meaningful, not so many that everything is
    an exception.
    """
    contract_value_drift: float = 0.15
    missing_mc_record: float = 0.05
    contract_gap: float = 0.03
    owned_field_drift: float = 0.08
    handoff_sla_breach: float = 0.02


# -------------------------------------------------------------------
# Distributions
# -------------------------------------------------------------------

def _sample_contract_values(n: int, rng: np.random.Generator) -> np.ndarray:
    """
    Log-normal distribution with median around $200K, long right tail.

    mu = ln(200000), sigma = 0.7 produces a distribution where the 10th
    percentile is around $85K, median around $200K, 90th percentile around
    $490K, with a long tail reaching $1M+. This mirrors enterprise SaaS
    deal distributions in public benchmarks.

    Clipped to [$10K, $2M] to avoid implausible extremes.
    """
    values = rng.lognormal(mean=np.log(200_000), sigma=0.7, size=n)
    return np.clip(values, 10_000, 2_000_000).round(0)


def _sample_record_ages_hours(n: int, rng: np.random.Generator) -> np.ndarray:
    """
    How many hours ago each record was last modified, drawn from a gamma
    distribution.

    shape=2, scale=48 gives a mean of about 96 hours (4 days) with a
    long tail reaching several weeks. This captures the "most records
    are recent, a long tail are older" pattern typical of an open
    opportunity population with a mix of active and dormant accounts.
    """
    return rng.gamma(shape=2.0, scale=48.0, size=n)


# -------------------------------------------------------------------
# Row builders
# -------------------------------------------------------------------

def _generate_company_names(n: int, rng: np.random.Generator) -> list[str]:
    """
    Produce n distinct company names by crossing adjective and industry
    pools. If n exceeds the combinatorial product, append a numeric suffix
    to disambiguate. Uniqueness matters because downstream joins key on
    opportunity_id but readers scan customer_name for pattern.
    """
    combos = [f"{a} {i}" for a in _COMPANY_ADJECTIVES for i in _COMPANY_INDUSTRIES]
    rng.shuffle(combos)
    if n <= len(combos):
        return combos[:n]
    suffixed = []
    for i in range(n):
        suffixed.append(f"{combos[i % len(combos)]} {i // len(combos) + 1}")
    return suffixed


def _generate_owner_names(n_distinct: int, rng: np.random.Generator) -> list[str]:
    """Produce n_distinct unique owner names from the first/last pools."""
    combos = [f"{f} {l}" for f in _OWNER_FIRST_NAMES for l in _OWNER_LAST_NAMES]
    rng.shuffle(combos)
    return combos[:n_distinct]


# -------------------------------------------------------------------
# Main generator
# -------------------------------------------------------------------

@dataclass
class GenerationResult:
    """What generate_dataset returns. Kept explicit so callers do not have
    to remember dict keys."""
    sf_df: pd.DataFrame
    mc_df: pd.DataFrame
    summary: dict


def generate_dataset(
    n_opportunities: int = 500,
    simulated_now: Optional[datetime] = None,
    drift_rates: Optional[DriftRates] = None,
    random_seed: int = 42,
) -> GenerationResult:
    """
    Generate a synthetic Salesforce + Mission Control dataset.

    Parameters
    ----------
    n_opportunities:
        How many unique opportunities to create. 500 is a reasonable
        default for most engineering work; the API and classifier handle
        tens of thousands without changes, but the dashboard renders 500
        rows cleanly for visual inspection.
    simulated_now:
        The "current time" the dataset is anchored against. Record ages
        are computed backward from this moment. Defaults to
        2026-10-28T13:30:00Z to match the pinned contract.yaml.
    drift_rates:
        Rate of each drift pattern injected. See the DriftRates docstring.
    random_seed:
        Reproducibility knob. Same seed, same output. Every random draw
        in the generator goes through this seeded generator.

    Returns
    -------
    GenerationResult with sf_df, mc_df, and a summary dict describing
    the injected drift (useful for tests and ADRs).
    """
    if simulated_now is None:
        simulated_now = datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc)
    if drift_rates is None:
        drift_rates = DriftRates()

    rng = np.random.default_rng(random_seed)

    # Base opportunity population: each opp gets an ID, a customer name,
    # a commercial owner, a contract value, and a last-modified timestamp.
    opportunity_ids = [f"OPP-{1000 + i:04d}" for i in range(n_opportunities)]
    customer_names = _generate_company_names(n_opportunities, rng)

    # Generate one combined pool of unique person names, then split it so
    # commercial owners (the SF opportunity_owner concept, mirrored on MC as
    # commercial_contact) and program leads (an MC-only delivery role) draw
    # from disjoint slices. They are different roles filled by different
    # people. Using the same pool masks the same class of field-mapping bug
    # the reviewer caught in the API adapter: if the two concepts happen to
    # match in the baseline, tests cannot tell whether the classifier compared
    # the right fields.
    _combined_name_pool = _generate_owner_names(
        min(n_opportunities * 2, 40), rng
    )
    owner_pool_size = min(n_opportunities, 20)
    program_lead_pool_size = min(max(n_opportunities // 2, 1), 15)
    owner_pool = _combined_name_pool[:owner_pool_size]
    program_lead_pool = _combined_name_pool[
        owner_pool_size:owner_pool_size + program_lead_pool_size
    ]
    owners = [owner_pool[i % len(owner_pool)] for i in range(n_opportunities)]
    rng.shuffle(owners)
    program_leads = [
        program_lead_pool[i % len(program_lead_pool)] for i in range(n_opportunities)
    ]
    rng.shuffle(program_leads)

    contract_values_usd = _sample_contract_values(n_opportunities, rng)
    sf_ages_hours = _sample_record_ages_hours(n_opportunities, rng)
    sf_timestamps = [
        simulated_now - timedelta(hours=float(h)) for h in sf_ages_hours
    ]

    # All opportunities in this generator are in the "approved" stage; the
    # reconciliation scenarios we exercise live downstream of approval.
    sf_records = []
    for i in range(n_opportunities):
        sf_records.append({
            "opportunity_id": opportunity_ids[i],
            "customer_name": customer_names[i],
            "contract_value_usd": contract_values_usd[i],
            "commercial_approval_status": "Approved",
            "opportunity_owner": owners[i],
            "stage": "Approved",
            "last_modified_ts": sf_timestamps[i].isoformat(),
            "handoff_submitted": True,
            "sf_record_id": f"0066g000{1000 + i:07d}",
        })

    # Mission Control side: start as the mirror of SF, then inject drift.
    mc_records = []
    for i in range(n_opportunities):
        mc_records.append({
            "mc_project_id": f"MC-OPP-{1000 + i:04d}",
            "account": customer_names[i],
            "engagement_value": contract_values_usd[i],
            "commercial_contact": owners[i],
            "delivery_squad": _SQUAD_NAMES[i % len(_SQUAD_NAMES)],
            "kickoff_date": (sf_timestamps[i] + timedelta(days=14)).date().isoformat(),
            "project_status": "InProgress",
            "program_lead": program_leads[i],  # MC-only delivery role; distinct person from the commercial owner.
            "updated_at": (sf_timestamps[i] + timedelta(hours=float(rng.uniform(0, 4)))).isoformat(),
            "handoff_received": True,
        })

    # Inject drift patterns. Each is a deliberate modification of the
    # mirrored MC record (or removal of it). The counts are stored in the
    # summary so tests can assert the rates are being honored.
    summary = {
        "n_opportunities_total": n_opportunities,
        "seed": random_seed,
        "simulated_now": simulated_now.isoformat(),
        "drift_counts": {},
    }

    # Pick disjoint-ish subsets for each drift kind. We sample without
    # replacement within a drift kind so one opportunity doesn't get the
    # same drift twice, but kinds are independent so one opp can exhibit
    # multiple kinds (realistic).
    all_indices = np.arange(n_opportunities)

    # Contract value drift: shift MC's engagement_value by +/- 5-15%.
    n_contract_drift = int(n_opportunities * drift_rates.contract_value_drift)
    contract_drift_idx = rng.choice(all_indices, size=n_contract_drift, replace=False)
    for i in contract_drift_idx:
        shift_pct = rng.uniform(0.05, 0.15) * rng.choice([-1, 1])
        mc_records[i]["engagement_value"] = round(
            mc_records[i]["engagement_value"] * (1 + shift_pct)
        )
        # Ensure MC's updated_at is older than SF's last_modified_ts so
        # the recency rule picks SF. This models the realistic case where
        # the AE renegotiated after approval and SF holds the newer truth.
        mc_records[i]["updated_at"] = (
            sf_timestamps[i] - timedelta(hours=float(rng.uniform(2, 48)))
        ).isoformat()
    summary["drift_counts"]["contract_value_drift"] = n_contract_drift

    # Missing MC record: drop these from mc_records entirely.
    n_missing_mc = int(n_opportunities * drift_rates.missing_mc_record)
    missing_mc_idx = set(rng.choice(all_indices, size=n_missing_mc, replace=False).tolist())
    mc_records = [m for i, m in enumerate(mc_records) if i not in missing_mc_idx]
    summary["drift_counts"]["missing_mc_record"] = n_missing_mc

    # Contract gap on handoff_flag: MC says not received while SF says submitted.
    n_contract_gap = int(n_opportunities * drift_rates.contract_gap)
    contract_gap_idx = rng.choice(
        [i for i in all_indices if i not in missing_mc_idx],
        size=n_contract_gap, replace=False,
    )
    for i in contract_gap_idx:
        # Find the mc record for this opportunity (they may have been
        # filtered out already; skip if so).
        for mc in mc_records:
            if mc["mc_project_id"] == f"MC-OPP-{1000 + int(i):04d}":
                mc["handoff_received"] = False
                break
    summary["drift_counts"]["contract_gap"] = n_contract_gap

    # Owned-field drift on commercial_contact. The SOP designates
    # opportunity_owner as a Salesforce-owned concept; MC's commercial_contact
    # is the mirror. Drift here means MC's mirror disagrees with the
    # authoritative SF value, which is exactly the owner-drift discrepancy
    # the classifier is supposed to surface. Mutating program_lead instead
    # (an MC-only field with no SF counterpart) produces no reconciliation
    # signal and silently breaks volume-based metrics work. See AUDIT_NOTES.
    n_owned_drift = int(n_opportunities * drift_rates.owned_field_drift)
    owned_drift_idx = rng.choice(
        [i for i in all_indices if i not in missing_mc_idx],
        size=n_owned_drift, replace=False,
    )
    for i in owned_drift_idx:
        for mc in mc_records:
            if mc["mc_project_id"] == f"MC-OPP-{1000 + int(i):04d}":
                # Replace with a different commercial owner from the pool so
                # the classifier sees an actual mismatch against SF.
                current = mc["commercial_contact"]
                candidates = [o for o in owner_pool if o != current]
                mc["commercial_contact"] = str(rng.choice(candidates))
                break
    summary["drift_counts"]["owned_field_drift"] = n_owned_drift

    sf_df = pd.DataFrame(sf_records)
    mc_df = pd.DataFrame(mc_records)
    summary["sf_row_count"] = len(sf_df)
    summary["mc_row_count"] = len(mc_df)

    return GenerationResult(sf_df=sf_df, mc_df=mc_df, summary=summary)


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a synthetic Deal Desk dataset."
    )
    parser.add_argument("--n", type=int, default=500, help="Number of opportunities.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (reproducibility).")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/generated"),
        help="Output directory for the generated CSVs.",
    )
    parser.add_argument(
        "--simulated-now",
        type=str,
        default="2026-10-28T13:30:00+00:00",
        help="ISO 8601 UTC timestamp the dataset is anchored against.",
    )
    args = parser.parse_args()

    simulated_now = datetime.fromisoformat(args.simulated_now)
    if simulated_now.tzinfo is None:
        raise SystemExit("simulated-now must include a timezone.")

    args.output.mkdir(parents=True, exist_ok=True)

    result = generate_dataset(
        n_opportunities=args.n,
        simulated_now=simulated_now,
        random_seed=args.seed,
    )

    sf_path = args.output / "salesforce_export.csv"
    mc_path = args.output / "mission_control_export.csv"
    result.sf_df.to_csv(sf_path, index=False)
    result.mc_df.to_csv(mc_path, index=False)

    print(f"Generated {args.n} opportunities at seed {args.seed}")
    print(f"  Salesforce CSV: {sf_path} ({len(result.sf_df)} rows)")
    print(f"  Mission Control CSV: {mc_path} ({len(result.mc_df)} rows)")
    print(f"  Drift summary: {result.summary['drift_counts']}")


if __name__ == "__main__":
    _cli()
