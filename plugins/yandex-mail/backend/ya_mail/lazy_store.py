"""Durable policy, quota windows and move journal for the optional backlog worker."""

from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, Field

from . import accounts


class Rules(BaseModel):
    folders: list[str] = Field(default_factory=lambda: ["INBOX"], min_length=1)
    protected: list[str] = Field(default_factory=list)
    approved: list[str] = Field(default_factory=list)
    review: list[str] = Field(default_factory=list)
    sent: bool = False
    weekly: float = Field(default=1, gt=0, le=100)
    anchor: str = ""
    endpoint: str = ""

    @property
    def revision(self):
        return sha256(self.model_dump_json().encode()).hexdigest()


@contextmanager
def db():
    with accounts.database() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS lazy_rules(
                account TEXT PRIMARY KEY REFERENCES accounts(email) ON DELETE CASCADE, policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lazy_windows(
                owner TEXT, reset INTEGER, short_left REAL, week_reset INTEGER, week_used REAL,
                PRIMARY KEY(owner,reset));
            CREATE TABLE IF NOT EXISTS lazy_completed_windows(
                owner TEXT, reset INTEGER, revision TEXT, PRIMARY KEY(owner,reset,revision));
            CREATE TABLE IF NOT EXISTS lazy_runs(
                id TEXT PRIMARY KEY, started INTEGER DEFAULT(unixepoch()), reason TEXT);
            CREATE TABLE IF NOT EXISTS lazy_run_accounts(
                run TEXT REFERENCES lazy_runs(id), account TEXT REFERENCES accounts(email) ON DELETE CASCADE,
                PRIMARY KEY(run,account));
            CREATE TABLE IF NOT EXISTS lazy_inventory(
                account TEXT REFERENCES accounts(email) ON DELETE CASCADE, folder TEXT, validity INTEGER,
                cutoff TEXT, next_uid INTEGER, PRIMARY KEY(account,folder));
            CREATE TABLE IF NOT EXISTS lazy_results(
                run TEXT REFERENCES lazy_runs(id), account TEXT REFERENCES accounts(email) ON DELETE CASCADE,
                folder TEXT, validity INTEGER, uid INTEGER, received REAL, state TEXT, subject TEXT,
                reason TEXT, dest TEXT, PRIMARY KEY(run,account,folder,validity,uid));
            CREATE TABLE IF NOT EXISTS lazy_queue(
                id INTEGER PRIMARY KEY, account TEXT REFERENCES accounts(email) ON DELETE CASCADE,
                folder TEXT, validity INTEGER, uid INTEGER, received REAL, revision TEXT,
                state TEXT DEFAULT 'pending', run TEXT, subject TEXT DEFAULT '', reason TEXT DEFAULT '',
                dest TEXT, dest_validity INTEGER, dest_next INTEGER, mid TEXT,
                UNIQUE(account,folder,validity,uid));
            CREATE INDEX IF NOT EXISTS lazy_order ON lazy_queue(state,received,id);
        """)
        yield con


def lock():
    accounts.DATA.mkdir(parents=True, exist_ok=True)
    return FileLock(accounts.DATA / "lazy-worker.lock", timeout=0)


def policy_lock():
    return FileLock(accounts.DATA / "lazy-policy.lock", timeout=40)


def policies():
    with db() as con:
        return {
            r[0]: Rules.model_validate_json(r[1])
            for r in con.execute("SELECT * FROM lazy_rules")
        }


def skill_path(email):
    return accounts.DATA / "lazy" / email / "SKILL.md"


def save(email, rules):
    # All callers change policy under the same process lock as move dispatch.
    path = skill_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            "# Правила ленивого разбора",
            "",
            f"Ящик {email}",
            "",
            "Папки: " + ", ".join(rules.folders),
            "Всегда сохранять: " + ", ".join(rules.protected),
            "Предлагать рекламу для переноса в Корзину: " + ", ".join(rules.approved),
            "На согласование: " + ", ".join(rules.review),
            "Защита отмеченных писем и переписки с ответами владельца действует раньше правил рассылок.",
            "Незнакомые отправители и письма с существенной информацией идут на согласование.",
            f"Доступ к заголовкам отправленных: {'да' if rules.sent else 'нет'}.",
            f"Недельный предел: {rules.weekly:g} п. п. за окно.",
            "",
        ]
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO lazy_rules VALUES(?,?)",
            (email, rules.model_dump_json()),
        )
        con.execute(
            "DELETE FROM lazy_queue WHERE account=? AND revision!=? AND state IN ('pending','working','keep','awaiting_trash')",
            (email, rules.revision),
        )
        con.execute("DELETE FROM lazy_inventory WHERE account=?", (email,))
    tmp.replace(path)


def enqueue(email, folder, validity, pairs, revision):
    with db() as con:
        con.executemany(
            """INSERT OR IGNORE INTO lazy_queue(account,folder,validity,uid,received,revision)
                        VALUES(?,?,?,?,?,?)""",
            ((email, folder, validity, uid, date, revision) for uid, date in pairs),
        )


def start_run(emails):
    run = uuid4().hex
    with db() as con:
        con.execute(
            "UPDATE lazy_runs SET reason='Предыдущий процесс остановлен' WHERE reason IS NULL"
        )
        con.execute(
            "UPDATE lazy_queue SET state='pending',run=NULL WHERE state='working'"
        )
        con.execute("INSERT INTO lazy_runs(id) VALUES(?)", (run,))
        con.executemany(
            "INSERT INTO lazy_run_accounts VALUES(?,?)",
            ((run, email) for email in emails),
        )
    return run


def claim(run, accounts_):
    marks = ",".join("?" for _ in accounts_)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            f"""SELECT * FROM lazy_queue WHERE state='pending' AND account IN ({marks})
                            ORDER BY received,id LIMIT 1""",
            list(accounts_),
        ).fetchone()
        if row:
            con.execute(
                "UPDATE lazy_queue SET state='working',run=? WHERE id=?",
                (run, row["id"]),
            )
            return dict(row)


def finish_item(key, state, reason, subject=""):
    with db() as con:
        con.execute(
            "UPDATE lazy_queue SET state=?,reason=?,subject=? WHERE id=?",
            (state, reason, subject, key),
        )
        if state != "pending":
            con.execute(
                """INSERT OR REPLACE INTO lazy_results
                        SELECT run,account,folder,validity,uid,received,state,subject,reason,dest
                        FROM lazy_queue WHERE id=? AND run IS NOT NULL""",
                (key,),
            )


def reserve_move(key, state, dest, box, mid, reason, subject):
    with db() as con:
        con.execute(
            """UPDATE lazy_queue SET state=?,dest=?,dest_validity=?,dest_next=?,mid=?,reason=?,subject=?
                       WHERE id=? AND state IN ('working','awaiting_trash')""",
            (
                "moving_" + state,
                dest,
                box[b"UIDVALIDITY"],
                box[b"UIDNEXT"],
                mid,
                reason,
                subject,
                key,
            ),
        )
        con.execute(
            """INSERT OR REPLACE INTO lazy_results
                    SELECT run,account,folder,validity,uid,received,state,subject,reason,dest
                    FROM lazy_queue WHERE id=?""",
            (key,),
        )


def finish_run(run, reason):
    with db() as con:
        con.execute(
            "UPDATE lazy_queue SET state='pending',run=NULL WHERE run=? AND state='working'",
            (run,),
        )
        con.execute("UPDATE lazy_runs SET reason=? WHERE id=?", (reason, run))


def window(owner, reset, remaining, week_reset, week_used):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO lazy_windows VALUES(?,?,?,?,?)",
            (owner, reset, remaining, week_reset, week_used),
        )
        return con.execute(
            "SELECT short_left,week_reset,week_used FROM lazy_windows WHERE owner=? AND reset=?",
            (owner, reset),
        ).fetchone()


def digest(email, permitted, run=None, offset=0, limit=20):
    with db() as con:
        report = con.execute(
            """SELECT r.* FROM lazy_runs r JOIN lazy_run_accounts q ON q.run=r.id
            WHERE q.account=? AND (? IS NULL OR r.id=?) ORDER BY r.started DESC,r.rowid DESC LIMIT 1""",
            (email, run, run),
        ).fetchone()
        if not report:
            return {
                "account": email,
                "attention": [],
                "counts": {},
                "reason": "Нет обработанных писем",
            }
        # Avoid exposing blocked folders even in the aggregate. The set is local to this SQLite connection.
        scope = "account=? AND run=?"
        args = (email, report["id"])
        if permitted is not None:
            con.execute("CREATE TEMP TABLE visible (folder TEXT PRIMARY KEY)")
            con.executemany(
                "INSERT INTO visible VALUES(?)", ((name,) for name in permitted)
            )
            scope += " AND folder IN visible AND (dest IS NULL OR dest IN visible)"
        counts = dict(
            con.execute(
                f"SELECT state,COUNT(*) FROM lazy_results WHERE {scope} GROUP BY state",
                args,
            )
        )
        rows = con.execute(
            f"""SELECT state,subject,reason,COALESCE(dest,folder) AS folder,uid AS original_uid
                         FROM lazy_results WHERE {scope} AND state IN ('review','uncertain','moving_trash','moving_review','awaiting_trash')
                         ORDER BY (state='awaiting_trash'),(state='review'),(reason='Отправитель требует согласования'),received,uid
                         LIMIT ? OFFSET ?""",
            (*args, limit + 1, offset),
        ).fetchall()
        attention = [dict(r) for r in rows[:limit]]
        return {
            "account": email,
            "run": report["id"],
            "attention": attention,
            "next_offset": offset + limit if len(rows) > limit else None,
            "counts": counts,
            "reason": report["reason"] or "Выполняется",
        }
