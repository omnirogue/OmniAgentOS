"""A1.5 golden corpus for additive-SQL detection (~50 cases, fail-closed)."""

from __future__ import annotations

import pytest

from omniagentos.policy.sqlcheck import is_additive_sql, looks_like_sql, strip_sql_comments

ADDITIVE = [
    "CREATE TABLE t (id TEXT PRIMARY KEY)",
    "create table if not exists t (id text)",
    "CREATE INDEX idx_t ON t(id)",
    "CREATE UNIQUE INDEX idx_u ON t(id)",
    "create unique index if not exists idx on t(a, b)",
    "ALTER TABLE t ADD COLUMN c TEXT",
    "alter table t add column c text not null default ''",
    "ALTER TABLE t ADD CONSTRAINT ck CHECK (c >= 0)",
    "CREATE VIRTUAL TABLE ft USING fts5(body)",
    "INSERT INTO t (id) VALUES ('x')",
    "insert into t select 1",
    "PRAGMA foreign_keys = ON",
    "BEGIN; CREATE TABLE t (id TEXT); COMMIT;",
    "CREATE TABLE a (id TEXT);\nCREATE TABLE b (id TEXT);\nCREATE INDEX i ON a(id);",
    "-- drop table legacy (historical note)\nCREATE TABLE t (id TEXT)",
    "/* delete from old_rows was done in v9 */\nALTER TABLE t ADD COLUMN v INT",
    "CREATE TABLE t (id TEXT); -- truncate happens elsewhere, not here",
    "\n\n  CREATE TABLE t (id TEXT)  ;\n\n",
    "CREATE TABLE T (ID TEXT); ALTER TABLE T ADD COLUMN N INT; INSERT INTO T VALUES ('a', 1);",
    "begin;\ncommit;",
]

NOT_ADDITIVE = [
    "DROP TABLE t",
    "drop table if exists t",
    "DELETE FROM t",
    "delete from t where id = 'x'",
    "TRUNCATE t",
    "truncate table t",
    "UPDATE t SET c = 1",
    "update t set c = c + 1 where id = 'x'",
    "ALTER TABLE t DROP COLUMN c",
    "ALTER TABLE t RENAME TO u",
    "CREATE TABLE t (id TEXT); DROP TABLE old",
    "CREATE TABLE t (id TEXT); DELETE FROM t",
    "ALTER TABLE users ADD COLUMN age INT; UPDATE users SET age = 0",
    "SELECT * FROM t",
    "REPLACE INTO t VALUES (1)",
    "CREATE TRIGGER trg AFTER INSERT ON t BEGIN DELETE FROM log; END",
    "CREATE VIEW v AS SELECT * FROM t",
    "",
    "   \n  ",
    "please remove the file and clean up",
    'sqlite3 app.db "CREATE TABLE t (id TEXT)"',
    "python -m omniagentos.db.migrate && echo done",
    "DROP TABLE a; -- CREATE TABLE b (id TEXT)",
    "Drop Table CaseMix",
    "dElEtE fRoM t",
    "/* unterminated comment hides this: DROP TABLE t",
    "-- only a comment, no statements",
    "CREATE TABLE t (id TEXT); TRUNCATE u;",
    "WITH x AS (SELECT 1) DELETE FROM t",
    "VACUUM",
]


@pytest.mark.parametrize("sql", ADDITIVE)
def test_additive_corpus(sql: str) -> None:
    assert is_additive_sql(sql) is True


@pytest.mark.parametrize("sql", NOT_ADDITIVE)
def test_not_additive_corpus(sql: str) -> None:
    assert is_additive_sql(sql) is False


def test_strip_comments_removes_line_and_block() -> None:
    out = strip_sql_comments("CREATE TABLE t (id TEXT); -- drop table x\n/* delete from y */")
    assert "drop table" not in out.lower()
    assert "delete from" not in out.lower()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CREATE TABLE t (id TEXT)", True),
        ("npm ci && npm test", False),
        ("please remove the file", False),
        ("select 1", True),
    ],
)
def test_looks_like_sql(text: str, expected: bool) -> None:
    assert looks_like_sql(text) is expected
