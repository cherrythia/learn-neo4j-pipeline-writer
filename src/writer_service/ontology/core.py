"""The core ontology (version 1). Changes after v1 go through drafts and the publish gate.

Taxonomy (``parent``) is subclass-of: an Issuer *is an* Organisation, a Filing *is a*
Document. Each class is self-contained (its own identity key and properties).
"""

from writer_service.ontology.spec import ClassSpec, PropertySpec, RelationSpec

P = PropertySpec

CORE_CLASSES: tuple[ClassSpec, ...] = (
    ClassSpec(
        "Organisation",
        "name",
        (P("name", "string", True), P("country", "string")),
        description="Any legal entity.",
    ),
    ClassSpec(
        "Issuer",
        "ticker",
        (
            P("ticker", "string", True),
            P("name", "string", True),
            P("sector", "string"),
            P("board", "string", description="mainboard or catalist"),
            P("country", "string"),
        ),
        parent="Organisation",
        description="A listed company that must comply with listing rules.",
    ),
    ClassSpec(
        "Document",
        "document_id",
        (P("document_id", "string", True), P("title", "string")),
        description="Any document that can carry evidence.",
    ),
    ClassSpec(
        "Filing",
        "filing_id",
        (
            P("filing_id", "string", True),
            P("filing_type", "string", description="annual_report or esg_report"),
            P("fiscal_year", "int"),
            P("due_on", "date"),
            P("filed_on", "date"),
        ),
        parent="Document",
        description="A report an issuer files with the exchange.",
    ),
    ClassSpec(
        "Rule",
        "rule_code",
        (
            P("rule_code", "string", True),
            P("title", "string"),
            P("text", "string"),
            P("filing_type", "string"),
        ),
        description="A listing-rule clause.",
    ),
    ClassSpec(
        "Obligation",
        "obligation_id",
        (P("obligation_id", "string", True), P("board", "string"), P("deadline_days", "int")),
        description="What a rule requires of a class of issuers, with a deadline.",
    ),
    ClassSpec(
        "Finding",
        "finding_id",
        (P("finding_id", "string", True), P("description", "string"), P("severity", "string")),
        description="An observed compliance issue.",
    ),
)

CORE_RELATIONS: tuple[RelationSpec, ...] = (
    RelationSpec("FILED_BY", "Filing", "Issuer", "many_to_one", description="Who filed it."),
    RelationSpec("DERIVES_FROM", "Obligation", "Rule", "many_to_one"),
    RelationSpec("CITES", "Filing", "Rule", "many_to_many"),
    RelationSpec("SATISFIES", "Filing", "Obligation", "many_to_many"),
    RelationSpec("CONCERNS", "Finding", "Issuer", "many_to_one"),
    RelationSpec("BREACHES", "Finding", "Rule", "many_to_many"),
)
