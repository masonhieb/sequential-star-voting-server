"""
Database initialization and pure query helpers for the STAR voting server.
"""

import sqlite3
from pathlib import Path
from typing import Optional


def get_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path) -> None:
    db = get_db(path)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL,
                body       TEXT NOT NULL DEFAULT '',
                author     TEXT,
                image_path TEXT
            );

            CREATE TABLE IF NOT EXISTS voters (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                last_name  TEXT NOT NULL,
                name_lower TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS rounds (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                round_number INTEGER NOT NULL UNIQUE,
                status       TEXT NOT NULL DEFAULT 'voting'
            );

            CREATE TABLE IF NOT EXISTS votes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                voter_id     INTEGER NOT NULL REFERENCES voters(id),
                candidate_id INTEGER NOT NULL REFERENCES candidates(id),
                round_id     INTEGER NOT NULL REFERENCES rounds(id),
                score        INTEGER NOT NULL CHECK(score >= 0 AND score <= 5),
                UNIQUE(voter_id, candidate_id, round_id)
            );

            CREATE TABLE IF NOT EXISTS ballots (
                voter_id INTEGER NOT NULL REFERENCES voters(id),
                round_id INTEGER NOT NULL REFERENCES rounds(id),
                PRIMARY KEY (voter_id, round_id)
            );

            CREATE TABLE IF NOT EXISTS winners (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id           INTEGER NOT NULL REFERENCES candidates(id),
                round_number           INTEGER NOT NULL,
                total_score            INTEGER NOT NULL,
                finalist1_id           INTEGER,
                finalist1_runoff_votes INTEGER,
                finalist2_id           INTEGER,
                finalist2_runoff_votes INTEGER,
                all_scores             TEXT
            );

            CREATE TABLE IF NOT EXISTS elections (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT NOT NULL DEFAULT 'Untitled Election',
                voting_mode  TEXT NOT NULL DEFAULT 'star',
                n_winners    INTEGER NOT NULL DEFAULT 1,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS election_results (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                election_id            INTEGER NOT NULL REFERENCES elections(id) ON DELETE CASCADE,
                place                  INTEGER NOT NULL,
                candidate_title        TEXT NOT NULL,
                total_score            INTEGER NOT NULL,
                finalist1_title        TEXT,
                finalist1_runoff_votes INTEGER,
                finalist2_title        TEXT,
                finalist2_runoff_votes INTEGER,
                all_scores             TEXT
            );

            CREATE TABLE IF NOT EXISTS election_ballots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                election_id     INTEGER NOT NULL REFERENCES elections(id) ON DELETE CASCADE,
                voter_name      TEXT NOT NULL,
                candidate_title TEXT NOT NULL,
                score           INTEGER NOT NULL
            );

            INSERT OR IGNORE INTO settings VALUES ('n_winners', '1');
            INSERT OR IGNORE INTO settings VALUES ('voting_mode', 'star');
            INSERT OR IGNORE INTO settings VALUES ('election_title', '');
        """)
        if not db.execute("SELECT 1 FROM rounds").fetchone():
            db.execute(
                "INSERT INTO rounds (round_number, status) VALUES (1, 'voting')"
            )
        db.commit()
    finally:
        db.close()


def get_setting(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def current_round(db: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM rounds ORDER BY round_number DESC LIMIT 1"
    ).fetchone()


def eliminated_ids(db: sqlite3.Connection) -> set[int]:
    return {r[0] for r in db.execute("SELECT candidate_id FROM winners").fetchall()}


def eligible_candidates(db: sqlite3.Connection) -> list[sqlite3.Row]:
    elim = eliminated_ids(db)
    return [
        c
        for c in db.execute("SELECT * FROM candidates ORDER BY id").fetchall()
        if c["id"] not in elim
    ]


def voter_has_voted(db: sqlite3.Connection, voter_id: int, round_id: int) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM ballots WHERE voter_id = ? AND round_id = ?",
            (voter_id, round_id),
        ).fetchone()
    )
