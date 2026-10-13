{#-
  Publish side of write-audit-publish (ADR-0014/0015), run via `dbt run-operation` so the
  swap SQL lives IN the dbt project — not an external script. The *audit* already happened:
  `dbt build --vars '{wap: true, wap_products: [...]}'` built each opted-in table port to
  `<name>__candidate` and ran its blocking contract DQ. These macros do the PUBLISH /
  QUARANTINE step.

  On Postgres the promote is a dependency-safe rename; on a warehouse the same step maps to
  `ALTER SCHEMA … SWAP` / `dbt clone`, on a lakehouse to an Iceberg branch fast-forward
  (ADR-0015). Keeping it here keeps that portable and version-controlled.
-#}

{#- PUBLISH: atomically swap each candidate into its published name. -#}
{% macro wap_publish(domain, products=[]) %}
  {%- if not execute -%}{{ return('') }}{%- endif -%}

  {%- set find_candidates -%}
    select schemaname, tablename
    from pg_tables
    where schemaname like '{{ domain }}\_%'
      and tablename like '%\_\_candidate'
  {%- endset -%}
  {%- set candidates = run_query(find_candidates).rows -%}

  {%- set cand_tables = [] -%}
  {%- for c in candidates -%}{%- do cand_tables.append(c[1]) -%}{%- endfor -%}

  {#- Guard (ADR-0014/0015): every opted-in product must have produced a candidate; if not,
      its materialization is not a supported WAP target — fail loudly, never silently skip. -#}
  {%- for p in products -%}
    {%- if (p ~ '__candidate') not in cand_tables -%}
      {{ exceptions.raise_compiler_error(
          domain ~ "." ~ p ~ " set publish.audit=required but no candidate was built — only "
          ~ "full-table output ports support write-audit-publish today (ADR-0014/0015).") }}
    {%- endif -%}
  {%- endfor -%}

  {%- for c in candidates -%}
    {%- set schema = c[0] -%}
    {%- set cand = c[1] -%}
    {%- set port = cand | replace('__candidate', '') -%}

    {#- Refuse to swap if a view/matview depends on the port: a DROP … CASCADE would silently
        destroy it and its grants. Rebuilding dependents is the per-model promote's job. -#}
    {%- set find_dependents -%}
      select n.nspname || '.' || v.relname
      from pg_depend d
      join pg_rewrite rw on rw.oid = d.objid
      join pg_class v on v.oid = rw.ev_class
      join pg_namespace n on n.oid = v.relnamespace
      where d.refobjid = to_regclass('{{ schema }}.{{ port }}')
        and v.oid <> to_regclass('{{ schema }}.{{ port }}')
    {%- endset -%}
    {%- set deps = run_query(find_dependents).rows -%}
    {%- if deps | length > 0 -%}
      {%- set dep_names = [] -%}
      {%- for dr in deps -%}{%- do dep_names.append(dr[0]) -%}{%- endfor -%}
      {{ exceptions.raise_compiler_error(
          "cannot swap " ~ schema ~ "." ~ port ~ ": dependent object(s) " ~ (dep_names | join(', '))
          ~ " would be lost — use the per-model portable promote (ADR-0015).") }}
    {%- endif -%}

    {#- RESTRICT (not CASCADE): the pre-check above is authoritative; this is defence in depth. -#}
    {%- do run_query("drop table if exists " ~ schema ~ "." ~ port ~ " restrict") -%}
    {%- do run_query("alter table " ~ schema ~ "." ~ cand ~ " rename to " ~ port) -%}
    {{ log("  published " ~ schema ~ "." ~ port ~ " (audit passed, swapped)", info=True) }}
  {%- endfor -%}
{% endmacro %}


{#- QUARANTINE: the audit failed — drop the candidates, leave consumers on the last good version. -#}
{% macro wap_drop_candidates(domain) %}
  {%- if not execute -%}{{ return('') }}{%- endif -%}
  {%- set find_candidates -%}
    select schemaname, tablename from pg_tables
    where schemaname like '{{ domain }}\_%' and tablename like '%\_\_candidate'
  {%- endset -%}
  {%- for c in run_query(find_candidates).rows -%}
    {%- do run_query("drop table if exists " ~ c[0] ~ "." ~ c[1] ~ " cascade") -%}
  {%- endfor -%}
{% endmacro %}
