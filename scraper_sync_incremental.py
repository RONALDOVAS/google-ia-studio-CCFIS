"""Sincronizador incremental do universo completo CGD.

Regra: o universo Matriz + Filial e sempre redescoberto. Somente contratos novos
ou cuja assinatura de listagem mudou recebem detalhamento pesado. Registros ja
persistidos e sem mudanca permanecem intactos.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from playwright.sync_api import sync_playwright

import scraper
import scraper_runner

DATA_PATH = Path("dados_alunos.json")
SNAPSHOT_PATH = Path("dados_universo_cgd.json")
MAX_CONTRACTS = max(1, int(os.getenv("CGD_MAX_CONTRACTS", "10000")))
LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "831")))
LISTING_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "12")))
LISTING_TIMEOUT = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
DETAIL_INTERVAL_MS = max(0, int(os.getenv("CGD_DETAIL_INTERVAL_MS", "200")))
HEADLESS = os.getenv("CGD_HEADLESS", "false").lower() in ("1", "true", "yes", "sim")
SOURCE = "https://app.cgd.com.br/alunos"

_CF_MARKERS = ("sorry, you have been blocked", "you have been blocked", "just a moment", "checking your browser", "cf-chl-", "challenge-platform")


def _norm(v):
    return " ".join(str(v or "").replace("\xa0", " ").split())


def _hash_text(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _page_url(page_number):
    parsed = urlparse(SOURCE)
    q = parse_qs(parsed.query, keep_blank_values=True)
    q["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(q, doseq=True)))


def _extract_ids(html):
    return set(re.findall(r"/contratos/(\d+)", html or "", re.I))


def _listing_signature(html, cid):
    """Assinatura do contexto do contrato na listagem.

    Remove o proprio ID da assinatura para evitar falsos positivos triviais e usa
    uma janela ao redor dos links. Mudanca de nome/status/campos da linha altera
    a assinatura e dispara detalhamento.
    """
    positions = [m.start() for m in re.finditer(rf"/contratos/{re.escape(cid)}(?:[\"'/?#]|\b)", html or "", re.I)]
    chunks = []
    for pos in positions[:8]:
        chunk = html[max(0, pos - 900): min(len(html), pos + 1800)]
        chunk = re.sub(r"/contratos/" + re.escape(cid), "/contratos/CONTRATO", chunk, flags=re.I)
        chunk = re.sub(r"\s+", " ", chunk)
        chunks.append(chunk[:2700])
    if not chunks:
        return _hash_text(f"contrato:{cid}")
    return _hash_text(" || ".join(chunks))


def _fetch_page(args):
    unidade, url, cookies, headers = args
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update(headers)
    r = s.get(url, timeout=LISTING_TIMEOUT, allow_redirects=True)
    path = urlparse(r.url).path.rstrip("/").lower()
    if "/login" in path:
        raise RuntimeError(f"sessao redirecionada para login: {url}")
    r.raise_for_status()
    html = r.text
    low = html.lower()
    if any(marker in low for marker in _CF_MARKERS):
        raise RuntimeError(f"bloqueio/challenge detectado na listagem: {url}")
    return unidade, url, html


def discover_universe(page, unidade):
    if not scraper.open_page(page, SOURCE, unidade, "lista_universo", 300):
        raise RuntimeError(f"[{unidade}] nao foi possivel abrir {SOURCE}")
    html = page.content()
    first_ids = _extract_ids(html)
    if not first_ids:
        raise RuntimeError(f"[{unidade}] primeira pagina sem contratos")
    session = scraper_runner._session_from_page(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    found = {}
    signatures = {}
    for cid in first_ids:
        found[cid] = scraper.contract_url(cid)
        signatures[cid] = _listing_signature(html, cid)

    urls = [_page_url(n) for n in range(1, LISTING_PAGES + 1)]
    errors = 0
    with ThreadPoolExecutor(max_workers=LISTING_WORKERS) as pool:
        futures = {pool.submit(_fetch_page, (unidade, u, cookies, headers)): u for u in urls[1:]}
        done = 1
        for fut in as_completed(futures):
            url = futures[fut]
            done += 1
            try:
                _, _, html = fut.result()
                ids = _extract_ids(html)
                for cid in ids:
                    found[cid] = scraper.contract_url(cid)
                    signatures[cid] = _listing_signature(html, cid)
                if done % 25 == 0 or done == len(urls):
                    print(f"[{unidade}] UNIVERSO paginas={done}/{len(urls)} contratos={len(found)}", flush=True)
            except Exception as exc:
                errors += 1
                print(f"[{unidade}] LISTAGEM_ERRO url={url}: {exc!r}", flush=True)

    if errors >= max(1, LISTING_PAGES // 2):
        raise RuntimeError(f"[{unidade}] listagem insuficiente: {errors}/{LISTING_PAGES} paginas falharam")
    if len(found) > MAX_CONTRACTS:
        print(f"[{unidade}] AVISO: universo descoberto={len(found)}; limite operacional={MAX_CONTRACTS}", flush=True)
        found = dict(list(found.items())[:MAX_CONTRACTS])
        signatures = {cid: signatures[cid] for cid in found}
    print(f"[{unidade}] UNIVERSO_COMPLETO_DESCOBERTO={len(found)} paginas_com_erro={errors}", flush=True)
    return found, signatures, errors


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except Exception as exc:
        raise RuntimeError(f"arquivo invalido {path}: {exc}")


def _key(aluno):
    return str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()


def _merge(existing, updates):
    by_id = {_key(a): a for a in existing if _key(a)}
    for a in updates:
        cid = _key(a)
        if cid:
            by_id[cid] = a
    return list(by_id.values())


def _signature_changed(existing, current):
    old = existing.get("assinatura_universo_cgd")
    return not old or old != current


def _detail(page, unidade, cid, reps):
    print(f"[{unidade}] DETALHE_NECESSARIO cid={cid}", flush=True)
    aluno = scraper.contract_bundle(page, cid, unidade, reps)
    if not aluno:
        raise RuntimeError(f"contrato sem resultado: {cid}")
    if not _norm(aluno.get("nome")) or _norm(aluno.get("nome")) == f"Contrato {cid}":
        raise RuntimeError(f"aluno nao identificado: {cid}")
    aluno["unidade"] = unidade
    aluno["assinatura_universo_cgd"] = CURRENT_SIGNATURES[cid]
    aluno["sincronizado_em"] = datetime.now(timezone.utc).isoformat()
    return aluno


def _run_unit(unidade, cfg, existing_by_id):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        try:
            scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
            contracts, signatures, listing_errors = discover_universe(page, unidade)
            global CURRENT_SIGNATURES
            CURRENT_SIGNATURES = signatures
            reps = scraper.get_replacements(page, unidade)
            current_ids = set(contracts)
            new_ids = [cid for cid in contracts if cid not in existing_by_id]
            changed_ids = [cid for cid in contracts if cid in existing_by_id and _signature_changed(existing_by_id[cid], signatures.get(cid))]
            unchanged_ids = [cid for cid in contracts if cid in existing_by_id and cid not in set(changed_ids)]
            print(f"[{unidade}] NOVOS={len(new_ids)} ALTERADOS={len(changed_ids)} SEM_MUDANCA={len(unchanged_ids)} UNIVERSO={len(current_ids)}", flush=True)
            updates = []
            targets = new_ids + changed_ids
            for idx, cid in enumerate(targets, 1):
                try:
                    updates.append(_detail(page, unidade, cid, reps))
                    print(f"[{unidade}] DETALHE_OK {idx}/{len(targets)} cid={cid}", flush=True)
                except Exception as exc:
                    print(f"[{unidade}] DETALHE_ERRO cid={cid}: {exc!r}", flush=True)
                    raise
                if DETAIL_INTERVAL_MS and idx < len(targets):
                    page.wait_for_timeout(DETAIL_INTERVAL_MS)
            return updates, signatures, current_ids, listing_errors, len(new_ids), len(changed_ids), len(unchanged_ids)
        finally:
            context.close()
            browser.close()


def main():
    print("=" * 90, flush=True)
    print("CGD SYNC — UNIVERSO COMPLETO + DETECCAO DE MUDANCAS + ATUALIZACAO INCREMENTAL", flush=True)
    print("Nenhum alvo cumulativo de 750. A listagem inteira e redescoberta em toda rodada.", flush=True)
    print("Detalhamento pesado somente para NOVOS ou ALTERADOS.", flush=True)
    print("=" * 90, flush=True)
    existing = _load_json(DATA_PATH, [])
    if not isinstance(existing, list):
        raise RuntimeError("dados_alunos.json precisa ser uma lista")
    old_snapshot = _load_json(SNAPSHOT_PATH, {"unidades": {}})
    if not isinstance(old_snapshot, dict):
        old_snapshot = {"unidades": {}}

    all_updates = []
    snapshot = {"gerado_em": datetime.now(timezone.utc).isoformat(), "regra": "UNIVERSO_COMPLETO_INCREMENTAL", "unidades": {}}
    totals = {"universo": 0, "novos": 0, "alterados": 0, "sem_mudanca": 0}
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            # Usa uma sessao persistente nesta unidade; o discovery abre HTTP paralelo
            # com os cookies dessa sessao e o detalhe usa a mesma sessao do navegador.
            cfg = scraper.CONFIG[unidade]
            browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
            context = browser.new_context()
            page = context.new_page()
            try:
                scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
                contracts, signatures, listing_errors = discover_universe(page, unidade)
                global CURRENT_SIGNATURES
                CURRENT_SIGNATURES = signatures
                reps = scraper.get_replacements(page, unidade)
                existing_by_id = {_key(a): a for a in existing if a.get("unidade") == unidade and _key(a)}
                new_ids = [cid for cid in contracts if cid not in existing_by_id]
                changed_ids = [cid for cid in contracts if cid in existing_by_id and _signature_changed(existing_by_id[cid], signatures.get(cid))]
                unchanged = [cid for cid in contracts if cid in existing_by_id and cid not in set(changed_ids)]
                print(f"[{unidade}] UNIVERSO={len(contracts)} NOVOS={len(new_ids)} ALTERADOS={len(changed_ids)} SEM_MUDANCA={len(unchanged)}", flush=True)
                updates = []
                targets = new_ids + changed_ids
                for idx, cid in enumerate(targets, 1):
                    aluno = _detail(page, unidade, cid, reps)
                    updates.append(aluno)
                    if idx % 25 == 0 or idx == len(targets):
                        print(f"[{unidade}] DETALHAMENTO {idx}/{len(targets)}", flush=True)
                    if DETAIL_INTERVAL_MS and idx < len(targets):
                        page.wait_for_timeout(DETAIL_INTERVAL_MS)
                # Atualiza a assinatura inclusive dos que nao precisaram de detalhe,
                # preservando todos os demais dados antigos exatamente como estavam.
                for cid in unchanged:
                    old = existing_by_id[cid]
                    old["assinatura_universo_cgd"] = signatures[cid]
                    old["visto_no_cgd_em"] = datetime.now(timezone.utc).isoformat()
                all_updates.extend(updates)
                snapshot["unidades"][unidade] = {
                    "contratos": {cid: signatures[cid] for cid in contracts},
                    "total": len(contracts),
                    "novos": len(new_ids),
                    "alterados": len(changed_ids),
                    "sem_mudanca": len(unchanged),
                    "paginas_com_erro": listing_errors,
                }
                totals["universo"] += len(contracts)
                totals["novos"] += len(new_ids)
                totals["alterados"] += len(changed_ids)
                totals["sem_mudanca"] += len(unchanged)
            finally:
                context.close()
                browser.close()

    merged = _merge(existing, all_updates)
    DATA_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"UNIVERSO_TOTAL_CGD={totals['universo']}", flush=True)
    print(f"NOVOS_CAPTURADOS={totals['novos']}", flush=True)
    print(f"CONTRATOS_ALTERADOS_ATUALIZADOS={totals['alterados']}", flush=True)
    print(f"SEM_MUDANCA_PRESERVADOS={totals['sem_mudanca']}", flush=True)
    print(f"BASE_PERSISTIDA={len(merged)}", flush=True)
    print("SINCRONIZACAO_INCREMENTAL=OK", flush=True)


if __name__ == "__main__":
    main()
