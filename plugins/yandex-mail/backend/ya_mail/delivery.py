"""Durable Ya-Ma undo window. Each pending message has one OS-locked local sender."""

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import make_msgid
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from . import accounts, folders, knowledge, mail, outbox
from .accounts import Problem, current, database
from .transport import mailbox

WINDOW = 30


class ConfirmAddresses(BaseModel):
    send_to_these_addresses: bool = Field(
        title="Запланировать отправку перечисленным адресатам"
    )


def preview(msg):
    return {
        "from": str(msg["From"]),
        **{
            h.lower(): [a.addr_spec for v in msg.get_all(h, []) for a in v.addresses]
            for h in ("To", "Cc", "Bcc")
        },
        "subject": str(msg.get("Subject", "")),
        "attachments": [
            a.get_filename() or a.get_content_type() for a in msg.iter_attachments()
        ],
        "message_id": str(msg["Message-ID"]),
    }


def view(row):
    result = json.loads(row["result"]) if row["result"] else None
    if isinstance(result, dict):
        result.pop("edit_digest", None)
        if row["state"] == "saving_draft":
            result = {"check_drafts_for": result["check_drafts_for"]}
    return {
        "job_id": row["id"],
        "state": row["state"],
        **json.loads(row["preview"]),
        "due_at": datetime.fromtimestamp(row["due"], UTC).isoformat()
        if row["due"]
        else None,
        "result": result,
    }


def allowed(sources):
    for ref in sources:
        with database() as db:
            if not db.execute(
                "SELECT 1 FROM accounts WHERE email=?", (ref.account,)
            ).fetchone():
                raise Problem("Исходный ящик больше не подключён.")
        settings = folders.rules(ref.account)
        if any(mode == "blocked" for mode, _ in settings.values()):
            with mailbox(current(ref.account)) as client:
                folders.allow(ref.account, ref.folder, client)


def origins(ref):
    with database() as db:
        row = db.execute(
            "SELECT sources FROM draft_origins WHERE email=? AND ref=?",
            (ref.account, ref.model_dump_json()),
        ).fetchone()
    return [mail.Ref(**s) for s in json.loads(row[0])] if row else []


def spawn(job):
    kw = (
        {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, "-I", "-B", "-m", "ya_mail.cli", "deliver", job],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **kw,
    )


def recover():
    with database() as db:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT id FROM delivery_jobs WHERE state IN ('queued','dispatching')"
            )
        ]
    for job in ids:
        spawn(job)


def previous(db, email, request, digest):
    row = db.execute(
        "SELECT id,state,due,digest,preview,result FROM delivery_jobs WHERE email=? AND request=?",
        (email, request),
    ).fetchone()
    if row:
        if row["digest"] != digest:
            raise Problem("Этот ключ отправки уже использован с другим содержимым.")
        return view(row)
    old = db.execute(
        "SELECT * FROM sends WHERE email=? AND request=?", (email, request)
    ).fetchone()
    if old:
        if old["digest"] != digest:
            raise Problem("Этот ключ отправки уже использован с другим содержимым.")
        return {
            "state": "finished" if old["result"] else "uncertain",
            "message_id": old["message_id"],
            "result": json.loads(old["result"]) if old["result"] else None,
        }


