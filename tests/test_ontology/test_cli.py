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
