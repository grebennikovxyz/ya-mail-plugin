import argparse
import json

from . import wizard
from .accounts import Problem, forget, profiles


def main():
    cli = argparse.ArgumentParser(prog="ya-mail", description="Яндекс Почта для Codex")
    actions = cli.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("connect", help="Мастер подключения")
    setup.add_argument("--terminal", action="store_true", help="Текстовый мастер")
    actions.add_parser("status", help="Подключённые ящики и разрешения")
    check = actions.add_parser(
        "check", help="Проверить список папок и поиск по INBOX без вывода писем"
    )
    check.add_argument("account")
    actions.add_parser("serve", help="MCP через stdio")
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
        if args.action == "serve":
            from .server import mcp

            mcp.run()
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
            from .transport import mailbox

            with mailbox(current(args.account)) as client:
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
