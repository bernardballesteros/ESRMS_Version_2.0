"""Colours and small building blocks matching the Citizen Dashboard reference design.

Two palettes, dark (default) and light. apply() swaps the module-level colour names, and every screen
reads them (t.BG, t.TEXT, ...) when it is built, so re-rendering a screen after apply() re-colours it.
Don't copy these values into module-level constants or default arguments elsewhere: they would keep
the palette that was active at import time.
"""
import flet as ft

PALETTES = {
    "dark": dict(
        BG="#05090F", BG_TOP="#0A1424", PANEL="#0B1526", PANEL_HI="#0F1D33", BORDER="#1C3556",
        CYAN="#22D3EE", TEAL="#2DD4BF", TEXT="#E6EDF7", MUTED="#8FA3BF", DIM="#5B6F8C",
        RED="#EF4444", BLUE="#3B82F6", AMBER="#F59E0B", GREEN="#10B981", SLATE="#64748B", VIOLET="#A78BFA",
        LOGO_BG="#0C1422", SOS_PANEL=("#0D1B33", "#070D19"), CRITICAL_BG="#1A0F16",
    ),
    "light": dict(
        BG="#F3F6FA", BG_TOP="#FFFFFF", PANEL="#FFFFFF", PANEL_HI="#E8EEF6", BORDER="#C3D0E0",
        # Darker accents: the dark theme's cyan/amber/green are unreadable on white.
        CYAN="#0E7490", TEAL="#0F766E", TEXT="#0F172A", MUTED="#475569", DIM="#7C8BA1",
        RED="#DC2626", BLUE="#2563EB", AMBER="#B45309", GREEN="#047857", SLATE="#475569", VIOLET="#7C3AED",
        LOGO_BG="#0C1422", SOS_PANEL=("#E8EEF6", "#DCE5F0"), CRITICAL_BG="#FDECEC",
    ),
}
ON_ACCENT = "#03121A"   # text on a cyan / amber filled button, both themes
MONO = "monospace"
name = "dark"
ACCENTS = {}


def apply(theme_name):
    """Make `theme_name` ('dark' or 'light') the palette that t.BG, t.TEXT, ... refer to."""
    global name
    name = theme_name if theme_name in PALETTES else "dark"
    globals().update(PALETTES[name])
    ACCENTS.update(red=RED, blue=BLUE, amber=AMBER, green=GREEN, cyan=CYAN, slate=SLATE, violet=VIOLET)  # noqa: F821
    return name


apply("dark")


def alpha(hex_color, a):
    """'#RRGGBB' + opacity 0..1 -> '#AARRGGBB'."""
    return f"#{int(a * 255):02X}{hex_color.lstrip('#')[-6:]}"


def accent(key):
    return ACCENTS.get(key, CYAN)


def on(color):
    """Readable text colour on a filled button of `color`."""
    return "white" if color == RED or name == "light" else ON_ACCENT


def pad(h=0, v=0):
    return ft.Padding(left=h, top=v, right=h, bottom=v)


def mono(value, size=11, color=None, weight=None, **kw):
    return ft.Text(value, size=size, color=color or MUTED, font_family=MONO, weight=weight, **kw)


def label(value, size=14, color=None, weight=ft.FontWeight.W_600, **kw):
    return ft.Text(value, size=size, color=color or TEXT, weight=weight, **kw)


def panel(content, *, padding=14, border_color=None, bgcolor=None, radius=16, on_click=None, **kw):
    return ft.Container(
        content=content, padding=padding, bgcolor=bgcolor or PANEL, border_radius=ft.BorderRadius.all(radius),
        border=ft.Border.all(1, border_color or BORDER), on_click=on_click, ink=on_click is not None, **kw,
    )


def accent_card(content, color, *, bgcolor=None, border_color=None, padding=None, on_click=None):
    """Rounded card with a coloured left stripe.

    Flutter can't round a border whose sides differ in colour, so the stripe is painted with a
    hard-stop gradient instead of a left BorderSide.
    """
    bgcolor = bgcolor or PANEL
    return ft.Container(
        content=content, padding=padding or ft.Padding(left=16, top=12, right=12, bottom=12),
        border_radius=ft.BorderRadius.all(16), border=ft.Border.all(1, border_color or BORDER),
        gradient=ft.LinearGradient(begin=ft.Alignment.CENTER_LEFT, end=ft.Alignment.CENTER_RIGHT,
                                   colors=[color, color, bgcolor, bgcolor], stops=[0.0, 0.012, 0.012, 1.0]),
        on_click=on_click, ink=on_click is not None,
    )


def pill(text, color, icon=None, size=10):
    row = [ft.Icon(getattr(ft.Icons, icon), size=size + 3, color=color)] if icon else []
    row.append(mono(text, size=size, color=color, weight=ft.FontWeight.BOLD))
    return ft.Container(
        content=ft.Row(row, spacing=5, tight=True),
        padding=pad(10, 4), border_radius=ft.BorderRadius.all(20),
        border=ft.Border.all(1, alpha(color, 0.7)), bgcolor=alpha(color, 0.10),
    )


def dot(color, size=8):
    return ft.Container(width=size, height=size, bgcolor=color, border_radius=ft.BorderRadius.all(size))


def primary_button(text, on_click, color=None, icon=None, expand=False, disabled=False):
    color = color or CYAN
    return ft.FilledButton(
        content=ft.Text(text, weight=ft.FontWeight.BOLD, color=on(color)),
        icon=getattr(ft.Icons, icon) if icon else None, icon_color=on(color),
        bgcolor=color, on_click=on_click, expand=expand, disabled=disabled, height=48,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
    )


def ghost_button(text, on_click, color=None, icon=None, expand=False):
    color = color or CYAN
    return ft.OutlinedButton(
        content=ft.Text(text, color=color, weight=ft.FontWeight.W_600),
        icon=getattr(ft.Icons, icon) if icon else None, icon_color=color,
        on_click=on_click, expand=expand, height=48,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12),
                             side=ft.BorderSide(1, alpha(color, 0.6))),
    )


def field(label_text, **kw):
    return ft.TextField(
        label=label_text, border_color=BORDER, focused_border_color=CYAN, color=TEXT,
        label_style=ft.TextStyle(color=MUTED), cursor_color=CYAN, border_radius=12,
        bgcolor=PANEL, filled=True, fill_color=PANEL, **kw,
    )


def theme():
    return ft.Theme(color_scheme=ft.ColorScheme(primary=CYAN, surface=PANEL, on_surface=TEXT, error=RED))


def mode():
    return ft.ThemeMode.LIGHT if name == "light" else ft.ThemeMode.DARK
