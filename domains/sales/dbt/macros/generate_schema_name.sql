{#- dbt reads generate_schema_name only from the ROOT project, so this one-line override delegates
    to the shared naming in dgp_dbt (one sandbox schema under the Slim CI target). -#}
{% macro generate_schema_name(custom_schema_name=none, node=none) -%}
    {{ return(dgp_dbt.schema_name(custom_schema_name, node)) }}
{%- endmacro %}
