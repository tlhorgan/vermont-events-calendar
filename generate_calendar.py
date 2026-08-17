from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event


OUTPUT = Path("vermont-events.ics")

VERMONT_COM = "https://vermont.com/calendar/"
VERMONT_PUBLIC = "https://www.vermontpublic.org/vermont-events-calendar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BAD_TITLES = {
    "vermont",
    "vermont guides",
    "calendar",
    "calendar of events",
    "event results",
    "featured events",
    "more info",
}


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def norm(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    ).lower()

    value = value.replace("&", " and ")

    return clean(
        re.sub(r"[^a-z0-9]+", " ", value)
    )


def get(url):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()
    return response


def valid_title(title):
    title = clean(title)

    if not title:
        return False

    if norm(title) in BAD_TITLES:
        return False

    if len(title) < 3:
        return False

    return True


def make_item(
    title,
    start,
    end,
    location,
    description,
    url,
    source,
):
    return {
        "title": clean(title),
        "start": start,
        "end": end,
        "location": clean(location),
        "description": clean(description),
        "url": clean(url),
        "sources": [source],
    }


def parse_time_range(text, event_date):
    """
    Parse common Vermont.com time strings such as:

      10 - 11am
      12:30 - 1:30pm
      1 - 3pm
      5:00-8:00pm
      11am to 5pm
    """

    m = re.search(
        r"\b(\d{1,2}(?::\d{2})?)\s*"
        r"(am|pm)?\s*"
        r"(?:-|–|—|to)\s*"
        r"(\d{1,2}(?::\d{2})?)\s*"
        r"(am|pm)\b",
        text,
        re.I,
    )

    if not m:
        return None, None

    start_clock = m.group(1)
    start_ampm = m.group(2)
    end_clock = m.group(3)
    end_ampm = m.group(4)

    # If only the ending AM/PM is given, infer it for the start.
    if not start_ampm:
        start_ampm = end_ampm

    try:
        start_time = dtparser.parse(
            f"{start_clock}{start_ampm}"
        ).time()

        end_time = dtparser.parse(
            f"{end_clock}{end_ampm}"
        ).time()

        start = datetime.combine(
            event_date,
            start_time,
        )

        end = datetime.combine(
            event_date,
            end_time,
        )

        # Handle evening ranges such as 11pm-1am.
        if end <= start:
            end += timedelta(days=1)

        return start, end

    except Exception:
        return None, None


