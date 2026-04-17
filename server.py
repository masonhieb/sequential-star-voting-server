#!/usr/bin/env python3
"""
N Sequential STAR Voting Server

STAR voting (Score Then Automatic Runoff) with N sequential rounds.
Each round elects one winner who is removed from the pool; winners are stored
in a ranked hierarchy (1st, 2nd, … Nth place).
"""

import argparse
import asyncio
import io
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

import markdown as md_lib
from aiohttp import web

import database
import test_data

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

MASON_FIRST_NAME = "mason"
MASON_LAST_NAME  = "hieb"
ADMIN_PASSWORD   = "hunter2"
MAX_IMAGE_DIM = 500
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def render_markdown(text: str) -> str:
    return md_lib.markdown(text or "", extensions=["extra", "nl2br"])


class VotingServer:
    def __init__(
        self,
        db_path: str = "voting.db",
        images_dir: str = "images",
        templates_dir: str = "templates",
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.db_path = Path(db_path)
        self.images_dir = Path(images_dir)
        self.templates_dir = Path(templates_dir)
        self.host = host
        self.port = port
        # voter_name_lower -> asyncio.Queue for SSE
        self._sse_clients: dict[str, asyncio.Queue] = {}

        self.images_dir.mkdir(exist_ok=True)
        self.templates_dir.mkdir(exist_ok=True)

        self.app = web.Application(middlewares=[self._make_db_middleware()])
        self._setup_routes()
        database.init_db(self.db_path)

    # ── Middleware ────────────────────────────────────────────────────────────

    def _make_db_middleware(self):
        db_path = self.db_path

        @web.middleware
        async def db_middleware(request: web.Request, handler):
            request["db"] = database.get_db(db_path)
            try:
                return await handler(request)
            finally:
                request["db"].close()

        return db_middleware

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_mason(self, first_name: str, last_name: str) -> bool:
        return (
            first_name.strip().lower() == MASON_FIRST_NAME
            and last_name.strip().lower() == MASON_LAST_NAME
        )

    def _require_mason(self, data: dict) -> Optional[web.Response]:
        parts = data.get("voter_name", "").strip().split(None, 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        if not self._is_mason(first, last):
            return web.json_response(
                {"error": "Forbidden: Mason Hieb only"}, status=403
            )
        return None

    # ── State building ────────────────────────────────────────────────────────

    def _build_voters_data(self, db: sqlite3.Connection) -> list[dict]:
        round_row = database.current_round(db)
        if not round_row:
            return []
        round_id = round_row["id"]
        active = set(self._sse_clients.keys())
        return [
            {
                "id": v["id"],
                "first_name": v["first_name"],
                "last_name": v["last_name"],
                "name": f"{v['first_name']} {v['last_name']}",
                "voted": database.voter_has_voted(db, v["id"], round_id),
                "active": v["name_lower"] in active,
            }
            for v in db.execute(
                "SELECT * FROM voters ORDER BY last_name, first_name"
            ).fetchall()
        ]

    def _build_state(self, db: sqlite3.Connection) -> dict:
        n_winners      = int(database.get_setting(db, "n_winners", "1"))
        voting_mode    = database.get_setting(db, "voting_mode", "star")
        election_title = database.get_setting(db, "election_title", "")
        round_row   = database.current_round(db)
        if not round_row:
            return {}

        round_id     = round_row["id"]
        round_number = round_row["round_number"]
        round_status = round_row["status"]

        eligible = database.eligible_candidates(db)
        candidates_data = [
            {
                "id": c["id"],
                "title": c["title"],
                "body_html": render_markdown(c["body"]),
                "author": c["author"],
                "image_url": f"/images/{c['image_path']}" if c["image_path"] else None,
            }
            for c in eligible
        ]

        winners_data = []
        for w in db.execute(
            """SELECT w.*, c.title  AS cand_title,
                      f1.title AS f1_title,
                      f2.title AS f2_title
               FROM winners w
               JOIN candidates c  ON w.candidate_id  = c.id
               LEFT JOIN candidates f1 ON w.finalist1_id = f1.id
               LEFT JOIN candidates f2 ON w.finalist2_id = f2.id
               ORDER BY w.round_number"""
        ).fetchall():
            raw = json.loads(w["all_scores"]) if w["all_scores"] else {}
            all_scores_list = []
            for cid_str, score in sorted(raw.items(), key=lambda x: -x[1]):
                cid  = int(cid_str)
                cand = db.execute(
                    "SELECT title FROM candidates WHERE id = ?", (cid,)
                ).fetchone()
                all_scores_list.append(
                    {
                        "candidate_id": cid,
                        "title": cand["title"] if cand else str(cid),
                        "score": score,
                    }
                )
            winners_data.append(
                {
                    "round_number": w["round_number"],
                    "candidate_id": w["candidate_id"],
                    "candidate_title": w["cand_title"],
                    "total_score": w["total_score"],
                    "finalist1_id": w["finalist1_id"],
                    "finalist1_title": w["f1_title"],
                    "finalist1_runoff_votes": w["finalist1_runoff_votes"],
                    "finalist2_id": w["finalist2_id"],
                    "finalist2_title": w["f2_title"],
                    "finalist2_runoff_votes": w["finalist2_runoff_votes"],
                    "all_scores": all_scores_list,
                }
            )

        return {
            "n_winners": n_winners,
            "voting_mode": voting_mode,
            "election_title": election_title,
            "round_number": round_number,
            "round_id": round_id,
            "round_status": round_status,
            "candidates": candidates_data,
            "winners": winners_data,
            "voters": self._build_voters_data(db),
            "active_voter_names": list(self._sse_clients.keys()),
            "election_complete": (
                len(winners_data) >= n_winners or round_status == "complete"
            ),
        }

    # ── Voting algorithms ─────────────────────────────────────────────────────

    def _compute_star_winner(
        self, db: sqlite3.Connection, round_id: int
    ) -> Optional[dict]:
        eligible     = database.eligible_candidates(db)
        eligible_ids = [c["id"] for c in eligible]

        if not eligible_ids:
            return None

        if len(eligible_ids) == 1:
            cid = eligible_ids[0]
            return {
                "winner_id": cid,
                "total_scores": {cid: 0},
                "finalist1_id": cid,
                "finalist1_votes": 0,
                "finalist2_id": None,
                "finalist2_votes": 0,
            }

        votes = db.execute(
            "SELECT voter_id, candidate_id, score FROM votes WHERE round_id = ?",
            (round_id,),
        ).fetchall()

        voter_scores: dict[int, dict[int, int]] = {}
        for v in votes:
            voter_scores.setdefault(v["voter_id"], {})[v["candidate_id"]] = v["score"]

        totals: dict[int, int] = {cid: 0 for cid in eligible_ids}
        for ballot in voter_scores.values():
            for cid, score in ballot.items():
                if cid in totals:
                    totals[cid] += score

        sorted_ids  = sorted(eligible_ids, key=lambda x: totals[x], reverse=True)
        f1_id, f2_id = sorted_ids[0], sorted_ids[1]

        f1_votes = f2_votes = 0
        for ballot in voter_scores.values():
            s1 = ballot.get(f1_id, 0)
            s2 = ballot.get(f2_id, 0)
            if s1 > s2:
                f1_votes += 1
            elif s2 > s1:
                f2_votes += 1

        winner_id = f1_id if f1_votes >= f2_votes else f2_id

        return {
            "winner_id": winner_id,
            "total_scores": totals,
            "finalist1_id": f1_id,
            "finalist1_votes": f1_votes,
            "finalist2_id": f2_id,
            "finalist2_votes": f2_votes,
        }

    def _compute_score_winners(
        self, db: sqlite3.Connection, round_id: int, n: int
    ) -> list[dict]:
        """Return top-N candidates ordered by total score (no runoff)."""
        eligible     = database.eligible_candidates(db)
        eligible_ids = [c["id"] for c in eligible]

        totals: dict[int, int] = {cid: 0 for cid in eligible_ids}
        for row in db.execute(
            "SELECT candidate_id, SUM(score) AS total FROM votes"
            " WHERE round_id = ? GROUP BY candidate_id",
            (round_id,),
        ).fetchall():
            if row["candidate_id"] in totals:
                totals[row["candidate_id"]] = row["total"]

        ranked = sorted(eligible_ids, key=lambda x: totals[x], reverse=True)
        return [
            {"candidate_id": cid, "total_score": totals[cid]}
            for cid in ranked[:n]
        ]

    # ── SSE broadcast ─────────────────────────────────────────────────────────

    async def _broadcast(self, event_type: str, data) -> None:
        msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        dead = []
        for name, q in list(self._sse_clients.items()):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(name)
        for name in dead:
            self._sse_clients.pop(name, None)

    # ── Routes ────────────────────────────────────────────────────────────────

    def _setup_routes(self):
        r = self.app.router
        r.add_get("/",           self._index)
        r.add_get("/admin",      self._admin_page)
        r.add_get("/api/stream", self._stream)
        r.add_get("/api/state",      self._get_state)
        r.add_get("/api/candidates", self._get_candidates)
        r.add_get("/api/voters",     self._get_voters)
        r.add_post("/api/register",  self._register)
        r.add_post("/api/vote",      self._vote)
        r.add_post("/api/admin/login",                self._admin_login)
        r.add_post("/api/admin/candidates",           self._admin_add_candidate)
        r.add_delete("/api/admin/candidates/{id}",    self._admin_delete_candidate)
        r.add_post("/api/admin/generate-test",        self._admin_generate_test)
        r.add_post("/api/admin/clear-candidates",     self._admin_clear_candidates)
        r.add_post("/api/admin/settings",             self._admin_update_settings)
        r.add_post("/api/admin/reveal",               self._admin_reveal_winner)
        r.add_post("/api/admin/reset",                self._admin_reset)
        r.add_get("/api/admin/elections",             self._admin_get_elections)
        r.add_post("/api/admin/unsubmit",             self._admin_unsubmit)
        r.add_post("/api/admin/upload-image",         self._admin_upload_image)
        r.add_get("/api/my-scores",                   self._get_my_scores)
        r.add_static("/images", self.images_dir, name="images")

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=(self.templates_dir / "index.html").read_text(),
            content_type="text/html",
        )

    async def _admin_page(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=(self.templates_dir / "admin.html").read_text(),
            content_type="text/html",
        )

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        voter_name = request.query.get("voter_name", "").strip()
        name_lower = voter_name.lower() if voter_name else None
        db = request["db"]

        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        if name_lower:
            self._sse_clients[name_lower] = q

        resp = web.StreamResponse()
        resp.headers.update(
            {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            }
        )
        await resp.prepare(request)

        await resp.write(
            f"event: state\ndata: {json.dumps(self._build_state(db))}\n\n".encode()
        )
        if name_lower:
            await self._broadcast("voters_update", self._build_voters_data(db))

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                    await resp.write(msg.encode())
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except Exception:
            pass
        finally:
            if name_lower and self._sse_clients.get(name_lower) is q:
                self._sse_clients.pop(name_lower, None)
            await self._broadcast("voters_update", self._build_voters_data(db))

        return resp

    async def _get_state(self, request: web.Request) -> web.Response:
        return web.json_response(self._build_state(request["db"]))

    async def _get_candidates(self, request: web.Request) -> web.Response:
        rows = request["db"].execute("SELECT * FROM candidates ORDER BY id").fetchall()
        return web.json_response(
            [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "body": c["body"],
                    "body_html": render_markdown(c["body"]),
                    "author": c["author"],
                    "image_url": f"/images/{c['image_path']}" if c["image_path"] else None,
                }
                for c in rows
            ]
        )

    async def _get_voters(self, request: web.Request) -> web.Response:
        return web.json_response(self._build_voters_data(request["db"]))

    async def _get_my_scores(self, request: web.Request) -> web.Response:
        db = request["db"]
        voter_name = request.query.get("voter_name", "").strip()
        if not voter_name:
            return web.json_response({})
        voter = db.execute(
            "SELECT * FROM voters WHERE name_lower = ?", (voter_name.lower(),)
        ).fetchone()
        if not voter:
            return web.json_response({})
        round_row = database.current_round(db)
        if not round_row:
            return web.json_response({})
        scores = {
            str(row["candidate_id"]): row["score"]
            for row in db.execute(
                "SELECT candidate_id, score FROM votes WHERE voter_id = ? AND round_id = ?",
                (voter["id"], round_row["id"]),
            ).fetchall()
        }
        return web.json_response(scores)

    async def _register(self, request: web.Request) -> web.Response:
        db   = request["db"]
        data = await request.json()
        first_name = data.get("first_name", "").strip()
        last_name  = data.get("last_name",  "").strip()

        if not first_name:
            return web.json_response({"error": "First name required"}, status=400)
        if not last_name:
            return web.json_response({"error": "Last name required"}, status=400)

        name_lower = f"{first_name} {last_name}".lower()
        existing   = db.execute(
            "SELECT * FROM voters WHERE name_lower = ?", (name_lower,)
        ).fetchone()

        if existing:
            if existing["name_lower"] in self._sse_clients:
                return web.json_response({"error": "That user is already signed in."}, status=409)
            return web.json_response(
                {
                    "id":           existing["id"],
                    "first_name":   existing["first_name"],
                    "last_name":    existing["last_name"],
                    "name":         f"{existing['first_name']} {existing['last_name']}",
                    "is_mason":     self._is_mason(existing["first_name"], existing["last_name"]),
                    "already_exists": True,
                }
            )

        db.execute(
            "INSERT INTO voters (first_name, last_name, name_lower) VALUES (?,?,?)",
            (first_name, last_name, name_lower),
        )
        db.commit()
        voter = db.execute(
            "SELECT * FROM voters WHERE name_lower = ?", (name_lower,)
        ).fetchone()

        await self._broadcast("voters_update", self._build_voters_data(db))

        return web.json_response(
            {
                "id":         voter["id"],
                "first_name": voter["first_name"],
                "last_name":  voter["last_name"],
                "name":       f"{voter['first_name']} {voter['last_name']}",
                "is_mason":   self._is_mason(voter["first_name"], voter["last_name"]),
                "already_exists": False,
            }
        )

    async def _vote(self, request: web.Request) -> web.Response:
        db         = request["db"]
        data       = await request.json()
        voter_name = data.get("voter_name", "").strip()
        scores     = data.get("scores", {})

        if not voter_name:
            return web.json_response({"error": "voter_name required"}, status=400)

        voter = db.execute(
            "SELECT * FROM voters WHERE name_lower = ?", (voter_name.lower(),)
        ).fetchone()
        if not voter:
            return web.json_response({"error": "Voter not found"}, status=404)

        round_row = database.current_round(db)
        if not round_row or round_row["status"] != "voting":
            return web.json_response({"error": "No active voting round"}, status=400)

        round_id     = round_row["id"]
        eligible_ids = {c["id"] for c in database.eligible_candidates(db)}

        if {int(k) for k in scores.keys()} != eligible_ids:
            return web.json_response(
                {"error": "Must score all eligible candidates"}, status=400
            )

        for cid_str, score in scores.items():
            cid = int(cid_str)
            if not isinstance(score, int) or score < 0 or score > 5:
                return web.json_response(
                    {"error": f"Score must be 0–5, got {score!r}"}, status=400
                )
            db.execute(
                """INSERT INTO votes (voter_id, candidate_id, round_id, score)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(voter_id, candidate_id, round_id)
                   DO UPDATE SET score = excluded.score""",
                (voter["id"], cid, round_id, score),
            )

        db.execute(
            "INSERT OR REPLACE INTO ballots (voter_id, round_id) VALUES (?, ?)",
            (voter["id"], round_id),
        )
        db.commit()

        await self._broadcast("voters_update", self._build_voters_data(db))
        return web.json_response({"ok": True})

    # ── Admin endpoints ───────────────────────────────────────────────────────

    async def _admin_add_candidate(self, request: web.Request) -> web.Response:
        db   = request["db"]
        data = await request.json()
        title = data.get("title", "").strip()
        if not title:
            return web.json_response({"error": "Title required"}, status=400)
        body       = (data.get("body")       or "").strip()
        author     = (data.get("author")     or "").strip() or None
        image_path = (data.get("image_path") or "").strip() or None

        db.execute(
            "INSERT INTO candidates (title, body, author, image_path) VALUES (?,?,?,?)",
            (title, body, author, image_path),
        )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_delete_candidate(self, request: web.Request) -> web.Response:
        db  = request["db"]
        cid = int(request.match_info["id"])
        if db.execute(
            "SELECT 1 FROM votes WHERE candidate_id = ?", (cid,)
        ).fetchone():
            return web.json_response(
                {"error": "Cannot delete a candidate that has received votes"},
                status=400,
            )
        db.execute("DELETE FROM candidates WHERE id = ?", (cid,))
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_clear_candidates(self, request: web.Request) -> web.Response:
        db = request["db"]
        if db.execute("SELECT 1 FROM votes LIMIT 1").fetchone():
            return web.json_response(
                {"error": "Reset the election first to clear all votes, then you can clear candidates"},
                status=400,
            )
        db.execute("DELETE FROM candidates")
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_generate_test(self, request: web.Request) -> web.Response:
        db = request["db"]
        if db.execute("SELECT 1 FROM candidates LIMIT 1").fetchone():
            return web.json_response(
                {"error": "Candidates already exist — clear them before generating test data"},
                status=400,
            )
        candidates = test_data.generate_test_candidates(10)
        for c in candidates:
            db.execute(
                "INSERT INTO candidates (title, body, author, image_path) VALUES (?,?,?,?)",
                (c["title"], c["body"], c["author"], c["image_path"]),
            )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True, "count": len(candidates)})

    async def _admin_update_settings(self, request: web.Request) -> web.Response:
        db   = request["db"]
        data = await request.json()
        err  = self._require_mason(data)
        if err:
            return err
        if "n_winners" in data:
            n = int(data["n_winners"])
            if n < 1:
                return web.json_response({"error": "n_winners must be ≥ 1"}, status=400)
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('n_winners', ?)", (str(n),)
            )
        if "voting_mode" in data:
            mode = data["voting_mode"]
            if mode not in ("star", "score"):
                return web.json_response(
                    {"error": "voting_mode must be 'star' or 'score'"}, status=400
                )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('voting_mode', ?)", (mode,)
            )
        if "election_title" in data:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('election_title', ?)",
                (str(data["election_title"]).strip(),),
            )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_reveal_winner(self, request: web.Request) -> web.Response:
        db   = request["db"]
        data = await request.json()
        err  = self._require_mason(data)
        if err:
            return err

        round_row = database.current_round(db)
        if not round_row or round_row["status"] != "voting":
            return web.json_response({"error": "No active voting round"}, status=400)

        n_winners      = int(database.get_setting(db, "n_winners", "1"))
        voting_mode    = database.get_setting(db, "voting_mode", "star")
        existing_count = db.execute("SELECT COUNT(*) FROM winners").fetchone()[0]
        if existing_count >= n_winners:
            return web.json_response(
                {"error": "All winners already elected"}, status=400
            )

        round_number    = round_row["round_number"]
        all_scores_json: str

        if voting_mode == "score":
            ranked = self._compute_score_winners(db, round_row["id"], n_winners)
            if not ranked:
                return web.json_response({"error": "No eligible candidates"}, status=400)
            eligible_ids = [c["id"] for c in database.eligible_candidates(db)]
            totals: dict[int, int] = {cid: 0 for cid in eligible_ids}
            for row in db.execute(
                "SELECT candidate_id, SUM(score) AS total FROM votes"
                " WHERE round_id = ? GROUP BY candidate_id",
                (round_row["id"],),
            ).fetchall():
                if row["candidate_id"] in totals:
                    totals[row["candidate_id"]] = row["total"]
            all_scores_json = json.dumps({str(k): v for k, v in totals.items()})
            for place, w in enumerate(ranked, start=1):
                db.execute(
                    "INSERT INTO winners (candidate_id, round_number, total_score, all_scores)"
                    " VALUES (?,?,?,?)",
                    (w["candidate_id"], place, w["total_score"], all_scores_json),
                )
            db.execute(
                "UPDATE rounds SET status = 'complete' WHERE id = ?", (round_row["id"],)
            )
        else:
            result = self._compute_star_winner(db, round_row["id"])
            if not result:
                return web.json_response({"error": "No eligible candidates"}, status=400)
            winner_id    = result["winner_id"]
            total_scores = result["total_scores"]
            all_scores_json = json.dumps({str(k): v for k, v in total_scores.items()})
            db.execute(
                """INSERT INTO winners
                   (candidate_id, round_number, total_score,
                    finalist1_id, finalist1_runoff_votes,
                    finalist2_id, finalist2_runoff_votes, all_scores)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    winner_id,
                    round_number,
                    total_scores.get(winner_id, 0),
                    result.get("finalist1_id"),
                    result.get("finalist1_votes"),
                    result.get("finalist2_id"),
                    result.get("finalist2_votes"),
                    all_scores_json,
                ),
            )
            new_count = existing_count + 1
            if new_count >= n_winners:
                db.execute(
                    "UPDATE rounds SET status = 'complete' WHERE id = ?", (round_row["id"],)
                )
            else:
                db.execute(
                    "UPDATE rounds SET status = 'revealed' WHERE id = ?", (round_row["id"],)
                )
                db.execute(
                    "INSERT INTO rounds (round_number, status) VALUES (?, 'voting')",
                    (round_number + 1,),
                )

        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_reset(self, request: web.Request) -> web.Response:
        db   = request["db"]
        data = await request.json()
        err  = self._require_mason(data)
        if err:
            return err

        # Snapshot any completed winners into election history before wiping
        if db.execute("SELECT 1 FROM winners LIMIT 1").fetchone():
            title       = database.get_setting(db, "election_title", "") or "Untitled Election"
            voting_mode = database.get_setting(db, "voting_mode", "star")
            n_winners   = int(database.get_setting(db, "n_winners", "1"))
            db.execute(
                "INSERT INTO elections (title, voting_mode, n_winners) VALUES (?,?,?)",
                (title, voting_mode, n_winners),
            )
            election_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            for w in db.execute(
                """SELECT w.round_number, w.total_score,
                          w.finalist1_runoff_votes, w.finalist2_runoff_votes,
                          w.all_scores,
                          c.title  AS cand_title,
                          f1.title AS f1_title,
                          f2.title AS f2_title
                   FROM winners w
                   JOIN candidates c  ON w.candidate_id  = c.id
                   LEFT JOIN candidates f1 ON w.finalist1_id = f1.id
                   LEFT JOIN candidates f2 ON w.finalist2_id = f2.id
                   ORDER BY w.round_number"""
            ).fetchall():
                raw = json.loads(w["all_scores"]) if w["all_scores"] else {}
                resolved = []
                for cid_str, score in sorted(raw.items(), key=lambda x: -x[1]):
                    cand = db.execute(
                        "SELECT title FROM candidates WHERE id = ?", (int(cid_str),)
                    ).fetchone()
                    resolved.append(
                        {"title": cand["title"] if cand else cid_str, "score": score}
                    )
                db.execute(
                    """INSERT INTO election_results
                       (election_id, place, candidate_title, total_score,
                        finalist1_title, finalist1_runoff_votes,
                        finalist2_title, finalist2_runoff_votes, all_scores)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        election_id, w["round_number"], w["cand_title"], w["total_score"],
                        w["f1_title"], w["finalist1_runoff_votes"],
                        w["f2_title"], w["finalist2_runoff_votes"],
                        json.dumps(resolved),
                    ),
                )
            # Snapshot individual ballot scores
            for row in db.execute(
                """SELECT v.first_name || ' ' || v.last_name AS voter_name,
                          c.title AS candidate_title,
                          vt.score
                   FROM votes vt
                   JOIN voters    v ON vt.voter_id     = v.id
                   JOIN candidates c ON vt.candidate_id = c.id
                   JOIN ballots    b ON b.voter_id = vt.voter_id
                                   AND b.round_id = vt.round_id
                   ORDER BY v.name_lower, c.title"""
            ).fetchall():
                db.execute(
                    "INSERT INTO election_ballots (election_id, voter_name, candidate_title, score)"
                    " VALUES (?,?,?,?)",
                    (election_id, row["voter_name"], row["candidate_title"], row["score"]),
                )

        db.executescript("""
            DELETE FROM votes;
            DELETE FROM ballots;
            DELETE FROM winners;
            DELETE FROM rounds;
            INSERT INTO rounds (round_number, status) VALUES (1, 'voting');
        """)
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_get_elections(self, request: web.Request) -> web.Response:
        db = request["db"]
        elections = []
        for e in db.execute(
            "SELECT * FROM elections ORDER BY completed_at DESC"
        ).fetchall():
            results = []
            for r in db.execute(
                "SELECT * FROM election_results WHERE election_id = ? ORDER BY place",
                (e["id"],),
            ).fetchall():
                results.append(
                    {
                        "place":                  r["place"],
                        "candidate_title":        r["candidate_title"],
                        "total_score":            r["total_score"],
                        "finalist1_title":        r["finalist1_title"],
                        "finalist1_runoff_votes": r["finalist1_runoff_votes"],
                        "finalist2_title":        r["finalist2_title"],
                        "finalist2_runoff_votes": r["finalist2_runoff_votes"],
                        "all_scores":             json.loads(r["all_scores"]) if r["all_scores"] else [],
                    }
                )
            ballots: dict[str, list] = {}
            for b in db.execute(
                "SELECT voter_name, candidate_title, score FROM election_ballots"
                " WHERE election_id = ? ORDER BY voter_name, candidate_title",
                (e["id"],),
            ).fetchall():
                ballots.setdefault(b["voter_name"], []).append(
                    {"candidate_title": b["candidate_title"], "score": b["score"]}
                )
            elections.append(
                {
                    "id":           e["id"],
                    "title":        e["title"],
                    "voting_mode":  e["voting_mode"],
                    "n_winners":    e["n_winners"],
                    "completed_at": e["completed_at"],
                    "results":      results,
                    "ballots":      ballots,
                }
            )
        return web.json_response(elections)

    async def _admin_login(self, request: web.Request) -> web.Response:
        data = await request.json()
        if data.get("password") == ADMIN_PASSWORD:
            return web.json_response({"ok": True})
        return web.json_response({"error": "Wrong password"}, status=401)

    async def _admin_unsubmit(self, request: web.Request) -> web.Response:
        db   = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        voter_id = data.get("voter_id")
        if not voter_id:
            return web.json_response({"error": "voter_id required"}, status=400)
        round_row = database.current_round(db)
        if not round_row or round_row["status"] != "voting":
            return web.json_response({"error": "No active voting round"}, status=400)
        db.execute(
            "DELETE FROM ballots WHERE voter_id = ? AND round_id = ?",
            (int(voter_id), round_row["id"]),
        )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_upload_image(self, request: web.Request) -> web.Response:
        reader   = await request.multipart()
        filename = None
        async for field in reader:
            if field.name != "image":
                continue
            orig = field.filename or "image.jpg"
            ext  = Path(orig).suffix.lower()
            if ext not in ALLOWED_IMAGE_EXTS:
                return web.json_response({"error": "Unsupported image type"}, status=400)
            data  = await field.read()
            fname = f"{int(time.time() * 1000)}{ext}"
            dest  = self.images_dir / fname
            if PIL_AVAILABLE:
                img = Image.open(io.BytesIO(data))
                img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.LANCZOS)
                img.save(dest)
            else:
                dest.write_bytes(data)
            filename = fname
            break

        if not filename:
            return web.json_response({"error": "No image provided"}, status=400)
        return web.json_response({"filename": filename, "url": f"/images/{filename}"})

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        print(f"Starting N Sequential STAR Voting Server on {self.host}:{self.port}")
        web.run_app(self.app, host=self.host, port=self.port, print=None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="N Sequential STAR Voting Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--db",   default="voting.db",  help="SQLite database path")
    args = parser.parse_args()

    server = VotingServer(db_path=args.db, host=args.host, port=args.port)
    server.run()
