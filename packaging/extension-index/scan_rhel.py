"""Map extension -> PGDG RPM package for EL8/EL9 x PG13..18 using repo filelists."""
import gzip, re, sys, urllib.request, xml.etree.ElementTree as ET
out_dir = sys.argv[1]
BASE = "https://download.postgresql.org/pub/repos/yum/{v}/redhat/rhel-{el}-x86_64/"
rows = []
for el in (8, 9):
    for v in (13, 14, 15, 16, 17, 18):
        base = BASE.format(v=v, el=el)
        try:
            repomd = urllib.request.urlopen(base + "repodata/repomd.xml", timeout=60).read().decode()
        except Exception as e:
            print(f"# EL{el} PG{v}: {e}", file=sys.stderr); continue
        href = re.search(r'href="(repodata/[^"]*-filelists\.xml\.gz)"', repomd).group(1)
        path = f"{out_dir}/el{el}-pg{v}-filelists.xml.gz"
        urllib.request.urlretrieve(base + href, path)
        seen = set()
        for _, elem in ET.iterparse(gzip.open(path), events=("end",)):
            if elem.tag.endswith("}package"):
                name = elem.get("name")
                for f in elem:
                    if f.tag.endswith("}file") and f.text and "/extension/" in f.text and f.text.endswith(".control"):
                        ext = f.text.rsplit("/", 1)[1][:-8]
                        if (name, ext) not in seen:
                            seen.add((name, ext)); rows.append((el, v, name, ext))
                elem.clear()
        print(f"# EL{el} PG{v}: {len(seen)} control files", file=sys.stderr)
for r in rows:
    print("\t".join(map(str, r)))
