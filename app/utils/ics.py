from __future__ import annotations

from dataclasses import dataclass
from datetime import date


def _ics_escape(s: str) -> str:
    """
    iCalendar  ()
    - \\ -> \\\\
    - ; , ,  Process
    """
    s = (s or "").replace("\\", "\\\\")
    s = s.replace(";", "\\;").replace(",", "\\,")
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return s


def _fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


@dataclass(frozen=True)
class ICSEvent:
    uid: str
    summary: str
    start: date
    description: str = ""
    url: str | None = None


def build_calendar(events: list[ICSEvent], *, cal_name: str = "IP Docket Calendar") -> str:
    """
    All-day  (text/calendar) ICS Create
    """
    lines: list[str] = []
    lines.append("BEGIN:VCALENDAR")
    lines.append("VERSION:2.0")
    lines.append("PRODID:-//IPDocket//IPM//EN")
    lines.append("CALSCALE:GREGORIAN")
    lines.append(f"X-WR-CALNAME:{_ics_escape(cal_name)}")

    for e in events:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{_ics_escape(e.uid)}")
        lines.append(f"SUMMARY:{_ics_escape(e.summary)}")
        lines.append(f"DTSTART;VALUE=DATE:{_fmt_date(e.start)}")
        if e.description:
            lines.append(f"DESCRIPTION:{_ics_escape(e.description)}")
        if e.url:
            lines.append(f"URL:{_ics_escape(e.url)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
