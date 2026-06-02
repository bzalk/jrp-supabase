from .settings import *

def table_stats_sql(exact_rows, include_columns):
    if exact_rows:
        row_count_expr = """
        coalesce(
          (
            xpath(
              '/table/row/count/text()',
              query_to_xml(
                format('select count(*) as count from %I.%I', n.nspname, c.relname),
                false,
                true,
                ''
              )
            )
          )[1]::text::bigint,
          0
        )
        """
    else:
        row_count_expr = """
        greatest(
          coalesce(s.n_live_tup, 0)::bigint,
          case when c.reltuples >= 0 then c.reltuples::bigint else 0 end
        )
        """

    column_count_select = ""
    column_count_join = ""
    column_count_json = ""
    if include_columns:
        column_count_select = "coalesce(cols.column_count, 0) as column_count,"
        column_count_join = """
  left join lateral (
    select count(*)::int as column_count
    from pg_attribute a
    where a.attrelid = c.oid
      and a.attnum > 0
      and not a.attisdropped
  ) cols on true"""
        column_count_json = "'column_count', column_count,"

    return f"""
set statement_timeout = {STATS_STATEMENT_TIMEOUT_MS};
with tables as (
  select
    n.nspname as table_schema,
    c.relname as table_name,
    {row_count_expr} as row_count,
    {column_count_select}
    pg_total_relation_size(c.oid)::bigint as total_bytes
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  left join pg_stat_all_tables s on s.relid = c.oid
  {column_count_join}
  where c.relkind in ('r', 'p')
    and n.nspname not in ('information_schema', 'pg_catalog')
    and n.nspname not like 'pg_toast%'
    and n.nspname not like 'pg_temp_%'
)
select jsonb_build_object(
  'database_name', current_database(),
  'table_count', (select count(*) from tables),
  'tables', coalesce(
    (
      select jsonb_agg(
        jsonb_build_object(
          'schema', table_schema,
          'name', table_name,
          'row_count', row_count,
          'row_count_exact', {str(exact_rows).lower()},
          {column_count_json}
          'total_bytes', total_bytes
        )
        order by table_schema, table_name
      )
      from tables
    ),
    '[]'::jsonb
  ),
  'has_storage_buckets', to_regclass('storage.buckets') is not null
)::text;
"""


def database_schemas_sql():
    return f"""
set statement_timeout = {STATS_STATEMENT_TIMEOUT_MS};
with schemas as (
  select
    n.oid,
    n.nspname as name,
    pg_get_userbyid(n.nspowner) as owner,
    count(c.oid) filter (where c.relkind in ('r', 'p'))::int as table_count,
    coalesce(
      sum(
        case
          when c.relkind in ('r', 'p', 'm') then pg_total_relation_size(c.oid)
          else 0
        end
      ),
      0
    )::bigint as total_bytes
  from pg_namespace n
  left join pg_class c
    on c.relnamespace = n.oid
   and c.relkind in ('r', 'p', 'v', 'm', 'f', 'S')
  where n.nspname not in ('information_schema', 'pg_catalog')
    and n.nspname not like 'pg_toast%'
    and n.nspname not like 'pg_temp_%'
  group by n.oid, n.nspname, n.nspowner
)
select jsonb_build_object(
  'database_name', current_database(),
  'schemas', coalesce(
    (
      select jsonb_agg(
        jsonb_build_object(
          'name', name,
          'owner', owner,
          'table_count', table_count,
          'total_bytes', total_bytes
        )
        order by name
      )
      from schemas
    ),
    '[]'::jsonb
  )
)::text;
"""


def storage_bucket_stats_sql():
    return f"""
set statement_timeout = {STATS_STATEMENT_TIMEOUT_MS};
select coalesce(
  jsonb_agg(
    jsonb_build_object(
      'id', id,
      'name', name,
      'public', public,
      'created_at', created_at,
      'updated_at', updated_at,
      'file_size_limit', file_size_limit,
      'allowed_mime_types', allowed_mime_types
    )
    order by name, id
  ),
  '[]'::jsonb
)::text
from storage.buckets;
"""


