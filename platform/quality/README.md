# platform/quality (`dgp-platform-quality`)

The quality **engine**. It executes the rules declared in an ODCS contract against a dataset
and returns a pass/fail result with per-rule assertions. It is a pure engine (data-mesh.md,
"Where quality lives"): the *rules* come from the contract (the domain), the *mandate* that
certain checks are required comes from governance, and this package only *runs* them.

## Used by the landing-zone gate (proof P2)

A landing file must match its ODCS contract before it is allowed into the warehouse. The
ingestion loader calls this engine per landing file; on failure it **quarantines** the file
(files it to `landing-archive/quarantine/<source>/<dataset>/<partition>/<run_id>/` with a
`_reason.json`), records `status = quarantined` and the per-rule results in the ingestion ledger,
and loads nothing — consumers keep the last good version. The results appear in DataHub as
assertions on the landing dataset (`make catalog-quality`, ADR-0024).

## Checks executed (from the ODCS schema)

| ODCS field | Check |
|---|---|
| property present in `schema[].properties` | the column must exist in the file |
| `required: true` | no null/empty values |
| `unique: true` | no duplicate values |
| `logicalType: integer` | values parse as integers |
| `logicalTypeOptions.minimum` / `maximum` | values within range |

One streaming pass, so a million-row file is fine (validated on real Olist geolocation,
1,000,163 rows).

## Reading a contract: `dgp_platform_quality.odcs`

Every platform reader of an ODCS contract goes through `odcs.load(path)`: masking and grants,
the catalog generator, dbt sync (`platform/transform/sync_contracts.py`), the
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

## Division of labour with datacontract-cli

- **datacontract-cli** validates the *contract itself* (`datacontract lint`) in CI — is the
  ODCS well-formed? This is the `governance/policies/check_contracts.py` gate.
- **This engine** validates *data against the contract* — does the file obey the rules? This is
  the runtime landing gate.

Output-port (table/view) contract execution reuses the same engine against warehouse tables in
later milestones.
