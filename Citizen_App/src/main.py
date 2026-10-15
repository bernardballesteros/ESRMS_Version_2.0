"""ESRMS Citizen Emergency App (Flet). Run: flet run src/main.py   Build: flet build apk"""
import logging

import flet as ft

from esrms.config import ConfigError
from esrms.ui import theme as t

logging.basicConfig(level=logging.INFO)


async def main(page: ft.Page):
    page.title = "ESRMS Citizen"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = page.dark_theme = t.theme()
    page.bgcolor = t.BG
    page.padding = 0

    try:
        from esrms.ui.app import CitizenApp
        app = CitizenApp(page)
    except ConfigError as exc:
        page.add(ft.Container(ft.Text(f"Configuration error: {exc}", color=t.RED), padding=24))
        return
    page.data = app  # keep a strong reference for the page's lifetime
    await app.start()


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
