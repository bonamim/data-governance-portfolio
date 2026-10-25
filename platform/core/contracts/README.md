# platform/core/contracts (`dgp-platform-contracts`)

The platform's one reader of ODCS contracts, `dgp_platform_contracts.odcs`. It sits in the core
because every pillar reads contracts: access (masking, grants), quality (the landing gate),
context (catalog, semantics) and the rest of the core (dbt sync, ingestion).

Every platform reader of an ODCS contract goes through `odcs.load(path)`: masking and grants,
the catalog generator, dbt sync (`platform/core/transform/generate_dbt_ports.py`), the
semantic MCP, the governance checks and this engine. It returns the contract with its **linked
definitions inlined**: a property with `authoritativeDefinitions: [{type: businessDefinition, url: <file>}]` (or the
CLI's `semantics`/`definition`) inherits every element it leaves unset (description, classification,
logicalType, tags, customProperties such as `maskingPolicy`, …) from that file, and what the
contract sets itself wins. So a meaning defined once reaches every reader, and a column that
inherits PII is masked like one that declares it.

- **Same rules as datacontract-cli's resolver.** A conformance test runs both on the same files
  and on every contract in the repo.
- **Links are files in git** (a definition file, or `<contract>#schema/<object>/properties/<p>`).
  A URL is refused rather than fetched, so a contract resolves offline and the same everywhere.
  A link that does not resolve rejects the contract.
- **dbt sync gets resolved copies.** The CLI's `dbt sync` reads a contract as written.
- **The breaking-change gate resolves both sides** (the base side against the definitions as they
  were on the base branch), and treats a changed definition as a change to every contract
  linking to it (`odcs.linked_files`).
- `governance/tests/test_contract_reads.py` fails if a new reader skips the loader.
- The definitions columns link to are the ontology's value types, each kept by the domain that owns
  it (`domains/<d>/contracts/definitions/`, ADR-0027); `make check-ontology` keeps each column's
  meaning in one place.
