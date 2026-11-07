# pgmon-postgres: the official Debian-based postgres image plus every
# extension package apt.postgresql.org publishes for that major version.
#
# Every extension in pgmon's Extensions tab that is packaged for Debian is
# then already on disk, so enabling it is one click (CREATE EXTENSION)
# instead of a package install on the host.
#
#   podman build --build-arg PG_MAJOR=17 -t pgmon-postgres:17 postgres-image
#
# DEBIAN_RELEASE must match the image an existing data directory was
# created on (trixie for postgres:<v> tags since late 2025, bookworm before):
# a different glibc changes collation order, and text indexes built under
# the old one would silently return wrong results until REINDEXed.
#
# Nothing is preloaded by default; the image only provides the files.
# shared_preload_libraries stays under your (and pgmon's) control.
ARG PG_MAJOR=17
ARG DEBIAN_RELEASE=trixie
FROM docker.io/library/postgres:${PG_MAJOR}-${DEBIAN_RELEASE}
ARG PG_MAJOR
# Extended regular expression of package names to leave out (debug symbols
# and documentation are never wanted; add more with --build-arg).
ARG EXCLUDE="dbgsym|-dbg$|-doc$"
COPY install-extensions.sh /usr/local/sbin/pgmon-install-extensions
RUN chmod 0755 /usr/local/sbin/pgmon-install-extensions \
 && PG_MAJOR="${PG_MAJOR}" EXCLUDE="${EXCLUDE}" /usr/local/sbin/pgmon-install-extensions
LABEL org.opencontainers.image.title="pgmon-postgres" \
      org.opencontainers.image.description="PostgreSQL with every apt.postgresql.org extension package installed"
