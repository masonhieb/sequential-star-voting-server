#!/usr/bin/env python3
"""
snapshot.py — export a read-only snapshot of the current in-progress election.

Usage:
  python snapshot.py                        # prints JSON to stdout
  python snapshot.py -o snapshot.json       # writes to file
  python snapshot.py --db path/to/voting.db -o out.json
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def get_setting(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot current election to JSON")
    parser.add_argument("--db", default="voting.db", help="Path to SQLite database")
    now_chicago = datetime.now(ZoneInfo("America/Chicago"))
    default_filename = now_chicago.strftime("snapshot-%Y-%m-%dT%H-%M-%S%z.json")
    parser.add_argument("-o", "--output", default=default_filename, help=f"Output file (default: {default_filename})")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: database '{db_path}' not found.", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    # ── Election metadata ────────────────────────────────────────────────────
    election_title = get_setting(db, "election_title") or "Untitled Election"
    voting_mode    = get_setting(db, "voting_mode", "star")
    n_winners      = int(get_setting(db, "n_winners", "1"))
    election_state = get_setting(db, "election_state", "ELECTION_ACTIVE")

    round_row = db.execute(
        "SELECT * FROM rounds ORDER BY round_number DESC LIMIT 1"
    ).fetchone()
    round_number = round_row["round_number"] if round_row else 1
    round_id     = round_row["id"]           if round_row else None
    round_status = round_row["status"]       if round_row else "unknown"

    # ── Candidates ───────────────────────────────────────────────────────────
    candidates = [
        {"id": c["id"], "title": c["title"], "author": c["author"]}
        for c in db.execute("SELECT id, title, author FROM candidates ORDER BY id").fetchall()
    ]
    cand_by_id = {c["id"]: c["title"] for c in candidates}

    # ── Votes & ballots for current round ────────────────────────────────────
    submitted_voter_ids: set[int] = set()
    if round_id is not None:
        submitted_voter_ids = {
            r[0] for r in db.execute(
                "SELECT voter_id FROM ballots WHERE round_id = ?", (round_id,)
            ).fetchall()
        }

    scores_by_voter: dict[int, dict[int, int]] = {}
    if round_id is not None:
        for row in db.execute(
            "SELECT voter_id, candidate_id, score FROM votes WHERE round_id = ?",
            (round_id,),
        ).fetchall():
            scores_by_voter.setdefault(row["voter_id"], {})[row["candidate_id"]] = row["score"]

    # ── Build ballot list (all registered voters) ────────────────────────────
    ballots = []
    for v in db.execute(
        "SELECT id, first_name, last_name FROM voters ORDER BY last_name, first_name"
    ).fetchall():
        voter_id   = v["id"]
        voter_name = f"{v['first_name']} {v['last_name']}"
        submitted  = voter_id in submitted_voter_ids
        raw_scores = scores_by_voter.get(voter_id, {})
        # Map candidate title → score for readability
        scores = {
            cand_by_id[cid]: score
            for cid, score in raw_scores.items()
            if cid in cand_by_id
        }
        ballots.append({
            "voter":     voter_name,
            "submitted": submitted,
            "scores":    scores,
        })

    db.close()

    # ── Tally total scores across submitted ballots ──────────────────────────
    totals: dict[str, int] = {c["title"]: 0 for c in candidates}
    for ballot in ballots:
        if ballot["submitted"]:
            for title, score in ballot["scores"].items():
                if title in totals:
                    totals[title] += score
    total_scores = dict(sorted(totals.items(), key=lambda x: -x[1]))

    # ── Assemble output ──────────────────────────────────────────────────────
    out = {
        "exported_at":    now_chicago.isoformat(),
        "election_title": election_title,
        "voting_mode":    voting_mode,
        "n_winners":      n_winners,
        "election_state": election_state,
        "round_number":   round_number,
        "round_status":   round_status,
        "candidates":     candidates,
        "total_scores":   total_scores,
        "ballots":        ballots,
    }

    text = json.dumps(out, indent=2, ensure_ascii=False)

    Path(args.output).write_text(text, encoding="utf-8")
    submitted_count = sum(1 for b in ballots if b["submitted"])
    print(
        f"Exported {len(ballots)} voters ({submitted_count} submitted) "
        f"— {len(candidates)} candidates → {args.output}"
    )


if __name__ == "__main__":
    main()
