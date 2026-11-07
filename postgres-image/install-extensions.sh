#!/bin/sh
# Installs every extension package apt.postgresql.org has for $PG_MAJOR.
# Packages are installed in one transaction when possible; if that fails
# (one package with an unsatisfiable dependency would sink them all) each
# is retried on its own and the failures are recorded, not fatal.
set -eu
: "${PG_MAJOR:?}"
EXCLUDE="${EXCLUDE:-dbgsym|-dbg$|-doc$}"
out=/usr/share/pgmon-postgres
mkdir -p "$out"
export DEBIAN_FRONTEND=noninteractive
apt-get update
pkgs=$( { apt-cache search --names-only "^postgresql-${PG_MAJOR}-" | awk '{print $1}';
          for pl in plperl plpython3 pltcl; do echo "postgresql-${pl}-${PG_MAJOR}"; done; } \
        | grep -vE "$EXCLUDE" | sort -u)
if ! apt-get install -y --no-install-recommends $pkgs; then
  : > "$out/failed.txt"
  for p in $pkgs; do
    apt-get install -y --no-install-recommends "$p" || echo "$p" >> "$out/failed.txt"
  done
fi
dpkg-query -W -f '${Package}\t${Version}\n' 'postgresql-*' > "$out/packages.txt"
ls /usr/share/postgresql/${PG_MAJOR}/extension/*.control | sed 's#.*/##; s#\.control$##' > "$out/extensions.txt"
apt-get clean
rm -rf /var/lib/apt/lists/*
echo "pgmon-postgres: $(wc -l < "$out/extensions.txt") extensions available"
