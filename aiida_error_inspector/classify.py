"""Error classifiers: the rules that decide which tag a failed workchain gets.

Three kinds:

``substring``
    Case-insensitive (by default) substring match against a named file. This is
    what the tool has always done, and remains the default so existing
    ``patterns.json`` files load unchanged.
``regex``
    Same, but the pattern is a regular expression.
``exit_code``
    Match the failing CalcJob's ``exit_status``. Needs **no file read at all**,
    because the exit status comes back projected from the same query that finds
    the CalcJob — so a whole group can be classified by exit code at query speed.

Deliberately free of AiiDA and Textual imports so it can be unit-tested.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

KIND_SUBSTRING = "substring"
KIND_REGEX = "regex"
KIND_EXIT_CODE = "exit_code"
KINDS = (KIND_SUBSTRING, KIND_REGEX, KIND_EXIT_CODE)

_TEXT_KINDS = (KIND_SUBSTRING, KIND_REGEX)


class ClassifierError(ValueError):
    """A classifier definition is not usable."""


@dataclass(frozen=True)
class Classifier:
    """One tagging rule."""

    tag: str
    kind: str = KIND_SUBSTRING
    filename: str | None = None
    pattern: str | None = None
    case_sensitive: bool = False
    exit_code: int | None = None
    _regex: re.Pattern[str] | None = field(
        default=None, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        if not self.tag or not self.tag.strip():
            raise ClassifierError("Classifier needs a tag name")
        if self.kind not in KINDS:
            raise ClassifierError(f"Unknown classifier kind {self.kind!r}")

        if self.kind in _TEXT_KINDS:
            if not self.filename:
                raise ClassifierError(f"Tag {self.tag!r}: {self.kind} needs a filename")
            if not self.pattern:
                raise ClassifierError(f"Tag {self.tag!r}: {self.kind} needs a pattern")
            if self.kind == KIND_REGEX:
                flags = 0 if self.case_sensitive else re.IGNORECASE
                try:
                    compiled = re.compile(self.pattern, flags)
                except re.error as exc:
                    raise ClassifierError(
                        f"Tag {self.tag!r}: invalid regex {self.pattern!r} ({exc})"
                    ) from exc
                object.__setattr__(self, "_regex", compiled)
        elif self.exit_code is None:
            raise ClassifierError(f"Tag {self.tag!r}: exit_code classifier needs a code")

    # -- behaviour ---------------------------------------------------------- #

    def needs_file(self) -> str | None:
        """The file this classifier must read, or None if it reads nothing."""
        return self.filename if self.kind in _TEXT_KINDS else None

    def matches_text(self, text: str) -> bool:
        if self.kind == KIND_REGEX:
            assert self._regex is not None
            return self._regex.search(text) is not None
        if self.kind == KIND_SUBSTRING:
            assert self.pattern is not None
            if self.case_sensitive:
                return self.pattern in text
            return self.pattern.lower() in text.lower()
        return False

    def matches_exit(self, exit_status: int | None) -> bool:
        if self.kind != KIND_EXIT_CODE:
            return False
        return exit_status == self.exit_code

    def fingerprint(self) -> str:
        """Stable identity of this *rule*, used by the scan cache.

        Two classifiers with the same fingerprint ask the same question, so a
        node already tested against one need not be re-read for the other.
        Renaming the tag does change the fingerprint, which is intentional: the
        answer is recorded per tag.
        """
        payload = json.dumps(
            {
                "tag": self.tag,
                "kind": self.kind,
                "filename": self.filename,
                "pattern": self.pattern,
                "case_sensitive": self.case_sensitive,
                "exit_code": self.exit_code,
            },
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()[:16]

    # -- serialisation ------------------------------------------------------ #

    @classmethod
    def from_json(cls, tag: str, blob: Any) -> "Classifier":
        """Decode one ``patterns.json`` entry.

        An entry with no ``kind`` is read as a substring rule, so the three
        patterns already on disk keep working untouched.
        """
        if not isinstance(blob, dict):
            raise ClassifierError(f"Tag {tag!r}: expected an object, got {type(blob).__name__}")
        kind = blob.get("kind", KIND_SUBSTRING)
        exit_code = blob.get("exit_code")
        if exit_code is not None:
            try:
                exit_code = int(exit_code)
            except (TypeError, ValueError) as exc:
                raise ClassifierError(f"Tag {tag!r}: exit_code must be an integer") from exc
        return cls(
            tag=tag,
            kind=kind,
            filename=blob.get("filename"),
            pattern=blob.get("pattern"),
            case_sensitive=bool(blob.get("case_sensitive", False)),
            exit_code=exit_code,
        )

    def to_json(self) -> dict[str, Any]:
        if self.kind == KIND_EXIT_CODE:
            return {"kind": self.kind, "exit_code": self.exit_code}
        blob: dict[str, Any] = {
            "kind": self.kind,
            "filename": self.filename,
            "pattern": self.pattern,
        }
        if self.case_sensitive:
            blob["case_sensitive"] = True
        return blob

    def describe(self) -> str:
        """One-line human description, for the tag inspector."""
        if self.kind == KIND_EXIT_CODE:
            return f"exit_status == {self.exit_code}"
        flavour = "regex" if self.kind == KIND_REGEX else "contains"
        case = "" if self.case_sensitive else " (i)"
        return f"{flavour}{case} {self.pattern!r}"


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


def load_classifiers(data: Any) -> tuple[list[Classifier], list[str]]:
    """Decode a whole ``patterns.json``.

    Returns ``(classifiers, errors)``; a single malformed entry must not stop
    the rest from loading.
    """
    classifiers: list[Classifier] = []
    errors: list[str] = []
    if not isinstance(data, dict):
        return classifiers, ["patterns file is not an object"]
    for tag, blob in data.items():
        try:
            classifiers.append(Classifier.from_json(tag, blob))
        except ClassifierError as exc:
            errors.append(str(exc))
    return classifiers, errors


def dump_classifiers(classifiers: Iterable[Classifier]) -> dict[str, Any]:
    return {c.tag: c.to_json() for c in classifiers}


def group_by_file(classifiers: Iterable[Classifier]) -> dict[str | None, list[Classifier]]:
    """Bucket classifiers by the file they need.

    The scan reads each distinct file **once** and then asks every classifier in
    that bucket. The previous code looped patterns on the outside and workchains
    on the inside, so a node's ``aiida.out`` was re-read once per pattern.
    ``None`` buckets the classifiers that need no file at all.
    """
    buckets: dict[str | None, list[Classifier]] = {}
    for classifier in classifiers:
        buckets.setdefault(classifier.needs_file(), []).append(classifier)
    return buckets