def table_metadata_sql(schema_name, table_name, exact_rows):
    schema_literal = sql_literal(schema_name)
    table_literal = sql_literal(table_name)
    if exact_rows:
        row_count_expr = """
        coalesce(
          (
            xpath(
              '/table/row/count/text()',
              query_to_xml(
                format('select count(*) as count from %I.%I', s.table_schema, s.table_name),
                false,
                true,
                ''
              )
            )
          )[1]::text::bigint,
          0
        )
        """
    else:
        row_count_expr = """
        greatest(
          coalesce(s.n_live_tup, 0)::bigint,
          case when s.reltuples >= 0 then s.reltuples::bigint else 0 end
        )
        """

    return f"""
set statement_timeout = {STATS_STATEMENT_TIMEOUT_MS};
with selected as (
  select
    c.oid,
    n.nspname as table_schema,
    c.relname as table_name,
    c.relkind,
    c.reltuples,
    st.n_live_tup,
    pg_total_relation_size(c.oid)::bigint as total_bytes,
    pg_relation_size(c.oid)::bigint as table_bytes,
    pg_indexes_size(c.oid)::bigint as index_bytes,
    greatest(
      pg_total_relation_size(c.oid)::bigint
      - pg_relation_size(c.oid)::bigint
      - pg_indexes_size(c.oid)::bigint,
      0
    ) as toast_bytes
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  left join pg_stat_all_tables st on st.relid = c.oid
  where n.nspname = {schema_literal}
    and c.relname = {table_literal}
    and c.relkind in ('r', 'p')
  order by c.oid
  limit 1
),
columns as (
  select
    coalesce(
      jsonb_agg(
        jsonb_build_object(
          'ordinal_position', a.attnum,
          'name', a.attname,
          'data_type', format_type(a.atttypid, a.atttypmod),
          'type_schema', tn.nspname,
          'type_name', t.typname,
          'is_nullable', not a.attnotnull,
          'default', pg_get_expr(ad.adbin, ad.adrelid),
          'is_identity', a.attidentity <> '',
          'identity_generation',
            case a.attidentity
              when 'a' then 'always'
              when 'd' then 'by_default'
              else null
            end,
          'generated',
            case a.attgenerated
              when 's' then 'stored'
              when 'v' then 'virtual'
              else null
            end
        )
        order by a.attnum
      ),
      '[]'::jsonb
    ) as payload,
    count(a.attnum)::int as column_count
  from selected s
  join pg_attribute a on a.attrelid = s.oid
  join pg_type t on t.oid = a.atttypid
  join pg_namespace tn on tn.oid = t.typnamespace
  left join pg_attrdef ad on ad.adrelid = s.oid and ad.adnum = a.attnum
  where a.attnum > 0
    and not a.attisdropped
),
primary_key as (
  select coalesce(
    jsonb_agg(a.attname order by array_position(i.indkey::int[], a.attnum)),
    '[]'::jsonb
  ) as payload
  from selected s
  join pg_index i on i.indrelid = s.oid and i.indisprimary
  join pg_attribute a on a.attrelid = s.oid and a.attnum = any(i.indkey)
),
indexes as (
  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'name', ci.relname,
        'definition', pg_get_indexdef(i.indexrelid),
        'is_unique', i.indisunique,
        'is_primary', i.indisprimary,
        'size_bytes', pg_relation_size(ci.oid)::bigint
      )
      order by ci.relname
    ),
    '[]'::jsonb
  ) as payload
  from selected s
  join pg_index i on i.indrelid = s.oid
  join pg_class ci on ci.oid = i.indexrelid
)
select coalesce(
  (
    select jsonb_build_object(
      'database_name', current_database(),
      'schema', s.table_schema,
      'name', s.table_name,
      'exists', true,
      'kind',
        case s.relkind
          when 'r' then 'table'
          when 'p' then 'partitioned_table'
          else s.relkind::text
        end,
      'row_count', {row_count_expr},
      'row_count_exact', {str(exact_rows).lower()},
      'column_count', coalesce(c.column_count, 0),
      'total_bytes', s.total_bytes,
      'table_bytes', s.table_bytes,
      'index_bytes', s.index_bytes,
      'toast_bytes', s.toast_bytes,
      'primary_key', coalesce(pk.payload, '[]'::jsonb),
      'columns', coalesce(c.payload, '[]'::jsonb),
      'indexes', coalesce(i.payload, '[]'::jsonb)
    )
    from selected s
    cross join columns c
    cross join primary_key pk
    cross join indexes i
  ),
  jsonb_build_object(
    'database_name', current_database(),
    'schema', {schema_literal},
    'name', {table_literal},
    'exists', false,
    'kind', null,
    'row_count', null,
    'row_count_exact', {str(exact_rows).lower()},
    'column_count', 0,
    'total_bytes', null,
    'table_bytes', null,
    'index_bytes', null,
    'toast_bytes', null,
    'primary_key', '[]'::jsonb,
    'columns', '[]'::jsonb,
    'indexes', '[]'::jsonb
  )
)::text;
"""


