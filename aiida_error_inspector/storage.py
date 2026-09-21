"""Persistent state: data-directory resolution, atomic JSON I/O, and the tag store.

Everything here is deliberately free of AiiDA and Textual imports so it can be
unit-tested without a profile or a running app.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

APP_NAME = "aiida-error-inspector"
ENV_DATA_DIR = "AIIDA_ERROR_INSPECTOR_DATA"

TAGS_FORMAT_VERSION = 2


# --------------------------------------------------------------------------- #
# Data directory
# --------------------------------------------------------------------------- #


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    """Pick where persistent state lives.

    Precedence: explicit (``--data-dir``) > ``$AIIDA_ERROR_INSPECTOR_DATA`` >
    the repository's ``data/`` directory when running from a source checkout >
    the platform user-data directory.

    The previous behaviour hardcoded ``<package>/../data``, which is not
    writable for a non-editable install.
    """
    if explicit:
        return _ensure(Path(explicit).expanduser())

    env_value = os.environ.get(ENV_DATA_DIR)
    if env_value:
        return _ensure(Path(env_value).expanduser())

    repo_data = Path(__file__).resolve().parent.parent / "data"
    if repo_data.is_dir():
        return repo_data

    try:
        from platformdirs import user_data_dir
    except ImportError:  # pragma: no cover - platformdirs is a declared dependency
        return _ensure(Path.home() / ".local" / "share" / APP_NAME)

    return _ensure(Path(user_data_dir(APP_NAME)))


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Atomic JSON I/O
# --------------------------------------------------------------------------- #


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via a temp file in the same directory, then ``os.replace``.

    A plain ``open(path, "w")`` truncates first, so an interrupt mid-write
    leaves a zero-length file — which for ``tags.json`` means losing every tag
    ever recorded. ``os.replace`` is atomic on POSIX and Windows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def quarantine(path: Path) -> Path | None:
    """Move an unparseable file aside so the next save cannot overwrite it."""
    if not path.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, target)
    except OSError as exc:
        logger.error("Could not quarantine %s: %s", path, exc)
        return None
    logger.warning("Quarantined unreadable %s as %s", path, target.name)
    return target


def load_json(path: Path, default: Any) -> tuple[Any, Path | None]:
    """Load JSON, quarantining the file if it cannot be parsed.

    Returns ``(value, quarantined_path)``. The caller is expected to surface a
    notification when ``quarantined_path`` is not None — silently resetting to
    an empty value (the previous behaviour) meant the next save destroyed the
    user's real data.
    """
    if not path.exists():
        return default, None
    try:
        with open(path, "r") as handle:
            return json.load(handle), None
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.error("Error loading %s: %s", path, exc)
        return default, quarantine(path)


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #


def parse_tags(data: Any) -> dict[int, set[str]]:
    """Decode any of the three on-disk tag layouts into ``{pk: {tag, ...}}``.

    Supported inputs:

    * ``{"version": 2, "tags": {tag: [pk, ...]}}`` — current.
    * ``{tag: [pk, ...]}`` — previous format, still on disk for most users.
    * ``{"pk": "tag"}`` — the original format.

    The old code sniffed the format with ``next(iter(data)).isdigit()``, which
    inspects only the *first* key. A tag named e.g. ``"305"`` (exactly what
    exit-code tagging produces) sorted first would make the whole file parse as
    the legacy layout. Dispatching on value types avoids that entirely.
    """
    if not isinstance(data, dict) or not data:
        return {}

    if isinstance(data.get("tags"), dict) and "version" in data:
        data = data["tags"]
        if not data:
            return {}

    tags: dict[int, set[str]] = {}
    values = list(data.values())

    if all(isinstance(value, (list, tuple)) for value in values):
        # {tag: [pk, ...]}
        for tag_name, pks in data.items():
            for pk in pks:
                try:
                    tags.setdefault(int(pk), set()).add(str(tag_name))
                except (TypeError, ValueError):
                    logger.warning("Skipping non-integer PK %r under tag %r", pk, tag_name)
        return tags

    if all(isinstance(value, str) for value in values):
        # legacy {pk: tag}
        for pk, tag_name in data.items():
            try:
                tags.setdefault(int(pk), set()).add(tag_name)
            except (TypeError, ValueError):
                logger.warning("Skipping non-integer PK %r in legacy tags", pk)
        return tags

    logger.error("Unrecognised tags layout; refusing to guess")
    return {}


def serialise_tags(tags: dict[int, set[str]]) -> dict[str, Any]:
    """Encode ``{pk: {tag, ...}}`` as ``{"version": 2, "tags": {tag: [pk, ...]}}``."""
    by_tag: dict[str, list[int]] = {}
    for pk, names in tags.items():
        for name in names:
            by_tag.setdefault(name, []).append(pk)
    for name in by_tag:
        by_tag[name].sort()
    return {
        "version": TAGS_FORMAT_VERSION,
        "tags": {name: by_tag[name] for name in sorted(by_tag)},
    }


def tag_counts(tags: dict[int, set[str]]) -> dict[str, int]:
    """Number of PKs carrying each tag."""
    counts: dict[str, int] = {}
    for names in tags.values():
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Scan cache
# --------------------------------------------------------------------------- #


def parse_scan_cache(data: Any) -> dict[int, set[str]]:
    """Decode ``scanned.json`` — ``{pk: [classifier fingerprint, ...]}``.

    Also accepts the superseded ``categorized.json`` payload (a flat list of
    PKs), which recorded only *matched* nodes and so forced a full re-read of
    every unmatched node on every re-scan. Migrated entries carry no
    fingerprints, so they are simply re-tested once against the current
    classifier set and then cached properly.
    """
    cache: dict[int, set[str]] = {}
    if isinstance(data, list):
        for pk in data:
            try:
                cache.setdefault(int(pk), set())
            except (TypeError, ValueError):
                continue
        return cache
    if isinstance(data, dict):
        for pk, fingerprints in data.items():
            try:
                cache[int(pk)] = {str(f) for f in fingerprints or ()}
            except (TypeError, ValueError):
                continue
    return cache


def serialise_scan_cache(cache: dict[int, set[str]]) -> dict[str, list[str]]:
    return {str(pk): sorted(fps) for pk, fps in sorted(cache.items())}


def merge_scan_cache(
    cache: dict[int, set[str]], pks: Iterable[int], fingerprints: Iterable[str]
) -> None:
    """Record that ``pks`` have now been tested against ``fingerprints``."""
    fps = set(fingerprints)
    for pk in pks:
        cache.setdefault(pk, set()).update(fps)
