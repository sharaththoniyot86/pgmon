"""User/role administration for the monitored Postgres server.

Everything here operates on the *monitored* database (via its own pool),
never on this app's own SQLite user store (see app/userdb.py for that --
unrelated, don't confuse "pgmon logins" with "Postgres roles").

Design notes for anyone extending this:
  - Role/schema/table names can't be parameterized in DDL the way values
    can, so every identifier is (a) shape-validated against _SAFE_IDENT
    up front, and (b) still passed through Postgres's own quote_ident()
    before being spliced into SQL text -- belt and suspenders. Passwords
    and VALID UNTIL values go through quote_literal() the same way.
  - Privilege keywords (SELECT/INSERT/...) can't be parameterized either;
    those are checked against a hardcoded whitelist per object type
    instead of being quoted.
  - The role this pool is currently authenticated as (`SELECT
    current_user`) is off-limits for DROP and for revoking its own LOGIN
    -- both would either fail outright or silently strand the app's own
    connection pool.
"""
from __future__ import annotations

import re

import asyncpg

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class RoleError(Exception):
    """Raised for anything that should surface as a 400 to the caller --
    bad identifiers, disallowed self-modification, whitelist violations,
    or a Postgres error we've wrapped with more context."""


def _validate_ident(name: str, what: str = "name") -> str:
    name = (name or "").strip()
    if not _SAFE_IDENT.match(name):
        raise RoleError(
            f"Invalid {what} {name!r}: must start with a letter or underscore and contain only "
            "letters, digits, underscore, $ (quoted/exotic identifiers aren't supported here)."
        )
    return name


async def _qi(conn: asyncpg.Connection, name: str) -> str:
    """Quoted identifier, safe to splice into SQL text."""
    return await conn.fetchval("SELECT quote_ident($1)", name)


async def _ql(conn: asyncpg.Connection, value: str) -> str:
    """Quoted string literal, safe to splice into SQL text."""
    return await conn.fetchval("SELECT quote_literal($1)", value)


async def _current_user(conn: asyncpg.Connection) -> str:
    return await conn.fetchval("SELECT current_user")


# --------------------------------------------------------------- listing ---
async def list_roles(pool: asyncpg.Pool) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin,
                   r.rolreplication, r.rolbypassrls, r.rolinherit, r.rolconnlimit,
                   r.rolvaliduntil,
                   COALESCE(m.member_of, ARRAY[]::text[]) AS member_of,
                   COALESCE(a.active_connections, 0)::int AS active_connections
            FROM pg_roles r
            LEFT JOIN LATERAL (
                SELECT array_agg(pr.rolname ORDER BY pr.rolname) AS member_of
                FROM pg_auth_members am JOIN pg_roles pr ON pr.oid = am.roleid
                WHERE am.member = r.oid
            ) m ON true
            LEFT JOIN LATERAL (
                SELECT count(*) AS active_connections
                FROM pg_stat_activity sa WHERE sa.usename = r.rolname
            ) a ON true
            ORDER BY r.rolname
            """
        )
    return [dict(r) for r in rows]


async def get_role_detail(pool: asyncpg.Pool, rolname: str) -> dict:
    _validate_ident(rolname, "role name")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin,
                   rolreplication, rolbypassrls, rolinherit, rolconnlimit, rolvaliduntil
            FROM pg_roles WHERE rolname = $1
            """,
            rolname,
        )
        if row is None:
            raise RoleError(f"No such role: {rolname!r}.")

        member_of = await conn.fetch(
            """
            SELECT pr.rolname, am.admin_option
            FROM pg_auth_members am JOIN pg_roles pr ON pr.oid = am.roleid
            JOIN pg_roles me ON me.oid = am.member
            WHERE me.rolname = $1 ORDER BY pr.rolname
            """,
            rolname,
        )
        members = await conn.fetch(
            """
            SELECT pm.rolname, am.admin_option
            FROM pg_auth_members am JOIN pg_roles pm ON pm.oid = am.member
            JOIN pg_roles owner ON owner.oid = am.roleid
            WHERE owner.rolname = $1 ORDER BY pm.rolname
            """,
            rolname,
        )
        sessions = await conn.fetch(
            """
            SELECT pid, datname, client_addr::text AS client_addr, application_name,
                   state, backend_start
            FROM pg_stat_activity WHERE usename = $1 ORDER BY backend_start
            """,
            rolname,
        )
        is_self = rolname == await _current_user(conn)

    grants = await list_grants(pool, rolname)
    return {
        **dict(row),
        "member_of": [dict(r) for r in member_of],
        "members": [dict(r) for r in members],
        "active_sessions": [dict(r) for r in sessions],
        "grants": grants,
        "is_self": is_self,
    }


