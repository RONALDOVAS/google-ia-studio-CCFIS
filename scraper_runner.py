"""Executor otimizado do scraper CGD.

A listagem e percorrida uma unica vez por unidade. A cor da linha e lida em
lote no DOM: vermelho/inativo e descartado; azul/ativo e reservado. Depois da
listagem, cada contrato ativo e aberto somente uma vez para os detalhes.
"""
import os
import re
import time
from urllib.parse import urlparse

import scraper

scraper.MAX_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "5000"))
scraper.MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "10000"))
scraper.dump = lambda page, unidade, nome: None
FAST_WAIT = int(os.getenv("CGD_PAGE_WAIT_MS", "180"))
LIST_WAIT = int(os.getenv("CGD_LIST_WAIT_MS", "100"))
_CURRENT_UNIT = None
_CURRENT_CFG = None


# ---------------------------------------------------------------------------
# Remove --no-sandbox dos argumentos fornecidos e dos argumentos padrao.
# ---------------------------------------------------------------------------
class _BrowserTypeProxy:
    def __init__(self, browser_type):
        self._browser_type = browser_type

    def __getattr__(self, name):
        attr = getattr(self._browser_type, name)
        if name not in ("launch", "launch_persistent_context"):
            return attr

        def wrapped(*args, **kwargs):
            args_list = [a for a in list(kwargs.get("args") or []) if str(a).strip().lower() != "--no-sandbox"]
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
scraper.sync_playwright = lambda: _SyncPlaywrightProxy(_original_sync_playwright())


# ---------------------------------------------------------------------------
# Navegacao curta e recuperacao da sessao.
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
            print(f"[{unidade}] navegacao {tentativa}/2: {exc}")
        if tentativa == 1:
            time.sleep(0.7)
    return False


scraper.open_page = open_fast