async def enqueue(acc, request, fingerprint, msg, sources, draft, ctx, client):
    request = str(request)
    digest = hashlib.sha256(fingerprint).hexdigest()
    sources = list({s.model_dump_json(): s for s in sources}.values())
    allowed(sources)
    with database() as db:
        found = previous(db, acc.email, request, digest)
    if found:
        if found["state"] == "queued":
            spawn(found["job_id"])
        return found
    if not msg.get("Message-ID"):
        msg["Message-ID"] = make_msgid()
    recipients = outbox.envelope(acc, msg)
    for h in ("To", "Cc", "Bcc"):
        if any(v.defects for v in msg.get_all(h, [])):
            raise Problem("Проверьте формат адресов в черновике.")
    summary = preview(msg)
    risks = knowledge.guard(client, acc, msg)
    # A reply addressed to a different person can be risky even when both are familiar.
    if msg.get("In-Reply-To"):
        for s in sources:
            if s == draft:
                continue
            with mailbox(current(s.account)) as origin:
                sender = mail.reply_source(origin, s, current(s.account))
            original = {a for a, _ in knowledge.addresses(sender, ("From",))}
            reply_targets = {a for a, _ in knowledge.addresses(msg, ("To",))}
            if not reply_targets.issubset(original):
                risks.append("Получатель ответа отличается от From исходного письма.")
    if draft:
        with database() as db:
            earlier = db.execute(
                "SELECT protected FROM draft_origins WHERE ref=?",
                (draft.model_dump_json(),),
            ).fetchone()
        if earlier and earlier[0] == 2:
            risks.append("Проверьте адресатов исправленного черновика.")
    if risks:
        prompt = "\n".join(
            [
                *risks,
                "Подтвердите адресатов для отправки через 30 секунд.",
                json.dumps(summary, ensure_ascii=False, indent=2),
            ]
        )
        response = await ctx.elicit(prompt, ConfirmAddresses)
        if response.action != "accept" or not response.data.send_to_these_addresses:
            raise Problem("Отправка не запланирована.")
    if draft and mail.raw_message(client, draft, acc).as_bytes() != fingerprint:
        raise Problem("Черновик изменился. Откройте его снова и проверьте получателей.")
    allowed(sources)
    job = str(uuid4())
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        found = previous(db, acc.email, request, digest)
        if found:
            return found
        if draft:
            protection = db.execute(
                "SELECT protected FROM draft_origins WHERE ref=?",
                (draft.model_dump_json(),),
            ).fetchone()
            if protection and protection[0] < 0:
                raise Problem(
                    "Этот черновик заменён при правке. Выберите последнюю сохранённую версию."
                )
        if risks:
            db.executemany(
                "DELETE FROM contact_choices WHERE email=? AND addr=?",
                [(acc.email, a.lower()) for a in recipients],
            )
        db.execute(
            "INSERT INTO delivery_jobs(id,email,request,digest,state,due,mime,preview,sources,draft_ref) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                job,
                acc.email,
                request,
                digest,
                "queued",
                time.time() + WINDOW,
                msg.as_bytes(),
                json.dumps(summary),
                json.dumps([s.model_dump() for s in sources]),
                draft.model_dump_json() if draft else None,
            ),
        )
        result = view(
            db.execute("SELECT * FROM delivery_jobs WHERE id=?", (job,)).fetchone()
        )
    try:
        spawn(job)
    except OSError:
        stop(acc.email, job)
        raise Problem("Не удалось запустить отправитель. Письмо остановлено.") from None
    return result


def stop(email, job):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT state,result FROM delivery_jobs WHERE email=? AND id=?",
            (email, str(job)),
        ).fetchone()
        if not row:
            raise Problem("Такой отправки нет в этом ящике.")
        if row[0] == "queued":
            db.execute(
                "UPDATE delivery_jobs SET state='stopped',due=NULL WHERE id=?",
                (str(job),),
            )
        elif row[0] == "edited":
            saved_ref = mail.Ref(**json.loads(row["result"])["ref"]).model_dump_json()
            db.execute(
                "UPDATE delivery_jobs SET state='stopped',due=NULL WHERE email=? AND draft_ref=? AND state='queued'",
                (email, saved_ref),
            )
        elif row[0] not in ("stopped", "edited", "saving_draft"):
            raise Problem("Отменить уже нельзя. Проверьте статус отправки.")
    return {"job_id": str(job), "state": "stopped" if row[0] == "queued" else row[0]}


def listing(email, job=None):
    with database() as db:
        rows = db.execute(
            "SELECT id,state,due,preview,result,sources FROM delivery_jobs WHERE email=? AND (? IS NULL OR id=?) ORDER BY rowid DESC LIMIT 100",
            (email, job, job),
        ).fetchall()
    result = []
    for r in rows:
        try:
            allowed([mail.Ref(**s) for s in json.loads(r["sources"])])
        except Problem:
            result.append({"job_id": r["id"], "state": r["state"], "restricted": True})
        else:
            result.append(view(r))
    return result


def edit(account, job, patch):
    job = str(job)
    stop(current(account).email, job)
    acc = current(account, "organize")
    with worker_lock(job, prefix="edit") as owner:
        if not owner:
            raise Problem("Отправка остановлена. Сохранение правок ещё выполняется.")
        return edit_locked(acc, job, patch)


