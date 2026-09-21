#!/usr/bin/env python
"""Measure the traversal fix against a real group. Read-only.

For every failed workchain in a group, runs the superseded two-level query and
the new call-link walk side by side and reports the difference. This is the
number that motivated the rewrite: in the logs from the previous version, 91 of
188 failed workchains reported "No failed CalcJob found" and so were never
examined at all.

Nothing is written — no tags, no cache, no database changes.

    python scripts/measure_traversal.py "my-group-label"
    python scripts/measure_traversal.py "my-group" --profile other --limit 50
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter


def old_query(orm, father_pk):
    """The traversal this release replaces (app.py:1882 in the old layout)."""
    qb = orm.QueryBuilder()
    qb.append(orm.WorkChainNode, filters={"id": father_pk}, tag="father")
    qb.append(
        orm.WorkChainNode,
        with_incoming="father",
        filters={"attributes.exit_status": {"!==": 0}},
        tag="child_wc",
    )
    qb.append(orm.CalcJobNode, with_incoming="child_wc", project=["id"], tag="calcjob")
    qb.order_by({"calcjob": {"ctime": "desc"}})
    return [row[0] for row in qb.limit(1).all()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", help="Group label to measure")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--limit", type=int, default=0, help="Only the first N workchains")
    parser.add_argument("--max-depth", type=int, default=8)
    args = parser.parse_args(argv)

    from aiida import orm
    from aiida.manage.configuration import load_profile

    profile = load_profile(args.profile)

    from aiida_error_inspector import traversal

    print(f"profile: {profile.name}   group: {args.group}\n")

    started = time.monotonic()
    fathers = traversal.failed_workchains_in_group(args.group)
    if args.limit:
        fathers = fathers[: args.limit]
    if not fathers:
        print("No failed workchains found in that group.")
        return 1
    print(f"failed workchains: {len(fathers)}  ({time.monotonic() - started:.1f}s)")

    # Old father filter, for comparison: it required process_state == "finished".
    old_fathers = (
        orm.QueryBuilder()
        .append(orm.Group, filters={"label": args.group}, tag="g")
        .append(
            orm.WorkChainNode,
            with_group="g",
            filters={
                "and": [
                    {"attributes.exit_status": {"!==": 0}},
                    {"attributes.process_state": "finished"},
                ]
            },
            project=["id"],
        )
        .all(flat=True)
    )
    print(f"  old father filter would consider: {len(old_fathers)}")
    print(f"  new father filter considers:      {len(fathers)}")
    missed_fathers = {f.pk for f in fathers} - set(old_fathers)
    if missed_fathers:
        print(
            f"  -> {len(missed_fathers)} workchain(s) the old scan could never even "
            f"look at (excepted/killed)"
        )

    started = time.monotonic()
    forest = traversal.call_forest([f.pk for f in fathers], max_depth=args.max_depth)
    forest_time = time.monotonic() - started
    print(f"\ncall-graph walk: {forest_time:.1f}s for {len(fathers)} workchains")

    old_hits = new_hits = 0
    depths: Counter = Counter()
    recovered = []
    old_started = time.monotonic()

    for father in fathers:
        old_found = old_query(orm, father.pk)
        candidates = traversal.select_candidate_calcjobs(
            forest.get(father.pk, []), limit=0
        )
        if old_found:
            old_hits += 1
        if candidates:
            new_hits += 1
            depths[candidates[0].depth] += 1
            if not old_found:
                recovered.append(father.pk)

    old_time = time.monotonic() - old_started

    total = len(fathers)
    print(f"(old per-workchain queries took {old_time:.1f}s)\n")
    print("=" * 62)
    print(f"{'':34}{'count':>8}{'of total':>10}")
    print("-" * 62)
    print(f"{'old query found a CalcJob':34}{old_hits:>8}{old_hits / total:>9.0%}")
    print(f"{'new walk found a CalcJob':34}{new_hits:>8}{new_hits / total:>9.0%}")
    print(f"{'newly reachable':34}{len(recovered):>8}{len(recovered) / total:>9.0%}")
    print(f"{'still no failing CalcJob':34}{total - new_hits:>8}{(total - new_hits) / total:>9.0%}")
    print("=" * 62)

    if depths:
        print("\nDepth of the failing CalcJob below its workchain:")
        for depth in sorted(depths):
            marker = "  <- the only depth the old query handled" if depth == 2 else ""
            print(f"  depth {depth}: {depths[depth]:>5}{marker}")

    if recovered:
        shown = ", ".join(str(pk) for pk in recovered[:20])
        print(f"\nExamples of newly reachable workchains: {shown}")
        if len(recovered) > 20:
            print(f"  ... and {len(recovered) - 20} more")

    print(
        "\nThose workchains can now be tagged. Nothing was written; run the TUI "
        "and press 'u' to apply your saved rules to them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
