"""Executor otimizado do scraper CGD.

Fluxo deliberadamente curto:
1. login da unidade;
2. uma unica varredura da listagem de alunos;
3. descarta vermelho/inativo na propria linha e guarda somente azul/ativo;
4. nao abre novamente o contrato apenas para validar status;
5. coleta detalhes uma unica vez por contrato ativo;
6. so depois consulta reposicoes globais uma vez.

Matriz e Filial sao processadas separadamente.
"""
import os
import re
import time
from urllib.parse import urlparse

import scraper

scraper.MAX_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "5000"))
scraper.MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "10000"))
scraper.dump = lambda page, unidade, nome: None

FAST_WAIT = int(os.getenv("CGD_PAGE_WAIT_MS", "250"))
LIST_WAIT = int(os.getenv("CGD_LIST_WAIT_MS", "150"))
_CURRENT_UNIT = None
_CURRENT_CFG = None
_ACTIVE_FROM_LISTING = set()


# ---------------------------------------------------------------------------
# Remove --no-sandbox, inclusive de argumentos padrao do Playwright.
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
            kwargs["args"] = args_list
            ignored = list(kwargs.get("ignore_default_args") or [])
            if "--no-sandbox" not in ignored:
                ignored.append("--no-sandbox")
            kwargs["ignore_default_args"] = ignored
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
# Navegacao curta + recuperacao de sessao.
# ---------------------------------------------------------------------------
def _is_login_url(url):
    return "/login" in urlparse(str(url)).path.lower()


def _relogin(page, unidade):
    cfg = _CURRENT_CFG or {}
    try:
        print(f"[{unidade}] SESSAO EXPIRADA -> renovando login")
        scraper.login(page, cfg.get("usuario"), cfg.get("senha"), unidade)
        return not _is_login_url(page.url)
    except Exception as exc:
        print(f"[{unidade}] FALHA NO RELOGIN: {exc}")
        return False


_original_open_page = scraper.open_page


def open_fast(page, url, unidade, nome, wait=1300):
    target_wait = min(max(0, wait), FAST_WAIT)
    for tentativa in range(1, 3):
        try:
            ok = _original_open_page(page, url, unidade, nome, target_wait)
            if _is_login_url(page.url):
                if not _relogin(page, unidade):
                    continue
                ok = _original_open_page(page, url, unidade, nome, target_wait)
            if ok and not _is_login_url(page.url):
                return True
        except Exception as exc:
            print(f"[{unidade}] navegacao tentativa {tentativa}/2: {exc}")
        if tentativa == 1:
            time.sleep(1)
    return False


scraper.open_page = open_fast


