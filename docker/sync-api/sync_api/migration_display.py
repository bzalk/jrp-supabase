from .settings import *

def migration_statement_count(migration):
    statements = migration_display_statements(migration)
    if statements:
        return len(statements)
    value = migration.get("statement_count")
    return value if isinstance(value, int) else 0


def migration_sql(migration):
    return "\n\n".join(migration_display_statements(migration))


def raw_migration_sql(migration):
    return "\n\n".join(str(statement) for statement in migration.get("statements") or [])


def normalize_migration_sql(sql):
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = MIGRATION_FLATTENED_LINE_COMMENT_RE.sub("\n", sql)
    sql = MIGRATION_CREATE_POLICY_RE.sub(
        r"DROP POLICY IF EXISTS \1 ON \2;\nCREATE POLICY \1 ON \2",
        sql,
    )
    return sql.strip()


def dollar_quote_tag_at(sql, index):
    if sql[index] != "$":
        return None
    match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
    return match.group(0) if match else None


def split_sql_statements(sql):
    statements = []
    buffer = []
    index = 0
    single_quote = False
    double_quote = False
    dollar_quote = None

    while index < len(sql):
        char = sql[index]

        if dollar_quote:
            if sql.startswith(dollar_quote, index):
                buffer.append(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
                continue
            buffer.append(char)
            index += 1
            continue

        if single_quote:
            buffer.append(char)
            if char == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                buffer.append(sql[index + 1])
                index += 2
                continue
            if char == "'":
                single_quote = False
            index += 1
            continue

        if double_quote:
            buffer.append(char)
            if char == '"' and index + 1 < len(sql) and sql[index + 1] == '"':
                buffer.append(sql[index + 1])
                index += 2
                continue
            if char == '"':
                double_quote = False
            index += 1
            continue

        if char == "'":
            single_quote = True
            buffer.append(char)
            index += 1
            continue

        if char == '"':
            double_quote = True
            buffer.append(char)
            index += 1
            continue

        if char == "$":
            tag = dollar_quote_tag_at(sql, index)
            if tag:
                dollar_quote = tag
                buffer.append(tag)
                index += len(tag)
                continue

        buffer.append(char)
        index += 1

        if char == ";":
            statement = format_migration_statement("".join(buffer))
            if statement:
                statements.append(statement)
            buffer = []

    statement = format_migration_statement("".join(buffer))
    if statement:
        statements.append(statement)
    return statements


def split_top_level_commas(sql):
    parts = []
    buffer = []
    index = 0
    depth = 0
    single_quote = False
    double_quote = False
    dollar_quote = None

    while index < len(sql):
        char = sql[index]

        if dollar_quote:
            if sql.startswith(dollar_quote, index):
                buffer.append(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
                continue
            buffer.append(char)
            index += 1
            continue

        if single_quote:
            buffer.append(char)
            if char == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                buffer.append(sql[index + 1])
                index += 2
                continue
            if char == "'":
                single_quote = False
            index += 1
            continue

        if double_quote:
            buffer.append(char)
            if char == '"' and index + 1 < len(sql) and sql[index + 1] == '"':
                buffer.append(sql[index + 1])
                index += 2
                continue
            if char == '"':
                double_quote = False
            index += 1
            continue

        if char == "'":
            single_quote = True
        elif char == '"':
            double_quote = True
        elif char == "$":
            tag = dollar_quote_tag_at(sql, index)
            if tag:
                dollar_quote = tag
                buffer.append(tag)
                index += len(tag)
                continue
        elif char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts


def format_create_table_statement(statement):
    match = re.match(
        r"^(CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[^\s(]+)\s*\((.*)\);$",
        statement,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return statement
    columns = split_top_level_commas(match.group(2))
    if not columns:
        return statement
    formatted_columns = ",\n  ".join(columns)
    return f"{match.group(1)} (\n  {formatted_columns}\n);"


def format_policy_statement(statement):
    if not re.match(r"^CREATE\s+POLICY\b", statement, re.IGNORECASE):
        return statement
    statement = re.sub(r"\s+ON\s+", "\n  ON ", statement, count=1, flags=re.IGNORECASE)
    statement = re.sub(r"\s+FOR\s+", "\n  FOR ", statement, count=1, flags=re.IGNORECASE)
    statement = re.sub(r"\s+TO\s+", "\n  TO ", statement, count=1, flags=re.IGNORECASE)
    statement = re.sub(r"\s+USING\s+", "\n  USING ", statement, count=1, flags=re.IGNORECASE)
    statement = re.sub(r"\s+WITH\s+CHECK\s+", "\n  WITH CHECK ", statement, count=1, flags=re.IGNORECASE)
    return statement


def format_dollar_quoted_statement(statement):
    statement = re.sub(r"\s+AS\s+\$\$\s*", " AS $$\n", statement, count=1, flags=re.IGNORECASE)
    statement = re.sub(r"^DO\s+\$\$\s*", "DO $$\n", statement, count=1, flags=re.IGNORECASE)
    statement = re.sub(r"\s*\$\$;$", "\n$$;", statement)
    return statement


def compact_sql_whitespace(sql):
    output = []
    index = 0
    pending_space = False
    single_quote = False
    double_quote = False
    dollar_quote = None

    def flush_space():
        nonlocal pending_space
        if pending_space and output and output[-1] not in "(\n ":
            output.append(" ")
        pending_space = False

    while index < len(sql):
        char = sql[index]

        if dollar_quote:
            if sql.startswith(dollar_quote, index):
                output.append(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
                continue
            output.append(char)
            index += 1
            continue

        if single_quote:
            output.append(char)
            if char == "'" and index + 1 < len(sql) and sql[index + 1] == "'":
                output.append(sql[index + 1])
                index += 2
                continue
            if char == "'":
                single_quote = False
            index += 1
            continue

        if double_quote:
            output.append(char)
            if char == '"' and index + 1 < len(sql) and sql[index + 1] == '"':
                output.append(sql[index + 1])
                index += 2
                continue
            if char == '"':
                double_quote = False
            index += 1
            continue

        if char.isspace():
            pending_space = True
            index += 1
            continue

        flush_space()

        if char == "'":
            single_quote = True
        elif char == '"':
            double_quote = True
        elif char == "$":
            tag = dollar_quote_tag_at(sql, index)
            if tag:
                dollar_quote = tag
                output.append(tag)
                index += len(tag)
                continue

        output.append(char)
        index += 1

    return "".join(output).strip()


def format_migration_statement(statement):
    statement = compact_sql_whitespace(statement)
    if not statement:
        return ""
    statement = format_create_table_statement(statement)
    statement = format_policy_statement(statement)
    statement = format_dollar_quoted_statement(statement)
    return statement


def migration_display_statements(migration):
    return split_sql_statements(normalize_migration_sql(raw_migration_sql(migration)))