# --------------------------------------------------------------- create ---
async def create_role(pool: asyncpg.Pool, payload: dict) -> None:
    rolname = _validate_ident(payload.get("rolname", ""), "role name")
    password = (payload.get("password") or "").strip() or None
    member_of = [str(r) for r in (payload.get("member_of") or [])]
    for r in member_of:
        _validate_ident(r, "role to grant membership in")

    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", rolname)
        if existing:
            raise RoleError(f"Role {rolname!r} already exists.")

        qname = await _qi(conn, rolname)
        opts = [
            "LOGIN" if payload.get("can_login") else "NOLOGIN",
            "SUPERUSER" if payload.get("superuser") else "NOSUPERUSER",
            "CREATEDB" if payload.get("createdb") else "NOCREATEDB",
            "CREATEROLE" if payload.get("createrole") else "NOCREATEROLE",
            "REPLICATION" if payload.get("replication") else "NOREPLICATION",
            "BYPASSRLS" if payload.get("bypassrls") else "NOBYPASSRLS",
            "INHERIT" if payload.get("inherit", True) else "NOINHERIT",
        ]
        opts.append(f"CONNECTION LIMIT {_parse_connlimit(payload.get('connlimit'))}")
        if password:
            opts.append(f"PASSWORD {await _ql(conn, password)}")
        valid_until = _clean_valid_until(payload.get("valid_until"))
        if valid_until:
            opts.append(f"VALID UNTIL {await _ql(conn, valid_until)}")

        try:
            await conn.execute(f"CREATE ROLE {qname} WITH {' '.join(opts)}")
        except asyncpg.PostgresError as e:
            raise RoleError(f"CREATE ROLE failed: {e}")

        for target in member_of:
            qtarget = await _qi(conn, target)
            try:
                await conn.execute(f"GRANT {qtarget} TO {qname}")
            except asyncpg.PostgresError as e:
                raise RoleError(f"Role created, but granting membership in {target!r} failed: {e}")


def _parse_connlimit(value) -> int:
    if value in (None, ""):
        return -1
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RoleError("Connection limit must be an integer (-1 for unlimited).")
    if n < -1:
        raise RoleError("Connection limit must be -1 (unlimited) or a non-negative integer.")
    return n


_VALID_UNTIL_RE = re.compile(
    r"^(infinity|\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?([+-]\d{2}(:\d{2})?)?)?)$", re.IGNORECASE
)


def _clean_valid_until(value) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    if not _VALID_UNTIL_RE.match(value):
        raise RoleError(
            f"Invalid 'valid until' value {value!r}: use 'infinity' or a timestamp like "
            "2027-01-31 or 2027-01-31 23:59:00+00."
        )
    return value


