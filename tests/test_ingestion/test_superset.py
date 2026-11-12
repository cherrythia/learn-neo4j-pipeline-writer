from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.pool import StaticPool

from demo_env import data
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
RETIRED = sum(1 for a in data.HARDWARE if a.status == "retired")


def superset() -> SupersetClient:
    app = create_mock_superset(username="admin", password="pw")
    return SupersetClient("http://testserver", "admin", "pw", http=TestClient(app))


@pytest.fixture
def source() -> Iterator[Engine]:
    """The generated demo seed, loaded into an attached SQLite schema 'demo_itam'."""
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def attach(dbapi_conn: Any, _: Any) -> None:
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS demo_itam")

    with engine.begin() as conn:
        raw = conn.connection.dbapi_connection
        assert raw is not None
        sql = render_demo_inserts().replace("CREATE TABLE ", "CREATE TABLE demo_itam.")
        sql = sql.replace("INSERT INTO ", "INSERT INTO demo_itam.")
        raw.executescript(sql)  # type: ignore[union-attr]
    yield engine
    engine.dispose()


# ------------------------------------------------------------------ Superset client


def test_client_resolves_dataset_from_mock_superset() -> None:
    ds = superset().dataset(2)
    assert (ds.table_name, ds.schema, ds.backend) == ("assets", "demo_itam", "mysql")
    assert ds.columns[:2] == ("asset_tag", "name") and ds.ref == "superset:dataset:2"


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
    total = len(data.HARDWARE)
    assert len(rows) == total and rows[0]["asset_tag"] == data.HARDWARE[0].asset_tag
    assert len({r["asset_tag"] for r in rows}) == total  # no duplicates across pages
    assert len(list(DatasetReader(source).rows(ds, limit=5))) == 5
    assert DatasetReader(source).count(ds) == total


def test_reader_supports_virtual_sql_datasets(source: Engine) -> None:
    ds = DatasetInfo(
        9,
        "retired_assets",
        None,
        "SELECT asset_tag, dept_code FROM demo_itam.assets WHERE status = 'retired'",
        "mysql",
        ("asset_tag", "dept_code"),
    )
    assert RETIRED > 0 and len(list(DatasetReader(source).rows(ds))) == RETIRED


# ------------------------------------------------------------------ mapping

DEPARTMENTS = DatasetMapping(
    entities=[
        EntityMapping(class_name="Department", columns={"dept_code": "dept_code", "name": "name"})
    ]
)
DEPARTMENTS_DS = DatasetInfo(1, "departments", "demo_itam", None, "mysql", ("dept_code", "name"))
ASSETS = DatasetMapping(
    entities=[
        EntityMapping(
            class_name="HardwareAsset",
            columns={"asset_tag": "asset_tag", "name": "name", "status": "status"},
        )
    ]
)
ASSETS_DS = DatasetInfo(
    2,
    "assets",
    "demo_itam",
    None,
    "mysql",
    ("asset_tag", "name", "hardware_type", "manufacturer", "status", "dept_code"),
)


def test_validate_mapping_reports_every_problem() -> None:
    bad = DatasetMapping(
        entities=[
            EntityMapping(class_name="Department", columns={"name": "name", "head": "nope"}),
            EntityMapping(class_name="Company", columns={}),
        ],
        relations=[
            RelationMapping(
                relation="OWNS",
                source=EndMapping(class_name="Department", key_column="x"),
                target=EndMapping(class_name="Department", key_column="dept_code"),
            )
        ],
    )
    errors = validate_mapping(bad, SPEC, DEPARTMENTS_DS)
    joined = " | ".join(errors)
    for expected in (
        "no property 'head'",
        "no column 'nope'",
        "identity key 'dept_code'",
        "unknown or deprecated class 'Company'",
        "relation 'OWNS'",
        "column 'x'",
    ):
        assert expected in joined
    assert validate_mapping(DEPARTMENTS, SPEC, DEPARTMENTS_DS) == []
    assert validate_mapping(ASSETS, SPEC, ASSETS_DS) == []
    assert validate_mapping(DatasetMapping(), SPEC, DEPARTMENTS_DS) == ["mapping is empty"]


def test_rows_become_grounded_candidates() -> None:
    rows = [
        {
            "asset_tag": "HW-23-0001",
            "name": "altair",
            "hardware_type": "laptop",
            "manufacturer": "Corvane (Demo)",
            "status": "in_use",
            "dept_code": "ITS",
        }
    ]
    cands, texts = rows_to_candidates(ASSETS, ASSETS_DS, rows)
    (c,) = cands
    assert isinstance(c, CandidateEntity)
    assert c.properties == {"asset_tag": "HW-23-0001", "name": "altair", "status": "in_use"}
    assert [e.quote for e in c.evidence] == [
        "asset_tag: HW-23-0001",
        "name: altair",
        "status: in_use",
    ]
    assert all(e.locator == "row 1 (asset_tag=HW-23-0001)" for e in c.evidence)
    from writer_service.pipeline.verification import grounding_score

    assert all(grounding_score(e.quote, texts[0][2]) == 1.0 for e in c.evidence)


