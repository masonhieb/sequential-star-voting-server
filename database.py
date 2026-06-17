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
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            INSERT OR IGNORE INTO settings (key, value) VALUES
                ('n_winners',       '1'),
                ('voting_mode',     'star'),
                ('election_title',  ''),
                ('election_state',  'ELECTION_ACTIVE'),
                ('entry_context',   ''),
                ('show_author',     '1'),
                ('app_mode',                  'standard'),
                ('codename_company_name',     ''),
                ('codename_enforce_letter',   '1');

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

            CREATE TABLE IF NOT EXISTS candidate_entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type   TEXT NOT NULL,
                value        TEXT NOT NULL,
                submitted_by TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS candidate_sets (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS candidate_set_items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                set_id     INTEGER NOT NULL REFERENCES candidate_sets(id) ON DELETE CASCADE,
                title      TEXT NOT NULL,
                body       TEXT NOT NULL DEFAULT '',
                author     TEXT,
                image_path TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            -- Company codenaming mode: tree-name codenames already used for a
            -- past company, keyed only by the company's first letter. The
            -- real company name is never stored here (or anywhere else
            -- persistent) — see settings.codename_company_name, which is
            -- transient and cleared once a codenaming round finishes.
            CREATE TABLE IF NOT EXISTS selected_codenames (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                codename              TEXT NOT NULL UNIQUE,
                company_first_letter  TEXT NOT NULL
            );

            -- Persistent pool of submitted codename candidates, keyed by
            -- first letter. Every accepted submission is saved here so past
            -- suggestions are auto-loaded into the candidate list when a new
            -- round opens for the same letter. Names stored lowercase.
            CREATE TABLE IF NOT EXISTS codename_candidates (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                letter TEXT NOT NULL,
                name   TEXT NOT NULL,
                UNIQUE(letter, name)
            );

        """
        )
        if not db.execute("SELECT 1 FROM rounds").fetchone():
            db.execute("INSERT INTO rounds (round_number, status) VALUES (1, 'voting')")
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


# ── Company codenaming helpers ──────────────────────────────────────────────


def codename_required_letter(company_name: str) -> Optional[str]:
    """First uppercase letter of the (private) company name, or None if it
    doesn't start with a letter (e.g. starts with a digit/symbol) — callers
    should treat None as "letter enforcement can't apply right now"."""
    name = (company_name or "").strip()
    return name[0].upper() if name and name[0].isalpha() else None


def is_codename_used(db: sqlite3.Connection, codename: str) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM selected_codenames WHERE codename = ?",
            (codename.strip().lower(),),
        ).fetchone()
    )


def codename_pool_for_letter(db: sqlite3.Connection, letter: str) -> list[dict]:
    """Pool candidates for a letter, alphabetically. name is lowercase."""
    rows = db.execute(
        "SELECT id, name FROM codename_candidates WHERE letter = ? ORDER BY name",
        (letter,),
    ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def codenames_for_letter(db: sqlite3.Connection, letter: str) -> list[str]:
    """Lowercase codenames already used for a given letter, oldest first.
    Stored form is always lowercase (the table's UNIQUE constraint is on
    that canonical form) — title-case at render time if desired."""
    rows = db.execute(
        "SELECT codename FROM selected_codenames"
        " WHERE company_first_letter = ? ORDER BY id",
        (letter,),
    ).fetchall()
    return [r["codename"] for r in rows]
