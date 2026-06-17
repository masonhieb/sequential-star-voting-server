#!/usr/bin/env python3
"""
Voting Tools Server
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
MASON_LAST_NAME = "hieb"
ADMIN_PASSWORD = "hunter2"
MAX_IMAGE_DIM = 500
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def render_markdown(text: str) -> str:
    return md_lib.markdown(text or "", extensions=["extra", "nl2br"], tab_length=2)


class VotingServer:
    def __init__(
        self,
        db_path: str = "voting.db",
        images_dir: str = "images",
        templates_dir: str = "templates",
        host: str = "0.0.0.0",
        port: int = 8080,
        prefix: str = "",
    ):
        self.db_path = Path(db_path)
        self.images_dir = Path(images_dir)
        self.templates_dir = Path(templates_dir)
        self.host = host
        self.port = port
        self.prefix = prefix.rstrip("/")
        # voter_name_lower -> asyncio.Queue for SSE
        self._sse_clients: dict[str, asyncio.Queue] = {}

        self.images_dir.mkdir(exist_ok=True)
        self.templates_dir.mkdir(exist_ok=True)

        self.app = web.Application(
            middlewares=[self._make_db_middleware(), self._make_base_middleware()]
        )
        self._setup_routes()
        database.init_db(self.db_path)

    # ── Middleware ────────────────────────────────────────────────────────────

    def _make_base_middleware(self):
        prefix = self.prefix

        @web.middleware
        async def base_middleware(request: web.Request, handler):
            resp = await handler(request)
            if (
                prefix
                and isinstance(resp, web.Response)
                and resp.content_type == "text/html"
                and resp.text
            ):
                # When the app is served under a sub-path (e.g. /voting/) via a
                # reverse proxy that strips the prefix before forwarding to us,
                # the browser still sees URLs rooted at /voting/. Two categories
                # of URL reference need fixing:
                #
                # 1. HTML element attributes (img src, a href, etc.)
                #    The HTML <base href="/voting/"> tag tells the browser to
                #    resolve ALL relative URLs in the document against /voting/
                #    instead of /. Our templates use relative paths for images
                #    (e.g. "images/foo.jpg") so they automatically resolve to
                #    /voting/images/foo.jpg. Without this tag they'd resolve to
                #    /images/foo.jpg and miss the proxy entirely.
                #
                # 2. JavaScript fetch() and EventSource() calls
                #    The <base> tag has NO effect on JS. Our JS uses absolute
                #    paths like fetch('/api/state') — the leading slash makes
                #    the browser send the request to the root (/api/state), not
                #    to /voting/api/state, so it hits the wrong backend entirely.
                #    The injected script monkey-patches both window.fetch and
                #    window.EventSource: any URL starting with '/' gets the
                #    prefix prepended (e.g. '/api/state' → '/voting/api/state')
                #    before the real browser function is called. URLs that are
                #    already relative or use a full origin are left untouched.
                #    EventSource.prototype is re-assigned so that instanceof
                #    checks against the original class still pass.
                #
                # The script is minified to a single line to avoid any
                # whitespace/newline issues when injected into the <head>.
                patch = (
                    f"<script>!function(){{"
                    f'var p="{prefix}";'
                    f"var f=window.fetch;"
                    f'window.fetch=function(u,o){{return f(u&&u[0]==="/"?p+u:u,o);}};'
                    f"var E=window.EventSource;"
                    f'window.EventSource=function(u,o){{return new E(u&&u[0]==="/"?p+u:u,o);}};'
                    f"window.EventSource.prototype=E.prototype;"
                    f"}}();</script>"
                )
                body = resp.text.replace(
                    "<head>", f'<head>\n  <base href="{prefix}/">\n  {patch}', 1
                )
                return web.Response(
                    text=body,
                    content_type="text/html",
                    headers={
                        k: v
                        for k, v in resp.headers.items()
                        if k.lower() not in ("content-length", "content-type")
                    },
                )
            return resp

        return base_middleware

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
            return web.json_response({"error": "Forbidden: admin only"}, status=403)
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
        n_winners = int(database.get_setting(db, "n_winners", "1"))
        voting_mode = database.get_setting(db, "voting_mode", "star")
        election_title  = database.get_setting(db, "election_title", "")
        election_state  = database.get_setting(db, "election_state", "ELECTION_ACTIVE")
        entry_context   = database.get_setting(db, "entry_context",  "")
        show_author     = database.get_setting(db, "show_author",     "1") == "1"

        # Company codenaming mode: only the derived letter (and codenames
        # already used for it) are public. The real company name
        # (settings.codename_company_name) is deliberately never read into
        # this dict — it's admin-only, via GET /api/admin/codename.
        app_mode = database.get_setting(db, "app_mode", "standard")
        codename_enforce_letter = (
            database.get_setting(db, "codename_enforce_letter", "1") == "1"
        )
        codename_required_letter = None
        codename_used_for_letter: list[str] = []
        if app_mode == "codenaming":
            codename_required_letter = database.codename_required_letter(
                database.get_setting(db, "codename_company_name", "")
            )
            if codename_required_letter:
                codename_used_for_letter = database.codenames_for_letter(
                    db, codename_required_letter
                )

        round_row = database.current_round(db)
        if not round_row:
            return {}

        round_id = round_row["id"]
        round_number = round_row["round_number"]
        round_status = round_row["status"]

        eligible = database.eligible_candidates(db)
        candidates_data = [
            {
                "id": c["id"],
                "title": c["title"],
                "body_html": render_markdown(c["body"]),
                "author": c["author"],
                "image_url": f"images/{c['image_path']}" if c["image_path"] else None,
            }
            for c in eligible
        ]

        winners_data = []
        for w in db.execute("""SELECT w.*, c.title  AS cand_title,
                      f1.title AS f1_title,
                      f2.title AS f2_title
               FROM winners w
               JOIN candidates c  ON w.candidate_id  = c.id
               LEFT JOIN candidates f1 ON w.finalist1_id = f1.id
               LEFT JOIN candidates f2 ON w.finalist2_id = f2.id
               ORDER BY w.round_number""").fetchall():
            raw = json.loads(w["all_scores"]) if w["all_scores"] else {}
            all_scores_list = []
            for cid_str, score in sorted(raw.items(), key=lambda x: -x[1]):
                cid = int(cid_str)
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
            "election_state": election_state,
            "entry_context": entry_context,
            "show_author": show_author,
            "app_mode": app_mode,
            "codename_required_letter": codename_required_letter,
            "codename_enforce_letter": codename_enforce_letter,
            "codename_used_for_letter": codename_used_for_letter,
        }

    # ── Voting algorithms ─────────────────────────────────────────────────────

    def _compute_star_winner(
        self, db: sqlite3.Connection, round_id: int
    ) -> Optional[dict]:
        eligible = database.eligible_candidates(db)
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

        sorted_ids = sorted(eligible_ids, key=lambda x: totals[x], reverse=True)
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
        eligible = database.eligible_candidates(db)
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
        return [{"candidate_id": cid, "total_score": totals[cid]} for cid in ranked[:n]]

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
        r.add_get("/", self._index)
        r.add_get("/admin", self._admin_page)
        r.add_get("/api/stream", self._stream)
        r.add_get("/api/state", self._get_state)
        r.add_get("/api/candidates", self._get_candidates)
        r.add_get("/api/voters", self._get_voters)
        r.add_post("/api/register", self._register)
        r.add_post("/api/vote", self._vote)
        r.add_get("/api/admin/sets", self._admin_get_sets)
        r.add_post("/api/admin/sets", self._admin_create_set)
        r.add_post("/api/admin/sets/import", self._admin_import_sets)
        r.add_delete("/api/admin/sets/{id}", self._admin_delete_set)
        r.add_post("/api/admin/sets/{id}/items", self._admin_add_set_item)
        r.add_delete(
            "/api/admin/sets/{id}/items/{item_id}", self._admin_delete_set_item
        )
        r.add_put(
            "/api/admin/sets/{id}/items/{item_id}", self._admin_update_set_item
        )
        r.add_post("/api/admin/sets/{id}/save-current", self._admin_save_current_to_set)
        r.add_post("/api/admin/sets/{id}/load", self._admin_load_set)
        r.add_post("/api/admin/sets/{id}/reorder", self._admin_reorder_set)
        r.add_post("/api/admin/login", self._admin_login)
        r.add_post("/api/admin/candidates", self._admin_add_candidate)
        r.add_delete("/api/admin/candidates/{id}", self._admin_delete_candidate)
        r.add_post("/api/admin/generate-test", self._admin_generate_test)
        r.add_post("/api/admin/clear-candidates", self._admin_clear_candidates)
        r.add_post("/api/admin/settings", self._admin_update_settings)
        r.add_post("/api/admin/reveal", self._admin_reveal_winner)
        r.add_post("/api/admin/reset", self._admin_reset)
        r.add_get("/api/admin/elections", self._admin_get_elections)
        r.add_delete("/api/admin/elections/{id}", self._admin_delete_election)
        r.add_post("/api/admin/unsubmit", self._admin_unsubmit)
        r.add_post("/api/entry", self._submit_entry)
        r.add_post("/api/admin/upload-image", self._admin_upload_image)
        r.add_post("/api/admin/import-voters", self._admin_import_voters)
        r.add_get("/api/my-scores", self._get_my_scores)
        r.add_post("/api/codename/submit", self._submit_codename)
        r.add_delete("/api/codename/candidate/{id}", self._delete_own_codename)
        r.add_get("/api/admin/codename", self._admin_codename_get)
        r.add_post("/api/admin/codename/configure", self._admin_codename_configure)
        r.add_post("/api/admin/codename/history", self._admin_codename_add_history)
        r.add_delete(
            "/api/admin/codename/history/{id}",
            self._admin_codename_delete_history,
        )
        r.add_get("/api/admin/codename/pool", self._admin_codename_pool_get)
        r.add_post("/api/admin/codename/pool", self._admin_codename_pool_add)
        r.add_put("/api/admin/codename/pool/{id}", self._admin_codename_pool_update)
        r.add_delete("/api/admin/codename/pool/{id}", self._admin_codename_pool_delete)
        r.add_post(
            "/api/admin/codename/open-submissions",
            self._admin_codename_open_submissions,
        )
        r.add_post(
            "/api/admin/codename/start-voting", self._admin_codename_start_voting
        )
        r.add_post(
            "/api/admin/codename/finish-round", self._admin_codename_finish_round
        )
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
                    "image_url": (
                        f"images/{c['image_path']}" if c["image_path"] else None
                    ),
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
        db = request["db"]
        data = await request.json()
        first_name = data.get("first_name", "").strip()
        last_name = data.get("last_name", "").strip()

        if not first_name:
            return web.json_response({"error": "First name required"}, status=400)
        if not last_name:
            return web.json_response({"error": "Last name required"}, status=400)

        name_lower = f"{first_name} {last_name}".lower()
        existing = db.execute(
            "SELECT * FROM voters WHERE name_lower = ?", (name_lower,)
        ).fetchone()

        if existing:
            if existing["name_lower"] in self._sse_clients:
                return web.json_response(
                    {"error": "That user is already signed in."}, status=409
                )
            return web.json_response(
                {
                    "id": existing["id"],
                    "first_name": existing["first_name"],
                    "last_name": existing["last_name"],
                    "name": f"{existing['first_name']} {existing['last_name']}",
                    "is_mason": self._is_mason(
                        existing["first_name"], existing["last_name"]
                    ),
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
                "id": voter["id"],
                "first_name": voter["first_name"],
                "last_name": voter["last_name"],
                "name": f"{voter['first_name']} {voter['last_name']}",
                "is_mason": self._is_mason(voter["first_name"], voter["last_name"]),
                "already_exists": False,
            }
        )

    async def _vote(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        voter_name = data.get("voter_name", "").strip()
        scores = data.get("scores", {})

        if not voter_name:
            return web.json_response({"error": "voter_name required"}, status=400)

        voter = db.execute(
            "SELECT * FROM voters WHERE name_lower = ?", (voter_name.lower(),)
        ).fetchone()
        if not voter:
            return web.json_response({"error": "Voter not found"}, status=404)

        if database.get_setting(db, "election_state", "ELECTION_ACTIVE") != "ELECTION_ACTIVE":
            return web.json_response({"error": "Voting is not open"}, status=403)

        round_row = database.current_round(db)
        if not round_row or round_row["status"] != "voting":
            return web.json_response({"error": "No active voting round"}, status=400)

        round_id = round_row["id"]
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
        db = request["db"]
        data = await request.json()
        title = data.get("title", "").strip()
        if not title:
            return web.json_response({"error": "Title required"}, status=400)
        body = (data.get("body") or "").strip()
        author = (data.get("author") or "").strip() or None
        image_path = (data.get("image_path") or "").strip() or None

        db.execute(
            "INSERT INTO candidates (title, body, author, image_path) VALUES (?,?,?,?)",
            (title, body, author, image_path),
        )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_delete_candidate(self, request: web.Request) -> web.Response:
        db = request["db"]
        cid = int(request.match_info["id"])
        if db.execute("SELECT 1 FROM votes WHERE candidate_id = ?", (cid,)).fetchone():
            return web.json_response(
                {"error": "Cannot delete a candidate that has received votes"},
                status=400,
            )
        if db.execute("SELECT 1 FROM winners WHERE candidate_id = ?", (cid,)).fetchone():
            return web.json_response(
                {"error": "Cannot delete a candidate that has been declared a winner — reset the election first"},
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
                {
                    "error": "Reset the election first to clear all votes, then you can clear candidates"
                },
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
                {
                    "error": "Candidates already exist — clear them before generating test data"
                },
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
        db = request["db"]
        data = await request.json()
        err = self._require_mason(data)
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
        if "election_state" in data:
            valid = {
                "ELECTION_ACTIVE",
                "ELECTION_INACTIVE",
                "CANDIDATE_ENTRY_NAMES",
                "CODENAME_SUBMISSION",
            }
            if data["election_state"] not in valid:
                return web.json_response({"error": "Invalid election_state"}, status=400)
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('election_state', ?)",
                (data["election_state"],),
            )
        if "entry_context" in data:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('entry_context', ?)",
                (str(data["entry_context"]).strip(),),
            )
        if "show_author" in data:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('show_author', ?)",
                ("1" if data["show_author"] else "0",),
            )
        if "app_mode" in data:
            mode = data["app_mode"]
            if mode not in ("standard", "codenaming"):
                return web.json_response(
                    {"error": "app_mode must be 'standard' or 'codenaming'"},
                    status=400,
                )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('app_mode', ?)", (mode,)
            )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    def _archive_codename(self, db: sqlite3.Connection, candidate_id: int) -> None:
        """If we're in company-codenaming mode, permanently record the
        winning codename (lowercased) against just the company's first
        letter — never the company name itself. No-ops outside codenaming
        mode, so it's safe to call unconditionally once a winner is known."""
        if database.get_setting(db, "app_mode", "standard") != "codenaming":
            return
        letter = database.codename_required_letter(
            database.get_setting(db, "codename_company_name", "")
        )
        if not letter:
            return
        cand = db.execute(
            "SELECT title FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if not cand:
            return
        db.execute(
            "INSERT OR IGNORE INTO selected_codenames (codename, company_first_letter)"
            " VALUES (?, ?)",
            (cand["title"].strip().lower(), letter),
        )

    async def _admin_reveal_winner(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        err = self._require_mason(data)
        if err:
            return err

        if database.get_setting(db, "election_state", "") != "ELECTION_INACTIVE":
            return web.json_response(
                {"error": "Close voting before revealing the winner."}, status=400
            )

        round_row = database.current_round(db)
        if not round_row or round_row["status"] != "voting":
            return web.json_response({"error": "No active voting round"}, status=400)

        n_winners = int(database.get_setting(db, "n_winners", "1"))
        voting_mode = database.get_setting(db, "voting_mode", "star")
        existing_count = db.execute("SELECT COUNT(*) FROM winners").fetchone()[0]
        if existing_count >= n_winners:
            return web.json_response(
                {"error": "All winners already elected"}, status=400
            )

        round_number = round_row["round_number"]
        all_scores_json: str

        if voting_mode == "score":
            ranked = self._compute_score_winners(db, round_row["id"], n_winners)
            if not ranked:
                return web.json_response(
                    {"error": "No eligible candidates"}, status=400
                )
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
            if ranked:
                self._archive_codename(db, ranked[0]["candidate_id"])
        else:
            result = self._compute_star_winner(db, round_row["id"])
            if not result:
                return web.json_response(
                    {"error": "No eligible candidates"}, status=400
                )
            winner_id = result["winner_id"]
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
            self._archive_codename(db, winner_id)
            new_count = existing_count + 1
            if new_count >= n_winners:
                db.execute(
                    "UPDATE rounds SET status = 'complete' WHERE id = ?",
                    (round_row["id"],),
                )
            else:
                db.execute(
                    "UPDATE rounds SET status = 'revealed' WHERE id = ?",
                    (round_row["id"],),
                )
                db.execute(
                    "INSERT INTO rounds (round_number, status) VALUES (?, 'voting')",
                    (round_number + 1,),
                )

        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    def _archive_current_election(
        self, db: sqlite3.Connection, title_override: Optional[str] = None
    ) -> bool:
        """Snapshot the current winners + per-voter ballots into
        elections/election_results/election_ballots, the same permanent
        history the admin's Election History card reads from. Returns False
        (no-op) if there's nothing to archive. Callers are responsible for
        wiping the live votes/ballots/winners/rounds afterward — this only
        archives, it doesn't clean up.

        `title_override` lets a caller (e.g. a finished codenaming round)
        supply a synthetic, non-identifying title instead of falling back
        to the `election_title` setting."""
        if not db.execute("SELECT 1 FROM winners LIMIT 1").fetchone():
            return False

        title = title_override or (
            database.get_setting(db, "election_title", "") or "Untitled Election"
        )
        voting_mode = database.get_setting(db, "voting_mode", "star")
        n_winners = int(database.get_setting(db, "n_winners", "1"))
        db.execute(
            "INSERT INTO elections (title, voting_mode, n_winners) VALUES (?,?,?)",
            (title, voting_mode, n_winners),
        )
        election_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for w in db.execute("""SELECT w.round_number, w.total_score,
                      w.finalist1_runoff_votes, w.finalist2_runoff_votes,
                      w.all_scores,
                      c.title  AS cand_title,
                      f1.title AS f1_title,
                      f2.title AS f2_title
               FROM winners w
               JOIN candidates c  ON w.candidate_id  = c.id
               LEFT JOIN candidates f1 ON w.finalist1_id = f1.id
               LEFT JOIN candidates f2 ON w.finalist2_id = f2.id
               ORDER BY w.round_number""").fetchall():
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
                    election_id,
                    w["round_number"],
                    w["cand_title"],
                    w["total_score"],
                    w["f1_title"],
                    w["finalist1_runoff_votes"],
                    w["f2_title"],
                    w["finalist2_runoff_votes"],
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
                (
                    election_id,
                    row["voter_name"],
                    row["candidate_title"],
                    row["score"],
                ),
            )
        return True

    async def _admin_reset(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        err = self._require_mason(data)
        if err:
            return err

        self._archive_current_election(db)

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
                        "place": r["place"],
                        "candidate_title": r["candidate_title"],
                        "total_score": r["total_score"],
                        "finalist1_title": r["finalist1_title"],
                        "finalist1_runoff_votes": r["finalist1_runoff_votes"],
                        "finalist2_title": r["finalist2_title"],
                        "finalist2_runoff_votes": r["finalist2_runoff_votes"],
                        "all_scores": (
                            json.loads(r["all_scores"]) if r["all_scores"] else []
                        ),
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
                    "id": e["id"],
                    "title": e["title"],
                    "voting_mode": e["voting_mode"],
                    "n_winners": e["n_winners"],
                    "completed_at": e["completed_at"],
                    "results": results,
                    "ballots": ballots,
                }
            )
        return web.json_response(elections)

    async def _admin_delete_election(self, request: web.Request) -> web.Response:
        db = request["db"]
        election_id = int(request.match_info["id"])
        db.execute("DELETE FROM elections WHERE id = ?", (election_id,))
        db.commit()
        return web.json_response({"ok": True})

    # ── Candidate sets ────────────────────────────────────────────────────────

    def _set_items(self, db: sqlite3.Connection, set_id: int) -> list[dict]:
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "body": row["body"],
                "author": row["author"],
                "image_url": (
                    f"images/{row['image_path']}" if row["image_path"] else None
                ),
                "image_path": row["image_path"],
            }
            for row in db.execute(
                "SELECT * FROM candidate_set_items WHERE set_id = ? ORDER BY sort_order, id",
                (set_id,),
            ).fetchall()
        ]

    async def _admin_get_sets(self, request: web.Request) -> web.Response:
        db = request["db"]
        sets = []
        for s in db.execute("SELECT * FROM candidate_sets ORDER BY name").fetchall():
            sets.append(
                {
                    "id": s["id"],
                    "name": s["name"],
                    "items": self._set_items(db, s["id"]),
                }
            )
        return web.json_response(sets)

    async def _admin_create_set(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "Name required"}, status=400)
        try:
            db.execute("INSERT INTO candidate_sets (name) VALUES (?)", (name,))
            db.commit()
        except Exception:
            return web.json_response(
                {"error": "A set with that name already exists"}, status=400
            )
        row = db.execute(
            "SELECT * FROM candidate_sets WHERE name = ?", (name,)
        ).fetchone()
        return web.json_response({"id": row["id"], "name": row["name"], "items": []})

    async def _admin_delete_set(self, request: web.Request) -> web.Response:
        db = request["db"]
        set_id = int(request.match_info["id"])
        db.execute("DELETE FROM candidate_sets WHERE id = ?", (set_id,))
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_add_set_item(self, request: web.Request) -> web.Response:
        db = request["db"]
        set_id = int(request.match_info["id"])
        if not db.execute(
            "SELECT 1 FROM candidate_sets WHERE id = ?", (set_id,)
        ).fetchone():
            return web.json_response({"error": "Set not found"}, status=404)
        data = await request.json()
        title = (data.get("title") or "").strip()
        if not title:
            return web.json_response({"error": "Title required"}, status=400)
        body = (data.get("body") or "").strip()
        author = (data.get("author") or "").strip() or None
        image_path = (data.get("image_path") or "").strip() or None
        next_order = db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM candidate_set_items WHERE set_id = ?",
            (set_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO candidate_set_items (set_id, title, body, author, image_path, sort_order)"
            " VALUES (?,?,?,?,?,?)",
            (set_id, title, body, author, image_path, next_order),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM candidate_set_items WHERE set_id = ? ORDER BY id DESC LIMIT 1",
            (set_id,),
        ).fetchone()
        return web.json_response(
            {
                "id": row["id"],
                "title": row["title"],
                "body": row["body"],
                "author": row["author"],
                "image_url": (
                    f"images/{row['image_path']}" if row["image_path"] else None
                ),
                "image_path": row["image_path"],
            }
        )

    async def _admin_delete_set_item(self, request: web.Request) -> web.Response:
        db = request["db"]
        item_id = int(request.match_info["item_id"])
        db.execute("DELETE FROM candidate_set_items WHERE id = ?", (item_id,))
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_update_set_item(self, request: web.Request) -> web.Response:
        db = request["db"]
        item_id = int(request.match_info["item_id"])
        set_id = int(request.match_info["id"])
        if not db.execute(
            "SELECT 1 FROM candidate_set_items WHERE id = ? AND set_id = ?",
            (item_id, set_id),
        ).fetchone():
            return web.json_response({"error": "Item not found"}, status=404)
        data = await request.json()
        title = (data.get("title") or "").strip()
        if not title:
            return web.json_response({"error": "Title required"}, status=400)
        db.execute(
            "UPDATE candidate_set_items SET title=?, body=?, author=? WHERE id=?",
            (
                title,
                (data.get("body") or "").strip(),
                (data.get("author") or "").strip() or None,
                item_id,
            ),
        )
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_save_current_to_set(self, request: web.Request) -> web.Response:
        db = request["db"]
        set_id = int(request.match_info["id"])
        if not db.execute(
            "SELECT 1 FROM candidate_sets WHERE id = ?", (set_id,)
        ).fetchone():
            return web.json_response({"error": "Set not found"}, status=404)
        db.execute("DELETE FROM candidate_set_items WHERE set_id = ?", (set_id,))
        for i, c in enumerate(db.execute("SELECT * FROM candidates ORDER BY id").fetchall()):
            db.execute(
                "INSERT INTO candidate_set_items (set_id, title, body, author, image_path, sort_order)"
                " VALUES (?,?,?,?,?,?)",
                (set_id, c["title"], c["body"], c["author"], c["image_path"], i),
            )
        db.commit()
        return web.json_response(
            {
                "ok": True,
                "count": db.execute(
                    "SELECT COUNT(*) FROM candidate_set_items WHERE set_id = ?",
                    (set_id,),
                ).fetchone()[0],
            }
        )

    async def _admin_load_set(self, request: web.Request) -> web.Response:
        db = request["db"]
        set_id = int(request.match_info["id"])
        if not db.execute(
            "SELECT 1 FROM candidate_sets WHERE id = ?", (set_id,)
        ).fetchone():
            return web.json_response({"error": "Set not found"}, status=404)
        if db.execute("SELECT 1 FROM votes LIMIT 1").fetchone():
            return web.json_response(
                {
                    "error": "Reset the election first before loading a new candidate set"
                },
                status=400,
            )
        db.execute("DELETE FROM candidates")
        for item in db.execute(
            "SELECT * FROM candidate_set_items WHERE set_id = ? ORDER BY sort_order, id",
            (set_id,),
        ).fetchall():
            db.execute(
                "INSERT INTO candidates (title, body, author, image_path) VALUES (?,?,?,?)",
                (item["title"], item["body"], item["author"], item["image_path"]),
            )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_import_sets(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        sets = data.get("sets", [])
        overwrite = bool(data.get("overwrite", False))
        imported = skipped = replaced = 0
        for s in sets:
            name = (s.get("name") or "").strip()
            items = s.get("items") or []
            if not name:
                skipped += 1
                continue
            existing = db.execute(
                "SELECT id FROM candidate_sets WHERE name = ?", (name,)
            ).fetchone()
            if existing and not overwrite:
                skipped += 1
                continue
            if existing:
                db.execute(
                    "DELETE FROM candidate_set_items WHERE set_id = ?", (existing["id"],)
                )
                set_id = existing["id"]
                replaced += 1
            else:
                db.execute("INSERT INTO candidate_sets (name) VALUES (?)", (name,))
                set_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                imported += 1
            for i, item in enumerate(items):
                db.execute(
                    "INSERT INTO candidate_set_items"
                    " (set_id, title, body, author, image_path, sort_order)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        set_id,
                        (item.get("title") or "").strip(),
                        (item.get("body") or "").strip(),
                        item.get("author") or None,
                        item.get("image_path") or None,
                        i,
                    ),
                )
        db.commit()
        return web.json_response(
            {"ok": True, "imported": imported, "replaced": replaced, "skipped": skipped}
        )

    async def _admin_reorder_set(self, request: web.Request) -> web.Response:
        db = request["db"]
        set_id = int(request.match_info["id"])
        data = await request.json()
        item_ids = data.get("item_ids", [])
        for sort_order, item_id in enumerate(item_ids):
            db.execute(
                "UPDATE candidate_set_items SET sort_order = ? WHERE id = ? AND set_id = ?",
                (sort_order, item_id, set_id),
            )
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_login(self, request: web.Request) -> web.Response:
        data = await request.json()
        if data.get("password") == ADMIN_PASSWORD:
            return web.json_response({"ok": True})
        return web.json_response({"error": "Wrong password"}, status=401)

    async def _admin_unsubmit(self, request: web.Request) -> web.Response:
        db = request["db"]
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

    # ── Company codenaming admin endpoints ──────────────────────────────────
    # These read/write the private company name or mutate the codenaming
    # round, so — like _admin_unsubmit above — they're gated by the admin
    # password directly rather than the _require_mason voter-identity trick
    # used by reveal/reset/settings.

    async def _admin_codename_get(self, request: web.Request) -> web.Response:
        db = request["db"]
        if request.query.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        company_name = database.get_setting(db, "codename_company_name", "")
        history = [
            {
                "id": r["id"],
                "codename": r["codename"],
                "company_first_letter": r["company_first_letter"],
            }
            for r in db.execute(
                "SELECT id, codename, company_first_letter FROM selected_codenames"
                " ORDER BY id"
            ).fetchall()
        ]
        return web.json_response(
            {
                "company_name": company_name,
                "enforce_letter_check": database.get_setting(
                    db, "codename_enforce_letter", "1"
                )
                == "1",
                "required_letter": database.codename_required_letter(company_name),
                "history": history,
            }
        )

    async def _admin_codename_add_history(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        codename = (data.get("codename") or "").strip().lower()
        letter = (data.get("company_first_letter") or "").strip().upper()
        if not codename:
            return web.json_response({"error": "codename required"}, status=400)
        if not letter or len(letter) != 1 or not letter.isalpha():
            return web.json_response({"error": "company_first_letter required (single A-Z)"}, status=400)
        try:
            db.execute(
                "INSERT INTO selected_codenames (codename, company_first_letter) VALUES (?, ?)",
                (codename, letter),
            )
            db.commit()
        except Exception:
            return web.json_response({"error": "That codename is already in history"}, status=400)
        return web.json_response({"ok": True})

    async def _admin_codename_delete_history(self, request: web.Request) -> web.Response:
        db = request["db"]
        if request.query.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        row_id = int(request.match_info["id"])
        db.execute("DELETE FROM selected_codenames WHERE id = ?", (row_id,))
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_codename_configure(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if "company_name" in data:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('codename_company_name', ?)",
                (str(data["company_name"]).strip(),),
            )
        if "enforce_letter_check" in data:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('codename_enforce_letter', ?)",
                ("1" if data["enforce_letter_check"] else "0",),
            )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_codename_open_submissions(
        self, request: web.Request
    ) -> web.Response:
        db = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if database.get_setting(db, "app_mode", "standard") != "codenaming":
            return web.json_response(
                {"error": "Enable Company Codenaming mode first"}, status=400
            )
        if not database.get_setting(db, "codename_company_name", "").strip():
            return web.json_response(
                {"error": "Set a company name first"}, status=400
            )
        if db.execute("SELECT 1 FROM candidates LIMIT 1").fetchone():
            return web.json_response(
                {
                    "error": "Candidates already exist — finish the current round first"
                },
                status=400,
            )
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('election_state', 'CODENAME_SUBMISSION')"
        )
        letter = database.codename_required_letter(
            database.get_setting(db, "codename_company_name", "")
        )
        if letter:
            pool_rows = db.execute(
                "SELECT name FROM codename_candidates WHERE letter = ?"
                " AND name NOT IN (SELECT codename FROM selected_codenames)",
                (letter,),
            ).fetchall()
            for row in pool_rows:
                title = row["name"].capitalize()
                db.execute(
                    "INSERT OR IGNORE INTO candidates (title, body, author, image_path)"
                    " VALUES (?, '', NULL, NULL)",
                    (title,),
                )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_codename_start_voting(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if database.get_setting(db, "election_state", "") != "CODENAME_SUBMISSION":
            return web.json_response(
                {"error": "Not currently in the codename submission phase"},
                status=400,
            )
        if not db.execute("SELECT 1 FROM candidates LIMIT 1").fetchone():
            return web.json_response(
                {
                    "error": "Need at least one codename submission before voting can start"
                },
                status=400,
            )
        db.execute("INSERT OR REPLACE INTO settings VALUES ('n_winners', '1')")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('voting_mode', 'star')")
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('election_state', 'ELECTION_ACTIVE')"
        )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _admin_codename_finish_round(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if database.get_setting(db, "election_state", "") != "ELECTION_INACTIVE":
            return web.json_response(
                {"error": "Close voting before finishing the round."}, status=400
            )
        if not db.execute("SELECT 1 FROM winners LIMIT 1").fetchone():
            return web.json_response(
                {"error": "Reveal the winner before finishing the round"},
                status=400,
            )

        letter = database.codename_required_letter(
            database.get_setting(db, "codename_company_name", "")
        )
        title = f"Codenaming round (letter {letter})" if letter else "Codenaming round"
        self._archive_current_election(db, title_override=title)

        # Codename candidates are single-use per company, unlike standard
        # elections where reset deliberately keeps candidates around — so
        # this also wipes `candidates`, which _admin_reset never does.
        db.executescript("""
            DELETE FROM votes;
            DELETE FROM ballots;
            DELETE FROM winners;
            DELETE FROM rounds;
            DELETE FROM candidates;
            INSERT INTO rounds (round_number, status) VALUES (1, 'voting');
        """)
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('codename_company_name', '')"
        )
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('codename_enforce_letter', '1')"
        )
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('election_state', 'ELECTION_INACTIVE')"
        )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _submit_entry(self, request: web.Request) -> web.Response:
        db = request["db"]
        election_state = database.get_setting(db, "election_state", "ELECTION_ACTIVE")
        if not election_state.startswith("CANDIDATE_ENTRY_"):
            return web.json_response({"error": "Not in candidate entry mode"}, status=400)
        data = await request.json()
        value = (data.get("value") or "").strip()
        voter_name = (data.get("voter_name") or "").strip() or None
        if not value:
            return web.json_response({"error": "Value required"}, status=400)
        db.execute(
            "INSERT INTO candidates (title, body, author, image_path) VALUES (?,?,?,?)",
            (value, "", voter_name, None),
        )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _submit_codename(self, request: web.Request) -> web.Response:
        db = request["db"]
        if database.get_setting(db, "app_mode", "standard") != "codenaming":
            return web.json_response({"error": "Not in codenaming mode"}, status=400)
        if database.get_setting(db, "election_state", "") != "CODENAME_SUBMISSION":
            return web.json_response(
                {"error": "Codename submissions are not open"}, status=400
            )
        data = await request.json()
        value = (data.get("value") or "").strip()
        voter_name = (data.get("voter_name") or "").strip() or None
        if not value:
            return web.json_response({"error": "Value required"}, status=400)

        company_name = database.get_setting(db, "codename_company_name", "")
        required_letter = database.codename_required_letter(company_name)
        enforce_letter = (
            database.get_setting(db, "codename_enforce_letter", "1") == "1"
        )
        if enforce_letter and required_letter and value[0].upper() != required_letter:
            return web.json_response(
                {
                    "error": f'Codenames this round must start with "{required_letter}".'
                },
                status=400,
            )

        if db.execute(
            "SELECT 1 FROM candidates WHERE LOWER(title) = LOWER(?)", (value,)
        ).fetchone():
            return web.json_response(
                {"error": "That name has already been suggested this round."},
                status=400,
            )
        if database.is_codename_used(db, value):
            return web.json_response(
                {
                    "error": "That codename has already been used for a previous company."
                },
                status=400,
            )

        db.execute(
            "INSERT INTO candidates (title, body, author, image_path) VALUES (?,?,?,?)",
            (value, "", voter_name, None),
        )
        if required_letter:
            db.execute(
                "INSERT OR IGNORE INTO codename_candidates (letter, name) VALUES (?, ?)",
                (required_letter, value.strip().lower()),
            )
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    async def _delete_own_codename(self, request: web.Request) -> web.Response:
        db = request["db"]
        if database.get_setting(db, "election_state", "") != "CODENAME_SUBMISSION":
            return web.json_response({"error": "Submissions are not open"}, status=400)
        data = await request.json()
        voter_name = (data.get("voter_name") or "").strip()
        if not voter_name:
            return web.json_response({"error": "voter_name required"}, status=400)
        cid = int(request.match_info["id"])
        row = db.execute("SELECT author FROM candidates WHERE id = ?", (cid,)).fetchone()
        if not row:
            return web.json_response({"error": "Not found"}, status=404)
        if (row["author"] or "").strip().lower() != voter_name.lower():
            return web.json_response({"error": "You can only delete your own submissions"}, status=403)
        db.execute("DELETE FROM candidates WHERE id = ?", (cid,))
        db.commit()
        await self._broadcast("state_update", self._build_state(db))
        return web.json_response({"ok": True})

    # ── Codename candidate pool endpoints ──────────────────────────────────────

    async def _admin_codename_pool_get(self, request: web.Request) -> web.Response:
        db = request["db"]
        if request.query.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        letter = (request.query.get("letter") or "").strip().upper()
        if not letter or len(letter) != 1 or not letter.isalpha():
            return web.json_response({"error": "letter param required (single A-Z)"}, status=400)
        return web.json_response({"candidates": database.codename_pool_for_letter(db, letter)})

    async def _admin_codename_pool_add(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        letter = (data.get("letter") or "").strip().upper()
        name = (data.get("name") or "").strip().lower()
        if not letter or len(letter) != 1 or not letter.isalpha():
            return web.json_response({"error": "letter required (single A-Z)"}, status=400)
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        db.execute(
            "INSERT OR IGNORE INTO codename_candidates (letter, name) VALUES (?, ?)",
            (letter, name),
        )
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_codename_pool_update(self, request: web.Request) -> web.Response:
        db = request["db"]
        data = await request.json()
        if data.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        row_id = int(request.match_info["id"])
        new_name = (data.get("name") or "").strip().lower()
        if not new_name:
            return web.json_response({"error": "name required"}, status=400)
        row = db.execute("SELECT letter FROM codename_candidates WHERE id = ?", (row_id,)).fetchone()
        if not row:
            return web.json_response({"error": "Not found"}, status=404)
        if new_name[0].upper() != row["letter"]:
            return web.json_response(
                {"error": f'Name must start with "{row["letter"]}"'}, status=400
            )
        db.execute("UPDATE codename_candidates SET name = ? WHERE id = ?", (new_name, row_id))
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_codename_pool_delete(self, request: web.Request) -> web.Response:
        db = request["db"]
        if request.query.get("admin_password") != ADMIN_PASSWORD:
            return web.json_response({"error": "Unauthorized"}, status=401)
        row_id = int(request.match_info["id"])
        db.execute("DELETE FROM codename_candidates WHERE id = ?", (row_id,))
        db.commit()
        return web.json_response({"ok": True})

    async def _admin_import_voters(self, request: web.Request) -> web.Response:
        db = request["db"]
        reader = await request.multipart()
        lines = None
        async for field in reader:
            if field.name == "voters_file":
                data = await field.read()
                lines = data.decode("utf-8", errors="replace").splitlines()
                break
        if lines is None:
            return web.json_response({"error": "No file provided"}, status=400)
        imported = skipped = invalid = 0
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                invalid += 1
                continue
            first_name = parts[0].strip()
            last_name = parts[1].strip()
            if not first_name or not last_name:
                invalid += 1
                continue
            name_lower = f"{first_name} {last_name}".lower()
            if db.execute("SELECT 1 FROM voters WHERE name_lower = ?", (name_lower,)).fetchone():
                skipped += 1
                continue
            db.execute(
                "INSERT INTO voters (first_name, last_name, name_lower) VALUES (?,?,?)",
                (first_name, last_name, name_lower),
            )
            imported += 1
        db.commit()
        await self._broadcast("voters_update", self._build_voters_data(db))
        return web.json_response({"ok": True, "imported": imported, "skipped": skipped, "invalid": invalid})

    async def _admin_upload_image(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        filename = None
        async for field in reader:
            if field.name != "image":
                continue
            orig = field.filename or "image.jpg"
            ext = Path(orig).suffix.lower()
            if ext not in ALLOWED_IMAGE_EXTS:
                return web.json_response(
                    {"error": "Unsupported image type"}, status=400
                )
            data = await field.read()
            fname = f"{int(time.time() * 1000)}{ext}"
            dest = self.images_dir / fname
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
        return web.json_response({"filename": filename, "url": f"images/{filename}"})

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        print(f"Starting N Sequential STAR Voting Server on {self.host}:{self.port}")
        web.run_app(self.app, host=self.host, port=self.port, print=None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="N Sequential STAR Voting Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument("--db", default="voting.db", help="SQLite database path")
    parser.add_argument("--prefix", default="", help="URL path prefix, e.g. /voting")
    args = parser.parse_args()

    server = VotingServer(
        db_path=args.db, host=args.host, port=args.port, prefix=args.prefix
    )
    server.run()
