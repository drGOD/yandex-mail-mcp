"""
Unit tests for SMTP send. No network and no real mail.

Covers MCP SDK 2 startup, envelope parsing, port selection, the Sent
APPEND success path, and app-password space stripping.
"""

import asyncio
from contextlib import contextmanager
from email.utils import formataddr

import pytest
import smtplib

import yandex_mail_mcp as mail
from mcp.server.mcpserver import MCPServer


def test_server_imports_and_starts_on_mcp_2():
    """Importing the module must succeed on mcp 2.x and register send_email."""
    assert isinstance(mail.mcp, MCPServer)
    assert mail.mcp.name == "Yandex Mail"
    assert mail.mcp.version == mail.VERSION

    tools = asyncio.run(mail.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "send_email" in names
    assert "reply_email" in names
    assert "forward_email" in names


def test_normalize_app_password_strips_spaces_and_keeps_none():
    assert mail._normalize_app_password(None) is None
    assert mail._normalize_app_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"
    assert mail._normalize_app_password("nospaces") == "nospaces"


def test_smtp_login_strips_spaces_from_app_password(monkeypatch):
    logins = []

    class FakeSSL:
        def __init__(self, host, port):
            assert (host, port) == (mail.SMTP_SERVER, mail.SMTP_SSL_PORT)

        def login(self, user, password):
            logins.append((user, password))

        def quit(self):
            pass

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(mail, "EMAIL", "me@yandex.ru")
    monkeypatch.setattr(mail, "PASSWORD", "abcd efgh ijkl mnop")

    with mail.smtp_connection():
        pass

    assert logins == [("me@yandex.ru", "abcdefghijklmnop")]


def test_smtp_prefers_ssl_port_465(monkeypatch):
    calls = []

    class FakeSSL:
        def __init__(self, host, port):
            calls.append(("ssl", host, port))

        def login(self, user, password):
            calls.append(("login", user, password))

        def quit(self):
            calls.append(("quit",))

    class FakeSMTP:
        def __init__(self, host, port):
            calls.append(("plain", host, port))

        def starttls(self):
            calls.append(("starttls",))

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail, "EMAIL", "me@yandex.ru")
    monkeypatch.setattr(mail, "PASSWORD", "secret")

    with mail.smtp_connection():
        pass

    assert calls[0] == ("ssl", "smtp.yandex.com", 465)
    assert ("plain", "smtp.yandex.com", 587) not in calls
    assert ("starttls",) not in calls
    assert ("login", "me@yandex.ru", "secret") in calls


def test_smtp_falls_back_to_starttls_on_587(monkeypatch):
    calls = []

    class FakeSSL:
        def __init__(self, host, port):
            calls.append(("ssl", host, port))
            raise OSError("465 unreachable")

    class FakeSMTP:
        def __init__(self, host, port):
            calls.append(("plain", host, port))

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def quit(self):
            pass

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(mail, "EMAIL", "me@yandex.ru")
    monkeypatch.setattr(mail, "PASSWORD", "secret")

    with mail.smtp_connection():
        pass

    assert ("ssl", "smtp.yandex.com", 465) in calls
    assert ("plain", "smtp.yandex.com", 587) in calls
    assert ("starttls",) in calls
    assert ("login", "me@yandex.ru", "secret") in calls


def test_auth_failure_does_not_switch_ports(monkeypatch):
    plain_calls = []

    class FakeSSL:
        def __init__(self, host, port):
            assert port == 465

        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad")

        def quit(self):
            pass

    class FakeSMTP:
        def __init__(self, host, port):
            plain_calls.append(port)

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail, "EMAIL", "me@yandex.ru")
    monkeypatch.setattr(mail, "PASSWORD", "secret")

    with pytest.raises(smtplib.SMTPAuthenticationError):
        with mail.smtp_connection():
            pass

    assert plain_calls == []


class _SentIMAP:
    def __init__(self):
        self.appends = []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def append(self, mailbox, flags, date_time, message):
        self.appends.append((mailbox, flags, message))
        return "OK", [b"APPENDUID 1 1"]

    def logout(self):
        return "OK", [b"bye"]


