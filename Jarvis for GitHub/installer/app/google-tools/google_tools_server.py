"""Jarvis Gmail + Google Calendar tools (read-only), as a minimal MCP server over stdio.

The "simple" setup the user chose on 2026-11-07, without a Google Cloud app:
  - Gmail through IMAP with a Google "app password" (imaplib; mailboxes are opened
    read-only and messages fetched with BODY.PEEK, so nothing is marked as read)
  - Calendar through the calendar's "secret address in iCal format" (read-only feed)

Credentials: GOOGLE-ACCOUNT.txt in the Jarvis folder, filled in by the user
(never pasted into chat). It's read on every call, so no restart is needed after filling it.

Tools: check_email, read_email, calendar_events. Runs in google-tools\\.venv
(icalendar, recurring-ical-events, tzdata). Speaks JSON-RPC 2.0 on stdin/stdout.
"""

from __future__ import annotations

import email
import html
import imaplib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from email import policy
from pathlib import Path
from zoneinfo import ZoneInfo

PROTOCOL = "2025-06-18"
# The Jarvis folder (this file is in its google-tools\ subfolder), so it works wherever
# Jarvis is installed (also from the setup kit for other PCs).
ROOT = Path(__file__).resolve().parent.parent
CRED_FILE = ROOT / "GOOGLE-ACCOUNT.txt"


def _local_tz():
    """This PC's time zone: start-jarvis.ps1 passes its IANA name; else the current UTC offset."""
    try:
        return ZoneInfo(os.environ.get("OPENJARVIS_TIMEZONE") or "")
    except Exception:  # noqa: BLE001  (unset or unknown name)
        return datetime.now().astimezone().tzinfo


TZ = _local_tz()
SETUP_HINT = (f"Gmail/Calendar isn't set up yet: the user fills in {CRED_FILE} "
              "(steps in 'How to use Jarvis.txt').")


def creds() -> dict:
    out = {}
    if CRED_FILE.exists():
        for line in CRED_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().upper()] = v.strip().strip("\"'").strip()
    return out


# ---- Gmail ---------------------------------------------------------------------------------

def _imap():
    c = creds()
    addr, pw = c.get("GMAIL_ADDRESS", ""), c.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not addr or not pw:
        raise LookupError(SETUP_HINT)
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    try:
        m.login(addr, pw)
    except imaplib.IMAP4.error as exc:
        raise LookupError("Gmail refused the sign-in. Check the address and app password in "
                          "GOOGLE-ACCOUNT.txt (an app password is 16 letters; your normal "
                          f"password won't work). ({exc})") from None
    return m


def _all_mail(m) -> str:
    typ, boxes = m.list()
    for b in boxes or []:
        s = b.decode("utf-8", "replace")
        if "\\All" in s:
            return '"' + s.rsplit(' "/" ', 1)[-1].strip().strip('"') + '"'
    return "INBOX"


def _hdr(v) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(v or ""))).strip()
    except Exception:  # noqa: BLE001
        return (v or "").strip()


