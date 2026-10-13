{#-
  Write-audit-publish (ADR-0014). WAP is OPT-IN per data product: publish.py passes
  the opted-in products in `wap_products`. A table model in that list is written to a
  `<name>__candidate` relation instead of its published name; the platform runs its
  blocking contractual DQ against the candidate and only swaps it into place on pass,
  so a bad build never reaches consumers (proof P3). Everything else publishes in
  place: non-opted-in products, view ports, and internal models.
-#}
{% macro generate_alias_name(custom_alias_name=none, node=none) %}
    {%- set base = custom_alias_name if custom_alias_name else node.name -%}
    {%- if var("wap", false)
           and node is not none
           and node.name in var("wap_products", [])
           and node.config.materialized == "table" -%}
        {{ base }}__candidate
    {%- else -%}
        {{ base }}
    {%- endif -%}
{% endmacro %}
