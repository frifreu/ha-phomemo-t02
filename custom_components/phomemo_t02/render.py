"""Bildaufbereitung und Druckprotokoll für den Phomemo T02 (ohne HA-Abhängigkeit)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .const import BYTES_PER_LINE, MAX_LINES_PER_BLOCK, PRINT_WIDTH_PX

FONT_DIR = Path(__file__).parent / "fonts"
MARGIN = 4

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    """Bricht Text pixelgenau um; überlange Wörter werden hart getrennt."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip() if current else word
            if draw.textlength(candidate, font=font) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            while draw.textlength(word, font=font) > width:
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


def render_text(text: str, font_size: int = 22, stroke: int = 1) -> Image.Image:
    """Rendert Text mit einfacher Auszeichnung.

    ``# Titel``  -> große fette Zeile
    ``**Text**`` -> ganze Zeile fett
    ``---``      -> Trennlinie

    ``stroke``: zusätzliche Strichstärke in Pixeln (0 = normal, 1 = kräftiger)
    """
    width = PRINT_WIDTH_PX - 2 * MARGIN
    scratch = ImageDraw.Draw(Image.new("L", (1, 1)))
    normal, bold, big = _font(font_size), _font(font_size, True), _font(int(font_size * 1.4), True)

    ops: list[tuple[str, object, str, int]] = []  # (art, font, text, höhe)
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.strip() == "---":
            ops.append(("rule", None, "", 12))
            continue
        font = normal
        if line.startswith("# "):
            font, line = big, line[2:]
        elif line.startswith("**") and line.endswith("**") and len(line) > 4:
            font, line = bold, line[2:-2]
        ascent, descent = font.getmetrics()
        for part in _wrap(scratch, line, font, width):
            ops.append(("text", font, part, ascent + descent + 2 + stroke))

    height = sum(op[3] for op in ops) + 2 * MARGIN
    img = Image.new("L", (PRINT_WIDTH_PX, max(height, 1)), 255)
    draw = ImageDraw.Draw(img)
    y = MARGIN
    for kind, font, part, h in ops:
        if kind == "rule":
            draw.line((MARGIN, y + h // 2, PRINT_WIDTH_PX - MARGIN, y + h // 2), fill=0, width=2)
        else:
            draw.text((MARGIN, y), part, font=font, fill=0, stroke_width=stroke, stroke_fill=0)
        y += h
    return img.convert("1", dither=Image.Dither.NONE)


def _atkinson(img: Image.Image) -> Image.Image:
    """Atkinson-Raster: verteilt nur 6/8 des Fehlers, helle Flächen bleiben sauber.

    Für Thermodruck besser als Floyd-Steinberg (weniger "Grauschleier").
    """
    w, h = img.size
    px = [list(map(float, img.crop((0, y, w, y + 1)).getdata())) for y in range(h)]
    out = Image.new("1", (w, h), 1)
    put = out.putpixel
    for y in range(h):
        row, nxt = px[y], px[y + 1] if y + 1 < h else None
        nxt2 = px[y + 2] if y + 2 < h else None
        for x in range(w):
            old = row[x]
            if old < 128:
                put((x, y), 0)
                err = old / 8
            else:
                err = (old - 255) / 8
            if x + 1 < w:
                row[x + 1] += err
            if x + 2 < w:
                row[x + 2] += err
            if nxt is not None:
                if x > 0:
                    nxt[x - 1] += err
                nxt[x] += err
                if x + 1 < w:
                    nxt[x + 1] += err
            if nxt2 is not None:
                nxt2[x] += err
    return out


def prepare_image(path, rotate: str = "auto", dither: str = "atkinson") -> Image.Image:
    """Skaliert ein Bild auf Druckbreite und rastert es für den Thermodruck.

    rotate: "auto" (Querformat drehen), "always", "never"
    dither: "atkinson" (Fotos, Standard), "floyd" (feiner, dunkler), "none" (Grafiken/Text)
    """
    with Image.open(path) as src:
        img = ImageOps.exif_transpose(src).convert("L")
    if rotate == "always" or (rotate == "auto" and img.width > img.height * 1.2):
        img = img.rotate(90, expand=True)
    ratio = PRINT_WIDTH_PX / img.width
    img = img.resize((PRINT_WIDTH_PX, max(1, round(img.height * ratio))), Image.Resampling.LANCZOS)
    if dither == "none":
        return img.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.Dither.NONE)
    # Thermodruck läuft zu: aufhellen und nachschärfen, damit Gesichter erkennbar bleiben
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.point([round(255 * (v / 255) ** 0.8) for v in range(256)])
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=2))
    if dither == "floyd":
        return img.convert("1")
    return _atkinson(img)


