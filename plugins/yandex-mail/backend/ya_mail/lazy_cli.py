"""User-owned onboarding and entrypoints for lazy cleanup."""

import asyncio
import json
import os
import subprocess
import sys
from itertools import batched
from uuid import UUID

from filelock import FileLock, Timeout

from . import accounts, lazy_codex, lazy_mail, lazy_store, lazy_worker
from .accounts import Problem
from .transport import mailbox


def register(actions):
    lazy = actions.add_parser("lazy", help="Ленивый разбор накопившейся почты")
    commands = lazy.add_subparsers(dest="operation", required=True)
    for name in ("setup", "preview", "digest", "run", "disable", "doctor"):
        p = commands.add_parser(name)
        p.add_argument("--account", help="Адрес подключённого ящика")
        if name == "preview":
            p.add_argument("--limit", type=int, default=8)
        if name == "run":
            p.add_argument("--max-messages", type=int, required=True)
            p.add_argument("--token-budget", type=int, required=True)
        if name == "digest":
            p.add_argument("--run", dest="run_id")
            p.add_argument("--offset", type=int, default=0)
            p.add_argument("--limit", type=int, default=20)
    p = commands.add_parser("watch")
    p.add_argument("--socket", help="Сокет рабочего Codex App Server")
    p.add_argument("--once", action="store_true", help="Проверить допуск один раз")


def yes(question):
    return input(question + " [д/Н] ").strip().casefold() in {"д", "да", "y", "yes"}


def choose(email, interactive):
    profiles = accounts.profiles()
    if email:
        return accounts.current(email).email
    if len(profiles) == 1:
        return profiles[0]["email"]
    if interactive and profiles:
        print("\n".join(p["email"] for p in profiles))
        return accounts.current(input("Адрес ящика: ")).email
    raise Problem("Укажите --account с адресом подключённого ящика.")


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def safe_label(value):
    # Mail headers are untrusted terminal input, including CSI and bidi controls.
    import unicodedata

    return "".join(
        " " if unicodedata.category(c).startswith("C") else c for c in str(value)
    )


def setup(email):
    acc = accounts.current(email, "organize")
    rules = lazy_store.policies().get(email, lazy_store.Rules())
    with mailbox(acc) as client:
        available = lazy_mail.permitted_sources(client, email, None)
        print("Доступные папки:", ", ".join(map(safe_label, available)))
        value = input("Папки через запятую, Enter — " + ", ".join(rules.folders) + ": ")
        if value.strip():
            rules.folders = list(dict.fromkeys(f.strip() for f in value.split(",")))
        if not set(rules.folders) <= set(available):
            raise Problem("Укажите только доступные папки.")
        rules.sent = yes(
            "Прочитать заголовки Отправленных для защиты личных цепочек и поиска давних контактов?"
        )
        print("Подсчитываю переписки по заголовкам…")
        old, examples = lazy_mail.discover(client, acc, rules)
    protected = set(rules.protected)
    print("С кем вы давно не общались")
    for page in batched(old, 20):
        for i, (peer, stats) in enumerate(page, 1):
            print(
                i,
                safe_label(peer),
                stats["last"],
                f"писем {stats['incoming'] + stats['outgoing']}, "
                f"цепочек {len(stats['threads'])}, дней {len(stats['days'])}",
            )
        numbers = input(
            "Номера людей для постоянной защиты через пробел, Enter — пропустить: "
        ).split()
        for n in numbers:
            if not n.isdecimal() or not 1 <= int(n) <= len(page):
                raise Problem("Выберите номера из показанного списка.")
            protected.add(page[int(n) - 1][0])
        if len(page) < 20 or not yes("Показать следующую страницу контактов?"):
            break
    extra = input(
        "Дополнительные адреса и псевдонимы для защиты через запятую (Enter — пропустить): "
    )
    protected.update(accounts.address(a).lower() for a in extra.split(",") if a.strip())
    if rules.protected:
        print("Ранее защищённые адреса:", ", ".join(rules.protected))
        remove = input(
            "Для снятия защиты введите точные адреса через запятую, Enter — сохранить все: "
        )
        removed = {
            accounts.address(a).lower() for a in remove.split(",") if a.strip()
        } & set(rules.protected)
        if removed and yes(
            "Снять персональную защиту с " + ", ".join(sorted(removed)) + "?"
        ):
            protected -= removed
    approved = set(rules.approved)
    review = set(rules.review)
    for i, (peer, (count, subject)) in enumerate(examples):
        if i and i % 10 == 0 and not yes("Показать ещё десять рассылок?"):
            break
        print(safe_label(peer), count, safe_label(subject))
        answer = input(
            "Реклама в список для подтверждения [1], на согласование [2], всегда сохранять [3], Enter — оставить: "
        ).strip()
        if answer not in {"1", "2", "3"}:
            continue
        if peer in protected and answer != "3":
            print("Сохранено правило защиты.")
            continue
        approved.discard(peer)
        review.discard(peer)
        {"1": approved, "2": review, "3": protected}[answer].add(peer)
    rules.protected = sorted(protected)
    rules.approved = sorted(approved - protected)
    rules.review = sorted(review - protected)
    weekly = input(
        f"Недельный предел в процентных пунктах за короткое окно [{rules.weekly:g}]: "
    ).strip()
    try:
        if weekly:
            rules.weekly = float(weekly.replace(",", "."))
        rules = lazy_store.Rules.model_validate(rules.model_dump())
    except ValueError:
        raise Problem(
            "Укажите недельный предел больше 0 и не выше 100 процентных пунктов."
        ) from None
    rules.anchor = ""
    if yes("Включить автоматический разбор в последние 60 минут окна Codex?"):
        rules.endpoint = input(
            "Адрес рабочего Codex (локальный ws://, путь сокета или Enter для стандартного): "
        ).strip()
        other = {
            r.endpoint
            for a, r in lazy_store.policies().items()
            if a != email and r.anchor
        }
        if other and other != {rules.endpoint}:
            raise Problem("Укажите тот же рабочий Codex, что и для остальных ящиков.")
        existing = os.environ.get("CODEX_THREAD_ID", "")
        anchor = (
            input(
                f"ID чата рабочего Codex для проверки подключения [{existing}]: "
            ).strip()
            or existing
        )
        try:
            rules.anchor = str(UUID(anchor))
        except ValueError:
            raise Problem(
                "Нужен UUID рабочего чата Codex. Для ручного режима откажитесь от автозапуска."
            ) from None
    print("Предварительные решения. Письма остаются в исходных папках.")
    model = None

    def preview_decision(item):
        nonlocal model

        async def classify():
            nonlocal model
            if model is None:
                async with lazy_codex.Proxy(model_catalog_only=True) as proxy:
                    model = await lazy_codex.model_name(proxy)
            return await lazy_codex.classify(model, [item])

        return asyncio.run(classify())

    print("Для одобренных рассылок очищенный текст передаётся классификатору Codex.")
    show(lazy_worker.preview(email, rules, classify=preview_decision))
    if yes("Сохранить эти правила и разрешить разбор на их основе?"):
        with lazy_store.policy_lock():
            lazy_store.save(email, rules)
        print("Правила сохранены:", lazy_store.skill_path(email))
        ensure_watch()


