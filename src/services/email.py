"""Email service for handling subscriptions and sending summaries."""

import email
import html
import imaplib
import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import List

from ..ai.markdown_utils import clean_app_summary_markdown
from ..models import EmailConfig

logger = logging.getLogger(__name__)


# ============================================================================
# 邮件卡片渲染：把清洗后的 summary Markdown 解析为结构化条目，渲染成
# 极简报刊风 HTML 邮件。全部样式内联、table 布局，兼容 QQ/Gmail/Outlook。
# ============================================================================

_TITLE_RE = re.compile(r"^##\s+\[(.+?)\]\((.+?)\)\s*⭐️?\s*([\d.]+)/10")
_TAG_RE = re.compile(r"`([^`]+)`")
_REF_RE = re.compile(r"^-\s+\[(.+?)\]\((.+?)\)\s*$")
_SRC_RE = re.compile(r"[a-z]+ · .*\d+月|\d{4}-\d{2}-\d{2}")
_FIELD_RE = re.compile(r"^\*\*(背景|参考链接|社区讨论|标签)\*\*:?\s*(.*)$")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TOTAL_RE = re.compile(r"从 (\d+) 条")


def _esc(value) -> str:
    """HTML 转义，用于把不可信文本嵌入邮件模板。"""
    return html.escape(str(value), quote=True)


def _parse_email_items(cleaned_md: str) -> List[dict]:
    """把 clean_app_summary_markdown 处理后的 Markdown 拆成结构化条目。

    每条含 idx/title/url/score/summary/source/background/discussion/tags/refs/discuss_link。
    参考链接在 clean 后是「**参考链接** 标题段 + 空行 + 列表段」两段结构，需跨段读取。
    """
    blocks = re.split(r"(?m)^(?=## \[)", cleaned_md)
    items: List[dict] = []
    idx = 0
    for block in blocks:
        block = block.strip()
        if not block.startswith("## ["):
            continue
        lines = block.split("\n")
        m = _TITLE_RE.match(lines[0])
        if not m:
            continue
        idx += 1
        title, url, score = m.group(1), m.group(2), float(m.group(3))
        rest = "\n".join(lines[1:]).strip()
        paras = [p.strip() for p in re.split(r"\n\s*\n", rest) if p.strip()]
        summary = paras[0] if paras else ""
        source = background = discussion = ""
        tags: List[str] = []
        refs: List[dict] = []
        discuss_link = ""
        i = 1
        while i < len(paras):
            p = paras[i]
            first = p.split("\n", 1)[0].strip()
            fm = _FIELD_RE.match(first)
            if fm:
                field, val = fm.group(1), fm.group(2).strip()
                if field == "背景":
                    background = val
                elif field == "社区讨论":
                    discussion = val
                elif field == "标签":
                    tags = _TAG_RE.findall(p)
                elif field == "参考链接":
                    # 列表与标题同段，或被空行隔到下一段。
                    # 下一段可能以转义文本列表项开头（不安全链接被 clean 降级），
                    # 故用 "- " 而非 "- [" 判断，遍历各行时 _REF_RE 只取真链接。
                    list_paras = [p] if val else []
                    if not val and i + 1 < len(paras) and paras[i + 1].startswith("- "):
                        list_paras.append(paras[i + 1])
                        i += 1
                    for lp in list_paras:
                        for rl in lp.split("\n"):
                            rm = _REF_RE.match(rl.strip())
                            if rm:
                                refs.append({"title": rm.group(1), "url": rm.group(2)})
                i += 1
                continue
            if not first.startswith("**") and _SRC_RE.search(first) and "·" in first:
                source = first
                dlm = re.search(r"\[社区讨论\]\(([^)]+)\)", source)
                if dlm:
                    discuss_link = dlm.group(1)
                    source = re.sub(r"\s*·\s*\[社区讨论\]\([^)]+\)", "", source).strip()
            i += 1
        items.append(
            {
                "idx": idx,
                "title": title,
                "url": url,
                "score": score,
                "summary": summary,
                "source": source,
                "background": background,
                "discussion": discussion,
                "tags": tags,
                "refs": refs,
                "discuss_link": discuss_link,
            }
        )
    return items


