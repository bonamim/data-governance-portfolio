# Standard: shipping a breaking change to an output port

How a domain changes an output port in a way its consumers would notice, without breaking them.
The policy and its reasons are in [ADR-0019](../../docs/adr/0019-breaking-change-management.md);
this is the how-to. The gate it walks through is `make check-breaking` (CI runs it on every PR).

## What counts as breaking

`datacontract breaking` decides, comparing the port's contract with `main`. Removing, renaming or
narrowing a column (a type, `required`, a range) is breaking; adding a column or loosening a rule is
not. A change to a linked data element counts as a change to every contract that links it. A
non-breaking change needs nothing from this page: change the contract, bump the minor or patch
version, and merge.

## Choose the path

Ask one question about **every** reader of the port: *can it change in the same pull request?*

| Reader | Same PR? | Why |
|---|---|---|
| A data product in this repo (a dbt model on `ref('dgp_<domain>', '<port>')`) | **yes** | same repo, same CI, same deploy |
| A Superset dashboard on the port | no | its datasets and charts are changed and released outside the PR |
| Ad-hoc SQL, notebooks, an AI agent's saved queries | no | nobody can find and change them all in one PR |
| An external team or system | no | it ships on its own release train |

- **All readers can → coordinated migration** (below). This is the default and the common case:
  one PR, no versions, the old shape is gone the moment it merges.
- **Any reader can't → versioned port** (below). The new shape ships as a second port beside the
  old one, and the old one is retired after a dated migration window.

Lineage in DataHub and the `consumers:` / `inputs:` of the data product definitions say who reads a
port. A reader that is not declared anywhere (ad-hoc SQL) is the reason to prefer a versioned port
when in doubt about a widely read port.

## Path 1 — coordinated migration

**Example.** The customer domain renames `customers.customer_state` to `state_code`. Its only
reader is `sales.sales_performance`, a dbt model in this repo, so both change in one PR.

**1. Change the contract and bump its major version** — `domains/customer/contracts/customers.odcs.yaml`:

```yaml
version: 2.0.0            # was 1.0.0: a breaking change is a new major
# ...
    properties:
      # ...
      - name: state_code   # was customer_state
        authoritativeDefinitions:
          - type: businessDefinition
            url: ../../reference/contracts/definitions/state_code.odcs.yaml
```

**2. Change the model and regenerate its dbt schema** — `domains/customer/dbt/models/silver/customers.sql`
selects `customer_state as state_code`, then `make dbt-ports` rewrites `_ports.yml` from the
contract.

**3. Migrate every reader in the same PR** — `domains/sales/dbt/models/gold/sales_performance.sql`:

```sql
select customer_id, customer_unique_id, state_code as customer_state
from {{ ref('dgp_customer', 'customers') }}
```

Sales aliases the new name back, so its own port does not change and the break stops here. A
reader that would pass the break on changes its own contract the same way — its own major, its
own consumers.

**4. Get the readers' approval.** The gate now reports:

```
BREAKING CHANGE in output port 'customers' (vs origin/main):
  ...
  Affected consumers: sales
  BLOCKED: v2 still needs approval from: sales. ...
```

The sales team reviews the PR — the change to *their* model is in it — and approves. The approval
workflow records it as `governance/approvals/customers-v2.yaml` (or commit it by hand):

```yaml
port: customers
version: 2
approved_by:
  - team: sales
    user: jane
    at: 2026-10-25
    pr: 42
```

**5. Build it, then merge.** The gate passes (`APPROVED: v2 breaking change signed off by all
affected consumers`). CI's Slim CI plan lists the models the change rebuilds — the port and its
readers; `make slim-ci` builds them for real, which proves every reader works on the new shape.
The deploy rebuilds `customers` and `sales_performance` together; masking, grants and the catalog
follow from the contract. Nobody ever sees a port and a reader that disagree.

Try it: `make prove-p5` removes a column and walks the gate from *blocked* to *approved*, then reverts.

## Path 2 — versioned port

**Example.** The customer domain drops `customers.customer_zip_code_prefix`. A Superset dashboard
maps customers by zip code, and its owners need a few weeks to move it. The old shape has to stay
available until then.

