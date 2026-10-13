{#- dbt reads generate_alias_name only from the ROOT project, never a package, so this
    one-line override delegates to the shared write-audit-publish logic in dgp_dbt. -#}
{% macro generate_alias_name(custom_alias_name=none, node=none) -%}
    {{ return(dgp_dbt.wap_alias(custom_alias_name, node)) }}
{%- endmacro %}
