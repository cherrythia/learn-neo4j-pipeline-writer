"""Fictional IT asset management (ITAM) demo data: the single source for the CMDB seed SQL, the
mock Superset datasets and the sample documents (vendor invoice PDF, asset register XLSX)."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from typing import Literal

HardwareType = Literal["laptop", "desktop", "server", "network"]
AssetStatus = Literal["in_stock", "in_use", "in_repair", "retired"]
Criticality = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Department:
    dept_code: str
    name: str


@dataclass(frozen=True)
class HardwareAsset:
    asset_tag: str
    name: str  # hostname
    hardware_type: HardwareType
    manufacturer: str
    model: str
    serial_number: str
    status: AssetStatus
    purchase_date: date
    cost: float
    dept_code: str


@dataclass(frozen=True)
class Software:
    software_id: str
    name: str
    publisher: str
    version: str


@dataclass(frozen=True)
class Application:
    app_code: str
    name: str
    criticality: Criticality
    status: str
    owner_dept: str


@dataclass(frozen=True)
class SoftwareInstall:
    install_id: int
    software_id: str
    asset_tag: str


DEPARTMENTS: list[Department] = [
    Department("ITS", "IT Services"),
    Department("FIN", "Finance"),
    Department("ENG", "Engineering"),
    Department("HRM", "Human Resources"),
    Department("SAL", "Sales"),
    Department("MKT", "Marketing"),
    Department("OPS", "Operations"),
    Department("LEG", "Legal"),
    Department("EXO", "CEO's Office"),
]

# Fictional manufacturers and models, per hardware type.
_MODELS: dict[HardwareType, tuple[str, str, str]] = {
    # type: (manufacturer, model, serial prefix)
    "laptop": ("Corvane (Demo)", "Aero 14", "CVA14"),
    "desktop": ("Corvane (Demo)", "Tower 5", "CVT5"),
    "server": ("Halden Compute (Demo)", "R640X", "HDR"),
    "network": ("Tessel Networks (Demo)", "SW-48", "TSW48"),
}
_PRICES: dict[HardwareType, float] = {
    "laptop": 2150.0,
    "desktop": 1480.0,
    "server": 14800.0,
    "network": 3420.0,
}
_HOST: dict[HardwareType, str] = {"laptop": "lt", "desktop": "dt", "server": "sv", "network": "nw"}


def _fleet_asset(i: int) -> HardwareAsset:
    """The existing fleet, bought in 2023-2024 (deterministic, no randomness)."""
    kinds: list[HardwareType] = ["laptop", "laptop", "desktop", "laptop", "server", "network"]
    kind = kinds[i % len(kinds)]
    dept = DEPARTMENTS[i % len(DEPARTMENTS)].dept_code
    if kind in ("server", "network"):
        dept = "ITS"  # infrastructure sits with IT
    maker, model, prefix = _MODELS[kind]
    year = 23 if i < 12 else 24
    tag = f"HW-{year}-{i + 1:04d}"
    status: AssetStatus = "retired" if i in (2, 9, 15) else "in_repair" if i == 7 else "in_use"
    return HardwareAsset(
        asset_tag=tag,
        name=f"{dept.lower()}-{_HOST[kind]}-{year}{i + 1:02d}",
        hardware_type=kind,
        manufacturer=maker,
        model=model,
        serial_number=f"{prefix}-{(i * 7919) % 100000:05d}{chr(65 + i % 26)}",
        status=status,
        purchase_date=date(2000 + year, 1 + (i * 5) % 12, 1 + (i * 11) % 28),
        cost=_PRICES[kind],
        dept_code=dept,
    )


FLEET: list[HardwareAsset] = [_fleet_asset(i) for i in range(24)]

# --- The vendor invoice (samples/meridian_invoice_INV-2025-0418.pdf) -------------------------

VENDOR_NAME = "Meridian Systems Supply (Demo)"
VENDOR_COUNTRY = "Singapore"
CUSTOMER_NAME = "Larkspur Analytics (Demo)"
INVOICE_NUMBER = "INV-2025-0418"
INVOICE_DATE = date(2025, 11, 3)
CURRENCY = "SGD"
GST_RATE = 0.09


def _bought(
    tag: str, kind: HardwareType, model: str, serial: str, cost: float, dept: str
) -> HardwareAsset:
    maker = _MODELS[kind][0]
    return HardwareAsset(
        asset_tag=tag,
        name=f"{dept.lower()}-{_HOST[kind]}-25{tag[-2:]}",
        hardware_type=kind,
        manufacturer=maker,
        model=model,
        serial_number=serial,
        status="in_use",
        purchase_date=INVOICE_DATE,
        cost=cost,
        dept_code=dept,
    )


INVOICED_HARDWARE: list[HardwareAsset] = [
    _bought("HW-25-0101", "laptop", "Aero 14", "CVA14-8F2K91", 2150.0, "FIN"),
    _bought("HW-25-0102", "laptop", "Aero 14", "CVA14-8F2K94", 2150.0, "ENG"),
    _bought("HW-25-0103", "laptop", "Aero 16", "CVA16-3M7Q05", 2890.0, "ENG"),
    _bought("HW-25-0110", "server", "R640X", "HDR-77120-A", 14800.0, "ITS"),
    _bought("HW-25-0120", "network", "SW-48", "TSW48-00931", 3420.0, "ITS"),
]

HARDWARE: list[HardwareAsset] = FLEET + INVOICED_HARDWARE

SOFTWARE: list[Software] = [
    Software("SW-QDOC", "Quillstack Docs", "Quillstack (Demo)", "2025.2"),
    Software("SW-LEDG", "Ledgerline Accounting", "Ledgerline (Demo)", "11.4"),
    Software("SW-PGDB", "PolarGrid Database", "PolarGrid (Demo)", "16.3"),
    Software("SW-ORBT", "Orbit Endpoint Protection", "Orbit Security (Demo)", "8.1"),
    Software("SW-CNVS", "Canvasly Design", "Canvasly (Demo)", "4.0"),
    Software("SW-TKTR", "Tickety Service Desk", "Tickety (Demo)", "3.7"),
]


@dataclass(frozen=True)
class LicenseLine:
    """A software licence billed on the invoice (becomes a SoftwareLicense asset)."""

    asset_tag: str
    software_id: str
    license_type: str
    seats: int
    unit_price: float
    expiry_date: date
    dept_code: str

    @property
    def amount(self) -> float:
        return self.seats * self.unit_price


INVOICED_LICENSES: list[LicenseLine] = [
    LicenseLine("SL-25-0201", "SW-QDOC", "subscription", 50, 180.0, date(2026, 10, 31), "ENG"),
    LicenseLine("SL-25-0202", "SW-LEDG", "subscription", 10, 640.0, date(2026, 10, 31), "FIN"),
]


def invoice_subtotal() -> float:
    return sum(a.cost for a in INVOICED_HARDWARE) + sum(x.amount for x in INVOICED_LICENSES)


def invoice_total() -> float:
    return round(invoice_subtotal() * (1 + GST_RATE), 2)


def software_by_id(software_id: str) -> Software:
    return next(s for s in SOFTWARE if s.software_id == software_id)


def department_by_code(dept_code: str) -> Department:
    return next(d for d in DEPARTMENTS if d.dept_code == dept_code)


APPLICATIONS: list[Application] = [
    Application("APP-ERP", "Finance ERP", "high", "live", "FIN"),
    Application("APP-HRP", "HR Self-Service Portal", "medium", "live", "HRM"),
    Application("APP-CRM", "Sales CRM", "high", "live", "SAL"),
    Application("APP-SDK", "IT Service Desk", "medium", "live", "ITS"),
    Application("APP-WEB", "Public Website", "high", "live", "MKT"),
    Application("APP-BIW", "Analytics Warehouse", "medium", "pilot", "ENG"),
    Application("APP-LGL", "Contract Register", "low", "retiring", "LEG"),
]


def _installs() -> list[SoftwareInstall]:
    pairs: list[tuple[str, str]] = []
    for hw in HARDWARE:
        if hw.status == "retired":
            continue
        if hw.hardware_type in ("laptop", "desktop", "server"):
            pairs.append(("SW-ORBT", hw.asset_tag))  # endpoint protection everywhere
        if hw.hardware_type == "laptop" and hw.dept_code == "ENG":
            pairs.append(("SW-QDOC", hw.asset_tag))
        if hw.hardware_type == "laptop" and hw.dept_code == "FIN":
            pairs.append(("SW-LEDG", hw.asset_tag))
        if hw.hardware_type == "laptop" and hw.dept_code == "MKT":
            pairs.append(("SW-CNVS", hw.asset_tag))
        if hw.hardware_type == "server":
            pairs.append(("SW-PGDB", hw.asset_tag))
    servers = [hw.asset_tag for hw in HARDWARE if hw.hardware_type == "server"]
    pairs.append(("SW-TKTR", servers[0]))
    return [SoftwareInstall(i, sw, tag) for i, (sw, tag) in enumerate(pairs, start=1)]


SOFTWARE_INSTALLS: list[SoftwareInstall] = _installs()

# Dataset order = Superset dataset ids (1-based): dataset 1 is the departments table.
TABLES: dict[str, type] = {
    "departments": Department,
    "assets": HardwareAsset,
    "software": Software,
    "applications": Application,
    "software_installs": SoftwareInstall,
}
ROWS: dict[str, list[object]] = {
    "departments": list(DEPARTMENTS),
    "assets": list(HARDWARE),
    "software": list(SOFTWARE),
    "applications": list(APPLICATIONS),
    "software_installs": list(SOFTWARE_INSTALLS),
}


def columns_of(table: str) -> tuple[str, ...]:
    return tuple(f.name for f in fields(TABLES[table]))


# Superset view of the demo tables (used by the mock API and the real-Superset bootstrap).
SCHEMA = "demo_itam"


@dataclass(frozen=True)
class Dataset:
    id: int
    table_name: str


DATASETS: list[Dataset] = [Dataset(i, t) for i, t in enumerate(TABLES, start=1)]
