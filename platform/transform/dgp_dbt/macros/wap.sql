{#-
  Shared write-audit-publish logic (ADR-0014). The real implementation lives here in
  the package; each domain project installs it and adds a one-line generate_alias_name
  override that delegates to it (dbt only reads generate_alias_name from the ROOT
  project, never a package, so the thin override is required).

  WAP is opt-in: publish.py passes the opted-in products in `wap_products`. A table
  model in that list is written to `<name>__candidate` so its blocking contractual DQ
  can run before an atomic swap publishes it; everything else builds in place.
-#}
{% macro wap_alias(custom_alias_name, node) %}
    {%- set base = custom_alias_name if custom_alias_name else node.name -%}
    {%- if var("wap", false)
           and node is not none
           and node.name in var("wap_products", [])
           and node.config.materialized == "table" -%}
        {{ return(base ~ "__candidate") }}
    {%- else -%}
        {{ return(base) }}
    {%- endif -%}
{% endmacro %}