The new major is **its own port**, `customers_v2`, with its own contract, model and data product
definition. `customers` (v1) keeps its contract, model and table untouched. dbt's
`versions: [{v: 1}, {v: 2}]` is deliberately not used: an unpinned `ref()` (dbt-loom's included)
resolves to the latest version, so releasing v2 would silently move every unpinned reader onto
the breaking shape (ADR-0019, *Amendment*).

### Release — one PR, no approval needed

**1. Add the v2 contract** — `domains/customer/contracts/customers_v2.odcs.yaml`, a copy of v1
with the change:

```yaml
version: 2.0.0                       # the major is the suffix: customers_v2 <-> 2.x.y
id: customer.customers_v2
name: Customer - Customers v2
status: active
schema:
  - name: customers_v2
    # ... v1's properties, without customer_zip_code_prefix
```

**2. Deprecate v1 with a sunset date** — `domains/customer/contracts/customers.odcs.yaml`:

```yaml
version: 1.0.0                       # unchanged: v1's interface does not change
status: deprecated                   # was active
customProperties:
  - property: sunsetDate
    value: "2026-12-31"              # the end of the migration window
```

**3. Add the v2 data product definition** — `domains/customer/data_products/customers_v2.yaml`,
a copy of v1's with:

```yaml
name: customers_v2
version_of: customers                # makes it v1's successor
display_name: "Customer Directory (v2)"
output_ports:
  - kind: table
    contract: domains/customer/contracts/customers_v2.odcs.yaml
```

**4. Add the v2 model** — `domains/customer/dbt/models/silver/customers_v2.sql`, v1's model
without the column — then `make dbt-ports`.

The gate passes **without any approval**: no contract a consumer reads has changed
(`customers_v2: new output-port contract (no prior version) — OK`). `make check-port-versions`
notes who still reads v1:

```
notice: customers is deprecated, sunset 2026-12-31 (67 days): still read by sales.sales_performance; successor customers_v2
```

### The migration window

Both versions are built, masked and granted; readers move one at a time, each in its own PR.

- **dbt readers** change `ref('dgp_customer', 'customers')` to `'customers_v2'` and
  `customer.customers` to `customer.customers_v2` in their `inputs:`. Until then every CI run
  prints the notice above, naming them. (v1's sunset is also its dbt `deprecation_date`; dbt's
  own warning does not reliably surface through dbt-loom's cross-project refs.)
- **Everyone else** sees it in DataHub: v1 carries a *Deprecated* banner naming `customers_v2`
  and the date. The dashboard owners repoint their dataset at `customer_masked.customers_v2`.
- **After the sunset**, CI fails until v1 is retired, naming who has not moved:
  `customers: sunset 2026-12-31 has passed — retire it after migrating sales.sales_performance`.
  The window is a deadline, not a suggestion.

### Retire v1 — one PR, the consumers sign off

Delete v1's contract, data product definition and model (`customers.odcs.yaml`,
`data_products/customers.yaml`, `models/silver/customers.sql`). The gate blocks the deletion
while any data product still lists `customer.customers` in its `inputs:`, then asks v1's declared
consumers to sign off: their approval of the PR is recorded, like a major's, as
`governance/approvals/customers-retire.yaml`:

```yaml
port: customers
change: retire v1 (successor customers_v2)
approved_by:
  - team: sales
```

`customers_v2` stays `customers_v2`: renaming it back would be another breaking change.

Try it: `make prove-p19` runs this whole lifecycle on the live platform — release, both versions
served, sunset, a blocked early retirement, migration, approved retirement — and reverts.

## Quick reference

| | Coordinated migration | Versioned port |
|---|---|---|
| When | every reader can change in the same PR | any reader can't (BI, ad-hoc SQL, external) |
| Contract | edited in place, major bumped | new `<port>_v<N>` contract; the old one `deprecated` + `sunsetDate` |
| Model, definition | edited in place | new ones, `version_of: <port>`; the old ones untouched |
| Readers | changed in the same PR | each moves before the sunset, in its own PR |
| Approval | `approvals/<port>-v<N>.yaml`, before merge | none to release; `approvals/<port>-retire.yaml` to retire |
| Checks | `check-breaking` | `check-breaking`, `check-port-versions` |
| Proof | P5 | P19 |