# ---------------------------------------------------------------------------
# Leitura de contratos em lote: uma chamada JS por pagina, nao dezenas.
# ---------------------------------------------------------------------------
def _classify_color(item):
    text = " ".join(str(x or "").lower() for x in (
        item.get("class"), item.get("style"), item.get("title"), item.get("aria"),
        item.get("rowClass"), item.get("rowStyle"), item.get("rowColor")
    ))
    if any(x in text for x in (
        "text-danger", "bg-danger", "danger", "vermelho", "red",
        "inativo", "inactive", "cancelado", "encerrado"
    )):
        return "red"
    if any(x in text for x in (
        "text-primary", "bg-primary", "text-info", "bg-info", "azul", "blue",
        "ativo", "active", "vigente"
    )):
        return "blue"
    for key in ("color", "backgroundColor", "borderColor", "rowColor"):
        value = str(item.get(key) or "")
        m = re.search(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", value.lower())
        if not m:
            continue
        r, g, b = map(int, m.groups())
        if r >= 150 and r >= g * 1.45 and r >= b * 1.45 and g <= 150 and b <= 150:
            return "red"
        if b >= 120 and b >= r * 1.20 and b >= g * 1.05:
            return "blue"
    return "unknown"


def collect_contracts_batch(page, found):
    global _CURRENT_UNIT
    try:
        items = page.locator('a[href*="/contratos/"]').evaluate_all("""
            els => els.map(a => {
                const tr = a.closest('tr');
                const nodes = [a, tr, tr && tr.parentElement].filter(Boolean);
                const pick = (n, p) => nodes.map(x => p === 'style' ? x.getAttribute('style') : x.getAttribute(p)).filter(Boolean).join(' ');
                const styles = nodes.map(x => { const s=getComputedStyle(x); return [s.color,s.backgroundColor,s.borderColor].join(' '); }).join(' ');
                return {
                    href: a.href,
                    class: pick(a,'class'), style: pick(a,'style'), title: pick(a,'title'), aria: pick(a,'aria-label'),
                    rowClass: tr ? tr.getAttribute('class') : '', rowStyle: tr ? tr.getAttribute('style') : '',
                    rowColor: styles, color: getComputedStyle(a).color,
                    backgroundColor: getComputedStyle(a).backgroundColor,
                    borderColor: getComputedStyle(a).borderColor
                };
            })
        """)
    except Exception as exc:
        print(f"[{_CURRENT_UNIT}] leitura DOM em lote falhou: {exc}")
        return

    for item in items or []:
        try:
            href = scraper.abs_url(page, item.get("href"))
            if not scraper.is_contract(href):
                continue
            cid = scraper.contract_id(href)
            if not cid or cid in found:
                continue
            color = _classify_color(item)
            if color == "red":
                print(f"[{_CURRENT_UNIT}] CONTRATO {cid}: VERMELHO -> IGNORADO")
                continue
            if color == "blue":
                found[cid] = scraper.contract_url(cid)
                continue
            # A listagem real normalmente usa azul/vermelho. Se o CSS estiver
            # neutro por algum motivo, nao abrimos o contrato: ele nao entra na
            # coleta automatica. Isso evita gastar tempo com registros duvidosos.
            print(f"[{_CURRENT_UNIT}] CONTRATO {cid}: COR NAO IDENTIFICADA -> IGNORADO")
        except Exception:
            continue


scraper.collect_contracts = collect_contracts_batch


# ---------------------------------------------------------------------------
# Paginacao sem repetir tres fontes. Fallback somente se a primeira nao tiver
# nenhum contrato, nunca para revarrer uma lista ja populada.
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
                e.click(timeout=4000)
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
                if "página não encontrada" in current.lower() or "pagina nao encontrada" in current.lower():
                    try:
                        page.goto(before_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    print(f"[{_CURRENT_UNIT}] CGD devolveu 404 na pagina seguinte; preservando contratos ja lidos")
                    return False
                if page.url != before_url or current != before:
                    return True
        except Exception:
            continue
    return False


scraper.next_page = next_page_once


def discover_once(page, unidade, destino):
    found = {}
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
        if not open_fast(page, src, unidade, "lista_alunos", FAST_WAIT):
            continue
        seen = set()
        for pn in range(1, scraper.MAX_PAGES + 1):
            scraper.collect_contracts(page, found)
            sig = scraper.body(page)[:12000]
            if sig in seen:
                print(f"[{unidade}] pagina repetida -> fim")
                break
            seen.add(sig)
            if pn == 1 or pn % 25 == 0:
                print(f"[{unidade}] pagina_lista={pn} contratos_azuis={len(found)}")
            if len(found) >= scraper.MAX_CONTRACTS:
                break
            if not scraper.next_page(page):
                break
        if found:
            print(f"[{unidade}] FONTE UTILIZADA: {src}")
            print(f"[{unidade}] CONTRATOS AZUIS/ATIVOS RESERVADOS: {len(found)}")
            return list(found.values())
    return []


# ---------------------------------------------------------------------------
# Uma coleta por contrato ativo. Nao existe mais validacao de status +
# contract_bundle em seguida: a cor da listagem ja fez o filtro.
# ---------------------------------------------------------------------------
def run_unit_fast(unidade, cfg, pw):
    global _CURRENT_UNIT, _CURRENT_CFG
    _CURRENT_UNIT = unidade
    _CURRENT_CFG = cfg
    profile = scraper.EDGE_PROFILE_BASE / unidade
    profile.mkdir(parents=True, exist_ok=True)
    b = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile), channel="msedge", headless=False,
        viewport={"width": 1440, "height": 1000},
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--no-sandbox"],
    )
    page = b.pages[0] if b.pages else b.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        contracts = discover_once(page, unidade, cfg.get("destino"))
        print(f"[{unidade}] CONTRATOS ATIVOS PARA DETALHAMENTO: {len(contracts)}")
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
    print("SCRAPER CGD - UMA PAGINACAO / SOMENTE AZUIS / SEM DUPLICACAO")
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