def _render_email_card(item: dict, is_first: bool) -> str:
    """渲染单条新闻卡片 HTML（单栏纵向流，元信息行 + 满宽标题 + 各区块）。"""
    sc = "#8B1A1A" if item["score"] >= 9 else "#0F3D5C"
    sb = "#FAEDED" if item["score"] >= 9 else "#EDF3F8"
    sn = str(item["score"]).replace(".", "·")
    top = "" if is_first else "border-top:1px solid #E5E5DD;"

    meta_left = (
        f'<span style="font-family:\'Source Han Serif\',Georgia,serif;font-size:13px;'
        f'color:#9A9A92;margin-right:12px;">{item["idx"]:02d}</span>'
    )
    meta_left += "".join(
        f'<span style="font-family:\'SF Mono\',\'Source Han Sans\',sans-serif;font-size:10px;'
        f'letter-spacing:0.1em;color:{sc};margin-right:8px;">{_esc(t)}</span>'
        for t in item["tags"][:2]
    )
    meta_right = (
        f'<span style="display:inline-block;background:{sb};border:1px solid {sc};'
        f'padding:4px 8px;font-family:\'Source Han Serif\',Georgia,serif;font-size:14px;'
        f'font-weight:700;color:{sc};line-height:1;">{sn}'
        f'<span style="font-size:10px;font-weight:400;opacity:0.6;">/10</span></span>'
    )

    src_parts = [s.strip() for s in item["source"].split("·") if s.strip()]
    src_html = '<span style="color:#C9C9C0;margin:0 8px;">·</span>'.join(
        f"<span>{_esc(s)}</span>" for s in src_parts
    )
    discuss_html = (
        f'<span style="color:#C9C9C0;margin:0 8px;">·</span>'
        f'<a href="{_esc(item["discuss_link"])}" style="color:{sc};text-decoration:none;">社区讨论 →</a>'
        if item["discuss_link"]
        else ""
    )

    bg_html = (
        f'<div style="margin:16px 0 0 0;padding-left:14px;border-left:2px solid #D8D8CE;">'
        f'<div style="font-family:\'SF Mono\',monospace;font-size:9px;letter-spacing:0.15em;'
        f'color:#9A9A92;margin-bottom:6px;text-transform:uppercase;">背景</div>'
        f'<p style="margin:0;font-family:\'Source Han Sans\',\'Noto Sans SC\',sans-serif;'
        f'font-size:13px;line-height:1.75;color:#6A6A6A;">{_esc(item["background"])}</p></div>'
        if item["background"]
        else ""
    )
    disc_html = (
        f'<div style="margin:14px 0 0 0;padding-left:14px;border-left:2px solid #D8D8CE;">'
        f'<div style="font-family:\'SF Mono\',monospace;font-size:9px;letter-spacing:0.15em;'
        f'color:#9A9A92;margin-bottom:6px;text-transform:uppercase;">社区讨论</div>'
        f'<p style="margin:0;font-family:\'Source Han Sans\',\'Noto Sans SC\',sans-serif;'
        f'font-size:13px;line-height:1.75;color:#6A6A6A;font-style:italic;">{_esc(item["discussion"])}</p></div>'
        if item["discussion"]
        else ""
    )
    refs_html = ""
    if item["refs"]:
        rows = "".join(
            f'<div style="font-family:\'Source Han Sans\',\'Noto Sans SC\',sans-serif;'
            f'font-size:12.5px;line-height:1.6;margin-bottom:4px;">'
            f'<span style="color:#B8B8AE;font-family:\'SF Mono\',monospace;font-size:10px;'
            f'margin-right:6px;">{j + 1:02d}</span>'
            f'<a href="{_esc(r["url"])}" style="color:{sc};text-decoration:none;'
            f'border-bottom:1px dotted {sc};">{_esc(r["title"])}</a></div>'
            for j, r in enumerate(item["refs"])
        )
        refs_html = (
            f'<div style="margin:14px 0 0 0;">'
            f'<div style="font-family:\'SF Mono\',monospace;font-size:9px;letter-spacing:0.15em;'
            f'color:#9A9A92;margin-bottom:8px;text-transform:uppercase;">参考链接</div>{rows}</div>'
        )

    return (
        f'<tr><td style="padding:28px 0 0 0;{top}">\n'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        f'<tr><td style="vertical-align:top;">{meta_left}</td>'
        f'<td align="right" style="vertical-align:top;">{meta_right}</td></tr></table>\n'
        f'<div style="margin-top:10px;"><a href="{_esc(item["url"])}" '
        f'style="font-family:\'Source Han Serif\',\'Noto Serif SC\',Georgia,serif;font-size:21px;'
        f'line-height:1.4;color:#1A1A1A;text-decoration:none;font-weight:600;'
        f'letter-spacing:-0.005em;display:block;">{_esc(item["title"])}</a></div>\n'
        f'<p style="margin:14px 0 0 0;font-family:\'Source Han Sans\',\'Noto Sans SC\','
        f'-apple-system,sans-serif;font-size:15px;line-height:1.85;color:#2A2A2A;'
        f'letter-spacing:0.01em;">{_esc(item["summary"])}</p>\n'
        f"{bg_html}{disc_html}{refs_html}\n"
        f'<div style="font-family:\'SF Mono\',\'Source Han Sans\',monospace;font-size:11px;'
        f'color:#9A9A92;margin-top:16px;letter-spacing:0.02em;">{src_html}{discuss_html}</div>\n'
        f"</td></tr>"
    )


