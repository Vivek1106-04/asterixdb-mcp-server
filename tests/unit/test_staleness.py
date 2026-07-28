"""Unit tests for stale-note detection against fresh query rows.

The two cases that motivated the check are named after what they do to an
answer: a drifted count is silently wrong, and a field that changed type turns
a stored conversion rule into a thousandfold error.
"""

from __future__ import annotations

from asterixdb_mcp.staleness import (
    MAX_CHECKED_ROWS,
    MAX_CONFLICTS_PER_NOTE,
    MAX_REASON_LEN,
    check_rows,
    field_types,
    flag_suspect,
    is_suspect,
    note_conflicts,
    numeric_drift,
    renamed_fields,
    render_warning,
    result_claims,
    type_drift,
)

DRIFT_NOTE = "Status breakdown: Operational (410), Low kV (17), Proposed (5), Closed (1)."
DRIFTED_ROWS = [{"status": "Operational", "n": 290}, {"status": "Retired", "n": 120}]


# the two Phase-2 traps


def test_drifted_count_is_flagged_under_its_own_category() -> None:
    conflicts = numeric_drift(DRIFT_NOTE, DRIFTED_ROWS)

    assert conflicts == ["'operational': note says 410.0, this result shows 290.0"]


def test_field_that_became_numeric_is_flagged() -> None:
    note = "DAMAGE_PROPERTY values use string suffixes 'K' and 'M'; multiply out before summing."
    rows = [{"EVENT_TYPE": "Flash Flood", "DAMAGE_PROPERTY": 1500000.0}]

    assert type_drift(note, rows) == [
        "'DAMAGE_PROPERTY': note describes it as string, this result returns number"
    ]


# false positives, the failure mode worse than a surviving stale note


def test_matching_count_is_not_a_conflict() -> None:
    assert numeric_drift(DRIFT_NOTE, [{"status": "Operational", "n": 410}]) == []


def test_value_inside_the_stored_range_is_not_a_conflict() -> None:
    assert numeric_drift("ratings for Deluxe run 1 to 5", [{"tier": "Deluxe", "avg": 2.5}]) == []


def test_field_name_alone_never_labels_a_claim() -> None:
    # "711 rows in the dataset" and a per-county bed count are unlike quantities.
    note = "hospitals.beds holds 711 rows"
    assert numeric_drift(note, [{"county": "Maricopa", "beds": 9000}]) == []


def test_a_note_asserting_both_types_asserts_nothing() -> None:
    note = "DAMAGE_PROPERTY: string or number depending on the source file"
    assert type_drift(note, [{"DAMAGE_PROPERTY": 1500000.0}]) == []


def test_matching_type_is_not_a_conflict() -> None:
    assert type_drift("tags is a string field", [{"tags": "a,b"}]) == []


def test_note_mentioning_no_field_asserts_no_type() -> None:
    assert type_drift("counts are refreshed nightly", [{"tags": "a,b"}]) == []


def test_second_mention_settles_a_type_the_first_left_ambiguous() -> None:
    note = "tags: string or number, unclear. Later proven: tags is a number."
    assert type_drift(note, [{"tags": "a,b"}]) == [
        "'tags': note describes it as number, this result returns string"
    ]


# field_types


def test_mixed_type_field_carries_no_assertion() -> None:
    assert field_types([{"amount": "1.5M"}, {"amount": 1500000.0}]) == {}


def test_short_field_names_and_untyped_values_are_dropped() -> None:
    rows = [{"n": 5, "ok": True, "note": None, "county": "Pima", "beds": 12}]

    assert field_types(rows) == {"county": "string", "beds": "number"}


def test_non_dict_rows_are_ignored() -> None:
    assert field_types(["scalar", 42]) == {}
    assert result_claims(["scalar", 42]) == {}


