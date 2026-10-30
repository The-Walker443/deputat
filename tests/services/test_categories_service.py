"""Category catalogue management (app/services/categories.py)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Category
from app.services import categories as svc


def by_code(db, code: str) -> Category:
    return db.scalars(select(Category).where(Category.code == code)).one()


def test_tree_lists_main_categories_with_children(db):
    tree = svc.list_tree(db)
    codes = [n.category.code for n in tree]
    assert codes[:3] == ["U", "VN", "K"]
    assert "KR" in codes
    u = next(n for n in tree if n.category.code == "U")
    assert [c.name for c in u.children][:2] == ["Regelunterricht", "Förderunterricht"]
    no_pseudo = svc.list_tree(db, include_pseudo=False)
    assert "KR" not in [n.category.code for n in no_pseudo]


def test_rename_color_active_keeps_code(db):
    k = by_code(db, "K")
    svc.update_category(db, k.id, name="  Korrektur  ", color="#aa00ff", active=False)
    db.refresh(k)
    assert (k.code, k.name, k.color, k.active) == ("K", "Korrektur", "#AA00FF", False)


def test_code_is_immutable(db):
    k = by_code(db, "K")
    with pytest.raises(svc.CategoryError, match="fest"):
        svc.update_category(db, k.id, code="KX")
    svc.update_category(db, k.id, code="K", name="Korrekturen")  # same code is fine


@pytest.mark.parametrize("color", ["red", "#12345", "#GGGGGG", ""])
def test_invalid_color_rejected(db, color):
    with pytest.raises(svc.CategoryError):
        svc.update_category(db, by_code(db, "U").id, color=color)


def test_name_unique_among_siblings(db):
    with pytest.raises(svc.CategoryError, match="schon"):
        svc.update_category(db, by_code(db, "K").id, name="unterricht")
    with pytest.raises(svc.CategoryError):
        svc.update_category(db, by_code(db, "K").id, name="   ")


def test_add_subcategory_generates_stable_code_and_inherits_flags(db):
    u = by_code(db, "U")
    child = svc.add_subcategory(db, u.id, "Ganztag")
    assert child.code == "U.5"
    assert child.parent_id == u.id
    assert child.is_unterricht and child.counts_as_mehrarbeit_candidate
    assert child.color == u.color
    second = svc.add_subcategory(db, u.id, "Theater-AG", color="#112233")
    assert second.code == "U.6" and second.color == "#112233"
    assert second.sort > child.sort
    with pytest.raises(svc.CategoryError, match="schon"):
        svc.add_subcategory(db, u.id, "ganztag")


def test_subcategory_rules(db):
    sub = by_code(db, "U.1")
    with pytest.raises(svc.CategoryError, match="Hauptkategorien"):
        svc.add_subcategory(db, sub.id, "Unter-Unter")
    with pytest.raises(svc.CategoryError, match="keine Unterkategorien"):
        svc.add_subcategory(db, by_code(db, "KR").id, "Halber Tag")
    s = by_code(db, "S")
    child = svc.add_subcategory(db, s.id, "Diverses")
    assert child.code == "S.1"


def test_move_reorders_siblings(db):
    vn, k = by_code(db, "VN"), by_code(db, "K")
    svc.move_category(db, k.id, "up")
    codes = [c.code for c in svc.siblings(db, k)]
    assert codes.index("K") < codes.index("VN")
    # moving the first one up is a no-op
    first = svc.siblings(db, k)[0]
    svc.move_category(db, first.id, "up")
    assert svc.siblings(db, k)[0].id == first.id
    with pytest.raises(svc.CategoryError):
        svc.move_category(db, vn.id, "sideways")


def test_not_found(db):
    with pytest.raises(svc.CategoryNotFound):
        svc.get_category(db, 99999)
    with pytest.raises(svc.CategoryNotFound):
        svc.get_category(db, "abc")