def _render_email_html(
    cleaned_md: str, date_str: str, total_fetched: int, sender_name: str, unsub_keyword: str
) -> str:
    """组装整封邮件 HTML：报头 + 导语 + 卡片列表 + 页脚。"""
    items = _parse_email_items(cleaned_md)
    cards = "\n".join(_render_email_card(p, i == 0) for i, p in enumerate(items))
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Horizon Daily · {date_str}</title></head>\n"
        '<body style="margin:0;padding:0;background:#EAEAE2;-webkit-text-size-adjust:100%;">\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="background:#EAEAE2;"><tr><td align="center" style="padding:32px 16px;">\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" '
        'style="max-width:640px;background:#FAFAF7;">\n'
        '<tr><td style="padding:52px 52px 0 52px;text-align:center;border-top:3px solid #1A1A1A;">\n'
        '<div style="font-family:\'SF Mono\',monospace;font-size:10px;letter-spacing:0.4em;'
        'color:#7A7A7A;text-transform:uppercase;">每日精选</div>\n'
        '<div style="font-family:\'Source Han Serif\',\'Noto Serif SC\',Georgia,\'Times New Roman\','
        'serif;font-size:56px;font-weight:700;color:#1A1A1A;letter-spacing:-0.03em;line-height:1;'
        'margin:18px 0 10px 0;">Horizon</div>\n'
        '<div style="font-family:\'Source Han Serif\',Georgia,serif;font-size:13px;font-style:italic;'
        'color:#5A5A5A;">一份来自 AI 雷达的每日情报简报</div>\n'
        '<div style="display:inline-block;width:40px;height:2px;background:#1A1A1A;margin:26px 0 0 0;"></div>\n'
        '<div style="font-family:\'SF Mono\',monospace;font-size:11px;color:#5A5A5A;margin-top:16px;'
        f'letter-spacing:0.12em;">{date_str}</div>\n'
        "</td></tr>\n"
        '<tr><td style="padding:36px 52px 8px 52px;">\n'
        '<p style="margin:0;font-family:\'Source Han Serif\',Georgia,serif;font-size:17px;'
        'line-height:1.75;color:#2A2A2A;font-style:italic;letter-spacing:0.005em;'
        'border-left:2px solid #0F3D5C;padding-left:20px;">本期从 '
        f'<strong style="font-style:normal;color:#1A1A1A;">{total_fetched}</strong> 条内容中精选 '
        f'<strong style="font-style:normal;color:#8B1A1A;">{len(items)}</strong> 条重要资讯。</p>\n'
        "</td></tr>\n"
        '<tr><td style="padding:0 52px;"><table role="presentation" cellpadding="0" '
        f'cellspacing="0" border="0" width="100%">\n{cards}\n</table></td></tr>\n'
        '<tr><td style="padding:48px 52px 0 52px;border-top:1px solid #E5E5DD;">\n'
        '<div style="text-align:center;"><div style="display:inline-block;width:32px;height:1px;'
        'background:#1A1A1A;"></div>\n'
        '<div style="font-family:\'Source Han Serif\',Georgia,serif;font-size:14px;color:#5A5A5A;'
        'margin-top:16px;font-style:italic;">下期见。</div></div>\n'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="margin-top:32px;"><tr>\n'
        '<td style="font-family:\'SF Mono\',monospace;font-size:10px;color:#9A9A92;'
        f'letter-spacing:0.05em;line-height:1.6;">{_esc(sender_name)} · 由 Agnes AI 评分筛选<br>'
        f'回复 "{_esc(unsub_keyword)}" 退订</td>\n'
        "</tr></table>\n"
        "</td></tr>\n"
        '<tr><td style="padding:0 52px 40px 52px;"></td></tr>\n'
        "</table></td></tr></table></body></html>"
    )


