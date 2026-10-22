# pg-ai-monitor

AI-assisted PostgreSQL monitoring, administration, and tuning dashboard.

Register any number of Postgres servers and get, per database: live overview
and connection/lock/wait-event monitoring, a security audit (`pg_hba.conf`
rules, SSL, roles), schema browsing with partition management, a
deterministic table-partitioning advisor with one-click live conversion, a
SQL tuning/index advisor, role and privilege management, logical/physical
backup and restore (including scheduled backups and point-in-time
recovery), and an Oracle/Postgres-to-Postgres migration tool. An optional
Anthropic API key turns on an AI Assistant chat and AI-written reviews on
top of all of the above -- every deterministic feature works without one.

**Before you install**, have ready:

- **An image**: there is no public image for this app. Build one from this
  chart's Dockerfile and push it to a registry your cluster can pull from,
  then set `image.repository` / `image.tag` accordingly.
- **A session secret**: `openssl rand -hex 32`, pasted into the Session
  Secret field. Keep it stable across upgrades -- changing it logs everyone
  out.

After install, see the in-app **Notes** (or `helm status`) for the one-time
command to create your first login -- this chart does not ship a default
admin account.
