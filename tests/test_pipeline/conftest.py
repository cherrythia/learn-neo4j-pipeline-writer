import pytest

from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
from writer_service.ontology.spec import OntologySpec
from writer_service.pipeline.graph_view import InMemoryGraphView


@pytest.fixture
def spec() -> OntologySpec:
    return OntologySpec(CORE_CLASSES, CORE_RELATIONS)


@pytest.fixture
def graph() -> InMemoryGraphView:
    return InMemoryGraphView()
