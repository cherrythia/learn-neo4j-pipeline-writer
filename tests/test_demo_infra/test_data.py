from demo_env import data


def test_row_counts_match_demo_story() -> None:
    assert len(data.DEPARTMENTS) >= 8
    assert len(data.FLEET) == 24
    assert len(data.INVOICED_HARDWARE) == 5  # laptops, a server and a switch on the invoice
    assert len(data.HARDWARE) == len(data.FLEET) + len(data.INVOICED_HARDWARE)
    assert len(data.SOFTWARE) >= 6
    assert len(data.APPLICATIONS) >= 6
    assert len(data.SOFTWARE_INSTALLS) > len(data.HARDWARE) // 2


def test_primary_keys_unique() -> None:
    for rows, key in [
        (data.DEPARTMENTS, "dept_code"),
        (data.HARDWARE, "asset_tag"),
        (data.HARDWARE, "serial_number"),
        (data.HARDWARE, "name"),
        (data.SOFTWARE, "software_id"),
        (data.APPLICATIONS, "app_code"),
        (data.SOFTWARE_INSTALLS, "install_id"),
        (data.INVOICED_LICENSES, "asset_tag"),
    ]:
        ids = [getattr(r, key) for r in rows]
        assert len(ids) == len(set(ids)), key


def test_department_names_are_unique() -> None:
    """Dataset 1 (departments) is synced in the demo: duplicate-looking names would be routed
    to review as possible duplicates instead of committing."""
    names = [d.name.lower() for d in data.DEPARTMENTS]
    assert len(names) == len(set(names))


def test_foreign_keys_resolve() -> None:
    depts = {d.dept_code for d in data.DEPARTMENTS}
    software = {s.software_id for s in data.SOFTWARE}
    tags = {a.asset_tag for a in data.HARDWARE}
    assert all(a.dept_code in depts for a in data.HARDWARE)
    assert all(a.owner_dept in depts for a in data.APPLICATIONS)
    assert all(i.software_id in software and i.asset_tag in tags for i in data.SOFTWARE_INSTALLS)
    assert all(x.software_id in software and x.dept_code in depts for x in data.INVOICED_LICENSES)


def test_asset_tags_never_collide_between_hardware_and_licences() -> None:
    """HardwareAsset and SoftwareLicense share the Asset identity key (asset_tag)."""
    hw = {a.asset_tag for a in data.HARDWARE}
    assert not hw & {x.asset_tag for x in data.INVOICED_LICENSES}


def test_enumerations_are_closed() -> None:
    assert {a.hardware_type for a in data.HARDWARE} == {"laptop", "desktop", "server", "network"}
    assert {a.status for a in data.HARDWARE} <= {"in_stock", "in_use", "in_repair", "retired"}
    assert {a.criticality for a in data.APPLICATIONS} <= {"low", "medium", "high"}


def test_invoiced_hardware_was_bought_on_the_invoice_date() -> None:
    for a in data.INVOICED_HARDWARE:
        assert a.purchase_date == data.INVOICE_DATE
        assert a.status == "in_use"
    assert all(a.purchase_date < data.INVOICE_DATE for a in data.FLEET)


def test_invoice_total_adds_up() -> None:
    lines = sum(a.cost for a in data.INVOICED_HARDWARE) + sum(
        x.seats * x.unit_price for x in data.INVOICED_LICENSES
    )
    assert data.invoice_subtotal() == lines
    assert data.invoice_total() == round(lines * (1 + data.GST_RATE), 2)


def test_data_is_fictional_marker_present() -> None:
    # every company-like name carries the demo suffix so nobody mistakes it for a real one
    assert data.VENDOR_NAME.endswith("(Demo)") and data.CUSTOMER_NAME.endswith("(Demo)")
    assert all(a.manufacturer.endswith("(Demo)") for a in data.HARDWARE)
    assert all(s.publisher.endswith("(Demo)") for s in data.SOFTWARE)


def test_some_assets_are_retired_or_in_repair() -> None:
    retired = [a for a in data.HARDWARE if a.status == "retired"]
    assert 1 <= len(retired) < len(data.HARDWARE)
    assert any(a.status == "in_repair" for a in data.HARDWARE)
    # retired devices have nothing installed any more
    retired_tags = {a.asset_tag for a in retired}
    assert not any(i.asset_tag in retired_tags for i in data.SOFTWARE_INSTALLS)


def test_infrastructure_sits_with_it_services() -> None:
    for a in data.HARDWARE:
        if a.hardware_type in ("server", "network"):
            assert a.dept_code == "ITS", a.asset_tag


def test_names_synced_from_the_cmdb_never_look_like_duplicates() -> None:
    """Structured rows must commit with zero review: no two asset hostnames, departments,
    software or applications may be close enough for the matcher to flag or doubt them."""
    from writer_service.pipeline.matching import AMBIGUITY_FLOOR, name_similarity

    for names in (
        [a.name for a in data.HARDWARE],
        [d.name for d in data.DEPARTMENTS],
        [s.name for s in data.SOFTWARE],
        [a.name for a in data.APPLICATIONS],
    ):
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                assert name_similarity(a, b) < AMBIGUITY_FLOOR, (a, b)
