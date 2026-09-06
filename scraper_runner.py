"""Executor CGD: autenticacao Edge + listagem e detalhes via HTTP paralelo.

O Edge e usado para autenticar e obter a sessao real. Depois disso, a coleta
nao navega pagina por pagina no navegador: cada contrato e consultado por HTTP
em paralelo. Isso elimina o gargalo de abrir quatro paginas do CGD para cada
aluno e deixa o Edge livre/estavel enquanto a coleta trabalha em alta
concorrencia.
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


def _fetch(session, url):
    r = session.get(url, timeout=DETAIL_TIMEOUT_S, allow_redirects=True)
    path = urlparse(r.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise SessionExpired(f"SESSAO_EXPIRADA: {url} -> {r.url}")
    r.raise_for_status()
    return r.text


def _fetch_listing(args):
    unidade, url, cookies, headers = args
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update(headers)
    r = s.get(url, timeout=LISTING_TIMEOUT_S, allow_redirects=True)
    path = urlparse(r.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise SessionExpired(f"[{unidade}] LISTAGEM_SESSAO_EXPIRADA: {url} -> {r.url}")
    r.raise_for_status()
    return url, _extract_contract_ids(r.text), len(r.text)


def _extract_contract_ids(html):
    return set(re.findall(r"/contratos/(\d+)", html or "", re.I))


def optimized_discover_contracts(page, unidade, destino):
    if not scraper.open_page(page, LISTING_SOURCE, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {page.url}")
    first = _extract_contract_ids(page.content())
    if not first:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS")
    session = _session_from_browser(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    urls = [_page_url(LISTING_SOURCE, n) for n in range(1, LISTING_PAGES + 1)]
    found = {cid: scraper.contract_url(cid) for cid in first}
    print(f"[{unidade}] LISTAGEM HTTP: paginas=1..{LISTING_PAGES} workers={LISTING_HTTP_WORKERS} p1={len(first)}", flush=True)
    errors = 0
    done = 1
    with ThreadPoolExecutor(max_workers=LISTING_HTTP_WORKERS) as pool:
        futures = {pool.submit(_fetch_listing, (unidade, u, cookies, headers)): u for u in urls[1:]}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                _, ids, size = fut.result()
                before = len(found)
                for cid in ids:
                    found[cid] = scraper.contract_url(cid)
                done += 1
                pn = parse_qs(urlparse(url).query).get("page", ["?"])[0]
                print(f"[{unidade}] pagina_lista={pn}/{LISTING_PAGES} contratos={len(found)} novos={len(found)-before} bytes={size}", flush=True)
            except Exception as exc:
                done += 1
                errors += 1
                print(f"[{unidade}] pagina_lista_ERRO: {exc}", flush=True)
    print(f"[{unidade}] LISTAGEM FINAL: {done}/{LISTING_PAGES} paginas, {len(found)} contratos, {errors} erros", flush=True)
    if errors >= max(1, LISTING_PAGES // 2):
        raise RuntimeError(f"[{unidade}] LISTAGEM_HTTP_DEMASIADOS_ERROS={errors}")
    return list(found.values())[:MAX_CONTRACTS], cookies, headers


def _norm(v):
    return " ".join(str(v or "").replace("\xa0", " ").split())


def _low(v):
    return _norm(v).lower()


def _soup(html):
    return BeautifulSoup(html or "", "html.parser")


def _tables(soup):
    result = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        thead = table.find("thead")
        hc = thead.find_all(["th", "td"]) if thead else rows[0].find_all(["th", "td"])
        heads = [_norm(x.get_text(" ", strip=True)) for x in hc]
        start = 0 if thead else (1 if rows[0].find_all("th") else 0)
        data = []
        for tr in rows[start:]:
            cells = tr.find_all("td")
            vals = [_norm(x.get_text(" ", strip=True)) for x in cells]
            if vals:
                data.append(vals)
        result.append((heads, data))
    return result


def _col(heads, *names):
    names = tuple(_low(x) for x in names)
    for i, h in enumerate(heads):
        if any(n in _low(h) for n in names):
            return i
    return None


def _extract_name(soup, fallback=None):
    for inp in soup.find_all("input"):
        key = _low(inp.get("name") or inp.get("id"))
        if "nome" in key:
            value = _norm(inp.get("value"))
            if len(value) >= 3 and len(value.split()) >= 2:
                return value
    text = soup.get_text("\n", strip=True)
    for pat in (r"(?:Nome completo|Nome do aluno|Aluno|Estudante)\s*[:\-]\s*([^\n|]{4,150})", r"\bNome\s*[:\-]\s*([^\n|]{4,150})"):
        m = re.search(pat, text, re.I)
        if m:
            return _norm(m.group(1))
    return fallback


def _extract_frequency(soup):
    records = []
    faltas = 0
    presencas = 0
    absence = ("faltou", "falta", "ausente", "não compareceu", "nao compareceu")
    presence = ("presente", "presença", "presenca", "compareceu")
    for heads, rows in _tables(soup):
        si = _col(heads, "status", "situação", "situacao", "presença", "presenca", "frequência", "frequencia")
        di = _col(heads, "data", "dia")
        ai = _col(heads, "aluno", "nome", "estudante")
        for row in rows:
            status = _low(row[si]) if si is not None and si < len(row) else ""
            kind = ""
            if any(x == status or x in status for x in absence):
                faltas += 1
                kind = "falta"
            elif any(x == status or x in status for x in presence):
                presencas += 1
                kind = "presenca"
            records.append({
                "data": row[di] if di is not None and di < len(row) else None,
                "status": row[si] if si is not None and si < len(row) else None,
                "aluno": row[ai] if ai is not None and ai < len(row) else None,
                "classificacao": kind,
                "valores": row,
                "cabecalhos": heads,
            })
    return {"faltas": faltas, "presencas": presencas, "registros": records}


def _extract_disciplines(soup, src):
    rows_out = []
    for heads, rows in _tables(soup):
        joined = _low(" ".join(heads))
        if not any(x in joined for x in ("disciplina", "módulo", "modulo", "passo", "etapa", "progresso", "carga horária", "carga horaria", "status")):
            continue
        mapping = {
            "disciplina": ("disciplina",), "modulo": ("módulo", "modulo"), "passo": ("passo", "etapa"),
            "progresso": ("progresso",), "carga_horaria": ("carga horária", "carga horaria", "carga"),
            "data": ("data", "última", "ultima"), "status": ("status", "situação", "situacao", "estado")
        }
        for row in rows:
            item = {"disciplina": None, "modulo": None, "passo": None, "progresso": None, "carga_horaria": None, "data": None, "status": None, "cabecalhos": heads, "valores": row, "origem": src}
            for key, names in mapping.items():
                i = _col(heads, *names)
                if i is not None and i < len(row):
                    item[key] = row[i]
            rows_out.append(item)
    text = soup.get_text(" ", strip=True)
    matches = list(re.finditer(r"M[oó]dulo\s*(\d+)\b", text, re.I))
    for i, m in enumerate(matches):
        chunk = text[m.start():matches[i + 1].start() if i + 1 < len(matches) else min(len(text), m.end() + 1000)]
        pm = re.search(r"(\d{1,3})\s*%", chunk)
        sm = re.search(r"(?:Passo|Etapa)\s*(\d+)\b", chunk, re.I)
        dm = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", chunk)
        rows_out.append({"disciplina": None, "modulo": m.group(1), "passo": sm.group(1) if sm else None, "progresso": pm.group(1) + "%" if pm else None, "carga_horaria": None, "data": dm.group(1) if dm else None, "status": None, "texto_contexto": chunk[:3000], "cabecalhos": [], "valores": [], "origem": src})
    return rows_out


def _belongs(r, cid, sid, name):
    raw = _low(" ".join(str(x) for x in r.get("valores", [])))
    return any(v and _low(v) in raw for v in (cid, sid, name))


def _detail_http(args):
    unidade, cid, reps, cookies, headers, attempt = args
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(headers)
    session.headers["Referer"] = scraper.contract_url(cid)
    try:
        contract_html = _fetch(session, scraper.contract_url(cid))
        contract_soup = _soup(contract_html)
        sid = None
        for a in contract_soup.find_all("a", href=True):
            m = re.search(r"/alunos/(\d+)", a.get("href", ""), re.I)
            if m:
                sid = m.group(1)
                break
        freq_html = _fetch(session, scraper.child_url(cid, "frequencias"))
        freq_soup = _soup(freq_html)
        freq = _extract_frequency(freq_soup)
        name = _extract_name(freq_soup)
        course_html = _fetch(session, scraper.child_url(cid, "cursos"))
        schedule_html = _fetch(session, scraper.child_url(cid, "horarios"))
        rows = _extract_disciplines(_soup(course_html), scraper.child_url(cid, "cursos"))
        schedule_text = _norm(_soup(schedule_html).get_text(" ", strip=True))[:20000]
        student_raw = ""
        if sid:
            student_html = _fetch(session, f"{scraper.CGD_URL.rstrip('/')}/alunos/{sid}/edit")
            student_soup = _soup(student_html)
            name = _extract_name(student_soup, name)
            student_raw = _norm(student_soup.get_text(" ", strip=True))[:25000]
        if not sid:
            m = re.search(r"/alunos/(\d+)", contract_html, re.I)
            sid = m.group(1) if m else None
        rows, done, cur, fut = scraper.classify(rows)
        def num(row, key):
            m = re.search(r"\d+", str(row.get(key) or ""))
            return int(m.group()) if m else -1
        point = max(cur, key=lambda r: (num(r, "modulo"), num(r, "passo"), num(r, "progresso"))) if cur else None
        aluno = {
            "cgd_matricula_id": cid, "nome": name or f"Contrato {cid}", "contrato": cid,
            "email": None, "telefone": None, "curso": None, "turma": None, "professor": None,
            "data_matricula": None, "data_inicio": None, "data_fim": None, "unidade": unidade,
            "faltas": freq["faltas"], "presencas": freq["presencas"], "ultimo_acesso": None,
            "criticidade": None, "dias_desde_ultimo_acesso": None, "status": "ATIVO",
            "cgd_url": scraper.contract_url(cid), "disciplinas": rows, "disciplinas_concluidas": done,
            "disciplinas_em_andamento": cur, "disciplinas_futuras": fut, "progresso_atual": point,
            "horarios": schedule_text, "aluno_raw": student_raw, "frequencia_raw": freq["registros"],
            "reposicoes": [r for r in reps if _belongs(r, cid, sid, name)],
            "capturado_em": __import__("datetime").datetime.utcnow().isoformat() + "Z"
        }
        aluno = scraper.validate_real_detail(aluno, cid, unidade)
        return {"ok": True, "cid": cid, "aluno": aluno, "attempt": attempt}
    except Exception as exc:
        return {"ok": False, "cid": cid, "error": repr(exc), "attempt": attempt}


def process_details_fast(unidade, contracts, reps, cookies, headers):
    targets = list(contracts[:DETAIL_LIMIT] if DETAIL_LIMIT else contracts)
    if not targets:
        return []
    workers = min(DETAIL_HTTP_WORKERS, len(targets))
    print(f"[{unidade}] INICIO DETALHAMENTO HTTP PARALELO: {len(targets)} contratos / {workers} workers", flush=True)
    pending = [scraper.contract_id(c) for c in targets]
    results = []
    for attempt in range(1, DETAIL_RETRIES + 2):
        if not pending:
            break
        print(f"[{unidade}] LOTE_DETALHE_HTTP tentativa={attempt} contratos={len(pending)}", flush=True)
        next_pending = []
        args = [(unidade, cid, reps, cookies, headers, attempt) for cid in pending if cid]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_detail_http, a) for a in args]
            for i, fut in enumerate(as_completed(futures), 1):
                try:
                    result = fut.result()
                except Exception as exc:
                    result = {"ok": False, "cid": "desconhecido", "error": repr(exc)}
                if result.get("ok"):
                    aluno = result["aluno"]
                    results.append(aluno)
                    print(f"[{unidade}] CONTRATO_OK {i}/{len(args)} cid={result['cid']} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} freq_registros={len(aluno.get('frequencia_raw') or [])}", flush=True)
                else:
                    cid = result.get("cid")
                    if cid and cid != "desconhecido":
                        next_pending.append(cid)
                    print(f"[{unidade}] FALHA_DETALHE {cid}: {result.get('error')}", flush=True)
        print(f"[{unidade}] PROGRESSO_DETALHAMENTO: sucesso={len(results)} pendentes_retry={len(next_pending)}", flush=True)
        pending = next_pending
    print(f"[{unidade}] DETALHAMENTO_FINAL: sucesso={len(results)} falhas={len(pending)} de={len(targets)}", flush=True)
    return results


def run_unit(unidade, cfg, pw):
    profile = scraper.EDGE_PROFILE_BASE / unidade
    profile.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(channel="msedge", headless=False)
    context = browser.new_context()
    page = context.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        contracts, cookies, headers = optimized_discover_contracts(page, unidade, cfg["destino"])
        reps = scraper.get_replacements(page, unidade)
        print(f"[{unidade}] REPOSICOES GLOBAIS CAPTURADAS: {len(reps)}", flush=True)
        # Mantem o Edge aberto mostrando a rota autenticada; a coleta pesada acontece fora dele.
        print(f"[{unidade}] EDGE AUTENTICADO: coleta de detalhes iniciada sem navegacao sequencial do navegador", flush=True)
        return process_details_fast(unidade, contracts, reps, cookies, headers)
    finally:
        context.close()
        browser.close()


def main():
    print("=" * 80, flush=True)
    print("SCRAPER CGD - COLETA REAL RAPIDA POR UNIDADE / ALUNO", flush=True)
    print("Fluxo: Edge autentica -> HTTP paralelo lista -> HTTP paralelo detalhes", flush=True)
    print(f"Configuracao: listing_workers={LISTING_HTTP_WORKERS}, detail_workers={DETAIL_HTTP_WORKERS}, detail_limit_por_unidade={DETAIL_LIMIT}, detail_retries={DETAIL_RETRIES}", flush=True)
    print("=" * 80, flush=True)
    all_alunos = []
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            try:
                all_alunos.extend(run_unit(unidade, scraper.CONFIG[unidade], pw))
            except Exception as exc:
                print(f"[{unidade}] UNIDADE_ABORTADA: {exc!r}", flush=True)
    scraper.JSON_PATH.write_text(json.dumps(all_alunos, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80, flush=True)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_alunos)}", flush=True)
    print(f"MATRIZ: {sum(1 for a in all_alunos if a.get('unidade') == 'matriz')}", flush=True)
    print(f"FILIAL: {sum(1 for a in all_alunos if a.get('unidade') == 'filial')}", flush=True)
    print("=" * 80, flush=True)


if __name__ == "__main__":
    main()
