import imaplib
import smtplib
import ssl
from contextlib import contextmanager

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from .accounts import MODES, Account, Problem, address, save


@contextmanager
def mailbox(account):
    try:
        with IMAPClient(
            "imap.yandex.ru",
            port=993,
            ssl=True,
            ssl_context=ssl.create_default_context(),
            timeout=25,
        ) as client:
            client.login(account.login, account.password)
            yield client
    except LoginError:
        raise Problem(
            "Яндекс отклонил вход. Сверьте адрес ящика, пароль приложения типа «Почта» и разрешение на вход по IMAP в настройках Почты."
        ) from None
    except imaplib.IMAP4.error:
        raise Problem(
            "Яндекс отклонил команду IMAP. Проверьте доступ к ящику, папку и поддержку операции."
        ) from None
    except OSError:
        raise Problem(
            "Соединение IMAP прервано. Проверьте сеть и состояние письма перед повтором."
        ) from None


@contextmanager
def outgoing(account):
    try:
        with smtplib.SMTP_SSL(
            "smtp.yandex.ru", 465, timeout=25, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(account.login, account.password)
            yield smtp
    except (OSError, smtplib.SMTPException):
        raise Problem(
            "Ошибка SMTP. Проверьте доступ и состояние отправки в Почте."
        ) from None


def connect(email, password, mode):
    if mode not in MODES:
        raise Problem("Выберите режим доступа.")
    email = address(email).lower()
    password = password.strip().replace(" ", "")
    if (
        not password
        or not password.isascii()
        or not password.isalnum()
        or len(password) > 128
    ):
        raise Problem("Скопируйте пароль приложения типа «Почта» из Яндекс ID.")
    account = Account(email, mode, password)
    with mailbox(account) as client:
        client.select_folder("INBOX", readonly=True)
    if mode == "send":
        with outgoing(account):
            pass
    save(account)
