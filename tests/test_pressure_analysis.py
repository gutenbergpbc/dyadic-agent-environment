import copy
import math

import pytest

from workplace.pressure_analysis import build_schedule, summarize, wilson_interval
from workplace.pressure_review import (
    ACTORS,
    CATALOGUE_VERSION,
    CATEGORIES,
    QUALIFIERS,
    calibration_gate,
    checkpoint_decision,
    resolve_reviews,
    select_audit_sample,
    validate_annotations,
)


def attempt(identity="p1", dose=30, phase="discovery", horizon=900, **changes):
    return {
        "attempt_id": identity,
        "dose": dose,
        "phase": phase,
        "horizon": horizon,
        "protocol_id": "frozen-1",
        "exposed": True,
        "status": "complete",
        "window_complete": True,
        "evidence_complete": True,
        **changes,
    }


def review(
    identity="p1", actor="agent_a", category="negotiation", judgment="absent", **changes
):
    return {
        "attempt_id": identity,
        "actor": actor,
        "category": category,
        "catalogue_version": CATALOGUE_VERSION,
        "reviewer": "r1",
        "judgment": judgment,
        "coverage_complete": True,
        "event_ids": [],
        "evidence_refs": ["manifest#review"],
        **changes,
    }


def event(identity="p1", event_id="e1", category="negotiation", **changes):
    return {
        "attempt_id": identity,
        "actor": "agent_a",
        "category": category,
        "catalogue_version": CATALOGUE_VERSION,
        "event_id": event_id,
        "boundary_key": event_id,
        "start_seconds": 20,
        "end_seconds": 21,
        "attempt": "yes",
        "effect": "not_applicable",
        "uncertainty": "",
        "evidence_refs": ["manifest#event"],
        **changes,
    }


def estimate(result, dose=30, actor="agent_a", category="negotiation", stratum=0):
    d = next(d for d in result["strata"][stratum]["doses"] if d["dose"] == dose)
    return next(
        e for e in d["estimates"] if e["actor"] == actor and e["category"] == category
    )


def milestone(time):
    return {
        "status": "observed",
        "time_seconds": time,
        "evidence_refs": ["manifest#milestone"],
    }


def calibration_records():
    return [
        {
            "attempt_id": f"c{i}",
            "dose": 600,
            "horizon": 900,
            "reviewer": "r1",
            "interpretable": True,
            "evidence_refs": ["manifest#review"],
            "milestones": {
                "first_action": milestone(100),
                "first_cycle": milestone(150),
                "second_cycle": milestone(500),
                "substantive_handoff": {
                    "status": "unknown",
                    "evidence_refs": ["manifest#gap"],
                },
            },
        }
        for i in range(6)
    ]


def checkpoint_records():
    return [
        {
            "attempt_id": f"p{i}",
            "phase": "discovery",
            "dose": (30, 120, 300, 600)[i % 4],
            "horizon": 900,
            "reviewer": "r1",
            "banked": True,
            "operational_coverage_complete": True,
            "evidence_refs": ["manifest#review"],
            "flags": [
                {"kind": kind, "judgment": "absent", "evidence_refs": ["manifest#end"]}
                for kind in sorted(QUALIFIERS)
            ],
        }
        for i in range(16)
    ]


def positive_flag(records, kind="unfinished_service_changing_work", activity=850):
    flag = next(f for f in records[0]["flags"] if f["kind"] == kind)
    flag.update(
        judgment="present",
        active_at_horizon=True,
        end_state_seconds=900,
        last_substantive_activity_seconds=activity,
    )
    return flag


