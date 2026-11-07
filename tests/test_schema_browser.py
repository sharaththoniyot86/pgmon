"""Tests for the schema browser's data shaping.

Run with:  python tests/test_schema_browser.py

The catalog queries themselves are verified against a live PostgreSQL 18
instance (that's the only thing that can confirm a DDL reconstruction is
faithful). What's pinned here is the pure logic layered on top of them,
which no live instance holds still long enough to assert on: the
partition rollup, where an empty parent must report its children's bytes
without double-counting them.
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.metrics import _rollup_partition_totals

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}{'' if cond else ' -- ' + detail}")
    if not cond:
        FAILS.append(name)


def obj(name, size=None, rows=None, partitioned=False, parent=None):
    return {
        "name": name, "size_bytes": size, "row_estimate": rows,
        "is_partitioned": partitioned, "is_partition": parent is not None,
        "parent_name": parent,
    }


def by_name(objects):
    return {o["name"]: o for o in objects}


def test_single_level():
    print("\nSINGLE-LEVEL PARTITIONED TABLE")
    objects = [
        obj("events", size=0, rows=0, partitioned=True),
        obj("events_01", size=8192, rows=100, parent="events"),
        obj("events_02", size=16384, rows=250, parent="events"),
        obj("plain", size=4096, rows=7),
    ]
    _rollup_partition_totals(objects)
    o = by_name(objects)
    check("parent sums its partitions' bytes", o["events"]["size_bytes"] == 24576, str(o["events"]["size_bytes"]))
    check("parent sums its partitions' rows", o["events"]["row_estimate"] == 350, str(o["events"]["row_estimate"]))
    check("partitions keep their own size", o["events_01"]["size_bytes"] == 8192)
    check("unrelated table untouched", o["plain"]["size_bytes"] == 4096 and o["plain"]["row_estimate"] == 7)


def test_sub_partitioned():
    print("\nSUB-PARTITIONED TREE (partition of a partition)")
    objects = [
        obj("evt", size=0, rows=0, partitioned=True),
        obj("evt_2026", size=0, rows=0, partitioned=True, parent="evt"),
        obj("evt_2026_01", size=1000, rows=10, parent="evt_2026"),
        obj("evt_2026_02", size=2000, rows=20, parent="evt_2026"),
    ]
    _rollup_partition_totals(objects)
    o = by_name(objects)
    check("mid-level parent totals its own children", o["evt_2026"]["size_bytes"] == 3000, str(o["evt_2026"]["size_bytes"]))
    check("top-level parent totals the whole tree", o["evt"]["size_bytes"] == 3000, str(o["evt"]["size_bytes"]))
    check("rows total through both levels", o["evt"]["row_estimate"] == 30, str(o["evt"]["row_estimate"]))


def test_unknown_values():
    print("\nUNANALYSED / UNSIZED RELATIONS (NULL, not zero)")
    objects = [
        obj("p", size=0, rows=None, partitioned=True),
        obj("p_a", size=None, rows=None, parent="p"),
        obj("p_b", size=512, rows=5, parent="p"),
    ]
    _rollup_partition_totals(objects)
    o = by_name(objects)
    check("a NULL child doesn't zero the total", o["p"]["size_bytes"] == 512, str(o["p"]["size_bytes"]))
    check("rows become known once one child is", o["p"]["row_estimate"] == 5, str(o["p"]["row_estimate"]))

    only_unknown = [obj("q", size=None, rows=None, partitioned=True), obj("q_a", parent="q")]
    _rollup_partition_totals(only_unknown)
    check("all-unknown stays unknown, not 0", by_name(only_unknown)["q"]["size_bytes"] is None)


def test_orphans_and_cycles():
    print("\nMALFORMED INPUT")
    # A partition whose parent lives in another schema isn't in this list.
    objects = [obj("child", size=100, rows=1, parent="elsewhere")]
    _rollup_partition_totals(objects)
    check("orphan partition keeps its own numbers", by_name(objects)["child"]["size_bytes"] == 100)

    # Can't happen in the catalog; must not hang if it somehow did.
    cyclic = [
        obj("a", size=1, rows=1, partitioned=True, parent="b"),
        obj("b", size=2, rows=1, partitioned=True, parent="a"),
    ]
    cyclic[0]["is_partition"] = True
    cyclic[1]["is_partition"] = True
    _rollup_partition_totals(cyclic)
    check("cycle terminates instead of recursing forever", True)


def main():
    test_single_level()
    test_sub_partitioned()
    test_unknown_values()
    test_orphans_and_cycles()
    print("\n" + ("FAILURES: " + ", ".join(FAILS) if FAILS else "ALL SCHEMA BROWSER TESTS PASSED"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