# ---------------------------------------------------------------- alter ---
async def alter_role(pool: asyncpg.Pool, rolname: str, payload: dict) -> None:
    rolname = _validate_ident(rolname, "role name")
    new_name = (payload.get("new_name") or "").strip() or None

    async with pool.acquire() as conn:
        self_user = await _current_user(conn)
        if rolname == self_user:
            if new_name or payload.get("can_login") is False:
                raise RoleError(
                    "Refusing to rename or revoke login from the role this app is currently "
                    "connected as -- that would lock the app's own connection pool out."
                )

        qname = await _qi(conn, rolname)

        if new_name:
            _validate_ident(new_name, "new role name")
            existing = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", new_name)
            if existing:
                raise RoleError(f"Role {new_name!r} already exists.")
            qnew = await _qi(conn, new_name)
            try:
                await conn.execute(f"ALTER ROLE {qname} RENAME TO {qnew}")
            except asyncpg.PostgresError as e:
                raise RoleError(f"Rename failed: {e}")
            rolname, qname = new_name, qnew

        opts = []
        if "can_login" in payload:
            opts.append("LOGIN" if payload["can_login"] else "NOLOGIN")
        if "superuser" in payload:
            opts.append("SUPERUSER" if payload["superuser"] else "NOSUPERUSER")
        if "createdb" in payload:
            opts.append("CREATEDB" if payload["createdb"] else "NOCREATEDB")
        if "createrole" in payload:
            opts.append("CREATEROLE" if payload["createrole"] else "NOCREATEROLE")
        if "replication" in payload:
            opts.append("REPLICATION" if payload["replication"] else "NOREPLICATION")
        if "bypassrls" in payload:
            opts.append("BYPASSRLS" if payload["bypassrls"] else "NOBYPASSRLS")
        if "inherit" in payload:
            opts.append("INHERIT" if payload["inherit"] else "NOINHERIT")
        if "connlimit" in payload:
            opts.append(f"CONNECTION LIMIT {_parse_connlimit(payload['connlimit'])}")
        if payload.get("password"):
            opts.append(f"PASSWORD {await _ql(conn, payload['password'])}")
        if "valid_until" in payload:
            vu = _clean_valid_until(payload.get("valid_until")) or "infinity"
            opts.append(f"VALID UNTIL {await _ql(conn, vu)}")

        if opts:
            try:
                await conn.execute(f"ALTER ROLE {qname} WITH {' '.join(opts)}")
            except asyncpg.PostgresError as e:
                raise RoleError(f"ALTER ROLE failed: {e}")

        if "member_of" in payload:
            desired = {_validate_ident(r, "role to grant membership in") for r in (payload["member_of"] or [])}
            current = {
                r["rolname"]
                for r in await conn.fetch(
                    """
                    SELECT pr.rolname FROM pg_auth_members am
                    JOIN pg_roles pr ON pr.oid = am.roleid
                    JOIN pg_roles me ON me.oid = am.member
                    WHERE me.rolname = $1
                    """,
                    rolname,
                )
            }
            for target in desired - current:
                qtarget = await _qi(conn, target)
                await conn.execute(f"GRANT {qtarget} TO {qname}")
            for target in current - desired:
                qtarget = await _qi(conn, target)
                await conn.execute(f"REVOKE {qtarget} FROM {qname}")


async def set_password(pool: asyncpg.Pool, rolname: str, password: str, valid_until: str | None = None) -> None:
    rolname = _validate_ident(rolname, "role name")
    if not password:
        raise RoleError("Password must not be empty.")
    async with pool.acquire() as conn:
        qname = await _qi(conn, rolname)
        opts = [f"PASSWORD {await _ql(conn, password)}"]
        vu = _clean_valid_until(valid_until)
        if vu:
            opts.append(f"VALID UNTIL {await _ql(conn, vu)}")
        try:
            await conn.execute(f"ALTER ROLE {qname} WITH {' '.join(opts)}")
        except asyncpg.PostgresError as e:
            raise RoleError(f"Setting password failed: {e}")


# ------------------------------------------------------------- sessions ---
async def terminate_sessions(pool: asyncpg.Pool, rolname: str) -> int:
    rolname = _validate_ident(rolname, "role name")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT pg_terminate_backend(pid) AS ok FROM pg_stat_activity "
            "WHERE usename = $1 AND pid <> pg_backend_pid()",
            rolname,
        )
    return sum(1 for r in rows if r["ok"])


