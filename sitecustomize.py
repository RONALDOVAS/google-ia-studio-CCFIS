"""Patch de inicializacao do scraper CGD.

O CGD atual renderiza o formulario de login com inputs que nao expõem
name/id confiaveis. O login legado dependia desses atributos e falhava com
CAMPOS_LOGIN_NAO_ENCONTRADOS. Este patch substitui apenas a autenticacao,
priorizando type/placeholder/aria-label e mantendo o restante do scraper.
"""

import re
from urllib.parse import urlparse


def _patch():
    try:
        import scraper
    except Exception:
        return

    def first_visible(page, selectors):
        for selector in selectors:
            try:
                loc = page.locator(selector)
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible() and item.is_enabled():
                        return item
            except Exception:
                continue
        return None

    def login(page, user, password, unidade):
        if not user or not password:
            raise RuntimeError(f"[{unidade}] CREDENCIAIS_NAO_CONFIGURADAS")

        page.goto(scraper.CGD_LOGIN_URL, wait_until="domcontentloaded", timeout=scraper.PAGE_TIMEOUT_MS)
        page.wait_for_timeout(1200)

        email = first_visible(page, [
            'input[type="email"]',
            'input[placeholder*="e-mail" i]',
            'input[placeholder*="email" i]',
            'input[aria-label*="e-mail" i]',
            'input[aria-label*="email" i]',
            'input[name*="email" i]',
            'input[name*="user" i]',
            'input[name*="login" i]',
            'input[type="text"]',
        ])
        senha = first_visible(page, [
            'input[type="password"]',
            'input[placeholder*="senha" i]',
            'input[aria-label*="senha" i]',
            'input[name*="senha" i]',
            'input[name*="password" i]',
        ])

        if not email or not senha:
            raise RuntimeError(f"[{unidade}] CAMPOS_LOGIN_NAO_ENCONTRADOS_APOS_PATCH: {page.url}")

        email.fill(user)
        senha.fill(password)

        button = first_visible(page, [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Entrar")',
            'button:has-text("Acessar")',
            'button:has-text("Login")',
        ])
        if not button:
            raise RuntimeError(f"[{unidade}] BOTAO_LOGIN_NAO_ENCONTRADO_APOS_PATCH: {page.url}")

        button.click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2200)

        final_path = urlparse(page.url).path.rstrip("/").lower()
        if final_path == "/login" or final_path.startswith("/login/"):
            raise RuntimeError(f"[{unidade}] LOGIN_REJEITADO_OU_SESSAO_NAO_ESTABELECIDA: {page.url}")
        if not scraper.same_host(page.url):
            raise RuntimeError(f"[{unidade}] LOGIN_SAIU_DO_HOST_CGD: {page.url}")

        print(f"[{unidade}] LOGIN OK (patch): {page.url}", flush=True)

    scraper.login = login

_patch()
