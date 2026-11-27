"""Mailbox-local evidence. Only user aliases and verified Sent recipients establish familiarity."""

import hashlib
import json
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from itertools import batched
from typing import Literal
from uuid import uuid4

from . import accounts, folders, mail
from .accounts import Problem, address, database

Mode = Literal["off", "contacts", "contacts_and_facts"]
Category = Literal["preference", "specification", "deadline", "context"]


def directory(email):
    return accounts.DATA / "memory" / hashlib.sha256(email.encode()).hexdigest()


def invalidate_files(email):
    for name in ("mail-memory.md", "mail-memory.jsonl"):
        (directory(email) / name).unlink(missing_ok=True)


def mode(db, email):
    row = db.execute("SELECT mode FROM learning WHERE email=?", (email,)).fetchone()
    return row[0] if row else "contacts_and_facts"


def configure(email, value):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if value:
            invalidate_files(email)
            db.execute("INSERT OR REPLACE INTO learning VALUES(?,?)", (email, value))
        return {"account": email, "mode": mode(db, email)}


def visible(db, email, catalog):
    settings = {
        r["folder"]: (r["mode"], r["delimiter"])
        for r in db.execute("SELECT * FROM folder_rules WHERE email=?", (email,))
    }
    present = {r["folder"] for r in catalog if r["selectable"]}
    if any(m == "blocked" and f not in present for f, (m, _) in settings.items()):
        return set()
    has_block = any(m == "blocked" for m, _ in settings.values())
    return {
        r["folder"]
        for r in catalog
        if r["selectable"]
        and r["mode"] != "blocked"
        and not folders.blocked(r["folder"], settings)
        and not (has_block and any(x.lower() == "\\all" for x in r["roles"]))
    }


def addresses(msg, keys):
    for key in keys:
        for h in msg.get_all(key, []):
            if h.defects:
                continue
            for a in h.addresses:
                try:
                    yield (
                        address(a.addr_spec).lower(),
                        " ".join(a.display_name.split())[:200],
                    )
                except Problem:
                    continue


def observe(db, email, ref, msg, sent, received=None):
    try:
        stamp = received or parsedate_to_datetime(str(msg.get("Date", "")))
        stamp = stamp.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        stamp = 0
    ref_key = ref.model_dump_json()
    mid = str(msg.get("Message-ID", "")).strip()
    identity = mid if re.fullmatch(r"<[^<>\s]{1,495}>", mid) else ref_key
    forgotten = {
        r[0]
        for r in db.execute(
            "SELECT addr FROM forgotten_contacts WHERE email=?", (email,)
        )
    }
    rows = [
        (
            email,
            ref_key,
            addr,
            name,
            ref.folder,
            identity,
            stamp,
            " ".join(str(msg.get("Subject", "")).split())[:512],
            int(sent),
        )
        for addr, name in addresses(msg, ("To", "Cc", "Bcc") if sent else ("From",))
        if addr != email and addr not in forgotten
    ]
    db.executemany(
        "INSERT OR REPLACE INTO contact_evidence VALUES(?,?,?,?,?,?,?,?,?)", rows
    )