# ----------------------------------------------------------------- drop ---
async def drop_role(pool: asyncpg.Pool, rolname: str, *, reassign_to: str | None = None,
                     terminate: bool = False) -> list[str]:
    rolname = _validate_ident(rolname, "role name")
    log: list[str] = []
    async with pool.acquire() as conn:
        self_user = await _current_user(conn)
        if rolname == self_user:
            raise RoleError(
                "Refusing to drop the role this app is currently connected as -- that would "
                "lock the app's own connection pool out."
            )
        qname = await _qi(conn, rolname)

        if terminate:
            n = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = $1 AND pid <> pg_backend_pid()",
                rolname,
            )
            if n:
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = $1 AND pid <> pg_backend_pid()",
                    rolname,
                )
                log.append(f"Terminated {n} active session(s) for {rolname!r}.")

        if reassign_to:
            reassign_to = _validate_ident(reassign_to, "reassignment target role")
            qtarget = await _qi(conn, reassign_to)
            try:
                await conn.execute(f"REASSIGN OWNED BY {qname} TO {qtarget}")
                log.append(f"Reassigned objects owned by {rolname!r} to {reassign_to!r} (current database only).")
                await conn.execute(f"DROP OWNED BY {qname}")
                log.append(f"Dropped remaining grants/privileges owned by {rolname!r} (current database only).")
            except asyncpg.PostgresError as e:
                raise RoleError(f"Cleanup before drop failed: {e}")

        try:
            await conn.execute(f"DROP ROLE {qname}")
        except asyncpg.PostgresError as e:
            raise RoleError(
                f"DROP ROLE failed: {e}. The role likely still owns objects or holds grants in "
                "another database, or has grants in this one -- retry with 'reassign owned "
                "objects' checked, or clean it up in the other database(s) first."
            )
        log.append(f"Dropped role {rolname!r}.")
    return log


# --------------------------------------------------------------- membership
async def grant_membership(pool: asyncpg.Pool, member_role: str, into_role: str, *, with_admin_option: bool = False) -> None:
    member_role = _validate_ident(member_role, "member role")
    into_role = _validate_ident(into_role, "target role")
    async with pool.acquire() as conn:
        qm = await _qi(conn, member_role)
        qi = await _qi(conn, into_role)
        suffix = " WITH ADMIN OPTION" if with_admin_option else ""
        try:
            await conn.execute(f"GRANT {qi} TO {qm}{suffix}")
        except asyncpg.PostgresError as e:
            raise RoleError(f"Granting membership failed: {e}")


async def revoke_membership(pool: asyncpg.Pool, member_role: str, from_role: str) -> None:
    member_role = _validate_ident(member_role, "member role")
    from_role = _validate_ident(from_role, "role to revoke membership from")
    async with pool.acquire() as conn:
        qm = await _qi(conn, member_role)
        qf = await _qi(conn, from_role)
        try:
            await conn.execute(f"REVOKE {qf} FROM {qm}")
        except asyncpg.PostgresError as e:
            raise RoleError(f"Revoking membership failed: {e}")


