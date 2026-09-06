"""Runner CGD: listagem HTTP rapida + detalhamento autenticado e parser robusto de frequencia."""

import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
import scraper
from playwright.sync_api import sync_playwright

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "831")))
LISTING_HTTP_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "4")))
LISTING_TIMEOUT_S = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
DETAIL_TIMEOUT_S = max(30, int(os.getenv("CGD_DETAIL_TIMEOUT_S", "120")))
DETAIL_RETRIES = max(0, int(os.getenv("CGD_DETAIL_RETRIES", "1")))
MAX_CONTRACTS = scraper.MAX_CONTRACTS
LISTING_SOURCE = "https://app.cgd.com.br/alunos"


def _plain(value):
    text = " ".join(str(value or "").replace("\xa0", " ").split())
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn").lower()


def _listing_source(page, destino):
    return LISTING_SOURCE


def _page_url(source, page_number):
    parsed = urlparse(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _session_from_page(page):
    session = requests.Session()
    for cookie in page.context.cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
    try:
        user_agent = page.evaluate("() => navigator.userAgent")
    except Exception:
        user_agent = None
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    if user_agent:
        session.headers["User-Agent"] = user_agent
    return session


def _extract_contract_ids(html):
    return set(re.findall(r"/contratos/(\d+)", html or "", flags=re.IGNORECASE))


def _fetch_listing(args):
    unidade, url, cookies, headers = args
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(headers)
    response = session.get(url, timeout=LISTING_TIMEOUT_S, allow_redirects=True)
    final_path = urlparse(response.url).path.rstrip("/").lower()
    if final_path.startswith("/login") or "/login" in final_path:
        raise RuntimeError(f"sessao redirecionada para login: {url}")
    response.raise_for_status()
    return unidade, url, _extract_contract_ids(response.text), len(response.text)


def optimized_discover_contracts(page, unidade, destino):
    source = _listing_source(page, destino)
    print(f"[{unidade}] FONTE_LISTAGEM_FIXA: {source}", flush=True)
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {source} final={page.url}")
    first_ids = _extract_contract_ids(page.content())
    print(f"[{unidade}] LISTAGEM REAL: {page.url} contratos_p1={len(first_ids)}", flush=True)
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS: {page.url}")
    session = _session_from_page(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    urls = [_page_url(source, n) for n in range(1, LISTING_PAGES + 1)]
    found = {cid: scraper.contract_url(cid) for cid in first_ids}
    print(f"[{unidade}] PAGINACAO_HTTP_DIRETA iniciado paginas=1..{LISTING_PAGES} workers={LISTING_HTTP_WORKERS}", flush=True)
    completed, errors = 1, 0
    with ThreadPoolExecutor(max_workers=LISTING_HTTP_WORKERS) as pool:
        futures = {pool.submit(_fetch_listing, (unidade, url, cookies, headers)): url for url in urls[1:]}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, _, ids, body_size = future.result()
                before = len(found)
                for cid in ids:
                    found[cid] = scraper.contract_url(cid)
                completed += 1
                page_number = parse_qs(urlparse(url).query).get("page", ["?"])[0]
                if completed % 10 == 0 or page_number == str(LISTING_PAGES):
                    print(f"[{unidade}] pagina_lista={page_number}/{LISTING_PAGES} contratos_acumulados={len(found)} novos={len(found)-before} bytes={body_size}", flush=True)
            except Exception as exc:
                completed += 1
                errors += 1
                print(f"[{unidade}] pagina_lista_ERRO url={url}: {exc}", flush=True)
    print(f"[{unidade}] PAGINACAO_HTTP_FINAL paginas={completed}/{LISTING_PAGES} contratos={len(found)} erros={errors}", flush=True)
    if errors >= max(1, LISTING_PAGES // 2):
        raise RuntimeError(f"[{unidade}] LISTAGEM_HTTP_DEMASIADOS_ERROS: {errors}/{LISTING_PAGES}")
    return list(found.values())[:MAX_CONTRACTS]


def robust_extract_frequency(page, cid):
    """Extrai frequencia diretamente do DOM real, sem depender do nome do cabecalho."""
    records = []
    faltas = 0
    presencas = 0
    absence = ("faltou", "falta", "ausente", "nao compareceu", "nao comparecimento")
    presence = ("presente", "presenca", "compareceu")

    def classify(text):
        t = _plain(text)
        if any(x in t for x in absence):
            return "falta"
        if any(x in t for x in presence):
            return "presenca"
        return ""

    try:
        rows = page.locator("table tr")
        for i in range(rows.count()):
            tr = rows.nth(i)
            cells = tr.locator("td")
            if cells.count() == 0:
                continue
            values = []
            rich = []
            for j in range(cells.count()):
                cell = cells.nth(j)
                text = " ".join(cell.all_text_contents()).strip()
                attrs = " ".join(filter(None, [cell.get_attribute("title"), cell.get_attribute("aria-label"), cell.get_attribute("class")]))
                values.append(" ".join(x for x in (text, attrs) if x))
                rich.append(text)
            joined = " | ".join(values)
            kind = classify(joined)
            if not kind:
                continue
            if kind == "falta":
                faltas += 1
            else:
                presencas += 1
            date_value = next((v for v in rich if re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", v)), None)
            records.append({
                "data": date_value,
                "status": "Faltou" if kind == "falta" else "Presente",
                "aluno": None,
                "classificacao": kind,
                "valores": rich,
                "cabecalhos": [],
            })
    except Exception as exc:
        print(f"[FREQUENCIA] cid={cid} erro_dom={exc!r}", flush=True)

    # Fallback para o texto renderizado quando o CGD nao usa uma tabela convencional.
    if not records:
        try:
            text = page.locator("body").inner_text(timeout=5000)
            lines = [" ".join(x.split()) for x in text.splitlines() if x.strip()]
            for idx, line in enumerate(lines):
                kind = classify(line)
                if not kind:
                    continue
                if kind == "falta":
                    faltas += 1
                else:
                    presencas += 1
                date_value = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", line)
                records.append({
                    "data": date_value.group(0) if date_value else None,
                    "status": "Faltou" if kind == "falta" else "Presente",
                    "aluno": None,
                    "classificacao": kind,
                    "valores": [line],
                    "cabecalhos": [],
                })
        except Exception as exc:
            print(f"[FREQUENCIA] cid={cid} erro_texto={exc!r}", flush=True)

    print(f"[FREQUENCIA] cid={cid} faltas={faltas} presencas={presencas} registros={len(records)}", flush=True)
    if not records:
        raise RuntimeError(f"FREQUENCIA_NAO_CAPTURADA cid={cid} url={page.url}")
    return {"faltas": faltas, "presencas": presencas, "registros": records}


class Progress:
    def __init__(self):
        self.start = time.monotonic()
        self.total = 0
        self.done = 0
        self.ok = 0
        self.fail = 0

    def set_total(self, total):
        self.total = max(self.total, total)

    def update(self, ok=True, label=""):
        self.done += 1
        if ok:
            self.ok += 1
        else:
            self.fail += 1
        elapsed = max(0.001, time.monotonic() - self.start)
        rate = self.done / elapsed
        remaining = max(0, self.total - self.done)
        eta = remaining / rate if rate else 0
        width = 22
        pct = (self.done / self.total * 100) if self.total else 0
        filled = min(width, int(width * pct / 100))
        bar = "█" * filled + "░" * (width - filled)
        def fmt(sec):
            sec = int(max(0, sec)); return f"{sec//60:02d}:{sec%60:02d}"
        msg = f"\r[{bar}] {pct:5.1f}% | {self.done}/{self.total} | OK:{self.ok} F:{self.fail} | {rate:.2f} aluno/s | decorrido {fmt(elapsed)} | ETA {fmt(eta)}"
        if label:
            msg += f" | {label}"
        sys.stdout.write(msg[:260].ljust(260))
        sys.stdout.flush()
        if self.done >= self.total:
            sys.stdout.write("\n")
            sys.stdout.flush()


progress = Progress()
_original_extract_frequency = scraper.extract_frequency
scraper.extract_frequency = robust_extract_frequency


def _persistent_detail_round(u, cfg, contracts, reps, storage_state, attempt):
    results, failed = [], []
    if not contracts:
        return results, failed
    progress.set_total(6 if os.getenv("CGD_DIAGNOSTICO", "0") in ("1", "true", "yes", "sim") else len(contracts) * 2)
    print(f"[{u}] DETALHAMENTO: {len(contracts)} contratos / Edge autenticado", flush=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        try:
            for index, contract in enumerate(contracts, 1):
                cid = scraper.contract_id(contract)
                if not cid:
                    continue
                try:
                    result = scraper.contract_bundle(page, cid, u, reps)
                    results.append(result)
                    progress.update(True, f"{u} contrato={cid}")
                    print(f"[{u}] CONTRATO_OK {index}/{len(contracts)} cid={cid} nome={result.get('nome')} faltas={result.get('faltas')} presencas={result.get('presencas')} freq_registros={len(result.get('frequencia_raw') or [])}", flush=True)
                except Exception as exc:
                    failed.append(cid)
                    progress.update(False, f"{u} contrato={cid} FALHA")
                    print(f"[{u}] CONTRATO_ERRO {index}/{len(contracts)} cid={cid}: {exc}", flush=True)
        finally:
            context.close()
            browser.close()
    return results, failed


def safe_process_details(u, cfg, contracts, reps, storage_state):
    if not contracts:
        return []
    pending = list(contracts)
    results = []
    for attempt in range(1, DETAIL_RETRIES + 2):
        if not pending:
            break
        print(f"[{u}] INICIO DETALHAMENTO: rodada={attempt} pendentes={len(pending)}", flush=True)
        batch_results, failed_ids = _persistent_detail_round(u, cfg, pending, reps, storage_state, attempt)
        results.extend(batch_results)
        pending = [scraper.contract_url(cid) for cid in failed_ids if cid]
        if pending and attempt <= DETAIL_RETRIES:
            print(f"[{u}] RETENTATIVA: {len(pending)} contratos", flush=True)
    print(f"[{u}] DETALHAMENTO FINALIZADO: sucesso={len(results)} falhas={len(pending)} de={len(contracts)}", flush=True)
    for cu in pending:
        print(f"[{u}] CONTRATO_NAO_CAPTURADO: {cu}", flush=True)
    return results


scraper.discover_contracts = optimized_discover_contracts
scraper.process_details = safe_process_details


if __name__ == "__main__":
    scraper.main()
