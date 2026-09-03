"""Camada de execucao robusta do scraper CGD.

Objetivos desta camada:
- suportar paginacao longa sem parar artificialmente em 200/300 paginas;
- recuperar automaticamente a sessao quando o CGD redirecionar para /login;
- remover o argumento --no-sandbox do Edge/Playwright;
- reduzir esperas desnecessarias para acelerar a coleta;
- nao gerar HTML/TXT/PNG durante a descoberta;
- validar contrato ativo antes da coleta detalhada;
- manter Matriz e Filial separadas.
"""
import os
import re
import time

import scraper

# Limites seguros para a base real. Podem ser sobrescritos pelo workflow.
scraper.MAX_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "5000"))
scraper.MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "10000"))

# A descoberta nao deve criar milhares de arquivos de diagnostico.
scraper.dump = lambda page, unidade, nome: None

# Espera curta: o goto ja usa domcontentloaded. O valor pode ser ajustado no workflow.
FAST_WAIT = int(os.getenv("CGD_PAGE_WAIT_MS", "500"))
LIST_WAIT = int(os.getenv("CGD_LIST_WAIT_MS", "350"))

# Unidade/credenciais da execucao atual. Usado para renovar sessao automaticamente.
_CURRENT_UNIT = None
_CURRENT_CFG = None


# ---------------------------------------------------------------------------
# 1) Remove --no-sandbox do Playwright/Edge
# ---------------------------------------------------------------------------
class _BrowserTypeProxy:
    def __init__(self, browser_type):
        self._browser_type = browser_type

    def __getattr__(self, name):
        attr = getattr(self._browser_type, name)
        if name not in ("launch", "launch_persistent_context"):
            return attr

        def wrapped(*args, **kwargs):
            args_list = list(kwargs.get("args") or [])
            args_list = [a for a in args_list if str(a).strip().lower() != "--no-sandbox"]
            if args_list:
                kwargs["args"] = args_list
            else:
                kwargs.pop("args", None)
            return attr(*args, **kwargs)

        return wrapped


class _PlaywrightProxy:
    def __init__(self, playwright):
        self._playwright = playwright
        self.chromium = _BrowserTypeProxy(playwright.chromium)

    def __getattr__(self, name):
        return getattr(self._playwright, name)


class _SyncPlaywrightProxy:
    def __init__(self, manager):
        self._manager = manager
        self._playwright = None

    def __enter__(self):
        self._playwright = self._manager.__enter__()
        return _PlaywrightProxy(self._playwright)

    def __exit__(self, exc_type, exc, tb):
        return self._manager.__exit__(exc_type, exc, tb)


_original_sync_playwright = scraper.sync_playwright


def sync_playwright_without_sandbox():
    return _SyncPlaywrightProxy(_original_sync_playwright())


scraper.sync_playwright = sync_playwright_without_sandbox


# ---------------------------------------------------------------------------
# 2) Sessao: detectar /login e refazer login automaticamente
# ---------------------------------------------------------------------------
def _is_login_url(url):
    try:
        return "/login" in (urlparse(url).path.lower())
    except Exception:
        return "/login" in str(url).lower()


# Import local para nao alterar o scraper principal.
from urllib.parse import urlparse


def _relogin(page, unidade):
    cfg = _CURRENT_CFG or {}
    user = cfg.get("usuario")
    password = cfg.get("senha")
    if not user or not password:
        print(f"[{unidade}] SESSAO EXPIRADA, mas credenciais da unidade nao estao disponiveis.")
        return False
    try:
        print(f"[{unidade}] SESSAO EXPIRADA -> renovando login automaticamente")
        scraper.login(page, user, password, unidade)
        return not _is_login_url(page.url)
    except Exception as exc:
        print(f"[{unidade}] FALHA AO RENOVAR LOGIN: {exc}")
        return False


_original_open_page = scraper.open_page


def robust_open_page(page, url, unidade, nome, wait=1300):
    # No discovery/detalhamento, usar espera curta; evita minutos acumulados.
    target_wait = FAST_WAIT if wait > FAST_WAIT else wait
    for tentativa in range(1, 4):
        try:
            ok = _original_open_page(page, url, unidade, nome, target_wait)
            if ok and not _is_login_url(page.url):
                return True

            # O CGD pode redirecionar para /login/contratos/<id> quando a sessao cai.
            if _is_login_url(page.url):
                if _relogin(page, unidade):
                    # Reabre exatamente a rota que estava sendo processada.
                    ok2 = _original_open_page(page, url, unidade, nome, target_wait)
                    if ok2 and not _is_login_url(page.url):
                        return True
        except Exception as exc:
            print(f"[{unidade}] tentativa {tentativa}/3 falhou em {url}: {exc}")
        if tentativa < 3:
            time.sleep(min(2 * tentativa, 4))
    print(f"[{unidade}] NAVEGACAO NAO RECUPERADA: {url}")
    return False


scraper.open_page = robust_open_page


# ---------------------------------------------------------------------------
# 3) Paginacao rapida e resistente
# ---------------------------------------------------------------------------
NEXT_SELECTORS = [
    'a[rel="next"]',
    'button[rel="next"]',
    'a[aria-label*="next" i]',
    'button[aria-label*="next" i]',
    'a[aria-label*="proxima" i]',
    'button[aria-label*="proxima" i]',
    'a[aria-label*="próxima" i]',
    'button[aria-label*="próxima" i]',
    'a:has-text("Próxima")',
    'button:has-text("Próxima")',
    'a:has-text("Proxima")',
    'button:has-text("Proxima")',
    'a:has-text("Next")',
    'button:has-text("Next")',
    'a:has-text("›")',
    'button:has-text("›")',
]


