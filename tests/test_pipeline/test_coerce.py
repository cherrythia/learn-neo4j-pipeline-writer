from datetime import date

import pytest

from writer_service.pipeline.gates import _CoercionError, coerce


@pytest.mark.parametrize(
    "value,datatype,expected",
    [
        ("  a  b ", "string", "a b"),
        (2025, "string", "2025"),
        ("2025", "int", 2025),
        (" -3 ", "int", -3),
        ("1.5", "float", 1.5),
        (2, "float", 2.0),
        ("2026-04-30", "date", date(2026, 4, 30)),
        (date(2026, 4, 30), "date", date(2026, 4, 30)),
        ("Yes", "bool", True),
        ("0", "bool", False),
        (None, "int", None),
    ],
)
def test_coerce_ok(value: object, datatype: str, expected: object) -> None:
    assert coerce(value, datatype) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value,datatype",
    [
        (True, "int"), (1.5, "int"), ("1.5", "int"), ("x", "float"), (True, "float"),
        ("2026-02-30", "date"), ("30 April 2026", "date"), ("maybe", "bool"), ("x", "unknown"),
    ],
)
def test_coerce_rejects(value: object, datatype: str) -> None:
    with pytest.raises(_CoercionError):
        coerce(value, datatype)  # type: ignore[arg-type]
