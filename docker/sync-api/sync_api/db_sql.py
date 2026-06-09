from .settings import *
from .http_utils import sql_literal, sql_identifier


def platform_schema_sql_list():
    return ", ".join(
        sql_literal(schema_name)
        for schema_name in sorted(PRESERVE_TARGET_PLATFORM_SCHEMAS_FOR_IMPORT)
    )


def reset_schema_ownership_sql():
    platform_schemas = platform_schema_sql_list()
    return f"""
set client_min_messages = warning;
select coalesce(
  jsonb_agg(
    jsonb_build_object(
      'schema', n.nspname,
      'owner', pg_get_userbyid(n.nspowner),
      'can_drop',
        pg_has_role(n.nspowner, 'MEMBER')
        and n.nspname not in ({platform_schemas})
        and n.oid not in (select extnamespace from pg_extension)
        and not exists (
          select 1
          from pg_depend d
          where d.classid = 'pg_namespace'::regclass
            and d.objid = n.oid
            and d.refclassid = 'pg_extension'::regclass
        )
    )
    order by n.nspname
  ),
  '[]'::jsonb
)::text
from pg_namespace n
where n.nspname not in ('pg_catalog', 'information_schema')
  and n.nspname not like 'pg_toast%'
  and n.nspname not like 'pg_temp_%';
"""


def reset_existing_schema_names_sql():
    return """
set client_min_messages = warning;
select coalesce(jsonb_agg(n.nspname order by n.nspname), '[]'::jsonb)::text
from pg_namespace n
where n.nspname not in ('pg_catalog', 'information_schema')
  and n.nspname not like 'pg_toast%'
  and n.nspname not like 'pg_temp_%';
"""


def reset_existing_extension_names_sql():
    return """
set client_min_messages = warning;
select coalesce(jsonb_agg(extname order by extname), '[]'::jsonb)::text
from pg_extension;
"""


def reset_existing_event_trigger_names_sql():
    return """
set client_min_messages = warning;
select coalesce(jsonb_agg(evtname order by evtname), '[]'::jsonb)::text
from pg_event_trigger;
"""


def reset_existing_publication_names_sql():
    return """
set client_min_messages = warning;
select coalesce(jsonb_agg(pubname order by pubname), '[]'::jsonb)::text
from pg_publication;
"""


def reset_ensure_import_platform_schemas_sql():
    create_statements = "\n".join(
        f"create schema if not exists {sql_identifier(schema_name)};"
        for schema_name in sorted(SKIP_SOURCE_PLATFORM_SCHEMAS_FOR_HOSTED_RESTORE)
    )
    return f"""
set client_min_messages = warning;
{create_statements}
"""


def migration_ledger_export_sql():
    relation_literal = sql_literal(
        f"{BRANCH_MIGRATION_LEDGER_SCHEMA}.{BRANCH_MIGRATION_LEDGER_TABLE}"
    )
    return f"""
set client_min_messages = warning;
select case
  when to_regclass({relation_literal}) is null then '[]'::jsonb
  else (
    select coalesce(
      jsonb_agg(
        jsonb_build_object(
          'version', version,
          'name', name,
          'statements', statements
        )
        order by version
      ),
      '[]'::jsonb
    )
    from {BRANCH_MIGRATION_LEDGER_SCHEMA}.{BRANCH_MIGRATION_LEDGER_TABLE}
  )
end::text;
"""


def migration_ledger_restore_sql(migrations):
    payload_literal = sql_literal(json.dumps(migrations))
    schema_ident = sql_identifier(BRANCH_MIGRATION_LEDGER_SCHEMA)
    table_ident = sql_identifier(BRANCH_MIGRATION_LEDGER_TABLE)
    return f"""
set client_min_messages = warning;
create schema if not exists {schema_ident};
create table if not exists {schema_ident}.{table_ident} (
  version text primary key,
  statements text[],
  name text
);
truncate table {schema_ident}.{table_ident};
with migration_data as (
  select item
  from jsonb_array_elements({payload_literal}::jsonb) as item
)
insert into {schema_ident}.{table_ident} (
  version,
  statements,
  name
)
select
  item->>'version',
  array(
    select jsonb_array_elements_text(coalesce(item->'statements', '[]'::jsonb))
  ),
  item->>'name'
from migration_data
order by item->>'version';
"""


