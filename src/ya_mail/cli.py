import argparse
import json

from . import lazy_cli, wizard
from .accounts import Problem, forget, profiles


def start_background():
    from .delivery import recover
    from .updates import start

    recover()
    start()
    lazy_cli.ensure_watch()


def main():
    cli = argparse.ArgumentParser(prog="ya-mail", description="Ya-Ma для Codex")
    actions = cli.add_subparsers(dest="action", required=True)
    lazy_cli.register(actions)
    updates = actions.add_parser("updates", help="Проверка опубликованной версии Ya-Ma")
    setting = updates.add_mutually_exclusive_group()
    for flag in ("on", "off", "check"):
        setting.add_argument(
            "--" + flag, dest="update_mode", action="store_const", const=flag
        )
    setup = actions.add_parser("connect", help="Мастер подключения")
    interface = setup.add_mutually_exclusive_group()
    interface.add_argument("--web", action="store_true", help="Мастер в браузере")
    setup.add_argument("--port", type=int, default=8776)
    interface.add_argument("--terminal", action="store_true", help="Текстовый мастер")
    actions.add_parser("status", help="Подключённые ящики и разрешения")
    check = actions.add_parser(
        "check", help="Проверить список папок и поиск по INBOX без вывода писем"
    )
    check.add_argument("account")
    actions.add_parser("serve", help="MCP через stdio")
    http = actions.add_parser("serve-http", help="Личный MCP за HTTPS-прокси")
    http.add_argument("--public-origin", required=True)
    http.add_argument("--port", type=int, default=8775)
    worker = actions.add_parser("deliver", help=argparse.SUPPRESS)
    worker.add_argument("job")
    read = actions.add_parser(
        "read", help="Экономичный разовый запуск Codex для чтения почты"
    )
    read.add_argument("--show-model", action="store_true")
    read.add_argument("prompt", nargs="?")
    actions.add_parser(
        "connect-360", help="Дополнительный вход в официальный MCP Яндекс 360"
    )
    actions.add_parser("disconnect-360", help="Отключить официальный MCP Яндекс 360")
    actions.add_parser("serve-360", help="Официальный MCP Яндекс 360 через stdio")
    logout = actions.add_parser(
        "disconnect", help="Удалить локальный доступ и позиции расписаний"
    )
    logout.add_argument("account")
    args = cli.parse_args()
    try:
        if args.action == "updates":
            from . import updates as releases

            value = (
                releases.configure(args.update_mode == "on")
                if args.update_mode in ("on", "off")
                else releases.check(force=args.update_mode == "check")
            )
            print(json.dumps(value, ensure_ascii=False, indent=2))
        elif args.action == "lazy":
            lazy_cli.dispatch(args)
        elif args.action in {"serve", "serve-http"}:
            if args.action == "serve-http":
                from .remote import serve

                if not 1 <= args.port <= 65535:
                    raise Problem("Укажите порт от 1 до 65535.")
                serve(args.public_origin, args.port)
            else:
                from .server import mcp

                start_background()
                mcp.run()
        elif args.action == "deliver":
            from .delivery import run

            run(args.job)
        elif args.action == "read":
            from .economy import launch

            raise SystemExit(launch(args.prompt, args.show_model))
        elif args.action in {"connect-360", "disconnect-360", "serve-360"}:
            from . import native

            if args.action == "connect-360":
                native.connect()
            elif args.action == "disconnect-360":
                native.disconnect()
            else:
                import asyncio

                asyncio.run(native.serve())
        elif args.action == "status":
            print(json.dumps(profiles(), ensure_ascii=False, indent=2))
        elif args.action == "check":
            from .accounts import current
            from .folders import allow
            from .transport import mailbox

            account = current(args.account)
            with mailbox(account) as client:
                allow(account.email, "INBOX", client)
                folders = client.list_folders()
                client.select_folder("INBOX", readonly=True)
                matches = client.search(["UNDELETED", "UNSEEN"])
                print(
                    json.dumps(
                        {
                            "imap": "OK",
                            "folder_count": len(folders),
                            "unread_count": len(matches),
                        }
                    )
                )
        elif args.action == "disconnect":
            forget(args.account)
            print(
                "Локальный доступ отключён. Для отзыва пароля удалите его на",
                wizard.PASSWORDS,
            )
        elif args.web:
            from .web_wizard import serve

            if not 1 <= args.port <= 65535:
                raise Problem("Укажите порт от 1 до 65535.")
            serve(args.port)
        elif args.terminal:
            wizard.terminal()
        else:
            try:
                import tkinter
            except ImportError:
                wizard.terminal()
            else:
                try:
                    wizard.window()
                except tkinter.TclError:
                    wizard.terminal()
    except Problem as exc:
        cli.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