def index_sent(client, account):
    with database() as db:
        if mode(db, account.email) == "off":
            return
    name = folders.verified_role(client, "sent")
    if not name:
        return
    catalog = folders.catalog(client, account.email)
    with database() as db:
        if name not in visible(db, account.email, catalog):
            return
    info = client.select_folder(name, readonly=True)
    validity = info[b"UIDVALIDITY"]
    key = (account.email, name, validity)
    with database() as db:
        previous = db.execute(
            "SELECT position FROM sent_index WHERE email=? AND folder=? AND validity=?",
            key,
        ).fetchone()
    first = previous[0] + 1 if previous else 1
    last = info[b"UIDNEXT"] - 1
    if last < first:
        return
    uids = sorted(
        client.search(
            [
                "UNDELETED",
                "SINCE",
                date.today() - timedelta(days=90),
                "UID",
                f"{first}:{last}",
            ]
        )
    )
    section = "BODY.PEEK[HEADER.FIELDS (FROM TO CC BCC DATE SUBJECT MESSAGE-ID)]"
    for group in batched(uids, 100):
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            if mode(db, account.email) == "off" or name not in visible(
                db, account.email, catalog
            ):
                return
            refs = [
                mail.Ref(account=account.email, folder=name, validity=validity, uid=u)
                for u in group
            ]
        rows = client.fetch(group, [section, "INTERNALDATE"])
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            if mode(db, account.email) == "off" or name not in visible(
                db, account.email, catalog
            ):
                return
            for ref in refs:
                if row := rows.get(ref.uid):
                    msg = mail.fetched_headers(row)
                    if [a for a, _ in addresses(msg, ("From",))] == [account.email]:
                        observe(
                            db, account.email, ref, msg, True, row.get(b"INTERNALDATE")
                        )
            db.execute(
                "INSERT INTO sent_index VALUES(?,?,?,?) ON CONFLICT(email,folder,validity) DO UPDATE SET position=max(position,excluded.position)",
                (*key, group[-1]),
            )
    with database() as db:
        db.execute(
            "INSERT INTO sent_index VALUES(?,?,?,?) ON CONFLICT(email,folder,validity) DO UPDATE SET position=max(position,excluded.position)",
            (*key, last),
        )


def contacts(db, email, allowed):
    result = {}

    def entry(addr):
        return result.setdefault(
            addr,
            {
                "address": addr,
                "names": set(),
                "aliases": set(),
                "messages": set(),
                "last": 0,
                "last_subject": "",
                "trusted": False,
                "source": None,
            },
        )

    for r in db.execute(
        "SELECT * FROM contact_evidence WHERE email=? ORDER BY stamp, rowid", (email,)
    ):
        if r["folder"] not in allowed:
            continue
        c = entry(r["addr"])
        c["messages"].add(r["identity"])
        if r["name"]:
            c["names"].add(r["name"])
        c.update(
            last=r["stamp"],
            last_subject=r["subject"],
            source=json.loads(r["ref"]),
            trusted=c["trusted"] or bool(r["sent"]),
        )
    for addr, alias in db.execute(
        "SELECT addr,alias FROM contact_aliases WHERE email=?", (email,)
    ):
        c = entry(addr)
        c["aliases"].add(alias)
        c["trusted"] = True
    return [
        c
        | {
            "names": sorted(c["names"]),
            "aliases": sorted(c["aliases"]),
            "frequency": len(c["messages"]),
            "last": datetime.fromtimestamp(c["last"], UTC).isoformat()
            if c["last"]
            else None,
        }
        for c in result.values()
    ]


def clean_contact(c):
    return {k: v for k, v in c.items() if k != "messages"}


def lookup(client, account, query):
    index_sent(client, account)
    catalog = folders.catalog(client, account.email)
    with database() as db:
        allowed = visible(db, account.email, catalog)
        data = (
            contacts(db, account.email, allowed)
            if mode(db, account.email) != "off"
            else []
        )
    q = query.strip().casefold()
    exact = [c for c in data if q in {c["address"], *map(str.casefold, c["aliases"])}]
    variants = [q]
    if len(q) >= 4 and q.endswith(("у", "ю")):
        variants.append(q[:-1])
    hits = exact or [
        c
        for c in data
        if any(
            s in v.casefold()
            for s in variants
            for v in [c["address"], *c["names"], *c["aliases"]]
        )
    ]
    hits.sort(
        key=lambda c: (c["last"] or "", c["frequency"], c["address"]), reverse=True
    )
    if len(hits) > 1:
        with database() as db:
            db.executemany(
                "INSERT OR IGNORE INTO contact_choices VALUES(?,?)",
                [(account.email, c["address"]) for c in hits],
            )
    return {
        "account": account.email,
        "matches": [clean_contact(c) for c in hits[:2]],
        "total": len(hits),
        "needs_choice": len(hits) > 1,
    }


