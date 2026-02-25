import sqlite3
import json
import os
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "anki_quiz.db"


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS question_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_file TEXT,
            prompt TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            options TEXT NOT NULL,          -- JSON: ["opt_a", "opt_b", ...]
            correct_index INTEGER NOT NULL, -- 0-based
            explanation TEXT NOT NULL,      -- JSON: {"correct": "...", "options": {"a": "...", ...}}
            category TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (set_id) REFERENCES question_sets(id)
        );

        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL UNIQUE,
            times_seen INTEGER DEFAULT 0,
            times_correct INTEGER DEFAULT 0,
            mastered INTEGER DEFAULT 0,     -- 1 = user clicked "I know this"
            last_seen_at TIMESTAMP,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            role TEXT NOT NULL,             -- 'user' or 'assistant'
            content TEXT NOT NULL,
            question_id INTEGER,            -- nullable, links to the question being viewed
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (set_id) REFERENCES question_sets(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
    """)
    conn.commit()

    # Add sort_order column if not exists
    cols = [row[1] for row in conn.execute("PRAGMA table_info(question_sets)").fetchall()]
    if "sort_order" not in cols:
        conn.execute("ALTER TABLE question_sets ADD COLUMN sort_order INTEGER DEFAULT 0")
        conn.commit()

    conn.close()


# ── Question Set CRUD ──

def create_question_set(name: str, source_file: str = "", prompt: str = "") -> int:
    conn = get_conn()
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM question_sets"
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO question_sets (name, source_file, prompt, sort_order) VALUES (?, ?, ?, ?)",
        (name, source_file, prompt, max_order + 1),
    )
    conn.commit()
    set_id = cur.lastrowid
    conn.close()
    return set_id


def get_all_question_sets() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT qs.*, COUNT(q.id) as question_count "
        "FROM question_sets qs LEFT JOIN questions q ON qs.id = q.set_id "
        "GROUP BY qs.id ORDER BY qs.sort_order ASC, qs.created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_question_set(set_id: int):
    conn = get_conn()
    # Delete in dependency order: chat_messages -> progress -> questions -> question_sets
    conn.execute("DELETE FROM chat_messages WHERE set_id = ?", (set_id,))
    conn.execute(
        "DELETE FROM progress WHERE question_id IN "
        "(SELECT id FROM questions WHERE set_id = ?)", (set_id,)
    )
    conn.execute("DELETE FROM questions WHERE set_id = ?", (set_id,))
    conn.execute("DELETE FROM question_sets WHERE id = ?", (set_id,))
    conn.commit()
    conn.close()


def reorder_question_sets(ordered_ids: list[int]):
    conn = get_conn()
    for i, set_id in enumerate(ordered_ids):
        conn.execute(
            "UPDATE question_sets SET sort_order = ? WHERE id = ?",
            (i, set_id),
        )
    conn.commit()
    conn.close()


# ── Question CRUD ──

def add_question(set_id: int, question_text: str, options: list[str],
                 correct_index: int, explanation: dict, category: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO questions (set_id, question_text, options, correct_index, explanation, category) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (set_id, question_text, json.dumps(options), correct_index, json.dumps(explanation), category),
    )
    conn.commit()
    qid = cur.lastrowid
    # init progress row
    conn.execute(
        "INSERT OR IGNORE INTO progress (question_id) VALUES (?)", (qid,)
    )
    conn.commit()
    conn.close()
    return qid


def get_questions_by_set(set_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT q.*, p.times_seen, p.times_correct, p.mastered, p.last_seen_at "
        "FROM questions q LEFT JOIN progress p ON q.id = p.question_id "
        "WHERE q.set_id = ? ORDER BY q.id",
        (set_id,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"])
        d["explanation"] = json.loads(d["explanation"])
        result.append(d)
    return result


def get_review_queue(set_id: int) -> list[dict]:
    """Get unmastered questions for Anki-style review."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT q.*, p.times_seen, p.times_correct, p.mastered, p.last_seen_at "
        "FROM questions q LEFT JOIN progress p ON q.id = p.question_id "
        "WHERE q.set_id = ? AND (p.mastered = 0 OR p.mastered IS NULL) "
        "ORDER BY p.times_seen ASC, RANDOM()",
        (set_id,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"])
        d["explanation"] = json.loads(d["explanation"])
        result.append(d)
    return result


def get_set_stats(set_id: int) -> dict:
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE set_id = ?", (set_id,)
    ).fetchone()[0]
    mastered = conn.execute(
        "SELECT COUNT(*) FROM questions q JOIN progress p ON q.id = p.question_id "
        "WHERE q.set_id = ? AND p.mastered = 1", (set_id,)
    ).fetchone()[0]
    conn.close()
    return {"total": total, "mastered": mastered, "remaining": total - mastered}


# ── Progress ──

def record_answer(question_id: int, is_correct: bool):
    conn = get_conn()
    conn.execute(
        "UPDATE progress SET times_seen = times_seen + 1, "
        "times_correct = times_correct + ?, last_seen_at = CURRENT_TIMESTAMP "
        "WHERE question_id = ?",
        (1 if is_correct else 0, question_id),
    )
    conn.commit()
    conn.close()


def mark_mastered(question_id: int, mastered: bool = True):
    conn = get_conn()
    conn.execute(
        "UPDATE progress SET mastered = ? WHERE question_id = ?",
        (1 if mastered else 0, question_id),
    )
    conn.commit()
    conn.close()


# ── Categories ──

def get_categories_for_set(set_id: int) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT category FROM questions WHERE set_id = ? AND category != ''",
        (set_id,),
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


# ── Chat Messages ──

def add_chat_message(set_id: int, role: str, content: str, question_id: int | None = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO chat_messages (set_id, role, content, question_id) VALUES (?, ?, ?, ?)",
        (set_id, role, content, question_id),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()
    return msg_id


def get_chat_history(set_id: int, limit: int = 50) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE set_id = ? ORDER BY created_at ASC LIMIT ?",
        (set_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