def source_migrations_sql():
    relation_literal = sql_literal(
        f"{BRANCH_MIGRATION_LEDGER_SCHEMA}.{BRANCH_MIGRATION_LEDGER_TABLE}"
    )
    source_schema = sql_identifier(BRANCH_MIGRATION_LEDGER_SCHEMA)
    source_table = sql_identifier(BRANCH_MIGRATION_LEDGER_TABLE)
    return f"""
set client_min_messages = warning;
select case
  when to_regclass({relation_literal}) is null then '[]'::jsonb
  else (
    select coalesce(
      jsonb_agg(
        jsonb_build_object(
          'version', version,
          'name', name,
          'statements', statements,
          'statement_count', coalesce(array_length(statements, 1), 0)
        )
        order by version
      ),
      '[]'::jsonb
    )
    from {source_schema}.{source_table}
  )
end::text;
"""


def target_promotion_records_sql(source_environment):
    source_literal = sql_literal(source_environment)
    schema_ident = sql_identifier(MIGRATION_PROMOTION_LEDGER_SCHEMA)
    table_ident = sql_identifier(MIGRATION_PROMOTION_LEDGER_TABLE)
    return f"""
set client_min_messages = warning;
select coalesce(
  jsonb_agg(
    jsonb_build_object(
      'source_version', source_version,
      'source_name', source_name,
      'status', status,
      'checksum_sha256', checksum_sha256,
      'statement_count', statement_count,
      'source_environment', source_environment,
      'target_environment', target_environment,
      'batch_id', batch_id,
      'batch_label', batch_label,
      'started_at', started_at,
      'promoted_at', promoted_at,
      'duration_ms', duration_ms,
      'error_message', error_message
    )
    order by source_version
  ),
  '[]'::jsonb
)::text
from {schema_ident}.{table_ident}
where source_environment = {source_literal};
"""


def reset_preclean_sql(drop_only_owned=True):
    owner_filter = "and pg_has_role(n.nspowner, 'MEMBER')" if drop_only_owned else ""
    platform_schemas = platform_schema_sql_list()
    return """
set client_min_messages = warning;
do $$
declare
  schema_name text;
begin
  for schema_name in
    select n.nspname
    from pg_namespace n
    where n.nspname not in ('pg_catalog', 'information_schema')
      and n.nspname not like 'pg_toast%'
      and n.nspname not like 'pg_temp_%'
      and n.nspname not in ({platform_schemas})
      and n.oid not in (select extnamespace from pg_extension)
      and not exists (
        select 1
        from pg_depend d
        where d.classid = 'pg_namespace'::regclass
          and d.objid = n.oid
          and d.refclassid = 'pg_extension'::regclass
      )
      {owner_filter}
    order by n.nspname
  loop
    execute format('drop schema if exists %I cascade', schema_name);
  end loop;
end
$$;
create schema if not exists public;
""".format(owner_filter=owner_filter, platform_schemas=platform_schemas)