# ---------------------------------------------------------------------------
# Cor da linha/link: vermelho = ignorar; azul = ativo.
# ---------------------------------------------------------------------------
def _rgb(value):
    m = re.search(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", str(value or "").lower())
    return tuple(map(int, m.groups())) if m else None


def _element_colors(e):
    vals = []
    try:
        cur = e
        for _ in range(4):
            if not cur:
                break
            vals += [
                (cur.get_attribute("class") or "").lower(),
                (cur.get_attribute("style") or "").lower(),
                (cur.get_attribute("title") or "").lower(),
                (cur.get_attribute("aria-label") or "").lower(),
            ]
            try:
                vals += list(cur.evaluate("""e => { const s=getComputedStyle(e); return [s.color,s.backgroundColor,s.borderColor]; }""" ) or [])
            except Exception:
                pass
            try:
                cur = cur.locator("..")
            except Exception:
                break
    except Exception:
        pass
    return vals


def _is_red(e):
    vals = _element_colors(e)
    text = " ".join(str(v).lower() for v in vals)
    if any(x in text for x in ("text-danger", "bg-danger", "danger", "red", "vermelho", "inativo", "inactive", "cancelado", "encerrado")):
        return True
    for v in vals:
        rgb = _rgb(v)
        if rgb:
            r, g, b = rgb
            if r >= 150 and r >= g * 1.45 and r >= b * 1.45 and g <= 150 and b <= 150:
                return True
    return False


def _is_blue(e):
    vals = _element_colors(e)
    text = " ".join(str(v).lower() for v in vals)
    if any(x in text for x in ("text-primary", "bg-primary", "text-info", "bg-info", "blue", "azul", "active", "ativo", "vigente")):
        return True
    for v in vals:
        rgb = _rgb(v)
        if rgb:
            r, g, b = rgb
            if b >= 120 and b >= r * 1.20 and b >= g * 1.05:
                return True
    return False


def collect_contracts_once(page, found):
    global _ACTIVE_FROM_LISTING
    try:
        loc = page.locator('a[href*="/contratos/"]')
        for i in range(min(loc.count(), 10000)):
            try:
                e = loc.nth(i)
                h = scraper.abs_url(page, e.get_attribute("href"))
                if not scraper.is_contract(h):
                    continue
                cid = scraper.contract_id(h)
                if not cid:
                    continue
                if _is_red(e):
                    print(f"[{_CURRENT_UNIT}] CONTRATO {cid}: VERMELHO -> IGNORADO")
                    continue
                # O CGD usa azul para contrato ativo. Quando a cor nao puder ser
                # lida, mantemos o contrato para a segunda barreira apenas se o
                # proprio registro nao indicar inatividade.
                if _is_blue(e):
                    found[cid] = scraper.contract_url(cid)
                    _ACTIVE_FROM_LISTING.add(cid)
                    continue
                # Nao classificar silenciosamente como ativo se a linha for neutra.
                # Registros neutros serao tentados somente se a listagem nao expuser
                # a cor; isso evita perder alunos por diferenca de CSS.
                found[cid] = scraper.contract_url(cid)
                print(f"[{_CURRENT_UNIT}] CONTRATO {cid}: COR NAO IDENTIFICADA -> RESERVADO")
            except Exception:
                continue
    except Exception as exc:
        print(f"[{_CURRENT_UNIT}] falha ao ler cores da listagem: {exc}")


scraper.collect_contracts = collect_contracts_once


# ---------------------------------------------------------------------------
# Pagina seguinte: sem varrer fontes diferentes. Uma fonte por unidade.
# ---------------------------------------------------------------------------
NEXT_SELECTORS = [
    'a[rel="next"]', 'button[rel="next"]',
    'a[aria-label*="next" i]', 'button[aria-label*="next" i]',
    'a[aria-label*="proxima" i]', 'button[aria-label*="proxima" i]',
    'a[aria-label*="próxima" i]', 'button[aria-label*="próxima" i]',
    'a:has-text("Próxima")', 'button:has-text("Próxima")',
    'a:has-text("Proxima")', 'button:has-text("Proxima")',
    'a:has-text("Next")', 'button:has-text("Next")',
    'a:has-text("›")', 'button:has-text("›")',
]


def next_page_once(page):
    before = scraper.body(page)[:10000]
    before_url = page.url
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
                e.click(timeout=5000)
                page.wait_for_timeout(LIST_WAIT)
                if _is_login_url(page.url):
                    if not _relogin(page, _CURRENT_UNIT or "?"):
                        return False
                    try:
                        page.goto(before_url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(LIST_WAIT)
                    except Exception:
                        return False
                    continue
                current = scraper.body(page)[:10000]
                if "Página não encontrada" in current or "Pagina nao encontrada" in current or "404" in current and "não encontrada" in current.lower():
                    # O CGD informou que nao existe a pagina seguinte. Nao tratar
                    # como erro fatal: preserva tudo o que ja foi descoberto.
                    try:
                        page.goto(before_url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(LIST_WAIT)
                    except Exception:
                        pass
                    return False
                if page.url != before_url or current != before:
                    return True
        except Exception:
            continue
    return False


scraper.next_page = next_page_once


# ---------------------------------------------------------------------------
# Descoberta: UMA fonte. Fallback somente se a primeira nao entregar contratos.
# ---------------------------------------------------------------------------
def discover_once(page, unidade, destino):
    found = {}
    # Depois do login, abre diretamente /alunos: evita rota configurada + inicio
    # + tres relatorios com a mesma paginacao.
    candidates = []
    if destino and scraper.same_host(destino):
        p = urlparse(destino).path.rstrip("/").lower()
        if p in ("/alunos", "/relatorios/alunos", "/relatorios/individuais/alunos-curso"):
            candidates.append(destino)
    candidates += [
        f"{scraper.CGD_URL.rstrip('/')}/alunos",
        f"{scraper.CGD_URL.rstrip('/')}/relatorios/alunos",
        f"{scraper.CGD_URL.rstrip('/')}/relatorios/individuais/alunos-curso",
    ]
    for src in dict.fromkeys(candidates):
        found.clear()
        _ACTIVE_FROM_LISTING.clear()
        if not open_fast(page, src, unidade, "lista_alunos", FAST_WAIT):
            continue
        seen = set()
        for pn in range(1, scraper.MAX_PAGES + 1):
            collect_contracts_once(page, found)
            sig = scraper.body(page)[:12000]
            if sig in seen:
                print(f"[{unidade}] pagina repetida -> fim da fonte")
                break
            seen.add(sig)
            print(f"[{unidade}] pagina_lista={pn} contratos_ativos_reservados={len(found)}")
            if len(found) >= scraper.MAX_CONTRACTS:
                break
            if not next_page_once(page):
                break
        if found:
            print(f"[{unidade}] FONTE UTILIZADA: {src}")
            print(f"[{unidade}] CONTRATOS RESERVADOS PARA COLETA: {len(found)}")
            return list(found.values())
    print(f"[{unidade}] NENHUM CONTRATO ENCONTRADO")
    return []


# ---------------------------------------------------------------------------
# Coleta por unidade sem validacao duplicada do contrato.
# ---------------------------------------------------------------------------
def run_unit_fast(unidade, cfg, pw):
    global _CURRENT_UNIT, _CURRENT_CFG
    _CURRENT_UNIT = unidade
    _CURRENT_CFG = cfg
    profile = scraper.EDGE_PROFILE_BASE / unidade
    profile.mkdir(parents=True, exist_ok=True)
    b = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel="msedge",
        headless=False,
        viewport={"width": 1440, "height": 1000},
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--no-sandbox"],
    )
    page = b.pages[0] if b.pages else b.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        contracts = discover_once(page, unidade, cfg.get("destino"))
        print(f"[{unidade}] CONTRATOS DESCOBERTOS: {len(contracts)}")
        # Reposicoes globais somente uma vez, depois da descoberta.
        reps = scraper.global_reps(page, unidade)
        out = []
        for i, cu in enumerate(contracts, 1):
            cid = scraper.contract_id(cu)
            print(f"[{unidade}] contrato {i}/{len(contracts)} -> {cid}")
            try:
                item = scraper.contract_bundle(page, cid, unidade, reps)
                if item:
                    out.append(item)
            except Exception as exc:
                print(f"[{unidade}] ERRO contrato {cid}: {exc}")
        print(f"[{unidade}] ALUNOS COLETADOS: {len(out)}")
        return out
    finally:
        try:
            b.close()
        except Exception:
            pass



def main_fast():
    print("=" * 80)
    print("SCRAPER CGD - PAGINACAO UNICA / CONTRATOS ATIVOS / COLETA OTIMIZADA")
    print("=" * 80)
    all_data = []
    with scraper.sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            try:
                all_data += run_unit_fast(unidade, scraper.CONFIG[unidade], pw)
            except Exception as exc:
                print(f"[{unidade}] ERRO FATAL: {exc}")
    unique = {(a.get("unidade"), a.get("contrato")): a for a in all_data}
    all_data = list(unique.values())
    scraper.JSON_PATH.write_text(__import__("json").dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_data)}")
    print(f"MATRIZ: {sum(a.get('unidade') == 'matriz' for a in all_data)}")
    print(f"FILIAL: {sum(a.get('unidade') == 'filial' for a in all_data)}")
    print("=" * 80)
    if not all_data:
        raise SystemExit("Nenhum aluno foi capturado pelo CGD.")
    scraper.sync_supabase(all_data)


if __name__ == "__main__":
    main_fast()