def build_job(img: Image.Image, feed_lines: int = 4, density: int = 4) -> bytes:
    """Erzeugt den Byte-Strom für den Drucker (ESC/POS-Raster, Phomemo-Variante)."""
    if img.mode != "1" or img.width != PRINT_WIDTH_PX:
        raise ValueError("Bild muss 1-Bit und 384 px breit sein")
    # PIL: Bit 1 = weiß; Drucker: Bit 1 = schwarz -> invertieren
    raw = bytes(b ^ 0xFF for b in img.tobytes())

    out = bytearray(b"\x1b\x40")          # ESC @  Initialisieren
    out += b"\x1b\x61\x01"                # ESC a 1  zentriert
    out += b"\x1f\x11\x02" + bytes([density])  # Phomemo: Druckdichte/Hitze
    for start in range(0, img.height, MAX_LINES_PER_BLOCK):
        lines = min(MAX_LINES_PER_BLOCK, img.height - start)
        out += b"\x1d\x76\x30\x00"         # GS v 0
        out += BYTES_PER_LINE.to_bytes(2, "little")
        out += lines.to_bytes(2, "little")
        out += raw[start * BYTES_PER_LINE:(start + lines) * BYTES_PER_LINE]
    out += b"\x1b\x64" + bytes([feed_lines])  # ESC d n  Vorschub
    out += b"\x1b\x64" + bytes([feed_lines])
    out += b"\x1f\x11\x08\x1f\x11\x0e\x1f\x11\x07\x1f\x11\x09"  # Phomemo-Abschluss
    return bytes(out)


# ---------------------------------------------------------------- Kalender

KURZTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _parse(value: str, tz=None) -> tuple[datetime | date, bool]:
    if len(value) == 10:
        return date.fromisoformat(value), True
    dt = datetime.fromisoformat(value)
    if tz is not None and dt.tzinfo is not None:
        dt = dt.astimezone(tz)  # immer in Ortszeit rechnen
    return dt, False


def _short(d: date) -> str:
    return f"{KURZTAGE[d.weekday()]} {d.day:02d}.{d.month:02d}."


def _entries(day: date, calendars: dict[str, tuple[str, list[dict]]], show_location: bool,
             show_source: bool, tz) -> list[tuple[tuple, str, str, str]]:
    entries = []
    for name, events in calendars.values():
        for ev in events:
            start, all_day = _parse(ev["start"], tz)
            end, _ = _parse(ev.get("end", ev["start"]), tz)
            if all_day:
                last = end - timedelta(days=1) if end > start else start  # Ende ist exklusiv
                if start == day and last == day:
                    when = "ganztägig"
                elif start == last:
                    when = f"{_short(start)} ganztägig"
                else:
                    when = f"{_short(start)}–{_short(last)}"
                key = (max(start, day), -1)
            else:
                sd, ed = start.date(), end.date()
                if sd == day and ed == day:
                    when = f"{start:%H:%M}–{end:%H:%M}"
                elif sd < day:
                    when = f"bis {_short(ed) + ' ' if ed != day else ''}{end:%H:%M}"
                else:
                    when = f"{_short(sd)} {start:%H:%M}–{end:%H:%M}"
                key = (max(sd, day), start.hour * 60 + start.minute if sd >= day else 0)
            title = (ev.get("summary") or "(ohne Titel)").strip()
            location = (ev.get("location") or "").strip()
            if location and title.endswith(f" / {location}"):
                title = title[: -len(location) - 3].rstrip()  # "… / Ort" am Titelende kürzen
            extra = []
            if show_location and location:
                extra.append(location)
            if show_source:
                extra.append(f"[{name}]")
            entries.append((key, when, title, " · ".join(extra)))
    entries.sort(key=lambda e: e[0])
    return entries


def _append(lines: list[str], entries) -> None:
    for _, when, title, extra in entries:
        lines.append(f"**{when}**")
        lines.append(title)
        if extra:
            lines.append(f"  {extra}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()


def format_calendar(day: date, calendars: dict[str, tuple[str, list[dict]]],
                    show_location: bool = True, empty_text: str = "Keine Termine heute.",
                    tz=None, preview: dict[str, tuple[str, list[dict]]] | None = None,
                    preview_title: str = "Morgen") -> str:
    """Baut den Drucktext aus calendar.get_events-Antworten.

    ``calendars``: entity_id -> (Anzeigename, Ereignisliste) für ``day``
    ``preview``:   wie oben, Termine der Folgetage (z. B. Abfuhr) als eigener Abschnitt
    """
    show_source = len(set(calendars) | set(preview or {})) > 1
    lines = [f"# {WOCHENTAGE[day.weekday()]}", f"**{day.day}. {MONATE[day.month - 1]} {day.year}**", "---"]
    today = _entries(day, calendars, show_location, show_source, tz)
    if today:
        _append(lines, today)
    else:
        lines.append(empty_text)
    if preview:
        upcoming = _entries(day + timedelta(days=1), preview, False, False, tz)
        if upcoming:
            lines += ["---", f"# {preview_title}"]
            _append(lines, upcoming)
    lines.append("---")
    return "\n".join(lines)


def density_test_job(levels: list[int]) -> tuple[bytes, int]:
    """Ein Datenstrom mit je einem kurzen Muster pro Dichtestufe."""
    out, height = bytearray(), 0
    for level in levels:
        img = render_text(f"**Dichte {level}**\nHonigraum 25.10. – Täglich\n---", 22, 1)
        bar = Image.new("1", (PRINT_WIDTH_PX, 24), 0)  # schwarzer Balken
        both = Image.new("1", (PRINT_WIDTH_PX, img.height + bar.height), 1)
        both.paste(img, (0, 0))
        both.paste(bar, (0, img.height))
        out += build_job(both, feed_lines=2, density=level)
        height += both.height + 16
    return bytes(out), height