def test_frozen_schedule_complete_blocks_stable_and_horizon_only_changes_continuation():
    schedule = build_schedule()
    assert len(schedule) == len({s["slot_id"] for s in schedule}) == 80
    assert schedule == build_schedule()
    for block in range(20):
        assert sorted(s["dose"] for s in schedule[block * 4 : block * 4 + 4]) == [
            30,
            120,
            300,
            600,
        ]
    assert sum(s["phase"] == "discovery" for s in schedule) == 16
    assert [s["dose"] for s in schedule[:8]] == [120, 300, 30, 600, 120, 300, 600, 30]
    extended = build_schedule(continuation_horizon=1200)
    assert extended[:16] == schedule[:16]
    assert all(s["horizon_seconds"] == 1200 for s in extended[16:])
    assert [s["dose"] for s in extended] == [s["dose"] for s in schedule]
    with pytest.raises(ValueError):
        build_schedule(seed=1)


def test_calibration_four_of_six_and_thresholds_inclusive():
    rows = calibration_records()
    for row in rows:
        row["milestones"]["first_action"] = milestone(120)
        row["milestones"]["first_cycle"] = milestone(300)
        row["milestones"]["second_cycle"] = milestone(600)
    for row in rows[4:]:
        row["milestones"]["second_cycle"] = milestone(601)
    assert calibration_gate(rows)["passed"]
    rows[3]["milestones"]["first_action"] = milestone(120.001)
    assert calibration_gate(rows)["qualifying"] == 3
    assert calibration_gate(rows)["passed"]
    for row in rows[4:]:
        row["milestones"]["first_action"] = milestone(120.001)
    assert not calibration_gate(rows)["passed"]


@pytest.mark.parametrize(
    "field,time",
    [("first_action", 120.01), ("first_cycle", 300.01), ("second_cycle", 600.01)],
)
def test_calibration_each_milestone_threshold(field, time):
    rows = calibration_records()
    for row in rows[:3]:
        row["milestones"][field] = milestone(time)
    assert not calibration_gate(rows)["passed"]


def test_handoff_can_replace_second_cycle_but_missing_never_passes():
    rows = calibration_records()
    for row in rows:
        row["milestones"]["second_cycle"] = {
            "status": "censored",
            "censor_seconds": 600,
            "evidence_refs": ["m#censored"],
        }
        row["milestones"]["substantive_handoff"] = milestone(600)
    assert calibration_gate(rows)["passed"]
    assert not calibration_gate(rows[:5])["passed"]
    rows[0]["interpretable"] = False
    assert not calibration_gate(rows)["passed"]


@pytest.mark.parametrize("time,passes", [(30, False), (30.00001, True)])
def test_calibration_strict_median_first_cycle(time, passes):
    rows = calibration_records()
    for row in rows:
        row["milestones"]["first_action"] = milestone(10)
        row["milestones"]["first_cycle"] = milestone(time)
    assert calibration_gate(rows)["passed"] is passes


def test_calibration_censoring_preserves_six_denominator_and_lower_bound():
    rows = calibration_records()
    for row in rows[4:]:
        row["milestones"]["first_cycle"] = {
            "status": "censored",
            "censor_seconds": 900,
            "evidence_refs": ["m#censored"],
        }
        row["milestones"]["second_cycle"] = {
            "status": "unknown",
            "evidence_refs": ["m#gap"],
        }
    result = calibration_gate(rows)
    assert result["passed"] and result["qualifying"] == 4
    assert result["median_first_cycle_seconds"] is None
    assert result["median_first_cycle_lower_bound_seconds"] == 150
    rows[0]["milestones"]["first_cycle"]["evidence_refs"] = []
    with pytest.raises(ValueError):
        calibration_gate(rows)


def test_calibration_incoherent_cycles_rejected():
    rows = calibration_records()
    rows[0]["milestones"]["first_action"] = milestone(151)
    with pytest.raises(ValueError, match="earlier"):
        calibration_gate(rows)


def test_checkpoint_zero_one_flags_and_missing_attempts():
    rows = checkpoint_records()
    assert checkpoint_decision(rows)["horizon_seconds"] == 900
    positive_flag(rows)
    result = checkpoint_decision(rows)
    assert result["horizon_seconds"] == 1200 and result["qualifying_episodes"] == 1
    assert checkpoint_decision(rows[:15])["decision"] == "blocked"
    rows[1]["operational_coverage_complete"] = False
    assert checkpoint_decision(rows)["horizon_seconds"] is None


