-- Finishes wiring up the least-privilege 'pgmon_app' role for pgmon's own
-- Postgres connection (replacing the superuser 'postgres' account it uses
-- today). The bare role already exists (CREATE ROLE pgmon_app LOGIN
-- PASSWORD ... ran fine); these are the privilege grants that this
-- session's permission classifier blocked as privilege-escalation-shaped,
-- so they need to be run by a human (or with Bash permission granted).
--
-- Run as: kubectl exec -i -n postgres postgres-postgresql-0 -- \
--   env PGPASSWORD="$(cat /root/projects/postgres-password.txt)" \
--   psql -U postgres -d appdb -v ON_ERROR_STOP=1 -f - \
--   < /root/projects/pg-ai-monitor/pgmon-app-role-setup.sql
--
-- Password for the new role is in /root/projects/pgmon-app-password.txt

-- Standard predefined roles: read-only monitoring stats/settings,
-- permission to pg_terminate_backend other (non-superuser) sessions,
-- and PG17+'s pg_maintain for VACUUM/ANALYZE/REINDEX without superuser.
GRANT pg_monitor, pg_signal_backend, pg_maintain TO pgmon_app;

-- Let it manage roles the way roles.py's create_role/alter_role/drop_role
-- already assume a privileged connection can.
ALTER ROLE pgmon_app CREATEROLE CREATEDB;

-- appdb's objects are all currently owned by 'postgres'; hand ownership to
-- pgmon_app so the Security tab's arbitrary GRANT/REVOKE features keep
-- working without full superuser.
REASSIGN OWNED BY postgres TO pgmon_app;

-- Fine-grained ALTER SYSTEM SET/RESET grants (PG15+), one per parameter
-- pgmon's Details-tab Edit... button exposes (context <> 'internal') --
-- there's no single "grant on all parameters" shorthand.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT name FROM pg_settings WHERE context <> 'internal' LOOP
    EXECUTE format('GRANT ALTER SYSTEM ON PARAMETER %I TO pgmon_app', r.name);
  END LOOP;
END $$;

-- After this runs, point pgmon at the new role and roll it out:
--   kubectl patch secret pgmonitor-secrets -n pgmonitor -p \
--     "{\"stringData\":{\"DB_PASSWORD\":\"$(cat /root/projects/pgmon-app-password.txt)\"}}"
--   kubectl set env deployment/pg-ai-monitor -n pgmonitor DB_USER=pgmon_app
--   kubectl rollout restart deployment/pg-ai-monitor -n pgmonitor
--
-- Note: pgmon_app is still a very powerful role (owns every object in
-- appdb, can create/alter/drop roles, can ALTER SYSTEM) -- it is NOT a
-- meaningfully sandboxed monitoring account. The real gain is dropping
-- the superuser bit itself: pgmon_app can't read pg_authid, can't
-- COPY TO/FROM PROGRAM, can't touch objects in the other databases on
-- this instance (postgres, secondapp, appdb_restored), and a leaked
-- credential no longer means full-instance superuser. True least-privilege
-- would mean splitting pgmon's read-only monitoring connection from a
-- separate, narrowly-scoped connection for the write-capable admin
-- actions (settings edit, vacuum, kill session, role management) -- a
-- larger app change than a role/grant script, not attempted here.
