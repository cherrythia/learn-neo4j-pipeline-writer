from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.pool import StaticPool

from demo_env.mock_superset import create_mock_superset
from demo_env.seed_sql import render_demo_inserts
from writer_service.governance.db import create_engine_for
from writer_service.governance.migrate import upgrade
from writer_service.governance.models import AuditLog, ColumnMapping
from writer_service.governance.uow import UnitOfWork
from writer_service.ingestion.superset import (
    MAPPING_PROMPT,
    DatasetInfo,
    DatasetMapping,
    DatasetReader,
    EndMapping,
    EntityMapping,
    MappingError,
    RelationMapping,
    SupersetClient,
    SupersetError,
    approve_mapping,
    approved_mapping,
    rows_to_candidates,
    save_draft,
    suggest_mapping,
    validate_mapping,
)
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.candidates import CandidateEntity, CandidateRelation

SPEC = OntologySpec(CORE_CLASSES, CORE_RELATIONS)


def superset() -> SupersetClient:
    app = create_mock_superset(username="admin", password="pw")
    return SupersetClient("http://testserver", "admin", "pw", http=TestClient(app))


@pytest.fixture
def source() -> Iterator[Engine]:
    """The generated demo seed, loaded into an attached SQLite schema 'demo_compliance'."""
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def attach(dbapi_conn: Any, _: Any) -> None:
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS demo_compliance")

    with engine.begin() as conn:
        raw = conn.connection.dbapi_connection
        assert raw is not None
        sql = render_demo_inserts().replace("CREATE TABLE ", "CREATE TABLE demo_compliance.")
        sql = sql.replace("INSERT INTO ", "INSERT INTO demo_compliance.")
        raw.executescript(sql)  # type: ignore[union-attr]
    yield engine
    engine.dispose()


# ------------------------------------------------------------------ Superset client


def test_client_resolves_dataset_from_mock_superset() -> None:
    ds = superset().dataset(2)
    assert (ds.table_name, ds.schema, ds.backend) == ("filings", "demo_compliance", "mysql")
    assert ds.columns[:2] == ("filing_id", "issuer_id") and ds.ref == "superset:dataset:2"


def test_client_errors() -> None:
    with pytest.raises(SupersetError, match="not found"):
        superset().dataset(999)
    bad = SupersetClient(
        "http://testserver",
        "admin",
        "wrong",
        http=TestClient(create_mock_superset(username="admin", password="pw")),
    )
    with pytest.raises(SupersetError, match="login failed"):
        bad.dataset(1)


# ------------------------------------------------------------------ reader


def test_reader_pages_through_all_rows(source: Engine) -> None:
    ds = superset().dataset(2)
    rows = list(DatasetReader(source, page_size=7).rows(ds))
    assert len(rows) == 40 and rows[0]["filing_id"] == "AR-2025-APH"
    assert len({r["filing_id"] for r in rows}) == 40  # no duplicates across pages
    assert len(list(DatasetReader(source).rows(ds, limit=5))) == 5
    assert DatasetReader(source).count(ds) == 40


def test_reader_supports_virtual_sql_datasets(source: Engine) -> None:
    ds = DatasetInfo(
        9,
        "late_filings",
        None,
        "SELECT filing_id, issuer_id FROM demo_compliance.filings WHERE filed_on > due_on",
        "mysql",
        ("filing_id", "issuer_id"),
    )
    assert len(list(DatasetReader(source).rows(ds))) == 3


# ------------------------------------------------------------------ mapping

ISSUERS = DatasetMapping(
    entities=[
        EntityMapping(
            class_name="Issuer", columns={"ticker": "ticker", "name": "name", "board": "board"}
        )
    ]
)
ISSUERS_DS = DatasetInfo(
    1,
    "issuers",
    "demo_compliance",
    None,
    "mysql",
    ("issuer_id", "name", "ticker", "sector", "board", "country"),
)


def test_validate_mapping_reports_every_problem() -> None:
    bad = DatasetMapping(
        entities=[
            EntityMapping(class_name="Issuer", columns={"name": "name", "ceo": "nope"}),
            EntityMapping(class_name="Company", columns={}),
        ],
        relations=[
            RelationMapping(
                relation="OWNS",
                source=EndMapping(class_name="Issuer", key_column="x"),
                target=EndMapping(class_name="Issuer", key_column="ticker"),
            )
        ],
    )
    errors = validate_mapping(bad, SPEC, ISSUERS_DS)
    joined = " | ".join(errors)
    for expected in (
        "no property 'ceo'",
        "no column 'nope'",
        "identity key 'ticker'",
        "unknown or deprecated class 'Company'",
        "relation 'OWNS'",
        "column 'x'",
    ):
        assert expected in joined
    assert validate_mapping(ISSUERS, SPEC, ISSUERS_DS) == []
    assert validate_mapping(DatasetMapping(), SPEC, ISSUERS_DS) == ["mapping is empty"]


