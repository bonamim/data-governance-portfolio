{#-
  Shared write-audit-publish logic (ADR-0014/0015). The real implementation lives here in
  the package. Each domain project installs it and adds a one-line generate_alias_name
  override that delegates to it (dbt only reads generate_alias_name from the ROOT
  project, never a package, so the thin override is required).

  WAP is opt-in per model: the orchestrator builds one opted-in model at a time with
  `--vars '{wap: true, wap_products: [<model>]}'`. A table or incremental model in that list is
  written to `<name>__candidate` so its blocking contractual DQ runs before `wap_promote`
  publishes it. Everything else builds in place. Views publish in place (nothing to audit
  before a view exists).

  The tests run with `wap_stage: audit` against `<name>__audit`: a view of the table exactly as
  consumers would get it after the promote (for a microbatch window: the published table with
  the window replaced by the new batches).
-#}
{% macro wap_alias(custom_alias_name, node) %}
    {%- set base = custom_alias_name if custom_alias_name else node.name -%}
    {%- if var("wap", false)
           and node is not none
           and node.name in var("wap_products", [])
           and node.config.materialized in ("table", "incremental") -%}
        {{ return(base ~ ("__audit" if var("wap_stage", "") == "audit" else "__candidate")) }}
    {%- else -%}
        {{ return(base) }}
    {%- endif -%}
{% endmacro %}
