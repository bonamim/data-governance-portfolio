# ADR-0027: Business ontology: define a column's meaning once, inherit it everywhere

**Status:** Accepted (built, `make check-ontology`). **Relates to** [ADR-0008](0008-monorepo-module-boundaries.md)
(ownership boundaries), [ADR-0010](0010-semantic-layer-ossie.md) (Apache Ossie),
[ADR-0013](0013-role-based-dynamic-masking.md) (masking),
[ADR-0018](0018-catalog-deployment-and-catalog-as-code.md) (catalog as code),
[ADR-0019](0019-breaking-change-management.md) (breaking changes).

## Context
The same column was documented by hand in every contract that carried it, and the copies drifted:

- `customer_id` was described as a "per-order customer key" that "joins to sales.orders" in `customer.customers` and
  as "Customer who placed the order" in `sales.orders`. The second hides that the key is per order,
  which is the trap behind counting keys as people.
- The landing `reviews` contract had no descriptions at all for the columns its output port
  documents. `seller_city`, `geolocation_city` and `customer_city` were three undocumented copies
  of one idea under three names.
- Classification was typed per column too, so a PII column was only as safe as each copy.

People maintaining the contracts asked for one place per definition. A shared "general" contract
was considered and rejected: it would be one more physical-looking contract that every domain
edits, and it says what another column *looks like*, not what a column *means*.

## Three Ossie artefacts, three questions
Apache Ossie separates what we had been calling "semantics" into three documents:

| Artefact | Answers | Here |
|---|---|---|
| **Ontology** (`*.ontology.yaml`) | *What is the business made of, and what does each thing mean?* Customers, orders, reviews and how they relate, independent of any source system, table or report | `governance/ontology/commerce.ontology.yaml` (this ADR) |
| **Semantic model** (`*.semantic_model.yaml`) | *What can people and AI ask?* Datasets, metrics and dimensions over physical tables | `domains/<d>/semantics/` (ADR-0010) |
| **Mapping** (`*.mapping.yaml`) | *Which semantic-model fields populate which concepts?* | not yet, see *Mapping* below |

The ontology is not about the source and not the analytics model: it is the shared vocabulary both
are described in. That is why it is named for the business (`commerce`), not the data source.

## Decision
Model the business as an ontology, and let a contract column **link to the definition of a concept
and inherit its meaning**. The contract keeps what is specific to its table, and the definition holds
what is true of the idea everywhere. Every file and field follows an existing standard.

### Where things live and who maintains them
| Path | Holds | Maintained by | Standard |
|---|---|---|---|
| `governance/ontology/commerce.ontology.yaml` | The **business terms**: entity types and the value types the business has a rule about, each with a business definition (meaning, rule, use), plus relationships, identifiers and IRIs | the governance guild (one representative per domain + platform, CODEOWNERS), a federated artefact | **Apache Ossie** 0.2.0.dev0, checked with **Ossie's own validator** |
| `domains/<d>/contracts/dictionary/<value_type>.odcs.yaml` | The **data elements**, i.e. the data dictionary: what a column holding each value type the domain owns contains (description, classification, logical type + options, synonyms, examples) | **the owning domain** (its folder, its CODEOWNERS) | an **ODCS property**, in datacontract-cli's layout of definitions next to the contracts that use them |
| `domains/reference/contracts/dictionary/` | Shared place data elements (zip-code prefix, city, state). Other domains *link* them to reuse the meaning. *Reading* the reference domain's data needs its output port, like any domain's (none yet) | the reference domain, a domain like the others with its own team | same |
| `governance/tags/sensitivity.yaml`, `privacy_category.yaml` | The governed tags, one file per tag: **sensitivity** (restricted, confidential, internal, public, from ODCS `classification`) and **privacy category** (identifier, quasi_identifier, sensitive, from the custom property `privacyCategory`) | the governance guild | the **Snowflake-provided** tags `SNOWFLAKE.TAGS.SENSITIVITY` and `SNOWFLAKE.CORE.PRIVACY_CATEGORY`, and DataHub tags |
| `dgp_platform_semantics/vendor/ossie/` | Ossie's validator and schemas, copied unchanged at a pinned commit | platform | Apache Ossie |

**Ownership is where a file lives, not a list.** A value type belongs to the domain whose folder
defines it. An entity type belongs to the domain that defines its identifying value types (`Order` →
`OrderId` → sales). The domain registry (`governance/domains/`) stays the one list of domains, and
CODEOWNERS the one list of who approves a folder. There is no second ownership file to keep in step.
Lifecycle approval is the pull request: a definition merged to `main` is approved.