def reset_app_schema_objects_sql(schema_names):
    if not schema_names:
        return "select '[]'::jsonb::text;\n"
    schema_values = ", ".join(f"({sql_literal(schema)})" for schema in schema_names)
    return f"""
set client_min_messages = warning;
do $$
declare
  item record;
begin
  for item in
    select
      case c.relkind
        when 'v' then 'view'
        when 'm' then 'materialized view'
        when 'f' then 'foreign table'
        when 'S' then 'sequence'
        else 'table'
      end as object_type,
      c.oid::regclass::text as object_identity,
      case c.relkind
        when 'v' then 1
        when 'm' then 2
        when 'f' then 3
        when 'r' then 4
        when 'p' then 4
        when 'S' then 5
        else 6
      end as drop_order
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    join (values {schema_values}) as allowed(schema_name)
      on allowed.schema_name = n.nspname
    where c.relkind in ('r', 'p', 'v', 'm', 'f', 'S')
      and pg_has_role(c.relowner, 'MEMBER')
      and not exists (
        select 1
        from pg_depend d
        where d.classid = 'pg_class'::regclass
          and d.objid = c.oid
          and d.deptype = 'e'
      )
    order by drop_order, n.nspname, c.relname
  loop
    execute format('drop %s if exists %s cascade', item.object_type, item.object_identity);
  end loop;

  for item in
    select
      case p.prokind
        when 'p' then 'procedure'
        when 'a' then 'aggregate'
        else 'function'
      end as object_type,
      p.oid::regprocedure::text as object_identity
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
    join (values {schema_values}) as allowed(schema_name)
      on allowed.schema_name = n.nspname
    where pg_has_role(p.proowner, 'MEMBER')
      and not exists (
        select 1
        from pg_depend d
        where d.classid = 'pg_proc'::regclass
          and d.objid = p.oid
          and d.deptype = 'e'
      )
    order by n.nspname, p.proname
  loop
    execute format('drop %s if exists %s cascade', item.object_type, item.object_identity);
  end loop;

  for item in
    select t.oid::regtype::text as object_identity
    from pg_type t
    join pg_namespace n on n.oid = t.typnamespace
    join (values {schema_values}) as allowed(schema_name)
      on allowed.schema_name = n.nspname
    where t.typtype in ('c', 'd', 'e', 'm', 'r')
      and t.typrelid = 0
      and pg_has_role(t.typowner, 'MEMBER')
      and not exists (
        select 1
        from pg_depend d
        where d.classid = 'pg_type'::regclass
          and d.objid = t.oid
          and d.deptype = 'e'
      )
    order by n.nspname, t.typname
  loop
    execute format('drop type if exists %s cascade', item.object_identity);
  end loop;
end
$$;
"""


def managed_table_privileges_sql(schema_names):
    if not schema_names:
        return "select '[]'::jsonb::text;\n"
    schema_literals = ", ".join(sql_literal(schema) for schema in schema_names)
    return f"""
set client_min_messages = warning;
select coalesce(
  jsonb_agg(
    jsonb_build_object(
      'schema', n.nspname,
      'table', c.relname,
      'can_insert', has_table_privilege(c.oid, 'INSERT'),
      'can_truncate', has_table_privilege(c.oid, 'TRUNCATE')
    )
    order by n.nspname, c.relname
  ),
  '[]'::jsonb
)::text
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname in ({schema_literals})
  and c.relkind in ('r', 'p');
"""


def managed_sequence_privileges_sql(schema_names):
    if not schema_names:
        return "select '[]'::jsonb::text;\n"
    schema_literals = ", ".join(sql_literal(schema) for schema in schema_names)
    return f"""
set client_min_messages = warning;
select coalesce(
  jsonb_agg(
    jsonb_build_object(
      'schema', n.nspname,
      'sequence', c.relname,
      'can_update', has_sequence_privilege(c.oid, 'UPDATE')
    )
    order by n.nspname, c.relname
  ),
  '[]'::jsonb
)::text
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname in ({schema_literals})
  and c.relkind = 'S';
"""


def truncate_tables_sql(table_keys, restart_identity=True):
    if not table_keys:
        return "select '[]'::jsonb::text;\n"
    table_values = ", ".join(
        f"({sql_literal(schema_name)}, {sql_literal(table_name)})"
        for schema_name, table_name in sorted(table_keys)
    )
    identity_clause = " restart identity" if restart_identity else ""
    return f"""
set client_min_messages = warning;
do $$
declare
  table_refs text;
begin
  select string_agg(format('%I.%I', n.nspname, c.relname), ', ' order by n.nspname, c.relname)
    into table_refs
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  join (values {table_values}) as allowed(schema_name, table_name)
    on allowed.schema_name = n.nspname
   and allowed.table_name = c.relname
  where true
    and c.relkind in ('r', 'p');

  if table_refs is not null then
    execute 'truncate table ' || table_refs || '{identity_clause} cascade';
  end if;
end
$$;
"""