def edit_locked(acc, job, patch):
    with database() as db:
        row = db.execute(
            "SELECT * FROM delivery_jobs WHERE email=? AND id=?", (acc.email, job)
        ).fetchone()
    sources = [mail.Ref(**s) for s in json.loads(row["sources"])]
    allowed(sources)
    if row["state"] == "saving_draft":
        marker = json.loads(row["result"])
        with mailbox(acc) as client:
            folder = mail.special(client, "drafts")
            page = mail.search_folder(
                client,
                acc,
                folder,
                mail.Filter(message_id=marker["check_drafts_for"]),
                2,
            )
            if len(page["messages"]) > 1:
                raise Problem(
                    "Отправка остановлена. Найдено несколько черновиков с Message-ID "
                    + marker["check_drafts_for"]
                )
        with database() as db:
            if page["messages"]:
                item = page["messages"][0]
                ref = mail.Ref(**item["ref"])
                saved = {
                    "ref": item["ref"],
                    "message_id": marker["check_drafts_for"],
                    "folder": folder,
                    "from": acc.email,
                    "to": marker["to"],
                    "subject": item["Subject"],
                    "edit_digest": marker["edit_digest"],
                }
                db.execute(
                    "INSERT OR REPLACE INTO draft_origins VALUES(?,?,?,2)",
                    (acc.email, ref.model_dump_json(), row["sources"]),
                )
                db.execute(
                    "UPDATE delivery_jobs SET state='edited',result=?,mime=NULL WHERE id=?",
                    (json.dumps(saved), job),
                )
            else:
                previous_ref = marker["previous_ref"]
                if previous_ref:
                    db.execute(
                        "UPDATE draft_origins SET protected=2 WHERE ref=?",
                        (previous_ref,),
                    )
                db.execute(
                    "UPDATE delivery_jobs SET state=?,result=? WHERE id=?",
                    (marker["previous_state"], marker["previous_result"], job),
                )
        return edit_locked(acc, job, patch)
    prior_ref = None
    patch_key = hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()
    if row["state"] == "edited":
        saved = json.loads(row["result"])
        if (
            not any(v is not None for v in patch.values())
            or saved.get("edit_digest") == patch_key
        ):
            return {k: v for k, v in saved.items() if k != "edit_digest"}
        prior_ref = mail.Ref(**saved["ref"])
        with mailbox(acc) as client:
            msg = mail.raw_message(client, prior_ref, acc)
    else:
        msg = mail.PARSER.parsebytes(row["mime"])
    for key, val in patch.items():
        if val is None:
            continue
        if key == "text":
            part = msg.get_body(preferencelist=("plain",))
            if not part or (msg.get_body(preferencelist=("html",)) is not None):
                raise Problem(
                    "Отправка остановлена. Для этого HTML-письма сохраните черновик без замены текста и поправьте его в Почте."
                )
            part.set_content(val)
        else:
            del msg[key]
            if key == "subject":
                msg["Subject"] = val
            elif val:
                msg[key] = ", ".join(accounts.address(v) for v in val)
    del msg["Message-ID"]
    msg["Message-ID"] = make_msgid()
    marker = json.dumps(
        {
            "check_drafts_for": str(msg["Message-ID"]),
            "previous_state": row["state"],
            "previous_result": row["result"],
            "previous_ref": prior_ref.model_dump_json() if prior_ref else None,
            "edit_digest": patch_key,
            "to": preview(msg)["to"],
        }
    )
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "UPDATE delivery_jobs SET state='saving_draft',result=? WHERE id=? AND state=? AND result IS ?",
            (marker, job, row["state"], row["result"]),
        ).rowcount:
            raise Problem("Черновик уже сохраняется. Проверьте статус.")
        if prior_ref:
            key = prior_ref.model_dump_json()
            previous_job = db.execute(
                "SELECT state FROM delivery_jobs WHERE email=? AND draft_ref=?",
                (acc.email, key),
            ).fetchone()
            if previous_job and previous_job[0] not in ("queued", "stopped"):
                raise Problem(
                    "Черновик уже передан на отправку или изменён. Проверьте очередь."
                )
            db.execute(
                "UPDATE delivery_jobs SET state='stopped',due=NULL WHERE email=? AND draft_ref=? AND state='queued'",
                (acc.email, key),
            )
            db.execute("UPDATE draft_origins SET protected=-1 WHERE ref=?", (key,))
    with mailbox(acc) as client:
        before = json.loads(row["preview"])
        after = preview(msg)
        changed = any(before[key] != after[key] for key in ("to", "cc", "bcc"))
        draft = outbox.save_draft(
            client, acc, msg, sources, protected=2 if changed else 1
        )
    with database() as db:
        db.execute(
            "UPDATE delivery_jobs SET state='edited',mime=NULL,result=? WHERE id=?",
            (json.dumps(draft | {"edit_digest": patch_key}), job),
        )
    return draft


@contextmanager
def worker_lock(job, prefix="delivery"):
    path = accounts.DATA / (prefix + "-" + str(UUID(job)) + ".lock")
    with path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


def finish(job, state, result):
    with database() as db:
        db.execute(
            "UPDATE delivery_jobs SET state=?,result=?,due=NULL,mime=NULL WHERE id=?",
            (state, json.dumps(result), job),
        )
        if state == "finished" and result.get("accepted") and not result.get("refused"):
            db.execute(
                "UPDATE draft_origins SET protected=0 WHERE ref=(SELECT draft_ref FROM delivery_jobs WHERE id=?)",
                (job,),
            )


def reconcile(row):
    with database() as db:
        ledger = db.execute(
            "SELECT result FROM sends WHERE email=? AND request=?",
            (row["email"], row["request"]),
        ).fetchone()
    if ledger and ledger[0]:
        finish(row["id"], "finished", json.loads(ledger[0]))
    else:
        finish(
            row["id"],
            "uncertain",
            {"check_sent_for": json.loads(row["preview"])["message_id"]},
        )


