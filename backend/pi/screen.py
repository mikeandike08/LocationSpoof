"""Draws the device's 320x240 status screen. Pure Pillow: no hardware, so it runs (and is checked) anywhere.

Layout, matching the Display HAT Mini's buttons (A, B on the left edge; X, Y on the right):

    +-----------------------------------+
    | LocationSpoof          [ READY ]  |
    | A 1 Home              Gym 3 X     |
    |                                   |
    |   SPOOFING                        |
    |   Home                            |
    |   40.71280, -74.00600             |
    |   (status / hints)                |
    | B 2 Work          Beach 4 Y       |
    +-----------------------------------+
"""

from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 320, 240

BG = (14, 16, 19)
PANEL = (27, 31, 37)
TEXT = (232, 234, 238)
MUTED = (144, 153, 166)
ACCENT = (79, 140, 255)
OK = (34, 197, 94)
WARN = (245, 158, 11)
DANGER = (239, 68, 68)

LEVEL_COLORS = {"ok": OK, "spoofing": ACCENT, "busy": WARN, "warn": WARN, "error": DANGER, "off": MUTED}

# Which physical button sits in which corner, and which preset slot it triggers.
BUTTON_SLOTS = {"A": 1, "B": 2, "X": 3, "Y": 4}
CORNERS = {"A": "top-left", "B": "bottom-left", "X": "top-right", "Y": "bottom-right"}


@dataclass
class ScreenState:
    chip: str = "STARTING"  # short status in the top-right chip
    level: str = "busy"  # ok | spoofing | busy | warn | error | off
    headline: str = "Starting…"  # e.g. SPOOFING / REAL LOCATION / DRIVING ROUTE
    title: str = ""  # big line: the place name
    subtitle: str = ""  # coordinates or route progress
    message: str = ""  # wrapped status / setup hint
    presets: dict[int, str] = field(default_factory=dict)  # slot -> place name
    toast: Optional[str] = None  # brief confirmation over everything
    alarm: bool = False  # an ongoing critical problem: red frame


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1 ships a scalable default font
    except TypeError:
        return ImageFont.load_default()


FONTS = {s: _font(s) for s in (11, 13, 15, 18, 26)}


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    """Trim text with an ellipsis so it fits in `width` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(draw, lines[-1] + " …", font, width)
    return [_fit(draw, line, font, width) for line in lines]


def _button_label(draw: ImageDraw.ImageDraw, button: str, name: Optional[str]) -> None:
    slot = BUTTON_SLOTS[button]
    corner = CORNERS[button]
    font = FONTS[13]
    label = _fit(draw, name or "(empty)", font, 104)  # ASCII only: the built-in font lacks arrows/dashes
    top = corner.startswith("top")
    y = 42 if top else HEIGHT - 26
    pill_w = 34
    color = TEXT if name else MUTED
    if corner.endswith("left"):
        draw.rounded_rectangle((4, y - 2, 4 + pill_w, y + 18), 6, fill=PANEL)
        draw.text((10, y), f"{button}{slot}", font=font, fill=ACCENT)
        draw.text((4 + pill_w + 6, y), label, font=font, fill=color)
    else:
        x1 = WIDTH - 4
        draw.rounded_rectangle((x1 - pill_w, y - 2, x1, y + 18), 6, fill=PANEL)
        draw.text((x1 - pill_w + 6, y), f"{slot}{button}", font=font, fill=ACCENT)
        text_w = draw.textlength(label, font=font)
        draw.text((x1 - pill_w - 6 - text_w, y), label, font=font, fill=color)


def render(state: ScreenState) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)

    # header
    d.text((10, 9), "LocationSpoof", font=FONTS[18], fill=TEXT)
    chip_font = FONTS[13]
    chip = state.chip.upper()
    chip_w = d.textlength(chip, font=chip_font) + 16
    color = LEVEL_COLORS.get(state.level, MUTED)
    d.rounded_rectangle((WIDTH - 8 - chip_w, 8, WIDTH - 8, 30), 11, fill=color)
    d.text((WIDTH - 8 - chip_w + 8, 12), chip, font=chip_font, fill=BG)

    # preset buttons in the corners
    for button, slot in BUTTON_SLOTS.items():
        _button_label(d, button, state.presets.get(slot))

    # body
    x, width = 14, WIDTH - 28
    y = 72
    d.text((x, y), state.headline.upper(), font=FONTS[13], fill=color)
    y += 20
    if state.title:
        d.text((x, y), _fit(d, state.title, FONTS[26], width), font=FONTS[26], fill=TEXT)
        y += 32
    if state.subtitle:
        d.text((x, y), _fit(d, state.subtitle, FONTS[15], width), font=FONTS[15], fill=MUTED)
        y += 22
    if state.message:
        for line in _wrap(d, state.message, FONTS[13], width, max_lines=max(1, (HEIGHT - 34 - y) // 16)):
            d.text((x, y), line, font=FONTS[13], fill=MUTED if state.level not in ("error", "warn") else TEXT)
            y += 16

    if state.alarm:
        for inset in range(4):
            d.rectangle((inset, inset, WIDTH - 1 - inset, HEIGHT - 1 - inset), outline=DANGER)

    if state.toast:
        font = FONTS[18]
        text = _fit(d, state.toast, font, WIDTH - 60)
        w = d.textlength(text, font=font)
        box = ((WIDTH - w) / 2 - 16, HEIGHT / 2 - 22, (WIDTH + w) / 2 + 16, HEIGHT / 2 + 22)
        d.rounded_rectangle(box, 12, fill=PANEL, outline=ACCENT, width=2)
        d.text(((WIDTH - w) / 2, HEIGHT / 2 - 11), text, font=font, fill=TEXT)
    return img
