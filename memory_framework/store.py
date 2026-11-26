"""Persistent, scoped events and versioned memory for host applications."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from uuid import uuid4


EVENT_KINDS = {"user_message", "assistant_message", "feedback", "opportunity", "tool_result"}
MEMORY_KINDS = {"fact", "preference", "habit", "commitment", "observation"}
OPERATIONS = {"add", "revise", "restrict", "forget", "prioritize"}
RELATIONS = {"correction", "supersession", "conflict", "unknown"}
RECALL_INTENTS = {"current", "historical", "evidence", "habit"}
MAX_REF_IDS = 20
MAX_REF_ID_LENGTH = 160
MAX_QUERY_LENGTH = 800
_GRAPH_DEPTH = 2
_GRAPH_WIDTH = 40
PROPOSAL_FIELDS = {
    "proposal_id", "operation", "kind", "subject", "predicate", "value", "evidence_ids",
    "effective_at", "memory_id", "base_version", "relation", "weight", "pinned",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    scope TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL, occurred_at TEXT NOT NULL,
    text TEXT NOT NULL, related_to TEXT, metadata TEXT NOT NULL, redacted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, event_id)
);
CREATE INDEX IF NOT EXISTS events_time ON events(scope, occurred_at DESC, event_id);
CREATE INDEX IF NOT EXISTS events_related ON events(scope, related_to);
CREATE TABLE IF NOT EXISTS event_sources (
    scope TEXT NOT NULL, event_id TEXT NOT NULL, source_id TEXT NOT NULL,
    PRIMARY KEY (scope, event_id, source_id)
);
CREATE INDEX IF NOT EXISTS event_sources_lookup ON event_sources(scope, source_id);
CREATE TABLE IF NOT EXISTS memories (
    scope TEXT NOT NULL, id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
    predicate TEXT NOT NULL, slot TEXT NOT NULL, latest INTEGER NOT NULL DEFAULT 0,
    restricted INTEGER NOT NULL DEFAULT 0, forgotten INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_slot ON memories(scope, slot) WHERE forgotten = 0;
CREATE TABLE IF NOT EXISTS versions (
    memory_id TEXT NOT NULL, version INTEGER NOT NULL, value TEXT NOT NULL, state TEXT NOT NULL,
    relation TEXT NOT NULL, recorded_at TEXT NOT NULL, effective_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, version)
);
CREATE TABLE IF NOT EXISTS priorities (
    id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
    weight INTEGER NOT NULL, pinned INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS priorities_lookup ON priorities(memory_id, id DESC);
CREATE TABLE IF NOT EXISTS evidence (
    scope TEXT NOT NULL, memory_id TEXT NOT NULL, version INTEGER NOT NULL, event_id TEXT NOT NULL,
    PRIMARY KEY (memory_id, version, event_id)
);
CREATE INDEX IF NOT EXISTS evidence_scope_event ON evidence(scope, event_id);
CREATE TABLE IF NOT EXISTS proposals (
    scope TEXT NOT NULL, proposal_id TEXT NOT NULL, digest TEXT NOT NULL,
    status TEXT NOT NULL, memory_id TEXT, version INTEGER,
    PRIMARY KEY (scope, proposal_id)
);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, name, limit=512):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Expected an ISO 8601 timestamp with a time zone")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        pass
    raise ValueError("Expected an ISO 8601 timestamp with a time zone")


def _ids(values, name, limit=MAX_REF_IDS):
    if not isinstance(values, (list, tuple)) or len(values) > limit:
        raise ValueError(f"{name} must be a list with at most {limit} IDs")
    return tuple(dict.fromkeys(_text(value, name, MAX_REF_ID_LENGTH) for value in values))


def _enum(value, allowed, name):
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"Invalid {name}")
    return value


def _normal(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _match_terms(query):
    terms = re.findall(r"[a-z0-9]+|[\u3040-\u30ff\u3400-\u9fff]+|[^\W\d_]+", _normal(query))
    return {piece for term in terms for piece in (
        (term,) if term.isascii() or len(term) <= 2 else (term, *(term[i:i + 2] for i in range(len(term) - 1)))
    )}


@dataclass(frozen=True)
class Scope:
    """Opaque identifiers supplied by a trusted, authenticated host."""

    tenant: str
    assistant: str
    user: str
    conversation: str

    def __post_init__(self):
        for key, value in vars(self).items():
            if _text(value, key, 160) != value:
                raise ValueError(f"{key} cannot have surrounding whitespace")

    @property
    def key(self):
        return _json([self.tenant, self.assistant, self.user, self.conversation])


class MemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        if str(self.path) == ":memory:":
            raise ValueError("Use a SQLite file, including for temporary tests")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete = ON")
        return db

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _event(db, scope, event_id, known_at=None):
        row = db.execute("""SELECT event_id, kind, occurred_at, text, related_to, metadata FROM events
            WHERE scope = ? AND event_id = ? AND redacted = 0 AND (? IS NULL OR occurred_at <= ?)
        """, (scope, event_id, known_at, known_at)).fetchone()
        if row is None:
            return None
        return dict(row, metadata=json.loads(row["metadata"]))

    def get_event(self, scope, event_id):
        with closing(self._connect()) as db:
            return self._event(db, scope.key, event_id)

    def record_event(self, scope, *, event_id, kind, occurred_at, text="", related_to=None, metadata=None):
        event_id = _text(event_id, "event_id", 160)
        _enum(kind, EVENT_KINDS, "event kind")
        if not isinstance(text, str) or len(text) > 32768:
            raise ValueError("Invalid event text")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict) or len(_json(metadata)) > 8192:
            raise ValueError("Invalid event metadata")
        if related_to is not None:
            related_to = _text(related_to, "related_to", 160)
        sources = _ids(metadata.get("used_evidence_ids", ()), "used_evidence_ids")
        event = dict(event_id=event_id, kind=kind, occurred_at=_time(occurred_at), text=text,
                     related_to=related_to, metadata=metadata)
        with self._write() as db:
            existing = db.execute("SELECT redacted FROM events WHERE scope = ? AND event_id = ?",
                                  (scope.key, event_id)).fetchone()
            if existing:
                if not existing["redacted"] and self._event(db, scope.key, event_id) == event:
                    return event
                raise ValueError("event_id has already been used")
            for source in (*sources, *((related_to,) if related_to else ())):
                if self._event(db, scope.key, source, event["occurred_at"]) is None:
                    raise ValueError("Event reference is unavailable in this scope")
            db.execute("INSERT INTO events(scope,event_id,kind,occurred_at,text,related_to,metadata) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (scope.key, event_id, kind, event["occurred_at"], text, related_to, _json(metadata)))
            db.executemany("INSERT INTO event_sources VALUES(?,?,?)",
                           ((scope.key, event_id, source) for source in sources))
        return event

    @staticmethod
    def _descendants(db, scope, initial):
        if not initial:
            return set()
        values = ",".join("(?)" for _ in initial)
        return {row[0] for row in db.execute(f"""
            WITH RECURSIVE affected(id) AS (
                VALUES {values}
                UNION
                SELECT s.event_id FROM event_sources s JOIN affected a ON a.id = s.source_id
                WHERE s.scope = ?
            )
            SELECT id FROM affected
        """, (*initial, scope))}

    def _restricted_sources(self, db, scope):
        rows = db.execute("SELECT DISTINCT e.event_id FROM evidence e JOIN memories m ON m.id = e.memory_id "
                          "WHERE e.scope = ? AND m.restricted = 1 AND m.forgotten = 0", (scope,))
        return self._descendants(db, scope, [row[0] for row in rows])

    def read_evidence(self, scope, ids, *, known_at=None, for_initiative=False):
        ids = _ids(ids, "ids")
        known_at = _time(known_at) if known_at is not None else None
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            restricted = self._restricted_sources(db, scope.key) if for_initiative else set()
            return [event for event_id in ids if event_id not in restricted
                    if (event := self._event(db, scope.key, event_id, known_at)) is not None]

    def _is_shared_experience(self, scope, event, known_at):
        if event["kind"] == "opportunity" or not event["text"].strip():
            return False
        role = event["metadata"].get("evidence_role")
        if role == "policy":
            return False
        if role == "experience":
            return True
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT DISTINCT m.kind FROM evidence e
                JOIN memories m ON m.id = e.memory_id
                JOIN versions v ON v.memory_id = e.memory_id AND v.version = e.version
                WHERE e.scope = ? AND e.event_id = ? AND m.forgotten = 0
                  AND v.state = 'confirmed' AND v.recorded_at <= ?
            """, (scope.key, event["event_id"], known_at))
            kinds = {row[0] for row in rows}
        return not kinds or bool(kinds - {"habit", "preference"})

    def recall(self, scope, query="", *, intent="current", anchor_ids=(), as_of=None, limit=8,
               known_at=None, for_initiative=False, exclude_kinds=(), exclude_event_id=None,
               confirmed_only=False):
        _enum(intent, RECALL_INTENTS, "recall intent")
        if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Invalid recall query or limit")
        anchors = set(_ids(anchor_ids, "anchor_ids"))
        now = datetime.now(timezone.utc).isoformat()
        as_of = _time(now if as_of is None else as_of)
        known_at = _time(now if known_at is None else known_at)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            restricted = self._restricted_sources(db, scope.key) if for_initiative else set()
            neighbors = self._nearby_events(db, scope.key, anchors, known_at, restricted, for_initiative)
            if intent == "evidence":
                return self._search_events(db, scope.key, query, min(as_of, known_at), anchors, neighbors,
                                           restricted, limit, exclude_kinds, exclude_event_id)
            rows = db.execute("""
                SELECT m.id AS memory_id, m.kind, m.subject, m.predicate, m.restricted,
                       m.latest AS head_version, v.*,
                       p.weight, p.pinned,
                       (SELECT initial.weight FROM priorities initial
                        WHERE initial.memory_id = m.id AND initial.recorded_at <= ?
                        ORDER BY initial.id LIMIT 1) AS initial_weight,
                       (SELECT MAX(a.version) FROM versions a
                        WHERE a.memory_id = m.id AND a.state IN ('confirmed','disputed')
                          AND a.effective_at <= ? AND a.recorded_at <= ?) AS active
                FROM memories m JOIN versions v ON m.id = v.memory_id
                LEFT JOIN priorities p ON p.id = (SELECT MAX(a.id) FROM priorities a
                    WHERE a.memory_id = m.id AND a.recorded_at <= ?)
                WHERE m.scope = ? AND m.forgotten = 0 AND v.recorded_at <= ?
                ORDER BY v.recorded_at DESC, v.version DESC
            """, (known_at, as_of, known_at, known_at, scope.key, known_at))
            terms = _match_terms(query)
            ranked = []
            for row in rows:
                if confirmed_only and row["state"] != "confirmed":
                    continue
                if intent == "current" and row["version"] != row["active"]:
                    continue
                if intent == "habit" and (row["kind"] not in {"habit", "preference"}
                        or row["state"] != "tentative" and row["version"] != row["active"]):
                    continue
                if for_initiative and row["restricted"]:
                    continue
                links = db.execute("SELECT e.event_id, s.redacted, s.occurred_at FROM evidence e JOIN events s "
                                   "ON s.scope = e.scope AND s.event_id = e.event_id "
                                   "WHERE e.memory_id = ? AND e.version = ? ORDER BY e.event_id",
                                   (row["memory_id"], row["version"])).fetchall()
                if any(link["redacted"] or link["event_id"] in restricted for link in links):
                    continue
                ids = [link["event_id"] for link in links if link["occurred_at"] <= known_at]
                if not ids:
                    continue
                overlap = len(terms & _match_terms(f'{row["subject"]} {row["predicate"]} {row["value"]}')) if terms else 0
                score = overlap + 20 * (row["memory_id"] in anchors or bool(neighbors.intersection(ids)))
                if (terms or anchors) and not score:
                    continue
                related = self._count_connections(db, scope.key, row, known_at, for_initiative)
                weight = row["weight"] or 0
                record = {key: row[key] for key in (
                    "memory_id", "kind", "subject", "predicate", "version", "head_version", "state", "relation",
                    "recorded_at", "effective_at",
                )}
                record.update(value=json.loads(row["value"]), evidence_ids=ids, weight=weight,
                              initial_weight=row["initial_weight"] or 0, related_memories=related,
                              pinned=bool(row["pinned"]))
                ranked.append(((intent == "habit" and row["state"] == "confirmed", score, weight), record))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            return [record for _, record in ranked[:limit]]

    @staticmethod
    def _count_connections(db, scope, record, known_at, for_initiative):
        return db.execute("""
            SELECT COUNT(DISTINCT b.memory_id)
            FROM evidence a
            JOIN evidence b ON b.scope = a.scope AND b.event_id = a.event_id AND b.memory_id != a.memory_id
            JOIN memories m ON m.id = b.memory_id
            JOIN versions v ON v.memory_id = b.memory_id AND v.version = b.version
            JOIN events s ON s.scope = a.scope AND s.event_id = a.event_id
            WHERE a.scope = ? AND a.memory_id = ? AND a.version = ?
              AND m.forgotten = 0 AND (? = 0 OR m.restricted = 0)
              AND s.redacted = 0 AND s.occurred_at <= ? AND v.recorded_at <= ?
        """, (scope, record["memory_id"], record["version"], for_initiative, known_at, known_at)).fetchone()[0]

    @staticmethod
    def _nearby_events(db, scope, anchors, known_at, restricted, for_initiative):
        if not anchors:
            return set()
        marks = ",".join("?" for _ in anchors)
        first = db.execute(f"""
            SELECT s.event_id FROM events s WHERE s.scope = ? AND s.redacted = 0 AND s.occurred_at <= ?
            AND (s.event_id IN ({marks}) OR s.event_id IN (
                SELECT e.event_id FROM evidence e JOIN memories m ON m.id = e.memory_id
                JOIN versions v ON v.memory_id = e.memory_id AND v.version = e.version
                WHERE e.scope = ? AND m.forgotten = 0 AND (? = 0 OR m.restricted = 0)
                  AND v.recorded_at <= ? AND m.id IN ({marks})
            ))
            ORDER BY s.occurred_at DESC, s.event_id LIMIT ?
        """, (scope, known_at, *anchors, scope, for_initiative, known_at, *anchors, _GRAPH_WIDTH))
        frontier = {row[0] for row in first} - restricted
        found = set(frontier)
        for _ in range(_GRAPH_DEPTH):
            if not frontier:
                break
            marks = ",".join("?" for _ in frontier)
            rows = db.execute(f"""
                SELECT s.event_id FROM events s WHERE s.scope = ? AND s.redacted = 0 AND s.occurred_at <= ?
                AND s.event_id IN (
                    SELECT related_to FROM events WHERE scope = ? AND event_id IN ({marks})
                    UNION SELECT event_id FROM events WHERE scope = ? AND related_to IN ({marks})
                    UNION SELECT source_id FROM event_sources WHERE scope = ? AND event_id IN ({marks})
                    UNION SELECT event_id FROM event_sources WHERE scope = ? AND source_id IN ({marks})
                )
                ORDER BY s.occurred_at DESC, s.event_id LIMIT ?
            """, (scope, known_at, scope, *frontier, scope, *frontier,
                  scope, *frontier, scope, *frontier, _GRAPH_WIDTH))
            frontier = {row[0] for row in rows} - found - restricted
            found.update(frontier)
        return found

    def _search_events(self, db, scope, query, as_of, anchors, neighbors, restricted, limit,
                       exclude_kinds, exclude_event_id):
        terms = _match_terms(query)
        ranked = []
        rows = db.execute("""SELECT event_id, kind, text FROM events
            WHERE scope = ? AND redacted = 0 AND occurred_at <= ?
            ORDER BY occurred_at DESC, event_id""", (scope, as_of))
        for row in rows:
            if row["event_id"] in restricted or row["event_id"] == exclude_event_id or row["kind"] in exclude_kinds:
                continue
            relevance = len(terms & _match_terms(row["text"])) if terms else 0
            score = relevance + 20 * (row["event_id"] in neighbors)
            if score or not (terms or anchors):
                ranked.append((score, row["event_id"]))
                if not (terms or anchors) and len(ranked) == limit:
                    break
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [self._event(db, scope, event_id) for _, event_id in ranked[:limit]]

    def propose(self, scope, proposal, *, event_id, approved=False):
        """Stage a model suggestion or apply it through a trusted host."""
        try:
            return self._propose(scope.key, proposal, event_id, approved is True)
        except (TypeError, ValueError) as exc:
            return {"status": "rejected", "reason": str(exc)}

    def _propose(self, scope, proposal, event_id, approved):
        if not isinstance(proposal, dict) or proposal.keys() - PROPOSAL_FIELDS:
            raise ValueError("Unknown proposal fields")
        proposal_id = _text(proposal.get("proposal_id"), "proposal_id", 160)
        operation = _enum(proposal.get("operation"), OPERATIONS, "operation")
        if operation == "restrict" and type(proposal.get("value", True)) is not bool:
            raise ValueError("Restrict value must be true or false")
        scoring = {"weight", "pinned"} & proposal.keys()
        if scoring and operation not in {"add", "prioritize"}:
            raise ValueError("Weight and pin belong to add or prioritize")
        if operation == "prioritize" and not scoring:
            raise ValueError("Prioritize requires weight or pinned")
        if "weight" in scoring and (type(proposal["weight"]) is not int or not 0 <= proposal["weight"] <= 100):
            raise ValueError("Weight must be an integer from 0 to 100")
        if "pinned" in scoring and type(proposal["pinned"]) is not bool:
            raise ValueError("Pinned must be true or false")
        ids = _ids(proposal.get("evidence_ids"), "evidence_ids")
        if not ids:
            raise ValueError("At least one source is required")
        digest = hashlib.sha256(_json(proposal).encode()).hexdigest()
        with self._write() as db:
            prior = db.execute("SELECT * FROM proposals WHERE scope = ? AND proposal_id = ?",
                               (scope, proposal_id)).fetchone()
            if prior and prior["digest"] != digest:
                raise ValueError("proposal_id has already been used for different data")
            if prior and operation == "forget" and prior["status"] == "applied":
                return self._result("applied", prior["memory_id"], prior["version"])
            event = self._event(db, scope, event_id)
            if event is None:
                raise ValueError("Cannot establish the current event")
            if any(self._event(db, scope, source, event["occurred_at"]) is None for source in ids):
                raise ValueError("Evidence is unavailable in this scope")
            if prior and (prior["status"] == "applied" or not approved and operation != "forget"):
                return self._result(prior["status"], prior["memory_id"], prior["version"])
            entry = self._resolve_target(db, scope, proposal, operation)
            if operation != "add":
                expected = prior["version"] if prior and prior["status"] == "tentative" else proposal["base_version"]
                if entry["latest"] != expected:
                    return self._result("stale_version", entry["id"], entry["latest"])
            if operation == "prioritize":
                version = entry["latest"]
                if "pinned" in proposal and not approved:
                    result = self._result("needs_confirmation", entry["id"], version)
                else:
                    version += 1
                    self._set_priority(db, entry["id"], proposal, event["occurred_at"], approved)
                    db.execute("UPDATE memories SET latest = ? WHERE id = ?", (version, entry["id"]))
                    db.executemany("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)",
                                   ((scope, entry["id"], version, source) for source in
                                    dict.fromkeys((*ids, event_id) if approved else ids)))
                    result = self._result("applied", entry["id"], version)
            elif operation in {"restrict", "forget"}:
                affected = set()
                if operation == "forget":
                    priority = self._priority(db, entry["id"])
                    if priority and priority["pinned"]:
                        return {
                            **self._result("rejected", entry["id"], entry["latest"]),
                            "reason_code": "pinned",
                            "reason": "An explicit retention request protects this memory",
                        }
                    affected, shared = self._forget_targets(db, scope, entry["id"])
                    if shared:
                        return {
                            **self._result("rejected", entry["id"], entry["latest"]),
                            "reason_code": "shared_evidence",
                            "reason": "Other memories still reference this evidence",
                            "evidence_ids": sorted(shared),
                        }
                if not approved:
                    result = self._result("needs_confirmation", entry["id"], entry["latest"])
                else:
                    result = self._restrict_or_forget(
                        db, scope, operation, entry, proposal.get("value", True), affected,
                    )
            else:
                if approved and event_id not in ids:
                    ids += (event_id,)
                result = self._save_version(db, scope, proposal, entry, event, ids, prior, approved)
                if result["status"] in {"tentative", "applied"} and ("weight" in proposal or approved and scoring):
                    self._set_priority(db, entry["id"], proposal, event["occurred_at"], approved)
            db.execute("INSERT OR REPLACE INTO proposals VALUES(?,?,?,?,?,?)",
                       (scope, proposal_id, digest, result["status"], result["memory_id"], result["version"]))
            return result

    @staticmethod
    def _priority(db, memory_id):
        return db.execute("SELECT weight, pinned FROM priorities WHERE memory_id = ? ORDER BY id DESC LIMIT 1",
                          (memory_id,)).fetchone()

    def _set_priority(self, db, memory_id, proposal, recorded_at, approved):
        old = self._priority(db, memory_id)
        weight = proposal.get("weight", old["weight"] if old else 0)
        pinned = bool(old and old["pinned"])
        if approved and "pinned" in proposal:
            pinned = proposal["pinned"]
        if old is None or (weight, pinned) != (old["weight"], bool(old["pinned"])):
            db.execute("INSERT INTO priorities(memory_id,recorded_at,weight,pinned) VALUES(?,?,?,?)",
                       (memory_id, recorded_at, weight, pinned))

    @staticmethod
    def _result(status, memory_id, version):
        return {"status": status, "memory_id": memory_id, "version": version}

    @staticmethod
    def _resolve_target(db, scope, proposal, operation):
        if operation == "add":
            if "memory_id" in proposal or "base_version" in proposal or "relation" in proposal:
                raise ValueError("Add does not address an existing version")
            kind = _enum(proposal.get("kind"), MEMORY_KINDS, "memory kind")
            subject = _text(proposal.get("subject"), "subject")
            predicate = _text(proposal.get("predicate"), "predicate")
            slot = _json([kind, _normal(subject), _normal(predicate)])
            row = db.execute("SELECT * FROM memories WHERE scope = ? AND slot = ? AND forgotten = 0",
                             (scope, slot)).fetchone()
            if row is None:
                memory_id = uuid4().hex
                db.execute("INSERT INTO memories(scope,id,kind,subject,predicate,slot) VALUES(?,?,?,?,?,?)",
                           (scope, memory_id, kind, subject, predicate, slot))
                row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return row
        if type(proposal.get("base_version")) is not int or proposal["base_version"] < 1:
            raise ValueError("An existing memory edit requires its base_version")
        if {"kind", "subject", "predicate"}.intersection(proposal):
            raise ValueError("An edit uses memory_id instead of kind, subject or predicate")
        row = db.execute("SELECT * FROM memories WHERE scope = ? AND id = ? AND forgotten = 0",
                         (scope, _text(proposal.get("memory_id"), "memory_id", 160))).fetchone()
        if row is None:
            raise ValueError("Memory is unavailable in this scope")
        return row

    def _save_version(self, db, scope, proposal, entry, event, ids, prior, approved):
        if "value" not in proposal:
            raise ValueError("Add and revise require a value")
        value = _json(proposal["value"])
        if len(value) > 4000:
            raise ValueError("Memory value is too long")
        relation = "new" if proposal["operation"] == "add" else _enum(
            proposal.get("relation"), RELATIONS, "revision relation",
        )
        effective = _time(proposal.get("effective_at", event["occurred_at"]))
        state = ("disputed" if relation == "conflict" else "confirmed") if approved else "tentative"
        previous = db.execute("SELECT * FROM versions WHERE memory_id = ? ORDER BY version DESC LIMIT 1",
                              (entry["id"],)).fetchone()
        if previous and relation == "new" and previous["value"] != value:
            raise ValueError("Existing memory has a different value; use revise")
        if previous and prior is None and relation == "new" and "effective_at" in proposal:
            if previous["effective_at"] != effective:
                raise ValueError("Existing memory has a different effective time; use revise")
        # Approval of a tentative proposal appends a recorded version, so a replay
        # before approval can still see the tentative history.
        if prior and (prior["memory_id"] != entry["id"] or prior["version"] != entry["latest"]):
            return self._result("stale_version", entry["id"], entry["latest"])
        reusable = previous and prior is None and relation == "new" and previous["state"] == state
        recorded = event["occurred_at"]
        if reusable:
            version = previous["version"]
        else:
            version = entry["latest"] + 1
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?)",
                       (entry["id"], version, value, state, relation, recorded, effective))
            db.execute("UPDATE memories SET latest = ? WHERE id = ?", (version, entry["id"]))
        db.executemany("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)",
                       ((scope, entry["id"], version, source) for source in ids))
        return self._result("applied" if approved else "tentative", entry["id"], version)

    def _forget_targets(self, db, scope, memory_id):
        ids = [row[0] for row in db.execute("SELECT DISTINCT event_id FROM evidence WHERE memory_id = ?", (memory_id,))]
        affected = self._descendants(db, scope, ids)
        shared = {row[0] for row in db.execute("""
            SELECT DISTINCT e.event_id FROM evidence e JOIN memories m ON m.id = e.memory_id
            WHERE e.scope = ? AND m.id != ? AND m.forgotten = 0
        """, (scope, memory_id))} & affected
        return affected, shared

    def _restrict_or_forget(self, db, scope, operation, entry, restricted, affected):
        version = entry["latest"] + 1
        if operation == "restrict":
            db.execute("UPDATE memories SET restricted = ?, latest = ? WHERE id = ?",
                       (restricted, version, entry["id"]))
        else:
            db.executemany("UPDATE events SET text = '', metadata = '{}', redacted = 1 "
                           "WHERE scope = ? AND event_id = ?", ((scope, source) for source in affected))
            db.execute("UPDATE versions SET value = 'null' WHERE memory_id = ?", (entry["id"],))
            db.execute("DELETE FROM priorities WHERE memory_id = ?", (entry["id"],))
            db.execute("UPDATE memories SET forgotten = 1, subject = '', predicate = '', slot = '', latest = ? "
                       "WHERE id = ?", (version, entry["id"]))
        return self._result("applied", entry["id"], version)
