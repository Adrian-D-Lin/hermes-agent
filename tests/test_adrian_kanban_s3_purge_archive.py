"""Real filesystem recovery tests for v0.28 section 8.4 preservation."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def archive():
    path = Path(__file__).parents[1] / "plugins/adrian-kanban/purge_archive.py"
    spec = importlib.util.spec_from_file_location("purge_archive_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def test_archive_preserves_files_empty_directories_and_retry(archive, tmp_path):
    source = tmp_path / "old-task"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "notes.txt").write_bytes(b"irreplaceable notes\x00")
    before = archive.inventory(source)
    destination = tmp_path / "archived-task"
    result = archive.archive_path(source, destination, before)
    assert result["verified"] is True
    assert not source.exists()
    assert (destination / "empty").is_dir()
    assert (destination / "notes.txt").read_bytes() == b"irreplaceable notes\x00"
    assert archive.inventory(destination) == before
    assert archive.archive_path(source, destination, before) == result


def test_changed_source_is_not_moved(archive, tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("original", encoding="utf-8")
    expected = archive.inventory(source)
    source.write_text("new user changes", encoding="utf-8")
    destination = tmp_path / "archive"
    with pytest.raises(ValueError):
        archive.archive_path(source, destination, expected)
    assert source.read_text() == "new user changes"
    assert not destination.exists()


def test_existing_archive_is_never_overwritten(archive, tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "archive"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    with pytest.raises(ValueError):
        archive.archive_path(source, destination, archive.inventory(source))
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"


@pytest.mark.parametrize("reverse", [False, True])
def test_containing_paths_are_not_moved(archive, tmp_path, reverse):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    source, destination = (child, parent) if reverse else (parent, child / "archive")
    with pytest.raises(ValueError):
        archive.archive_path(source, destination, archive.inventory(source))
    assert child.is_dir()


def test_symlink_object_does_not_archive_its_target(archive, tmp_path):
    target = tmp_path / "external"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    source = tmp_path / "link"
    try:
        source.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    expected = archive.inventory(source)
    assert len(expected["entries"]) == 1
    assert expected["entries"][0]["type"] == "symlink"
    destination = tmp_path / "archived-link"
    archive.archive_path(source, destination, expected)
    assert destination.is_symlink()
    assert os.readlink(destination) == str(target)
    assert (target / "keep.txt").read_text() == "keep"


def test_nested_directory_symlink_inventory_does_not_read_external_files(
    archive, tmp_path
):
    source = tmp_path / "source"
    target = tmp_path / "external"
    source.mkdir()
    target.mkdir()
    (target / "private.txt").write_text("not part of archive", encoding="utf-8")
    link = source / "external-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create symlinks: {exc}")
    expected = archive.inventory(source)
    assert len(expected["entries"]) == 2
    assert {e["type"] for e in expected["entries"]} == {"directory", "symlink"}
    destination = tmp_path / "archive"
    archive.archive_path(source, destination, expected)
    assert (destination / "external-link").is_symlink()
    assert (target / "private.txt").read_text() == "not part of archive"


def test_git_worktree_archive_preserves_uncommitted_work_and_registration(
    archive, tmp_path
):
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "base",
    )
    source = tmp_path / "segment"
    _git(repo, "worktree", "add", "-b", "segment-branch", str(source))
    (source / "unfinished.txt").write_text("preserve work", encoding="utf-8")
    (source / ".gitignore").write_text("*.cache\n", encoding="utf-8")
    expected = archive.inventory(source, worktree=True)
    destination = tmp_path / "archived-segment"
    result = archive.archive_path(source, destination, expected, worktree=True)
    assert result["verified"] is True
    assert (destination / "unfinished.txt").read_text() == "preserve work"
    assert Path(_git(destination, "rev-parse", "--show-toplevel")) == destination
    assert _git(destination, "branch", "--show-current") == "segment-branch"
    assert archive.archive_path(source, destination, expected, worktree=True) == result
    assert archive.inventory(destination, worktree=True) == expected
    assert "worktree " + destination.as_posix() in _git(
        repo, "worktree", "list", "--porcelain"
    )


def test_main_checkout_cannot_be_archived_as_linked_worktree(archive, tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init")
    expected = archive.inventory(repo, worktree=True)
    with pytest.raises(ValueError):
        archive.archive_path(repo, tmp_path / "archive", expected, worktree=True)
    assert (repo / ".git").is_dir()