class EmailManager:
    """Manages email subscriptions and sending summaries."""

    def __init__(self, config: EmailConfig, console=None):
        self.config = config
        self.pwd = os.getenv(self.config.password_env)
        if console is None:
            try:
                from rich.console import Console

                self.console = Console()
            except ImportError:

                class DummyConsole:
                    def print(self, *args, **kwargs):
                        print(*args, **kwargs)

                self.console = DummyConsole()
        else:
            self.console = console

        if not self.pwd and self.config.enabled:
            logger.warning(
                f"Environment variable {self.config.password_env} not set. Email features may fail."
            )
            self.console.print(
                f"[yellow]Warning: Environment variable {self.config.password_env} not set. Email features may fail.[/yellow]"
            )

    def check_subscriptions(self, storage_manager):
        """Checks inbox for subscription requests and updates subscriber list."""
        if not self.config.enabled or not self.config.imap_enabled:
            return

        try:
            mail = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
            mail.login(self.config.email_address, self.pwd)
            mail.select("INBOX")

            keyword = self.config.subscribe_keyword
            search_crit = f'(UNSEEN SUBJECT "{keyword}")'

            status, messages = mail.search(None, search_crit)

            if status == "OK" and messages[0]:
                email_ids = messages[0].split()
                subscribers = storage_manager.load_subscribers()

                for e_id in email_ids:
                    _, msg_data = mail.fetch(e_id, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])

                            subject = str(msg.get("Subject") or "").strip()
                            if subject.upper() != keyword.upper():
                                continue

                            sender = msg.get("From")

                            if sender:
                                _, email_addr = parseaddr(sender)
                                if email_addr and "@" in email_addr:
                                    if (
                                        "noreply" in email_addr.lower()
                                        or "no-reply" in email_addr.lower()
                                    ):
                                        continue

                                    if email_addr not in subscribers:
                                        storage_manager.add_subscriber(email_addr)
                                        subscribers = storage_manager.load_subscribers()
                                        self._send_reply(
                                            email_addr,
                                            "Subscribed to Horizon",
                                            "You have been successfully subscribed to Horizon daily summaries.",
                                        )
                                        logger.info(f"Added subscriber: {email_addr}")
                                    else:
                                        logger.info(f"Already subscribed: {email_addr}")

            unsub_keyword = self.config.unsubscribe_keyword
            search_crit_unsub = f'(UNSEEN SUBJECT "{unsub_keyword}")'

            status, messages = mail.search(None, search_crit_unsub)

            if status == "OK" and messages[0]:
                email_ids = messages[0].split()
                subscribers = storage_manager.load_subscribers()

                for e_id in email_ids:
                    _, msg_data = mail.fetch(e_id, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])

                            subject = str(msg.get("Subject") or "").strip()
                            if subject.upper() != unsub_keyword.upper():
                                continue

                            sender = msg.get("From")

                            if sender:
                                _, email_addr = parseaddr(sender)
                                if email_addr and "@" in email_addr:
                                    if (
                                        "noreply" in email_addr.lower()
                                        or "no-reply" in email_addr.lower()
                                    ):
                                        continue

                                    if email_addr in subscribers:
                                        storage_manager.remove_subscriber(email_addr)
                                        subscribers = storage_manager.load_subscribers()
                                        self._send_reply(
                                            email_addr,
                                            "Unsubscribed from Horizon",
                                            "You have been successfully unsubscribed from Horizon daily summaries.",
                                        )
                                        logger.info(f"Removed subscriber: {email_addr}")
                                    else:
                                        logger.info(f"Not subscribed: {email_addr}")

            mail.close()
            mail.logout()

        except Exception as e:
            logger.error(f"Error checking subscriptions: {e}")

    def send_daily_summary(self, summary_md: str, subject: str, subscribers: List[str]):
        """Sends the daily summary to all subscribers."""
        if not self.config.enabled or not subscribers:
            return

        cleaned_summary = clean_app_summary_markdown(summary_md)

        # 从清洗后的 Markdown 提取报头所需的日期与抓取总数
        date_match = _DATE_RE.search(summary_md)
        date_str = date_match.group(1) if date_match else ""
        total_match = _TOTAL_RE.search(cleaned_summary)
        total_fetched = int(total_match.group(1)) if total_match else 0

        html_body = _render_email_html(
            cleaned_summary,
            date_str,
            total_fetched,
            self.config.sender_name,
            self.config.unsubscribe_keyword,
        )

        try:
            with smtplib.SMTP_SSL(
                self.config.smtp_server, self.config.smtp_port
            ) as server:
                server.login(
                    self.config.smtp_username or self.config.email_address, self.pwd
                )

                for subscriber in subscribers:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = (
                        f"{self.config.sender_name} <{self.config.email_address}>"
                    )
                    msg["To"] = subscriber

                    text_part = MIMEText(cleaned_summary, "plain")
                    html_part = MIMEText(html_body, "html")

                    msg.attach(text_part)
                    msg.attach(html_part)

                    try:
                        server.send_message(msg)
                        logger.info(f"Sent summary to {subscriber}")
                    except Exception as e:
                        logger.error(f"Failed to send to {subscriber}: {e}")

        except Exception as e:
            logger.error(f"SMTP Error: {e}")

    def _send_reply(self, to_email: str, subject: str, body: str):
        """Helper to send a simple reply."""
        try:
            with smtplib.SMTP_SSL(
                self.config.smtp_server, self.config.smtp_port
            ) as server:
                server.login(
                    self.config.smtp_username or self.config.email_address, self.pwd
                )

                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"] = f"{self.config.sender_name} <{self.config.email_address}>"
                msg["To"] = to_email

                server.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send reply to {to_email}: {e}")