### Conventions (and where each comes from)
| Item | Convention | Source |
|---|---|---|
| Ontology file | `<name>.ontology.yaml`, one document | Ossie (`examples/flights.ontology.yaml`). Its validator requires every referenced concept in the same document |
| Concept names | PascalCase (`CustomerKey`), relationships snake_case (`placed_by`), value types written in block style. Checked by rule 1: a name is an identifier, never a label | Ossie examples |
| Identifiers / attributes | `identify_by`, with `OneToOne` for an identifier and `ManyToOne` for an attribute. Compound identifiers are allowed | Ossie spec (a Review is identified by review id *and* order, as Ossie's Runway is by designator and airport) |
| Value-type base | `extends: [String \| Integer \| Date …]` | Ossie built-in concepts |
| Global identifiers | `iri` with `prefixes` (`schema:Order` = <https://schema.org/Order>): aligns a concept with a shared vocabulary, optional, never fetched | Ossie spec |
| Definition file | `dictionary/<snake_case of the value type>.odcs.yaml`, one file per term, named for the **concept**, not a column (`customer_key`, linked from columns called `customer_id`, and `zip_code_prefix`, linked from `seller_zip_code_prefix`) | datacontract-cli ("a glossary kept as one file per term") |
| The link | `authoritativeDefinitions: [{type: businessDefinition, url: <relative path>}]` | ODCS recommended type `businessDefinition` (not the CLI-specific `semantics`), resolved by datacontract-cli |
| Contract file | `<product>.odcs.yaml` | ODCS tooling |

### The link
```yaml
# domains/customer/contracts/customers.odcs.yaml
- name: customer_id
  authoritativeDefinitions:
    - type: businessDefinition
      url: ../../sales/contracts/dictionary/customer_key.odcs.yaml
  physicalType: text
  required: true
```

The type says *what* the link means, not *which fields* come over: datacontract-cli inlines every
element the definition sets (description, **classification**, logical type,
synonyms, examples) wherever the column leaves it unset, and **what the contract sets itself
wins**. The platform reads every contract the same way through `dgp_platform_contracts.odcs` (its
rules pinned to the CLI's by a conformance test), so masking, grants, the catalog, dbt sync, the
semantic MCP and the policy checks all see the inherited meaning. A column that
inherits its masking policy and an `identifier` privacy category is protected like one that
declares them.

### What goes where
| In the definition (meaning) | On the contract column (this table) |
|---|---|
| description, classification, logicalType (+ options such as a 1 to 5 range), synonyms, examples | name, physicalType, required, unique, primaryKey, quality rules, masking policy (`customProperties`) |
| | an override where context changes the meaning: a customer's zip prefix is `internal` (with city and state it narrows a person down) though the definition says `public` |

A column writes nothing it would only copy: most columns are a name, a link and their physical
facts.

### Homonyms
A homonym (one column name, two meanings) needs no list: each column links the definition it
means, so the difference is written where it applies. The check accepts two definitions under one
name and prints them. What it refuses is a shared name with an unlinked copy.

The real case is revenue. `sales.sales_performance.total_revenue` is what customers **paid**
(payments, including freight and instalment interest) and links `PaymentAmount`.
`sales.seller_performance.total_revenue` is the **value of the items sold** at their price, excluding
freight, and links `ItemsValue`. On the Olist data they are 16,008,872.12 and 13,591,643.70. Freight
alone is 2,251,909.54, so a consumer who reads one for the other is off by 15%. The check prints
`homonym: total_revenue means ItemsValue or PaymentAmount, by contract`, the catalog shows each column's term,
and the semantic MCP tells an agent which meaning a column has.

### Business term, metric, data element, reference data
A business term is not the spelled-out name of a column ("LTV means Lifetime Value"): it is the
business concept, the rule or formula behind it, the area responsible, and what it is used for. Four
layers keep that apart (DAMA-DMBOK's glossary / data dictionary / reference data split):

| Layer | Here | Example |
|---|---|---|
| Business term | the ontology: entity types, and value types with a business `description` | `Customer`, `Order`, `PaymentAmount` vs `ItemsValue`, `CustomerKey` |
| Metric (a business term with a formula) | the domain's Ossie semantic model, where the formula is executable | `valid_order_count` (an LTV would live here) |
| Data element | `domains/<d>/contracts/dictionary/`, linked from every column | `StateCode`, `OrderId` |
| Reference data | the reference domain's data products | the Brazilian states and zip prefixes |

Every column links a data element. Some data elements represent a business term (`PaymentAmount`),
most do not (`StateCode` is a shared data element whose values are reference data). Entity types
must have a business definition (rule 3).

In the catalog the two stay apart. The *Business glossary* holds only business terms (glossary
terms, grouped by the responsible domain, which owns each). A data element is not a glossary term:
it is the *Data element* structured property on the column. Its allowed values are the data
dictionary, each with its definition, and the column's description is the data element's,
inherited. A column carries a business term when its data element is one (`review_score` →
`ReviewScore`) or identifies or refers to an entity (`order_id` → *Order*). A table, view or file
declares the entity one row is with an ODCS `ontology` link on its schema object
(`…commerce.ontology.yaml#Order`) and carries that entity's term. An aggregate of several entities
(`sales_performance`, one row per state) declares none. How each kind of asset gets its meaning
(column, table, metric, data product, chart) is in the standard.

**Each name has one source: the concept's name.** An Ossie concept has no label field, and its
schema allows no extra one, so the PascalCase name (`CustomerKey`, rule 1) is the only name stored.
Its readable form is derived, never written: split at each capital, `CustomerKey` → "Customer Key"
(`dgp_governance.registry.display_name`). A catalog shows the readable form (the DataHub glossary
term and the column's *Data element*, as Snowflake Horizon names a node "Gross Margin"), with the
concept kept as a property. A technical tool (the Ossie bundle, the Visualizer, the AI tools) shows
the concept itself. No file holds a second label: the ODCS `businessName` is optional, and the
check refuses it on a definition or a linked column (rules 4 and 7), so a label cannot drift from
the concept. A concept's `description` is its definition and does not repeat the name.

### Governed tags: sensitivity and privacy, applied by the build
Business terms say what a column *means*. Governed tags say how it must be *protected*, and are
never glossary terms (the split DataHub, Purview and Snowflake make too). The tags follow
Snowflake's own: **sensitivity** is `SNOWFLAKE.TAGS.SENSITIVITY` (restricted, confidential,
internal, public: the ODCS `classification`), and **privacy category** is
`SNOWFLAKE.CORE.PRIVACY_CATEGORY` (identifier, quasi_identifier, sensitive: the ODCS custom
property `privacyCategory`), which Snowflake's sensitive-data classification also sets. Snowflake
keeps them apart, and so do we: the first version's single `pii / internal / public` list mixed a
privacy category (`pii`) with sensitivity levels. A customer's zip-code prefix is *internal* and a
*quasi-identifier*. The customer person id is *restricted* and an *identifier*.

Values are usually set on the data element and inherited, and the customer person id's
`maskingPolicy` sits on its data element too, so every column holding it is masked. **The tags are
applied by the build, not by a separate step**. They are Snowflake's, so nothing is created.
`generate_dbt_ports.py` writes each port's resolved values into its dbt metadata, and the shared dbt
post-hook `dgp_dbt.apply_governance_tags` sets them on the table and its columns right after the
model is built, on a write-audit-publish candidate before promotion. Consumers are granted access
only after the build, so data never sits in an untagged object they can read. On Postgres (no tags)
the post-hook is a no-op and the masking views enforce access. The workflow is in
[`governance/standards/ontology.md`](../../governance/standards/ontology.md).

**The raw table a landing file loads into is governed the same way, by the loader.** It has a
contract (the landing one), and it is where restricted data sits unmasked, so a tag can't wait for a
port. As soon as the table exists and before its first row is written, on every load, the loader
writes the resolved contract onto the table: each description as a `COMMENT`, and on Snowflake the same governed tags the build
sets on a port (`dgp_governance.registry.warehouse_tags` is the one answer for both writers).
`dgp-ingest governance-preview --warehouse snowflake` prints the statements on any machine. Before
the first row, not after the load: Snowflake commits DDL on its own, so a table governed after its
rows would be readable ungoverned for a moment. The loader owns the raw table and dbt only reads it,
so a dbt source's YAML stays hand-written: its catalog entity, the one DataHub opens, gets the
column descriptions from the same `COMMENT`s through dbt's catalog, and its table description,
terms and tags from `catalog-govern`.

### Semantic models and the mapping
Ossie links an ontology to a semantic model through a **mapping**: which semantic-model fields
populate which concepts, so a question in business terms ("revenue per Customer") compiles through
the semantic model. Three changes keep each fact in one place:

- **The semantic models follow the current Ossie draft** (`version: 0.2.0.dev0`), checked with
  Ossie's own validator in `check-semantics`.
- **A dimension that is a contract column has no description in the source**: only derived fields
  and metrics carry one. The first models repeated every column's description, and one had already
  drifted (`num_customers`).
- **Consumers read the compiled model, never the source.** Ossie tools (Snowflake's
  `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_OSSIE_YAML`, a viewer, a converter) read a document as it is, and
  Snowflake's own CI/CD guide keeps descriptions in the Ossie YAML. So `ossie.compile` turns a source
  model into a complete one, giving each contract-column field its description and synonyms
  (`ai_context.synonyms`) from the resolved contract. Every consumer reads that: the semantic
  MCP and Superset compile in process (`ossie.load_compiled`), and `make ossie-build` writes the
  compiled models into the deployable bundle. Source → compile → validate → deploy, as for dbt
  ([docs/source-compile-deploy.md](../source-compile-deploy.md)).
- **The mapping is generated, never written** (`make ossie-build`): a dataset's source is an
  output port whose contract names the entity one row is (its `ontology` link) and, per column, the
  data element whose value type it holds. So `orders` maps *Order* by `orders.order_id` and its
  *CustomerKey* by `orders.customer_id`. It is checked against Ossie's mapping schema. An aggregate
  dataset (one row per state) maps no entity. Ossie's cross-document mapping check is still an open
  pull request.

### Rules: `governance/policies/check_ontology.py` (`make check`, CI)
1. One ontology document, passed by Ossie's own validator (schema, unique names, references), with
   concept names in PascalCase and relationship names in snake_case.
2. Value types and definition files match one to one, and none is defined twice.
3. An entity type's identifying value types are defined by one registered domain, its owner, which
   also defines every value type the entity type declares first.
4. A definition is a valid ODCS property carrying meaning only (no physical type, keys,
   `required` or quality), and its `logicalType` agrees with the built-in its value type extends.
   It sets no `businessName`: the readable name is derived from the concept.
5. Every definition is used by a contract column.
6. A column takes its meaning only from a definition: any link datacontract-cli resolves points at a
   `contracts/dictionary/` file, and resolves.
7. One place per fact: a linked column does not restate or blank its definition's description,
   classification, options, synonyms or examples, and sets no `businessName`.
8. A linked column's `logicalType` does not contradict its definition's.
9. A column name used in two or more contracts is linked in all of them (a homonym links two
   definitions).
10. Every entity type has a business definition.
11. A table's `ontology` link names an entity type of the ontology, in a file that exists.
12. Every data element names the value type it holds (`ontology` link, resolving). A governed tag
    read from a custom property (`privacyCategory`) holds one of the tag's values.

### Consumers
- **dbt / DataHub:** dbt sync gets resolved contracts, so inherited descriptions reach dbt docs and
  DataHub. `catalog-govern` publishes the *Business glossary* (grouped by responsible domain, with
  owners and related terms) and the *Data element* property, and gives every contracted asset (each
  output port, each landing file and the raw table it loads into) its entity's term and each column
  its data element, its business term and its governed tags. So a term's page lists every table and
  column that means it, from the landing file to the port.
- **Seeing it:** the exported Ossie documents open in Ossie tools. The open-source Ossie Visualizer
  shows the ontology as a graph with no code of ours (`make ossie-ui`, a pinned build of the
  upstream project, see the standard). A Mermaid
  diagram generated by the platform was tried first and dropped: a tool that reads the standard
  needs no maintenance here.
- **Warehouse:** the dbt build sets the governed tags on a port and the loader on a raw table
  (above). `dbt run-operation dgp_dbt.governance_tags_preview` and `dgp-ingest governance-preview`
  print the Snowflake statements on any warehouse.
- **AI:** `list_data_products` returns each column's concept: columns of different products with
  the same concept mean the same thing whatever they are called (`orders.customer_id` and
  `customers.customer_id` are both `CustomerKey`).
- **Ossie tools:** `make ossie-build` writes the Ossie bundle to `build/ossie/`: the ontology (each
  value type with its description, as Ossie's examples carry it), the compiled semantic models and
  the mappings, referencing each other inside the bundle. Every document is validated with Ossie's
  own validator. It is what gets deployed, written once and published in the standard shape.
- **Breaking changes:** a changed definition changes every contract linking to it. The gate resolves
  both sides and flags those contracts (ADR-0019).

## Scale
| | Small team, small model | Many teams, large model (this repo) |
|---|---|---|
| Ontology | one document | still one document (Ossie requires it to be self-contained), changed by small pull requests that the guild and the affected domain review |
| Definitions | one folder | one folder per owning domain, next to its contracts. A domain changes its meanings without touching anyone else's files |
| Shared concepts | in the ontology owner's folder | in the reference domain (places, codes), the data-mesh home for data many domains share |
| Repositories | one | every repository keeps the same layout. A domain vendors the definitions it reuses (read-only, at the owner's path, pinned to the owner's release), like dbt-loom manifests, so links resolve unchanged and offline (`test_ontology_multirepo.py`) |
| More than one ontology | n/a | one per **independent** universe of discourse (commerce vs. HR vs. finance), each a self-contained Ossie document owned by the guild, related through shared `iri`s. Ossie has no import yet (an annotation for OWL import is an open pull request), so one business is not split into documents that reference each other |
| Checks | `make check-ontology` reads each contract once, with no network and no service | the same |

## Alternatives considered
- **A shared "general" ODCS contract.** Rejected (see Context). The loader still supports a link to
  another contract's column, but rule 6 keeps meaning in definitions.
- **One ontology document per domain.** Rejected: Ossie's validator requires every concept a
  relationship names to be in the same document (`Order placed_by Customer` crosses domains), so
  split documents are not valid Ossie. Domains own their *definitions* instead.
- **An ownership/stewardship file.** Rejected: it would repeat the domain registry and CODEOWNERS.
- **Ossie mapping documents as the binding.** They bind concepts to *Ossie semantic model* fields,
  but every landing contract and most ports have no semantic model. The ODCS link binds any contracted
  table in any layer, and mappings are generated from the links where a semantic model exists (see
  *Mapping*).
- **First-arrival ownership** (the first product to need a term maintains it). Rejected: ownership
  follows the thing the term describes. `customer_id` reached the customer domain's contract first,
  but the key is issued per order, so sales defines it. Arrival order would put meanings in the
  wrong hands and move them when products are retired.
- **Classification as glossary terms** (as first built). Rejected: classification is a policy
  label that becomes a warehouse tag, and mixing it into the glossary blurs meaning and policy.
- **Descriptions in the Ossie document** (as Ossie's examples do). That writes each meaning twice,
  so the export puts the definition's description there instead. A value type without a business
  rule has no `description` in the source ontology, and the built one (`build/ossie/`) always has
  it, from the data element.
- **Our own Ossie validation.** Rejected once we found Ossie's: `validation/validate.py` is meant to
  be vendored. Its `ossie validate` CLI is not implemented yet and the Python package is not
  published.
- **DataHub Business Attributes.** Beta, behind a flag, UI-only and one-to-one, which goes against
  catalog-as-code (ADR-0018).
- **Concepts hosted by a vendor (Entropy Data IRIs).** Resolution needs a network call and an API
  key. The loader refuses URLs, so a contract resolves offline and the same everywhere.

## Portability
- **Any architecture.** Nothing depends on data mesh: in a central warehouse "the owning domain" is
  whichever team owns the folder.
- **Snowflake.** The governed tags are Snowflake's own (`SNOWFLAKE.TAGS.SENSITIVITY`,
  `SNOWFLAKE.CORE.PRIVACY_CATEGORY`), set by the dbt build on each column and on the table (its most
  sensitive column's sensitivity), and by the loader on each raw table. The transformer and loader
  roles need Snowflake's tag-apply privileges. The warehouse is then where the tags live: DataHub's
  Snowflake source ingests them (`extract_tags`), and `catalog-govern` stops writing its own on
  warehouse assets (`WAREHOUSE_PLATFORM=snowflake`), so a column never shows them twice. On Postgres
  `catalog-govern` writes the DataHub tags. A landing file always takes them from `catalog-govern`:
  object storage has no column tags. Masking on Snowflake applies
  the data element's `maskingPolicy` to the column, or a tag-based policy keyed on these tags.
  Snowflake Horizon's Business Ontology (private preview) maps onto the same model and is not
  adopted before GA. Snowflake backs OSI/Ossie, which the structure follows.
- **Not tested on Snowflake.** There is no account here. The statements the build and the loader
  would run are rendered and reviewed (`governance_tags_preview`, `dgp-ingest governance-preview`,
  unit-tested). The loader itself loads Postgres only, so on Snowflake it is its governance step
  that is ready, not the load.
- **Ossie maturity.** The ontology spec is a 0.2.0.dev0 draft. The validator and schemas are
  vendored at a pinned commit, so a draft change is an explicit upgrade.

## Consequences
- A column that means an existing concept is a link, not a description. A new concept is a value
  type and a relationship in the ontology plus a definition in the owning domain's folder,
  enforced together by the check.
- Ossie's validator also covers semantic models: run on ours it reports they declare an older spec
  version than the vendored draft, to align when the Superset/Ossie integration is upgraded.
- Definitions carry no quality rules, so a rule stays per table. A meaning-level constraint that
  must hold everywhere belongs in `logicalTypeOptions` (the 1 to 5 review score), which the gates run.
