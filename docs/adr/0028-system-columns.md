# ADR-0028: System columns: the platform's and the products', named apart and never blocked

**Status:** Accepted (built, `make check`, proof P12.7). **Relates to**
[ADR-0024](0024-landing-zone-layout-access-and-lifecycle.md) (landing lifecycle),
[ADR-0025](0025-separate-contract-gate-and-raw-load.md) (gate and load),
[ADR-0027](0027-business-ontology.md) (meaning defined once).

## Context
A raw table held exactly the columns its source delivered. Which delivery a row came from, from
where and when, was only in the ingestion ledger, one row per file. That works while each load
replaces raw with one file. It stops working when several files of a partition are appended into
one table, when a source is a stream or an API rather than a file, or when a data product wants
its own technical columns (a row hash, a dedup rank).

Adding such columns used to collide with the contracts in three places. The loader dropped and
recreated any raw table whose columns did not match the file's header, taking the views built on
it with them. A column no contract described was undocumented and untagged, and no gate noticed.
And a column used in several contracts without a linked definition failed the ontology check, so a
port passing `loaded_at` through next to another port doing the same would have been blocked.

## Decision

### 1. Two reserved prefixes, by owner
| Prefix | Owner | Defined in | Added by |
|---|---|---|---|
| `_system_` | the platform | `governance/system_columns.yaml`, once | the loader, on every raw table |
| `_internal_` | a data product | its port contract, like any column | the product's own models |

Who owns a column shows in its name. A product can add any `_internal_*` column without asking
anyone, and it can never collide with a column the platform adds later. No source delivers either
prefix, so `check-contracts` refuses one in a landing contract.

### 2. Every raw table carries the platform's default columns
Landing and OLTP sources alike, after the delivered columns:

| Column | A file | A database table |
|---|---|---|
| `_system_batch_id` | the ledger batch (one file, or a partition's files under a batch completion signal) | one id per snapshot |
| `_system_source_path` | the archived file, `s3://<archive>/<key>` | `postgres://<db>/<schema>.<table>` |
| `_system_partition` | the delivered partition, `dt=YYYY-MM-DD` | the day of the snapshot |
| `_system_loaded_at` | when the load ran, UTC, shared by the whole batch | the same |

`_system_source_path` is a URI so a stream (`kafka://<topic>`) or an API (`https://…`) fits the
same column. `_system_batch_id` is recorded in the ledger too, so any row joins its ledger entries.

### 3. Filled in the one write, governed before it
The loader stamps a file's values on every row in the same COPY (each value is the column's default
for that COPY only), so rows are written once. The table is described and tagged as soon as it
exists and before its first row (ADR-0027), system columns included. A raw table from before this
ADR gains the columns in place on its next load (`ALTER TABLE ADD COLUMN`), never dropped, so the
views on it survive.

### 4. Contracts don't block them
- A `_system_*` column in any contract (a port passing one through) is named, and inherits its
  description and classification from the platform's definition, as a linked column inherits its
  data element. Port completeness, dbt sync and the catalog all see that meaning.
- A `_system_*` name the platform does not define is refused: a product's own column is
  `_internal_*`.
- Both prefixes are exempt from the rule that a column repeated across contracts links a definition
  (ontology rule 10). A platform column means the same everywhere by construction, and a product's
  own is its owner's to repeat.
- An `_internal_*` column in a port is documented like any column: the completeness gate requires
  its description and classification.

## Consequences
- The catalog shows the system columns on every raw table, its dbt source too, with the platform's
  descriptions and tags (`internal`), and never on the landing file, which doesn't have them.
- Every raw table has a contract of what its source holds, an OLTP table's too
  (`contracts/oltp/`), so its business columns are governed like the system ones.
- A new platform column is a pull request to `governance/system_columns.yaml` and the loader. A new
  product column needs no one's approval but its owner's.
- The loader loads Postgres only. The Snowflake types and governance statements are rendered and
  tested, and a Snowflake loader would fill the columns from its own COPY metadata
  (`METADATA$FILENAME`) the same way.