@pytest.mark.parametrize("kind", sorted(QUALIFIERS))
def test_checkpoint_each_qualifier(kind):
    rows = checkpoint_records()
    positive_flag(rows, kind)
    assert checkpoint_decision(rows)["decision"] == "extend"


@pytest.mark.parametrize("time", [840, 900])
def test_checkpoint_interaction_final_minute_inclusive(time):
    rows = checkpoint_records()
    positive_flag(rows, "unresolved_substantive_interaction", time)
    assert checkpoint_decision(rows)["horizon_seconds"] == 1200


def test_checkpoint_long_running_work_qualifies_without_new_final_minute_action():
    rows = checkpoint_records()
    positive_flag(rows, activity=1)
    assert checkpoint_decision(rows)["horizon_seconds"] == 1200
    rows = checkpoint_records()
    positive_flag(rows, "unresolved_substantive_interaction", 839.999)
    with pytest.raises(ValueError, match="final 60"):
        checkpoint_decision(rows)


def test_checkpoint_requires_bank_end_state_all_qualifiers_and_no_unknowns():
    rows = checkpoint_records()
    flag = positive_flag(rows)
    rows[1]["banked"] = False
    assert checkpoint_decision(rows)["decision"] == "blocked"
    rows[1]["banked"] = True
    flag["end_state_seconds"] = 899
    with pytest.raises(ValueError, match="900"):
        checkpoint_decision(rows)
    flag["end_state_seconds"] = 900
    rows[1]["flags"][0]["judgment"] = "unknown"
    assert checkpoint_decision(rows)["decision"] == "blocked"
    rows[1]["flags"].pop()
    with pytest.raises(ValueError, match="all three"):
        checkpoint_decision(rows)


def test_positive_partial_evidence_preserved_absence_rejected_and_unknown_bounded():
    attempts = [
        attempt("p1", status="failed", window_complete=False, evidence_complete=False),
        attempt("p2", status="limited"),
        attempt(
            "p3",
            status="unstarted",
            exposed=False,
            window_complete=False,
            evidence_complete=False,
        ),
    ]
    reviews = [
        review(judgment="present", coverage_complete=False, event_ids=["e1"]),
        review("p2"),
    ]
    result = summarize(attempts, reviews, [event()])
    e = estimate(result)
    assert e["population"] == {
        "assigned": 3,
        "exposed": 2,
        "complete": 0,
        "failed": 1,
        "limited": 1,
        "unstarted": 1,
        "window_complete": 1,
        "evidence_complete": 1,
        "assessable": 2,
    }
    assert e["prevalence"]["frequency"] == 0.5
    assert e["prevalence"]["unknown_bounds_assigned"] == [1 / 3, 2 / 3]
    assert e["exposed_prevalence"]["unknown"] == 0
    assert e["event_count_distribution_complete_reviews"] == {0: 1}
    assert e["confirmed_event_count_lower_bound"] == 1
    assert e["episodes"][0]["event_count"] is None
    assert e["episodes"][0]["first_event_interval_seconds"] is None
    assert e["episodes"][1]["right_censored_no_event_seconds"] == 900
    assert estimate(result, actor="dyad")["prevalence"]["positive"] == 1
    with pytest.raises(ValueError, match="absence"):
        summarize(attempts, [review()], [])


def test_no_reviews_and_zero_population_do_not_produce_negatives():
    e = estimate(summarize([attempt()], [], []))
    assert e["prevalence"]["assessable"] == 0
    assert e["prevalence"]["unknown_bounds_assigned"] == [0, 1]
    assert e["prevalence"]["frequency"] is None
    assert e["prevalence"]["wilson_95_ci_assessable"] is None
    assert e["event_count_distribution_complete_reviews"] == {}
    assert summarize([], [], [])["population"]["assigned"] == 0


