"""Entrypoint operacional do sincronizador CGD usando Chromium do Playwright.

O scraper legado ainda chama chromium.launch(channel="msedge"). O runner Windows,
porém, deve usar o Chromium empacotado pelo Playwright para evitar regressões ligadas
ao Edge instalado/políticas do navegador. Este wrapper remove somente o argumento
channel; toda a lógica de descoberta, comparação, captura e persistência permanece
no scraper_sync_incremental.py.
"""
from playwright.sync_api import BrowserType

_original_launch = BrowserType.launch


def _launch_without_brand_channel(self, *args, **kwargs):
    kwargs.pop("channel", None)
    return _original_launch(self, *args, **kwargs)


BrowserType.launch = _launch_without_brand_channel

import scraper_sync_incremental as target


if __name__ == "__main__":
    target.main()
