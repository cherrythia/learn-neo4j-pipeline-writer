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
| Excel | each non-empty data row, rendered `header: value; …` | `sheet Register row 5` (as Excel numbers rows, header = row 1) |

Unreadable input raises a clear `ValueError` (`… is not a readable PDF`).

## Extraction

The prompt (`document_extraction` v2) contains the ontology rendered as text, for example:

```
- HardwareAsset (key: asset_tag; properties: asset_tag*: string, name: string, status: string (one of: in_stock, in_use, in_repair, retired), purchase_date: date, cost: float (amount in the invoice currency), hardware_type: string (one of: laptop, desktop, server, network), manufacturer: string, model: string, serial_number: string) is-a Asset
- (HardwareAsset)-[:ASSIGNED_TO]->(Department) many_to_one
```

Each property shows its datatype and, from the ontology's property description, its notes or
allowed values (`one of: …`). Followed by the window text with `[page N]` markers. The model
must cite the marker and a verbatim quote for every fact, and give property values
**normalised to their datatype**: dates as `YYYY-MM-DD`, numbers as plain digits without
thousands separators or currency, and one of the listed values where the property has them.
The quote stays verbatim (`1 x 2,150.00`); the value is `2150.00`. The schema gate doesn't
guess these formats itself (`1,234` is a thousand in one locale and a decimal in another), so
an unnormalised value is rejected rather than misread.

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
