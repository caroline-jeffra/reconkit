"""Tests for the shared result types."""


from reconkit.core.models import ROW_FIELDS, Outcome, ProbeResult


def test_outcome_is_str_enum():
    # Outcome subclasses str so `.value` lands in CSV/JSON without conversion.
    assert Outcome.VALID == "valid"
    assert Outcome.SKIPPED.value == "skipped"


def test_to_row_uses_declared_field_order():
    row = ProbeResult("a.example", Outcome.VALID).to_row()
    assert tuple(row) == ROW_FIELDS


def test_to_row_renders_outcome_as_value_not_enum():
    row = ProbeResult("a.example", Outcome.NEGATIVE).to_row()
    assert row["outcome"] == "negative"
    assert not isinstance(row["outcome"], Outcome)


def test_to_row_renders_absent_status_as_empty_string():
    # None would serialise as null/None; an empty cell is what CSV wants.
    assert ProbeResult("a.example", Outcome.ERROR).to_row()["status"] == ""


def test_to_row_preserves_a_real_status():
    assert ProbeResult("a.example", Outcome.VALID, status=200).to_row()["status"] == 200


def test_to_row_keeps_data_as_a_dict():
    # write_results is responsible for encoding data; to_row leaves it structured.
    row = ProbeResult("a.example", Outcome.VALID, data={"names": ["admin"]}).to_row()
    assert row["data"] == {"names": ["admin"]}


def test_defaults_are_falsy_and_not_shared():
    a = ProbeResult("a.example", Outcome.VALID)
    b = ProbeResult("b.example", Outcome.VALID)
    a.data["x"] = 1
    assert b.data == {}          # field(default_factory) — no shared mutable default
    assert (a.status, a.scheme, a.final_url, a.detail) == (None, "", "", "")