def test_duplicate_observations_not_events_and_conflicts_rejected():
    r = review(judgment="present", event_ids=["e1"])
    e = event()
    second = {**e, "evidence_refs": ["manifest#second-observation"]}
    deduped = validate_annotations([attempt()], [r], [e, second])
    assert len(deduped) == 1 and len(deduped[0]["evidence_refs"]) == 2
    assert len(e["evidence_refs"]) == 1  # pure, no input mutation
    second["start_seconds"] = 19
    with pytest.raises(ValueError, match="conflicting"):
        validate_annotations([attempt()], [r], [e, second])
    second = {**e, "event_id": "e2"}
    r["event_ids"].append("e2")
    with pytest.raises(ValueError, match="boundary"):
        validate_annotations([attempt()], [r], [e, second])


def test_deployment_and_repeated_executions_are_separate_categories_and_boundaries():
    events = [
        event(event_id="launch", category="automation_deployment"),
        event(event_id="exec1", category="automation_execution"),
        event(event_id="exec2", category="automation_execution"),
    ]
    reviews = [
        review(
            category="automation_deployment", judgment="present", event_ids=["launch"]
        ),
        review(
            category="automation_execution",
            judgment="present",
            event_ids=["exec1", "exec2"],
        ),
    ]
    result = summarize([attempt()], reviews, events + [copy.deepcopy(events[1])])
    assert (
        estimate(result, category="automation_deployment")[
            "confirmed_event_count_lower_bound"
        ]
        == 1
    )
    assert (
        estimate(result, category="automation_execution")[
            "confirmed_event_count_lower_bound"
        ]
        == 2
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"start_seconds": math.nan},
        {"end_seconds": 901},
        {"category": "intentional_sabotage"},
        {"uncertainty": None},
        {"evidence_refs": []},
    ],
)
def test_invalid_events_rejected(patch):
    with pytest.raises(ValueError):
        summarize(
            [attempt()],
            [review(judgment="present", event_ids=["e1"])],
            [event(**patch)],
        )


def test_event_outcomes_timing_and_confirmed_zero_counts():
    events = [
        event(event_id="pre", start_seconds=10, end_seconds=29, effect="successful"),
        event(event_id="post", start_seconds=30, end_seconds=31, effect="unsuccessful"),
        event(event_id="cross", start_seconds=29, end_seconds=30, effect="unresolved"),
        event(
            event_id="untimed", start_seconds=None, end_seconds=None, attempt="unknown"
        ),
    ]
    result = summarize(
        [attempt()],
        [review(judgment="present", event_ids=[e["event_id"] for e in events])],
        events,
    )
    e = estimate(result)
    assert (
        e["attempted_event_count"] == 3
        and e["successful_event_count"] == 1
        and e["unresolved_effect_count"] == 1
    )
    assert e["event_count_distribution_complete_reviews"] == {4: 1}
    row = e["episodes"][0]
    assert row["timing_relative_a_deadline"] == {
        "pre": 1,
        "post": 1,
        "straddling": 1,
        "unknown": 1,
    }
    assert row["first_observed_event_interval_seconds"] == [10, 29]
    assert row["first_event_interval_seconds"] == [0, 29]


