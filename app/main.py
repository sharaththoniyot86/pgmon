from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

import asyncpg

from app import (
    ai, ai_settings, alerts, audit, auth, backup, backup_schedule, db, dbstore, metrics, migration,
    ora2pg_migration, partitioning, poller, repack, roles, scheduler, tuning, userdb, walmine,
)
from app.config import settings
from app.metrics import UnsafeQueryError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pgmon")

BASE_PATH = settings.BASE_PATH  # "" locally, "/pgmonitor" behind the ingress


@asynccontextmanager
async def lifespan(app: FastAPI):
    await userdb.init_db()
    await dbstore.init_db()
    await backup_schedule.init_db()
    await audit.init_db()
    await ai_settings.init_db()
    await backup.reconcile_on_startup()
    poller.start()
    scheduler.start()
    logger.info("pgmon started (base_path=%r, ai_enabled=%s)", BASE_PATH, await ai_settings.is_enabled())
    yield
    await scheduler.stop()
    await poller.stop()
    await db.close_all_pools()


app = FastAPI(title="pgmon - AI Postgres Monitor", lifespan=lifespan)
# NOTE: incoming request paths never carry BASE_PATH -- the ingress's
# stripPrefix middleware removes it before proxying here. Every internal
# route/mount below is therefore unprefixed; only URLs we *emit* (redirects,
# template links, static asset hrefs) get BASE_PATH re-added so the browser's
# next request round-trips through the ingress correctly.
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _hash_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:10]
    except FileNotFoundError:
        return "0"


# A content hash per static asset, computed once at process start and
# appended as a `?v=` query string wherever a template links to it. The
# no-store middleware below deliberately excludes /static/* so CSS/JS stay
# cacheable -- but that means the URL itself has to change whenever the
# content does, or a browser with an old cached copy keeps serving it
# forever after a redeploy. Restarting the pod (which every deploy does)
# recomputes this, so it's never stale for longer than one deploy.
ASSET_VERSIONS = {
    "css": _hash_file("app/static/css/style.css"),
    "js": _hash_file("app/static/js/app.js"),
    "vendor_chart": _hash_file("app/static/vendor/chart.umd.min.js"),
}