def _patch_transport(monkeypatch, imap, fail_send=False):
    sent = {}

    class FakeSMTP:
        def send_message(self, msg, from_addr, to_addrs):
            if fail_send:
                raise smtplib.SMTPDataError(451, b"try again")
            sent["msg"] = msg
            sent["from"] = from_addr
            sent["to"] = list(to_addrs)

    @contextmanager
    def fake_smtp():
        yield FakeSMTP()

    @contextmanager
    def fake_imap():
        yield imap

    monkeypatch.setattr(mail, "smtp_connection", fake_smtp)
    monkeypatch.setattr(mail, "imap_connection", fake_imap)
    monkeypatch.setattr(mail, "EMAIL", "me@yandex.ru")
    monkeypatch.setattr(mail, "PASSWORD", "secret")
    return sent


def test_envelope_cyrillic_display_name_and_comma_in_name(monkeypatch):
    imap = _SentIMAP()
    sent = _patch_transport(monkeypatch, imap)
    to = 'Иван <ivan@example.com>, "Иванов, Иван" <a@b.c>'

    result = mail.send_email(
        to=to,
        subject="Привет",
        body="текст",
        cc="Пётр <petr@example.com>",
        bcc="",
    )

    assert sent["to"] == ["ivan@example.com", "a@b.c", "petr@example.com"]
    for addr in sent["to"]:
        addr.encode("ascii")
    assert sent["msg"]["To"] == ", ".join(
        [
            formataddr(("Иван", "ivan@example.com")),
            formataddr(("Иванов, Иван", "a@b.c")),
        ]
    )
    assert sent["msg"]["Cc"] == formataddr(("Пётр", "petr@example.com"))
    assert sent["msg"]["Date"]
    assert sent["msg"]["Message-ID"]
    assert result["status"] == "sent"
    assert result["saved_to_sent"] == "Sent"
    assert len(imap.appends) == 1
    mailbox, flags, payload = imap.appends[0]
    assert mailbox == "Sent"
    assert flags == "\\Seen"
    assert b"Message-ID" in payload


def test_crlf_in_headers_is_rejected(monkeypatch):
    imap = _SentIMAP()
    sent = _patch_transport(monkeypatch, imap)

    with pytest.raises(ValueError):
        mail.send_email(
            to="ivan@example.com",
            subject="Hello\r\nBcc: evil@example.com",
            body="text",
        )

    assert sent == {}
    assert imap.appends == []


def test_reply_envelope_uses_bare_address_not_display_name(monkeypatch):
    encoded_from = formataddr(("Иванов, Иван", "a@b.c"))
    raw = (
        f"Reply-To: {encoded_from}\r\n"
        f"From: {encoded_from}\r\n"
        "Subject: Hi\r\n"
        "Message-ID: <orig@example.com>\r\n"
        "To: me@yandex.ru\r\n"
        "\r\n"
    ).encode("ascii")

    class FetchIMAP(_SentIMAP):
        def select(self, folder, readonly=False):
            return "OK", [b"1"]

        def uid(self, cmd, *args):
            if cmd == "FETCH":
                return "OK", [(b"1 (BODY[HEADER])", raw)]
            if cmd == "STORE":
                return "OK", [b"ok"]
            raise AssertionError(cmd)

    imap = FetchIMAP()
    sent = _patch_transport(monkeypatch, imap)

    result = mail.reply_email("INBOX", "10", body="reply body")

    assert sent["to"] == ["a@b.c"]
    assert sent["from"] == "me@yandex.ru"
    assert sent["msg"]["To"] == encoded_from
    assert sent["msg"]["In-Reply-To"] == "<orig@example.com>"
    assert sent["msg"]["Date"]
    assert sent["msg"]["Message-ID"]
    assert result["saved_to_sent"] == "Sent"
    assert len(imap.appends) == 1


def test_sent_append_runs_after_smtp_success(monkeypatch):
    imap = _SentIMAP()
    sent = _patch_transport(monkeypatch, imap)

    result = mail.send_email(
        to="ivan@example.com",
        subject="copy",
        body="hello",
    )

    assert sent["to"] == ["ivan@example.com"]
    assert result["saved_to_sent"] == "Sent"
    assert len(imap.appends) == 1
    assert imap.appends[0][0] == "Sent"


def test_sent_append_does_not_run_after_smtp_failure(monkeypatch):
    imap = _SentIMAP()
    _patch_transport(monkeypatch, imap, fail_send=True)

    with pytest.raises(smtplib.SMTPDataError):
        mail.send_email(
            to="ivan@example.com",
            subject="copy",
            body="hello",
        )

    assert imap.appends == []
