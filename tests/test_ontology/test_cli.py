from pathlib import Path

import pytest

from writer_service.governance.migrate import upgrade
from writer_service.ontology.cli import main, render
from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec


def test_render_shows_taxonomy_keys_and_relations() -> None:
    out = render(OntologySpec(CORE_CLASSES, CORE_RELATIONS))
    assert ":Issuer key=ticker is-a Organisation" in out
    assert "ticker: string (required)" in out
    assert "(Filing)-[:FILED_BY]->(Issuer) many_to_one" in out


def test_domain_errors_exit_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    monkeypatch.setenv("WRITER_GOVERNANCE_DB_URL", url)
    monkeypatch.chdir(tmp_path)  # no stray .env
    with pytest.raises(SystemExit, match="error: nothing is published"):
        main(["rollback"])  # fails before any Neo4j query, so no server is needed


def test_show_without_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    monkeypatch.setenv("WRITER_GOVERNANCE_DB_URL", url)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no such version"):
        main(["show"])


def test_show_zero_is_not_the_published_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from writer_service.governance.db import create_engine_for
    from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
    from writer_service.ontology.service import OntologyService

    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    OntologyService(create_engine_for(url), InMemoryGraphSchema()).bootstrap("t")
    monkeypatch.setenv("WRITER_GOVERNANCE_DB_URL", url)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no such version"):
        main(["show", "0"])


def test_sync_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#28: `writer-ontology sync` re-applies the published schema (full-text indexes)."""
    import writer_service.ontology.cli as cli
    from writer_service.governance.db import create_engine_for
    from writer_service.ontology.neo4j_schema import InMemoryGraphSchema
    from writer_service.ontology.service import OntologyService

    url = f"sqlite:///{tmp_path / 'g.sqlite'}"
    upgrade(url)
    graph = InMemoryGraphSchema()
    OntologyService(create_engine_for(url), graph).bootstrap("t")
    graph.names.discard("wsf_issuer_ticker_name")
    monkeypatch.setenv("WRITER_GOVERNANCE_DB_URL", url)
    monkeypatch.chdir(tmp_path)

    class FakeDriver:
        def __enter__(self) -> "FakeDriver":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(cli.GraphDatabase, "driver", lambda *a, **k: FakeDriver())
    monkeypatch.setattr(cli, "Neo4jGraphSchema", lambda driver: graph)
    main(["sync"])
    assert capsys.readouterr().out.strip() == "created: wsf_issuer_ticker_name; dropped: -"
