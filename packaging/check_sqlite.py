"""CI preflight: the Python's SQLite must have FTS5 with the trigram tokenizer (SQLite >= 3.34)."""

import sqlite3

c = sqlite3.connect(":memory:")
c.execute("CREATE VIRTUAL TABLE t USING fts5(p, tokenize='trigram')")
c.execute("INSERT INTO t VALUES ('반사율 측정 결과.xlsx')")
assert c.execute("SELECT count(*) FROM t WHERE t MATCH ?", ('"사율 측"',)).fetchone()[0] == 1
print("sqlite", sqlite3.sqlite_version, "FTS5 trigram OK (Korean substring)")
