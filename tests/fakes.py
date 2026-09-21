"""In-memory stand-ins for AiiDA nodes, so the file layer can be tested without a profile."""

from __future__ import annotations

import contextlib
import io
from pathlib import PurePath


class FakeRepository:
    """Mimics ``NodeRepository`` closely enough for node_inspector."""

    def __init__(self, files: dict[str, str | bytes]):
        self._files = {
            name: (data.encode() if isinstance(data, str) else data)
            for name, data in files.items()
        }
        self.open_calls: list[str] = []

    def walk(self, path=None):
        tree: dict[str, list[str]] = {}
        for name in self._files:
            parent, _, leaf = name.rpartition("/")
            tree.setdefault(parent or ".", []).append(leaf)
        for root in sorted(tree):
            dirnames = sorted(d for d in tree if d != "." and d.rpartition("/")[0] == (root if root != "." else ""))
            yield PurePath(root), dirnames, sorted(tree[root])

    @contextlib.contextmanager
    def open(self, path, mode="rb"):
        if path not in self._files:
            raise FileNotFoundError(path)
        self.open_calls.append(path)
        handle = io.BytesIO(self._files[path])
        try:
            yield handle
        finally:
            handle.close()


class _Base:
    def __init__(self, repository):
        self.repository = repository


class _Outputs:
    def __init__(self, retrieved):
        if retrieved is None:
            raise AttributeError("retrieved")
        self.retrieved = retrieved


class FakeFolder:
    """Stands in for the ``retrieved`` FolderData."""

    def __init__(self, files):
        self.base = _Base(FakeRepository(files))


class FakeCalcJob:
    """Stands in for a CalcJobNode."""

    def __init__(self, pk=1, retrieved=None, inputs=None):
        self.pk = pk
        self.base = _Base(FakeRepository(inputs or {}))
        self._retrieved = FakeFolder(retrieved) if retrieved is not None else None

    @property
    def outputs(self):
        if self._retrieved is None:
            raise AttributeError("no retrieved output")
        return _Outputs(self._retrieved)

    # Convenience for assertions in tests.
    @property
    def output_repo(self) -> FakeRepository:
        return self._retrieved.base.repository

    @property
    def input_repo(self) -> FakeRepository:
        return self.base.repository
