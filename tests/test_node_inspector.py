"""Tests for the streaming file layer, using in-memory fake nodes."""

from __future__ import annotations

import pytest

from aiida_error_inspector import node_inspector as ni
from aiida_error_inspector.classify import Classifier

from .fakes import FakeCalcJob


@pytest.fixture
def node():
    return FakeCalcJob(
        pk=42,
        retrieved={
            "aiida.out": "\n".join(f"line {i}" for i in range(1, 1001)),
            "_scheduler-stderr.txt": "MPICH ERROR rank 3\n",
            "CRASH": "     Error in routine electrons (1):\n     convergence NOT achieved\n",
            "out/data-file.xml": "<xml/>",
        },
        inputs={"aiida.in": "&CONTROL\n calculation='scf'\n/\n", "_aiidasubmit.sh": "#!/bin/bash\n"},
    )


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_lists_every_retrieved_file_not_just_three(node):
    """CRASH and nested files used to be invisible: the list was hardcoded."""
    names = ni.list_files(node, "output")
    assert "CRASH" in names
    assert "out/data-file.xml" in names
    assert set(names) == {"aiida.out", "CRASH", "_scheduler-stderr.txt", "out/data-file.xml"}


def test_pinned_files_come_first_in_order(node):
    names = ni.list_files(node, "output")
    assert names[:3] == ["aiida.out", "CRASH", "_scheduler-stderr.txt"]


def test_list_all_files_tags_each_kind(node):
    files = ni.list_all_files(node)
    kinds = dict(files)
    assert kinds["aiida.out"] == "output"
    assert kinds["aiida.in"] == "input"


def test_missing_retrieved_folder_is_not_an_error():
    bare = FakeCalcJob(pk=7, retrieved=None, inputs={"aiida.in": "x"})
    assert ni.list_files(bare, "output") == []
    assert ni.list_files(bare, "input") == ["aiida.in"]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def test_iter_lines_streams(node):
    lines = list(ni.iter_lines(node, "aiida.out", "output"))
    assert len(lines) == 1000
    assert lines[0] == "line 1"
    assert lines[-1] == "line 1000"


def test_read_tail_lines(node):
    tail, total = ni.read_tail_lines(node, "aiida.out", "output", n=5)
    assert total == 1000
    assert tail == [f"line {i}" for i in range(996, 1001)]


def test_read_preview_elides_the_middle(node):
    preview = ni.read_preview(node, "aiida.out", "output", head=3, tail=3)
    assert preview.head == ["line 1", "line 2", "line 3"]
    assert preview.tail == ["line 998", "line 999", "line 1000"]
    assert preview.omitted == 994
    assert "994 lines omitted" in preview.render()


def test_read_preview_tail_only_is_the_default_shape(node):
    preview = ni.read_preview(node, "aiida.out", "output", head=0, tail=10)
    assert preview.head == []
    assert len(preview.tail) == 10


def test_read_preview_marks_truncation(node):
    preview = ni.read_preview(node, "aiida.out", "output", tail=10, max_bytes=100)
    assert preview.truncated
    assert "truncated" in preview.render()


def test_read_text_caps(node):
    text, truncated = ni.read_text(node, "aiida.out", "output", max_bytes=50)
    assert truncated
    assert len(text) <= 50


def test_missing_file_is_reported_not_raised(node):
    preview = ni.read_preview(node, "nope.txt", "output")
    assert "not found" in preview.render()


# --------------------------------------------------------------------------- #
# Sizes
# --------------------------------------------------------------------------- #


def test_file_size_streams_and_caps(node):
    size, capped = ni.file_size(node, "_scheduler-stderr.txt", "output")
    assert size == len("MPICH ERROR rank 3\n")
    assert not capped

    size, capped = ni.file_size(node, "aiida.out", "output", cap=100)
    assert capped
    assert size == 100


def test_format_size():
    assert ni.format_size(None) == "—"
    assert ni.format_size(512) == "512 B"
    assert ni.format_size(2048) == "2.0 KB"
    assert ni.format_size(100, hit_cap=True).startswith(">")


# --------------------------------------------------------------------------- #
# search_file — the single-pass primitive
# --------------------------------------------------------------------------- #


def test_search_file_reads_once_for_many_classifiers(node):
    classifiers = [
        Classifier(tag="a", filename="aiida.out", pattern="line 500"),
        Classifier(tag="b", filename="aiida.out", pattern="line 900"),
        Classifier(tag="c", filename="aiida.out", pattern="nowhere"),
    ]
    matched = ni.search_file(node, "aiida.out", "output", classifiers)
    assert matched == {"a", "b"}
    # One open, regardless of how many classifiers targeted the file.
    assert node.output_repo.open_calls == ["aiida.out"]


def test_search_file_short_circuits_once_everything_matched(node):
    """A hit near the top of a big file should not cost a full read."""
    classifiers = [Classifier(tag="a", filename="aiida.out", pattern="line 1")]
    assert ni.search_file(node, "aiida.out", "output", classifiers) == {"a"}


def test_search_file_finds_matches_outside_the_last_2000_lines(node):
    """The old scan only looked at the tail, so early errors were invisible."""
    classifiers = [Classifier(tag="early", filename="aiida.out", pattern="line 1")]
    assert ni.search_file(node, "aiida.out", "output", classifiers) == {"early"}


def test_search_file_regex(node):
    classifiers = [
        Classifier(tag="qe", kind="regex", filename="CRASH", pattern=r"Error in routine\s+(\w+)")
    ]
    assert ni.search_file(node, "CRASH", "output", classifiers) == {"qe"}


def test_search_file_missing_file_raises_for_the_caller_to_count(node):
    with pytest.raises(FileNotFoundError):
        ni.search_file(node, "absent.txt", "output", [Classifier(tag="a", filename="absent.txt", pattern="x")])


def test_search_file_with_no_classifiers_does_no_io(node):
    assert ni.search_file(node, "aiida.out", "output", []) == set()
    assert node.output_repo.open_calls == []


@pytest.mark.parametrize("content", ["", "\n\n", "   \n\t\n"])
def test_search_file_empty_rule_matches_blank_files(content):
    node = FakeCalcJob(pk=1, retrieved={"aiida.out": content})
    rule = Classifier(tag="empty", kind="empty_file", filename="aiida.out")
    assert ni.search_file(node, "aiida.out", "output", [rule]) == {"empty"}


def test_search_file_empty_rule_ignores_files_with_content(node):
    rule = Classifier(tag="empty", kind="empty_file", filename="aiida.out")
    assert ni.search_file(node, "aiida.out", "output", [rule]) == set()


def test_search_file_empty_rule_shares_the_read_with_text_rules(node):
    rules = [
        Classifier(tag="empty", kind="empty_file", filename="aiida.out"),
        Classifier(tag="late", filename="aiida.out", pattern="line 999"),
    ]
    assert ni.search_file(node, "aiida.out", "output", rules) == {"late"}
    assert node.output_repo.open_calls == ["aiida.out"]


def test_search_file_empty_rule_does_not_match_a_missing_file(node):
    rule = Classifier(tag="empty", kind="empty_file", filename="absent.txt")
    with pytest.raises(FileNotFoundError):
        ni.search_file(node, "absent.txt", "output", [rule])