def tool_check_email(search: str = "", unread_only: bool = True, days: int = 3, max_results: int = 10) -> str:
    try:
        m = _imap()
    except LookupError as exc:
        return str(exc)
    try:
        box = _all_mail(m) if search else "INBOX"
        m.select(box, readonly=True)
        q = []
        if unread_only:
            q.append("is:unread")
        if days:
            q.append(f"newer_than:{max(1, int(days))}d")
        if search:
            q.append(re.sub(r'["\\]', " ", search))
        query = " ".join(q) or "in:inbox"
        query = query.encode("ascii", "ignore").decode()
        typ, data = m.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        uids = (data[0] or b"").split()
        if not uids:
            return f"No emails found for: {query}."
        total = len(uids)
        uids = uids[-max(1, min(25, int(max_results or 10))):]
        typ, rows = m.uid("FETCH", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        items = []
        for row in rows:
            if not isinstance(row, tuple):
                continue
            uid = re.search(rb"UID (\d+)", row[0])
            msg = email.message_from_bytes(row[1])
            when = _hdr(msg.get("Date"))
            try:
                when = email.utils.parsedate_to_datetime(when).astimezone(TZ).strftime("%a %d %b %I:%M %p")
            except Exception:  # noqa: BLE001
                pass
            items.append(f"[{'A' if box != 'INBOX' else 'I'}{uid.group(1).decode() if uid else '?'}] "
                         f"{when} | From: {_hdr(msg.get('From'))} | Subject: {_hdr(msg.get('Subject'))}")
        head = f"{total} email(s) match '{query}'" + (f"; newest {len(items)} shown" if total > len(items) else "")
        return head + " (use read_email with the [id] to open one):\n" + "\n".join(reversed(items))
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


def _text_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def tool_read_email(email_id: str) -> str:
    mid = (email_id or "").strip().strip("[]")
    if not re.fullmatch(r"[AI]\d+", mid):
        return "Give the id shown by check_email, like I1234."
    try:
        m = _imap()
    except LookupError as exc:
        return str(exc)
    try:
        m.select(_all_mail(m) if mid[0] == "A" else "INBOX", readonly=True)
        typ, rows = m.uid("FETCH", mid[1:], "(BODY.PEEK[])")
        raw = next((r[1] for r in rows if isinstance(r, tuple)), None)
        if not raw:
            return "That email wasn't found."
        msg = email.message_from_bytes(raw, policy=policy.default)
        files = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
        body = _text_of(msg)
        if len(body) > 6000:
            body = body[:6000] + "\n[...cut...]"
        return (f"From: {msg.get('From')}\nTo: {msg.get('To')}\nDate: {msg.get('Date')}\n"
                f"Subject: {msg.get('Subject')}\n" + (f"Attachments: {', '.join(files)}\n" if files else "")
                + f"\n{body}")
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass


# ---- Calendar ------------------------------------------------------------------------------

_ics_cache: tuple[float, str, bytes] = (0.0, "", b"")


def _ics() -> bytes:
    global _ics_cache
    url = creds().get("CALENDAR_ICAL_LINK", "")
    if not url:
        raise LookupError(SETUP_HINT)
    if not url.lower().startswith("https://"):
        raise LookupError("The calendar link in GOOGLE-ACCOUNT.txt must start with https://")
    if _ics_cache[1] == url and time.time() - _ics_cache[0] < 300:
        return _ics_cache[2]
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-home-assistant"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    if b"BEGIN:VCALENDAR" not in data[:2000]:
        raise LookupError("The calendar link didn't return a calendar. Use the 'Secret address in "
                          "iCal format' from Google Calendar settings.")
    _ics_cache = (time.time(), url, data)
    return data


def _start_day(start: str) -> date:
    s = (start or "today").strip().lower()
    today = datetime.now(TZ).date()
    if s in ("", "today", "now"):
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s == "yesterday":
        return today - timedelta(days=1)
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, n in enumerate(names):
        if s.startswith(n[:3]) and n.startswith(s.replace("next ", "")[:3]):
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    return datetime.strptime(s, "%Y-%m-%d").date()


def tool_calendar_events(start: str = "today", days: int = 1, search: str = "") -> str:
    import icalendar
    import recurring_ical_events

    try:
        cal = icalendar.Calendar.from_ical(_ics())
        first = _start_day(start)
    except LookupError as exc:
        return str(exc)
    except ValueError:
        return "Give start as today, tomorrow, a weekday name, or YYYY-MM-DD."
    days = max(1, min(60, int(days or 1)))
    a = datetime.combine(first, datetime.min.time(), TZ)
    b = a + timedelta(days=days)
    rows = []
    for ev in recurring_ical_events.of(cal).between(a, b):
        title = str(ev.get("SUMMARY", "(no title)"))
        if search and search.lower() not in (title + " " + str(ev.get("DESCRIPTION", ""))).lower():
            continue
        s = ev.decoded("DTSTART")
        e = ev.decoded("DTEND") if ev.get("DTEND") else None
        if isinstance(s, datetime):
            s = s.astimezone(TZ) if s.tzinfo else s.replace(tzinfo=TZ)
            e = (e.astimezone(TZ) if e.tzinfo else e.replace(tzinfo=TZ)) if isinstance(e, datetime) else None
            when = s.strftime("%a %d %b %I:%M %p").replace(" 0", " ") + (f"-{e:%I:%M %p}" if e else "")
            key = s
        else:
            when, key = s.strftime("%a %d %b") + " (all day)", datetime.combine(s, datetime.min.time(), TZ)
        where = str(ev.get("LOCATION", "")).strip()
        rows.append((key, f"{when}: {title}" + (f" @ {where}" if where else "")))
    span = first.strftime("%a %d %b") + ("" if days == 1 else f" + {days - 1} more day(s)")
    if not rows:
        return f"No calendar events for {span}" + (f" matching '{search}'." if search else ".")
    return f"Calendar for {span}:\n" + "\n".join(r for _, r in sorted(rows))


TOOLS = {
    "check_email": (tool_check_email, "List the user's Gmail messages (sender, subject, time, id). "
                    "Defaults: unread inbox mail from the last 3 days. 'search' takes Gmail search "
                    "words, e.g. 'from:amazon', 'subject:invoice', 'exam'. Read-only.",
                    {"search": {"type": "string"}, "unread_only": {"type": "boolean"},
                     "days": {"type": "integer", "description": "How many days back (default 3)."},
                     "max_results": {"type": "integer", "description": "Default 10, max 25."}}, []),
    "read_email": (tool_read_email, "Read one Gmail message in full, by the id check_email showed "
                   "(like I1234). It is not marked as read.",
                   {"email_id": {"type": "string"}}, ["email_id"]),
    "calendar_events": (tool_calendar_events, "The user's Google Calendar events (read-only): for "
                        "today by default, or a start day (today, tomorrow, a weekday, YYYY-MM-DD) "
                        "and a number of days; optional search word. Cannot add events.",
                        {"start": {"type": "string"}, "days": {"type": "integer"},
                         "search": {"type": "string"}}, []),
}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "jarvis-google-tools", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d,
                             "inputSchema": {"type": "object", "properties": props, "required": req}}
                            for n, (_, d, props, req) in TOOLS.items()]}
    elif method == "tools/call":
        p = msg.get("params", {})
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        try:
            text, err = TOOLS[name][0](**args), False
        except Exception as exc:  # noqa: BLE001
            text, err = f"{name} failed: {exc.__class__.__name__}: {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
