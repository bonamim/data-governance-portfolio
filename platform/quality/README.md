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

## Reading the contract

The engine reads contracts through `dgp_platform_contracts.odcs` (`platform/core/contracts`),
the platform's one ODCS reader, so a rule a column inherits from a linked definition is enforced
like one it declares.

## Division of labour with datacontract-cli

- **datacontract-cli** validates the *contract itself* (`datacontract lint`) in CI — is the
  ODCS well-formed? This is the `governance/policies/check_contracts.py` gate.
- **This engine** validates *data against the contract* — does the file obey the rules? This is
  the runtime landing gate.

Output-port (table/view) contract execution reuses the same engine against warehouse tables in
later milestones.
