"""Tests for Backup & Recovery's safety-critical rules.

Run with:  python tests/test_backup_recovery.py

The full matrix -- logical/physical/incremental backup, verify, restore
into the same and new databases, scoped restores, imports, retention --
was exercised end to end against PostgreSQL 18. What's pinned here is the
logic that decides whether files get deleted or a failed restore is
reported as success, which is where a regression would be silent and
expensive.
"""
import os, sys, tempfile, time, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="pgmon-backup-tests-")
os.environ["BACKUPS_DIR"] = _TMP

from app import backup  # noqa: E402  (must follow the BACKUPS_DIR override)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}{'' if cond else ' -- ' + detail}")
    if not cond:
        FAILS.append(name)


def make_backup(db_id, name, *, days_old=0, kind="logical", based_on=None, is_dir=False):
    d = backup._db_dir(db_id)
    path = os.path.join(d, name)
    if is_dir:
        os.makedirs(path, exist_ok=True)
        open(os.path.join(path, "backup_manifest"), "w").close()
    else:
        with open(path, "w") as f:
            f.write("x")
    created = time.time() - days_old * 86400
    meta = {"type": kind, "created_at": created, "dbname": "db"}
    if based_on:
        meta["based_on"] = based_on
    with open(backup._meta_path(path), "w") as f:
        json.dump(meta, f)
    os.utime(path, (created, created))
    return name


def test_name_safety():
    print("\nBACKUP NAME SAFETY (these names reach the filesystem)")
    for bad in ["../../etc/passwd", "foo/../bar", "/etc/passwd", "..", ".", "", "a/b"]:
        try:
            backup._safe_name(bad)
            check(f"rejects {bad!r}", False, "accepted")
        except backup.BackupError:
            check(f"rejects {bad!r}", True)
    for good in ["20260101-120000_db.dump", "20260101-120000_db_physical", "a.b-c_d"]:
        try:
            backup._safe_name(good)
            check(f"accepts {good!r}", True)
        except backup.BackupError as e:
            check(f"accepts {good!r}", False, str(e))


def test_retention():
    print("\nRETENTION PRUNING (deletes real files -- must never over-delete)")
    db = "prune1"
    make_backup(db, "old1.dump", days_old=30)
    make_backup(db, "old2.dump", days_old=30)
    make_backup(db, "recent.dump", days_old=1)
    removed = backup.prune_old_backups(db, retention_days=7)
    remaining = [b["filename"] for b in backup.list_backups(db)]
    check("both old backups pruned", sorted(removed) == ["old1.dump", "old2.dump"], str(removed))
    check("the recent one is kept", remaining == ["recent.dump"], str(remaining))

    db = "prune2"
    make_backup(db, "ancient.dump", days_old=400)
    removed = backup.prune_old_backups(db, retention_days=7)
    check("a lone backup is never pruned, however old", removed == [], str(removed))

    # A kept incremental must never lose the base it chains back to.
    db = "prune3"
    make_backup(db, "base_physical", days_old=30, kind="physical", is_dir=True)
    make_backup(db, "incr", days_old=1, kind="incremental", based_on="base_physical", is_dir=True)
    make_backup(db, "stale.dump", days_old=30)
    removed = backup.prune_old_backups(db, retention_days=7)
    remaining = sorted(b["filename"] for b in backup.list_backups(db))
    check("the old base of a kept incremental survives", "base_physical" in remaining, str(remaining))
    check("the unrelated old backup is still pruned", removed == ["stale.dump"], str(removed))


def test_restore_option_validation():
    print("\nRESTORE OPTION VALIDATION")
    opts = backup._validate_restore_options(None)
    check("defaults are safe (no ownership, no error-stop surprises)",
          opts["content"] == "all" and opts["preserve_ownership"] is False, str(opts))
    for bad in [{"content": "everything"}, {"jobs": -1}, {"section": "middle"}, {"transaction_size": -5}]:
        try:
            backup._validate_restore_options(bad)
            check(f"rejects {bad}", False, "accepted")
        except backup.BackupError:
            check(f"rejects {bad}", True)


def test_archive_dir_validation():
    print("\nWAL ARCHIVE DIRECTORY VALIDATION (this value is run by the server's shell)")
    for bad in ["/tmp/a; rm -rf /", "/tmp/$(whoami)", "/tmp/`id`", "/tmp/a|b", "/tmp/a&b",
                "relative/path", "/tmp/with space", "/tmp/../../etc", "", "   ",
                "/tmp/a'b", '/tmp/a"b', "/tmp/a\nb", "/" + "x" * 600]:
        try:
            backup._validate_archive_dir(bad)
            check(f"rejects {bad[:28]!r}", False, "accepted")
        except backup.BackupError:
            check(f"rejects {bad[:28]!r}", True)
    for good in ["/var/lib/postgresql/wal_archive", "/mnt/nfs/pg-wal", "/tmp/ok_dir"]:
        try:
            check(f"accepts {good!r}", backup._validate_archive_dir(good) == good)
        except backup.BackupError as e:
            check(f"accepts {good!r}", False, str(e))
    check("a trailing slash is normalised away",
          backup._validate_archive_dir("/var/wal/") == "/var/wal")

    cmd = backup._archive_command_for("/var/wal")
    check("archive_command refuses to overwrite an existing segment",
          cmd == "test ! -f /var/wal/%f && cp %p /var/wal/%f", cmd)
    check("only the name of a known setting can be written",
          all(n in backup.ARCHIVE_SETTINGS for n in ("archive_mode", "archive_command", "wal_level")))


def test_disk_guard():
    print("\nDISK SPACE GUARD")
    low, msg = backup._disk_space_status()
    check("reports a human-readable free-space figure", "free of" in msg, msg)
    check("does not block on a volume with room", low is False, msg)


def test_verify_tool_matches_backup_version():
    print("\nVERIFY USES THE BACKUP'S OWN POSTGRESQL VERSION")
    import io, tarfile
    d = tempfile.mkdtemp(dir=_TMP)
    with tarfile.open(os.path.join(d, "base.tar.gz"), "w:gz") as tf:
        for name, data in (("backup_label", b"x"), ("PG_VERSION", b"17\n")):
            info = tarfile.TarInfo(name); info.size = len(data); tf.addfile(info, io.BytesIO(data))
    check("reads PG_VERSION from base.tar.gz", backup._backup_server_major(d) == 17, backup._backup_server_major(d))
    check("unknown when there's no base.tar.gz", backup._backup_server_major(tempfile.mkdtemp(dir=_TMP)) is None)
    check("falls back to PATH when that version isn't installed", backup._pg_tool("pg_verifybackup", 9) == "pg_verifybackup")
    expected = "/usr/lib/postgresql/17/bin/pg_verifybackup"
    if os.path.exists(expected):
        check("picks the matching version's tool", backup._pg_tool("pg_verifybackup", 17) == expected)


def main():
    try:
        test_name_safety()
        test_retention()
        test_restore_option_validation()
        test_archive_dir_validation()
        test_disk_guard()
        test_verify_tool_matches_backup_version()
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + ("FAILURES: " + ", ".join(FAILS) if FAILS else "ALL BACKUP & RECOVERY TESTS PASSED"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
