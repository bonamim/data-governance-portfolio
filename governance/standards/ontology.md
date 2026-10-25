# Standard: business terms, data elements and governed tags — what goes where, and who owns it

Portable governance standard (the **WHAT**). It says where each kind of meaning is defined, how it
reaches each asset, who maintains it, and the steps a domain team follows. The platform enforces
the machine-checkable parts (`make check-ontology`, CI); the decision record is
[ADR-0027](../../docs/adr/0027-business-ontology.md).

## 1. Four layers — not to be confused

A business term is not the spelled-out name of a column. "LTV means Lifetime Value" translates an
acronym; the business term says what the business counts as value, the rule or formula, who is
responsible, and what it is used for.

| Layer | Answers | Example | Defined in | Maintained by |
|---|---|---|---|---|
| **Business term** | What does the business mean, by what rule, for what use? | *Customer* (a person, counted by person id, never by key); *Amount paid* vs *Items value* | the ontology, `governance/ontology/commerce.ontology.yaml` (Apache Ossie): an entity type, or a value type with a business `description` | the domain responsible for it (owner of the entity type); the ontology's structure by the governance guild |
| **Metric** — a business term with a formula | How is it calculated? | `valid_order_count` (orders not canceled or unavailable); an LTV would live here | the domain's Ossie semantic model, `domains/<d>/semantics/*.semantic_model.yaml` — the formula is executable SQL | the domain |
| **Data element** | What does a column holding this value contain — format, type, sensitivity? | *StateCode* (two-letter state code), *OrderId* | `domains/<d>/contracts/definitions/<term>.odcs.yaml` (ODCS property) | the domain that owns the value |
| **Reference data** | Which values exist / are allowed? | the Brazilian states and zip-code prefixes | a kind of data any domain can own — here the geolocation data of the **reference domain** | the owning domain |

A data element's `description` is the column documentation every column holding it inherits (the
dbt docs, the warehouse comment, the catalog's column description) — so it is kept, and it describes
the column, not the business. Its `logicalType`, `classification` and `privacyCategory` are inherited
the same way. The business definition of a term is in the ontology only.

## 2. How a term reaches each asset — the links

Every step is an explicit link in git; nothing depends on a file or column name.

| Asset | Link chain | What the catalog shows |
|---|---|---|
| **Column** | the contract column → `businessDefinition` → its **data element** → `ontology` → the **value type** it holds → (Ossie) the **entity type** that declares it | the inherited description; *Data element* (`OrderId`); the **business term** when the value type has a business definition (`review_score` → *Review score*) or identifies / refers to an entity (`order_id` → *Order*); the governed tags |
| **Table, view or file** | the contract's schema object → `ontology` → the **entity type** one row is (`orders` → `…commerce.ontology.yaml#Order`); an aggregate of several entities (`sales_performance`, one row per state) has none | the entity's business term on the dataset |
| **Metric / dimension** | a dimension that is a contract column is described by that column — the compiled model every consumer reads carries the column's description and synonyms (`ossie.compile`); a derived field and a metric carry their own description and formula; the **mapping** (generated, `make ossie-build`) binds the semantic model's datasets to the ontology | the metric in BI and to the AI agent, with the same SQL |
| **Data product** | its definition (`data_products/<name>.yaml`) and its output ports, which carry the above | the data product, its domain, owner and ports |
| **Chart, dashboard** | none of its own: it reads datasets | the business terms of the datasets upstream, through lineage |

Worked example — `customer_experience.reviews.order_id`:

```yaml
# domains/customer_experience/contracts/reviews.odcs.yaml (the column)
- name: order_id
  authoritativeDefinitions:
    - type: businessDefinition            # → the data element
      url: ../../sales/contracts/definitions/order_id.odcs.yaml
# domains/sales/contracts/definitions/order_id.odcs.yaml (the data element, owned by sales)
description: Identifies one order on the marketplace; the same value in every domain.
classification: internal
authoritativeDefinitions:
  - type: ontology                        # → the value type
    url: ../../../../governance/ontology/commerce.ontology.yaml#OrderId
# governance/ontology/commerce.ontology.yaml: Order identify_by has_order_id → OrderId
```

So the column is described once (by sales), is `internal`, holds `OrderId`, and refers to an
*Order* — the business term the catalog attaches to it.

## 3. Governed tags — sensitivity and privacy, as Snowflake names them

| Tag | Warehouse tag (Snowflake-provided) | Values | Where the value lives |
|---|---|---|---|
| `sensitivity` | `SNOWFLAKE.TAGS.SENSITIVITY` | restricted, confidential, internal, public | ODCS `classification` |
| `privacy_category` | `SNOWFLAKE.CORE.PRIVACY_CATEGORY` (also set by Snowflake's sensitive-data classification) | identifier, quasi_identifier, sensitive | ODCS custom property `privacyCategory`; none = not personal data |

They are different questions — a customer's zip-code prefix is *internal* and a *quasi-identifier*;
a seller's is *public* and not personal. Defined in `governance/tags/` (one file per tag, maintained
by the governance guild); usually set on the data element and inherited, overridden on a column
where its context differs. Business terms are never tags: tags carry policy, terms carry meaning.

