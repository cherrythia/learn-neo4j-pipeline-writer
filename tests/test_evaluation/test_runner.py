import json
from pathlib import Path

import pytest

from writer_service.evaluation.cli import main
from writer_service.evaluation.runner import evaluate
from writer_service.evaluation.scripted import GOLDEN, RECORDINGS, RUNS, record
from writer_service.ingestion.documents import EXTRACTION_PROMPT
from writer_service.llm.cache import InMemoryCache
from writer_service.llm.fake import FakeLLM
from writer_service.llm.structured import StructuredLLM

ROOT = Path(__file__).resolve().parents[2]


def test_self_consistency_repairs_imperfect_runs() -> None:
    llm = StructuredLLM(FakeLLM({EXTRACTION_PROMPT.name: RUNS}), InMemoryCache())
    report = evaluate(llm, ROOT / GOLDEN, 3, ROOT)
    assert report.per_run[0].false_positives == 1  # invented laptop HW-25-0104
    assert report.per_run[1].false_negatives == 3  # missed HW-25-0103, BILLED_ON, ASSIGNED_TO
    assert report.per_run[2].f1 == 1.0
    assert (report.voted.precision, report.voted.recall) == (1.0, 1.0)
    assert report.voted.attribute_accuracy == 1.0
    assert 0.9 < report.agreement < 0.95


def test_scripted_quotes_are_verbatim_in_the_sample_pdf() -> None:
    """The stand-in answers are grounded: every quote is on the page it cites (so the demo's
    document run never rejects them, and the golden set describes the real sample)."""
    from writer_service.evaluation.runner import chunks_for, load_golden
    from writer_service.pipeline.verification import grounding_score

    pages = {c.locator: c.text for c in chunks_for(load_golden(ROOT / GOLDEN), ROOT)}
    for run in RUNS[1:]:  # run 0 adds a hallucination on purpose
        for fact in [*run["entities"], *run["relations"]]:
            for ev in fact["evidence"]:
                assert grounding_score(ev["quote"], pages[ev["locator"]]) == 1.0, ev


def test_committed_recordings_are_up_to_date(tmp_path: Path) -> None:
    record(tmp_path, ROOT)
    fresh = {p.name: p.read_text() for p in tmp_path.glob("*.json")}
    committed = {p.name: p.read_text() for p in (ROOT / RECORDINGS).glob("*.json")}
    assert fresh == committed, "run: uv run python -m writer_service.evaluation.scripted"


def test_cli_replays_recordings_and_enforces_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)
    out = tmp_path / "report.json"
    assert main(["--runs", "3", "--min-f1", "0.95", "--json", str(out)]) == 0
    assert "voted" in capsys.readouterr().out
    (report,) = json.loads(out.read_text())
    assert report["voted"]["f1"] == 1.0 and report["runs"] == 3
    assert main(["--runs", "1", "--min-f1", "0.99"]) == 1  # run 0 alone has a hallucination


def test_replay_miss_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from writer_service.llm.base import LLMReplayMissError

    monkeypatch.chdir(ROOT)
    with pytest.raises(LLMReplayMissError):
        main(["--runs", "4"])  # no recording exists for a 4th sample


def test_live_mode_refuses_a_folder_with_recordings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review round 1: live mode over cached answers would report them as a live run."""
    monkeypatch.chdir(ROOT)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "live", "--recordings", str(RECORDINGS)])
    assert exc.value.code == 2
    assert "needs an empty --recordings folder" in capsys.readouterr().err


def test_live_mode_calls_the_model_into_a_fresh_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM({EXTRACTION_PROMPT.name: RUNS})
    monkeypatch.setattr("writer_service.evaluation.cli.build_llm_client", lambda s: fake)
    monkeypatch.chdir(ROOT)
    live = tmp_path / "live"
    assert main(["--mode", "live", "--runs", "3", "--recordings", str(live)]) == 0
    assert len(fake.calls) == 3 and any(live.iterdir())  # answers really came from the client


def test_scripted_recordings_use_the_default_model() -> None:
    from writer_service.config import Settings
    from writer_service.evaluation.scripted import MODEL

    assert Settings.model_fields["llm_model"].default == MODEL


def test_scripted_answers_pass_the_schema_gate() -> None:
    """Review round 1: values follow the prompt's normalisation (ISO dates, plain numbers,
    enumerated hardware types), so the stand-in facts are not rejected on upload."""
    from writer_service.ingestion.documents import DocumentExtractor, pdf_chunks
    from writer_service.ontology.core import CORE_CLASSES, CORE_RELATIONS
    from writer_service.ontology.spec import OntologySpec
    from writer_service.pipeline.gates import schema_gate

    spec = OntologySpec(CORE_CLASSES, CORE_RELATIONS)
    llm = StructuredLLM(FakeLLM({EXTRACTION_PROMPT.name: RUNS}), InMemoryCache())
    chunks = pdf_chunks(ROOT / "samples/zorblax_invoice_INV-2025-0418.pdf", source_ref="i.pdf")
    candidates = DocumentExtractor(llm, spec, 1).extract(chunks)  # run 0: 31 facts + 1 invented
    assert len(candidates) == 32
    for c in candidates:
        failures, _ = schema_gate(c, spec)
        assert failures == [], (c, failures)
