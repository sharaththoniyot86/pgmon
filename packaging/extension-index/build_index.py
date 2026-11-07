"""Build app/extension_index.json from the two package scans in this directory.

Inputs (TSV, produced by the scan scripts next to this file):

  debian_controls.tsv  -- scan_debian.sh, run inside any Debian/Ubuntu image
                          with apt.postgresql.org configured (pgmon's own image
                          works): major, package, extension, comment, requires,
                          trusted, superuser, default_version
  rhel_controls.tsv    -- scan_rhel.py, reads the PGDG yum repos' filelists:
                          el, major, package, extension

Output: one JSON object keyed by extension name. Each entry records which
package ships that extension's control file for each Postgres major, on
Debian/Ubuntu (apt.postgresql.org) and on RHEL-family 8/9 (PGDG yum), plus the
control-file metadata (comment, requires, trusted, superuser) from the newest
major that has it. Package names are stored as a "{v}" template plus the list
of majors whenever the name follows that pattern, which is nearly always.

  python3 build_index.py debian_controls.tsv rhel_controls.tsv > ../../app/extension_index.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict

# The Debian scan runs per major against the plperl/plpython3/pltcl
# packages' own naming, which doesn't start with postgresql-<v>-, so their
# languages are filled in from this table instead (RHEL ships the same set).
DEB_PL_PACKAGES = {
    "postgresql-plperl-{v}": ["plperl", "plperlu", "bool_plperl", "bool_plperlu",
                              "hstore_plperl", "hstore_plperlu", "jsonb_plperl", "jsonb_plperlu"],
    "postgresql-plpython3-{v}": ["plpython3u", "hstore_plpython3u", "jsonb_plpython3u", "ltree_plpython3u"],
    "postgresql-pltcl-{v}": ["pltcl", "pltclu"],
}

# RHEL packages that ship control files but aren't what anyone installing
# that extension should be pointed at.
RPM_EXCLUDE = re.compile(r"babelfish|-devel$|-debuginfo$|-llvmjit$|-docs?$")


def _pkg_rank(pkg: str) -> tuple:
    """Prefer the newest of several packages providing the same extension
    (postgis33_17 .. postgis36_17): the highest embedded number wins."""
    nums = [int(n) for n in re.findall(r"\d+", pkg)]
    return (nums, -len(pkg))


def _compress(per_major: dict[str, str]) -> dict:
    """{"15": "postgresql-15-cron", "16": "postgresql-16-cron"} ->
    {"package": "postgresql-{v}-cron", "majors": [15, 16]} when every major
    fits one template; otherwise the explicit per-major map."""
    majors = sorted(per_major, key=int)
    templates = {per_major[m].replace(m, "{v}") for m in majors}
    if len(templates) == 1:
        return {"package": templates.pop(), "majors": [int(m) for m in majors]}
    return {"by_major": {m: per_major[m] for m in majors}, "majors": [int(m) for m in majors]}


def main(deb_path: str, rpm_path: str) -> None:
    deb: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    meta: dict[str, tuple[int, dict]] = {}
    contrib: set[str] = set()

    for line in open(deb_path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 3 or parts[2].startswith("__"):
            continue
        parts += [""] * (8 - len(parts))
        major, pkg, ext, comment, requires, trusted, superuser, default_version = parts[:8]
        if pkg.endswith("-dbgsym"):
            continue
        # Debian's PostGIS ships "postgis-3.control" and creates the plain
        # "postgis.control" with update-alternatives at install time, so
        # the unversioned name -- the one CREATE EXTENSION uses -- is
        # never inside the .deb itself.
        alt = re.fullmatch(r"(.+)-\d+", ext)
        if alt:
            ext = alt.group(1)
        deb[ext][major].add(pkg)
        if pkg == f"postgresql-{major}":
            contrib.add(ext)
        m = int(major)
        if ext not in meta or m >= meta[ext][0]:
            meta[ext] = (m, {
                "comment": comment,
                "requires": [r.strip() for r in requires.split(",") if r.strip()],
                "trusted": trusted.strip().lower() in ("true", "on", "yes", "1"),
                "superuser": superuser.strip().lower() not in ("false", "off", "no", "0"),
                "default_version": default_version,
            })

    rpm: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    for line in open(rpm_path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 4:
            continue
        el, major, pkg, ext = parts
        if RPM_EXCLUDE.search(pkg):
            continue
        rpm[ext][f"el{el}"][major].add(pkg)
        if pkg in (f"postgresql{major}-contrib", f"postgresql{major}-server") or pkg.startswith(f"postgresql{major}-pl"):
            contrib.add(ext)

    deb_majors = sorted({m for e in deb.values() for m in e}, key=int)
    for tmpl, exts in DEB_PL_PACKAGES.items():
        for ext in exts:
            for m in deb_majors:
                deb[ext][m].add(tmpl.format(v=m))

    index = {}
    for ext in sorted(set(deb) | set(rpm)):
        # Compatibility shims ("pg_proctab--0.0.10-compat") and packages'
        # own regression-test fixtures aren't anything to offer anyone.
        if "--" in ext or ext.endswith(("_test", "_test_extension")):
            continue
        entry: dict = {"contrib": ext in contrib}
        entry.update(meta.get(ext, (0, {}))[1])
        if ext in deb:
            entry["deb"] = _compress({m: max(p, key=_pkg_rank) for m, p in deb[ext].items()})
        if ext in rpm:
            entry["rpm"] = {el: _compress({m: max(p, key=_pkg_rank) for m, p in by_major.items()})
                            for el, by_major in sorted(rpm[ext].items())}
        index[ext] = entry
    json.dump(index, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
