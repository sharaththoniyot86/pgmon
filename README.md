# pgmon

AI-powered PostgreSQL monitoring, administration, backup, and migration tool.

This branch holds release assets only. See the [Releases page](../../releases)
for the downloadable Docker image (`pgmon-1.0.0-docker.tar.gz`).

## Running it

```bash
docker load -i pgmon-1.0.0-docker.tar.gz

docker run -d --name pgmon -p 443:443 \
    -v pgmon-config:/etc/pgmon -v pgmon-data:/var/lib/pgmon \
    --restart unless-stopped pgmon:1.0.0
```

Browse to `https://<this-host>/`. The admin password is generated on first
start -- find it with `docker logs pgmon`.

### Running on a custom port

By default pgmon serves HTTPS on port 443. To use a different port instead
(e.g. 8443), set `PGMON_PORT` and match it in the `-p` mapping:

```bash
docker run -d --name pgmon -p 8443:8443 -e PGMON_PORT=8443 \
    -v pgmon-config:/etc/pgmon -v pgmon-data:/var/lib/pgmon \
    --restart unless-stopped pgmon:1.0.0
```

Then browse to `https://<this-host>:8443/`.

(Podman works the same way -- just replace `docker` with `podman` in the
commands above.)
