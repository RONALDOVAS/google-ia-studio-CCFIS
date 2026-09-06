"""Executor CGD: listagem HTTP + detalhamento HTTP paralelo autenticado.

O Edge autentica a conta e fornece os cookies da sessao. Depois disso, os
contratos sao coletados diretamente por HTTP em paralelo, sem abrir um Edge
para cada aluno e sem navegar quatro paginas por contrato. O parser usa
BeautifulSoup e preserva a estrutura de dados produzida pelo scraper.
"""

import os
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import scraper

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "831")))
LISTING_HTTP_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "12")))
LISTING_TIMEOUT_S = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "20")))
DETAIL_HTTP_WORKERS = max(1, int(os.getenv("CGD_DETAIL_HTTP_WORKERS", os.getenv("CGD_DETAIL_WORKERS", "12"))))
DETAIL_TIMEOUT_S = max(10, int(os.getenv("CGD_DETAIL_TIMEOUT_S", "45")))
DETAIL_RETRIES = max(0, int(os.getenv("CGD_DETAIL_RETRIES", "1")))
DETAIL_LIMIT = max(0, int(os.getenv("CGD_DETAIL_LIMIT", "0")))
MAX_CONTRACTS = scraper.MAX_CONTRACTS
LISTING_SOURCE = "https://app.cgd.com.br/alunos"
HEADLESS = os.getenv("CGD_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}


class SessionExpired(RuntimeError):
    pass


def _page_url(source, page_number):
    parsed = urlparse(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _session_from_browser(page):
    session = requests.Session()
    for cookie in page.context.cookies():
        try:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
        except Exception:
            session.cookies.set(cookie["name"], cookie["value"])
    try:
        session.headers["User-Agent"] = page.evaluate("() => navigator.userAgent")
    except Exception:
        pass
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return session


def _fetch_listing(args):
    unidade, url, cookies, headers = args
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(headers)
    response = session.get(url, timeout=LISTING_TIMEOUT_S, allow_redirects=True)
    path = urlparse(response.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise SessionExpired(f"[{unidade}] LISTAGEM_SESSAO_EXPIRADA: {url} -> {response.url}")
    response.raise_for_status()
    return url, scraper.norm(response.text), response.text


def _extract_contract_ids(html):
    return set(re.findall(r"/contratos/(\d+)", html or "", flags=re.IGNORECASE))


def optimized_discover_contracts(page, unidade, destino):
    source = LISTING_SOURCE
    print(f"[{unidade}] FONTE_LISTAGEM_FIXA: {source}")
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {source} final={page.url}")
    first_html = page.content()
    first_ids = _extract_contract_ids(first_html)
    print(f"[{unidade}] LISTAGEM REAL: {page.url} contratos_p1={len(first_ids)}")
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS: {page.url}")

    session = _session_from_browser(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    urls = [_page_url(source, n) for n in range(1, LISTING_PAGES + 1)]
    found = {cid: scraper.contract_url(cid) for cid in first_ids}
    print(f"[{unidade}] PAGINACAO_HTTP_DIRETA iniciado paginas=1..{LISTING_PAGES} workers={LISTING_HTTP_WORKERS} contratos_p1={len(first_ids)}")

    completed = 1
    errors = 0
    with ThreadPoolExecutor(max_workers=LISTING_HTTP_WORKERS) as pool:
        futures = {pool.submit(_fetch_listing, (unidade, url, cookies, headers)): url for url in urls[1:]}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, _, html = future.result()
                before = len(found)
                for cid in _extract_contract_ids(html):
                    found[cid] = scraper.contract_url(cid)
                completed += 1
                page_number = parse_qs(urlparse(url).query).get("page", ["?"])[0]
                print(f"[{unidade}] pagina_lista={page_number}/{LISTING_PAGES} contratos_acumulados={len(found)} novos={len(found)-before}")
            except Exception as exc:
                completed += 1
                errors += 1
                print(f"[{unidade}] pagina_lista_ERRO url={url}: {exc}")

    print(f"[{unidade}] PAGINACAO_HTTP_FINAL paginas={completed}/{LISTING_PAGES} contratos={len(found)} erros={errors}")
    if errors >= max(1, LISTING_PAGES // 2):
        raise RuntimeError(f"[{unidade}] LISTAGEM_HTTP_DEMASIADOS_ERROS: {errors}/{LISTING_PAGES}")
    return list(found.values())[:MAX_CONTRACTS], cookies, headers


def _norm(v):
    return " ".join(str(v or "").replace("\xa0", " ").split())


def _low(v):
    return _norm(v).lower()


def _soup(html):
    return BeautifulSoup(html or "", "html.parser")


def _body_text(soup):
    return _norm(soup.get_text(" ", strip=True))


def _html_tables(soup):
    out = []
    for table in soup.find_all("table"):
        heads = []
        thead = table.find("thead")
        if thead:
            heads = [_norm(x.get_text(" ", strip=True)) for x in thead.find_all(["th", "td"])]
        rows = table.find_all("tr")
        if not heads and rows:
            first = rows[0].find_all(["th", "td"])
            heads = [_norm(x.get_text(" ", strip=True)) for x in first]
        data = []
        start = 1 if not thead and rows and rows[0].find_all("th") else 0
        for tr in rows[start:]:
            vals = [_norm(x.get_text(" ", strip=True)) for x in tr.find_all("td")]
            if vals:
                data.append(vals)
        out.append((heads, data))
    return out


def _html_col(heads, *names):
    names = tuple(_low(x) for x in names)
    for i, h in enumerate(heads):
        if any(n in _low(h) for n in names):
            return i
    return None


def _html_links(soup):
    return [a.get("href") or "" for a in soup.find_all("a")]


def _extract_name_html(soup, fallback=None):
    for inp in soup.find_all("input"):
        key = _low(inp.get("name") or inp.get("id"))
        if "nome" in key:
            v = _norm(inp.get("value"))
            if len(v) >= 3 and len(v.split()) >= 2:
                return v
    text = soup.get_text("\n", strip=True)
    for pat in (r"(?:Nome completo|Nome do aluno|Aluno|Estudante)\s*[:\-]\s*([^\n|]{4,150})", r"\bNome\s*[:\-]\s*([^\n|]{4,150})"):
        m = re.search(pat, text, re.I)
        if m:
            return _norm(m.group(1))
    return fallback


def _cell_signal(cell):
    if cell is None:
        return ""
    parts = [_norm(cell.get_text(" ", strip=True))]
    for attr in ("class", "title", "aria-label", "data-status", "data-value", "data-presenca", "data-presenca-status"):
        value = cell.get(attr)
        if isinstance(value, list):
            value = " ".join(value)
        if value:
            parts.append(str(value))
    for child in cell.find_all(True):
        for attr in ("title", "aria-label", "data-status", "data-value", "class"):
            value = child.get(attr)
            if isinstance(value, list):
                value = " ".join(value)
            if value:
                parts.append(str(value))
    return _low(" ".join(parts))


def _extract_frequency_html(soup):
    rec, faltas, pres = [], 0, 0
    absence = ("falta", "faltou", "ausente", "não compareceu", "nao compareceu", "faixa-falta", "status-falta")
    presence = ("presente", "presença", "presenca", "compareceu", "faixa-presenca", "status-presenca")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        thead = table.find("thead")
        head_cells = thead.find_all(["th", "td"]) if thead else rows[0].find_all(["th", "td"])
        heads = [_norm(x.get_text(" ", strip=True)) for x in head_cells]
        si = _html_col(heads, "status", "situação", "situacao", "presença", "presenca", "frequência", "frequencia")
        di = _html_col(heads, "data", "dia")
        ai = _html_col(heads, "aluno", "nome", "estudante")
        start = 1 if not thead else 0
        for tr in rows[start:]:
            cells = tr.find_all("td")
            if not cells:
                continue
            values = [_norm(c.get_text(" ", strip=True)) for c in cells]
            signals = [_cell_signal(c) for c in cells]
            status = signals[si] if si is not None and si < len(signals) else ""
            combined = _low(" ".join(signals))
            kind = ""
            if any(x in status for x in absence) or any(x in combined for x in absence):
                faltas += 1
                kind = "falta"
            elif any(x in status for x in presence) or any(x in combined for x in presence):
                pres += 1
                kind = "presenca"
            rec.append({
                "data": values[di] if di is not None and di < len(values) else None,
                "status": values[si] if si is not None and si < len(values) else None,
                "aluno": values[ai] if ai is not None and ai < len(values) else None,
                "classificacao": kind,
                "valores": values,
                "cabecalhos": heads,
            })
    return {"faltas": faltas, "presencas": pres, "registros": rec}


def _extract_disciplines_html(soup, src):
    out = []
    for heads, rows in _html_tables(soup):
        joined = _low(" ".join(heads))
        if not any(x in joined for x in ("disciplina", "módulo", "modulo", "passo", "etapa", "progresso", "carga horária", "carga horaria", "status")):
            continue
        for row in rows:
            r = {"disciplina": None, "modulo": None, "passo": None, "progresso": None, "carga_horaria": None, "data": None, "status": None, "cabecalhos": heads, "valores": row, "origem": src}
            mapping = {"disciplina": ("disciplina",), "modulo": ("módulo", "modulo"), "passo": ("passo", "etapa"), "progresso": ("progresso",), "carga_horaria": ("carga horária", "carga horaria", "carga"), "data": ("data", "última", "ultima"), "status": ("status", "situação", "situacao", "estado")}
            for k, names in mapping.items():
                i = _html_col(heads, *names)
                if i is not None and i < len(row):
                    r[k] = row[i]
            out.append(r)
    txt = _body_text(soup)
    ms = list(re.finditer(r"M[oó]dulo\s*(\d+)\b", txt, re.I))
    for i, m in enumerate(ms):
        chunk = txt[m.start():ms[i + 1].start() if i + 1 < len(ms) else min(len(txt), m.end() + 1000)]
        sm = re.search(r"(?:Passo|Etapa)\s*(\d+)\b", chunk, re.I)
        pm = re.search(r"(\d{1,3})\s*%", chunk)
        dm = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", chunk)
        out.append({"disciplina": None, "modulo": m.group(1), "passo": sm.group(1) if sm else None, "progresso": pm.group(1) + "%" if pm else None, "carga_horaria": None, "data": dm.group(1) if dm else None, "status": None, "texto_contexto": chunk[:3000], "cabecalhos": [], "valores": [], "origem": src})
    return out


def _classify(rows):
    return scraper.classify(rows)


def _belongs(r, cid, sid, name):
    raw = _low(" ".join(str(x) for x in r.get("valores", [])))
    return any(v and _low(v) in raw for v in (cid, sid, name))


def _http_get(session, url):
    response = session.get(url, timeout=DETAIL_TIMEOUT_S, allow_redirects=True)
    path = urlparse(response.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise SessionExpired(f"SESSAO_EXPIRADA: {url} -> {response.url}")
    response.raise_for_status()
    return response.text


def _detail_http(args):
    unidade, cid, reps, cookies, headers, attempt = args
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(headers)
    session.headers.update({"Referer": scraper.contract_url(cid)})
    try:
        cu = scraper.contract_url(cid)
        contract_html = _http_get(session, cu)
        contract_soup = _soup(contract_html)
        sid = None
        for href in _html_links(contract_soup):
            m = re.search(r"/alunos/(\d+)", href or "", re.I)
            if m:
                sid = m.group(1)
                break
        course_url = scraper.child_url(cid, "cursos")
        schedule_url = scraper.child_url(cid, "horarios")
        freq_url = scraper.child_url(cid, "frequencias")
        course_html = _http_get(session, course_url)
        schedule_html = _http_get(session, schedule_url)
        freq_html = _http_get(session, freq_url)
        student_html = _http_get(session, f"{scraper.CGD_URL.rstrip('/')}/alunos/{sid}/edit") if sid else ""

        rows = _extract_disciplines_html(_soup(course_html), course_url)
        freq = _extract_frequency_html(_soup(freq_html))
        freq_soup = _soup(freq_html)
        name = _extract_name_html(freq_soup)
        if not sid:
            m = re.search(r"/alunos/(\d+)", contract_html, re.I)
            sid = m.group(1) if m else None
        student_soup = _soup(student_html)
        name = _extract_name_html(student_soup, name) if student_html else name
        rows, done, cur, fut = _classify(rows)
        def num(r, k):
            m = re.search(r"\d+", str(r.get(k) or ""))
            return int(m.group()) if m else -1
        point = max(cur, key=lambda r: (num(r, "modulo"), num(r, "passo"), num(r, "progresso"))) if cur else None
        aluno = {
            "cgd_matricula_id": cid,
            "nome": name or f"Contrato {cid}",
            "contrato": cid,
            "email": None,
            "telefone": None,
            "curso": None,
            "turma": None,
            "professor": None,
            "data_matricula": None,
            "data_inicio": None,
            "data_fim": None,
            "unidade": unidade,
            "faltas": freq["faltas"],
            "presencas": freq["presencas"],
            "ultimo_acesso": None,
            "criticidade": None,
            "dias_desde_ultimo_acesso": None,
            "status": "ATIVO",
            "cgd_url": cu,
            "disciplinas": rows,
            "disciplinas_concluidas": done,
            "disciplinas_em_andamento": cur,
            "disciplinas_futuras": fut,
            "progresso_atual": point,
            "horarios": _body_text(_soup(schedule_html))[:20000],
            "aluno_raw": _body_text(student_soup)[:25000] if student_html else "",
            "frequencia_raw": freq["registros"],
            "reposicoes": [r for r in reps if _belongs(r, cid, sid, name)],
            "capturado_em": scraper.datetime.utcnow().isoformat() + "Z",
        }
        aluno = scraper.validate_real_detail(aluno, cid, unidade)
        return {"ok": True, "cid": cid, "aluno": aluno, "attempt": attempt}
    except Exception as exc:
        return {"ok": False, "cid": cid, "error": repr(exc), "attempt": attempt}
    finally:
        session.close()


def process_details_fast(unidade, contracts, reps, cookies, headers):
    if not contracts:
        return []
    targets = list(contracts[:DETAIL_LIMIT] if DETAIL_LIMIT else contracts)
    workers = min(DETAIL_HTTP_WORKERS, len(targets))
    print(f"[{unidade}] INICIO DETALHAMENTO HTTP RAPIDO: {len(targets)} contratos / {workers} workers HTTP")
    if DETAIL_LIMIT:
        print(f"[{unidade}] LIMITE_CONTROLADO_DETALHE: {DETAIL_LIMIT}")

    pending = targets
    results = []
    for round_no in range(1, DETAIL_RETRIES + 2):
        if not pending:
            break
        print(f"[{unidade}] LOTE_HTTP_DETALHE {round_no}: {len(pending)} contratos")
        next_pending = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_detail_http, (unidade, scraper.contract_id(url), reps, cookies, headers, round_no)) for url in pending]
            for fut in as_completed(futures):
                try:
                    r = fut.result()
                except Exception as exc:
                    r = {"ok": False, "cid": "desconhecido", "error": repr(exc)}
                if r.get("ok"):
                    results.append(r["aluno"])
                    aluno = r["aluno"]
                    print(f"[{unidade}] CONTRATO_OK cid={r.get('cid')} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} freq_registros={len(aluno.get('frequencia_raw') or [])}")
                else:
                    cid = r.get("cid")
                    if cid and cid != "desconhecido":
                        next_pending.append(scraper.contract_url(cid))
                    print(f"[{unidade}] FALHA HTTP DETALHE cid={cid}: {r.get('error')}")
        print(f"[{unidade}] PROGRESSO HTTP DETALHAMENTO: sucesso_total={len(results)} falhas_para_retry={len(next_pending)}")
        pending = next_pending

    print(f"[{unidade}] DETALHAMENTO HTTP FINALIZADO: sucesso={len(results)} falhas={len(pending)} de={len(targets)}")
    return results


def run_unit(unidade, cfg, pw):
    profile = scraper.EDGE_PROFILE_BASE / unidade
    profile.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
    context = browser.new_context()
    page = context.new_page()
    state = profile / "storage_state.json"
    try:
        # Autenticacao continua sendo feita exclusivamente no Edge.
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        # Captura cookies autenticados e faz a descoberta das 831 paginas
        # diretamente por HTTP. Nao existe mais um lote de detalhes bloqueando
        # a paginacao logo no inicio.
        contracts, cookies, headers = optimized_discover_contracts(page, unidade, cfg["destino"])
        context.storage_state(path=str(state))
        reps = scraper.get_replacements(page, unidade)
    except Exception as exc:
        print(f"[{unidade}] ERRO FATAL: {exc!r}")
        raise
    finally:
        context.close()
        browser.close()

    # Somente depois da listagem completa, os detalhes sao buscados em paralelo.
    # Isso garante que a coleta das 831 paginas nao fique bloqueada por quatro
    # requisicoes sequenciais por aluno.
    return process_details_fast(unidade, contracts, reps, cookies, headers)


def main():
    print("=" * 80)
    print("SCRAPER CGD - COLETA REAL COMPLETA POR UNIDADE / ALUNO")
    print("Fluxo: autenticacao real -> listagem HTTP completa -> detalhes HTTP paralelos")
    print(f"Configuracao: listing_workers={LISTING_HTTP_WORKERS}, detail_http_workers={DETAIL_HTTP_WORKERS}, detail_limit={DETAIL_LIMIT}, timeout_s={DETAIL_TIMEOUT_S}, retries={DETAIL_RETRIES}")
    print("=" * 80)
    all_alunos = []
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            try:
                all_alunos += run_unit(unidade, scraper.CONFIG[unidade], pw)
            except Exception as exc:
                print(f"[{unidade}] UNIDADE_ABORTADA: {exc!r}")
    scraper.JSON_PATH.write_text(json.dumps(all_alunos, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_alunos)}")
    print(f"MATRIZ: {sum(1 for a in all_alunos if a.get('unidade') == 'matriz')}")
    print(f"FILIAL: {sum(1 for a in all_alunos if a.get('unidade') == 'filial')}")
    print("=" * 80)


if __name__ == "__main__":
    main()
