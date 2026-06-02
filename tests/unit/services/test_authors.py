from __future__ import annotations

import pytest

from app.domain.errors import TeamError
from app.domain.models import HumanAuthor
from app.persistence.db import Database
from app.persistence.repositories import AuthorRepo
from app.services.authors import AuthorService


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    database.init_schema()
    return database


@pytest.fixture
def service(db):
    return AuthorService(AuthorRepo(db))


def test_create_get_list_update_delete(service):
    a = service.create(HumanAuthor(name="Carol"))
    assert service.get(a.id).name == "Carol"
    assert [x.id for x in service.list()] == [a.id]
    a.name = "Caroline"
    service.update(a)
    assert service.get(a.id).name == "Caroline"
    service.delete(a.id)
    assert service.list() == []


def test_get_missing_raises(service):
    with pytest.raises(TeamError):
        service.get("nope")


def test_ensure_defaults_seeds_me_and_boss(service):
    defaults = service.ensure_defaults()
    assert defaults.me.name == "Me"
    assert defaults.boss.name == "Boss"
    names = {a.name for a in service.list()}
    assert names == {"Me", "Boss"}


def test_boss_default_weight_settings(service):
    defaults = service.ensure_defaults()
    assert defaults.boss.weight_enabled is True
    assert defaults.boss.weight_note.strip() != ""
    # Me is the operator's own voice: not weighted by default
    assert defaults.me.weight_enabled is False
    assert defaults.me.weight_note == ""


def test_ensure_defaults_is_idempotent(service):
    first = service.ensure_defaults()
    second = service.ensure_defaults()
    # no duplication on the second call
    assert len(service.list()) == 2
    assert {second.me.id, second.boss.id} == {first.me.id, first.boss.id}


def test_ensure_defaults_noop_when_authors_exist(service):
    existing = service.create(HumanAuthor(name="Custom"))
    service.ensure_defaults()
    # did NOT seed Me/Boss because authors already existed
    assert [a.id for a in service.list()] == [existing.id]
