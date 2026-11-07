"""Copying backups to an S3-compatible bucket (AWS S3, MinIO, Ceph RGW, ...).

One destination for all databases, set in the Backup & Recovery tab:
endpoint URL, bucket, optional folder prefix, access key/secret, and for an
air-gapped or private endpoint the things that usually differ from AWS --
path-style addressing (MinIO) and the CA certificate that signed the
endpoint's TLS certificate.

Every finished backup is copied to s3://<bucket>/<prefix>/<database>/<backup>
(a physical backup's directory as one object per file), with its
.meta.json next to it so a backup copied back from the bucket -- after
pgmon's own volume was lost, say -- restores like any other. The local copy
stays; restores always read local files.

boto3 is synchronous; callers in async code use asyncio.to_thread.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

# pgmon only ever talks to the configured endpoint: never fall back to
# AWS's instance-metadata service or shared credential files (an
# air-gapped host would just hang on them).
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, SSLError  # noqa: E402

from app import dbstore  # noqa: E402
from app.config import settings  # noqa: E402


class S3Error(Exception):
    pass


_SAFE_PREFIX = re.compile(r"^[A-Za-z0-9_.\-/]*$")
_SAFE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


# ------------------------------------------------------------------ storage
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s3_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            endpoint_url TEXT NOT NULL,
            region TEXT NOT NULL,
            bucket TEXT NOT NULL,
            prefix TEXT NOT NULL,
            access_key TEXT NOT NULL,
            secret_encrypted TEXT NOT NULL,
            path_style INTEGER NOT NULL,
            verify_tls INTEGER NOT NULL,
            ca_cert TEXT NOT NULL,
            auto_upload INTEGER NOT NULL,
            mirror_deletes INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return conn


def get_config() -> dict | None:
    """Including the decrypted secret -- never returned to the browser."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM s3_config WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cfg = dict(row)
    try:
        cfg["secret_key"] = dbstore._decrypt(cfg.pop("secret_encrypted"))
    except Exception:
        cfg["secret_key"] = ""
    for k in ("path_style", "verify_tls", "auto_upload", "mirror_deletes"):
        cfg[k] = bool(cfg[k])
    return cfg


