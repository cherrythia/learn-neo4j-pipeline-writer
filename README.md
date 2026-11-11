# learn-neo4j-pipeline-writer

A learning project: a **writer service** that ingests knowledge from multiple sources
(PDF, Excel, Superset/MySQL), runs every candidate fact through an LLM-assisted
**verification and scoring pipeline**, and only then writes accepted facts into **Neo4j**,
following a governed ontology and taxonomy managed from an admin page.

> Work is tracked issue-by-issue in the GitHub issue list. See `CHANGELOG.md` and `docs/`
> as they are added.
