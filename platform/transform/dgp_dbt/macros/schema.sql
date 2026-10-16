{#-
  Schema naming (ADR-0012, ADR-0015 §3). dbt's default (<target schema>_<layer>) everywhere,
  except the Slim CI target: there every model of the run lands in the one sandbox schema
  <domain>_ci, and whatever was not changed is read from the published schemas (--defer).
  Delegated to from each project's generate_schema_name, like the alias (root-project rule).
-#}
{% macro schema_name(custom_schema_name, node) %}
    {%- if target.name == "ci" or custom_schema_name is none -%}
        {{ return(target.schema) }}
    {%- else -%}
        {{ return(target.schema ~ "_" ~ (custom_schema_name | trim)) }}
    {%- endif -%}
{% endmacro %}

{#- Empty the Slim CI sandbox before a run, so it holds only this change's models. -#}
{% macro ci_reset() %}
  {%- if not execute or target.name != "ci" -%}{{ return('') }}{%- endif -%}
  {%- set q -%}
    select c.relname, case c.relkind when 'v' then 'view' else 'table' end
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = '{{ target.schema }}' and c.relkind in ('r', 'v', 'p')
  {%- endset -%}
  {%- for r in run_query(q).rows -%}
    {%- do run_query("drop " ~ r[1] ~ " if exists " ~ target.schema ~ "." ~ adapter.quote(r[0])
                     ~ " cascade") -%}
  {%- endfor -%}
{% endmacro %}
