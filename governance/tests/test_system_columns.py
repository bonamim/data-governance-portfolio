"""The platform's system columns (ADR-0028): defined once, inherited by any contract that names one,
so a port passing one through is documented without writing a word about it."""

import yaml
from _checks import completeness_violations
from dgp_governance import products, registry


def test_the_registry_defines_the_default_columns_and_both_prefixes():
    names = [c["name"] for c in registry.system_columns()]
    assert names == [
        "_system_batch_id",
        "_system_source_path",
        "_system_partition",
        "_system_loaded_at",
    ]
    assert registry.reserved_prefixes() == ("_system_", "_internal_")
    assert all(c.get("description") and c.get("classification") for c in registry.system_columns())


def test_a_port_naming_a_system_column_inherits_its_meaning(tmp_repo):
    contracts = tmp_repo / "domains" / "customer" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "customers.odcs.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": [
                    {
                        "name": "customers",
                        "description": "One row per customer record.",
                        "tags": ["output_port"],
                        "properties": [
                            {
                                "name": "customer_id",
                                "description": "The key.",
                                "classification": "internal",
                            },
                            {"name": "_system_loaded_at", "physicalType": "timestamptz"},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    port = products.output_port("customers")
    (loaded_at,) = [c for c in port.columns if c["name"] == "_system_loaded_at"]
    assert loaded_at["description"].startswith("When the load that wrote the row ran")
    assert loaded_at["classification"] == "internal"
    assert loaded_at["physicalType"] == "timestamptz"  # what the contract says itself stays
    assert completeness_violations(port.schema, "customers") == []
