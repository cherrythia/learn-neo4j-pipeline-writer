from pydantic import BaseModel, Field

from writer_service.llm.schema import strict_json_schema


class Inner(BaseModel):
    a: int
    b: str = "x"


class Outer(BaseModel):
    items: list[Inner]
    note: str | None = None
    score: float = Field(ge=0, le=1)


def test_every_object_is_closed_and_fully_required() -> None:
    schema = strict_json_schema(Outer)

    def objects(node: object) -> list[dict[str, object]]:
        found = []
        if isinstance(node, dict):
            if node.get("type") == "object":
                found.append(node)
            for v in node.values():
                found += objects(v)
        elif isinstance(node, list):
            for v in node:
                found += objects(v)
        return found

    objs = objects(schema)
    assert len(objs) >= 2
    for o in objs:
        assert o["additionalProperties"] is False
        assert set(o["required"]) == set(o["properties"])  # type: ignore[arg-type]


def test_schema_is_deterministic() -> None:
    assert strict_json_schema(Outer) == strict_json_schema(Outer)