def fetch_vermont_com():
    """
    Parse Vermont.com's main calendar results directly.

    This avoids opening individual pages, which previously caused
    global site headings such as "Vermont Guides" to become events.
    """

    soup = BeautifulSoup(
        get(VERMONT_COM).text,
        "html.parser",
    )

    items = []
    seen = set()

    # Vermont.com currently renders each result using repeated links
    # to the same destination. We use links containing a date label
    # such as "Saturday,8/22 2026" as anchors for each result.
    date_link_re = re.compile(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),"
        r"\s*\d{1,2}/\d{1,2}\s+20\d{2}$",
        re.I,
    )

    for date_link in soup.find_all("a"):
        date_label = clean(
            date_link.get_text(" ", strip=True)
        )

        if not date_link_re.match(date_label):
            continue

        href = date_link.get("href", "")
        if not href:
            continue

        event_url = urljoin(
            VERMONT_COM,
            href,
        )

        # Find nearby links pointing to the same destination.
        parent = date_link.parent

        block = parent
        for _ in range(5):
            if not block:
                break

            matching = [
                a for a in block.find_all(
                    "a",
                    href=True,
                )
                if urljoin(
                    VERMONT_COM,
                    a["href"],
                ) == event_url
            ]

            candidate_texts = [
                clean(
                    a.get_text(
                        " ",
                        strip=True,
                    )
                )
                for a in matching
            ]

            candidate_texts = [
                x for x in candidate_texts
                if x
                and not date_link_re.match(x)
                and norm(x) != "more info"
            ]

            # Usually town + title are the first two.
            if len(candidate_texts) >= 2:
                break

            block = block.parent

        if not block:
            continue

        candidate_texts = [
            clean(
                a.get_text(
                    " ",
                    strip=True,
                )
            )
            for a in block.find_all(
                "a",
                href=True,
            )
            if urljoin(
                VERMONT_COM,
                a["href"],
            ) == event_url
        ]

        candidate_texts = [
            x for x in candidate_texts
            if x
            and not date_link_re.match(x)
            and norm(x) != "more info"
        ]

        if len(candidate_texts) < 2:
            continue

        town = candidate_texts[0]
        title = candidate_texts[1]

        if not valid_title(title):
            continue

        try:
            event_date = dtparser.parse(
                date_label.replace(",", ", ")
            ).date()
        except Exception:
            continue

        block_text = clean(
            block.get_text(
                " ",
                strip=True,
            )
        )

        start, end = parse_time_range(
            block_text,
            event_date,
        )

        if start is None:
            # If no time is available, treat as all-day.
            start = event_date
            end = event_date + timedelta(days=1)

        # Try to identify organizer/venue from plain text between
        # the title and description.
        plain_lines = [
            clean(x)
            for x in block.get_text(
                "\n",
                strip=True,
            ).splitlines()
            if clean(x)
        ]

        location = town

        try:
            title_index = next(
                i for i, value in enumerate(plain_lines)
                if norm(value) == norm(title)
            )

            for candidate in plain_lines[
                title_index + 1:
                title_index + 5
            ]:
                if candidate in candidate_texts:
                    continue

                if date_link_re.match(candidate):
                    continue

                if re.search(
                    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)",
                    candidate,
                    re.I,
                ):
                    continue

                if norm(candidate) == "more info":
                    continue

                if 2 < len(candidate) < 100:
                    location = (
                        f"{candidate}, {town}, VT"
                    )
                    break

        except StopIteration:
            pass

        if location == town:
            location = f"{town}, VT"

        description = block_text

        # Remove repeated UI noise from description.
        for noise in [
            town,
            title,
            date_label,
            "More Info",
        ]:
            description = description.replace(
                noise,
                " ",
            )

        description = clean(description)

        key = (
            event_date,
            norm(title),
            norm(location),
        )

        if key in seen:
            continue

        seen.add(key)

        items.append(
            make_item(
                title=title,
                start=start,
                end=end,
                location=location,
                description=description,
                url=event_url,
                source="Vermont.com",
            )
        )

    print(
        f"Vermont.com: {len(items)} events"
    )

    return items