# ----------------------------------------------------- object privileges ---
_DB_PRIVS = {"CONNECT", "CREATE", "TEMPORARY", "TEMP"}
_SCHEMA_PRIVS = {"USAGE", "CREATE"}
_TABLE_PRIVS = {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
_SEQUENCE_PRIVS = {"USAGE", "SELECT", "UPDATE"}

_PRIV_SETS = {
    "database": _DB_PRIVS,
    "schema": _SCHEMA_PRIVS,
    "table": _TABLE_PRIVS,
    "sequence": _SEQUENCE_PRIVS,
    "all_tables_in_schema": _TABLE_PRIVS,
    "all_sequences_in_schema": _SEQUENCE_PRIVS,
}


def _priv_sql(object_type: str, privileges: list[str]) -> str:
    if object_type not in _PRIV_SETS:
        raise RoleError(f"Unknown object type {object_type!r}.")
    allowed = _PRIV_SETS[object_type]
    privs = [str(p).upper().strip() for p in (privileges or [])]
    if "ALL" in privs:
        return "ALL PRIVILEGES"
    bad = [p for p in privs if p not in allowed]
    if bad:
        raise RoleError(f"Invalid privilege(s) for {object_type}: {', '.join(bad)}. Allowed: {', '.join(sorted(allowed))}, ALL.")
    if not privs:
        raise RoleError("Select at least one privilege.")
    return ", ".join(privs)


async def _resolve_object_target(conn: asyncpg.Connection, object_type: str, object_name: str) -> str:
    object_name = (object_name or "").strip()
    if object_type == "database":
        dbname = await conn.fetchval("SELECT current_database()")
        return f"DATABASE {await _qi(conn, dbname)}"
    if object_type == "schema":
        _validate_ident(object_name, "schema name")
        return f"SCHEMA {await _qi(conn, object_name)}"
    if object_type in ("all_tables_in_schema", "all_sequences_in_schema"):
        _validate_ident(object_name, "schema name")
        kind = "TABLES" if object_type == "all_tables_in_schema" else "SEQUENCES"
        return f"ALL {kind} IN SCHEMA {await _qi(conn, object_name)}"
    if object_type in ("table", "sequence"):
        parts = object_name.split(".")
        if len(parts) != 2:
            raise RoleError(f"Provide the {object_type} as schema.name, e.g. public.employees.")
        schema, name = parts
        _validate_ident(schema, "schema name")
        _validate_ident(name, "object name")
        kw = "TABLE" if object_type == "table" else "SEQUENCE"
        return f"{kw} {await _qi(conn, schema)}.{await _qi(conn, name)}"
    raise RoleError(f"Unknown object type {object_type!r}.")


async def grant_privilege(pool: asyncpg.Pool, rolname: str, object_type: str, object_name: str,
                           privileges: list[str], *, with_grant_option: bool = False) -> None:
    rolname = _validate_ident(rolname, "role name")
    priv_sql = _priv_sql(object_type, privileges)
    async with pool.acquire() as conn:
        qrole = await _qi(conn, rolname)
        target_sql = await _resolve_object_target(conn, object_type, object_name)
        suffix = " WITH GRANT OPTION" if with_grant_option else ""
        try:
            await conn.execute(f"GRANT {priv_sql} ON {target_sql} TO {qrole}{suffix}")
        except asyncpg.PostgresError as e:
            raise RoleError(f"GRANT failed: {e}")


async def revoke_privilege(pool: asyncpg.Pool, rolname: str, object_type: str, object_name: str,
                            privileges: list[str], *, cascade: bool = False,
                            grant_option_only: bool = False) -> None:
    rolname = _validate_ident(rolname, "role name")
    priv_sql = _priv_sql(object_type, privileges)
    async with pool.acquire() as conn:
        qrole = await _qi(conn, rolname)
        target_sql = await _resolve_object_target(conn, object_type, object_name)
        prefix = "GRANT OPTION FOR " if grant_option_only else ""
        suffix = " CASCADE" if cascade else ""
        try:
            await conn.execute(f"REVOKE {prefix}{priv_sql} ON {target_sql} FROM {qrole}{suffix}")
        except asyncpg.PostgresError as e:
            raise RoleError(f"REVOKE failed: {e}")


async def list_grants(pool: asyncpg.Pool, rolname: str | None = None) -> list[dict]:
    """Unified view of object-level ACLs (database/schema/table/view/matview/
    sequence) in the *current* database, via aclexplode -- the same
    mechanism \\dp / \\ddp use under the hood. Optionally filtered to grants
    held by one role (PUBLIC grants, which affect everyone, are omitted from
    a filtered view to keep it focused on this role's own explicit grants)."""
    if rolname is not None:
        _validate_ident(rolname, "role name")
    query = """
        WITH acl AS (
            SELECT 'database' AS objtype, NULL::text AS schema, datname AS objname,
                   (aclexplode(datacl)).grantee AS grantee_oid,
                   (aclexplode(datacl)).privilege_type AS privilege,
                   (aclexplode(datacl)).is_grantable AS grantable
            FROM pg_database WHERE datname = current_database() AND datacl IS NOT NULL
            UNION ALL
            SELECT 'schema', NULL::text, nspname,
                   (aclexplode(nspacl)).grantee,
                   (aclexplode(nspacl)).privilege_type,
                   (aclexplode(nspacl)).is_grantable
            FROM pg_namespace WHERE nspacl IS NOT NULL AND nspname NOT LIKE 'pg\\_%'
            UNION ALL
            SELECT CASE relkind WHEN 'S' THEN 'sequence' ELSE 'table' END, nspname, relname,
                   (aclexplode(relacl)).grantee,
                   (aclexplode(relacl)).privilege_type,
                   (aclexplode(relacl)).is_grantable
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE relacl IS NOT NULL AND relkind IN ('r', 'v', 'm', 'S', 'f', 'p')
        )
        SELECT objtype, schema, objname,
               CASE WHEN grantee_oid = 0 THEN 'PUBLIC' ELSE grantee_oid::regrole::text END AS grantee,
               privilege, grantable
        FROM acl
        WHERE ($1::text IS NULL OR grantee_oid::regrole::text = $1)
        ORDER BY objtype, schema NULLS FIRST, objname, grantee, privilege
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, rolname)
    return [dict(r) for r in rows]