def fast_next_page(page):
    before_url = page.url
    before = scraper.body(page)[:12000]

    for sel in NEXT_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                e = loc.nth(i)
                if not e.is_visible():
                    continue
                if (e.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                if "disabled" in (e.get_attribute("class") or "").lower():
                    continue

                e.click(timeout=8000)
                # Aguarda apenas o suficiente para a pagina mudar.
                for pause in (LIST_WAIT, 500, 900):
                    page.wait_for_timeout(pause)
                    if _is_login_url(page.url):
                        if not _relogin(page, _CURRENT_UNIT or "?"):
                            return False
                        # Recupera a pagina que estava aberta antes da perda da sessao.
                        try:
                            page.goto(before_url, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(LIST_WAIT)
                        except Exception:
                            return False
                        # Nao clicar novamente dentro deste elemento; o loop externo
                        # continuara tentando a proxima pagina de forma segura.
                        break
                    if page.url != before_url or scraper.body(page)[:12000] != before:
                        return True
        except Exception:
            continue
    return False


scraper.next_page = fast_next_page


# ---------------------------------------------------------------------------
# 4) Filtro de contratos ativos
# ---------------------------------------------------------------------------
INACTIVE = (
    "inativo", "inativa", "encerrado", "encerrada", "cancelado", "cancelada",
    "suspenso", "suspensa", "rescindido", "rescindida", "finalizado", "finalizada",
    "concluido", "concluida", "concluído", "concluída",
)
ACTIVE = (
    "ativo", "ativa", "vigente", "em andamento", "em curso", "cursando",
)


def status_values(page):
    values = []
    try:
        for heads, rows in scraper.table_data(page):
            for i, h in enumerate(heads):
                hlow = scraper.low(h)
                if any(k in hlow for k in ("status", "situação", "situacao", "estado")):
                    for row in rows:
                        if i < len(row):
                            values.append(scraper.norm(row[i]))
    except Exception:
        pass

    try:
        for sel in (
            'select[name*="status" i]', 'select[name*="situacao" i]',
            'select[name*="situação" i]', 'select[id*="status" i]',
            'select[id*="situacao" i]', 'input[name*="status" i]',
            'input[name*="situacao" i]', 'input[id*="status" i]',
            'input[id*="situacao" i]',
        ):
            loc = page.locator(sel)
            for i in range(loc.count()):
                try:
                    e = loc.nth(i)
                    if e.is_visible():
                        value = e.input_value()
                        if value:
                            values.append(scraper.norm(value))
                except Exception:
                    pass
    except Exception:
        pass

    txt = scraper.body(page)
    for pat in (
        r"(?:situação|situacao|status|estado)\s*[:\-]\s*([^\n|]{1,80})",
        r"(?:situação|situacao|status|estado)[^\n]{0,30}\b(ativo|inativo|encerrado|cancelado|suspenso|vigente)\b",
    ):
        for m in re.finditer(pat, txt, re.I):
            values.append(scraper.norm(m.group(1)))
    return [v for v in values if v]


def contract_is_active(page, cid, unidade):
    values = status_values(page)
    joined = " | ".join(scraper.low(v) for v in values)
    if any(term in joined for term in INACTIVE):
        print(f"[{unidade}] CONTRATO {cid}: INATIVO -> IGNORADO | status={values[:5]}")
        return False
    if any(term in joined for term in ACTIVE):
        print(f"[{unidade}] CONTRATO {cid}: ATIVO -> COLETAR | status={values[:5]}")
        return True
    print(f"[{unidade}] CONTRATO {cid}: STATUS NAO IDENTIFICADO -> IGNORADO | valores={values[:8]}")
    return False


_original_contract_bundle = scraper.contract_bundle


def active_contract_bundle(page, cid, unidade, reps):
    # Primeira visita: somente status. So contrato ativo entra na coleta pesada.
    url = scraper.contract_url(cid)
    if not robust_open_page(page, url, unidade, f"validacao_status_{cid}", wait=FAST_WAIT):
        print(f"[{unidade}] CONTRATO {cid}: nao foi possivel validar status -> IGNORADO")
        return None
    if not contract_is_active(page, cid, unidade):
        return None
    return _original_contract_bundle(page, cid, unidade, reps)


scraper.contract_bundle = active_contract_bundle


# ---------------------------------------------------------------------------
# 5) Estatisticas por unidade
# ---------------------------------------------------------------------------
_original_run_unit = scraper.run_unit


def active_run_unit(unidade, cfg, pw):
    global _CURRENT_UNIT, _CURRENT_CFG
    _CURRENT_UNIT = unidade
    _CURRENT_CFG = cfg
    print(f"[{unidade}] INICIO DA COLETA | paginacao_max={scraper.MAX_PAGES} | contratos_max={scraper.MAX_CONTRACTS}")
    result = _original_run_unit(unidade, cfg, pw)
    result = [item for item in result if item]
    print(f"[{unidade}] RESULTADOS VALIDOS APOS FILTRO DE ATIVOS: {len(result)}")
    return result


scraper.run_unit = active_run_unit


if __name__ == "__main__":
    scraper.main()