def fetch_vermont_public():
    """
    Discover Vermont Public individual event pages and use JSON-LD
    when available.
    """

    soup = BeautifulSoup(
        get(VERMONT_PUBLIC).text,
        "html.parser",
    )

    urls = set()

    for a in soup.find_all(
        "a",
        href=True,
    ):
        href = a["href"]

        if "/vermont-events-calendar/event/" not in href:
            continue

        url = urljoin(
            VERMONT_PUBLIC,
            href,
        )

        url = (
            url.split("?")[0]
            .split("#")[0]
            .rstrip("/")
        )

        urls.add(url)

    print(
        f"Vermont Public discovered "
        f"{len(urls)} pages"
    )

    items = []

    for url in sorted(urls):
        try:
            page = BeautifulSoup(
                get(url).text,
                "html.parser",
            )

            found = False

            for tag in page.find_all(
                "script",
                attrs={
                    "type": "application/ld+json"
                },
            ):
                raw = (
                    tag.string
                    or tag.get_text()
                )

                if not raw:
                    continue

                try:
                    import json
                    data = json.loads(raw)
                except Exception:
                    continue

                objects = (
                    data
                    if isinstance(data, list)
                    else [data]
                )

                expanded = []

                for obj in objects:
                    if (
                        isinstance(obj, dict)
                        and isinstance(
                            obj.get("@graph"),
                            list,
                        )
                    ):
                        expanded.extend(
                            obj["@graph"]
                        )
                    else:
                        expanded.append(obj)

                for obj in expanded:
                    if not isinstance(obj, dict):
                        continue

                    types = obj.get(
                        "@type",
                        [],
                    )

                    if isinstance(
                        types,
                        str,
                    ):
                        types = [types]

                    if not any(
                        "Event" in str(t)
                        for t in types
                    ):
                        continue

                    title = clean(
                        obj.get("name")
                    )

                    if not valid_title(title):
                        continue

                    start_raw = obj.get(
                        "startDate"
                    )

                    if not start_raw:
                        continue

                    start = dtparser.parse(
                        str(start_raw)
                    )

                    if start.tzinfo:
                        start = start.replace(
                            tzinfo=None
                        )

                    end_raw = obj.get(
                        "endDate"
                    )

                    if end_raw:
                        end = dtparser.parse(
                            str(end_raw)
                        )

                        if end.tzinfo:
                            end = end.replace(
                                tzinfo=None
                            )
                    else:
                        end = (
                            start
                            + timedelta(hours=2)
                        )

                    location = ""

                    loc = obj.get(
                        "location"
                    )

                    if isinstance(
                        loc,
                        dict,
                    ):
                        parts = []

                        if loc.get("name"):
                            parts.append(
                                clean(
                                    loc.get("name")
                                )
                            )

                        address = loc.get(
                            "address"
                        )

                        if isinstance(
                            address,
                            dict,
                        ):
                            for field in [
                                "streetAddress",
                                "addressLocality",
                                "addressRegion",
                                "postalCode",
                            ]:
                                value = clean(
                                    address.get(
                                        field
                                    )
                                )

                                if value:
                                    parts.append(
                                        value
                                    )

                        elif isinstance(
                            address,
                            str,
                        ):
                            parts.append(
                                clean(address)
                            )

                        location = ", ".join(
                            dict.fromkeys(
                                p for p in parts
                                if p
                            )
                        )

                    description = clean(
                        BeautifulSoup(
                            str(
                                obj.get(
                                    "description",
                                    "",
                                )
                            ),
                            "html.parser",
                        ).get_text(" ")
                    )

                    event_url = clean(
                        obj.get("url")
                    ) or url

                    items.append(
                        make_item(
                            title=title,
                            start=start,
                            end=end,
                            location=location,
                            description=description,
                            url=event_url,
                            source="Vermont Public",
                        )
                    )

                    found = True
                    break

                if found:
                    break

            if not found:
                print(
                    "Vermont Public skip "
                    f"(no structured event): {url}"
                )

        except Exception as exc:
            print(
                f"Vermont Public skip "
                f"{url}: {exc}"
            )

    print(
        f"Vermont Public: "
        f"{len(items)} events"
    )

    return items


def event_day(item):
    value = item["start"]

    if isinstance(
        value,
        datetime,
    ):
        return value.date()

    return value


def duplicate(a, b):
    if event_day(a) != event_day(b):
        return False

    title_score = (
        1.0
        if norm(a["title"])
        == norm(b["title"])
        else 0.0
    )

    # Remove common suffix differences.
    ta = re.sub(
        r"\b(?:preview|matinee|opening night)\b",
        "",
        norm(a["title"]),
    )

    tb = re.sub(
        r"\b(?:preview|matinee|opening night)\b",
        "",
        norm(b["title"]),
    )

    if ta == tb:
        title_score = max(
            title_score,
            0.96,
        )

    if title_score >= 0.95:
        return True

    # Fuzzy comparison without importing another dependency.
    from difflib import SequenceMatcher

    score = SequenceMatcher(
        None,
        ta,
        tb,
    ).ratio()

    if score < 0.90:
        return False

    la = norm(
        a.get("location", "")
    )

    lb = norm(
        b.get("location", "")
    )

    if not la or not lb:
        return score >= 0.95

    location_score = SequenceMatcher(
        None,
        la,
        lb,
    ).ratio()

    return location_score >= 0.65


