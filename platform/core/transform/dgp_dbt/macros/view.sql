{#-
  Views rebuilt IN PLACE (P1, ADR-0013). dbt-postgres rebuilds a view by creating a new one,
  swapping it in and dropping the old one with CASCADE, which also drops every view built on it:
  the next layer's views, output ports, the masking views and their grants. Until the next model
  recreates them they are gone, and if that model fails they stay gone (an outage for consumers).

  `CREATE OR REPLACE VIEW` keeps the view's identity, so everything built on it survives. Postgres
  allows it when the existing columns keep their names, types and order (new ones may only be
  appended). The new query's columns are read from a temporary view, so the check is exact. A
  real change of shape falls back to dbt's default swap. That is a breaking change of a port,
  governed by the breaking-change gate (ADR-0019).

  dbt lets only the ROOT project override a built-in materialization, so each project has a
  one-line `view` materialization that delegates here (like the alias and schema macros).
-#}

{% macro _view_columns(relation_sql) %}
  {%- set q -%}
    select attname, format_type(atttypid, atttypmod) from pg_attribute
    where attrelid = {{ relation_sql }} and attnum > 0 and not attisdropped order by attnum
  {%- endset -%}
  {%- set out = [] -%}
  {%- for r in run_query(q).rows -%}{%- do out.append(r[0] ~ ' ' ~ r[1]) -%}{%- endfor -%}
  {{ return(out) }}
{% endmacro %}

{% macro view_replaceable(existing, sql) %}
  {%- set probe = 'dgp_view_probe_' ~ model.name -%}
  {%- do run_query("create temporary view " ~ probe ~ " as " ~ sql) -%}
  {%- set new = dgp_dbt._view_columns("'pg_temp." ~ probe ~ "'::regclass") -%}
  {%- do run_query("drop view pg_temp." ~ probe) -%}
  {%- set old = dgp_dbt._view_columns("'" ~ existing ~ "'::regclass") -%}
  {{ return(new[:old | length] == old) }}
{% endmacro %}

{% macro view_in_place() %}
  {%- set existing = load_cached_relation(this) -%}
  {%- if existing is none or not existing.is_view
        or not dgp_dbt.view_replaceable(existing, sql) -%}
    {{ return(dbt.materialization_view_default()) }}
  {%- endif -%}
  {%- set target = this.incorporate(type='view') -%}
  {%- set grant_config = config.get('grants') -%}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% call statement('main') -%}
    {{ get_replace_view_sql(target, sql) }}
  {%- endcall %}

  {% do apply_grants(target, grant_config, should_revoke=should_revoke(existing, full_refresh_mode=True)) %}
  {% do persist_docs(target, model) %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {{ adapter.commit() }}
  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {{ return({'relations': [target]}) }}
{% endmacro %}
