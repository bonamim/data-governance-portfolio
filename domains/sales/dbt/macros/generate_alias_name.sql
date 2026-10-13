{#-
  Write-audit-publish (ADR-0006, proof P3). When built with `--vars '{wap: true}'`,
  a materialized (table) output port is written to a `<name>__candidate` relation
  instead of its published name. The platform then runs the port's contractual DQ
  against the candidate and only swaps it into place if it passes — so a bad build
  never reaches consumers. View ports and internal models are unaffected.
-#}
{% macro generate_alias_name(custom_alias_name=none, node=none) %}
    {%- set base = custom_alias_name if custom_alias_name else node.name -%}
    {%- if var("wap", false)
           and node is not none
           and node.tags and "output_port" in node.tags
           and node.config.materialized == "table" -%}
        {{ base }}__candidate
    {%- else -%}
        {{ base }}
    {%- endif -%}
{% endmacro %}