def public_config(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else get_config()
    if not cfg:
        return {"configured": False}
    return {
        "configured": True,
        "endpoint_url": cfg["endpoint_url"], "region": cfg["region"], "bucket": cfg["bucket"],
        "prefix": cfg["prefix"], "access_key": cfg["access_key"], "path_style": cfg["path_style"],
        "verify_tls": cfg["verify_tls"], "has_ca_cert": bool(cfg["ca_cert"]),
        "auto_upload": cfg["auto_upload"], "mirror_deletes": cfg["mirror_deletes"],
        "updated_at": cfg["updated_at"],
        "location": f"s3://{cfg['bucket']}/{cfg['prefix'] + '/' if cfg['prefix'] else ''}",
    }


def delete_config() -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM s3_config WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    _ca_file(None)


def _validate(payload: dict, existing: dict | None) -> dict:
    p = payload or {}
    endpoint = (p.get("endpoint_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+(:\d+)?(/.*)?$", endpoint):
        raise S3Error("Endpoint URL must look like https://minio.example.internal:9000 (or http://...).")
    bucket = (p.get("bucket") or "").strip()
    if not _SAFE_BUCKET.match(bucket):
        raise S3Error("Bucket name must be 3-63 characters: lowercase letters, digits, dots and hyphens.")
    prefix = (p.get("prefix") or "").strip().strip("/")
    if not _SAFE_PREFIX.match(prefix) or ".." in prefix.split("/"):
        raise S3Error("Folder prefix may only contain letters, digits, '_', '.', '-' and '/'.")
    access_key = (p.get("access_key") or "").strip()
    secret_key = p.get("secret_key") or (existing or {}).get("secret_key") or ""
    if not access_key or not secret_key:
        raise S3Error("Access key and secret key are both required.")
    ca_cert = (p.get("ca_cert") or "").strip()
    if p.get("keep_ca_cert") and not ca_cert and existing:
        ca_cert = existing.get("ca_cert") or ""
    if ca_cert and "BEGIN CERTIFICATE" not in ca_cert:
        raise S3Error("The CA certificate must be PEM text (-----BEGIN CERTIFICATE----- ...).")
    return {
        "endpoint_url": endpoint, "region": (p.get("region") or "us-east-1").strip() or "us-east-1",
        "bucket": bucket, "prefix": prefix, "access_key": access_key, "secret_key": secret_key,
        "path_style": bool(p.get("path_style", True)), "verify_tls": bool(p.get("verify_tls", True)),
        "ca_cert": ca_cert, "auto_upload": bool(p.get("auto_upload", True)),
        "mirror_deletes": bool(p.get("mirror_deletes", True)),
    }


def save_config(payload: dict) -> dict:
    """Validate, prove the bucket is writable, then store."""
    existing = get_config()
    cfg = _validate(payload, existing)
    test(cfg)
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO s3_config (id, endpoint_url, region, bucket, prefix, access_key, secret_encrypted,
                   path_style, verify_tls, ca_cert, auto_upload, mirror_deletes, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET endpoint_url = excluded.endpoint_url, region = excluded.region,
                 bucket = excluded.bucket, prefix = excluded.prefix, access_key = excluded.access_key,
                 secret_encrypted = excluded.secret_encrypted, path_style = excluded.path_style,
                 verify_tls = excluded.verify_tls, ca_cert = excluded.ca_cert, auto_upload = excluded.auto_upload,
                 mirror_deletes = excluded.mirror_deletes, updated_at = excluded.updated_at""",
            (cfg["endpoint_url"], cfg["region"], cfg["bucket"], cfg["prefix"], cfg["access_key"],
             dbstore._encrypt(cfg["secret_key"]), int(cfg["path_style"]), int(cfg["verify_tls"]), cfg["ca_cert"],
             int(cfg["auto_upload"]), int(cfg["mirror_deletes"]), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return public_config()


# ------------------------------------------------------------------- client
def _ca_file(ca_cert: str | None) -> str | None:
    """botocore wants a CA bundle *file*; kept next to pgmon's own data."""
    path = os.path.join(os.path.dirname(os.path.abspath(settings.USERS_DB_PATH)), "s3-ca.pem")
    if not ca_cert:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None
    with open(path, "w") as f:
        f.write(ca_cert.strip() + "\n")
    os.chmod(path, 0o600)
    return path


def _client(cfg: dict):
    if cfg.get("ca_cert"):
        verify = _ca_file(cfg["ca_cert"])
    else:
        verify = bool(cfg.get("verify_tls", True))
    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint_url"],
        region_name=cfg["region"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        verify=verify,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if cfg.get("path_style", True) else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10, read_timeout=120,
            # boto3 >= 1.36 adds CRC checksums to every upload by default;
            # many S3-compatible servers (older MinIO, Ceph RGW) reject them.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _explain(e: Exception, cfg: dict) -> str:
    if isinstance(e, SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
        return (f"The TLS certificate of {cfg['endpoint_url']} isn't trusted. Paste the CA certificate that signed "
                f"it (PEM) into the CA certificate field.")
    if isinstance(e, EndpointConnectionError):
        return f"Can't reach {cfg['endpoint_url']} -- check the URL and that pgmon's network can reach it."
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("NoSuchBucket",) or status == 404:
            return f"Bucket {cfg['bucket']!r} doesn't exist at {cfg['endpoint_url']}."
        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch") or status in (401,):
            return "The access key or secret key is wrong."
        if code in ("AccessDenied", "Forbidden") or status == 403:
            return (f"Access denied for bucket {cfg['bucket']!r}: either the secret key is wrong, or the access key "
                    f"lacks permission (it needs list, read, write and delete on the bucket).")
        return f"{code or status}: {e.response.get('Error', {}).get('Message', str(e))}"
    return str(e)


def test(cfg: dict) -> None:
    client = _client(cfg)
    key = _key(cfg, ".pgmon-write-test")
    try:
        client.head_bucket(Bucket=cfg["bucket"])
        client.put_object(Bucket=cfg["bucket"], Key=key, Body=b"pgmon write test\n")
        client.delete_object(Bucket=cfg["bucket"], Key=key)
    except (BotoCoreError, ClientError) as e:
        raise S3Error(_explain(e, cfg))


# --------------------------------------------------------------- operations
def _key(cfg: dict, *parts: str) -> str:
    return "/".join(p.strip("/") for p in ((cfg.get("prefix") or ""),) + parts if p and p.strip("/"))


def _db_folder(db_id: str) -> str:
    """Objects are grouped by the database's pgmon name, which is readable in
    the bucket and survives reinstalling pgmon (its numeric ids don't)."""
    conn = sqlite3.connect(settings.USERS_DB_PATH)
    try:
        row = conn.execute("SELECT name FROM databases WHERE id = ?", (db_id,)).fetchone()
    finally:
        conn.close()
    name = row[0] if row else f"db-{db_id}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or f"db-{db_id}"


def _require() -> dict:
    cfg = get_config()
    if not cfg:
        raise S3Error("S3 storage isn't set up yet.")
    return cfg


def auto_upload_enabled() -> bool:
    cfg = get_config()
    return bool(cfg and cfg["auto_upload"])


def upload_backup(db_id: str, entry_path: str, meta: dict, log=None) -> dict:
    """Copy one backup (file, or a physical backup's directory) and its
    metadata to the bucket. Returns {"location", "uploaded_at", "bytes"}."""
    cfg = _require()
    client = _client(cfg)
    name = os.path.basename(entry_path.rstrip("/"))
    base = _key(cfg, _db_folder(db_id), name)
    total = 0
    try:
        if os.path.isdir(entry_path):
            for dirpath, _, files in os.walk(entry_path):
                for fn in sorted(files):
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, entry_path).replace(os.sep, "/")
                    if log:
                        log(f"S3: uploading {name}/{rel}")
                    client.upload_file(full, cfg["bucket"], f"{base}/{rel}")
                    total += os.path.getsize(full)
        else:
            if log:
                log(f"S3: uploading {name}")
            client.upload_file(entry_path, cfg["bucket"], base)
            total = os.path.getsize(entry_path)
        clean_meta = {k: v for k, v in meta.items() if not k.startswith("s3")}
        clean_meta["is_dir"] = os.path.isdir(entry_path)
        client.put_object(Bucket=cfg["bucket"], Key=base + ".meta.json",
                          Body=json.dumps(clean_meta).encode(), ContentType="application/json")
    except (BotoCoreError, ClientError) as e:
        raise S3Error(_explain(e, cfg))
    return {"location": f"s3://{cfg['bucket']}/{base}", "uploaded_at": time.time(), "bytes": total}


def list_remote(db_id: str) -> dict[str, dict]:
    """Backups in the bucket for this database: name -> metadata."""
    cfg = _require()
    client = _client(cfg)
    folder = _key(cfg, _db_folder(db_id)) + "/"
    names: dict[str, dict] = {}
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=folder, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                # Some S3-compatible servers (SeaweedFS, ...) keep an empty
                # "folder" after its objects are deleted: only a folder that
                # still holds objects is a backup.
                objs = []
                for sub in paginator.paginate(Bucket=cfg["bucket"], Prefix=cp["Prefix"]):
                    objs += sub.get("Contents", [])
                if objs:
                    names.setdefault(cp["Prefix"][len(folder):].rstrip("/"), {
                        "is_dir": True, "size_bytes": sum(o["Size"] for o in objs),
                        "created_at": min(o["LastModified"] for o in objs).timestamp()})
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(folder):]
                if rel.endswith(".meta.json") or not rel:
                    continue
                names.setdefault(rel, {"is_dir": False, "size_bytes": obj["Size"],
                                       "created_at": obj["LastModified"].timestamp()})
        for name in list(names):
            try:
                body = client.get_object(Bucket=cfg["bucket"], Key=f"{folder}{name}.meta.json")["Body"].read()
                names[name].update(json.loads(body))
            except (ClientError, ValueError):
                pass
    except (BotoCoreError, ClientError) as e:
        raise S3Error(_explain(e, cfg))
    return names


def fetch_backup(db_id: str, name: str, dest_dir: str, log=None) -> dict:
    """Copy a backup from the bucket into dest_dir. Returns its metadata."""
    cfg = _require()
    client = _client(cfg)
    folder = _key(cfg, _db_folder(db_id))
    base = f"{folder}/{name}"
    target = os.path.join(dest_dir, name)
    if os.path.exists(target):
        raise S3Error(f"{name} is already in pgmon's local backups.")
    tmp = tempfile.mkdtemp(prefix=".s3fetch-", dir=dest_dir)
    try:
        meta = {}
        try:
            meta = json.loads(client.get_object(Bucket=cfg["bucket"], Key=base + ".meta.json")["Body"].read())
        except (ClientError, ValueError):
            pass
        keys = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=cfg["bucket"], Prefix=base + "/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        if keys:
            staged = os.path.join(tmp, name)
            for k in keys:
                rel = k[len(base) + 1:]
                if not rel or ".." in rel.split("/"):
                    continue
                out = os.path.join(staged, *rel.split("/"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                if log:
                    log(f"S3: downloading {name}/{rel}")
                client.download_file(cfg["bucket"], k, out)
        else:
            staged = os.path.join(tmp, name)
            if log:
                log(f"S3: downloading {name}")
            client.download_file(cfg["bucket"], base, staged)
        os.rename(staged, target)
    except (BotoCoreError, ClientError) as e:
        raise S3Error(_explain(e, cfg))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    meta.pop("is_dir", None)
    return meta


def delete_remote(db_id: str, name: str) -> int:
    """Remove a backup's objects from the bucket. Returns how many."""
    cfg = _require()
    client = _client(cfg)
    base = _key(cfg, _db_folder(db_id), name)
    keys = [base, base + ".meta.json"]
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=cfg["bucket"], Prefix=base + "/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        removed = 0
        for i in range(0, len(keys), 1000):
            resp = client.delete_objects(Bucket=cfg["bucket"],
                                         Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})
            removed += len(keys[i:i + 1000]) - len(resp.get("Errors", []))
        return removed
    except (BotoCoreError, ClientError) as e:
        raise S3Error(_explain(e, cfg))
