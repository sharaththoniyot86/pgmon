"""Central configuration read from environment variables.

Everything secret (DB password, admin password hash, Anthropic key,
session secret) is expected to arrive via env vars sourced from a
Kubernetes Secret in production, or a local .env-style export when
running standalone for development.
"""
import os


def _require(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


class Settings:
    # Schema this app creates inside each registered database to store its
    # own per-database snapshot history -- see app/db.py's _bootstrap_schema.
    MONITOR_SCHEMA: str = os.environ.get("MONITOR_SCHEMA", "pgmon")

    # pgmon's own namespace/deployment name, purely for display -- the Users
    # tab's password-reset command (`kubectl exec -n ... deployment/... python
    # -m app.create_user ...`) is built from these rather than a hardcoded
    # guess. See api_account_role in app/main.py.
    PGMON_NAMESPACE: str = os.environ.get("PGMON_NAMESPACE", "pgmonitor")
    PGMON_DEPLOYMENT: str = os.environ.get("PGMON_DEPLOYMENT", "pg-ai-monitor")

    # "kubernetes" (default, preserves every existing deployment's behavior
    # unchanged) or "standalone" -- which password-reset instructions the
    # login page and Account tab show (kubectl exec vs. the
    # packaging/linux/pgmon-reset-password command). install.sh sets this to
    # "standalone" in the pgmon.env it generates.
    DEPLOYMENT_MODE: str = os.environ.get("DEPLOYMENT_MODE", "kubernetes")

    # User accounts (username/bcrypt-hash) live in this local SQLite DB --
    # see app/userdb.py and app/create_user.py. Mounted on a PVC in k8s so it
    # survives pod restarts.
    USERS_DB_PATH: str = os.environ.get("USERS_DB_PATH", "./pgmon_users.db")

    SESSION_SECRET: str = os.environ.get("SESSION_SECRET", "")
    SESSION_MAX_AGE_SECONDS: int = int(os.environ.get("SESSION_MAX_AGE_SECONDS", str(8 * 3600)))

    # AI integration.
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

    # Background metrics poller.
    POLL_INTERVAL_SECONDS: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
    SNAPSHOT_RETENTION_HOURS: int = int(os.environ.get("SNAPSHOT_RETENTION_HOURS", "48"))

    # Outbound alert delivery (see app/notify.py). Unset = disabled, which
    # keeps every existing deployment's behaviour exactly as it was.
    # Any endpoint accepting a JSON POST works -- Slack/Teams incoming
    # webhooks render the payload's "text" field directly.
    ALERT_WEBHOOK_URL: str = os.environ.get("ALERT_WEBHOOK_URL", "")
    # error (default) / warning / info. Defaults to error only: the first
    # thing that kills an alerting channel is volume, and the warning tier
    # here includes things that are routine on a busy server.
    ALERT_WEBHOOK_MIN_SEVERITY: str = os.environ.get("ALERT_WEBHOOK_MIN_SEVERITY", "error")
    # How long before an alert that is *still* firing is re-sent. Long
    # enough not to nag, short enough that something broken overnight is
    # still in the channel by morning.
    ALERT_WEBHOOK_REARM_SECONDS: int = int(os.environ.get("ALERT_WEBHOOK_REARM_SECONDS", str(6 * 3600)))
    ALERT_WEBHOOK_TIMEOUT_SECONDS: float = float(os.environ.get("ALERT_WEBHOOK_TIMEOUT_SECONDS", "10"))

    # Mounted under this path behind the ingress (e.g. "/pgmonitor"); empty for local dev at "/".
    BASE_PATH: str = os.environ.get("BASE_PATH", "").rstrip("/")

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ANTHROPIC_API_KEY)


settings = Settings()
