# Document ingestion (PDF, Excel)

Turns uploaded documents into candidate facts with page/row-anchored evidence.
Code: `src/writer_service/ingestion/documents.py`. [ADR-0007](adr/0007-document-ingestion.md).

```
PDF / XLSX ──► chunks (one per page / row, with a locator)
           ──► windows (≤ 12k chars, chunks never split)
           ──► LLM extraction per window (schema = published ontology vocabulary)
           ──► candidates + evidence (source, locator, verbatim quote)
           ──► self-consistency, gates, scoring … (pipeline, issue #10)
```

## Parsing

| Source | Chunk | Locator |
|---|---|---|
| PDF | each non-empty page (`pypdf` text) | `page 3` |
| Excel | each non-empty data row, rendered `header: value; …` | `sheet Checklist row 5` (as Excel numbers rows, header = row 1) |

Unreadable input raises a clear `ValueError` (`… is not a readable PDF`).

## Extraction

The prompt (`document_extraction` v1) contains the ontology rendered as text, for example:

```
- Issuer (key: ticker; properties: ticker*, name*, sector, board, country) is-a Organisation
- (Filing)-[:FILED_BY]->(Issuer) many_to_one
```

followed by the window text with `[page N]` markers. The model must cite the marker and a
verbatim quote for every fact.

**The vocabulary is enforced, not just requested.** The structured-output schema is generated
from the published ontology: `class_name`, `relation` and property names are JSON-schema
**enums** of the active (non-deprecated) names. The model can't return `"Company"`, and if a
provider ignored the schema, validation would reject the answer and retry.

Evidence citing a locator that doesn't exist in the window (`page 99`) is dropped; the fact
keeps its other evidence, and grounding scores it accordingly. Property values come back as
strings; the schema gate coerces them to the declared types.

Each window is one LLM call per self-consistency sample (`sample=0..N-1`), cached like every
other call ([LLM layer](llm.md)).

## Upload endpoint

`POST /ingest/documents` is wired up with the writer pipeline (issue #10), because it needs the
pipeline to run the extracted candidates through verification, gates and scoring.
