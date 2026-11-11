import pytest

from writer_service.llm.prompts import PromptTemplate


def test_render_fills_variables() -> None:
    p = PromptTemplate(name="x", version=1, system="You extract {kind}.", user="Text: {text}")
    system, user = p.render(kind="facts", text="hello")
    assert system == "You extract facts." and user == "Text: hello"


def test_missing_variable_is_an_error() -> None:
    p = PromptTemplate(name="x", version=1, system="s", user="{a} {b}")
    with pytest.raises(KeyError, match="b"):
        p.render(a="1")


def test_literal_braces_survive() -> None:
    p = PromptTemplate(name="x", version=1, system="s", user='Return JSON like {{"a": 1}} for {t}')
    assert p.render(t="T")[1] == 'Return JSON like {"a": 1} for T'
