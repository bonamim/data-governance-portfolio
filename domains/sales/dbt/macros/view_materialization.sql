{#- Views are rebuilt in place so what is built on them survives (dgp_dbt/macros/view.sql). dbt only
    takes a built-in materialization override from the ROOT project, hence this one-line wrapper. -#}
{%- materialization view, adapter='postgres' -%}
    {{ return(dgp_dbt.view_in_place()) }}
{%- endmaterialization -%}
