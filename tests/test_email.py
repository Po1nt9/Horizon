from email.mime.multipart import MIMEMultipart

from src.models import EmailConfig
from src.services.email import EmailManager


class FakeSMTP:
    instances = []

    def __init__(self, server, port):
        self.server = server
        self.port = port
        self.login_calls = []
        self.messages = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, username, password):
        self.login_calls.append((username, password))

    def send_message(self, message):
        self.messages.append(message)


class FakeIMAP:
    instances = []

    def __init__(self, server, port):
        FakeIMAP.instances.append((server, port))


def _email_config(**overrides):
    data = {
        "enabled": True,
        "smtp_server": "smtp.example.com",
        "smtp_port": 465,
        "imap_server": "imap.example.com",
        "imap_port": 993,
        "email_address": "noreply@example.com",
        "password_env": "EMAIL_PASSWORD",
    }
    data.update(overrides)
    return EmailConfig(**data)


def test_send_daily_summary_uses_smtp_username_when_configured(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    config = _email_config(smtp_username="resend")
    manager = EmailManager(config)

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    smtp = FakeSMTP.instances[0]
    assert smtp.login_calls == [("resend", "secret")]
    assert len(smtp.messages) == 1
    assert isinstance(smtp.messages[0], MIMEMultipart)
    assert smtp.messages[0]["From"] == "Horizon Daily <noreply@example.com>"
    assert smtp.messages[0]["To"] == "user@example.com"


def test_send_daily_summary_falls_back_to_email_address_for_smtp_login(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    config = _email_config()
    manager = EmailManager(config)

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    assert FakeSMTP.instances[0].login_calls == [("noreply@example.com", "secret")]


def test_send_daily_summary_escapes_raw_html(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())

    # 合法卡片结构，summary 内注入 XSS：渲染器必须把不可信文本 escape
    manager.send_daily_summary(
        "# Daily\n\n"
        '<a id="item-1"></a>\n'
        "## [Hello](https://example.com) ⭐️ 8.0/10\n\n"
        "Hello <img src=x onerror=alert(1)>\n\n"
        "source · author · 7月24日 10:00\n\n"
        "---\n",
        "Daily",
        ["user@example.com"],
    )

    html_part = FakeSMTP.instances[0].messages[0].get_payload()[1]
    html_body = html_part.get_payload(decode=True).decode()
    # 可执行的注入标签不得出现
    assert "<img src=x onerror=alert(1)>" not in html_body
    # 注入内容被 escape 为文本节点，不可执行
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_body


def test_send_daily_summary_cleans_app_generated_markdown_html(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    summary = """# Daily

<a id="item-1"></a>
## [Item](https://example.com/item) ⭐️ 8.0/10

Some summary text.

source · author · 7月24日 10:00

<details><summary>参考链接</summary>
<ul>
<li><a href="https://example.com/a">Example A</a></li>
<li><a href="https://example.com/b">Example B</a></li>
</ul>
</details>

---
"""

    manager.send_daily_summary(summary, "Daily", ["user@example.com"])

    message = FakeSMTP.instances[0].messages[0]
    text_body = message.get_payload()[0].get_payload(decode=True).decode()
    html_body = message.get_payload()[1].get_payload(decode=True).decode()

    # text part：clean 把 details 扁平成 markdown 列表，锚点删除
    assert '<a id="item-1"></a>' not in text_body
    assert "<details>" not in text_body
    assert "<summary>" not in text_body
    assert "**参考链接**" in text_body
    assert "- [Example A](https://example.com/a)" in text_body

    # html part：渲染器不输出原始 details/锚点，参考链接作为卡片 refs 渲染成 a 标签
    assert "<details>" not in html_body
    assert "<summary>" not in html_body
    assert '<a id="item-1"></a>' not in html_body
    assert 'href="https://example.com/a"' in html_body
    assert "Example A" in html_body
    assert 'href="https://example.com/b"' in html_body
    assert "Example B" in html_body


def test_send_daily_summary_does_not_link_unsafe_details_href(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    summary = """# Daily

<a id="item-1"></a>
## [Item](https://example.com/item) ⭐️ 8.0/10

Summary.

source · author · 7月24日 10:00

<details><summary>参考链接</summary>
<ul>
<li><a href="javascript:alert(1)">click [me](https://evil.example)</a></li>
<li><a href="https://safe.example">Safe Link</a></li>
</ul>
</details>

---
"""

    manager.send_daily_summary(summary, "Daily", ["user@example.com"])

    message = FakeSMTP.instances[0].messages[0]
    text_body = message.get_payload()[0].get_payload(decode=True).decode()
    html_body = message.get_payload()[1].get_payload(decode=True).decode()

    # text part：clean 把 javascript 链接降级为转义纯文本，不当链接保留
    assert "[click](javascript:alert(1))" not in text_body
    assert "- click \\[me\\]\\(https://evil.example\\)" in text_body

    # html part：恶意协议绝不以可点击 href 出现；安全链接正常渲染为卡片 refs
    assert 'href="javascript:alert(1)"' not in html_body
    assert "javascript:" not in html_body
    assert 'href="https://safe.example"' in html_body
    assert "Safe Link" in html_body


def test_check_subscriptions_skips_imap_when_disabled(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeIMAP.instances = []

    config = _email_config(imap_enabled=False)
    manager = EmailManager(config)

    manager.check_subscriptions(storage_manager=object())

    assert FakeIMAP.instances == []
