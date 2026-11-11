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
