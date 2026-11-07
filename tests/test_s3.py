"""Tests for copying backups to S3-compatible storage (app/s3store.py).

Run with:  python tests/test_s3.py

Optionally set PGMON_TEST_S3 to a JSON object with endpoint_url, bucket,
access_key, secret_key (and ca_cert_file for a private CA) to also run the
live round trip: test, upload a file and a folder, list, copy back, delete.
The full flow -- automatic copy after logical and physical backups, copy
back after a reinstall, restore with identical data, mirrored deletion,
private-CA TLS -- was exercised against an S3-compatible server.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("USERS_DB_PATH", os.path.join(tempfile.mkdtemp(), "users.db"))

from app.config import settings  # noqa: E402
settings.USERS_DB_PATH = os.environ["USERS_DB_PATH"]
if not getattr(settings, "SESSION_SECRET", None):
    settings.SESSION_SECRET = "test-only-secret-for-credential-encryption"
from app import s3store  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError, SSLError  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}{'' if cond else ' -- ' + str(detail)}")
    if not cond:
        FAILS.append(name)


GOOD = {"endpoint_url": "https://minio.minio.svc:9000/", "bucket": "pg-backups", "prefix": "/pgmon/",
        "access_key": "AK", "secret_key": "SK"}


def rejects(name, payload, existing=None):
    try:
        s3store._validate(payload, existing)
        check(name, False, "accepted")
    except s3store.S3Error:
        check(name, True)


def test_validation():
    print("\nSETTINGS VALIDATION")
    v = s3store._validate(GOOD, None)
    check("endpoint trailing slash trimmed", v["endpoint_url"] == "https://minio.minio.svc:9000", v)
    check("prefix slashes trimmed", v["prefix"] == "pgmon", v)
    check("defaults: path-style, verify TLS, auto upload, mirror deletes",
          v["path_style"] and v["verify_tls"] and v["auto_upload"] and v["mirror_deletes"], v)
    check("region defaults to us-east-1", v["region"] == "us-east-1")
    rejects("endpoint must be http(s)", {**GOOD, "endpoint_url": "minio:9000"})
    rejects("bucket name rules", {**GOOD, "bucket": "Bad_Bucket"})
    rejects("no '..' in prefix", {**GOOD, "prefix": "a/../b"})
    rejects("needs a secret", {**GOOD, "secret_key": ""})
    rejects("CA certificate must be PEM", {**GOOD, "ca_cert": "not a cert"})
    kept = s3store._validate({**GOOD, "secret_key": ""}, {"secret_key": "saved", "ca_cert": "-----BEGIN CERTIFICATE-----x"})
    check("empty secret keeps the saved one", kept["secret_key"] == "saved")
    kept = s3store._validate({**GOOD, "keep_ca_cert": True}, {"secret_key": "s", "ca_cert": "-----BEGIN CERTIFICATE-----x"})
    check("empty CA field keeps the saved certificate", kept["ca_cert"].startswith("-----BEGIN"))


def test_public_view_and_keys():
    print("\nWHAT THE BROWSER SEES / OBJECT NAMES")
    cfg = {**s3store._validate(GOOD, None), "updated_at": "t"}
    pub = s3store.public_config(cfg)
    check("no secret key", "SK" not in json.dumps(pub).replace('"AK"', ""), pub)
    check("location shown", pub["location"] == "s3://pg-backups/pgmon/", pub["location"])
    check("key with prefix", s3store._key(cfg, "orders-db", "x.dump") == "pgmon/orders-db/x.dump")
    check("key without prefix", s3store._key({**cfg, "prefix": ""}, "orders-db", "x.dump") == "orders-db/x.dump")


def test_error_messages():
    print("\nERROR MESSAGES")
    cfg = {"endpoint_url": "https://minio:9000", "bucket": "b"}
    def ce(code, status):
        return ClientError({"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "op")
    check("untrusted certificate", "CA certificate" in s3store._explain(SSLError(endpoint_url="x", error="CERTIFICATE_VERIFY_FAILED"), cfg))
    check("unreachable", "Can't reach" in s3store._explain(EndpointConnectionError(endpoint_url="x"), cfg))
    check("missing bucket", "doesn't exist" in s3store._explain(ce("NoSuchBucket", 404), cfg))
    check("bad keys", "wrong" in s3store._explain(ce("SignatureDoesNotMatch", 403), cfg))
    check("access denied", "Access denied" in s3store._explain(ce("AccessDenied", 403), cfg))


def live(conf):
    print("\nLIVE ROUND TRIP")
    import sqlite3
    payload = dict(conf)
    if payload.get("ca_cert_file"):
        payload["ca_cert"] = open(payload.pop("ca_cert_file")).read()
    conn = sqlite3.connect(settings.USERS_DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS databases (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT OR REPLACE INTO databases (id, name) VALUES (99, 'pgmon test db')")
    conn.commit(); conn.close()
    s3store.save_config({**payload, "prefix": "pgmon-tests"})
    work = tempfile.mkdtemp()
    f = os.path.join(work, "t.dump"); open(f, "wb").write(os.urandom(20000))
    d = os.path.join(work, "t_physical"); os.makedirs(d); open(os.path.join(d, "base.tar.gz"), "wb").write(os.urandom(9_000_000))
    open(os.path.join(d, "backup_manifest"), "w").write("{}")
    r1 = s3store.upload_backup("99", f, {"type": "logical"})
    r2 = s3store.upload_backup("99", d, {"type": "physical"})
    check("uploads land under the database's folder", r1["location"].endswith("/pgmon-tests/pgmon_test_db/t.dump"), r1)
    remote = s3store.list_remote("99")
    check("both listed", {"t.dump", "t_physical"} <= set(remote), remote)
    check("metadata travels", remote["t_physical"].get("type") == "physical" and remote["t_physical"]["is_dir"])
    back = tempfile.mkdtemp()
    s3store.fetch_backup("99", "t.dump", back); s3store.fetch_backup("99", "t_physical", back)
    check("file copied back byte-identical", open(os.path.join(back, "t.dump"), "rb").read() == open(f, "rb").read())
    check("folder copied back byte-identical (multipart size)",
          open(os.path.join(back, "t_physical", "base.tar.gz"), "rb").read() == open(os.path.join(d, "base.tar.gz"), "rb").read())
    s3store.delete_remote("99", "t.dump"); s3store.delete_remote("99", "t_physical")
    check("deleted from the bucket", not ({"t.dump", "t_physical"} & set(s3store.list_remote("99"))))
    s3store.delete_config()


if __name__ == "__main__":
    test_validation()
    test_public_view_and_keys()
    test_error_messages()
    if os.environ.get("PGMON_TEST_S3"):
        live(json.loads(os.environ["PGMON_TEST_S3"]))
    print(f"\n{'ALL S3 TESTS PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