@app.middleware("http")
async def no_store_for_pages(request: Request, call_next):
    """Stop the browser's back/forward cache (bfcache) from replaying a
    rendered page instead of making a fresh request.

    Without this, a page load's response (an authenticated dashboard, or
    even the post-login redirect) can be restored byte-for-byte by the
    Back/Forward buttons with no request ever reaching the server -- so
    logging out, then hitting Back, could show the dashboard again with no
    session check at all. Static assets are excluded since they're safe
    (and worth) caching normally.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
    return response


async def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("base_path", BASE_PATH)
    ctx.setdefault("ai_enabled", await ai_settings.is_enabled())
    ctx.setdefault("asset_v", ASSET_VERSIONS)
    return templates.TemplateResponse(request, name, ctx)


async def _resolve_pool(db_id: str):
    try:
        return await db.get_pool(db_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No such registered database: {db_id!r}")
    except (OSError, asyncio.TimeoutError, asyncpg.PostgresError) as e:
        # A registered database being unreachable is an expected, everyday
        # state for a *monitoring* tool to encounter (that's the whole point
        # of watching it) -- every tab hitting this should show a clear
        # "unreachable" message, not an unhandled 500. Covers connection
        # refused/timed out, DNS failure (OSError's socket.gaierror is a
        # subclass), and auth/permission failures on the target server.
        raise HTTPException(status_code=503, detail=f"{db_id!r} is unreachable: {e}")


# ---------------------------------------------------------------- health ---
@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ------------------------------------------------------------------ auth ---
LOGIN_NOTICES = {"password-changed": "Password changed. Please sign in again."}


# ----------------------------------------------------- login rate limiting
# In-memory (same tradeoff as the alert cache / job store elsewhere in this
# app: lost on restart, which just means counters reset -- the safe
# direction to fail for a rate limiter). Keyed by IP+username, not IP
# alone, so one slow/shared NAT'd IP (common behind a corporate proxy)
# doesn't lock everyone out of every account at once, while still stopping
# a brute force against one specific account.
_LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 5
_login_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_attempt_key(request: Request, username: str) -> str:
    return f"{_client_ip(request)}:{username.strip().lower()}"


def _login_lockout_remaining(key: str) -> float | None:
    """Prunes attempts outside the window; returns seconds remaining if
    still locked out, else None."""
    now = time.time()
    attempts = [t for t in _login_failures.get(key, []) if now - t < _LOGIN_ATTEMPT_WINDOW_SECONDS]
    if attempts:
        _login_failures[key] = attempts
    else:
        _login_failures.pop(key, None)
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        return max(_LOGIN_ATTEMPT_WINDOW_SECONDS - (now - attempts[0]), 0)
    return None


def _record_login_failure(key: str) -> None:
    _login_failures.setdefault(key, []).append(time.time())


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    error = "Invalid username or password." if request.query_params.get("error") else None
    # Fixed, known messages only (never reflect the query param itself) --
    # this is a query string, so treat it as attacker-controllable input.
    notice = LOGIN_NOTICES.get(request.query_params.get("notice", ""))
    return await render(
        request, "login.html", error=error, notice=notice,
        pgmon_namespace=settings.PGMON_NAMESPACE, pgmon_deployment=settings.PGMON_DEPLOYMENT,
        deployment_mode=settings.DEPLOYMENT_MODE,
    )


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    key = _login_attempt_key(request, username)
    if _login_lockout_remaining(key) is not None:
        # Same generic error as a wrong password -- don't reveal to an
        # attacker whether they're rate-limited vs. just wrong, same spirit
        # as the existing timing-safe unknown-username handling.
        logger.warning("login rate-limited: %s", key)
        return RedirectResponse(url=f"{BASE_PATH}/login?error=1", status_code=303)
    if not await auth.verify_credentials(username, password):
        _record_login_failure(key)
        return RedirectResponse(url=f"{BASE_PATH}/login?error=1", status_code=303)
    _login_failures.pop(key, None)
    token = auth.create_session_token(username)
    resp = RedirectResponse(url=f"{BASE_PATH}/", status_code=303)
    resp.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax", path="/",
        max_age=settings.SESSION_MAX_AGE_SECONDS,
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, user: str = Depends(auth.require_user)):
    return await render(request, "account.html", username=user)


@app.post("/api/account/change-password")
async def api_change_password(payload: dict, user: str = Depends(auth.require_user)):
    p = payload or {}
    current_password = p.get("current_password") or ""
    new_password = p.get("new_password") or ""
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    if not await auth.verify_credentials(user, current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    await userdb.set_password(user, new_password)
    await audit.log(user, "account.change_password")
    return {"ok": True}


@app.get("/api/account/role")
async def api_account_role(user: str = Depends(auth.require_user)):
    """So the dashboard can hide admin-only controls for a viewer without
    guessing from 403s -- the source of truth is still every individual
    require_admin-gated endpoint, this is just for the UI to render sensibly.
    Also carries pgmon's own namespace/deployment name, so the Users tab's
    password-reset command is built from what's actually configured
    (PGMON_NAMESPACE/PGMON_DEPLOYMENT) rather than a hardcoded guess."""
    return {
        "username": user, "role": await userdb.get_role(user),
        "pgmon_namespace": settings.PGMON_NAMESPACE, "pgmon_deployment": settings.PGMON_DEPLOYMENT,
        "deployment_mode": settings.DEPLOYMENT_MODE,
    }


# ------------------------------------------------------------- user mgmt ---
@app.get("/api/users")
async def api_users_list(user: str = Depends(auth.require_admin)):
    return await userdb.list_users()


@app.post("/api/users")
async def api_users_create(payload: dict, user: str = Depends(auth.require_admin)):
    p = payload or {}
    username = (p.get("username") or "").strip()
    password = p.get("password") or ""
    role = p.get("role") or "viewer"
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="username is required and password must be at least 8 characters.")
    try:
        await userdb.create_user(username, password, role=role)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail=f"User {username!r} already exists.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "user.create", detail=f"{username} ({role})")
    return {"ok": True}


@app.post("/api/users/{username}/role")
async def api_users_set_role(username: str, payload: dict, user: str = Depends(auth.require_admin)):
    role = (payload or {}).get("role") or ""
    try:
        ok = await userdb.set_role(username, role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"No such user: {username}")
    await audit.log(user, "user.set_role", detail=f"{username} -> {role}")
    return {"ok": True}


@app.delete("/api/users/{username}")
async def api_users_delete(username: str, user: str = Depends(auth.require_admin)):
    if username == user:
        raise HTTPException(status_code=400, detail="Can't delete your own account while logged in as it.")
    try:
        ok = await userdb.delete_user(username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"No such user: {username}")
    await audit.log(user, "user.delete", detail=username)
    return {"ok": True}


# ------------------------------------------------------------- audit log ---
@app.get("/api/audit-log")
async def api_audit_log(limit: int = 200, user: str = Depends(auth.require_admin)):
    return await audit.list_entries(min(limit, 1000))


# --------------------------------------------------------- error handler ---
@app.exception_handler(HTTPException)
async def unauth_redirect(request: Request, exc: HTTPException):
    # Note: request.url.path is the path *after* the ingress has stripped
    # BASE_PATH (e.g. "/pgmonitor"), so it never carries that prefix here --
    # only outgoing redirects/links need BASE_PATH re-added.
    if exc.status_code == 401 and not request.url.path.startswith("/api"):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def _database_connectivity_handler(request: Request, exc: Exception) -> JSONResponse:
    # A safety net _resolve_pool's own try/except doesn't fully cover: that
    # one only catches a *new* pool failing to connect, but db.get_pool()
    # returns an *existing* pool instantly from cache with no liveness
    # check -- so if a database goes unreachable sometime after it was
    # first successfully connected to, the actual ConnectionRefusedError/
    # timeout/Postgres error surfaces later, inside whichever metrics.*
    # function's own pool.acquire()/query call happens to run, none of
    # which are individually wrapped for this. Catching it once here,
    # globally, covers every route uniformly instead of hoping each of the
    # 100+ handlers remembers to. A registered database going unreachable
    # is an ordinary, expected state for a *monitoring* tool -- this earns
    # a clean 503, not a raw unhandled 500.
    logger.warning("%s: database connectivity error: %s", request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": f"Database error: {exc}"})


for _exc_type in (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
    app.add_exception_handler(_exc_type, _database_connectivity_handler)


# ------------------------------------------------------------- dashboard ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(auth.require_user)):
    return await render(request, "dashboard.html", username=user)


# ----------------------------------------------------------------- API -----
@app.get("/api/overview")
async def api_overview(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_overview(await _resolve_pool(db_id))


@app.get("/api/settings")
async def api_settings(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_settings(await _resolve_pool(db_id))


@app.post("/api/settings/optimize-advice")
async def api_settings_optimize_advice(payload: dict, user: str = Depends(auth.require_user)):
    # Pure calculation from the hardware specs given -- no database
    # connection needed, so no db_id here (see app/tuning.py).
    p = payload or {}
    try:
        cpu_count = int(p.get("cpu_count"))
        ram_mb = round(float(p.get("ram_gb")) * 1024)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="cpu_count and ram_gb are required and must be numbers.")
    max_conn = p.get("max_connections")
    try:
        max_conn = int(max_conn) if max_conn not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_connections must be a whole number.")
    try:
        recommendations = tuning.recommend_settings(
            cpu_count, ram_mb, p.get("db_type") or "web", p.get("storage") or "ssd", max_conn,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"recommendations": recommendations}


@app.post("/api/settings/{name}")
async def api_set_setting(
    name: str, payload: dict, db_id: str, user: str = Depends(auth.require_admin)
):
    pool = await _resolve_pool(db_id)
    reset = bool((payload or {}).get("reset", False))
    try:
        result = await metrics.set_parameter(pool, name, value=(payload or {}).get("value"), reset=reset)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "settings.reset" if reset else "settings.set",
                     detail=name if reset else f"{name} = {(payload or {}).get('value')!r}", db_id=db_id)
    return result


@app.get("/api/alerts")
async def api_alerts(db_id: str, user: str = Depends(auth.require_user)):
    return await alerts.get_alerts(await _resolve_pool(db_id), db_id)


@app.get("/api/connections")
async def api_connections(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_connections(await _resolve_pool(db_id))


@app.get("/api/locks")
async def api_locks(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_locks(await _resolve_pool(db_id))


@app.get("/api/wraparound")
async def api_wraparound(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_wraparound_info(await _resolve_pool(db_id))


@app.get("/api/replication")
async def api_replication(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_replication_info(await _resolve_pool(db_id))


@app.post("/api/connections/{pid}/terminate")
async def api_terminate_connection(
    pid: int, db_id: str, user: str = Depends(auth.require_admin)
):
    pool = await _resolve_pool(db_id)
    try:
        terminated = await metrics.terminate_backend(pool, pid)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not terminated:
        raise HTTPException(status_code=404, detail=f"No active backend with pid {pid}.")
    await audit.log(user, "connection.terminate", detail=f"pid {pid}", db_id=db_id)
    return {"ok": True, "pid": pid}


@app.get("/api/queries")
async def api_queries(
    db_id: str, limit: int = 25, order_by: str = "total_time",
    all_databases: bool = False, include_monitoring: bool = False,
    user: str = Depends(auth.require_user),
):
    try:
        return await metrics.get_top_queries(
            await _resolve_pool(db_id), limit=limit, order_by=order_by,
            all_databases=all_databases, include_monitoring=include_monitoring,
        )
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/queries/status")
async def api_queries_status(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_pg_stat_statements_status(await _resolve_pool(db_id))


@app.post("/api/queries/install-extension")
async def api_queries_install_extension(db_id: str, user: str = Depends(auth.require_admin)):
    try:
        result = await metrics.install_pg_stat_statements(await _resolve_pool(db_id))
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "extension.enable", detail="pg_stat_statements", db_id=db_id)
    return result


@app.post("/api/queries/reset")
async def api_queries_reset(db_id: str, user: str = Depends(auth.require_admin)):
    try:
        await metrics.reset_query_stats(await _resolve_pool(db_id))
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "queries.reset_stats", db_id=db_id)
    return {"ok": True}


@app.post("/api/queries/plan")
async def api_queries_plan(payload: dict, db_id: str, user: str = Depends(auth.require_user)):
    # No AI required (unlike /api/ai/explain-query) -- a plain EXPLAIN,
    # available even when ANTHROPIC_API_KEY isn't configured. analyze=True
    # actually runs the query, but get_query_plan restricts that to
    # SELECT/WITH, same as the AI path.
    p = payload or {}
    try:
        return await metrics.get_query_plan(
            await _resolve_pool(db_id), p.get("query", ""), analyze=bool(p.get("analyze")),
        )
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/queries/tuning-advice")
async def api_queries_tuning_advice(payload: dict, db_id: str, user: str = Depends(auth.require_user)):
    p = payload or {}
    try:
        return await metrics.get_tuning_advice(
            await _resolve_pool(db_id), p.get("query", ""), analyze=bool(p.get("analyze")),
        )
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/queries/hypopg-status")
async def api_hypopg_status(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_hypopg_status(await _resolve_pool(db_id))


@app.post("/api/queries/install-hypopg")
async def api_install_hypopg(db_id: str, user: str = Depends(auth.require_admin)):
    try:
        result = await metrics.install_hypopg(await _resolve_pool(db_id))
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "extension.enable", detail="hypopg", db_id=db_id)
    return result


@app.get("/api/tables")
async def api_tables(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_table_stats(await _resolve_pool(db_id))


@app.get("/api/indexes")
async def api_indexes(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_index_stats(await _resolve_pool(db_id))


@app.get("/api/schemas")
async def api_schemas(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_schemas(await _resolve_pool(db_id))


@app.get("/api/schemas/{schema_name}/objects")
async def api_schema_objects(
    schema_name: str, db_id: str, user: str = Depends(auth.require_user)
):
    return await metrics.get_schema_objects(await _resolve_pool(db_id), schema_name)


@app.get("/api/schemas/{schema_name}/objects/{object_name}")
async def api_schema_object_detail(
    schema_name: str, object_name: str, db_id: str, user: str = Depends(auth.require_user)
):
    detail = await metrics.get_object_detail(await _resolve_pool(db_id), schema_name, object_name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"No such object: {schema_name}.{object_name}")
    return detail


@app.get("/api/schemas/{schema_name}/objects/{object_name}/rows")
async def api_schema_object_rows(
    schema_name: str, object_name: str,
    limit: int = 50, offset: int = 0, order_by: str | None = None, order_dir: str = "asc",
    *, db_id: str, user: str = Depends(auth.require_user),
):
    pool = await _resolve_pool(db_id)
    try:
        return await metrics.get_object_rows(
            pool, schema_name, object_name, limit=limit, offset=offset, order_by=order_by, order_dir=order_dir
        )
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/schemas/{schema_name}/objects/{object_name}/partitions")
async def api_create_partition(
    schema_name: str, object_name: str, payload: dict,
    db_id: str, user: str = Depends(auth.require_admin),
):
    p = payload or {}
    name = p.get("name") or ""
    bound = p.get("bound") or ""
    if not name:
        raise HTTPException(status_code=400, detail="name is required.")
    try:
        await metrics.create_partition(await _resolve_pool(db_id), schema_name, object_name, name, bound)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "schema.create_partition", detail=f"{schema_name}.{name} of {object_name}: {bound}", db_id=db_id)
    return {"ok": True}


@app.delete("/api/schemas/{schema_name}/objects/{object_name}/partitions/{partition_name}")
async def api_detach_partition(
    schema_name: str, object_name: str, partition_name: str,
    db_id: str, user: str = Depends(auth.require_admin),
):
    try:
        await metrics.detach_partition(await _resolve_pool(db_id), schema_name, object_name, partition_name)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "schema.detach_partition", detail=f"{schema_name}.{partition_name} from {object_name}", db_id=db_id)
    return {"ok": True}


@app.get("/api/history")
async def api_history(db_id: str, hours: int = 6, user: str = Depends(auth.require_user)):
    return await metrics.get_history(await _resolve_pool(db_id), hours=hours)


@app.get("/api/security")
async def api_security(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_security_info(await _resolve_pool(db_id))


@app.post("/api/security/hba-rules")
async def api_security_add_hba_rule(db_id: str, payload: dict, user: str = Depends(auth.require_admin)):
    p = payload or {}
    try:
        result = await metrics.add_hba_rule(
            await _resolve_pool(db_id), p.get("type"), p.get("database"), p.get("user_name"),
            p.get("address"), p.get("auth_method"),
        )
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "security.hba_rule.add", detail=result.get("line"), db_id=db_id)
    return result


@app.put("/api/security/hba-rules/{line_number}")
async def api_security_edit_hba_rule(line_number: int, db_id: str, payload: dict, user: str = Depends(auth.require_admin)):
    p = payload or {}
    try:
        result = await metrics.edit_hba_rule(
            await _resolve_pool(db_id), line_number, p.get("expected") or {},
            p.get("type"), p.get("database"), p.get("user_name"), p.get("address"), p.get("auth_method"),
        )
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "security.hba_rule.edit", detail=f"line {line_number}", db_id=db_id)
    return result


@app.delete("/api/security/hba-rules/{line_number}")
async def api_security_delete_hba_rule(line_number: int, db_id: str, payload: dict, user: str = Depends(auth.require_admin)):
    p = payload or {}
    try:
        result = await metrics.delete_hba_rule(await _resolve_pool(db_id), line_number, p.get("expected") or {})
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "security.hba_rule.delete", detail=f"line {line_number}", db_id=db_id)
    return result


# ---------------------------------------------------------------- extensions ---
@app.get("/api/extensions")
async def api_extensions(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_extension_catalog_status(await _resolve_pool(db_id))


@app.post("/api/extensions/{name}/enable")
async def api_extensions_enable(name: str, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        await metrics.enable_extension(await _resolve_pool(db_id), name)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "extension.enable", detail=name, db_id=db_id)
    return {"ok": True}


@app.post("/api/extensions/{name}/disable")
async def api_extensions_disable(name: str, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        await metrics.disable_extension(await _resolve_pool(db_id), name)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "extension.disable", detail=name, db_id=db_id)
    return {"ok": True}


@app.post("/api/extensions/{name}/update")
async def api_extensions_update(name: str, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        result = await metrics.update_extension(await _resolve_pool(db_id), name)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result["updated"]:
        await audit.log(user, "extension.update", detail=f"{name} {result['from_version']} -> {result['to_version']}", db_id=db_id)
    return result


# ------------------------------------------------- roles & user management ---
@app.post("/api/roles")
async def api_roles_create(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        await roles.create_role(await _resolve_pool(db_id), payload or {})
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.create", detail=(payload or {}).get("rolname"), db_id=db_id)
    return {"ok": True}


@app.get("/api/roles/{rolname}")
async def api_roles_detail(rolname: str, db_id: str, user: str = Depends(auth.require_user)):
    try:
        return await roles.get_role_detail(await _resolve_pool(db_id), rolname)
    except roles.RoleError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put("/api/roles/{rolname}")
async def api_roles_alter(rolname: str, payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        await roles.alter_role(await _resolve_pool(db_id), rolname, payload or {})
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.alter", detail=f"{rolname}: {payload}", db_id=db_id)
    return {"ok": True}


@app.delete("/api/roles/{rolname}")
async def api_roles_drop(rolname: str, reassign_to: str | None = None, terminate: bool = False,
                          *, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        log = await roles.drop_role(await _resolve_pool(db_id), rolname, reassign_to=reassign_to or None, terminate=terminate)
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.drop", detail=rolname, db_id=db_id)
    return {"ok": True, "log": log}


@app.post("/api/roles/{rolname}/password")
async def api_roles_set_password(rolname: str, payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    try:
        await roles.set_password(await _resolve_pool(db_id), rolname, p.get("password") or "", valid_until=p.get("valid_until"))
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.set_password", detail=rolname, db_id=db_id)
    return {"ok": True}


@app.post("/api/roles/{rolname}/terminate-sessions")
async def api_roles_terminate_sessions(rolname: str, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        n = await roles.terminate_sessions(await _resolve_pool(db_id), rolname)
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.terminate_sessions", detail=f"{rolname} ({n} session(s))", db_id=db_id)
    return {"ok": True, "terminated": n}


@app.post("/api/roles/{rolname}/membership")
async def api_roles_grant_membership(rolname: str, payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    target_role = p.get("target_role")
    if not target_role:
        raise HTTPException(status_code=400, detail="target_role is required.")
    try:
        await roles.grant_membership(await _resolve_pool(db_id), rolname, target_role, with_admin_option=bool(p.get("with_admin_option")))
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.grant_membership", detail=f"{rolname} -> {target_role}", db_id=db_id)
    return {"ok": True}


@app.delete("/api/roles/{rolname}/membership/{target_role}")
async def api_roles_revoke_membership(rolname: str, target_role: str, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        await roles.revoke_membership(await _resolve_pool(db_id), rolname, target_role)
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.revoke_membership", detail=f"{rolname} -> {target_role}", db_id=db_id)
    return {"ok": True}


@app.get("/api/roles/{rolname}/grants")
async def api_roles_list_grants(rolname: str, db_id: str, user: str = Depends(auth.require_user)):
    try:
        return await roles.list_grants(await _resolve_pool(db_id), rolname)
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/roles/{rolname}/grants")
async def api_roles_grant_privilege(rolname: str, payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    try:
        await roles.grant_privilege(
            await _resolve_pool(db_id), rolname,
            object_type=p.get("object_type") or "", object_name=p.get("object_name") or "",
            privileges=p.get("privileges") or [], with_grant_option=bool(p.get("with_grant_option")),
        )
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.grant_privilege", detail=f"{rolname}: {p.get('privileges')} on {p.get('object_name')}", db_id=db_id)
    return {"ok": True}


@app.delete("/api/roles/{rolname}/grants")
async def api_roles_revoke_privilege(rolname: str, payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    try:
        await roles.revoke_privilege(
            await _resolve_pool(db_id), rolname,
            object_type=p.get("object_type") or "", object_name=p.get("object_name") or "",
            privileges=p.get("privileges") or [], cascade=bool(p.get("cascade")),
            grant_option_only=bool(p.get("grant_option_only")),
        )
    except roles.RoleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "role.revoke_privilege", detail=f"{rolname}: {p.get('privileges')} on {p.get('object_name')}", db_id=db_id)
    return {"ok": True}


@app.get("/api/backup")
async def api_backup(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_backup_info(await _resolve_pool(db_id))


# --------------------------------------------------- backup & recovery actions
@app.get("/api/backup/list")
async def api_backup_list(db_id: str, user: str = Depends(auth.require_user)):
    try:
        return backup.list_backups(db_id)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/backup/run")
async def api_backup_run(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    conn_info = await db.get_conn_info(db_id)
    try:
        job_id = await backup.start_backup(
            db_id, conn_info,
            backup_type=p.get("backup_type") or "logical",
            incremental_base=p.get("incremental_base"),
            scope_type=p.get("scope_type") or "whole",
            scope=p.get("scope") or [],
        )
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.run", detail=p.get("backup_type") or "logical", db_id=db_id)
    return {"job_id": job_id}


@app.post("/api/backup/import-logical")
async def api_backup_import_logical(
    db_id: str, file: UploadFile = File(...), user: str = Depends(auth.require_admin),
):
    try:
        result = await backup.import_logical_backup(db_id, file.filename or "import.dump", file.file)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.import_logical", detail=result["filename"], db_id=db_id)
    return result


@app.post("/api/backup/import-physical")
async def api_backup_import_physical(
    db_id: str, file: UploadFile = File(...), user: str = Depends(auth.require_admin),
):
    try:
        result = await backup.import_physical_backup(db_id, file.filename or "import.tar.gz", file.file)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.import_physical", detail=result["filename"], db_id=db_id)
    return result


@app.post("/api/backup/restore")
async def api_backup_restore(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    filename = p.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required.")
    conn_info = await db.get_conn_info(db_id)
    try:
        job_id = await backup.start_restore(
            db_id, conn_info, filename, target_dbname=p.get("target_dbname") or None,
            scope_type=p.get("scope_type") or "whole", scope=p.get("scope") or [],
            options=p.get("options") or {},
        )
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.restore", detail=filename, db_id=db_id)
    return {"job_id": job_id}


@app.post("/api/backup/import-roles")
async def api_backup_import_roles(
    db_id: str, file: UploadFile = File(...), user: str = Depends(auth.require_admin),
):
    """Applies a pg_dumpall --roles-only/--globals-only script -- roles are
    cluster-global, so db_id here is only "which registered database's
    connection to borrow," not a scope restriction."""
    conn_info = await db.get_conn_info(db_id)
    try:
        job_id = await backup.start_import_roles(conn_info, file.file)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.import_roles", detail=file.filename or "", db_id=db_id)
    return {"job_id": job_id}


