# pgmon standalone container (any Linux distro with Docker/Podman)

A single, self-contained image -- nginx, a self-signed TLS cert, and the
app itself all in one container. No Kubernetes, no separate reverse
proxy, no RHEL/Debian/distro-specific packaging -- the container doesn't
care what the host is running, only that it has a container runtime.
Mirrors the RPM/Linux-installer experience: on first start it generates
its own config, a self-signed cert, and an initial `admin` account, then
serves HTTPS directly.

## Getting the image onto the target machine

Built as a portable tarball -- `docker load` needs no registry access at
all, so this works even on a fully air-gapped machine:

```bash
docker load -i pgmon-1.0.0-docker.tar.gz
```

## Running it

```bash
docker run -d --name pgmon -p 443:443 \
    -v pgmon-config:/etc/pgmon -v pgmon-data:/var/lib/pgmon \
    --restart unless-stopped pgmon:1.0.0
```

The two named volumes matter: `/etc/pgmon` holds the generated config and
TLS cert, `/var/lib/pgmon` holds the SQLite accounts/settings DB. Without
them, config/admin account/cert would regenerate from scratch on every
`docker run` (fine for a quick throwaway test, wrong for anything meant to
persist).

The admin password is generated once, on first start, and printed to the
container's logs -- `docker logs pgmon` to find it; it's not shown again.

Browse to `https://<this-host>/`. Your browser will warn about the
certificate being self-signed -- expected, not an error; click through, or
put a real reverse proxy with a CA-issued cert in front of this container
instead if you have one (point it at this container's port over HTTP by
setting `PGMON_PORT` to something nginx-internal and not exposing it
externally, or just terminate TLS at your proxy and reverse-proxy straight
to this container's 443 -- either works, nginx inside doesn't need to be
the outermost edge).

## Choosing the HTTPS port

Defaults to 443. To use a different port:

```bash
docker run -d --name pgmon -p 8443:8443 -e PGMON_PORT=8443 \
    -v pgmon-config:/etc/pgmon -v pgmon-data:/var/lib/pgmon \
    --restart unless-stopped pgmon:1.0.0
```

Remember to also change the `-p` host-port mapping to match -- `-e
PGMON_PORT` only controls what port nginx listens on *inside* the
container; Docker's own `-p hostport:containerport` is what exposes it
outside.

## Resetting a password

```bash
docker exec -it pgmon pgmon-reset-password            # resets 'admin', prompts for the new password
docker exec -it pgmon pgmon-reset-password alice      # resets (or creates) a specific account
echo 'newpassword' | docker exec -i pgmon pgmon-reset-password admin   # non-interactive
```

## Upgrading

```bash
docker load -i pgmon-1.0.1-docker.tar.gz
docker rm -f pgmon
docker run -d --name pgmon -p 443:443 \
    -v pgmon-config:/etc/pgmon -v pgmon-data:/var/lib/pgmon \
    --restart unless-stopped pgmon:1.0.1
```

Config and the accounts DB live in the named volumes, not the container,
so recreating the container from a newer image tag leaves both untouched.

## Known limitations

Same two caveats as the RPM and universal Linux installers, for the same
reason -- this is a standalone, non-Kubernetes deployment:
- **Oracle→Postgres full-schema migration** (the `ora2pg`-based path) and
  its Oracle Instant Client dependency aren't bundled in this image (they
  add a large amount of image size for a feature most standalone
  deployments don't use) -- the app detects their absence and greys out
  that one feature rather than failing.
- **Automated restore of physical/incremental backups through the UI**
  needs pgmon to run inside the same Kubernetes cluster as the Postgres
  server (it drives `kubectl` against a StatefulSet) -- not applicable to
  a standalone container. Taking and downloading physical/incremental
  backups, and all logical (`pg_dump`/`pg_restore`) backup and restore,
  work the same as anywhere else -- `postgresql-client-18` is bundled for
  exactly that.

## Building the image yourself

From a checkout of the repo, on a machine with internet and Docker:

```bash
packaging/docker/build-image.sh 1.0.0
# -> dist/pgmon-1.0.0-docker.tar.gz
```

Verified end-to-end: fresh container generates config/cert/admin account
and serves a working HTTPS login; a restart leaves all three untouched
(no second password banner); a custom `PGMON_PORT` install works; the
`pgmon-reset-password` helper updates a password in a running container;
and the `docker save`/`docker load` round trip itself was tested, not
assumed -- the image was removed locally and reloaded from the exported
tarball before confirming it still runs.
