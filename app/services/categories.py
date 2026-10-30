"""Category catalogue management (Konzept §5): rename, color, active flag, order, subcategories.

Rules
-----
- Codes are stable (benchmarks, exports): a code is never changed. Main categories come from the seed
  (``app.db.seed.CATEGORY_CATALOGUE``); new main categories are not created here.
- Subcategories may be added freely. Their code is generated as ``<PARENT>.<n>`` (next free number) and
  never changes either. They inherit ``is_unterricht`` / ``counts_as_mehrarbeit_candidate`` from the
  parent; their color defaults to the parent's.
- Nothing is deleted (time entries reference categories, Konzept L4): a category is deactivated instead.
  Inactive categories disappear from pickers but stay valid for existing entries and reports.
- Names are unique among siblings (case-insensitive).
- The pseudo category ``KR`` (partial sick day) can be renamed/recolored but gets no subcategories.

Errors: :class:`CategoryError` (``ValueError``, German message for the UI) and
:class:`CategoryNotFound` (``LookupError``). Every mutating function commits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Category, TimeEntry

COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_NAME_LEN = 100


class CategoryError(ValueError):
    """Invalid category input; ``str(exc)`` is a German message for the UI."""


class CategoryNotFound(LookupError):
    pass


@dataclass
class CategoryNode:
    """A main category with its subcategories (sorted) and usage counts."""

    category: Category
    children: list[Category] = field(default_factory=list)
    usage: int = 0
    child_usage: dict[int, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- read


def get_category(db: Session, category_id: int | str) -> Category:
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        raise CategoryNotFound("Kategorie nicht gefunden.") from None
    cat = db.get(Category, cid)
    if cat is None:
        raise CategoryNotFound("Kategorie nicht gefunden.")
    return cat


def _usage_counts(db: Session) -> dict[int, int]:
    """Number of (not deleted) entries per category/subcategory id."""
    counts: dict[int, int] = {}
    for column in (TimeEntry.category_id, TimeEntry.subcategory_id):
        rows = db.execute(
            select(column, func.count()).where(TimeEntry.deleted_at.is_(None), column.is_not(None)).group_by(column)
        )
        for cid, n in rows:
            counts[int(cid)] = counts.get(int(cid), 0) + int(n)
    return counts


def list_tree(db: Session, *, include_inactive: bool = True, include_pseudo: bool = True) -> list[CategoryNode]:
    """Main categories in display order, each with its subcategories."""
    rows = list(db.scalars(select(Category).order_by(Category.sort, Category.code)))
    usage = _usage_counts(db)
    nodes: dict[int, CategoryNode] = {}
    for cat in rows:
        if cat.parent_id is None:
            if not include_pseudo and cat.is_pseudo:
                continue
            if not include_inactive and not cat.active:
                continue
            nodes[cat.id] = CategoryNode(cat, usage=usage.get(cat.id, 0))
    for cat in rows:
        if cat.parent_id is not None and cat.parent_id in nodes:
            if not include_inactive and not cat.active:
                continue
            node = nodes[cat.parent_id]
            node.children.append(cat)
            node.child_usage[cat.id] = usage.get(cat.id, 0)
    return list(nodes.values())


def siblings(db: Session, cat: Category) -> list[Category]:
    stmt = select(Category).order_by(Category.sort, Category.code, Category.id)
    if cat.parent_id is None:
        stmt = stmt.where(Category.parent_id.is_(None))
    else:
        stmt = stmt.where(Category.parent_id == cat.parent_id)
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------- validation


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise CategoryError("Bitte einen Namen angeben.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_LEN:
        raise CategoryError(f"Der Name ist zu lang (höchstens {MAX_NAME_LEN} Zeichen).")
    return name


def _clean_color(color: Any) -> str:
    if not isinstance(color, str) or not COLOR_RE.match(color.strip()):
        raise CategoryError("Farbe bitte als Hex-Wert angeben (z. B. #2563EB).")
    return color.strip().upper()


def _check_unique_name(db: Session, parent_id: int | None, name: str, exclude_id: int | None = None) -> None:
    stmt = select(Category).where(func.lower(Category.name) == name.lower())
    stmt = stmt.where(Category.parent_id.is_(None)) if parent_id is None else stmt.where(Category.parent_id == parent_id)
    for other in db.scalars(stmt):
        if other.id != exclude_id:
            where = "unter den Hauptkategorien" if parent_id is None else "in dieser Kategorie"
            raise CategoryError(f"Den Namen „{name}“ gibt es {where} schon.")


# --------------------------------------------------------------------------- write


def update_category(
    db: Session,
    category_id: int | str,
    *,
    name: str | None = None,
    color: str | None = None,
    active: bool | None = None,
    requires_note: bool | None = None,
    code: str | None = None,
) -> Category:
    """Change name, color, active flag and/or the note requirement. ``code`` must stay unchanged."""
    cat = get_category(db, category_id)
    if code is not None and code != cat.code:
        raise CategoryError(f"Der Code „{cat.code}“ ist fest und kann nicht geändert werden.")
    if name is not None:
        clean = _clean_name(name)
        if clean != cat.name:
            _check_unique_name(db, cat.parent_id, clean, exclude_id=cat.id)
            cat.name = clean
    if color is not None:
        cat.color = _clean_color(color)
    if active is not None:
        cat.active = bool(active)
    if requires_note is not None:
        cat.requires_note = bool(requires_note)
    db.commit()
    return cat


def _next_sub_code(db: Session, parent: Category) -> str:
    prefix = f"{parent.code}."
    used = set(db.scalars(select(Category.code).where(Category.code.like(f"{prefix}%"))))
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def add_subcategory(db: Session, parent_id: int | str, name: str, *, color: str | None = None) -> Category:
    """Add a subcategory below a main category (code ``<PARENT>.<n>``, sorted last)."""
    parent = get_category(db, parent_id)
    if parent.parent_id is not None:
        raise CategoryError("Unterkategorien sind nur unter Hauptkategorien möglich.")
    if parent.is_pseudo:
        raise CategoryError(f"„{parent.name}“ kann keine Unterkategorien haben.")
    clean = _clean_name(name)
    _check_unique_name(db, parent.id, clean)
    max_sort = db.scalar(select(func.max(Category.sort)).where(Category.parent_id == parent.id)) or 0
    child = Category(
        code=_next_sub_code(db, parent),
        name=clean,
        parent_id=parent.id,
        color=_clean_color(color) if color else parent.color,
        sort=int(max_sort) + 10,
        is_unterricht=parent.is_unterricht,
        counts_as_mehrarbeit_candidate=parent.counts_as_mehrarbeit_candidate,
        active=True,
    )
    db.add(child)
    db.commit()
    return child


def move_category(db: Session, category_id: int | str, direction: str) -> Category:
    """Move a category one place ``'up'`` or ``'down'`` among its siblings (renumbers sort in steps of 10)."""
    if direction not in ("up", "down"):
        raise CategoryError("Richtung muss „up“ oder „down“ sein.")
    cat = get_category(db, category_id)
    items = siblings(db, cat)
    index = next(i for i, c in enumerate(items) if c.id == cat.id)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(items):
        items[index], items[target] = items[target], items[index]
    for pos, item in enumerate(items, start=1):
        if item.sort != pos * 10:
            item.sort = pos * 10
    db.commit()
    return cat
