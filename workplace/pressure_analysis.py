"""Pure schedule and exploratory statistics for the dyadic pressure protocol.

This module consumes explicit reviewed records, never raw transcript semantics.
No third-party dependency and no model/network calls. See PRESSURE-ANALYSIS.md.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .pressure_review import (
    ACTORS,
    CATALOGUE_VERSION,
    CATEGORIES,
    DOSES,
    SEED,
    _require,
    calibration_gate,
    checkpoint_decision,
    review_key,
    select_audit_sample,
    validate_annotations,
)


def build_schedule(seed=SEED, continuation_horizon=900):
    """Frozen full 80 assigned slots, randomized complete four-dose blocks."""
    _require(seed == SEED, "approved study seed is 20261012")
    _require(continuation_horizon in {900, 1200}, "continuation H must be 900 or 1200")
    rng = random.Random(seed)
    slots = []
    for block in range(1, 21):
        doses = list(DOSES)
        rng.shuffle(doses)
        phase = "discovery" if block <= 4 else "continuation"
        for dose in doses:
            ordinal = len(slots) + 1
            slots.append(
                {
                    "slot_id": f"pressure-{ordinal:03d}",
                    "ordinal": ordinal,
                    "block": block,
                    "phase": phase,
                    "dose": dose,
                    "a_deadline_seconds": dose,
                    "horizon_seconds": 900
                    if phase == "discovery"
                    else continuation_horizon,
                    "seed": seed,
                }
            )
    return slots


def wilson_interval(positive, assessable, z=1.959963984540054):
    """95% Wilson interval, conditional on the explicitly assessable population."""
    _require(0 <= positive <= assessable, "invalid binomial counts")
    if assessable == 0:
        return None
    p = positive / assessable
    denominator = 1 + z * z / assessable
    center = (p + z * z / (2 * assessable)) / denominator
    radius = (
        z
        * math.sqrt(p * (1 - p) / assessable + z * z / (4 * assessable * assessable))
        / denominator
    )
    return [max(0.0, center - radius), min(1.0, center + radius)]


def _population(attempts):
    return {
        "assigned": len(attempts),
        "exposed": sum(a["exposed"] for a in attempts),
        "complete": sum(a["status"] == "complete" for a in attempts),
        "failed": sum(a["status"] == "failed" for a in attempts),
        "limited": sum(a["status"] == "limited" for a in attempts),
        "unstarted": sum(a["status"] == "unstarted" for a in attempts),
        "window_complete": sum(a["window_complete"] for a in attempts),
        "evidence_complete": sum(a["evidence_complete"] for a in attempts),
    }


def _prevalence(judgments):
    n = len(judgments)
    positive = judgments.count("present")
    negative = judgments.count("absent")
    unknown = n - positive - negative
    assessable = positive + negative
    return {
        "assigned": n,
        "positive": positive,
        "negative": negative,
        "unknown": unknown,
        "assessable": assessable,
        "frequency": positive / assessable if assessable else None,
        "wilson_95_ci_assessable": wilson_interval(positive, assessable),
        "unknown_bounds_assigned": [positive / n, (positive + unknown) / n]
        if n
        else None,
    }


def _judgment(attempt, actor, category, reviews):
    actors = ACTORS if actor == "dyad" else (actor,)
    rows = [reviews.get((attempt["attempt_id"], who, category)) for who in actors]
    judgments = [r["judgment"] if r else "unknown" for r in rows]
    judgment = (
        "present"
        if "present" in judgments
        else "absent"
        if all(x == "absent" for x in judgments)
        else "unknown"
    )
    coverage = (
        all(r and r["coverage_complete"] for r in rows)
        and attempt["window_complete"]
        and attempt["evidence_complete"]
    )
    # Unknown adjudication makes event counts uncertain even with complete logs.
    counts_complete = bool(coverage and all(x != "unknown" for x in judgments))
    return judgment, counts_complete


def _event_timing(event, deadline):
    start, end = event["start_seconds"], event["end_seconds"]
    if start is None:
        return "unknown"
    if end < deadline:
        return "pre"
    if start >= deadline:
        return "post"
    return "straddling"


def _category_summary(attempts, actor, category, reviews, events):
    judgments, exposed_judgments, rows = [], [], []
    for attempt in attempts:
        identity = attempt["attempt_id"]
        judgment, counts_complete = _judgment(attempt, actor, category, reviews)
        judgments.append(judgment)
        if attempt["exposed"]:
            exposed_judgments.append(judgment)
        selected = [
            e
            for e in events.get(identity, [])
            if e["category"] == category and (actor == "dyad" or e["actor"] == actor)
        ]
        # Unknown category judgments may carry disputed event records. Preserve
        # them as candidates, never silently promote them to confirmed behavior.
        confirmed = [
            e for e in selected if reviews[review_key(e)]["judgment"] == "present"
        ]
        timed = [e for e in confirmed if e["start_seconds"] is not None]
        observed_interval = (
            [
                min(e["start_seconds"] for e in timed),
                min(e["end_seconds"] for e in timed),
            ]
            if timed
            else None
        )
        true_first_interval = None
        if counts_complete and confirmed:
            true_first_interval = [
                min(
                    e["start_seconds"] if e["start_seconds"] is not None else 0
                    for e in confirmed
                ),
                min(e["end_seconds"] for e in timed) if timed else attempt["horizon"],
            ]
        rows.append(
            {
                "attempt_id": identity,
                "judgment": judgment,
                "event_count": len(confirmed) if counts_complete else None,
                "event_count_lower_bound": len(confirmed),
                "candidate_unresolved_count": len(selected) - len(confirmed),
                "count_complete": counts_complete,
                "first_observed_event_interval_seconds": observed_interval,
                "first_event_interval_seconds": true_first_interval,
                "right_censored_no_event_seconds": attempt["horizon"]
                if judgment == "absent"
                else None,
                "timing_relative_a_deadline": dict(
                    Counter(_event_timing(e, attempt["dose"]) for e in confirmed)
                ),
                "attempted_events": sum(e["attempt"] == "yes" for e in confirmed),
                "unknown_attempt_events": sum(
                    e["attempt"] == "unknown" for e in confirmed
                ),
                "effects": dict(Counter(e["effect"] for e in confirmed)),
                "event_ids": [e["event_id"] for e in confirmed],
            }
        )
    prevalence = _prevalence(judgments)
    population = _population(attempts)
    population["assessable"] = prevalence["assessable"]
    complete_counts = [r["event_count"] for r in rows if r["event_count"] is not None]
    return {
        "actor": actor,
        "category": category,
        "population": population,
        "prevalence": prevalence,
        "exposed_prevalence": _prevalence(exposed_judgments),
        "event_count_distribution_complete_reviews": dict(
            sorted(Counter(complete_counts).items())
        ),
        "event_count_complete_n": len(complete_counts),
        "event_count_incomplete_n": len(rows) - len(complete_counts),
        "mean_event_count_complete_reviews": statistics.mean(complete_counts)
        if complete_counts
        else None,
        "median_event_count_complete_reviews": statistics.median(complete_counts)
        if complete_counts
        else None,
        "confirmed_event_count_lower_bound": sum(
            r["event_count_lower_bound"] for r in rows
        ),
        "attempted_event_count": sum(r["attempted_events"] for r in rows),
        "successful_event_count": sum(r["effects"].get("successful", 0) for r in rows),
        "unresolved_effect_count": sum(r["effects"].get("unresolved", 0) for r in rows),
        "episodes": rows,
    }


def _stratum(attempts, phase, horizon, protocol_id, reviews, events):
    doses = []
    for dose in DOSES:
        population = [a for a in attempts if a["dose"] == dose]
        estimates = [
            _category_summary(population, actor, category, reviews, events)
            for actor in (*ACTORS, "dyad")
            for category in CATEGORIES
        ]
        doses.append(
            {
                "dose": dose,
                "population": _population(population),
                "estimates": estimates,
            }
        )
    contrasts = []
    for actor in (*ACTORS, "dyad"):
        for category in CATEGORIES:
            estimates = [
                next(
                    e
                    for e in d["estimates"]
                    if e["actor"] == actor and e["category"] == category
                )["prevalence"]
                for d in doses
            ]
            frequencies = [e["frequency"] for e in estimates]
            bounds = [e["unknown_bounds_assigned"] for e in estimates]
            contrast = (
                (frequencies[1] + frequencies[2] - frequencies[0] - frequencies[3]) / 2
                if all(x is not None for x in frequencies)
                else None
            )
            contrast_bounds = (
                [
                    (bounds[1][0] + bounds[2][0] - bounds[0][1] - bounds[3][1]) / 2,
                    (bounds[1][1] + bounds[2][1] - bounds[0][0] - bounds[3][0]) / 2,
                ]
                if all(x is not None for x in bounds)
                else None
            )
            contrasts.append(
                {
                    "actor": actor,
                    "category": category,
                    "middle_minus_extremes_assessable": contrast,
                    "middle_minus_extremes_unknown_bounds_assigned": contrast_bounds,
                    "definition": "mean(120,300) minus mean(30,600); equal dose weights; descriptive",
                }
            )
    return {
        "phase": phase,
        "horizon": horizon,
        "protocol_id": protocol_id,
        "population": _population(attempts),
        "doses": doses,
        "middle_vs_extremes": contrasts,
    }


def summarize(attempts, reviews, events, *, pool_same_protocol=False):
    """Return four-dose estimates separately by phase, H, and protocol identity.

    Same-protocol pooling requires explicit caller opt-in, identical H and
    protocol_id. Different-H phases never pool, including early windows.
    Missing actor/category reviews are unknown for their assigned dyad.
    """
    events = validate_annotations(attempts, reviews, events)
    review_index = {review_key(r): r for r in reviews}
    by_attempt = defaultdict(list)
    for event in events:
        by_attempt[event["attempt_id"]].append(event)
    groups = defaultdict(list)
    for attempt in attempts:
        groups[(attempt["phase"], attempt["horizon"], attempt["protocol_id"])].append(
            attempt
        )
    strata = [
        _stratum(group, phase, horizon, protocol_id, review_index, by_attempt)
        for (phase, horizon, protocol_id), group in sorted(groups.items())
    ]
    pooled = []
    if pool_same_protocol:
        compatible = defaultdict(list)
        for attempt in attempts:
            compatible[(attempt["horizon"], attempt["protocol_id"])].append(attempt)
        for (horizon, protocol_id), group in sorted(compatible.items()):
            if len({a["phase"] for a in group}) > 1:
                pooled.append(
                    _stratum(
                        group,
                        "pooled_same_protocol",
                        horizon,
                        protocol_id,
                        review_index,
                        by_attempt,
                    )
                )
    return {
        "schema_version": "pressure-analysis-v1",
        "catalogue_version": CATALOGUE_VERSION,
        "study_type": "exploratory multi-readout",
        "replication_unit": "dyad",
        "population": _population(attempts),
        "strata": strata,
        "pooled_strata": pooled,
        "claim_limits": [
            "Wilson intervals condition on assessability; missing outcomes are bounded separately.",
            "Partial evidence may establish presence but never absence or complete event counts.",
            "No monotonic fit, causal behavioral mechanism, intent inference, or universal-law claim.",
            "Different-H phases remain separate; no automatically truncated pooled estimate.",
            "Emergent categories require a separately versioned catalogue and consistent retrospective review.",
        ],
        "reviewed_cells": len(reviews),
        "expected_review_cells": len(attempts) * len(ACTORS) * len(CATEGORIES),
        "missing_review_cells": len(attempts) * len(ACTORS) * len(CATEGORIES)
        - len(reviews),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("schedule", "calibration", "checkpoint", "audit", "summarize"),
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--continuation-horizon", type=int, default=900)
    parser.add_argument("--pool-same-protocol", action="store_true")
    args = parser.parse_args(argv)
    data = json.loads(args.input.read_text()) if args.input else None
    if args.operation == "schedule":
        result = build_schedule(continuation_horizon=args.continuation_horizon)
    else:
        if data is None:
            parser.error("--input required")
        if args.operation == "calibration":
            result = calibration_gate(data)
        elif args.operation == "checkpoint":
            result = checkpoint_decision(data)
        elif args.operation == "audit":
            result = select_audit_sample(data["attempts"], data["reviews"])
        else:
            result = summarize(
                data["attempts"],
                data["reviews"],
                data["events"],
                pool_same_protocol=args.pool_same_protocol,
            )
    output = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
