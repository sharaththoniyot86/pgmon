"""Boundary tests for the enterprise alert thresholds.

Run with:  python tests/test_enterprise_alerts.py

The live tests proved the queries return correct real data and that the
alerts fire end-to-end. These pin the severity boundaries themselves,
which a live instance can't hold still for -- Postgres runs anti-wraparound
autovacuum even with autovacuum=off, so a real near-wraparound state
self-heals within minutes.
"""
import asyncio, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from unittest.mock import AsyncMock, patch
from app import alerts, metrics

LIMIT = metrics.XID_WRAPAROUND_LIMIT
FAILS = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        FAILS.append(name)


def wrap_payload(pct, mxid_pct=0.0, tables=None):
    age = int(LIMIT * pct / 100)
    return {
        "databases": [{"datname": "appdb", "xid_age": age, "mxid_age": 0}],
        "tables": tables or [],
        "settings": {"autovacuum_freeze_max_age": "200000000"},
        "max_xid_age": age, "max_mxid_age": int(LIMIT * mxid_pct / 100),
        "xid_limit": LIMIT, "xid_used_pct": pct, "mxid_used_pct": mxid_pct,
        "xid_remaining": LIMIT - age,
    }


async def alerts_for(wrap=None, repl=None):
    empty_overview = {"connections": {}, "max_connections": 100, "cache_hit_ratio_pct": 99.0, "db_stat": {}}
    with patch.object(metrics, "get_settings", AsyncMock(return_value={"settings": []})), \
         patch.object(metrics, "get_overview", AsyncMock(return_value=empty_overview)), \
         patch.object(metrics, "get_connections", AsyncMock(return_value=[])), \
         patch.object(metrics, "get_locks", AsyncMock(return_value=[])), \
         patch.object(metrics, "get_autovacuum_info", AsyncMock(return_value={"settings": {"autovacuum": "on"}, "tables": []})), \
         patch.object(metrics, "get_checkpoint_info", AsyncMock(return_value={"stats": {}})), \
         patch.object(metrics, "get_security_info", AsyncMock(return_value={"ssl_enabled": True, "password_encryption": "scram-sha-256", "hba_rules": []})), \
         patch.object(metrics, "get_backup_info", AsyncMock(return_value={"archive_mode": "on", "archiver": {}})), \
         patch.object(metrics, "get_wraparound_info", AsyncMock(return_value=wrap or wrap_payload(0))), \
         patch.object(metrics, "get_replication_info", AsyncMock(return_value=repl or {"slots": [], "standbys": [], "subscriptions": []})):
        return await alerts.get_alerts(pool=None, db_id=None)


def sev_of(alist, needle):
    for a in alist:
        if needle.lower() in a["title"].lower():
            return a["severity"]
    return None


async def main():
    print("XID WRAPAROUND THRESHOLDS (warn >=50%, error >=75%)")
    for pct, expected in [(10.0, None), (49.9, None), (50.0, "warning"),
                          (74.9, "warning"), (75.0, "error"), (95.0, "error")]:
        got = sev_of(await alerts_for(wrap=wrap_payload(pct)), "Transaction ID")
        check(f"{pct}% consumed -> {expected}", got == expected)

    print()
    print("MULTIXACT THRESHOLDS (independent counter)")
    for pct, expected in [(49.0, None), (60.0, "warning"), (80.0, "error")]:
        got = sev_of(await alerts_for(wrap=wrap_payload(0.0, mxid_pct=pct)), "MultiXact")
        check(f"mxid {pct}% -> {expected}", got == expected)

    print()
    print("OFFENDING-TABLE CALLOUT (only once overall age matters)")
    hot_table = [{"schema_name": "public", "table_name": "events",
                  "xid_age": int(LIMIT * 0.60), "mxid_age": 0,
                  "total_bytes": 10**9, "autovacuum_enabled": "on"}]
    low = await alerts_for(wrap=wrap_payload(10.0, tables=hot_table))
    check("table not named while overall age is healthy", sev_of(low, "freeze horizon") is None)
    high = await alerts_for(wrap=wrap_payload(60.0, tables=hot_table))
    check("table named once age is high", sev_of(high, "freeze horizon") == "warning")
    off_table = [dict(hot_table[0], autovacuum_enabled="off")]
    high_off = await alerts_for(wrap=wrap_payload(60.0, tables=off_table))
    check("autovacuum-disabled table escalates to error", sev_of(high_off, "freeze horizon") == "error")

    print()
    print("REPLICATION SLOT RULES")
    cases = [
        ("inactive slot, small retention -> warning",
         {"slot_name": "s1", "active": False, "wal_status": "reserved", "retained_bytes": 50 * 1024**2}, "warning"),
        ("inactive slot, >10GB retained -> error",
         {"slot_name": "s1", "active": False, "wal_status": "reserved", "retained_bytes": 11 * 1024**3}, "error"),
        ("wal_status=lost -> error",
         {"slot_name": "s1", "active": False, "wal_status": "lost", "retained_bytes": 0}, "error"),
        ("active slot, small retention -> no alert",
         {"slot_name": "s1", "active": True, "wal_status": "reserved", "retained_bytes": 1024}, None),
        ("active slot, >10GB retained -> warning",
         {"slot_name": "s1", "active": True, "wal_status": "reserved", "retained_bytes": 11 * 1024**3}, "warning"),
    ]
    for name, slot, expected in cases:
        got = sev_of(await alerts_for(repl={"slots": [slot], "standbys": [], "subscriptions": []}), "slot 's1'")
        check(name, got == expected)

    print()
    print("STANDBY LAG RULES (warn >=100MB, error >=1GB)")
    for lag, expected in [(10 * 1024**2, None), (150 * 1024**2, "warning"), (2 * 1024**3, "error")]:
        sb = {"application_name": "replica1", "state": "streaming", "replay_lag_bytes": lag}
        got = sev_of(await alerts_for(repl={"slots": [], "standbys": [sb], "subscriptions": []}), "replica1")
        check(f"{lag // 1024**2}MB lag -> {expected}", got == expected)

    sb = {"application_name": "replica1", "state": "catchup", "replay_lag_bytes": 0}
    res = await alerts_for(repl={"slots": [], "standbys": [sb], "subscriptions": []})
    check("non-streaming state flagged", any("not streaming" in a["title"] for a in res))

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {FAILS}")
        sys.exit(1)
    print("ALL THRESHOLD TESTS PASSED")

asyncio.run(main())