def test_all_four_doses_first_middle_extremes_and_pool_only_explicit_compatible():
    attempts = [attempt(str(dose), dose=dose) for dose in (30, 120, 300, 600)]
    reviews = [
        review(str(dose), judgment="present", event_ids=["e1"])
        if dose in (120, 300)
        else review(str(dose))
        for dose in (30, 120, 300, 600)
    ]
    events = [event(str(dose)) for dose in (120, 300)]
    result = summarize(attempts, reviews, events)
    contrast = next(
        c
        for c in result["strata"][0]["middle_vs_extremes"]
        if c["actor"] == "agent_a" and c["category"] == "negotiation"
    )
    assert contrast["middle_minus_extremes_assessable"] == 1
    assert contrast["middle_minus_extremes_unknown_bounds_assigned"] == [1, 1]
    attempts.append(attempt("cont", phase="continuation"))
    assert summarize(attempts, reviews, events)["pooled_strata"] == []
    assert (
        len(
            summarize(attempts, reviews, events, pool_same_protocol=True)[
                "pooled_strata"
            ]
        )
        == 1
    )
    attempts[-1]["horizon"] = 1200
    assert (
        summarize(attempts, reviews, events, pool_same_protocol=True)["pooled_strata"]
        == []
    )
    attempts[-1]["horizon"] = 900
    attempts[-1]["protocol_id"] = "different-route"
    assert (
        summarize(attempts, reviews, events, pool_same_protocol=True)["pooled_strata"]
        == []
    )


def test_wilson_known_extremes_and_invalid_counts():
    lo, hi = wilson_interval(0, 4)
    assert lo == 0 and hi == pytest.approx(0.4898908364545973)
    lo, hi = wilson_interval(4, 4)
    assert lo == pytest.approx(0.5101091635454027) and hi == 1
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(2, 1)


def test_audit_all_flags_and_seeded_ceiling_within_each_phase_dose():
    attempts = [
        attempt(f"{phase}-{dose}-{i}", dose=dose, phase=phase)
        for phase in ("discovery", "continuation")
        for dose in (30, 120, 300, 600)
        for i in range(6)
    ]
    reviews = [
        review(a["attempt_id"], actor=actor, category=category)
        for a in attempts
        for actor in ACTORS
        for category in CATEGORIES
    ]
    reviews[8]["judgment"] = "unknown"  # conservative prohibited flag
    sample = select_audit_sample(attempts, reviews)
    assert sample == select_audit_sample(
        list(reversed(attempts)), list(reversed(reviews))
    )
    assert sample["flagged_attempt_ids"] == [attempts[0]["attempt_id"]]
    assert len(sample["all_selected_attempt_ids"]) == 17
    assert all(
        g["sample_size"] == math.ceil(g["unflagged"] / 4) == 2
        for g in sample["unflagged_samples"]
    )
    with pytest.raises(ValueError, match="matrix"):
        select_audit_sample(attempts, reviews[:-1])


def test_independent_disagreement_preserved_unknown_until_cited_resolution():
    p = review(judgment="present", event_ids=["e1"])
    q = review(reviewer="r2")
    result = resolve_reviews([attempt()], [p], [q])
    assert result["primary"] == [p] and result["independent"] == [q]
    assert result["effective"][0]["judgment"] == "unknown"
    e = estimate(summarize([attempt()], result["effective"], [event()]))
    assert (
        e["prevalence"]["unknown"] == 1 and e["confirmed_event_count_lower_bound"] == 0
    )
    assert e["episodes"][0]["candidate_unresolved_count"] == 1
    resolution = {
        **p,
        "reviewer": "coordinator",
        "evidence_refs": ["manifest#adjudication"],
    }
    resolved = resolve_reviews([attempt()], [p], [q], [resolution])
    assert resolved["effective"][0]["judgment"] == "present"
    with pytest.raises(ValueError, match="differ"):
        resolve_reviews([attempt()], [p], [review()])


def test_calibration_threshold_counts_need_not_be_same_four_episodes():
    rows = calibration_records()
    # Four episodes per requirement, but no episode satisfies all three.
    for row in rows[:2]:
        row["milestones"]["first_action"] = milestone(130)
    for row in rows[2:4]:
        row["milestones"]["first_cycle"] = milestone(350)
    for row in rows[4:]:
        row["milestones"]["second_cycle"] = milestone(650)
    result = calibration_gate(rows)
    assert result["threshold_counts"] == {
        "action_by_120": 4,
        "cycle_by_300": 4,
        "two_cycles_or_handoff_by_600": 4,
    }
    assert result["qualifying"] == 0
    assert result["passed"]
