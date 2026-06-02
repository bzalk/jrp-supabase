from .settings import *

def table_key(table):
    return (table["schema"], table["name"])


def table_identity(key):
    return {"schema": key[0], "name": key[1]}


def bucket_key(bucket):
    return bucket["id"]


def bucket_identity(bucket):
    return {"id": bucket["id"], "name": bucket["name"]}


def compare_database_stats(source, target):
    source_tables = {table_key(table): table for table in source["tables"]}
    target_tables = {table_key(table): table for table in target["tables"]}
    source_table_keys = set(source_tables)
    target_table_keys = set(target_tables)

    row_count_differences = []
    for key in sorted(source_table_keys & target_table_keys):
        source_count = source_tables[key].get("row_count")
        target_count = target_tables[key].get("row_count")
        if source_count == target_count:
            continue
        delta = None
        if source_count is not None and target_count is not None:
            delta = target_count - source_count
        row_count_differences.append(
            {
                **table_identity(key),
                "source_row_count": source_count,
                "target_row_count": target_count,
                "delta": delta,
            }
        )

    source_buckets = {bucket_key(bucket): bucket for bucket in source["storage_buckets"]}
    target_buckets = {bucket_key(bucket): bucket for bucket in target["storage_buckets"]}
    source_bucket_keys = set(source_buckets)
    target_bucket_keys = set(target_buckets)

    return {
        "table_count_delta": target["table_count"] - source["table_count"],
        "storage_bucket_count_delta": (
            target["storage_bucket_count"] - source["storage_bucket_count"]
        ),
        "tables_missing_in_target": [
            table_identity(key) for key in sorted(source_table_keys - target_table_keys)
        ],
        "tables_missing_in_source": [
            table_identity(key) for key in sorted(target_table_keys - source_table_keys)
        ],
        "table_row_count_differences": row_count_differences,
        "storage_buckets_missing_in_target": [
            bucket_identity(source_buckets[key])
            for key in sorted(source_bucket_keys - target_bucket_keys)
        ],
        "storage_buckets_missing_in_source": [
            bucket_identity(target_buckets[key])
            for key in sorted(target_bucket_keys - source_bucket_keys)
        ],
    }


def changed_identities(source_items, target_items, key_fields, hash_field):
    source_by_key = {
        tuple(item.get(field) for field in key_fields): item for item in source_items
    }
    target_by_key = {
        tuple(item.get(field) for field in key_fields): item for item in target_items
    }

    changed = []
    for key in sorted(set(source_by_key) & set(target_by_key)):
        source_hash = source_by_key[key].get(hash_field)
        target_hash = target_by_key[key].get(hash_field)
        if source_hash and target_hash and source_hash != target_hash:
            changed.append({field: value for field, value in zip(key_fields, key)})
    return changed


def missing_identities(source_items, target_items, key_fields):
    source_keys = {tuple(item.get(field) for field in key_fields) for item in source_items}
    target_keys = {tuple(item.get(field) for field in key_fields) for item in target_items}
    return [
        {field: value for field, value in zip(key_fields, key)}
        for key in sorted(source_keys - target_keys)
    ]


def compare_database_functions(source, target):
    key_fields = ("schema", "name", "identity_arguments")
    return {
        "function_count_delta": target["function_count"] - source["function_count"],
        "functions_missing_in_target": missing_identities(
            source["functions"], target["functions"], key_fields
        ),
        "functions_missing_in_source": missing_identities(
            target["functions"], source["functions"], key_fields
        ),
        "function_definition_differences": changed_identities(
            source["functions"],
            target["functions"],
            key_fields,
            "definition_sha256",
        ),
    }


def compare_database_triggers(source, target):
    table_key_fields = ("schema", "table", "name")
    event_key_fields = ("name",)
    return {
        "table_trigger_count_delta": (
            target["table_trigger_count"] - source["table_trigger_count"]
        ),
        "event_trigger_count_delta": (
            target["event_trigger_count"] - source["event_trigger_count"]
        ),
        "table_triggers_missing_in_target": missing_identities(
            source["table_triggers"], target["table_triggers"], table_key_fields
        ),
        "table_triggers_missing_in_source": missing_identities(
            target["table_triggers"], source["table_triggers"], table_key_fields
        ),
        "table_trigger_definition_differences": changed_identities(
            source["table_triggers"],
            target["table_triggers"],
            table_key_fields,
            "definition_sha256",
        ),
        "event_triggers_missing_in_target": missing_identities(
            source["event_triggers"], target["event_triggers"], event_key_fields
        ),
        "event_triggers_missing_in_source": missing_identities(
            target["event_triggers"], source["event_triggers"], event_key_fields
        ),
        "event_trigger_definition_differences": changed_identities(
            source["event_triggers"],
            target["event_triggers"],
            event_key_fields,
            "definition_sha256",
        ),
    }
