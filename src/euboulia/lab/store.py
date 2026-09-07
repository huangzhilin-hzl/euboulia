"""Durable agent identities and inboxes with scoped credentials and execution leases."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LEASE_SECONDS = 45
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def text_field(data: dict[str, Any], key: str, limit: int, *, optional: bool = False) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError(f"Invalid {key} (maximum {limit} characters)")
    return value.strip()


class LabStore:
    def __init__(self, directory: Path) -> None:
        private_directory(directory)
        self.path = directory / "lab.sqlite3"
        self.key_path = directory / "admin.key"
        if not self.key_path.exists():
            with self.key_path.open("x", encoding="utf-8") as handle:
                self.key_path.chmod(0o600)
                handle.write(secrets.token_urlsafe(32))
        self.key_path.chmod(0o600)
        self.admin_key = self.key_path.read_text().strip()
        if len(self.admin_key) < 24:
            raise ValueError("Lab admin key is invalid; restore its private key file")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
                    capabilities TEXT NOT NULL, token_hash TEXT, pairing_hash TEXT,
                    pairing_until REAL, revoked INTEGER NOT NULL DEFAULT 0,
                    heartbeat REAL NOT NULL DEFAULT 0, host TEXT NOT NULL DEFAULT '',
                    workspace TEXT NOT NULL DEFAULT '', runtime TEXT NOT NULL DEFAULT '',
                    instance TEXT NOT NULL DEFAULT '', created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, prompt TEXT NOT NULL, context TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
                    lease TEXT, lease_until REAL, created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                    created REAL NOT NULL, UNIQUE(job_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                    subject TEXT NOT NULL, created REAL NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def audit(db: sqlite3.Connection, action: str, subject: str) -> None:
        db.execute(
            "INSERT INTO audit(action,subject,created) VALUES(?,?,?)",
            (action, subject, time.time()),
        )

    @staticmethod
    def expire(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE jobs SET state='interrupted', result=?, updated=? "
            "WHERE state IN ('running','cancelling') AND lease_until < ?",
            (
                "Connection lease expired; execution outcome is uncertain. Review before retrying.",
                time.time(),
                time.time(),
            ),
        )

    def create_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        name = text_field(data, "name", 80)
        role = text_field(data, "role", 1000)
        capabilities = text_field(data, "capabilities", 1000)
        agent_id, code = secrets.token_hex(12), secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute(
                "INSERT INTO agents(id,name,role,capabilities,pairing_hash,pairing_until,created) "
                "VALUES(?,?,?,?,?,?,?)",
                (agent_id, name, role, capabilities, digest(code), time.time() + 600, time.time()),
            )
            self.audit(db, "agent.created", agent_id)
        return {"agent_id": agent_id, "pairing_code": code, "expires_in": 600}

    def repair(self, agent_id: str) -> dict[str, Any]:
        code = secrets.token_urlsafe(24)
        with self.db() as db:
            if not db.execute("SELECT id FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise ValueError("Unknown agent")
            self._revoke(db, agent_id)
            db.execute(
                "UPDATE agents SET pairing_hash=?,pairing_until=?,revoked=0 WHERE id=?",
                (digest(code), time.time() + 600, agent_id),
            )
            self.audit(db, "agent.pairing-renewed", agent_id)
        return {"agent_id": agent_id, "pairing_code": code, "expires_in": 600}

    def pair(self, data: dict[str, Any]) -> dict[str, Any]:
        code = text_field(data, "code", 128)
        host = text_field(data, "host", 200)
        workspace = text_field(data, "workspace", 1000)
        runtime = text_field(data, "runtime", 40)
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM agents WHERE pairing_hash=? AND pairing_until>? AND revoked=0",
                (digest(code), time.time()),
            ).fetchone()
            if row is None:
                raise PermissionError("Invalid or expired pairing code")
            db.execute(
                "UPDATE agents SET token_hash=?,pairing_hash=NULL,pairing_until=NULL,"
                "host=?,workspace=?,runtime=? WHERE id=?",
                (digest(token), host, workspace, runtime, row["id"]),
            )
            self.audit(db, "agent.paired", row["id"])
        return {
            "agent_id": row["id"],
            "token": token,
            "name": row["name"],
            "role": row["role"],
            "capabilities": row["capabilities"],
        }

    def authenticate(self, token: str) -> str:
        with self.db() as db:
            row = db.execute(
                "SELECT id FROM agents WHERE token_hash=? AND revoked=0", (digest(token),)
            ).fetchone()
        if row is None:
            raise PermissionError("Agent credential is invalid or revoked")
        return str(row["id"])

    @staticmethod
    def _revoke(db: sqlite3.Connection, agent_id: str) -> None:
        db.execute(
            "UPDATE agents SET token_hash=NULL,pairing_hash=NULL,pairing_until=NULL,"
            "revoked=1,heartbeat=0,instance='' WHERE id=?",
            (agent_id,),
        )
        db.execute(
            "UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'interrupted' "
            "END, result='Agent connection revoked', updated=? "
            "WHERE agent_id=? AND state IN ('queued','running','cancelling')",
            (time.time(), agent_id),
        )

    def revoke(self, agent_id: str) -> None:
        with self.db() as db:
            self._revoke(db, agent_id)
            self.audit(db, "agent.revoked", agent_id)

    def submit(self, data: dict[str, Any]) -> dict[str, Any]:
        agent_id = text_field(data, "agent_id", 80)
        request_id = text_field(data, "request_id", 100)
        prompt = text_field(data, "prompt", 16000)
        kind = text_field(data, "kind", 20)
        if kind not in {"message", "task"}:
            raise ValueError("kind must be message or task")
        context = data.get("context", {})
        if not isinstance(context, dict):
            raise ValueError("Invalid context")
        context = {
            key: text_field(context, key, limit, optional=True)
            for key, limit in (
                ("room_id", 100),
                ("room_name", 200),
                ("goal_id", 100),
                ("goal_title", 500),
                ("goal_context", 12000),
                ("conversation_id", 100),
            )
        }
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True)
        with self.db() as db:
            agent = db.execute("SELECT * FROM agents WHERE id=? AND revoked=0", (agent_id,))
            if agent.fetchone() is None:
                raise ValueError("Agent is unavailable")
            existing = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if (
                    existing["agent_id"],
                    existing["prompt"],
                    existing["context"],
                    existing["kind"],
                ) != (agent_id, prompt, encoded, kind):
                    raise ValueError("Request ID already used with different content")
                return self.job(existing)
            job_id = secrets.token_hex(12)
            now = time.time()
            db.execute(
                "INSERT INTO jobs(id,agent_id,request_id,kind,prompt,context,state,created,updated)"
                "VALUES(?,?,?,?,?,?,'queued',?,?)",
                (job_id, agent_id, request_id, kind, prompt, encoded, now, now),
            )
            self.audit(db, "job.submitted", job_id)
            return self.job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    @staticmethod
    def job(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["context"] = json.loads(data["context"])
        data.pop("lease", None)
        data.pop("lease_until", None)
        return data

    def poll(self, agent_id: str, instance: str, *, claim: bool = True) -> dict[str, Any]:
        with self.db() as db:
            self.expire(db)
            row = db.execute(
                "SELECT * FROM agents WHERE id=? AND revoked=0", (agent_id,)
            ).fetchone()
            if row is None:
                raise PermissionError("Agent revoked")
            if row["instance"] != instance and row["heartbeat"] > time.time() - LEASE_SECONDS:
                raise ValueError("Another connector owns this agent; stop it before reconnecting")
            db.execute(
                "UPDATE agents SET heartbeat=?,instance=? WHERE id=?",
                (time.time(), instance, agent_id),
            )
            active = db.execute(
                "SELECT * FROM jobs WHERE agent_id=? AND state IN ('running','cancelling')",
                (agent_id,),
            ).fetchone()
            if active or not claim:
                return {"job": None}
            row = db.execute(
                "SELECT * FROM jobs WHERE agent_id=? AND state='queued' ORDER BY created LIMIT 1",
                (agent_id,),
            ).fetchone()
            if row is None:
                return {"job": None}
            lease = secrets.token_hex(24)
            db.execute(
                "UPDATE jobs SET state='running',lease=?,lease_until=?,updated=? WHERE id=?",
                (lease, time.time() + LEASE_SECONDS, time.time(), row["id"]),
            )
            self.audit(db, "job.claimed", row["id"])
            job = self.job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            return {"job": job, "lease": lease}

    def report(self, agent_id: str, data: dict[str, Any]) -> dict[str, Any]:
        job_id = text_field(data, "job_id", 80)
        lease = text_field(data, "lease", 100)
        state = text_field(data, "state", 20)
        result = text_field(data, "text", 32000, optional=True)
        sequence = data.get("sequence", 0)
        if type(sequence) is not int or not 0 <= sequence <= 10000:
            raise ValueError("Invalid sequence")
        if state not in TERMINAL | {"running"}:
            raise ValueError("Invalid job state")
        with self.db() as db:
            self.expire(db)
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND agent_id=? AND lease=?",
                (job_id, agent_id, lease),
            ).fetchone()
            if row is None:
                raise PermissionError("Invalid job lease")
            if row["state"] in TERMINAL:
                return {"state": row["state"], "stop": True}
            if sequence:
                prior = db.execute(
                    "SELECT kind,text FROM events WHERE job_id=? AND sequence=?", (job_id, sequence)
                ).fetchone()
                if prior and (prior["kind"], prior["text"]) != (state, result):
                    raise ValueError("Sequence already used with different content")
                db.execute(
                    "INSERT OR IGNORE INTO events(job_id,sequence,kind,text,created) "
                    "VALUES(?,?,?,?,?)",
                    (job_id, sequence, state, result, time.time()),
                )
            target = "cancelled" if row["state"] == "cancelling" and state in TERMINAL else state
            if target in TERMINAL:
                db.execute(
                    "UPDATE jobs SET state=?,result=?,updated=? WHERE id=?",
                    (target, result, time.time(), job_id),
                )
                self.audit(db, "job." + target, job_id)
            else:
                db.execute(
                    "UPDATE jobs SET lease_until=?,updated=? WHERE id=?",
                    (time.time() + LEASE_SECONDS, time.time(), job_id),
                )
            db.execute("UPDATE agents SET heartbeat=? WHERE id=?", (time.time(), agent_id))
            return {"state": target, "stop": row["state"] == "cancelling"}

    def cancel(self, job_id: str) -> None:
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' "
                "END,updated=? WHERE id=? AND state IN ('queued','running')",
                (time.time(), job_id),
            )
            self.audit(db, "job.cancel-requested", job_id)

    def disconnect(self, agent_id: str, instance: str) -> None:
        with self.db() as db:
            db.execute(
                "UPDATE agents SET heartbeat=0,instance='' WHERE id=? AND instance=?",
                (agent_id, instance),
            )

    def snapshot(self) -> dict[str, Any]:
        with self.db() as db:
            self.expire(db)
            agents = []
            for row in db.execute("SELECT * FROM agents ORDER BY created"):
                agent = {
                    key: row[key]
                    for key in (
                        "id",
                        "name",
                        "role",
                        "capabilities",
                        "host",
                        "workspace",
                        "runtime",
                    )
                }
                agent["status"] = (
                    "revoked"
                    if row["revoked"]
                    else "unpaired"
                    if not row["token_hash"]
                    else "offline"
                    if row["heartbeat"] < time.time() - LEASE_SECONDS
                    else "available"
                )
                if (
                    agent["status"] == "available"
                    and db.execute(
                        "SELECT id FROM jobs WHERE agent_id=? "
                        "AND state IN ('running','cancelling')",
                        (row["id"],),
                    ).fetchone()
                ):
                    agent["status"] = "busy"
                agent["last_seen"] = row["heartbeat"]
                agents.append(agent)
            jobs = [
                self.job(row)
                for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 200")
            ]
            return {"agents": agents, "jobs": jobs}

    def detail(self, job_id: str) -> dict[str, Any]:
        with self.db() as db:
            self.expire(db)
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown job")
            return {
                "job": self.job(row),
                "events": [
                    dict(event)
                    for event in db.execute(
                        "SELECT sequence,kind,text,created FROM events "
                        "WHERE job_id=? ORDER BY sequence",
                        (job_id,),
                    )
                ],
            }
