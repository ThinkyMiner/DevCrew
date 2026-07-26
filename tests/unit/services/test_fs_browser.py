"""Unit tests for the read-only directory browser backing the folder picker."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.errors import NotFound
from app.services.fs_browser import list_dirs


def test_lists_only_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "a-file.txt").write_text("x")

    listing = list_dirs(str(tmp_path))

    assert listing.path == str(tmp_path.resolve())
    names = [e.name for e in listing.entries]
    assert names == ["alpha", "beta"]  # sorted, files excluded
    assert all(Path(e.path).is_dir() for e in listing.entries)


def test_reports_parent(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    listing = list_dirs(str(child))
    assert listing.parent == str(tmp_path.resolve())


def test_root_has_no_parent() -> None:
    listing = list_dirs("/")
    assert listing.parent is None


def test_none_path_defaults_to_home() -> None:
    listing = list_dirs(None)
    assert listing.path == str(Path.home().resolve())


def test_missing_path_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(NotFound):
        list_dirs(str(tmp_path / "does-not-exist"))


def test_file_path_raises_not_found(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(NotFound):
        list_dirs(str(f))


def test_expands_user_tilde() -> None:
    listing = list_dirs("~")
    assert listing.path == str(Path.home().resolve())