def test_rows_become_grounded_candidates() -> None:
    rows = [
        {
            "issuer_id": 1,
            "name": "Aurora Pacific Holdings (Demo)",
            "ticker": "APH",
            "sector": "Energy",
            "board": "mainboard",
            "country": "SG",
        }
    ]
    cands, texts = rows_to_candidates(ISSUERS, ISSUERS_DS, rows)
    (c,) = cands
    assert isinstance(c, CandidateEntity)
    assert c.properties == {
        "ticker": "APH",
        "name": "Aurora Pacific Holdings (Demo)",
        "board": "mainboard",
    }
    assert [e.quote for e in c.evidence] == [
        "name: Aurora Pacific Holdings (Demo)",
        "ticker: APH",
        "board: mainboard",
    ]
    assert all(e.locator == "row issuer_id=1" for e in c.evidence)
    from writer_service.pipeline.verification import grounding_score

    assert all(grounding_score(e.quote, texts[0][2]) == 1.0 for e in c.evidence)


def test_relation_rows_and_missing_values() -> None:
    m = DatasetMapping(
        relations=[
            RelationMapping(
                relation="FILED_BY",
                source=EndMapping(class_name="Filing", key_column="filing_id"),
                target=EndMapping(class_name="Issuer", key_column="issuer_ticker"),
            )
        ]
    )
    ds = DatasetInfo(5, "v", None, "x", "mysql", ("filing_id", "issuer_ticker"))
    cands, _ = rows_to_candidates(
        m,
        ds,
        [{"filing_id": "F1", "issuer_ticker": "APH"}, {"filing_id": "F2", "issuer_ticker": None}],
    )
    (r,) = cands
    assert isinstance(r, CandidateRelation) and (r.source.key, r.target.key) == ("F1", "APH")


def test_llm_suggestion_is_constrained_and_stored_as_draft(tmp_path: Path) -> None:
    fake = FakeLLM(
        {
            MAPPING_PROMPT.name: [
                {
                    "entities": [
                        {
                            "class_name": "Issuer",
                            "columns": [
                                {"property": "ticker", "column": "ticker"},
                                {"property": "name", "column": "name"},
                            ],
                        }
                    ],
                    "relations": [],
                }
            ]
        }
    )
    draft = suggest_mapping(
        StructuredLLM(fake, InMemoryCache()),
        SPEC,
        1,
        ISSUERS_DS,
        [{"ticker": "APH", "name": "Aurora"}],
    )
    assert draft.entities[0].columns == {"ticker": "ticker", "name": "name"}
    schema = str(fake.calls[0].schema)
    assert "'issuer_id'" in schema and "'Issuer'" in schema  # columns and classes are enums


