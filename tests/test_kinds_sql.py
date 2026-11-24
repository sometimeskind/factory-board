"""The names the SQL allows equal the names in `kinds.py`. No database: the migration is
read as text, by constraint name.
"""

from __future__ import annotations

import re

import pytest

from factory_board import kinds
from factory_board.config import DEFAULT_MIGRATIONS_DIR

ITEMS_SQL = (DEFAULT_MIGRATIONS_DIR / "0001_items.sql").read_text()
# In the order they are applied, so the last file that defines a constraint is the live one.
MIGRATIONS_SQL = [path.read_text() for path in sorted(DEFAULT_MIGRATIONS_DIR.glob("*.sql"))]


def allowed(constraint: str) -> tuple[str, ...]:
    """The quoted names in the one `IN (...)` list of a named CHECK constraint, as the last
    migration that defines it leaves it.
    """
    pattern = rf"CONSTRAINT {constraint}\s+CHECK \(\s*\w+ IN \(([^()]*)\)\s*\)"
    per_file = [found for sql in MIGRATIONS_SQL if (found := re.findall(pattern, sql))]
    assert per_file, f"{constraint}: expected one `<column> IN (...)` check"
    found = per_file[-1]
    assert len(found) == 1, f"{constraint}: expected one `<column> IN (...)` check"
    names = tuple(re.findall(r"'([^']*)'", found[0]))
    assert names, f"{constraint}: no names in the list"
    # Nothing in the list but quoted names, commas and whitespace.
    assert re.sub(r"'[^']*'|[,\s]", "", found[0]) == ""
    return names


@pytest.mark.parametrize(
    ("constraint", "names"),
    [
        ("items_kind_allowed", kinds.KINDS),
        ("items_machine_allowed", kinds.MACHINES),
        ("items_subject_type_allowed", kinds.SUBJECT_TYPES),
        ("items_assignee_allowed", ("operator", *kinds.STAGE_KINDS)),
    ],
)
def test_the_sql_allows_the_same_names(constraint: str, names: tuple[str, ...]) -> None:
    in_sql = allowed(constraint)
    assert len(set(in_sql)) == len(in_sql)
    assert set(in_sql) == set(names)


def test_the_operator_kinds_in_the_sql() -> None:
    found = re.findall(r"WHEN assignee = 'operator' THEN kind IN \(([^()]*)\)", ITEMS_SQL)
    assert len(found) == 1
    assert set(re.findall(r"'([^']*)'", found[0])) == set(kinds.OPERATOR_KINDS)


def test_the_reader_notices_a_difference() -> None:
    assert set(allowed("items_machine_allowed")) != {*kinds.MACHINES, "laptop"}
    with pytest.raises(AssertionError, match="expected one"):
        allowed("items_no_such_constraint")