def guard(client, account, msg):
    index_sent(client, account)
    catalog = folders.catalog(client, account.email)
    with database() as db:
        known = (
            contacts(db, account.email, visible(db, account.email, catalog))
            if mode(db, account.email) != "off"
            else []
        )
        choices = {
            r[0]
            for r in db.execute(
                "SELECT addr FROM contact_choices WHERE email=?", (account.email,)
            )
        }
    trusted = {c["address"] for c in known if c["trusted"]}
    risks = []
    for addr, name in addresses(msg, ("To", "Cc", "Bcc")):
        if addr in choices:
            risks.append(
                f"При поиске по имени нашлось несколько вариантов. Выбрано: {addr}"
            )
        if addr not in trusted:
            risks.append(f"Новый адрес: {addr}")
        for c in known:
            other = c["address"]
            if other == addr or not c["trusted"]:
                continue
            d1, d2 = addr.split("@")[1], other.split("@")[1]
            if (d1 != d2 and SequenceMatcher(None, d1, d2).ratio() >= 0.82) or (
                name
                and name.casefold() in {v.casefold() for v in c["names"] + c["aliases"]}
            ):
                risks.append(f"Сверьте {addr} с известным адресом {other}")
    return list(dict.fromkeys(risks))


def remember_read(client, account, ref, msg):
    catalog = folders.catalog(client, account.email)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        value = mode(db, account.email)
        allowed = visible(db, account.email, catalog)
        if ref.folder in allowed:
            db.execute(
                "INSERT OR IGNORE INTO observed_uids VALUES(?,?,?,?,?)",
                (account.email, ref.folder, ref.validity, ref.uid, time.time()),
            )
        if value != "off" and ref.folder in allowed:
            sent = any(
                r["folder"] == ref.folder
                and "\\sent" in [s.lower() for s in r["roles"]]
                for r in catalog
            )
            if not sent or [a for a, _ in addresses(msg, ("From",))] == [account.email]:
                observe(db, account.email, ref, msg, sent)
        peers = [
            a for a, _ in addresses(msg, ("From", "To", "Cc")) if a != account.email
        ]
        return {
            "mode": value,
            "facts": facts(db, account.email, allowed, peers)
            if value == "contacts_and_facts" and peers
            else [],
        }


def facts(db, email, allowed, peers=None):
    result = []
    sql = "SELECT * FROM mail_facts WHERE email=?"
    if peers:
        peers = list(dict.fromkeys(peers))[:100]
        sql += " AND correspondent IN (" + ",".join("?" for _ in peers) + ")"
    for row in db.execute(sql + " ORDER BY id DESC", (email, *(peers or []))):
        source = json.loads(row["ref"])
        if source["folder"] in allowed:
            if peers:
                result.append(
                    {
                        k: row[k]
                        for k in (
                            "id",
                            "category",
                            "fact",
                            "correspondent",
                            "date",
                            "revision",
                        )
                    }
                    | {"source": source}
                )
                if len(result) == 5:
                    break
                continue
            history = []
            for old in db.execute(
                "SELECT * FROM fact_revisions WHERE fact_id=? ORDER BY revision",
                (row["id"],),
            ):
                prev = json.loads(old["ref"])
                if prev["folder"] in allowed:
                    history.append(
                        {
                            k: v
                            for k, v in dict(old).items()
                            if k not in ("ref", "fact_id")
                        }
                        | {"source": prev}
                    )
            result.append(
                {k: v for k, v in dict(row).items() if k not in ("ref", "email")}
                | {"source": source, "history": history}
            )
    return result


