#!/usr/bin/env python
"""Validator for the family-digest daily brief.

Two modes:

    python validate.py --today
        Prints today's date fields for digest.json (Europe/Amsterdam):
        date, date_he and updated, one "key<TAB>value" line each.

    python validate.py <new-digest.json> [old-digest.json]
        Checks a candidate digest. Old file defaults to digest.json next to
        this script. Exits 0 and prints OK, or exits 1 and prints every
        problem found (it does not stop at the first one).

Run the check BEFORE moving the candidate over digest.json, so the old file
is still in place for the 7-day rule comparison.
"""

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

HEB_WEEKDAYS = {
    6: "ראשון", 0: "שני", 1: "שלישי", 2: "רביעי",
    3: "חמישי", 4: "שישי", 5: "שבת",
}
HEB_MONTHS = [
    "בינואר", "בפברואר", "במרץ", "באפריל", "במאי", "ביוני",
    "ביולי", "באוגוסט", "בספטמבר", "באוקטובר", "בנובמבר", "בדצמבר",
]

HEBREW_START = re.compile(r"^[֐-׿]")
ID_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+$")
KEEP_DAYS = 7


def amsterdam_now():
    """Current time in Europe/Amsterdam, falling back to a fixed CET/CEST
    offset if the tz database is unavailable (Windows without tzdata)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Amsterdam"))
    except Exception:
        utc = datetime.now(timezone.utc)
        # CEST (+2) runs from the last Sunday of March to the last Sunday of
        # October; +1 otherwise. Month-level approximation is enough here.
        offset = 2 if 3 < utc.month < 10 else 1
        return utc.astimezone(timezone(timedelta(hours=offset)))


def today_fields():
    now = amsterdam_now()
    date_he = "יום {}, {} {} {}".format(
        HEB_WEEKDAYS[now.weekday()], now.day, HEB_MONTHS[now.month - 1], now.year
    )
    return {
        "date": now.strftime("%Y-%m-%d"),
        "date_he": date_he,
        "updated": now.strftime("%H:%M"),
    }


def all_items(d):
    """Every item in the file, tagged with the section it sits in."""
    for section in ("stories", "brief", "earlier"):
        for item in d.get(section, []):
            yield section, item


def check(new_path, old_path):
    problems = []

    def bad(msg):
        problems.append(msg)

    raw = Path(new_path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return ["file is not valid UTF-8: {}".format(e)]
    if re.search(r"[\ud800-\udfff]", text):
        bad("file contains lone surrogates; write emoji as literal characters")

    try:
        new = json.loads(text)
    except json.JSONDecodeError as e:
        return ["file is not valid JSON: {}".format(e)]

    fields = today_fields()
    for key in ("date", "date_he", "updated"):
        if key not in new:
            bad("missing top-level field: {}".format(key))
    if new.get("date") != fields["date"]:
        bad("date is {!r}, expected today {!r}".format(new.get("date"), fields["date"]))
    if new.get("date_he") != fields["date_he"]:
        bad("date_he is {!r}, expected {!r}".format(new.get("date_he"), fields["date_he"]))

    stories = new.get("stories", [])
    brief = new.get("brief", [])
    earlier = new.get("earlier", [])
    if not 6 <= len(stories) <= 8:
        bad("stories: {} items, must be 6 to 8".format(len(stories)))
    if not 3 <= len(brief) <= 5:
        bad("brief: {} items, must be 3 to 5".format(len(brief)))

    seen = {}
    for section, item in all_items(new):
        label = "{}[{}]".format(section, item.get("id") or "NO-ID")

        for key in ("id", "added"):
            if not item.get(key):
                bad("{}: missing {}".format(label, key))
        item_id = item.get("id")
        if item_id:
            if item_id in seen:
                bad("{}: duplicate id, also in {}".format(label, seen[item_id]))
            seen[item_id] = section
            if not ID_SHAPE.match(item_id):
                bad("{}: id should look like 2026-09-18-short-slug".format(label))
        if item.get("added"):
            try:
                date.fromisoformat(item["added"])
            except ValueError:
                bad("{}: added {!r} is not YYYY-MM-DD".format(label, item["added"]))

        url = item.get("url", "")
        if not url.startswith("https://nos.nl/"):
            bad("{}: url must be an nos.nl link, got {!r}".format(label, url))

        if section == "stories":
            paragraphs = item.get("paragraphs") or []
            if not item.get("tag"):
                bad("{}: missing tag".format(label))
            if not 1 <= len(paragraphs) <= 2:
                bad("{}: {} paragraphs, must be 1 (or 2 for the lead)".format(
                    label, len(paragraphs)))
            texts = [("title", item.get("title", ""))]
            texts += [("paragraph", p) for p in paragraphs]
        elif section == "brief":
            texts = [("text", item.get("text", ""))]
        else:
            texts = [("title", item.get("title", "")), ("text", item.get("text", ""))]

        for field, value in texts:
            if not value:
                bad("{}: empty {}".format(label, field))
            elif not HEBREW_START.match(value):
                bad("{}: {} must start with a Hebrew word, starts {!r}".format(
                    label, field, value[:24]))

    # The 7-day rule, checked against what is on the page right now.
    old_file = Path(old_path)
    if not old_file.exists():
        bad("old digest not found at {} — cannot check the 7-day rule".format(old_file))
    else:
        old = json.loads(old_file.read_text(encoding="utf-8"))
        carried = dict(seen)
        for _, item in all_items(new):
            for merged in item.get("merged_ids", []) or []:
                carried[merged] = "merged"
        added_dates = {i.get("id"): i.get("added") for _, i in all_items(new)}
        today = date.fromisoformat(fields["date"])
        for section, item in all_items(old):
            item_id, added = item.get("id"), item.get("added")
            if not item_id or not added:
                continue
            try:
                age = (today - date.fromisoformat(added)).days
            except ValueError:
                continue
            if age >= KEEP_DAYS:
                continue
            if item_id not in carried:
                bad("7-day rule: {!r} (added {}, {} days old) was dropped; "
                    "it must stay in stories, brief or earlier".format(item_id, added, age))
            elif item_id in added_dates and added_dates[item_id] != added:
                bad("7-day rule: {!r} changed its added date from {} to {}".format(
                    item_id, added, added_dates[item_id]))

    return problems


def main():
    # Windows consoles default to cp1252, which cannot encode Hebrew: without
    # this, printing a date_he or quoting a Hebrew title raises
    # UnicodeEncodeError and the validator dies instead of reporting.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = [a for a in sys.argv[1:] if a]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--today":
        for key, value in today_fields().items():
            print("{}\t{}".format(key, value))
        return 0

    new_path = args[0]
    old_path = args[1] if len(args) > 1 else str(HERE / "digest.json")
    problems = check(new_path, old_path)
    if problems:
        print("FAILED ({} problem{}):".format(
            len(problems), "" if len(problems) == 1 else "s"))
        for p in problems:
            print("  - {}".format(p))
        return 1

    d = json.loads(Path(new_path).read_text(encoding="utf-8"))
    print("OK  {}  stories={} brief={} earlier={}".format(
        d["date"], len(d["stories"]), len(d["brief"]), len(d.get("earlier", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
