#!/usr/bin/env python3
"""
Candidate set export / import tool.

Usage:
  python sets_tool.py list
  python sets_tool.py export output.json
  python sets_tool.py export output.json --set "My Set"
  python sets_tool.py import input.json
  python sets_tool.py import input.json --overwrite

All commands accept --db <path> (default: voting.db).
"""

import argparse
import json
import sys
from pathlib import Path

import database


def cmd_list(db_path: Path) -> None:
    db = database.get_db(db_path)
    try:
        sets = db.execute("SELECT * FROM candidate_sets ORDER BY name").fetchall()
        if not sets:
            print("No candidate sets found.")
            return
        for s in sets:
            count = db.execute(
                "SELECT COUNT(*) FROM candidate_set_items WHERE set_id = ?", (s["id"],)
            ).fetchone()[0]
            print(f"  [{s['id']:>3}] {s['name']}  ({count} candidate{'s' if count != 1 else ''})")
    finally:
        db.close()


def cmd_export(db_path: Path, output: Path, set_name: str | None) -> None:
    db = database.get_db(db_path)
    try:
        if set_name:
            rows = db.execute(
                "SELECT * FROM candidate_sets WHERE name = ?", (set_name,)
            ).fetchall()
            if not rows:
                print(f"Error: no set named '{set_name}'.", file=sys.stderr)
                sys.exit(1)
        else:
            rows = db.execute("SELECT * FROM candidate_sets ORDER BY name").fetchall()

        result = []
        for s in rows:
            items = [
                {
                    "title":      item["title"],
                    "body":       item["body"],
                    "author":     item["author"],
                    "image_path": item["image_path"],
                }
                for item in db.execute(
                    "SELECT * FROM candidate_set_items WHERE set_id = ? ORDER BY sort_order, id",
                    (s["id"],),
                ).fetchall()
            ]
            result.append({"name": s["name"], "items": items})
    finally:
        db.close()

    output.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    total_items = sum(len(s["items"]) for s in result)
    print(f"Exported {len(result)} set{'s' if len(result) != 1 else ''} "
          f"({total_items} candidates) → {output}")


def cmd_import(db_path: Path, input_path: Path, overwrite: bool) -> None:
    try:
        data = json.loads(input_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading {input_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, list):
        print("Error: JSON must be a list of set objects.", file=sys.stderr)
        sys.exit(1)

    db = database.get_db(db_path)
    try:
        imported = skipped = replaced = 0
        for s in data:
            name  = (s.get("name") or "").strip()
            items = s.get("items") or []
            if not name:
                print("  Warning: skipping set with empty name.")
                skipped += 1
                continue

            existing = db.execute(
                "SELECT id FROM candidate_sets WHERE name = ?", (name,)
            ).fetchone()

            if existing and not overwrite:
                print(f"  Skipped (already exists): {name}")
                skipped += 1
                continue

            if existing and overwrite:
                db.execute(
                    "DELETE FROM candidate_set_items WHERE set_id = ?", (existing["id"],)
                )
                set_id = existing["id"]
                replaced += 1
            else:
                db.execute("INSERT INTO candidate_sets (name) VALUES (?)", (name,))
                set_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                imported += 1

            for item in items:
                db.execute(
                    "INSERT INTO candidate_set_items (set_id, title, body, author, image_path)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        set_id,
                        (item.get("title") or "").strip(),
                        (item.get("body")  or "").strip(),
                        item.get("author")     or None,
                        item.get("image_path") or None,
                    ),
                )
            action = "Replaced" if (existing and overwrite) else "Imported"
            print(f"  {action}: {name} ({len(items)} candidates)")

        db.commit()
        print(f"\nDone — {imported} imported, {replaced} replaced, {skipped} skipped.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Candidate set export/import tool")
    parser.add_argument("--db", default="voting.db", help="Path to SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all candidate sets")

    exp = sub.add_parser("export", help="Export sets to a JSON file")
    exp.add_argument("output", type=Path, help="Output JSON file path")
    exp.add_argument("--set", dest="set_name", default=None,
                     help="Export only the set with this name (default: all)")

    imp = sub.add_parser("import", help="Import sets from a JSON file")
    imp.add_argument("input", type=Path, help="Input JSON file path")
    imp.add_argument("--overwrite", action="store_true",
                     help="Replace existing sets that share a name (default: skip)")

    args = parser.parse_args()
    db_path = Path(args.db)

    if not db_path.exists():
        print(f"Error: database '{db_path}' not found.", file=sys.stderr)
        sys.exit(1)

    if args.command == "list":
        cmd_list(db_path)
    elif args.command == "export":
        cmd_export(db_path, args.output, args.set_name)
    elif args.command == "import":
        cmd_import(db_path, args.input, args.overwrite)


if __name__ == "__main__":
    main()