def test_relation_rows_and_missing_values() -> None:
    m = DatasetMapping(
        relations=[
            RelationMapping(
                relation="ASSIGNED_TO",
                source=EndMapping(class_name="HardwareAsset", key_column="asset_tag"),
                target=EndMapping(class_name="Department", key_column="dept_code"),
            )
        ]
    )
    ds = DatasetInfo(5, "v", None, "x", "mysql", ("asset_tag", "dept_code"))
    cands, _ = rows_to_candidates(
        m,
        ds,
        [{"asset_tag": "HW-1", "dept_code": "FIN"}, {"asset_tag": "HW-2", "dept_code": None}],
    )
    (r,) = cands
    assert isinstance(r, CandidateRelation) and (r.source.key, r.target.key) == ("HW-1", "FIN")


def test_llm_suggestion_is_constrained_and_stored_as_draft(tmp_path: Path) -> None:
    fake = FakeLLM(
        {
            MAPPING_PROMPT.name: [
                {
                    "entities": [
                        {
                            "class_name": "Department",
                            "columns": [
                                {"property": "dept_code", "column": "dept_code"},
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
        DEPARTMENTS_DS,
        [{"dept_code": "FIN", "name": "Finance"}],
    )
    assert draft.entities[0].columns == {"dept_code": "dept_code", "name": "name"}
    schema = str(fake.calls[0].schema)
    assert "'dept_code'" in schema and "'Department'" in schema  # columns and classes are enums


def test_save_approve_retire(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    with UnitOfWork(engine) as uow:
        first = save_draft(uow, DEPARTMENTS_DS, DEPARTMENTS, suggested_by="llm", actor="bot").id
        broken = save_draft(
            uow, DEPARTMENTS_DS, DatasetMapping(), suggested_by="user", actor="alice"
        ).id
    with pytest.raises(MappingError, match="mapping is empty"), UnitOfWork(engine) as uow:
        approve_mapping(uow, broken, SPEC, DEPARTMENTS_DS, actor="alice")
    with UnitOfWork(engine) as uow:
        assert approved_mapping(uow, 1) is None
        approve_mapping(uow, first, SPEC, DEPARTMENTS_DS, actor="alice")
    with UnitOfWork(engine) as uow:
        second = save_draft(uow, DEPARTMENTS_DS, DEPARTMENTS, suggested_by="user", actor="alice").id
        approve_mapping(uow, second, SPEC, DEPARTMENTS_DS, actor="alice")
    with UnitOfWork(engine) as uow:
        statuses = {r.id: r.status for r in uow.session.scalars(select(ColumnMapping))}
        assert statuses[first] == "retired" and statuses[second] == "approved"
        assert approved_mapping(uow, 1) == DEPARTMENTS
        actions = [a.action for a in uow.session.scalars(select(AuditLog))]
        assert actions.count("mapping.approve") == 2
    with pytest.raises(MappingError, match="not a draft"), UnitOfWork(engine) as uow:
        approve_mapping(uow, second, SPEC, DEPARTMENTS_DS, actor="alice")


# ------------------------------------------------------------------ review round 1 (#34)


def test_approve_rejects_a_dataset_that_is_not_the_mappings(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    engine = create_engine_for(url)
    other = DatasetInfo(7, "departments_copy", None, None, "mysql", DEPARTMENTS_DS.columns)
    with UnitOfWork(engine) as uow:
        approve_mapping(
            uow,
            save_draft(uow, other, DEPARTMENTS, suggested_by="user", actor="a").id,
            SPEC,
            other,
            actor="a",
        )
        draft = save_draft(uow, DEPARTMENTS_DS, DEPARTMENTS, suggested_by="user", actor="a").id
    with pytest.raises(MappingError, match="belongs to dataset 1"), UnitOfWork(engine) as uow:
        approve_mapping(uow, draft, SPEC, other, actor="a")
    with UnitOfWork(engine) as uow:
        assert approved_mapping(uow, 7) == DEPARTMENTS  # dataset 7's mapping was not retired


def test_reader_pages_deterministically_when_the_first_column_repeats(source: Engine) -> None:
    ds = DatasetInfo(
        9,
        "assets_by_department",
        None,
        "SELECT dept_code, asset_tag FROM demo_itam.assets",  # dept_code repeats
        "mysql",
        ("dept_code", "asset_tag"),
    )
    statements: list[str] = []
    event.listen(source, "before_cursor_execute", lambda *a: statements.append(a[2]))
    rows = list(DatasetReader(source, page_size=3).rows(ds))
    total = len(data.HARDWARE)
    assert len(rows) == total and len({r["asset_tag"] for r in rows}) == total
    # SQLite happens to be stable; MySQL is not, so every page must order by every column
    assert all("ORDER BY ds.dept_code, ds.asset_tag" in q for q in statements), statements[:1]


def test_count_supports_virtual_sql_datasets(source: Engine) -> None:
    ds = DatasetInfo(
        9,
        "retired_assets",
        None,
        "SELECT asset_tag, dept_code FROM demo_itam.assets WHERE status = 'retired'",
        "mysql",
        ("asset_tag", "dept_code"),
    )
    assert RETIRED > 0 and DatasetReader(source).count(ds) == RETIRED


def test_driver_values_are_normalised_to_candidate_scalars() -> None:
    from datetime import date, datetime
    from decimal import Decimal

    ds = DatasetInfo(6, "licenses", None, None, "mysql", ("asset_tag", "purchase_date", "seats"))
    m = DatasetMapping(
        entities=[
            EntityMapping(
                class_name="SoftwareLicense",
                columns={
                    "asset_tag": "asset_tag",
                    "purchase_date": "purchase_date",
                    "seats": "seats",
                },
            )
        ]
    )
    rows = [
        {
            "asset_tag": "SL-1",
            "purchase_date": datetime(2025, 11, 3, 9, 30),
            "seats": Decimal("50"),
        },
        {"asset_tag": "SL-2", "purchase_date": datetime(2025, 11, 3), "seats": Decimal("12.5")},
    ]
    (f1, f2), _ = rows_to_candidates(m, ds, rows)
    assert isinstance(f1, CandidateEntity) and isinstance(f2, CandidateEntity)
    assert f1.properties["purchase_date"] == "2025-11-03T09:30:00" and f1.properties["seats"] == 50
    assert f2.properties["purchase_date"] == date(2025, 11, 3) and f2.properties["seats"] == 12.5


def test_validate_mapping_checks_relation_domain_and_range() -> None:
    ds = DatasetInfo(2, "assets", None, None, "mysql", ("asset_tag", "dept_code"))
    backwards = DatasetMapping(
        relations=[
            RelationMapping(
                relation="ASSIGNED_TO",
                source=EndMapping(class_name="Department", key_column="dept_code"),
                target=EndMapping(class_name="HardwareAsset", key_column="asset_tag"),
            )
        ]
    )
    errors = validate_mapping(backwards, SPEC, ds)
    assert any("source must be a 'HardwareAsset'" in e for e in errors), errors
    assert any("target must be a 'Department'" in e for e in errors), errors


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


def test_rows_sharing_a_first_column_all_ground_exactly() -> None:
    """Review round 2: locators must be unique, or row texts overwrite each other."""
    from writer_service.pipeline.verification import DictSourceText

    ds = DatasetInfo(2, "assets", None, None, "mysql", ("dept_code", "asset_tag", "hardware_type"))
    m = DatasetMapping(
        entities=[
            EntityMapping(
                class_name="HardwareAsset",
                columns={"asset_tag": "asset_tag", "hardware_type": "hardware_type"},
            )
        ]
    )
    rows = [
        {"dept_code": "ENG", "asset_tag": "HW-1", "hardware_type": "laptop"},
        {"dept_code": "ENG", "asset_tag": "HW-2", "hardware_type": "desktop"},
    ]
    cands, texts = rows_to_candidates(m, ds, rows)
    assert len({t[1] for t in texts}) == 2
    lookup = DictSourceText({(t[0], t[1]): t[2] for t in texts})
    assert [lookup.grounding(c) for c in cands] == [1.0, 1.0]


def test_relation_keys_are_normalised_like_entity_keys() -> None:
    from decimal import Decimal

    ds = DatasetInfo(2, "assets", None, None, "mysql", ("asset_tag", "dept_id"))
    m = DatasetMapping(
        entities=[EntityMapping(class_name="Department", columns={"dept_code": "dept_id"})],
        relations=[
            RelationMapping(
                relation="ASSIGNED_TO",
                source=EndMapping(class_name="HardwareAsset", key_column="asset_tag"),
                target=EndMapping(class_name="Department", key_column="dept_id"),
            )
        ],
    )
    (department, rel), _ = rows_to_candidates(
        m, ds, [{"asset_tag": "HW-1", "dept_id": Decimal("5.00")}]
    )
    assert isinstance(department, CandidateEntity) and isinstance(rel, CandidateRelation)
    assert str(department.properties["dept_code"]) == rel.target.key == "5"
