from .settings import *

def database_endpoint_from_config(config, role, default_user="supabase_admin"):
    db_url = config.get(f"{role}_db_url")
    if db_url:
        return {"kind": "url", "db_url": db_url}

    container = config.get(f"{role}_container")
    if role == "source" and not container:
        container = "supabase-db"
    if not container:
        raise ValueError(f"{role} must define db_url or container")

    return {
        "kind": "container",
        "container": container,
        "user": config.get(f"{role}_user") or default_user,
        "database": config.get(f"{role}_db_name") or "postgres",
    }


def reset_database_endpoint_from_config(config, role):
    reset_user = config.get(f"{role}_reset_user")
    if reset_user:
        reset_config = dict(config)
        reset_config[f"{role}_user"] = reset_user
        return database_endpoint_from_config(
            reset_config,
            role,
            default_user=reset_user,
        )
    return database_endpoint_from_config(config, role, default_user="postgres")


def psql_command(endpoint):
    if endpoint["kind"] == "url":
        return [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-A",
            "-t",
            "-q",
            endpoint["db_url"],
        ]

    return [
        "docker",
        "exec",
        "-i",
        endpoint["container"],
        "psql",
        "-U",
        endpoint["user"],
        "-d",
        endpoint["database"],
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-A",
        "-t",
        "-q",
    ]


def psql_json(endpoint, sql):
    process = subprocess.run(
        psql_command(endpoint),
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=STATS_TIMEOUT_SECONDS,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"psql failed: {detail}")

    lines = [line for line in process.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("psql returned no JSON output")
    return json.loads(lines[-1])


def endpoint_label(endpoint):
    if endpoint["kind"] == "url":
        parsed = urlparse(endpoint["db_url"])
        database = parsed.path.lstrip("/") or "postgres"
        return f"{parsed.hostname or 'url'}/{database}"
    return f"{endpoint['container']}/{endpoint['database']} as {endpoint['user']}"


def pg_dump_command(endpoint, options):
    command = ["pg_dump", "--format=custom"]
    if not options.get("include_table_data", True):
        command.append("--schema-only")
    if options.get("no_owner", True):
        command.append("--no-owner")
    if options.get("no_privileges", True):
        command.append("--no-privileges")
    for schema_name in options.get("schemas") or []:
        command += ["--schema", schema_name]

    if endpoint["kind"] == "url":
        command.append(endpoint["db_url"])
        return command

    return [
        "docker",
        "exec",
        "-i",
        endpoint["container"],
        *command,
        "-U",
        endpoint["user"],
        "-d",
        endpoint["database"],
    ]


def pg_restore_command(
    endpoint,
    options,
    clean=True,
    use_list=None,
    disable_trigger_checks=False,
    filter_unsupported_settings=False,
):
    command = ["pg_restore", "--exit-on-error"]
    if not filter_unsupported_settings:
        command.append("--single-transaction")
    if clean:
        command += ["--clean", "--if-exists"]
    if use_list:
        command += ["--use-list", use_list]
    if options.get("no_owner", True):
        command.append("--no-owner")
    if options.get("no_privileges", True):
        command.append("--no-privileges")
    for schema_name in options.get("schemas") or []:
        command += ["--schema", schema_name]
    if filter_unsupported_settings:
        command += ["--file", "-"]

    if endpoint["kind"] == "url":
        if filter_unsupported_settings:
            restore_args = command[1:]
            script = (
                "set -o pipefail; db_url=$1; shift; "
                "pg_restore \"$@\" | "
                "sed '/^SET transaction_timeout = 0;$/d' | "
                "psql --single-transaction -X -v ON_ERROR_STOP=1 \"$db_url\""
            )
            return [
                "bash",
                "-c",
                script,
                "pg_restore-url",
                endpoint["db_url"],
                *restore_args,
            ]
        command += ["--dbname", endpoint["db_url"]]
        return command

    restore_args = command[1:]
    pgoptions = "-c session_replication_role=replica" if disable_trigger_checks else ""
    if filter_unsupported_settings:
        script = (
            "set -o pipefail; "
            "container=$1; user=$2; database=$3; pgoptions=$4; shift 4; "
            "password=$(docker exec \"$container\" sh -c 'printf %s \"$POSTGRES_PASSWORD\"'); "
            "pg_restore \"$@\" | "
            "sed '/^SET transaction_timeout = 0;$/d' | "
            "PGPASSWORD=\"$password\" PGOPTIONS=\"$pgoptions\" "
            "psql --single-transaction -X -v ON_ERROR_STOP=1 "
            "-h \"$container\" -U \"$user\" -d \"$database\""
        )
        return [
            "bash",
            "-c",
            script,
            "pg_restore-container",
            endpoint["container"],
            endpoint["user"],
            endpoint["database"],
            pgoptions,
            *restore_args,
        ]

    script = (
        "container=$1; user=$2; database=$3; pgoptions=$4; shift 4; "
        "password=$(docker exec \"$container\" sh -c 'printf %s \"$POSTGRES_PASSWORD\"'); "
        "PGPASSWORD=\"$password\" PGOPTIONS=\"$pgoptions\" exec pg_restore \"$@\" "
        "-h \"$container\" -U \"$user\" -d \"$database\""
    )
    return [
        "sh",
        "-c",
        script,
        "pg_restore-container",
        endpoint["container"],
        endpoint["user"],
        endpoint["database"],
        pgoptions,
        *restore_args,
    ]
