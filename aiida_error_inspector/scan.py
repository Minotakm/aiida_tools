"""The scan: walk a group's failed workchains and apply classifiers to them.

Shape of the rewrite, versus what this replaces (``app.py:1412-1557``):

* **Workchains outer, classifiers inner.** The old ``u`` loop iterated patterns
  on the outside, so a node's ``aiida.out`` was fully re-read once per pattern.
* **One read per (node, file).** Classifiers are bucketed by the file they need,
  and each bucket streams its file once.
* **Exit-code classifiers cost no IO at all** — the exit status arrives
  projected from the traversal query.
* **A negative cache.** The old ``categorized.json`` recorded only *matched*
  nodes, so every unmatched node was re-read in full on every re-scan, forever.
  Here each node records which classifier *fingerprints* it has been tested
  against, so adding a fourth pattern only reads files for that pattern.
* **No shared mutable state.** The worker builds a ``ScanResult`` and hands it
  back once; it never touches the app's tag dict.

Nothing in this module imports Textual, and AiiDA only arrives through the
injected backend — which is what makes it testable against fakes.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .classify import Classifier, group_by_file

logger = logging.getLogger(__name__)

FATHER_CHUNK = 50


@dataclass(frozen=True)
class ScanRequest:
    group_label: str
    classifiers: tuple[Classifier, ...]
    max_calcjobs: int = 5
    max_depth: int = 8
    force: bool = False


@dataclass
class ScanProgress:
    done: int
    total: int
    current: str = ""


@dataclass
class ScanResult:
    """Everything the UI thread needs to apply and explain a scan."""

    new_tags: dict[int, set[str]] = field(default_factory=dict)
    scanned: dict[int, set[str]] = field(default_factory=dict)
    n_fathers: int = 0
    n_skipped_cached: int = 0
    n_no_calcjob: int = 0
    missing_file: Counter = field(default_factory=Counter)
    errors: dict[int, str] = field(default_factory=dict)
    cancelled: bool = False
    elapsed: float = 0.0

    @property
    def n_tagged(self) -> int:
        return sum(1 for tags in self.new_tags.values() if tags)

    @property
    def n_pairs(self) -> int:
        return sum(len(tags) for tags in self.new_tags.values())

    def summary(self) -> str:
        """Distinguish the reasons a scan found nothing.

        The old code said only "No new workchains matched", which conflated
        'pattern absent', 'file absent' and 'no failed workchains'.
        """
        if self.n_fathers == 0:
            return "No failed workchains in this group."

        bits: list[str] = []
        if self.n_tagged:
            bits.append(f"tagged {self.n_tagged} workchain(s) ({self.n_pairs} tag assignments)")
        else:
            bits.append("no new matches")
        if self.n_skipped_cached:
            bits.append(f"{self.n_skipped_cached} already scanned")
        if self.n_no_calcjob:
            bits.append(f"{self.n_no_calcjob} with no failing CalcJob")
        for filename, count in self.missing_file.most_common(3):
            bits.append(f"{filename} absent in {count}")
        if self.errors:
            bits.append(f"{len(self.errors)} read error(s)")
        if self.cancelled:
            bits.append("CANCELLED")
        return f"Scanned {self.n_fathers}: " + ", ".join(bits) + f" [{self.elapsed:.1f}s]"


@dataclass
class ScanBackend:
    """Injection seam. Defaults bind to the real AiiDA-backed implementations."""

    failed_fathers: Callable[[str], list[Any]]
    call_forest: Callable[..., dict[int, list[Any]]]
    select_candidates: Callable[..., list[Any]]
    load_calcjobs: Callable[[Sequence[int]], dict[int, Any]]
    search_file: Callable[..., set[str]]

    @classmethod
    def default(cls) -> "ScanBackend":
        from . import node_inspector, traversal

        return cls(
            failed_fathers=traversal.failed_workchains_in_group,
            call_forest=traversal.call_forest,
            select_candidates=traversal.select_candidate_calcjobs,
            load_calcjobs=traversal.load_calcjobs,
            search_file=node_inspector.search_file,
        )


def _chunks(seq: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


def run_scan(
    request: ScanRequest,
    *,
    backend: ScanBackend | None = None,
    scan_cache: dict[int, set[str]] | None = None,
    progress: Callable[[ScanProgress], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ScanResult:
    """Classify every failed workchain in a group. Runs off the UI thread."""
    backend = backend or ScanBackend.default()
    scan_cache = scan_cache or {}
    should_cancel = should_cancel or (lambda: False)
    started = time.monotonic()
    result = ScanResult()

    classifiers = list(request.classifiers)
    if not classifiers:
        return result

    fathers = backend.failed_fathers(request.group_label)
    result.n_fathers = len(fathers)

    # Which classifiers has each father not yet been tested against?
    todo_by_father: dict[int, list[Classifier]] = {}
    for father in fathers:
        if request.force:
            todo_by_father[father.pk] = classifiers
            continue
        applied = scan_cache.get(father.pk, set())
        todo = [c for c in classifiers if c.fingerprint() not in applied]
        if todo:
            todo_by_father[father.pk] = todo
        else:
            result.n_skipped_cached += 1

    pending = [f for f in fathers if f.pk in todo_by_father]
    if not pending:
        result.elapsed = time.monotonic() - started
        return result

    forest = backend.call_forest(
        [f.pk for f in pending], max_depth=request.max_depth
    )

    done = 0
    last_report = 0.0
    for chunk in _chunks(pending, FATHER_CHUNK):
        if should_cancel():
            result.cancelled = True
            break

        # Batch-load the ORM instances for this chunk's candidate CalcJobs.
        candidates_by_father: dict[int, list[Any]] = {}
        wanted: list[int] = []
        for father in chunk:
            candidates = backend.select_candidates(
                forest.get(father.pk, []), limit=request.max_calcjobs
            )
            candidates_by_father[father.pk] = candidates
            wanted.extend(ref.pk for ref in candidates)
        nodes = backend.load_calcjobs(wanted) if wanted else {}

        for father in chunk:
            if should_cancel():
                result.cancelled = True
                break

            todo = todo_by_father[father.pk]
            candidates = candidates_by_father[father.pk]
            if not candidates:
                result.n_no_calcjob += 1

            try:
                matched = _classify_one(
                    father=father,
                    candidates=candidates,
                    nodes=nodes,
                    classifiers=todo,
                    backend=backend,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001
                # The old code swallowed this silently and reported "no match".
                logger.exception("Scanning workchain %s failed", father.pk)
                result.errors[father.pk] = str(exc)
                continue

            if matched:
                result.new_tags[father.pk] = matched
            result.scanned[father.pk] = {c.fingerprint() for c in todo}

            done += 1
            now = time.monotonic()
            if progress and (now - last_report) > 0.25:
                last_report = now
                progress(ScanProgress(done, len(pending), father.label))

    if progress:
        progress(ScanProgress(done, len(pending), ""))
    result.elapsed = time.monotonic() - started
    return result


def _classify_one(
    *,
    father,
    candidates: list[Any],
    nodes: dict[int, Any],
    classifiers: list[Classifier],
    backend: ScanBackend,
    result: ScanResult,
) -> set[str]:
    """Apply every classifier to one father. At most one read per (node, file)."""
    matched: set[str] = set()
    buckets = group_by_file(classifiers)

    # Phase 1 — no IO. Exit codes, against the father and its failing CalcJobs.
    for classifier in buckets.get(None, ()):
        if classifier.matches_exit(father.exit_status):
            matched.add(classifier.tag)
            continue
        if any(classifier.matches_exit(ref.exit_status) for ref in candidates):
            matched.add(classifier.tag)

    # Phase 2 — one stream per (CalcJob, filename), serving every classifier
    # that targets that file.
    file_buckets = {name: cls for name, cls in buckets.items() if name is not None}
    if not file_buckets:
        return matched

    for ref in candidates:
        node = nodes.get(ref.pk)
        if node is None:
            continue
        for filename, bucket in file_buckets.items():
            remaining = [c for c in bucket if c.tag not in matched]
            if not remaining:
                continue
            found = _search_either_repository(
                backend, node, filename, remaining, result
            )
            matched |= found

    return matched


def _search_either_repository(
    backend: ScanBackend,
    node,
    filename: str,
    classifiers: list[Classifier],
    result: ScanResult,
) -> set[str]:
    """Look in the retrieved folder, then the input repository."""
    for file_type in ("output", "input"):
        try:
            return backend.search_file(node, filename, file_type, classifiers)
        except FileNotFoundError:
            continue
    result.missing_file[filename] += 1
    return set()
