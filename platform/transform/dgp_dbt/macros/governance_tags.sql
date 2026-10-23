{#-
  Governed tags on an output port, set by the build itself (ADR-0027) — so a table never holds data
  without its sensitivity and privacy tags: the post-hook runs right after the model is built (on a
  write-audit-publish candidate, before it is promoted), and the pipeline grants consumers access
  only after the build. The values come from the port's resolved ODCS contract: sync_contracts.py
  writes them into the model's and columns' `meta.governance_tags` as {warehouse tag: value}
  (governance/tags/: SNOWFLAKE.TAGS.SENSITIVITY, SNOWFLAKE.CORE.PRIVACY_CATEGORY).

  Registered once per project as a post-hook; a model without `governance_tags` is left alone.
-#}
{% macro apply_governance_tags() %}
  {{ return(adapter.dispatch('apply_governance_tags', 'dgp_dbt')()) }}
{% endmacro %}

{% macro default__apply_governance_tags() %}
  {#- No native object tags (Postgres): the masking views enforce access, the catalog shows the tags. -#}
  {{ return('') }}
{% endmacro %}

{% macro snowflake__apply_governance_tags() %}
  {% if execute %}
    {% for statement in dgp_dbt.governance_tag_statements(this, model) %}
      {% do run_query(statement) %}
    {% endfor %}
  {% endif %}
  {{ return('') }}
{% endmacro %}

{% macro governance_tag_statements(relation, node) %}
  {#- The Snowflake statements that tag `relation` (a table or view) as `node`'s metadata says. -#}
  {%- set kind = 'view' if node.config.materialized == 'view' else 'table' -%}
  {%- set out = [] -%}
  {%- for tag, value in (node.config.meta.get('governance_tags') or {}).items() -%}
    {%- do out.append('alter ' ~ kind ~ ' ' ~ relation ~ ' set tag ' ~ tag ~ " = '" ~ value ~ "'") -%}
  {%- endfor -%}
  {%- for name, column in node.columns.items() -%}
    {%- set meta = column.get('meta') or (column.get('config') or {}).get('meta') or {} -%}
    {%- for tag, value in (meta.get('governance_tags') or {}).items() -%}
      {%- do out.append('alter ' ~ kind ~ ' ' ~ relation ~ ' modify column ' ~ name ~ ' set tag ' ~ tag ~ " = '" ~ value ~ "'") -%}
    {%- endfor -%}
  {%- endfor -%}
  {{ return(out) }}
{% endmacro %}

{% macro governance_tags_preview(model_name) %}
  {#- `dbt run-operation dgp_dbt.governance_tags_preview --args '{model_name: customers}'` prints the
      statements the build would run on Snowflake, on any warehouse — to review or test them. -#}
  {% for node in graph.nodes.values() if node.resource_type == 'model' and node.name == model_name %}
    {% set relation = api.Relation.create(database=node.database, schema=node.schema, identifier=node.alias) %}
    {% for statement in dgp_dbt.governance_tag_statements(relation, node) %}
      {{ print(statement) }}
    {% endfor %}
  {% endfor %}
{% endmacro %}