**Applied by the build, before anyone can read the data.** The tags are Snowflake's, so nothing is
created; the transform sets them on each output port as part of building it — the dbt post-hook
`dgp_dbt.apply_governance_tags` runs right after the model is built (on a write-audit-publish
candidate, before it is promoted), with the values `generate_dbt_ports.py` wrote into the model's dbt
metadata from the resolved contract. Consumers are granted access only after the build, so data never
sits in an untagged object they can read. Masking follows the data element too: the customer person
id's `maskingPolicy` is on its data element, so every column holding it is masked. On a warehouse
without tags (the local Postgres) the post-hook does nothing: the masking views enforce access and
the catalog shows the tags (`dbt run-operation dgp_dbt.governance_tags_preview` prints what Snowflake
would run).

## 4. Reusing meaning is not consuming data

A domain may **link** another domain's data element or term freely — that is reusing a definition
(`customers.customer_state` links the reference domain's `StateCode`). To **read another domain's
data**, it needs that domain's **output port**, like any data product: the reference domain is a
domain like the others, and its geolocation data has no output port yet because nobody consumes it.
Owning a definition and publishing data are separate responsibilities of the same domain.

## 5. Ontology, taxonomy, knowledge graph — in this stack

| Concept | What it is | Here |
|---|---|---|
| **Ontology** | the model of the business: types of things, their attributes, how they relate, the rules | `commerce.ontology.yaml` (Apache Ossie) |
| **Taxonomy** | a controlled list (or tree) of terms to label things with | the governed tags `sensitivity` and `privacy_category`; the glossary's grouping by domain |
| **Business glossary** | the human-readable list of business terms | the ontology's business terms, published in DataHub |
| **Knowledge graph** | the ontology plus the things it describes, connected: assets, columns, terms, domains, owners, lineage | the DataHub metadata graph built from git (terms on tables and columns, data elements, domains, lineage); the Ossie mapping extends it to the data itself, through the semantic models |

## 6. Seeing the ontology

`make ossie-build` writes the complete Ossie documents to `build/ossie/` (the ontology with each
data element's description, the compiled semantic models, the generated mappings). Open them in any
Ossie tool. The
open-source [Ossie Visualizer](https://github.com/blankmeadow/ossie-visualizer) (Apache-2.0, runs
in the browser) reads them as they are — entity types and relationships as a graph, the
definitions, search and one/two-hop focus. It is packaged ready to run:

```bash
make ossie-ui        # builds the bundle, starts the Visualizer on http://127.0.0.1:4178
                     # → Import document → build/ossie/commerce.ontology.yaml
make ossie-ui-down
```

The image (`platform/context/semantics/visualizer/`) is built from a pinned upstream commit whose
source is verified by checksum — the project is young, so an upgrade is a deliberate commit +
checksum change, tested before it lands.

Its interface follows the browser's language and is in Chinese by default on a Chinese-language
browser; switch it to English with the **EN** button (top right) or `?lang=en`. Ossie itself has no
viewer planned (no issue or roadmap item); this is a community tool — read it, do not edit with it.

## 7. Explaining it to business and data product teams

- **Business teams own the business glossary.** A term says what the business means, the rule, and
  what it is used for — *Amount paid* is everything customers paid, freight and interest included;
  *Items value* is the merchandise only. Their domain is the term's owner in the catalog.
- **Data product teams own data elements.** A data element says what a column holds — format, type,
  sensitivity — written once and linked from every column that holds it; nobody retypes a column
  description. Most data elements are not business terms (`StateCode`, `OrderId`); some carry one
  (`PaymentAmount`).
- **Both meet on the column**: a column has exactly one data element, and — when the business has a
  rule about it — the business term too. One column name meaning two things (`total_revenue`) is
  visible because the two columns link different data elements.

## 8. Workflow — adding a column to a contract

1. **Look it up.** Search the business glossary and the *Data element* property in DataHub, or the
   files (`domains/*/contracts/definitions/`, the ontology).
2. **It exists → link the data element.** Add `authoritativeDefinitions: [{type: businessDefinition,
   url: <relative path>}]`; write nothing the data element already says (the check refuses copies).
   Override description, classification or privacy category only where your table changes them.
3. **It does not exist → define it with its owner.** It belongs to the domain that owns the thing it
   describes (the entity type), not to whoever needs it first:
   - you own that entity → add the value type and its relationship to the ontology, and its data
     element (with its `ontology` link) to your `definitions/`, in one pull request (the guild
     reviews the ontology part);
   - if the business has a rule about it, give the value type a business `description` — it becomes
     a business term;
   - another domain owns it → open the pull request to *their* `definitions/`. Being first does not
     make you the owner: `customer_id` reached the customer domain's contract first, but the key is
     issued per order, so sales defines it.
4. **Same name, different meaning → a homonym.** Link each column to the data element it means
   (`total_revenue` → `PaymentAmount` in `sales_performance`, `ItemsValue` in
   `seller_performance`). The check reports homonyms; it refuses a shared name left unlinked.
5. **Changing a definition changes every linked column.** The breaking-change gate (ADR-0019) treats
   it as a change to each contract that links it; the owner coordinates with the domains reusing it.

## 9. One repository or many

Links are relative paths, and every repository keeps the same layout (`domains/<d>/…`). In a
monorepo the paths exist already. With a repository per domain, a domain vendors the definitions it
reuses — read-only, at the owner's path, pinned to the owner's release — exactly as it vendors
upstream dbt manifests for dbt-loom. datacontract-cli and the platform then resolve the links
unchanged, offline; a definition not vendored fails the contract clearly
(`governance/tests/test_ontology_multirepo.py`). The ontology is the governance guild's artefact and
is vendored the same way.
