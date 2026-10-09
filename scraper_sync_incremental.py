"""Sincronizador CGD diferencial e persistente.

Objetivo operacional:
- redescobrir o universo completo Matriz + Filial em cada rodada;
- comparar o universo atual com a base ja detalhada;
- detalhar somente contratos novos, alterados ou incompletos;
- manter uma pequena fila rotativa para dados dinamicos que possam mudar sem
  alterar a assinatura da listagem;
- usar 750 apenas como teto de seguranca por unidade, nunca como meta fixa;
- persistir o progresso mesmo que alguns detalhes falhem;
- nunca substituir a base inteira por uma coleta parcial.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from time import perf_counter
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from playwright.sync_api import sync_playwright
import cgd_http_detail

# O detalhamento usa a mesma pagina autenticada para varias navegacoes.
# Reduzimos apenas a espera artificial entre navegacoes; nao alteramos timeout,
# concorrencia do navegador ou a sessao autenticada.
os.environ.setdefault("CGD_PAGE_WAIT_MS", "800")

import scraper
import scraper_runner

try:
    import importlib
    importlib.import_module("frequency_runtime_patch")
except Exception as exc:
    raise RuntimeError(f"PATCH_FREQUENCIA_REAL_ERRO: {type(exc).__name__}: {exc}") from exc

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_PATH = PROJECT_ROOT / "dados_alunos.json"
SNAPSHOT_PATH = PROJECT_ROOT / "dados_universo_cgd.json"
MAX_CONTRACTS = max(1, int(os.getenv("CGD_MAX_CONTRACTS", "10000")))
BATCH_PER_UNIT = max(1, int(os.getenv("CGD_DETAIL_BATCH_PER_UNIT", "750")))
DYNAMIC_REFRESH_HOURS = max(0, float(os.getenv("CGD_DYNAMIC_REFRESH_HOURS", "24")))
DYNAMIC_BATCH_PER_UNIT = max(0, int(os.getenv("CGD_DYNAMIC_BATCH_PER_UNIT", "100")))
LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "831")))
LISTING_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "12")))
LISTING_TIMEOUT = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
DETAIL_INTERVAL_MS = max(0, int(os.getenv("CGD_DETAIL_INTERVAL_MS", "50")))
DETAIL_WORKERS = max(1, int(os.getenv("CGD_DETAIL_WORKERS", "4")))
PENDING_FREQUENCY_PATH = PROJECT_ROOT / "dados_frequencias_a_registrar.json"
HEADLESS = os.getenv("CGD_HEADLESS", "false").lower() in ("1", "true", "yes", "sim")
SOURCE = "https://app.cgd.com.br/alunos"
CF_MARKERS = ("sorry, you have been blocked", "you have been blocked", "just a moment", "checking your browser", "cf-chl-", "challenge-platform")


def norm(v):
    return " ".join(str(v or "").replace("\xa0", " ").split())


def hash_text(text):
    return hashlib.sha256(re.sub(r"\s+", " ", text or "").strip().encode("utf-8", "ignore")).hexdigest()


def page_url(page_number):
    parsed = urlparse(SOURCE)
    q = parse_qs(parsed.query, keep_blank_values=True)
    q["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(q, doseq=True)))


def extract_ids(html):
    return set(re.findall(r"/contratos/(\d+)", html or "", re.I))


def listing_signature(html, cid):
    positions = [m.start() for m in re.finditer(rf"/contratos/{re.escape(cid)}(?:[\"'/?#]|\b)", html or "", re.I)]
    chunks = []
    for pos in positions[:8]:
        chunk = html[max(0, pos - 900):min(len(html), pos + 1800)]
        chunk = re.sub(r"/contratos/" + re.escape(cid), "/contratos/CONTRATO", chunk, flags=re.I)
        chunks.append(re.sub(r"\s+", " ", chunk)[:2700])
    return hash_text(" || ".join(chunks) if chunks else f"contrato:{cid}")


def fetch_listing(args):
    unidade, url, cookies, headers = args
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(headers)
    response = session.get(url, timeout=LISTING_TIMEOUT, allow_redirects=True)
    path = urlparse(response.url).path.rstrip("/").lower()
    if "/login" in path:
        raise RuntimeError(f"sessao redirecionada para login: {url}")
    response.raise_for_status()
    html = response.text
    if any(marker in html.lower() for marker in CF_MARKERS):
        raise RuntimeError(f"challenge/bloqueio detectado: {url}")
    return unidade, url, html


def discover_universe(page, unidade):
    started = perf_counter()
    if not scraper.open_page(page, SOURCE, unidade, "lista_universo", 300):
        raise RuntimeError(f"[{unidade}] nao foi possivel abrir {SOURCE}")
    first_html = page.content()
    first_ids = extract_ids(first_html)
    if not first_ids:
        raise RuntimeError(f"[{unidade}] primeira pagina sem contratos")

    session = scraper_runner._session_from_page(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    found = {cid: scraper.contract_url(cid) for cid in first_ids}
    signatures = {cid: listing_signature(first_html, cid) for cid in first_ids}
    urls = [page_url(n) for n in range(1, LISTING_PAGES + 1)]
    errors = 0

    with ThreadPoolExecutor(max_workers=LISTING_WORKERS) as pool:
        futures = {pool.submit(fetch_listing, (unidade, url, cookies, headers)): url for url in urls[1:]}
        done = 1
        for future in as_completed(futures):
            url = futures[future]
            done += 1
            try:
                _, _, html = future.result()
                for cid in extract_ids(html):
                    found[cid] = scraper.contract_url(cid)
                    signatures[cid] = listing_signature(html, cid)
                if done % 25 == 0 or done == len(urls):
                    print(f"[{unidade}] UNIVERSO paginas={done}/{len(urls)} contratos={len(found)}", flush=True)
            except Exception as exc:
                errors += 1
                print(f"[{unidade}] LISTAGEM_ERRO url={url}: {exc!r}", flush=True)

    if errors >= max(1, LISTING_PAGES // 2):
        raise RuntimeError(f"[{unidade}] listagem insuficiente: {errors}/{LISTING_PAGES} paginas falharam")
    if len(found) > MAX_CONTRACTS:
        raise RuntimeError(f"[{unidade}] universo={len(found)} excede limite operacional CGD_MAX_CONTRACTS={MAX_CONTRACTS}")
    elapsed = perf_counter() - started
    print(f"[{unidade}] UNIVERSO_COMPLETO_DESCOBERTO={len(found)} paginas_com_erro={errors} TEMPO_DESCOBERTA={elapsed:.2f}s", flush=True)
    return found, signatures, errors, elapsed


def capture_pending_frequency_route(page, unidade):
    candidates = []
    for text, href in scraper.links(page):
        hay = norm(f"{text} {href}").lower()
        if "frequenc" in hay and any(x in hay for x in ("registr", "pend", "lanç", "lanc")):
            candidates.append(href)
    if not candidates:
        for text, href in scraper.links(page):
            if "frequenc" in norm(f"{text} {href}").lower():
                candidates.append(href)
    for href in dict.fromkeys(candidates):
        if scraper.open_page(page, href, unidade, "frequencias_a_registrar", 500):
            return {"unidade": unidade, "url": page.url, "ok": True, "texto": scraper.body(page)[:50000], "tabelas": [{"cabecalhos": h, "linhas": r} for h, r in scraper.table_data(page)], "capturado_em": datetime.now(timezone.utc).isoformat()}
    return {"unidade": unidade, "url": None, "ok": False, "texto": "", "tabelas": [], "capturado_em": datetime.now(timezone.utc).isoformat()}


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"arquivo invalido {path}: {exc}")


def key(aluno):
    return str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()


def atomic_write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def signature_changed(existing, current):
    old = existing.get("assinatura_universo_cgd")
    return not old or old != current


def dynamic_data_stale(existing, now=None):
    if DYNAMIC_REFRESH_HOURS <= 0:
        return False
    stamp = existing.get("sincronizado_dinamico_em") or existing.get("sincronizado_em")
    if not stamp:
        return True
    try:
        checked = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return (current - checked).total_seconds() >= DYNAMIC_REFRESH_HOURS * 3600
    except Exception:
        return True


def detail(page, unidade, cid, reps, signature, http_session):
    print(f"[{unidade}] DETALHE_NECESSARIO cid={cid} modo=HTTP", flush=True)
    try:
        aluno = cgd_http_detail.contract_bundle_http(http_session, cid, unidade, reps)
        aluno = scraper.validate_real_detail(aluno, cid, unidade)
        print(f"[{unidade}] DETALHE_HTTP_OK cid={cid}", flush=True)
    except Exception as http_exc:
        print(f"[{unidade}] DETALHE_HTTP_FALLBACK_NAVEGADOR cid={cid}: {http_exc!r}", flush=True)
        aluno = scraper.contract_bundle(page, cid, unidade, reps)
        aluno = scraper.validate_real_detail(aluno, cid, unidade)
        aluno["detalhamento_modo"] = "browser_fallback"
    aluno["unidade"] = unidade
    aluno["assinatura_universo_cgd"] = signature
    now = datetime.now(timezone.utc).isoformat()
    aluno["sincronizado_em"] = now
    aluno["sincronizado_dinamico_em"] = now
    aluno["ultima_verificacao_cgd"] = now
    return aluno

def refresh_dynamic(page, unidade, cid, aluno, http_session):
    print(f"[{unidade}] DINAMICO_HTTP cid={cid} tipo=frequencia", flush=True)
    try:
        freq, _, ok = cgd_http_detail.refresh_frequency(http_session, cid)
        if not ok:
            raise RuntimeError("rota de frequencia sem conteúdo")
        aluno["faltas"] = freq["faltas"]
        aluno["presencas"] = freq["presencas"]
        aluno["frequencia_raw"] = freq["registros"]
        aluno["frequencia_status"] = "COM_FREQUENCIA_REAL" if freq["registros"] else "SEM_FREQUENCIA_A_INVESTIGAR"
        aluno["detalhamento_modo_dinamico"] = "http_autenticado"
        now = datetime.now(timezone.utc).isoformat()
        aluno["sincronizado_dinamico_em"] = now
        aluno["ultima_verificacao_cgd"] = now
        return aluno
    except Exception as http_exc:
        print(f"[{unidade}] DINAMICO_HTTP_FALLBACK_NAVEGADOR cid={cid}: {http_exc!r}", flush=True)
        aluno2 = scraper.contract_bundle(page, cid, unidade, aluno.get("reposicoes") or [])
        aluno2 = scraper.validate_real_detail(aluno2, cid, unidade)
        # A assinatura antiga só é preservada depois que o novo detalhe passou
        # pela validação; uma falha mantém o registro anterior intacto e retryable.
        aluno2["assinatura_universo_cgd"] = aluno.get("assinatura_universo_cgd")
        now = datetime.now(timezone.utc).isoformat()
        aluno2["sincronizado_dinamico_em"] = now
        aluno2["ultima_verificacao_cgd"] = now
        aluno2["detalhamento_modo_dinamico"] = "browser_fallback"
        return aluno2


def main():
    total_started = perf_counter()
    print("=" * 96, flush=True)
    print("CGD SYNC — UNIVERSO COMPLETO + DIFERENCIAL + PERSISTENCIA", flush=True)
    print("A listagem do universo e completa; o detalhamento ocorre somente onde ha necessidade.", flush=True)
    print("750 e apenas teto de seguranca por unidade; nao e meta de processamento.", flush=True)
    print(f"Fila dinamica rotativa: {DYNAMIC_BATCH_PER_UNIT}/unidade a cada {DYNAMIC_REFRESH_HOURS:g}h.", flush=True)
    print("MEDICAO DE PERFORMANCE ATIVA — sem alterar o limite de 750.", flush=True)
    print("OTIMIZACAO: detalhe HTTP autenticado; navegador apenas login/fallback.", flush=True)
    print("=" * 96, flush=True)

    existing = load_json(DATA_PATH, [])
    if not isinstance(existing, list):
        raise RuntimeError("dados_alunos.json precisa ser uma lista")

    by_id = {}
    for aluno in existing:
        cid = key(aluno)
        if cid:
            by_id[(str(aluno.get("unidade") or "").lower(), cid)] = aluno

    snapshot = {"gerado_em": datetime.now(timezone.utc).isoformat(), "regra": "UNIVERSO_COMPLETO_LOTES_750_INCREMENTAL", "unidades": {}}
    totals = {"universo": 0, "novos": 0, "alterados": 0, "capturados": 0, "sem_mudanca": 0, "erros_detalhe": 0}
    performance = {"discovery": {}, "comparison": {}, "detail": {}, "persistence": 0.0, "total": 0.0}
    pending_frequency = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
        try:
            for unidade in ("matriz", "filial"):
                unit_started = perf_counter()
                cfg = scraper.CONFIG[unidade]
                context = browser.new_context()
                page = context.new_page()
                try:
                    scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
                    pending_frequency.append(capture_pending_frequency_route(page, unidade))
                    contracts, signatures, listing_errors, discovery_elapsed = discover_universe(page, unidade)
                    performance["discovery"][unidade] = discovery_elapsed

                    comparison_started = perf_counter()
                    reps = scraper.get_replacements(page, unidade)
                    current = {cid: by_id.get((unidade, cid)) for cid in contracts}
                    new_ids = [cid for cid in contracts if current[cid] is None]
                    changed_ids = [cid for cid in contracts if current[cid] is not None and signature_changed(current[cid], signatures[cid])]
                    incomplete_ids = [
                        cid for cid in contracts
                        if current[cid] is not None
                        and not bool(current[cid].get("detalhamento_completo"))
                    ]
                    dynamic_ids = [
                        cid for cid in contracts
                        if current[cid] is not None
                        and cid not in set(new_ids)
                        and cid not in set(changed_ids)
                        and cid not in set(incomplete_ids)
                        and dynamic_data_stale(current[cid])
                    ]
                    changed_set = set(changed_ids)
                    new_set = set(new_ids)
                    incomplete_set = set(incomplete_ids)
                    retry_ids = [
                        cid for cid in incomplete_ids
                        if cid not in changed_set and cid not in new_set
                    ]
                    unchanged_ids = [
                        cid for cid in contracts
                        if current[cid] is not None
                        and cid not in changed_set
                        and cid not in incomplete_set
                    ]
                    priority = list(dict.fromkeys(changed_ids + new_ids + retry_ids))
                    dynamic_candidates = [cid for cid in dynamic_ids if cid not in priority]
                    remaining_capacity = max(0, BATCH_PER_UNIT - len(priority))
                    targets = priority[:BATCH_PER_UNIT]
                    if remaining_capacity:
                        targets.extend(dynamic_candidates[:min(DYNAMIC_BATCH_PER_UNIT, remaining_capacity)])
                    comparison_elapsed = perf_counter() - comparison_started
                    performance["comparison"][unidade] = comparison_elapsed
                    print(f"[{unidade}] COMPARACAO TEMPO={comparison_elapsed:.2f}s", flush=True)
                    print(
                        f"[{unidade}] UNIVERSO={len(contracts)} NOVOS={len(new_ids)} "
                        f"ALTERADOS={len(changed_ids)} INCOMPLETOS_RETRY={len(retry_ids)} "
                        f"DINAMICOS_STALE={len(dynamic_ids)} SEM_MUDANCA={len(unchanged_ids)} "
                        f"FILA_ATUAL={len(targets)}/{BATCH_PER_UNIT}",
                        flush=True,
                    )

                    detail_started = perf_counter()
                    captured = 0
                    detail_errors = []
                    captured_ids = []
                    http_session = cgd_http_detail.session_from_page(page)
                    priority_targets = priority[:BATCH_PER_UNIT]
                    dynamic_targets = [cid for cid in dynamic_candidates if cid not in set(priority_targets)][:max(0, min(DYNAMIC_BATCH_PER_UNIT, BATCH_PER_UNIT - len(priority_targets)))]
                    print(f"[{unidade}] INICIO DETALHAMENTO: HTTP={len(priority_targets)} DINAMICOS_FREQUENCIA={len(dynamic_targets)}", flush=True)
                    for idx, cid in enumerate(priority_targets, 1):
                        try:
                            aluno = detail(page, unidade, cid, reps, signatures[cid], http_session)
                            by_id[(unidade, cid)] = aluno
                            captured += 1
                            captured_ids.append(cid)
                            print(f"[{unidade}] DETALHE_OK {idx}/{len(priority_targets)} cid={cid} modo={aluno.get('detalhamento_modo')}", flush=True)
                        except Exception as exc:
                            detail_errors.append((cid, repr(exc)))
                            print(f"[{unidade}] DETALHE_ERRO cid={cid}: {exc!r}", flush=True)
                        print(f"[{unidade}] PROGRESSO DETALHAMENTO: {idx}/{len(priority_targets)} sucesso={captured} falhas={len(detail_errors)}", flush=True)
                    for idx, cid in enumerate(dynamic_targets, 1):
                        try:
                            aluno = by_id[(unidade, cid)]
                            by_id[(unidade, cid)] = refresh_dynamic(page, unidade, cid, aluno, http_session)
                            print(f"[{unidade}] DINAMICO_OK {idx}/{len(dynamic_targets)} cid={cid}", flush=True)
                        except Exception as exc:
                            detail_errors.append((cid, repr(exc)))
                            print(f"[{unidade}] DINAMICO_ERRO cid={cid}: {exc!r}", flush=True)
                    detail_elapsed = perf_counter() - detail_started
                    performance["detail"][unidade] = detail_elapsed
                    print(
                        f"[{unidade}] DETALHAMENTO TEMPO={detail_elapsed:.2f}s "
                        f"CAPTURADOS={captured} ERROS={len(detail_errors)}",
                        flush=True,
                    )

                    now = datetime.now(timezone.utc).isoformat()
                    for cid in unchanged_ids:
                        aluno = current[cid]
                        aluno["assinatura_universo_cgd"] = signatures[cid]
                        aluno["visto_no_cgd_em"] = now

                    # O snapshot guarda apenas assinaturas já confirmadas na base.
                    # Contratos novos/alterados que falharam na extração não avançam
                    # o hash e serão redescobertos na próxima execução.
                    confirmed_signatures = {
                        cid: by_id[(unidade, cid)].get("assinatura_universo_cgd")
                        for cid in contracts
                        if by_id.get((unidade, cid))
                        and by_id[(unidade, cid)].get("assinatura_universo_cgd")
                    }
                    snapshot["unidades"][unidade] = {
                        "total": len(contracts),
                        "contratos": confirmed_signatures,
                        "novos_detectados": len(new_ids),
                        "alterados_detectados": len(changed_ids),
                        "incompletos_para_retry": len(retry_ids),
                        "dinamicos_stale": len(dynamic_ids),
                        "sem_mudanca": len(unchanged_ids),
                        "lote_planejado": len(priority_targets) + len(dynamic_targets),
                        "teto_seguranca": BATCH_PER_UNIT,
                        "fila_dinamica_planejada": len(dynamic_targets),
                        "capturados_no_lote": captured,
                        "contratos_capturados_no_lote": sorted(captured_ids),
                        "erros_detalhe": len(detail_errors),
                        "paginas_com_erro": listing_errors,
                        "pendentes_apos_lote": sum(
                            1 for cid in contracts
                            if not by_id.get((unidade, cid))
                            or by_id[(unidade, cid)].get("assinatura_universo_cgd") != signatures[cid]
                            or not bool(by_id[(unidade, cid)].get("detalhamento_completo"))
                        ),
                        "detalhe_erros": [{"contrato": cid, "erro": err} for cid, err in detail_errors[:100]],
                        "performance_s": {
                            "descoberta": round(discovery_elapsed, 2),
                            "comparacao": round(comparison_elapsed, 2),
                            "detalhamento": round(detail_elapsed, 2),
                            "unidade_total_ate_aqui": round(perf_counter() - unit_started, 2),
                        },
                    }
                    totals["universo"] += len(contracts)
                    totals["novos"] += len(new_ids)
                    totals["alterados"] += len(changed_ids)
                    totals["capturados"] += captured
                    totals["sem_mudanca"] += len(unchanged_ids)
                    totals["erros_detalhe"] += len(detail_errors)
                finally:
                    context.close()
        finally:
            browser.close()

    merged = list(by_id.values())
    merged.sort(key=lambda a: (str(a.get("unidade") or ""), key(a)))

    persistence_started = perf_counter()
    atomic_write(DATA_PATH, merged)
    atomic_write(SNAPSHOT_PATH, snapshot)
    atomic_write(PENDING_FREQUENCY_PATH, {"source": "CGD", "unidades": pending_frequency, "capturado_em": datetime.now(timezone.utc).isoformat()})
    performance["persistence"] = perf_counter() - persistence_started
    print(f"PERSISTENCIA TEMPO={performance['persistence']:.2f}s", flush=True)

    pending = 0
    base_counts = {}
    for unidade, info in snapshot["unidades"].items():
        base_unit = {key(a) for a in merged if str(a.get("unidade") or "").lower() == unidade and key(a)}
        universe = set(info["contratos"])
        pending += len(universe - base_unit)
        base_counts[unidade] = len(base_unit)
        print(f"[{unidade}] BASE_PRESERVADA={len(base_unit)} UNIVERSO={len(universe)} PENDENTES={len(universe - base_unit)}", flush=True)

    performance["total"] = perf_counter() - total_started
    print("=" * 96, flush=True)
    print(f"PERFORMANCE_MATRIZ_DESCOBERTA={performance['discovery'].get('matriz', 0.0):.2f}s", flush=True)
    print(f"PERFORMANCE_FILIAL_DESCOBERTA={performance['discovery'].get('filial', 0.0):.2f}s", flush=True)
    print(f"PERFORMANCE_MATRIZ_COMPARACAO={performance['comparison'].get('matriz', 0.0):.2f}s", flush=True)
    print(f"PERFORMANCE_FILIAL_COMPARACAO={performance['comparison'].get('filial', 0.0):.2f}s", flush=True)
    print(f"PERFORMANCE_MATRIZ_DETALHES={performance['detail'].get('matriz', 0.0):.2f}s", flush=True)
    print(f"PERFORMANCE_FILIAL_DETALHES={performance['detail'].get('filial', 0.0):.2f}s", flush=True)
    print(f"PERFORMANCE_PERSISTENCIA={performance['persistence']:.2f}s", flush=True)
    print(f"PERFORMANCE_TOTAL={performance['total']:.2f}s", flush=True)
    print("=" * 96, flush=True)
    print(f"UNIVERSO_TOTAL_CGD={totals['universo']}", flush=True)
    print(f"NOVOS_DETECTADOS={totals['novos']}", flush=True)
    print(f"ALTERADOS_DETECTADOS={totals['alterados']}", flush=True)
    print(f"CAPTURADOS_NESTA_RODADA={totals['capturados']}", flush=True)
    print(f"SEM_MUDANCA_PRESERVADOS={totals['sem_mudanca']}", flush=True)
    print(f"ERROS_DETALHE_NESTA_RODADA={totals['erros_detalhe']}", flush=True)
    print(f"BASE_PERSISTIDA={len(merged)}", flush=True)
    print(f"BASE_MATRIZ={base_counts.get('matriz', 0)}", flush=True)
    print(f"BASE_FILIAL={base_counts.get('filial', 0)}", flush=True)
    print(f"PENDENTES_DE_DETALHAMENTO={pending}", flush=True)
    if totals["universo"] <= 0:
        raise RuntimeError("Nenhum contrato descoberto no universo CGD")
    print("LOTE_PERSISTIDO=OK", flush=True)


if __name__ == "__main__":
    main()