def database_functions_sql():
    return f"""
set statement_timeout = {STATS_STATEMENT_TIMEOUT_MS};
with database_functions as (
  select
    p.oid,
    n.nspname as schema,
    p.proname as name,
    pg_get_function_identity_arguments(p.oid) as identity_arguments,
    pg_get_function_arguments(p.oid) as arguments,
    pg_get_function_result(p.oid) as returns,
    l.lanname as language,
    case p.prokind
      when 'f' then 'function'
      when 'p' then 'procedure'
      when 'a' then 'aggregate'
      when 'w' then 'window'
      else p.prokind::text
    end as kind,
    case p.provolatile
      when 'i' then 'immutable'
      when 's' then 'stable'
      when 'v' then 'volatile'
      else p.provolatile::text
    end as volatility,
    p.prosecdef as security_definer,
    p.proisstrict as strict,
    p.proleakproof as leakproof,
    case p.proparallel
      when 's' then 'safe'
      when 'r' then 'restricted'
      when 'u' then 'unsafe'
      else p.proparallel::text
    end as parallel_safety,
    p.procost as cost,
    p.prorows as rows,
    pg_get_userbyid(p.proowner) as owner,
    obj_description(p.oid, 'pg_proc') as comment,
    case
      when p.prokind in ('f', 'p') then pg_get_functiondef(p.oid)
      when p.prokind = 'a' then jsonb_build_object(
        'kind', 'aggregate',
        'schema', n.nspname,
        'name', p.proname,
        'identity_arguments', pg_get_function_identity_arguments(p.oid),
        'arguments', pg_get_function_arguments(p.oid),
        'returns', pg_get_function_result(p.oid),
        'transition_function', ag.aggtransfn::regprocedure::text,
        'transition_type', ag.aggtranstype::regtype::text,
        'transition_space', ag.aggtransspace,
        'final_function',
          case when ag.aggfinalfn = 0 then null else ag.aggfinalfn::regprocedure::text end,
        'combine_function',
          case when ag.aggcombinefn = 0 then null else ag.aggcombinefn::regprocedure::text end,
        'serial_function',
          case when ag.aggserialfn = 0 then null else ag.aggserialfn::regprocedure::text end,
        'deserial_function',
          case when ag.aggdeserialfn = 0 then null else ag.aggdeserialfn::regprocedure::text end,
        'moving_transition_function',
          case when ag.aggmtransfn = 0 then null else ag.aggmtransfn::regprocedure::text end,
        'moving_inverse_transition_function',
          case when ag.aggminvtransfn = 0 then null else ag.aggminvtransfn::regprocedure::text end,
        'moving_final_function',
          case when ag.aggmfinalfn = 0 then null else ag.aggmfinalfn::regprocedure::text end,
        'moving_transition_type',
          case when ag.aggmtranstype = 0 then null else ag.aggmtranstype::regtype::text end,
        'moving_transition_space', ag.aggmtransspace,
        'sort_operator',
          case when ag.aggsortop = 0 then null else ag.aggsortop::regoperator::text end,
        'initial_condition', ag.agginitval,
        'moving_initial_condition', ag.aggminitval,
        'num_direct_arguments', ag.aggnumdirectargs,
        'final_extra', ag.aggfinalextra,
        'moving_final_extra', ag.aggmfinalextra,
        'final_modify', ag.aggfinalmodify,
        'moving_final_modify', ag.aggmfinalmodify,
        'aggregate_kind', ag.aggkind
      )::text
      else jsonb_build_object(
        'kind', p.prokind::text,
        'schema', n.nspname,
        'name', p.proname,
        'identity_arguments', pg_get_function_identity_arguments(p.oid),
        'arguments', pg_get_function_arguments(p.oid),
        'returns', pg_get_function_result(p.oid)
      )::text
    end as definition
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
  join pg_language l on l.oid = p.prolang
  left join pg_aggregate ag on ag.aggfnoid = p.oid
  where n.nspname not in ('information_schema', 'pg_catalog')
    and n.nspname not like 'pg_toast%'
    and n.nspname not like 'pg_temp_%'
)
select jsonb_build_object(
  'database_name', current_database(),
  'function_count', count(*),
  'functions', coalesce(
    jsonb_agg(
      jsonb_build_object(
        'schema', schema,
        'name', name,
        'identity_arguments', identity_arguments,
        'arguments', arguments,
        'returns', returns,
        'language', language,
        'kind', kind,
        'volatility', volatility,
        'security_definer', security_definer,
        'strict', strict,
        'leakproof', leakproof,
        'parallel_safety', parallel_safety,
        'cost', cost,
        'rows', rows,
        'owner', owner,
        'comment', comment,
        'definition', definition
      )
      order by schema, name, identity_arguments
    ),
    '[]'::jsonb
  )
)::text
from database_functions;
"""