def ensure_watch():
    if not (accounts.DATA / "mail.sqlite3").is_file():
        return
    if not any(r.anchor for r in lazy_store.policies().values()):
        return
    probe = FileLock(accounts.DATA / "lazy-watch.lock")
    try:
        with probe.acquire(timeout=0):
            pass
    except Timeout:
        return
    # The child owns the lifetime lock. Concurrent host startups may spawn a second
    # contender; it exits before opening Codex or reading a mailbox.
    opts = (
        {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, "-I", "-B", "-m", "ya_mail.cli", "lazy", "watch"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **opts,
    )


async def manual(email, count, budget):
    rules = lazy_store.policies().get(email)
    if not rules or not (1 <= count <= 1000 and 512 <= budget <= 1000000):
        raise Problem(
            "Настройте ящик и задайте 1–1000 писем, бюджет 512–1000000 токенов."
        )
    model = None
    init = asyncio.Lock()

    async def classify(item):
        nonlocal model
        async with init:
            if model is None:
                async with lazy_codex.Proxy(model_catalog_only=True) as proxy:
                    model = await lazy_codex.model_name(proxy)
        return await lazy_codex.classify(model, item)

    return await lazy_worker.run(
        {email: rules}, classify, max_messages=count, token_budget=budget
    )


async def doctor(email):
    rules = lazy_store.policies().get(email)
    if not rules or not rules.anchor:
        return {
            "automatic": False,
            "reason": "Автоматический режим не настроен для этого ящика",
        }
    try:
        async with lazy_codex.Proxy(rules.endpoint) as client:
            loaded = await client.pages("thread/loaded/list")
            data = await client.rpc(
                "account/rateLimits/read", {"excludeResetCreditDetails": True}
            )
            quota = (
                (data.get("rateLimitsByLimitId") or {}).get("codex")
                or data.get("rateLimits")
                or {}
            )
            return {
                "anchor_visible": rules.anchor in loaded,
                "attributed": bool(data.get("accountId")),
                "windows": [
                    {
                        k: v.get(k)
                        for k in ("windowDurationMins", "usedPercent", "resetsAt")
                    }
                    for v in [quota.get("primary"), quota.get("secondary")]
                    if v
                ],
            }
    except (OSError, Problem, TimeoutError):
        return {"automatic": False, "reason": "Рабочий App Server недоступен"}


def dispatch(args):
    if args.operation == "watch":
        accounts.DATA.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(accounts.DATA / "lazy-watch.lock", timeout=0):
                asyncio.run(lazy_worker.watch(args.socket, once=args.once))
        except Timeout:
            print("Наблюдатель уже запущен.")
        return
    email = choose(args.account, args.operation == "setup")
    if args.operation == "setup":
        setup(email)
    elif args.operation == "preview":
        if not 1 <= args.limit <= 100:
            raise Problem("Размер просмотра от 1 до 100.")
        show(
            lazy_worker.preview(
                email, lazy_store.policies().get(email, lazy_store.Rules()), args.limit
            )
        )
    elif args.operation == "digest":
        show(lazy_worker.digest(email, args.run_id, args.offset, args.limit))
    elif args.operation == "doctor":
        show(asyncio.run(doctor(email)))
    elif args.operation == "run":
        show(asyncio.run(manual(email, args.max_messages, args.token_budget)))
    elif args.operation == "disable":
        with lazy_store.policy_lock(), lazy_store.db() as con:
            con.execute("DELETE FROM lazy_rules WHERE account=?", (email,))
        lazy_store.skill_path(email).unlink(missing_ok=True)
        print("Ленивый разбор отключён для", email)
