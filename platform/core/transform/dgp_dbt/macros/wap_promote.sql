{#-
  Per-model write-audit-publish (ADR-0015 §2), run via `dbt run-operation` so the promote SQL
  lives IN the dbt project, not in an external script. The orchestrator (publish.py, or the
  Airflow transform DAG) sequences one opted-in model at a time:

    wap_prepare(model)                       stage the candidate
    dbt run --select <model> --vars wap      WRITE <name>__candidate (a window's batches, or
                                             the whole table)
    wap_audit_view(model, window)            <name>__audit = the table consumers would get
    dbt test --select <model> (stage audit)  AUDIT: the contract DQ, against <name>__audit
    pass -> wap_promote(model, window)       PUBLISH
    fail -> wap_drop_candidate(model)        QUARANTINE: consumers keep the last good version

  Promote on Postgres, per materialization:
    * table, or incremental rebuilt in full: if the published table has the same columns, the
      rows are replaced IN PLACE (truncate + insert, one transaction) so the table keeps its
      identity: dependent views, grants and masking survive. A new shape is renamed in, which
      is refused while anything depends on the old table (never DROP ... CASCADE).
    * incremental / microbatch with a window: the candidate holds only the audited batches;
      they are merged into the port (delete the window, insert the batch).
  On a warehouse the same steps map to a zero-copy clone swap / MERGE, on a lakehouse to an
  Iceberg branch fast-forward; the macro names stay the same.
-#}

{% macro _wap_node(model) %}
  {%- for node in graph.nodes.values()
        if node.resource_type == 'model' and node.name == model
           and node.package_name == project_name -%}
    {{ return(node) }}
  {%- endfor -%}
  {{ exceptions.raise_compiler_error("wap: no model named " ~ model ~ " in " ~ project_name) }}
{% endmacro %}

{% macro _wap_relations(node) %}
  {#- run-operation parses without the wap vars, so node.alias is the published name. -#}
  {%- set port = adapter.get_relation(database=node.database, schema=node.schema,
                                      identifier=node.alias) -%}
  {%- set cand = adapter.get_relation(database=node.database, schema=node.schema,
                                      identifier=node.alias ~ '__candidate') -%}
  {{ return({'port': port, 'cand': cand,
             'port_name': node.schema ~ '.' ~ node.alias,
             'cand_name': node.schema ~ '.' ~ node.alias ~ '__candidate',
             'audit_name': node.schema ~ '.' ~ node.alias ~ '__audit'}) }}
{% endmacro %}

{% macro _wap_columns(relation_name) %}
  {%- set q -%}
    select attname, format_type(atttypid, atttypmod)
    from pg_attribute
    where attrelid = to_regclass('{{ relation_name }}') and attnum > 0 and not attisdropped
    order by attnum
  {%- endset -%}
  {%- set cols = [] -%}
  {%- for r in run_query(q).rows -%}{%- do cols.append(r[0] ~ ' ' ~ r[1]) -%}{%- endfor -%}
  {{ return(cols) }}
{% endmacro %}

{% macro _wap_dependents(relation_name) %}
  {%- set q -%}
    select distinct n.nspname || '.' || v.relname
    from pg_depend d
    join pg_rewrite rw on rw.oid = d.objid
    join pg_class v on v.oid = rw.ev_class
    join pg_namespace n on n.oid = v.relnamespace
    where d.refobjid = to_regclass('{{ relation_name }}')
      and v.oid <> to_regclass('{{ relation_name }}')
  {%- endset -%}
  {%- set out = [] -%}
  {%- for r in run_query(q).rows -%}{%- do out.append(r[0]) -%}{%- endfor -%}
  {{ return(out) }}
{% endmacro %}


{#- STAGE: drop any stale candidate; for an incremental model whose published table exists and
    that is not being rebuilt in full, start an EMPTY candidate of the same shape, so dbt runs it
    incrementally and the candidate ends up holding exactly the window's batches. -#}
{% macro wap_prepare(model, full_refresh=false) %}
  {%- if not execute -%}{{ return('') }}{%- endif -%}
  {%- set node = dgp_dbt._wap_node(model) -%}
  {%- set r = dgp_dbt._wap_relations(node) -%}
  {%- do run_query("drop view if exists " ~ r.audit_name) -%}
  {%- do run_query("drop table if exists " ~ r.cand_name) -%}
  {%- if node.config.materialized == 'incremental' and not full_refresh
        and r.port is not none and r.port.is_table -%}
    {%- do run_query("create table " ~ r.cand_name ~ " (like " ~ r.port_name
                     ~ " including defaults)") -%}
    {{ log("  staged empty candidate " ~ r.cand_name ~ " (incremental: batches only)", info=True) }}
  {%- endif -%}
{% endmacro %}


{#- AUDIT: the table as consumers would get it after the promote — the candidate itself, or for a
    batch window the published rows outside the window plus the new batches. A view, so it
    costs nothing; the model's dbt tests (the contract DQ, including table-level rules such as
    row counts and uniqueness) run against it. -#}
{% macro wap_audit_view(model, window_start=none, window_end=none) %}
  {%- if not execute -%}{{ return('') }}{%- endif -%}
  {%- set node = dgp_dbt._wap_node(model) -%}
  {%- set r = dgp_dbt._wap_relations(node) -%}
  {%- set col_list = [] -%}
  {%- for c in dgp_dbt._wap_columns(r.cand_name) -%}
    {%- do col_list.append(adapter.quote(c.split(' ')[0])) -%}
  {%- endfor -%}
  {%- set cols = col_list | join(', ') -%}
  {%- set et = adapter.quote(node.config.event_time) -%}
  {%- if window_start is none -%}
    {%- do run_query("create or replace view " ~ r.audit_name ~ " as select " ~ cols ~ " from "
                     ~ r.cand_name) -%}
    {{ log("  audit view " ~ r.audit_name ~ " = the candidate", info=True) }}
  {%- else -%}
    {%- do run_query("create or replace view " ~ r.audit_name ~ " as select " ~ cols ~ " from "
                     ~ r.port_name ~ " where not (" ~ et ~ " >= '" ~ window_start
                     ~ "'::timestamp and " ~ et ~ " < '" ~ window_end ~ "'::timestamp)"
                     ~ " union all select " ~ cols ~ " from " ~ r.cand_name) -%}
    {{ log("  audit view " ~ r.audit_name ~ " = published outside " ~ window_start ~ " .. "
           ~ window_end ~ " + the new batches", info=True) }}
  {%- endif -%}
{% endmacro %}


{#- PUBLISH: promote the audited candidate. window_start/window_end are the batch window the
    candidate was built for (incremental); without them the candidate replaces the port. -#}
{% macro wap_promote(model, window_start=none, window_end=none) %}
  {%- if not execute -%}{{ return('') }}{%- endif -%}
  {%- set node = dgp_dbt._wap_node(model) -%}
  {%- set r = dgp_dbt._wap_relations(node) -%}
  {%- if r.cand is none -%}
    {{ exceptions.raise_compiler_error(
        r.port_name ~ " opted in to write-audit-publish but no candidate was built — only table "
        ~ "and incremental models support it (ADR-0014/0015).") }}
  {%- endif -%}
  {%- do run_query("drop view if exists " ~ r.audit_name) -%}
  {%- set cand_cols = dgp_dbt._wap_columns(r.cand_name) -%}
  {%- set col_list = [] -%}
  {%- for c in cand_cols -%}{%- do col_list.append(adapter.quote(c.split(' ')[0])) -%}{%- endfor -%}
  {%- set cols = col_list | join(', ') -%}

  {%- if r.port is not none and r.port.is_table and window_start is not none -%}
    {#- MERGE the audited batches: replace the window in the published table. -#}
    {%- if dgp_dbt._wap_columns(r.port_name) != cand_cols -%}
      {{ exceptions.raise_compiler_error(
          r.port_name ~ ": the batch's columns differ from the published table — rebuild it in "
          ~ "full (full_refresh) instead of merging a batch.") }}
    {%- endif -%}
    {%- set et = adapter.quote(node.config.event_time) -%}
    {%- do run_query("delete from " ~ r.port_name ~ " where " ~ et ~ " >= '" ~ window_start
                     ~ "'::timestamp and " ~ et ~ " < '" ~ window_end ~ "'::timestamp") -%}
    {%- do run_query("insert into " ~ r.port_name ~ " (" ~ cols ~ ") select " ~ cols
                     ~ " from " ~ r.cand_name) -%}
    {%- do run_query("drop table " ~ r.cand_name) -%}
    {{ log("  published " ~ r.port_name ~ " (audit passed, merged batches " ~ window_start
           ~ " .. " ~ window_end ~ ")", info=True) }}

  {%- elif r.port is not none and r.port.is_table
           and dgp_dbt._wap_columns(r.port_name) == cand_cols -%}
    {#- Same shape: replace the rows IN PLACE so the table keeps its identity (dependent views,
        grants, masking all survive), and carry over the docs dbt persisted on the candidate. -#}
    {%- do run_query("truncate " ~ r.port_name) -%}
    {%- do run_query("insert into " ~ r.port_name ~ " (" ~ cols ~ ") select " ~ cols
                     ~ " from " ~ r.cand_name) -%}
    {%- set copy_docs -%}
      do $$ declare c record; begin
        execute format('comment on table %s is %L', '{{ r.port_name }}',
                       obj_description(to_regclass('{{ r.cand_name }}'), 'pg_class'));
        for c in select attname, col_description(attrelid, attnum) as doc from pg_attribute
                 where attrelid = to_regclass('{{ r.cand_name }}') and attnum > 0
                   and not attisdropped loop
          execute format('comment on column %s.%I is %L', '{{ r.port_name }}', c.attname, c.doc);
        end loop;
      end $$
    {%- endset -%}
    {%- do run_query(copy_docs) -%}
    {%- do run_query("drop table " ~ r.cand_name) -%}
    {{ log("  published " ~ r.port_name ~ " (audit passed, replaced in place)", info=True) }}

  {%- else -%}
    {#- First publish, a view being replaced by a table, or a new shape: rename the candidate in.
        Refused while anything depends on the old relation — CASCADE would silently drop it. -#}
    {%- if r.port is not none -%}
      {%- set deps = dgp_dbt._wap_dependents(r.port_name) -%}
      {%- if deps | length > 0 -%}
        {{ exceptions.raise_compiler_error(
            "cannot replace " ~ r.port_name ~ " with a new shape: dependent object(s) "
            ~ (deps | join(', ')) ~ " would be lost — migrate them first (ADR-0019).") }}
      {%- endif -%}
      {%- do run_query("drop " ~ ("view" if r.port.is_view else "table") ~ " " ~ r.port_name
                       ~ " restrict") -%}
    {%- endif -%}
    {%- do run_query("alter table " ~ r.cand_name ~ " rename to " ~ adapter.quote(node.alias)) -%}
    {{ log("  published " ~ r.port_name ~ " (audit passed, renamed in)", info=True) }}
  {%- endif -%}
{% endmacro %}


{#- QUARANTINE: the audit failed — drop the candidate; consumers keep the last good version. -#}
{% macro wap_drop_candidate(model) %}
  {%- if not execute -%}{{ return('') }}{%- endif -%}
  {%- set r = dgp_dbt._wap_relations(dgp_dbt._wap_node(model)) -%}
  {%- do run_query("drop view if exists " ~ r.audit_name) -%}
  {%- do run_query("drop table if exists " ~ r.cand_name) -%}
  {{ log("  QUARANTINED " ~ r.port_name ~ ": audit failed, candidate dropped, last good kept",
         info=True) }}
{% endmacro %}