def database_triggers_sql(include_internal):
    internal_filter = "" if include_internal else "and not t.tgisinternal"
    return f"""
set statement_timeout = {STATS_STATEMENT_TIMEOUT_MS};
with table_triggers as (
  select
    t.oid,
    n.nspname as table_schema,
    c.relname as table_name,
    t.tgname as name,
    t.tgisinternal as is_internal,
    case t.tgenabled
      when 'O' then 'origin'
      when 'D' then 'disabled'
      when 'R' then 'replica'
      when 'A' then 'always'
      else t.tgenabled::text
    end as enabled_mode,
    case
      when (t.tgtype & 64) <> 0 then 'instead_of'
      when (t.tgtype & 2) <> 0 then 'before'
      else 'after'
    end as timing,
    array_remove(array[
      case when (t.tgtype & 4) <> 0 then 'insert' end,
      case when (t.tgtype & 8) <> 0 then 'delete' end,
      case when (t.tgtype & 16) <> 0 then 'update' end,
      case when (t.tgtype & 32) <> 0 then 'truncate' end
    ], null) as events,
    case when (t.tgtype & 1) <> 0 then 'row' else 'statement' end as orientation,
    fn_n.nspname as function_schema,
    p.proname as function_name,
    pg_get_function_identity_arguments(p.oid) as function_identity_arguments,
    pg_get_expr(t.tgqual, t.tgrelid, true) as when_condition,
    obj_description(t.oid, 'pg_trigger') as comment,
    pg_get_triggerdef(t.oid, true) as definition
  from pg_trigger t
  join pg_class c on c.oid = t.tgrelid
  join pg_namespace n on n.oid = c.relnamespace
  join pg_proc p on p.oid = t.tgfoid
  join pg_namespace fn_n on fn_n.oid = p.pronamespace
  where n.nspname not in ('information_schema', 'pg_catalog')
    and n.nspname not like 'pg_toast%'
    and n.nspname not like 'pg_temp_%'
    {internal_filter}
),
event_triggers as (
  select
    e.oid,
    e.evtname as name,
    e.evtevent as event,
    case e.evtenabled
      when 'O' then 'origin'
      when 'D' then 'disabled'
      when 'R' then 'replica'
      when 'A' then 'always'
      else e.evtenabled::text
    end as enabled_mode,
    e.evttags as tags,
    fn_n.nspname as function_schema,
    p.proname as function_name,
    pg_get_function_identity_arguments(p.oid) as function_identity_arguments,
    obj_description(e.oid, 'pg_event_trigger') as comment,
    (
      'CREATE EVENT TRIGGER ' || quote_ident(e.evtname) ||
      ' ON ' || e.evtevent ||
      coalesce(
        (
          select ' WHEN TAG IN (' || string_agg(quote_literal(tag), ', ' order by tag) || ')'
          from unnest(e.evttags) as tag
        ),
        ''
      ) ||
      ' EXECUTE FUNCTION ' || quote_ident(fn_n.nspname) || '.' || quote_ident(p.proname) || '(' ||
      pg_get_function_identity_arguments(p.oid) || ');'
    ) as definition
  from pg_event_trigger e
  join pg_proc p on p.oid = e.evtfoid
  join pg_namespace fn_n on fn_n.oid = p.pronamespace
)
select jsonb_build_object(
  'database_name', current_database(),
  'table_trigger_count', (select count(*) from table_triggers),
  'table_triggers', coalesce(
    (
      select jsonb_agg(
        jsonb_build_object(
          'schema', table_schema,
          'table', table_name,
          'name', name,
          'is_internal', is_internal,
          'enabled_mode', enabled_mode,
          'timing', timing,
          'events', events,
          'orientation', orientation,
          'function_schema', function_schema,
          'function_name', function_name,
          'function_identity_arguments', function_identity_arguments,
          'when_condition', when_condition,
          'comment', comment,
          'definition', definition
        )
        order by table_schema, table_name, name
      )
      from table_triggers
    ),
    '[]'::jsonb
  ),
  'event_trigger_count', (select count(*) from event_triggers),
  'event_triggers', coalesce(
    (
      select jsonb_agg(
        jsonb_build_object(
          'name', name,
          'event', event,
          'enabled_mode', enabled_mode,
          'tags', tags,
          'function_schema', function_schema,
          'function_name', function_name,
          'function_identity_arguments', function_identity_arguments,
          'comment', comment,
          'definition', definition
        )
        order by name
      )
      from event_triggers
    ),
    '[]'::jsonb
  )
)::text;
"""