def test_rows_beyond_the_check_bound_are_not_read() -> None:
    rows = [{"status": "Operational", "n": 410}] * MAX_CHECKED_ROWS
    rows.append({"status": "Operational", "n": 290})

    assert numeric_drift(DRIFT_NOTE, rows) == []


# result_claims


def test_rows_without_numbers_claim_nothing() -> None:
    assert result_claims([{"status": "Operational"}]) == {}


def test_row_claims_every_number_under_every_category_word() -> None:
    assert result_claims([{"status": "Operational", "n": 290}]) == {"operational": {"290.0"}}


# note_conflicts


def test_a_note_with_no_numbers_and_no_field_mention_is_clean() -> None:
    assert note_conflicts("prefer the covering index here", DRIFTED_ROWS) == []


def test_blank_note_or_empty_result_is_never_checked() -> None:
    assert note_conflicts("   ", DRIFTED_ROWS) == []
    assert note_conflicts(DRIFT_NOTE, []) == []


def test_reported_conflicts_are_capped() -> None:
    categories = ["alpha", "bravo", "charlie", "delta", "echo"]
    note = "; ".join(f"{name} total {i}" for i, name in enumerate(categories))
    rows = [{"label": name, "total": 100 + i} for i, name in enumerate(categories)]

    assert len(note_conflicts(note, rows)) == MAX_CONFLICTS_PER_NOTE
    assert len(numeric_drift(note, rows)) == MAX_CONFLICTS_PER_NOTE


def test_type_conflicts_are_capped() -> None:
    note = " ".join(f"field_{i} is a string." for i in range(10))
    rows = [{f"field_{i}": i for i in range(10)}]

    assert len(type_drift(note, rows)) == MAX_CONFLICTS_PER_NOTE


# suspect marking


def test_flagging_leaves_the_note_text_untouched() -> None:
    row = {"id": "D.s@t0", "subject": "D.s", "text": "note"}

    flagged = flag_suspect(row, ["'operational': note says 410.0"])

    assert flagged["text"] == "note" and flagged["id"] == "D.s@t0"
    assert flagged["suspect_reason"] == "'operational': note says 410.0"
    assert is_suspect(flagged) and not is_suspect(row)


def test_suspect_reason_is_length_capped() -> None:
    flagged = flag_suspect({"id": "x"}, ["y" * (MAX_REASON_LEN + 100)])

    assert len(flagged["suspect_reason"]) == MAX_REASON_LEN


def test_check_rows_flags_only_the_contradicted_note() -> None:
    rows = [
        {"id": "a", "subject": "RE.substations", "text": DRIFT_NOTE},
        {"id": "b", "subject": "RE.substations", "text": "prefer the covering index"},
    ]

    flagged, reported = check_rows(rows, DRIFTED_ROWS)

    assert is_suspect(flagged[0]) and not is_suspect(flagged[1])
    assert flagged[1] is rows[1]
    assert reported == [
        "- [RE.substations] 'operational': note says 410.0, this result shows 290.0"
    ]


def test_check_rows_reads_the_overlay_of_a_walk_owned_concept() -> None:
    rows = [{"id": "a", "subject": "RE.substations", "text": "core", "overlay": DRIFT_NOTE}]

    _, reported = check_rows(rows, DRIFTED_ROWS)

    assert len(reported) == 1


def test_check_rows_tolerates_a_row_with_no_text() -> None:
    flagged, reported = check_rows([{"id": "a"}], DRIFTED_ROWS)

    assert reported == [] and flagged == [{"id": "a"}]


# rendering


def test_warning_names_the_disagreement_and_how_to_fix_it() -> None:
    text = render_warning(["- [RE.substations] 'operational': note says 410.0"])

    assert "STALE NOTE CHECK" in text
    assert "'operational'" in text
    assert "memory_write" in text and "replaces=" in text


def test_no_warning_without_a_contradiction() -> None:
    assert render_warning([]) == ""


