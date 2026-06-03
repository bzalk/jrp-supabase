from .settings import *
from .audit import append_job_output, run_logged_command

def restore_archive_list(archive_path):
    process = subprocess.run(
        ["pg_restore", "--list", archive_path],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"pg_restore --list failed: {detail}")
    return process.stdout.splitlines()


def restore_list_line_for_schema(line, schema_name):
    marker = f" {schema_name} "
    return marker in f" {line} "


def restore_list_line_is_schema_definition(line, schema_name):
    return f" SCHEMA - {schema_name} " in f" {line} "


def restore_list_line_is_managed_data(line, schema_name):
    padded = f" {line} "
    return (
        f" TABLE DATA {schema_name} " in padded
        or f" SEQUENCE SET {schema_name} " in padded
    )


def parse_table_data_restore_line(line):
    match = re.search(r"\bTABLE DATA\s+(\S+)\s+(\S+)\s", line)
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_sequence_set_restore_line(line):
    match = re.search(r"\bSEQUENCE SET\s+(\S+)\s+(\S+)\s", line)
    if not match:
        return None
    return match.group(1), match.group(2)


def unquote_restore_name(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def parse_extension_restore_line(line):
    match = re.search(r"\bEXTENSION\s+-\s+(.+?)\s*$", line)
    if not match:
        return None
    return unquote_restore_name(match.group(1).split()[0])


def parse_extension_comment_restore_line(line):
    match = re.search(r"\bCOMMENT\s+-\s+EXTENSION\s+(.+?)\s*$", line)
    if not match:
        return None
    return unquote_restore_name(match.group(1).split()[0])


def parse_event_trigger_restore_line(line):
    match = re.search(r"\bEVENT TRIGGER\s+-\s+(.+?)\s", line)
    if not match:
        return None
    return unquote_restore_name(match.group(1))


def parse_event_trigger_comment_restore_line(line):
    match = re.search(r"\bCOMMENT\s+-\s+EVENT TRIGGER\s+(.+?)\s*$", line)
    if not match:
        return None
    return unquote_restore_name(match.group(1).split()[0])


def parse_publication_restore_line(line):
    match = re.search(r"\bPUBLICATION\s+-\s+(.+?)\s", line)
    if not match:
        return None
    return unquote_restore_name(match.group(1))


def parse_publication_comment_restore_line(line):
    match = re.search(r"\bCOMMENT\s+-\s+PUBLICATION\s+(.+?)\s*$", line)
    if not match:
        return None
    return unquote_restore_name(match.group(1).split()[0])


def write_filtered_restore_list(
    archive_path,
    list_path,
    managed_schemas,
    preserved_schemas=None,
    managed_table_data_keys=None,
    managed_sequence_set_keys=None,
    existing_extensions=None,
    existing_event_triggers=None,
    existing_publications=None,
    skipped_source_schemas=None,
    skipped_source_extensions=None,
):
    managed_schemas = set(managed_schemas)
    preserved_schemas = set(preserved_schemas or []) | managed_schemas
    managed_table_data_keys = set(managed_table_data_keys or [])
    managed_sequence_set_keys = set(managed_sequence_set_keys or [])
    existing_extensions = set(existing_extensions or [])
    existing_event_triggers = set(existing_event_triggers or [])
    existing_publications = set(existing_publications or [])
    skipped_source_schemas = set(skipped_source_schemas or [])
    skipped_source_extensions = set(skipped_source_extensions or [])
    kept = []
    removed_count = 0
    for line in restore_archive_list(archive_path):
        if not line or line.startswith(";"):
            kept.append(line)
            continue

        remove = False
        extension_name = parse_extension_restore_line(line)
        if extension_name and (
            extension_name in existing_extensions
            or extension_name in skipped_source_extensions
        ):
            remove = True
        if parse_extension_comment_restore_line(line):
            remove = True
        event_trigger_name = parse_event_trigger_restore_line(line)
        if event_trigger_name and (
            event_trigger_name in existing_event_triggers
            or event_trigger_name.startswith(("issue_", "pgrst_"))
        ):
            remove = True
        event_trigger_comment_name = parse_event_trigger_comment_restore_line(line)
        if event_trigger_comment_name and (
            event_trigger_comment_name in existing_event_triggers
            or event_trigger_comment_name.startswith(("issue_", "pgrst_"))
        ):
            remove = True
        publication_name = parse_publication_restore_line(line)
        if publication_name and (
            publication_name in existing_publications
            or publication_name == "supabase_realtime"
        ):
            remove = True
        publication_comment_name = parse_publication_comment_restore_line(line)
        if publication_comment_name and (
            publication_comment_name in existing_publications
            or publication_comment_name == "supabase_realtime"
        ):
            remove = True

        table_data_key = parse_table_data_restore_line(line)
        if table_data_key:
            schema_name = table_data_key[0]
            if schema_name in managed_schemas:
                remove = table_data_key not in managed_table_data_keys
            elif schema_name in preserved_schemas and schema_name != "public":
                remove = True

        sequence_set_key = parse_sequence_set_restore_line(line)
        if sequence_set_key:
            schema_name = sequence_set_key[0]
            if schema_name in managed_schemas:
                remove = sequence_set_key not in managed_sequence_set_keys
            elif schema_name in preserved_schemas and schema_name != "public":
                remove = True

        for schema_name in managed_schemas:
            if (
                restore_list_line_for_schema(line, schema_name)
                and not restore_list_line_is_managed_data(line, schema_name)
            ):
                remove = True
                break
        for schema_name in preserved_schemas:
            if restore_list_line_is_schema_definition(line, schema_name):
                remove = True
                break
            if (
                schema_name != "public"
                and schema_name not in managed_schemas
                and restore_list_line_for_schema(line, schema_name)
            ):
                remove = True
                break
        for schema_name in skipped_source_schemas:
            if (
                restore_list_line_is_schema_definition(line, schema_name)
                or restore_list_line_for_schema(line, schema_name)
            ):
                remove = True
                break

        if remove:
            removed_count += 1
            kept.append(";" + line)
        else:
            kept.append(line)

    Path(list_path).write_text("\n".join(kept) + "\n")
    return removed_count
