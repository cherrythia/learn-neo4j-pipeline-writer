"""The core ontology (version 1): IT asset management (ITAM). Changes after v1 go through drafts
and the publish gate.

Taxonomy (``parent``) is subclass-of: a HardwareAsset *is an* Asset, a SoftwareLicense *is an*
Asset. Each class is self-contained (its own identity key and properties), so subclasses repeat
the Asset properties they share.
"""

from writer_service.ontology.spec import ClassSpec, PropertySpec, RelationSpec

P = PropertySpec

_ASSET_PROPS: tuple[PropertySpec, ...] = (
    P("asset_tag", "string", True),
    P("name", "string"),
    P("status", "string", description="one of: in_stock, in_use, in_repair, retired"),
    P("purchase_date", "date"),
    P(
        "cost",
        "float",
        description=(
            "purchase cost of the asset (for a licence: the line total), in the document's currency"
        ),
    ),
)

CORE_CLASSES: tuple[ClassSpec, ...] = (
    ClassSpec(
        "Asset",
        "asset_tag",
        _ASSET_PROPS,
        description="Anything the organisation owns and tracks under an asset tag.",
    ),
    ClassSpec(
        "HardwareAsset",
        "asset_tag",
        (
            *_ASSET_PROPS,
            P("hardware_type", "string", description="one of: laptop, desktop, server, network"),
            P("manufacturer", "string"),
            P("model", "string"),
            P("serial_number", "string"),
        ),
        parent="Asset",
        description="A physical device: laptop, desktop, server or network equipment.",
    ),
    ClassSpec(
        "SoftwareLicense",
        "asset_tag",
        (
            *_ASSET_PROPS,
            P("license_type", "string", description="e.g. subscription, perpetual"),
            P("seats", "int"),
            P("expiry_date", "date"),
        ),
        parent="Asset",
        description="An entitlement to use a software product.",
    ),
    ClassSpec(
        "Software",
        "software_id",
        (
            P("software_id", "string", True),
            P("name", "string", True),
            P("publisher", "string"),
            P("version", "string"),
        ),
        description="A software product.",
    ),
    ClassSpec(
        "Application",
        "app_code",
        (
            P("app_code", "string", True),
            P("name", "string", True),
            P("criticality", "string", description="one of: low, medium, high"),
            P("status", "string"),
        ),
        description="A business application the organisation runs.",
    ),
    ClassSpec(
        "Department",
        "dept_code",
        (P("dept_code", "string", True), P("name", "string", True)),
        description="An organisational unit that holds assets and owns applications.",
    ),
    ClassSpec(
        "Vendor",
        "name",
        (P("name", "string", True), P("country", "string")),
        description="A supplier of hardware, software or services.",
    ),
    ClassSpec(
        "Invoice",
        "invoice_number",
        (
            P("invoice_number", "string", True),
            P("invoice_date", "date"),
            P("total_amount", "float"),
            P("currency", "string", description="ISO 4217 code, e.g. SGD"),
        ),
        description="A vendor invoice that bills assets.",
    ),
)

CORE_RELATIONS: tuple[RelationSpec, ...] = (
    RelationSpec(
        "ASSIGNED_TO",
        "HardwareAsset",
        "Department",
        "many_to_one",
        description="The department that holds the device.",
    ),
    RelationSpec("INSTALLED_ON", "Software", "HardwareAsset", "many_to_many"),
    RelationSpec(
        "LICENSES",
        "SoftwareLicense",
        "Software",
        "many_to_one",
        description="The product the licence entitles.",
    ),
    RelationSpec("ALLOCATED_TO", "SoftwareLicense", "Department", "many_to_one"),
    RelationSpec("RUNS_ON", "Application", "HardwareAsset", "many_to_many"),
    RelationSpec("USES", "Application", "Software", "many_to_many"),
    RelationSpec("OWNED_BY", "Application", "Department", "many_to_one"),
    RelationSpec("ISSUED_BY", "Invoice", "Vendor", "many_to_one", description="Who billed it."),
    RelationSpec(
        "BILLED_ON",
        "Asset",
        "Invoice",
        "many_to_one",
        description="The invoice an asset was bought on (any kind of asset).",
    ),
)