def test_save_approve_retire(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    with UnitOfWork(engine) as uow:
        first = save_draft(uow, ISSUERS_DS, ISSUERS, suggested_by="llm", actor="bot").id
        broken = save_draft(
            uow, ISSUERS_DS, DatasetMapping(), suggested_by="user", actor="alice"
        ).id
    with pytest.raises(MappingError, match="mapping is empty"), UnitOfWork(engine) as uow:
        approve_mapping(uow, broken, SPEC, ISSUERS_DS, actor="alice")
    with UnitOfWork(engine) as uow:
        assert approved_mapping(uow, 1) is None
        approve_mapping(uow, first, SPEC, ISSUERS_DS, actor="alice")
    with UnitOfWork(engine) as uow:
        second = save_draft(uow, ISSUERS_DS, ISSUERS, suggested_by="user", actor="alice").id
        approve_mapping(uow, second, SPEC, ISSUERS_DS, actor="alice")
    with UnitOfWork(engine) as uow:
        statuses = {r.id: r.status for r in uow.session.scalars(select(ColumnMapping))}
        assert statuses[first] == "retired" and statuses[second] == "approved"
        assert approved_mapping(uow, 1) == ISSUERS
        actions = [a.action for a in uow.session.scalars(select(AuditLog))]
        assert actions.count("mapping.approve") == 2
    with pytest.raises(MappingError, match="not a draft"), UnitOfWork(engine) as uow:
        approve_mapping(uow, second, SPEC, ISSUERS_DS, actor="alice")


# ------------------------------------------------------------------ review round 1 (#34)


def test_approve_rejects_a_dataset_that_is_not_the_mappings(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    other = DatasetInfo(7, "issuers_copy", None, None, "mysql", ISSUERS_DS.columns)
    with UnitOfWork(engine) as uow:
        approve_mapping(
            uow,
            save_draft(uow, other, ISSUERS, suggested_by="user", actor="a").id,
            SPEC,
            other,
            actor="a",
        )
        draft = save_draft(uow, ISSUERS_DS, ISSUERS, suggested_by="user", actor="a").id
    with pytest.raises(MappingError, match="belongs to dataset 1"), UnitOfWork(engine) as uow:
        approve_mapping(uow, draft, SPEC, other, actor="a")
    with UnitOfWork(engine) as uow:
        assert approved_mapping(uow, 7) == ISSUERS  # dataset 7's mapping was not retired


def test_reader_pages_deterministically_when_the_first_column_repeats(source: Engine) -> None:
    ds = DatasetInfo(
        9,
        "filings_by_issuer",
        None,
        "SELECT issuer_id, filing_id FROM demo_compliance.filings",  # issuer_id repeats
        "mysql",
        ("issuer_id", "filing_id"),
    )
    statements: list[str] = []
    event.listen(source, "before_cursor_execute", lambda *a: statements.append(a[2]))
    rows = list(DatasetReader(source, page_size=3).rows(ds))
    assert len(rows) == 40 and len({r["filing_id"] for r in rows}) == 40
    # SQLite happens to be stable; MySQL is not, so every page must order by every column
    assert all("ORDER BY ds.issuer_id, ds.filing_id" in q for q in statements), statements[:1]


def test_count_supports_virtual_sql_datasets(source: Engine) -> None:
    ds = DatasetInfo(
        9,
        "late_filings",
        None,
        "SELECT filing_id, issuer_id FROM demo_compliance.filings WHERE filed_on > due_on",
        "mysql",
        ("filing_id", "issuer_id"),
    )
    assert DatasetReader(source).count(ds) == 3


def test_driver_values_are_normalised_to_candidate_scalars() -> None:
    from datetime import date, datetime
    from decimal import Decimal

    ds = DatasetInfo(2, "filings", None, None, "mysql", ("filing_id", "filed_on", "fiscal_year"))
    m = DatasetMapping(
        entities=[
            EntityMapping(
                class_name="Filing",
                columns={
                    "filing_id": "filing_id",
                    "filed_on": "filed_on",
                    "fiscal_year": "fiscal_year",
                },
            )
        ]
    )
    rows = [
        {
            "filing_id": "F1",
            "filed_on": datetime(2026, 4, 28, 9, 30),
            "fiscal_year": Decimal("2025"),
        },
        {"filing_id": "F2", "filed_on": datetime(2026, 4, 28), "fiscal_year": Decimal("2025.5")},
    ]
    (f1, f2), _ = rows_to_candidates(m, ds, rows)
    assert isinstance(f1, CandidateEntity) and isinstance(f2, CandidateEntity)
    assert (
        f1.properties["filed_on"] == "2026-04-28T09:30:00" and f1.properties["fiscal_year"] == 2025
    )
    assert f2.properties["filed_on"] == date(2026, 4, 28) and f2.properties["fiscal_year"] == 2025.5


def test_validate_mapping_checks_relation_domain_and_range() -> None:
    ds = DatasetInfo(2, "filings", None, None, "mysql", ("filing_id", "issuer_id"))
    backwards = DatasetMapping(
        relations=[
            RelationMapping(
                relation="FILED_BY",
                source=EndMapping(class_name="Issuer", key_column="issuer_id"),
                target=EndMapping(class_name="Filing", key_column="filing_id"),
            )
        ]
    )
    errors = validate_mapping(backwards, SPEC, ds)
    assert any("source must be a 'Filing'" in e for e in errors), errors
    assert any("target must be a 'Issuer'" in e for e in errors), errors


def test_transport_errors_become_superset_errors() -> None:
    import httpx2

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    client = SupersetClient(
        "http://x",
        "a",
        "b",
        http=httpx2.Client(base_url="http://x", transport=httpx2.MockTransport(boom)),
    )
    with pytest.raises(SupersetError, match="unreachable"):
        client.dataset(1)


def test_missing_dataset_is_a_distinct_error() -> None:
    from writer_service.ingestion.superset import DatasetNotFound

    with pytest.raises(DatasetNotFound):
        superset().dataset(999)