@app.get("/api/backup/{filename}/tables")
async def api_backup_tables(filename: str, db_id: str, user: str = Depends(auth.require_user)):
    """Every table actually inside a logical backup file -- powers the
    restore modal's "selected schemas/tables" scope picker."""
    try:
        tables = await backup.list_backup_tables(db_id, filename)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"tables": tables}


@app.get("/api/backup/physical-restore-available")
async def api_physical_restore_available(user: str = Depends(auth.require_user)):
    return {"ok": backup.is_physical_restore_configured()}


@app.post("/api/backup/physical-restore")
async def api_backup_physical_restore(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    filename = p.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required.")
    try:
        job_id = await backup.start_physical_restore(db_id, filename, tablespace_mapping=p.get("tablespace_mapping") or {})
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.physical_restore", detail=filename, db_id=db_id)
    return {"job_id": job_id}


@app.post("/api/backup/new-instance-restore")
async def api_backup_new_instance_restore(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    filename = p.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required.")
    try:
        job_id = await backup.start_new_instance_restore(
            db_id, filename, recovery_target=p.get("recovery_target") or None,
            tablespace_mapping=p.get("tablespace_mapping") or {},
        )
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rt = p.get("recovery_target") or {}
    await audit.log(user, "backup.new_instance_restore",
                     detail=f"{filename}" + (f" -> {rt.get('type')}={rt.get('value')}" if rt else ""), db_id=db_id)
    return {"job_id": job_id}


@app.post("/api/backup/temp-instances/{name}/promote")
async def api_backup_promote_temp_instance(name: str, db_id: str, user: str = Depends(auth.require_admin)):
    conn_info = await db.get_conn_info(db_id)
    try:
        await backup.promote_temp_instance(conn_info, name)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.temp_instance.promote", detail=name, db_id=db_id)
    return {"ok": True}


@app.get("/api/backup/pitr-available")
async def api_pitr_available(user: str = Depends(auth.require_user)):
    return {"ok": backup.is_pitr_configured()}


# --------------------------------------------------------------- WAL mining
@app.post("/api/backup/walmine/backup")
async def api_walmine_backup(payload: dict, db_id: str, user: str = Depends(auth.require_user)):
    p = payload or {}
    filename = p.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required.")
    conn_info = await db.get_conn_info(db_id)
    try:
        job_id = await walmine.start_mine_backup_wal(
            db_id, filename, conn_info,
            xid=p.get("xid") or None, rmgr=p.get("rmgr") or None, table_contains=p.get("table_contains") or None,
        )
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.walmine", detail=filename, db_id=db_id)
    return {"job_id": job_id}


@app.post("/api/backup/walmine/live")
async def api_walmine_live(payload: dict, db_id: str, user: str = Depends(auth.require_user)):
    p = payload or {}
    conn_info = await db.get_conn_info(db_id)
    try:
        job_id = await walmine.start_mine_live_wal_archive(
            conn_info, segment_limit=p.get("segment_limit") or 10,
            xid=p.get("xid") or None, rmgr=p.get("rmgr") or None, table_contains=p.get("table_contains") or None,
        )
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.walmine", detail="live archive", db_id=db_id)
    return {"job_id": job_id}


# ------------------------------------------------------- backup schedules
@app.get("/api/backup/schedules")
async def api_backup_schedules_list(db_id: str, user: str = Depends(auth.require_user)):
    return await backup_schedule.list_schedules(db_id)


@app.post("/api/backup/schedules")
async def api_backup_schedules_create(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    try:
        result = await backup_schedule.create_schedule(
            db_id, p.get("backup_type"), p.get("frequency"), int(p.get("retention_days", 0)),
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.schedule.create", detail=f"{p.get('backup_type')}/{p.get('frequency')}", db_id=db_id)
    return result


@app.post("/api/backup/schedules/{schedule_id}/enabled")
async def api_backup_schedules_set_enabled(schedule_id: int, payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    enabled = bool((payload or {}).get("enabled"))
    ok = await backup_schedule.set_enabled(schedule_id, enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="No such schedule.")
    await audit.log(user, "backup.schedule.set_enabled", detail=f"{schedule_id} -> {enabled}", db_id=db_id)
    return {"ok": True}


@app.delete("/api/backup/schedules/{schedule_id}")
async def api_backup_schedules_delete(schedule_id: int, db_id: str, user: str = Depends(auth.require_admin)):
    ok = await backup_schedule.delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="No such schedule.")
    await audit.log(user, "backup.schedule.delete", detail=str(schedule_id), db_id=db_id)
    return {"ok": True}


@app.get("/api/backup/temp-instances")
async def api_backup_temp_instances(user: str = Depends(auth.require_user)):
    return await backup.list_temp_instances()


@app.delete("/api/backup/temp-instances/{name}")
async def api_backup_delete_temp_instance(name: str, user: str = Depends(auth.require_admin)):
    try:
        await backup.delete_temp_instance(name)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.temp_instance.delete", detail=name)
    return {"ok": True}


@app.get("/api/backup/jobs/{job_id}")
async def api_backup_job(job_id: str, user: str = Depends(auth.require_user)):
    job = backup.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such backup job.")
    return job


@app.get("/api/backup/download/{filename}")
async def api_backup_download(filename: str, db_id: str, user: str = Depends(auth.require_user)):
    # A logical backup is already a single file; a physical/incremental one
    # is a directory, tarred into a temp file on demand -- cleaned up via
    # BackgroundTask once the response has actually been sent.
    try:
        path, is_temp = await backup.prepare_download(db_id, filename)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    download_name = filename if not is_temp else f"{filename}.tar.gz"
    background = BackgroundTask(os.remove, path) if is_temp else None
    return FileResponse(path, filename=download_name, media_type="application/octet-stream", background=background)


@app.delete("/api/backup/{filename}")
async def api_backup_delete(filename: str, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        backup.delete_backup(db_id, filename)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.delete", detail=filename, db_id=db_id)
    return {"ok": True}


@app.post("/api/backup/{filename}/verify")
async def api_backup_verify(filename: str, db_id: str, user: str = Depends(auth.require_admin)):
    try:
        job_id = await backup.start_verify_backup(db_id, filename)
    except backup.BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "backup.verify", detail=filename, db_id=db_id)
    return {"job_id": job_id}


@app.get("/api/administration")
async def api_administration(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_administration_info(await _resolve_pool(db_id))


@app.get("/api/administration/partitioning-advisor")
async def api_partitioning_advisor(
    db_id: str, min_rows: int = 10_000, min_bytes: int = 10 * 1024 * 1024, user: str = Depends(auth.require_user),
):
    # Read-only -- every suggestion here is a recommendation (a DDL
    # skeleton to review and adapt), nothing is actually created, same
    # viewer-safe reasoning as the Index/Tuning Advisor.
    return await metrics.get_partitioning_advisor(await _resolve_pool(db_id), min_rows=min_rows, min_bytes=min_bytes)


@app.post("/api/administration/partitioning-advisor/check")
async def api_partitioning_check(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    for field in ("schema", "table", "column"):
        if not p.get(field):
            raise HTTPException(status_code=400, detail=f"{field} is required.")
    result = await metrics.check_partitioning_prerequisites(
        await _resolve_pool(db_id), p["schema"], p["table"], p["column"],
    )
    return {"can_convert": not result["blocking"], **result}


@app.post("/api/administration/partitioning-advisor/convert")
async def api_partitioning_convert(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    for field in ("schema", "table", "technique", "column"):
        if not p.get(field):
            raise HTTPException(status_code=400, detail=f"{field} is required.")
    if p["technique"] not in ("RANGE", "LIST", "HASH"):
        raise HTTPException(status_code=400, detail=f"Unknown technique: {p['technique']!r}")
    pool = await _resolve_pool(db_id)
    result = await metrics.check_partitioning_prerequisites(pool, p["schema"], p["table"], p["column"])
    if result["blocking"]:
        raise HTTPException(status_code=400, detail="Refusing to convert automatically:\n- " + "\n- ".join(result["blocking"]))
    job_id = await partitioning.start_conversion(pool, p["schema"], p["table"], p["technique"], p["column"])
    await audit.log(
        user, "administration.partition_convert",
        detail=f"{p['schema']}.{p['table']} -> {p['technique']}({p['column']})", db_id=db_id,
    )
    return {"job_id": job_id}


@app.get("/api/administration/partitioning-jobs/{job_id}")
async def api_partitioning_job(job_id: str, user: str = Depends(auth.require_user)):
    job = partitioning.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job


@app.post("/api/administration/vacuum")
async def api_administration_vacuum(
    payload: dict, db_id: str, user: str = Depends(auth.require_admin)
):
    schema_name = (payload or {}).get("schema", "public")
    table_name = (payload or {}).get("table", "")
    if not table_name:
        raise HTTPException(status_code=400, detail="table is required.")
    pool = await _resolve_pool(db_id)
    try:
        result = await metrics.vacuum_table(
            pool,
            schema_name,
            table_name,
            analyze=bool((payload or {}).get("analyze", True)),
            freeze=bool((payload or {}).get("freeze", False)),
            full=bool((payload or {}).get("full", False)),
        )
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    opts = ", ".join(o for o, on in (("analyze", payload.get("analyze", True)), ("freeze", payload.get("freeze")),
                                      ("full", payload.get("full"))) if on)
    await audit.log(user, "administration.vacuum", detail=f"{schema_name}.{table_name} ({opts})", db_id=db_id)
    return result


@app.get("/api/administration/repack-status")
async def api_repack_status(db_id: str, user: str = Depends(auth.require_user)):
    return await repack.get_repack_status(await _resolve_pool(db_id))


@app.post("/api/administration/install-repack")
async def api_install_repack(db_id: str, user: str = Depends(auth.require_admin)):
    try:
        result = await repack.install_repack(await _resolve_pool(db_id))
    except repack.RepackError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "extension.enable", detail="pg_repack", db_id=db_id)
    return result


@app.post("/api/administration/repack")
async def api_administration_repack(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    schema_name = (payload or {}).get("schema", "public")
    table_name = (payload or {}).get("table", "")
    if not table_name:
        raise HTTPException(status_code=400, detail="table is required.")
    conn_info = await db.get_conn_info(db_id)
    job_id = await repack.start_repack(conn_info, schema_name, table_name)
    await audit.log(user, "administration.repack", detail=f"{schema_name}.{table_name}", db_id=db_id)
    return {"job_id": job_id}


@app.get("/api/administration/repack-jobs/{job_id}")
async def api_repack_job(job_id: str, user: str = Depends(auth.require_user)):
    job = repack.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such repack job.")
    return job


@app.get("/api/performance")
async def api_performance(db_id: str, user: str = Depends(auth.require_user)):
    return await metrics.get_performance_overview(await _resolve_pool(db_id))


@app.get("/api/performance/io-history")
async def api_performance_io_history(
    hours: float = 1, start: str | None = None, end: str | None = None,
    *, db_id: str, user: str = Depends(auth.require_user),
):
    try:
        return await metrics.get_io_history(await _resolve_pool(db_id), hours=hours, start=start, end=end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid start/end: {e}")


@app.get("/api/performance/wait-events-history")
async def api_performance_wait_events_history(
    hours: float = 1, start: str | None = None, end: str | None = None,
    *, db_id: str, user: str = Depends(auth.require_user),
):
    try:
        return await metrics.get_wait_event_history(await _resolve_pool(db_id), hours=hours, start=start, end=end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid start/end: {e}")


# ------------------------------------------------------------- databases ---
_EMPTY_QUICK_STATS = {"connection_count": None, "cache_hit_ratio_pct": None, "tps": None, "uptime_seconds": None, "version": None}


async def _db_summary(db_id: str) -> dict:
    """Status + list-card stats for one database: ping (up/down + latency),
    and -- only if reachable -- live connection count, cache hit ratio, and
    transactions/sec."""
    try:
        pool = await db.get_pool(db_id)
    except Exception as e:
        return {"status": "down", "latency_ms": None, "error": str(e)[:200], **_EMPTY_QUICK_STATS,
                "alerts": poller.get_cached_alert_summary(db_id)}

    ping_result = await metrics.ping(pool)
    if ping_result["status"] != "up":
        return {**ping_result, **_EMPTY_QUICK_STATS, "alerts": poller.get_cached_alert_summary(db_id)}

    try:
        quick_stats, tps = await asyncio.gather(metrics.get_quick_stats(pool), metrics.get_tps(pool))
    except Exception:
        return {**ping_result, **_EMPTY_QUICK_STATS, "alerts": poller.get_cached_alert_summary(db_id)}
    return {**ping_result, **quick_stats, "tps": tps, "alerts": poller.get_cached_alert_summary(db_id)}


@app.get("/api/databases")
async def api_list_databases(user: str = Depends(auth.require_user)):
    entries = await dbstore.list_databases()
    summaries = await asyncio.gather(*(_db_summary(e["id"]) for e in entries))
    for entry, summary in zip(entries, summaries):
        entry.update(summary)
    return entries


@app.post("/api/databases")
async def api_add_database(payload: dict, user: str = Depends(auth.require_admin)):
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    dbname = (payload.get("dbname") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    try:
        port = int(payload.get("port") or 5432)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="port must be a number")

    if not all([name, host, dbname, username]):
        raise HTTPException(status_code=400, detail="name, host, dbname, and username are required")

    conn_info = db.ConnInfo(host=host, port=port, dbname=dbname, username=username, password=password)
    try:
        await db.test_connection(conn_info)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not connect: {e}")

    try:
        record = await dbstore.add_database(name, host, port, dbname, username, password)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"A database named '{name}' is already registered.")

    # Warm the pool now so schema bootstrap errors surface immediately rather
    # than silently on the next poll.
    try:
        pool = await db.get_pool(record["id"])
    except Exception as e:
        await dbstore.delete_database(record["id"])
        raise HTTPException(status_code=400, detail=f"Connected, but setup failed: {e}")

    if payload.get("enable_replication_hba"):
        record["hba_note"] = await metrics.ensure_replication_hba_rule(pool, username)

    await audit.log(user, "database.add", detail=f"{name} ({host}:{port}/{dbname})")
    return record


@app.post("/api/databases/test")
async def api_test_database(payload: dict, user: str = Depends(auth.require_user)):
    host = (payload.get("host") or "").strip()
    dbname = (payload.get("dbname") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    try:
        port = int(payload.get("port") or 5432)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="port must be a number")
    conn_info = db.ConnInfo(host=host, port=port, dbname=dbname, username=username, password=password)
    try:
        await db.test_connection(conn_info)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not connect: {e}")
    return {"ok": True}


@app.delete("/api/databases/{db_id}")
async def api_delete_database(db_id: str, user: str = Depends(auth.require_admin)):
    await db.drop_pool(db_id)
    poller.drop_cached_alerts(db_id)
    deleted = await dbstore.delete_database(db_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No such registered database.")
    await audit.log(user, "database.delete", db_id=db_id)
    return {"ok": True}


# ------------------------------------------------------------------- AI ----
async def _require_ai():
    if not await ai_settings.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="AI features are not configured. Set one up from the AI Assistant tab, or set ANTHROPIC_API_KEY.",
        )


@app.get("/api/ai/settings")
async def api_ai_settings_get(user: str = Depends(auth.require_user)):
    return await ai_settings.get_public()


@app.post("/api/ai/settings")
async def api_ai_settings_save(payload: dict, user: str = Depends(auth.require_admin)):
    p = payload or {}
    provider = p.get("provider") or "anthropic"
    if provider not in ai_settings.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    await ai_settings.save(
        provider, api_key=p.get("api_key") or None, base_url=p.get("base_url") or None, model=p.get("model") or None,
    )
    await audit.log(user, "ai.settings.save", detail=f"provider={provider}")
    return await ai_settings.get_public()


@app.post("/api/ai/settings/test")
async def api_ai_settings_test(payload: dict, user: str = Depends(auth.require_admin)):
    p = payload or {}
    provider = p.get("provider") or "anthropic"
    if provider not in ai_settings.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")
    api_key = p.get("api_key") or None
    if not api_key:
        # Testing an already-saved key (the field was left blank on purpose
        # to keep it) -- reuse it rather than asking the key to be retyped
        # just to run a test.
        current = await ai_settings.get_effective()
        if current["provider"] == provider:
            api_key = current["api_key"]
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required to test.")
    try:
        reply = await ai.test_connection(provider, api_key, p.get("base_url") or None, p.get("model") or None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Test failed: {e}")
    return {"ok": True, "reply": reply}


@app.delete("/api/ai/settings")
async def api_ai_settings_clear(user: str = Depends(auth.require_admin)):
    await ai_settings.clear()
    await audit.log(user, "ai.settings.clear")
    return await ai_settings.get_public()


@app.post("/api/ai/health-summary")
async def api_ai_health_summary(db_id: str, user: str = Depends(auth.require_user)):
    await _require_ai()
    pool = await _resolve_pool(db_id)
    snap = await metrics.build_snapshot(pool)
    history = await metrics.get_history(pool, hours=3)
    history_summary = f"{len(history)} snapshots over the last 3 hours available." if history else "No history yet."
    result = await ai.health_summary(snap, history_summary)
    return result


@app.post("/api/ai/chat")
async def api_ai_chat(payload: dict, db_id: str, user: str = Depends(auth.require_user)):
    await _require_ai()
    messages = payload.get("messages", [])
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages[] is required")
    pool = await _resolve_pool(db_id)
    snap = await metrics.build_snapshot(pool)
    reply = await ai.chat(messages, snap)
    return {"reply": reply}


@app.post("/api/ai/security-review")
async def api_ai_security_review(db_id: str, user: str = Depends(auth.require_user)):
    await _require_ai()
    info = await metrics.get_security_info(await _resolve_pool(db_id))
    return await ai.security_review(info)


@app.post("/api/ai/backup-review")
async def api_ai_backup_review(db_id: str, user: str = Depends(auth.require_user)):
    await _require_ai()
    info = await metrics.get_backup_info(await _resolve_pool(db_id))
    return await ai.backup_review(info)


@app.post("/api/ai/administration-review")
async def api_ai_administration_review(db_id: str, user: str = Depends(auth.require_user)):
    await _require_ai()
    info = await metrics.get_administration_info(await _resolve_pool(db_id))
    return await ai.administration_review(info)


@app.post("/api/ai/performance-review")
async def api_ai_performance_review(db_id: str, user: str = Depends(auth.require_user)):
    await _require_ai()
    info = await metrics.get_performance_overview(await _resolve_pool(db_id))
    return await ai.performance_review(info)


@app.post("/api/ai/explain-query")
async def api_ai_explain_query(payload: dict, db_id: str, user: str = Depends(auth.require_user)):
    await _require_ai()
    query = (payload or {}).get("query", "")
    pool = await _resolve_pool(db_id)
    try:
        result = await metrics.run_explain(pool, query)
    except UnsafeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    analysis = await ai.analyze_query(result["query"], result["plan"])
    return {"plan": result["plan"], "analysis": analysis}


# ------------------------------------------------------------- migration ---
# The *destination* is always whichever database this tab is open on (its
# managed pool, via db_id/_resolve_pool). The *source* (Oracle or another
# Postgres) is ad hoc, entered in the form each time -- deliberately never
# persisted, unlike registered databases.
@app.post("/api/migration/test-oracle")
async def api_migration_test_oracle(payload: dict, user: str = Depends(auth.require_user)):
    p = payload or {}
    try:
        return await migration.test_oracle_connection(
            p.get("host"), int(p.get("port", 1521)), p.get("service_name"), p.get("username"), p.get("password"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/migration/tables-oracle")
async def api_migration_tables_oracle(payload: dict, user: str = Depends(auth.require_user)):
    p = payload or {}
    try:
        return await migration.list_oracle_tables(
            p.get("host"), int(p.get("port", 1521)), p.get("service_name"), p.get("username"), p.get("password"),
            p.get("owner") or p.get("username"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/migration/test-postgres")
async def api_migration_test_postgres(payload: dict, user: str = Depends(auth.require_user)):
    p = payload or {}
    try:
        return await migration.test_postgres_connection(
            p.get("host"), int(p.get("port", 5432)), p.get("dbname"), p.get("username"), p.get("password"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/migration/tables-postgres")
async def api_migration_tables_postgres(payload: dict, user: str = Depends(auth.require_user)):
    p = payload or {}
    try:
        return await migration.list_postgres_tables(
            p.get("host"), int(p.get("port", 5432)), p.get("dbname"), p.get("username"), p.get("password"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/migration/start-oracle")
async def api_migration_start_oracle(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    tables = p.get("tables") or []
    if not tables:
        raise HTTPException(status_code=400, detail="Select at least one table.")
    dest_pool = await _resolve_pool(db_id)
    try:
        job_id = await migration.start_oracle_to_postgres(
            p.get("host"), int(p.get("port", 1521)), p.get("service_name"), p.get("username"), p.get("password"),
            p.get("owner") or p.get("username"), tables, dest_pool,
            p.get("dest_schema") or "public",
            create_if_missing=bool(p.get("create_if_missing", True)),
            truncate_first=bool(p.get("truncate_first", False)),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "migration.start_oracle", detail=f"{len(tables)} table(s) -> {p.get('dest_schema') or 'public'}", db_id=db_id)
    return {"job_id": job_id}


@app.post("/api/migration/start-postgres")
async def api_migration_start_postgres(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    tables = [(t["schema_name"], t["table_name"]) for t in (p.get("tables") or [])]
    if not tables:
        raise HTTPException(status_code=400, detail="Select at least one table.")
    dest_pool = await _resolve_pool(db_id)
    try:
        job_id = await migration.start_postgres_to_postgres(
            p.get("host"), int(p.get("port", 5432)), p.get("dbname"), p.get("username"), p.get("password"),
            tables, dest_pool, p.get("dest_schema") or "public",
            create_if_missing=bool(p.get("create_if_missing", True)),
            truncate_first=bool(p.get("truncate_first", False)),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "migration.start_postgres", detail=f"{len(tables)} table(s) -> {p.get('dest_schema') or 'public'}", db_id=db_id)
    return {"job_id": job_id}


@app.get("/api/migration/jobs/{job_id}")
async def api_migration_job(job_id: str, user: str = Depends(auth.require_user)):
    job = migration.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such migration job.")
    return job


@app.post("/api/migration/jobs/{job_id}/cancel")
async def api_migration_job_cancel(job_id: str, user: str = Depends(auth.require_admin)):
    if not migration.cancel_job(job_id):
        raise HTTPException(status_code=400, detail="Job is not running (already finished, or no such job).")
    return {"ok": True}


# ---------------------------------------------------- full schema (ora2pg) ---
# A separate flow from the table-data-only copier above: shells out to
# ora2pg to convert an entire Oracle schema -- tables, views, sequences,
# indexes, constraints, triggers, functions/procedures -- into the database
# this tab is open on, then (optionally) loads the data too. See
# app/ora2pg_migration.py for why this needed its own module.
@app.get("/api/migration/ora2pg-available")
async def api_ora2pg_available(user: str = Depends(auth.require_user)):
    return ora2pg_migration.is_available()


@app.post("/api/migration/start-ora2pg")
async def api_migration_start_ora2pg(payload: dict, db_id: str, user: str = Depends(auth.require_admin)):
    p = payload or {}
    availability = ora2pg_migration.is_available()
    if not availability["ok"]:
        raise HTTPException(status_code=400, detail=availability["reason"])
    try:
        dest_conn = await db.get_conn_info(db_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        job_id = await ora2pg_migration.start_full_migration(
            p.get("host"), int(p.get("port", 1521)), p.get("service_name"), p.get("username"), p.get("password"),
            p.get("owner") or p.get("username"),
            dest_conn, p.get("dest_schema") or "public",
            migrate_data=bool(p.get("migrate_data", True)),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit.log(user, "migration.start_ora2pg", detail=p.get("dest_schema") or "public", db_id=db_id)
    return {"job_id": job_id}


@app.get("/api/migration/ora2pg-jobs/{job_id}")
async def api_ora2pg_job(job_id: str, user: str = Depends(auth.require_user)):
    job = ora2pg_migration.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such migration job.")
    return {k: v for k, v in job.items() if not k.startswith("_")}


@app.post("/api/migration/ora2pg-jobs/{job_id}/cancel")
async def api_ora2pg_job_cancel(job_id: str, user: str = Depends(auth.require_admin)):
    if not ora2pg_migration.cancel_job(job_id):
        raise HTTPException(status_code=400, detail="Job is not running (already finished, or no such job).")
    await audit.log(user, "migration.cancel", detail=job_id)
    return {"ok": True}
