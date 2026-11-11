from datetime import date

from demo_env import data


def test_row_counts_match_demo_story() -> None:
    assert len(data.ISSUERS) == 20
    assert len(data.RULES) >= 6
    assert len(data.FILINGS) == 40  # annual + ESG report per issuer
    assert len(data.OBLIGATIONS) >= 6


def test_primary_keys_unique() -> None:
    for rows, key in [
        (data.ISSUERS, "issuer_id"),
        (data.FILINGS, "filing_id"),
        (data.RULES, "rule_code"),
        (data.OBLIGATIONS, "obligation_id"),
    ]:
        ids = [getattr(r, key) for r in rows]
        assert len(ids) == len(set(ids)), key


def test_foreign_keys_resolve() -> None:
    issuer_ids = {i.issuer_id for i in data.ISSUERS}
    rule_codes = {r.rule_code for r in data.RULES}
    assert all(f.issuer_id in issuer_ids for f in data.FILINGS)
    assert all(o.rule_code in rule_codes for o in data.OBLIGATIONS)


def test_enumerations_are_closed() -> None:
    assert {i.board for i in data.ISSUERS} <= {"mainboard", "catalist"}
    assert {f.filing_type for f in data.FILINGS} <= {"annual_report", "esg_report"}
    assert {o.board for o in data.OBLIGATIONS} <= {"mainboard", "catalist"}


def test_filing_dates_are_consistent() -> None:
    for f in data.FILINGS:
        assert isinstance(f.filed_on, date)
        assert f.filed_on.year == f.fiscal_year + 1


def test_data_is_fictional_marker_present() -> None:
    # every issuer name carries the demo suffix so nobody mistakes it for a real company
    assert all(i.name.endswith("(Demo)") for i in data.ISSUERS)


def test_some_filings_are_late_for_interesting_findings() -> None:
    late = [f for f in data.FILINGS if f.filed_on > f.due_on]
    assert 1 <= len(late) < len(data.FILINGS)


def test_deadlines_derive_from_obligations() -> None:
    for f in data.FILINGS:
        rule = data.timing_rule_for(f)
        issuer = next(i for i in data.ISSUERS if i.issuer_id == f.issuer_id)
        ob = next(
            o for o in data.OBLIGATIONS if o.rule_code == rule.rule_code and o.board == issuer.board
        )
        assert (f.due_on - data.date(2025, 12, 31)).days == ob.deadline_days


def test_timing_rule_text_agrees_with_obligation_deadline() -> None:
    """The pipeline reads deadlines from rule text, so text and obligation must agree."""
    import re

    year_end = data.date(2025, 12, 31)
    for (board, _ftype), code in data.TIMING_RULE.items():
        rule = next(r for r in data.RULES if r.rule_code == code)
        days = next(
            o.deadline_days for o in data.OBLIGATIONS if o.rule_code == code and o.board == board
        )
        m = re.search(r"within (\d+) (months|days)", rule.text)
        assert m, code
        n, unit = int(m.group(1)), m.group(2)
        if unit == "days":
            text_due = year_end + data.timedelta(days=n)
        else:  # month-end arithmetic from 31 Dec
            month = n
            text_due = data.date(2026, month + 1, 1) - data.timedelta(days=1)
        assert text_due == year_end + data.timedelta(days=days), code