def dedupe(items):
    items.sort(
        key=lambda item: (
            event_day(item),
            norm(item["title"]),
        )
    )

    kept = []
    removed = 0

    for item in items:
        match = None

        for existing in reversed(
            kept
        ):
            existing_day = event_day(
                existing
            )

            item_day = event_day(
                item
            )

            if existing_day != item_day:
                if existing_day < item_day:
                    break
                continue

            if duplicate(
                existing,
                item,
            ):
                match = existing
                break

        if match is None:
            kept.append(item)
            continue

        removed += 1

        if (
            len(
                clean(
                    item.get(
                        "location",
                        "",
                    )
                )
            )
            >
            len(
                clean(
                    match.get(
                        "location",
                        "",
                    )
                )
            )
        ):
            match["location"] = (
                item["location"]
            )

        if (
            len(
                clean(
                    item.get(
                        "description",
                        "",
                    )
                )
            )
            >
            len(
                clean(
                    match.get(
                        "description",
                        "",
                    )
                )
            )
        ):
            match["description"] = (
                item["description"]
            )

        # Prefer a source-specific event URL over the
        # generic Vermont.com calendar URL.
        if (
            item.get("url")
            and item["url"]
            != VERMONT_COM
        ):
            match["url"] = (
                item["url"]
            )

        for source in item[
            "sources"
        ]:
            if source not in match[
                "sources"
            ]:
                match[
                    "sources"
                ].append(source)

    print(
        f"Removed {removed} "
        "duplicate events"
    )

    return kept


def build_calendar(items):
    cal = Calendar()

    cal.add(
        "prodid",
        "-//Combined Vermont Events Calendar//EN",
    )
    cal.add(
        "version",
        "2.0",
    )
    cal.add(
        "calscale",
        "GREGORIAN",
    )
    cal.add(
        "x-wr-calname",
        "Vermont Events",
    )
    cal.add(
        "x-wr-timezone",
        "America/New_York",
    )

    now = datetime.now(
        timezone.utc
    )

    for item in items:
        event = Event()

        uid_source = (
            f"{event_day(item)}|"
            f"{norm(item['title'])}|"
            f"{norm(item.get('location', ''))}"
        )

        uid = (
            hashlib.sha256(
                uid_source.encode()
            )
            .hexdigest()[:30]
            + "@vermont-events"
        )

        event.add(
            "uid",
            uid,
        )

        event.add(
            "dtstamp",
            now,
        )

        event.add(
            "summary",
            item["title"],
        )

        event.add(
            "dtstart",
            item["start"],
        )

        event.add(
            "dtend",
            item["end"],
        )

        if item.get(
            "location"
        ):
            event.add(
                "location",
                item["location"],
            )

        if item.get(
            "url"
        ):
            event.add(
                "url",
                item["url"],
            )

        description = clean(
            item.get(
                "description",
                "",
            )
        )

        source_note = (
            "Sources: "
            + ", ".join(
                item["sources"]
            )
        )

        if description:
            description += (
                "\n\n"
                + source_note
            )
        else:
            description = (
                source_note
            )

        event.add(
            "description",
            description,
        )

        cal.add_component(
            event
        )

    OUTPUT.write_bytes(
        cal.to_ical()
    )

    print(
        f"Wrote {OUTPUT} with "
        f"{len(items)} unique events"
    )


def main():
    all_items = []

    for name, fn in [
        (
            "Vermont.com",
            fetch_vermont_com,
        ),
        (
            "Vermont Public",
            fetch_vermont_public,
        ),
    ]:
        try:
            events = fn()

            print(
                f"{name} returned "
                f"{len(events)} usable events"
            )

            all_items.extend(
                events
            )

        except Exception as exc:
            print(
                f"ERROR loading "
                f"{name}: {exc}"
            )

    if not all_items:
        raise RuntimeError(
            "No events collected "
            "from any source"
        )

    unique = dedupe(
        all_items
    )

    # Prevent a broken scrape from overwriting
    # a healthy calendar.
    if len(unique) < 15:
        raise RuntimeError(
            f"Only {len(unique)} unique "
            "events generated; refusing "
            "to publish a bad feed"
        )

    build_calendar(
        unique
    )


if __name__ == "__main__":
    main()