def awake_time():
    # Linux/macOS monotonic uptime excludes suspend. On Windows QPC can include it.
    if sys.platform == "win32":
        import ctypes

        ticks = ctypes.c_uint64()
        if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(ticks)):
            return ticks.value / 10_000_000
    return time.monotonic()


def dispatch(row, last_tick, awake_tick=None):
    job = row["id"]
    sources = [mail.Ref(**s) for s in json.loads(row["sources"])]

    def permissions(db):
        emails = sorted({row["email"], *(ref.account for ref in sources)})
        return [
            (
                list(
                    db.execute(
                        "SELECT mode,secret FROM accounts WHERE email=?", (email,)
                    )
                ),
                list(
                    db.execute(
                        "SELECT folder,delimiter,mode FROM folder_rules WHERE email=? ORDER BY folder",
                        (email,),
                    )
                ),
            )
            for email in emails
        ]

    try:
        with database() as db:
            snapshot = permissions(db)
        acc = current(row["email"], "send")
        allowed(sources)
        if row["draft_ref"]:
            ref = mail.Ref.model_validate_json(row["draft_ref"])
            with mailbox(acc) as client:
                live = mail.raw_message(client, ref, acc).as_bytes()
            if hashlib.sha256(live).hexdigest() != row["digest"]:
                raise Problem("Черновик изменился. Отправка остановлена.")
    except Exception as exc:
        with database() as db:
            db.execute(
                "UPDATE delivery_jobs SET state='stopped', due=NULL,result=? WHERE id=? AND state='queued'",
                (
                    json.dumps(
                        {
                            "error": str(exc)
                            if isinstance(exc, Problem)
                            else "Не удалось проверить доступ."
                        }
                    ),
                    job,
                ),
            )
        return
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        if permissions(db) != snapshot:
            db.execute(
                "UPDATE delivery_jobs SET due=? WHERE id=? AND state='queued'",
                (now + WINDOW, job),
            )
            return
        if now < last_tick or (
            awake_tick is not None
            and abs(now - last_tick - (awake_time() - awake_tick)) > 0.5
        ):
            db.execute(
                "UPDATE delivery_jobs SET due=? WHERE id=? AND state='queued'",
                (now + WINDOW, job),
            )
            return
        if not db.execute(
            "UPDATE delivery_jobs SET state='dispatching',due=NULL WHERE id=? AND state='queued' AND due<=?",
            (job, now),
        ).rowcount:
            return
    try:
        result = outbox.submit_digest(
            acc, row["request"], row["digest"], mail.PARSER.parsebytes(row["mime"])
        )
    except Exception as exc:
        with database() as db:
            ledger = db.execute(
                "SELECT 1 FROM sends WHERE email=? AND request=?",
                (acc.email, row["request"]),
            ).fetchone()
        if ledger:
            reconcile(row)
        else:
            finish(
                job,
                "failed",
                {
                    "error": str(exc)
                    if isinstance(exc, Problem)
                    else "Ошибка отправителя."
                },
            )
    else:
        finish(job, "finished", result)


def run(job):
    with worker_lock(job) as owner:
        if not owner:
            return
        with database() as db:
            row = db.execute(
                "SELECT * FROM delivery_jobs WHERE id=?", (job,)
            ).fetchone()
            if not row:
                return
            if row["state"] == "queued":
                # A restarted sender never dispatches an overdue message immediately.
                now = time.time()
                due = (
                    now + WINDOW
                    if row["started"] or row["due"] < now + WINDOW - 3
                    else row["due"]
                )
                db.execute(
                    "UPDATE delivery_jobs SET started=1,due=? WHERE id=? AND state='queued'",
                    (due, job),
                )
        if row["state"] == "dispatching":
            reconcile(row)
            return
        last, last_awake = time.time(), awake_time()
        while True:
            now, awake = time.time(), awake_time()
            with database() as db:
                if (
                    now < last
                    or now - last > 3
                    or abs(now - last - (awake - last_awake)) > 0.5
                ):
                    db.execute(
                        "UPDATE delivery_jobs SET due=? WHERE id=? AND state='queued'",
                        (now + WINDOW, job),
                    )
                row = db.execute(
                    "SELECT state,due FROM delivery_jobs WHERE id=?", (job,)
                ).fetchone()
                if not row or row["state"] != "queued":
                    return
                if now >= row["due"]:
                    row = db.execute(
                        "SELECT * FROM delivery_jobs WHERE id=?", (job,)
                    ).fetchone()
            if now >= row["due"]:
                dispatch(row, now, awake)
            last, last_awake = now, awake
            time.sleep(0.35)