def test_a_shared_category_is_not_enough_without_a_shared_measure() -> None:
    # The note's 150 is deaths; the row's number is dollars. Same category word,
    # different quantities — observed as a false positive against live data.
    note = "Deadliest events are dominated by Heat in Arizona (max 150 deaths in Phoenix)."
    rows = [{"EVENT_TYPE": "Heat", "DAMAGE_PROPERTY": 0.0}]

    assert numeric_drift(note, rows) == []


def test_a_claim_naming_the_measured_field_is_compared() -> None:
    rows = [{"county": "Maricopa", "beds": 9000}]

    assert numeric_drift("Maricopa has 8500 beds", rows) == [
        "'maricopa': note says 8500.0, this result shows 9000.0"
    ]


# renamed fields: only visible in a whole record


HOSPITALS_NOTE = (
    "hospitals dataset stores lat, lon, name, type, status ('OPEN' vs 'CLOSED'), "
    "state, county, beds (-999 for missing)."
)
RENAMED_ROWS = [
    {
        "lat": 31.3,
        "lon": -110.9,
        "name": "HOLY CROSS",
        "type": "GENERAL ACUTE CARE",
        "status": "OPEN",
        "state": "AZ",
        "county": "SANTA CRUZ",
        "bed_count": 56,
    }
]


def test_a_renamed_field_is_flagged_against_a_whole_record() -> None:
    assert note_conflicts(HOSPITALS_NOTE, RENAMED_ROWS, whole_rows=True) == [
        "'beds': the note names it, but the rows carry 'bed_count'"
    ]


def test_a_projected_result_never_reports_a_missing_field() -> None:
    # The query simply did not ask for it; absence is not evidence here.
    assert note_conflicts(HOSPITALS_NOTE, RENAMED_ROWS) == []


def test_prose_that_does_not_describe_fields_is_not_checked_for_renames() -> None:
    assert renamed_fields("substations contains 433 substations all in CA.", RENAMED_ROWS) == []


def test_a_note_whose_fields_all_still_exist_reports_nothing() -> None:
    rows = [{"id": "a", "lon": 1.0, "lat": 2.0, "height": 3.9, "subtype": "residential"}]
    note = "buildings has fields: id, lon, lat, height, subtype."

    assert renamed_fields(note, rows) == []


def test_an_unrelated_word_is_not_read_as_a_rename() -> None:
    note = "hospitals stores lat, lon, name, county and serves the Phoenix metro area."

    assert renamed_fields(note, RENAMED_ROWS) == []


def test_renames_are_capped() -> None:
    rows = [
        {"keep_one": 1, "keep_two": 2}
        | {f"{name}_count": i for i, name in enumerate(("widget", "gadget", "sprocket", "bracket"))}
    ]
    note = "names keep_one, keep_two and widgets, gadgets, sprockets, brackets"

    assert len(renamed_fields(note, rows)) == MAX_CONFLICTS_PER_NOTE


def test_a_stem_too_short_to_mean_anything_is_not_matched() -> None:
    rows = [{"keep_one": 1, "keep_two": 2, "id_count": 3}]

    assert renamed_fields("names keep_one, keep_two and ids", rows) == []


def test_a_plural_alone_is_not_a_rename() -> None:
    rows = [{"keep_one": 1, "keep_two": 2, "widget": 3}]

    assert renamed_fields("names keep_one, keep_two and widgets", rows) == []


def test_a_word_matching_only_the_tail_of_a_field_is_not_a_rename() -> None:
    # "counts" is prose; bed_count is a bed count, not a count of something.
    assert (
        renamed_fields("hospitals stores lat, lon, county and counts by state", RENAMED_ROWS) == []
    )


def test_a_field_name_inside_a_longer_word_is_not_a_mention() -> None:
    # "lat" also lives inside "population"; the type word there is unrelated.
    note = "hospitals stores lat, lon; joining population uses a FIPS code string."

    assert type_drift(note, RENAMED_ROWS) == []