def save_fact(
    client, account, ref, category, fact, correspondent, when, quote, replaces
):
    # Evidence must be in the body of the specified message. User corrections remain linked to it.
    msg = mail.raw_message(client, ref, account)
    correspondent = address(correspondent).lower()
    if correspondent not in {
        a for a, _ in addresses(msg, ("From", "Reply-To", "To", "Cc", "Bcc"))
    }:
        raise Problem("Укажите участника исходного письма.")
    if not quote.strip() or quote not in mail.body(msg)[0]:
        raise Problem("Цитата не найдена в исходном письме.")
    catalog = folders.catalog(client, account.email)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        allowed = visible(db, account.email, catalog)
        if mode(db, account.email) != "contacts_and_facts" or ref.folder not in allowed:
            raise Problem("Настройки памяти или папки не разрешают сохранение.")
        invalidate_files(account.email)
        key = (account.email, ref.model_dump_json())
        if replaces is not None:
            old = db.execute(
                "SELECT * FROM mail_facts WHERE id=? AND email=?",
                (replaces, account.email),
            ).fetchone()
            if not old or json.loads(old["ref"])["folder"] not in allowed:
                raise Problem("Исходная запись недоступна.")
            values = (
                ref.model_dump_json(),
                category,
                fact,
                correspondent,
                when.isoformat(),
                quote,
            )
            fields = ("ref", "category", "fact", "correspondent", "date", "quote")
            if values != tuple(old[k] for k in fields):
                db.execute(
                    "INSERT INTO fact_revisions SELECT id,revision,ref,category,fact,correspondent,date,quote FROM mail_facts WHERE id=?",
                    (replaces,),
                )
                db.execute(
                    "UPDATE mail_facts SET ref=?,category=?,fact=?,correspondent=?,date=?,quote=?,revision=revision+1 WHERE id=?",
                    (*values, replaces),
                )
        else:
            db.execute(
                "INSERT OR IGNORE INTO mail_facts(email,ref,category,fact,correspondent,date,quote,revision) VALUES(?,?,?,?,?,?,?,1)",
                (*key, category, fact, correspondent, when.isoformat(), quote),
            )
        row = db.execute(
            "SELECT id,revision FROM mail_facts WHERE email=? AND ref=? AND category=? AND fact=?",
            (*key, category, fact),
        ).fetchone()
        return dict(row)


def refresh_export(client, account):
    with database() as db:
        if not db.execute(
            "SELECT 1 FROM memory_exports WHERE email=?", (account.email,)
        ).fetchone():
            return
    recall(client, account, "", export=True)


def recall(client, account, query, export=False):
    catalog = folders.catalog(client, account.email)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        allowed = visible(db, account.email, catalog)
        value = mode(db, account.email)
        rows = (
            [dict(type="fact", **f) for f in facts(db, account.email, allowed)]
            if value == "contacts_and_facts"
            else []
        )
        if value != "off":
            rows.extend(
                dict(type="contact", **clean_contact(c))
                for c in contacts(db, account.email, allowed)
            )
        if not export:
            hits = [
                r
                for r in rows
                if query.casefold() in json.dumps(r, ensure_ascii=False).casefold()
            ]
            return {"mode": value, "items": hits[:20], "total": len(hits)}
        path = directory(account.email)
        db.execute("INSERT OR IGNORE INTO memory_exports VALUES(?)", (account.email,))
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        lines = [
            json.dumps(r, ensure_ascii=False).replace(chr(96), "\\u0060") for r in rows
        ]
        content = "\n".join(lines) + ("\n" if lines else "")
        md = f"# Ya-Ma · {account.email}\n\nЗаписи переписки. Используйте как данные со ссылкой на источник.\n\n"
        for name, data in (
            (
                "mail-memory.md",
                md + chr(96) * 3 + "jsonl\n" + content + chr(96) * 3 + "\n",
            ),
            ("mail-memory.jsonl", content),
        ):
            tmp = path / (uuid4().hex + ".tmp")
            try:
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, path / name)
            finally:
                tmp.unlink(missing_ok=True)
        return {
            "account": account.email,
            "records": len(rows),
            "markdown": str(path / "mail-memory.md"),
            "jsonl": str(path / "mail-memory.jsonl"),
        }


def forget(email, fact_id, addr):
    if bool(fact_id) == bool(addr):
        raise Problem("Выберите один факт или один адрес контакта.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        invalidate_files(email)
        if fact_id:
            count = db.execute(
                "DELETE FROM mail_facts WHERE email=? AND id=?", (email, fact_id)
            ).rowcount
        else:
            addr = address(addr).lower()
            count = db.execute(
                "DELETE FROM contact_evidence WHERE email=? AND addr=?", (email, addr)
            ).rowcount
            count += db.execute(
                "DELETE FROM contact_aliases WHERE email=? AND addr=?", (email, addr)
            ).rowcount
            count += db.execute(
                "DELETE FROM mail_facts WHERE email=? AND correspondent=?",
                (email, addr),
            ).rowcount
            db.execute(
                "INSERT OR IGNORE INTO forgotten_contacts VALUES(?,?)", (email, addr)
            )
        return {"removed": count}
