#!/bin/bash
# For each PostgreSQL major: download every postgresql-<v>-* extension package
# (plus the server package, which carries contrib), and emit one TSV line per
# extension .control file found inside: major, package, extension, metadata.
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq dpkg-dev >/dev/null 2>&1
mkdir -p /tmp/d && cd /tmp/d
for v in 13 14 15 16 17 18; do
  pkgs=$(apt-cache search --names-only "^postgresql-$v($|-)" | awk '{print $1}' | grep -vE 'dbgsym|-doc$|-dbg$|^postgresql-'$v'-jit')
  for p in $pkgs; do
    rm -rf x *.deb
    apt-get download -qq "$p" >/dev/null 2>&1 || { echo -e "$v\t$p\t__DOWNLOAD_FAILED__"; continue; }
    dpkg-deb -x *.deb x 2>/dev/null
    found=0
    for f in $(find x -path "*/extension/*.control" 2>/dev/null); do
      ext=$(basename "$f" .control)
      get() { grep -E "^\s*$1\s*=" "$f" | head -1 | sed -E "s/^\s*$1\s*=\s*//; s/^'//; s/'\s*$//"; }
      echo -e "$v\t$p\t$ext\t$(get comment)\t$(get requires)\t$(get trusted)\t$(get superuser)\t$(get default_version)"
      found=1
    done
    [ $found = 0 ] && echo -e "$v\t$p\t__NO_CONTROL__"
  done
done
