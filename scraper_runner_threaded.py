"""Executor CGD: listagem rapida e detalhamento paralelo por contrato."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import scraper
import scraper_runner
from playwright.sync_api import sync_playwright

JSON_PATH = Path("dados_alunos.json")
_FALTA_TOKENS = ("faltou", "falta", "ausente", "nao compareceu", "não compareceu")
_PRESENTE_TOKENS = ("presente", "presenca", "presença", "compareceu")
_CF_MARKERS = ("verifying you are human", "just a moment", "checking your browser", "cf-chl-", "challenge-platform")
_original_contract_bundle = scraper.contract_bundle


def _recalcular_frequencia(aluno):
    faltas = presencas = 0
    for registro in aluno.get("frequencia_raw") or []:
        valores = []
        if registro.get("status"):
            valores.append(scraper.low(registro["status"]))
        valores.extend(scraper.low(v) for v in (registro.get("valores") or []) if v is not None)
        texto = " | ".join(valores)
        if any(token in texto for token in _FALTA_TOKENS):
            faltas += 1
        elif any(token in texto for token in _PRESENTE_TOKENS):
            presencas += 1
    aluno["faltas"] = faltas
    aluno["presencas"] = presencas
    return aluno


def _curso_modal_preservado(page, cid, aluno):
    if aluno.get("disciplinas"):
        return aluno
    url = f"{scraper.CGD_URL.rstrip('/')}/contratos/cursos/modal-demonstrativo-cursos/{cid}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
        page.wait_for_timeout(scraper.PAGE_WAIT_MS)
        deadline = time.monotonic() + max(5, int(os.getenv("CGD_AJAX_WAIT_S", "12")))
        while time.monotonic() < deadline:
            if any(rows for _, rows in scraper.table_data(page)):
                break
            page.wait_for_timeout(500)
        rows = scraper.extract_disciplines(page, url)
        if rows:
            rows, done, cur, fut = scraper.classify(rows)
            aluno["disciplinas"] = rows
            aluno["disciplinas_concluidas"] = done
            aluno["disciplinas_em_andamento"] = cur
            aluno["disciplinas_futuras"] = fut
    except Exception as exc:
        print(f"[{aluno.get('unidade')}] CURSOS_MODAL_ERRO cid={cid}: {exc!r}", flush=True)
    return aluno


def _contract_bundle_preservado(page, cid, unidade, reps):
    aluno = _original_contract_bundle(page, cid, unidade, reps)
    if aluno:
        aluno = _recalcular_frequencia(aluno)
        aluno = _curso_modal_preservado(page, cid, aluno)
    return aluno


scraper.contract_bundle = _contract_bundle_preservado
scraper.discover_contracts = scraper_runner.optimized_discover_contracts


def _pagina_protegida(page):
    try:
        url = (page.url or "").lower()
        texto = scraper.low(page.locator("body").inner_text())[:20000]
        return "/cdn-cgi/challenge" in url or any(marker in texto for marker in _CF_MARKERS)
    except Exception:
        return False


def _validar_sessao(page, unidade):
    page.goto(scraper.CGD_URL, wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
    for tentativa in range(1, 7):
        page.wait_for_timeout(1000)
        if not _pagina_protegida(page):
            print(f"[{unidade}] SESSAO_CGD_VALIDADA tentativa={tentativa} url={page.url}", flush=True)
            return
        page.reload(wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
    raise RuntimeError(f"[{unidade}] CLOUDFLARE_SESSAO_NAO_VALIDADA")


def _frequencia_com_espera(page, cid):
    ultimo = None
    for tentativa, espera in enumerate((0, 750, 1500, 2500, 4000), 1):
        if espera:
            page.wait_for_timeout(espera)
        try:
            resultado = scraper_runner.robust_extract_frequency(page, cid)
            if resultado.get("registros"):
                print(f"[FREQUENCIA_VALIDADA] cid={cid} tentativa={tentativa} registros={len(resultado['registros'])}", flush=True)
                return resultado
            ultimo = RuntimeError("parser retornou zero registros")
        except Exception as exc:
            ultimo = exc
        if tentativa < 5:
            print(f"[FREQUENCIA_AGUARDANDO] cid={cid} tentativa={tentativa}", flush=True)
    raise RuntimeError(f"FREQUENCIA_NAO_CAPTURADA_APOS_ESPERA cid={cid}: {ultimo}")


scraper.extract_frequency = _frequencia_com_espera


def _detalhar_worker(unidade, contratos, reps, storage_state, worker_id, cursor, lock, resultados, falhas):
    """Cada worker possui seu proprio Edge/context/page e consome a fila de contratos."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        try:
            while True:
                with lock:
                    if cursor[0] >= len(contratos):
                        break
                    index = cursor[0]
                    cursor[0] += 1
                contract = contratos[index]
                cid = scraper.contract_id(contract)
                if not cid:
                    continue
                try:
                    print(f"[{unidade}] WORKER={worker_id} INICIO {index + 1}/{len(contratos)} cid={cid}", flush=True)
                    aluno = scraper.contract_bundle(page, cid, unidade, reps)
                    if not aluno or not (aluno.get("frequencia_raw") or []):
                        raise RuntimeError(f"CONTRATO_SEM_FREQUENCIA cid={cid}")
                    with lock:
                        resultados.append(aluno)
                    print(f"[{unidade}] WORKER={worker_id} OK {index + 1}/{len(contratos)} cid={cid} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')}", flush=True)
                except Exception as exc:
                    with lock:
                        falhas.append(cid)
                    print(f"[{unidade}] WORKER={worker_id} ERRO {index + 1}/{len(contratos)} cid={cid}: {exc!r}", flush=True)
        finally:
            context.close()
            browser.close()


def _capturar_detalhes_mesmo_contexto(page, unidade, contracts, reps, existing_ids):
    alvo = max(1, int(os.getenv("CGD_DETAIL_TARGET_PER_UNIT", "3")))
    workers = max(1, int(os.getenv("CGD_DETAIL_WORKERS", "3")))
    diagnostic = os.getenv("CGD_DIAGNOSTICO", "0").lower() in ("1", "true", "yes", "sim")
    candidatos = [c for c in contracts if scraper.contract_id(c) and scraper.contract_id(c) not in existing_ids]
    faltam = max(0, alvo - len(existing_ids))
    selecionados = candidatos[:faltam]
    print(f"[{unidade}] INCREMENTAL_EXISTENTES={len(existing_ids)} ALVO_CUMULATIVO={alvo} NOVOS_NECESSARIOS={faltam} NOVOS_DISPONIVEIS={len(candidatos)} NOVOS_SELECIONADOS={len(selecionados)}", flush=True)
    if diagnostic:
        print(f"[{unidade}] DIAGNOSTICO_INCREMENTAL_ATIVO alvo={alvo}; detalhamento paralelo workers={workers}", flush=True)
    if not selecionados:
        return []

    storage_state = page.context.storage_state()
    resultados, falhas = [], []
    cursor = [0]
    lock = threading.Lock()
    worker_count = min(workers, len(selecionados))
    print(f"[{unidade}] DETALHAMENTO_PARALELO_INICIADO contratos={len(selecionados)} workers={worker_count}", flush=True)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(_detalhar_worker, unidade, selecionados, reps, storage_state, wid, cursor, lock, resultados, falhas) for wid in range(1, worker_count + 1)]
        for future in futures:
            future.result()
    print(f"[{unidade}] DETALHAMENTO_INCREMENTAL_FINALIZADO novos_sucesso={len(resultados)} novos_falhas={len(falhas)} existentes_preservados={len(existing_ids)} alvo={alvo}", flush=True)
    return resultados


def _run_unit_mesma_sessao(unidade, cfg, pw, existing_ids):
    headless = os.getenv("CGD_HEADLESS", "0").lower() in ("1", "true", "yes", "sim")
    print(f"[{unidade}] MODO_SESSAO_UNICA Edge headless={headless}", flush=True)
    browser = pw.chromium.launch(channel="msedge", headless=headless)
    context = browser.new_context()
    page = context.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        _validar_sessao(page, unidade)
        contracts = scraper.discover_contracts(page, unidade, cfg["destino"])
        print(f"[{unidade}] CONTRATOS_DISCOVERED={len(contracts)}", flush=True)
        reps = scraper.get_replacements(page, unidade)
        print(f"[{unidade}] REPOSICOES_GLOBAIS_CAPTURADAS={len(reps)}", flush=True)
        _validar_sessao(page, unidade)
        return _capturar_detalhes_mesmo_contexto(page, unidade, contracts, reps, existing_ids)
    except Exception as exc:
        print(f"[{unidade}] ERRO FATAL SESSAO_UNICA: {exc!r}", flush=True)
        return []
    finally:
        context.close()
        browser.close()


def _load_existing():
    if not JSON_PATH.exists():
        return []
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("dados_alunos.json nao possui raiz lista")
    print(f"[INCREMENTAL] DADOS_EXISTENTES={len(data)}", flush=True)
    return data


def _merge_incremental(existing, novos):
    merged = {}
    for aluno in existing + novos:
        cid = str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()
        if cid:
            merged[cid] = aluno
    return list(merged.values())


def main_incremental():
    print("=" * 80, flush=True)
    print("SCRAPER CGD - COLETA REAL INCREMENTAL / DETALHAMENTO PARALELO", flush=True)
    print("Fluxo: autenticar -> descobrir contratos -> ignorar persistidos -> detalhar somente novos", flush=True)
    print("Contagem do CGD tratada como dinamica; o alvo e cumulativo por unidade.", flush=True)
    print("=" * 80, flush=True)
    existing = _load_existing()
    novos = []
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            existing_ids = {str(a.get("contrato") or a.get("cgd_matricula_id") or "").strip() for a in existing if a.get("unidade") == unidade}
            existing_ids.discard("")
            novos.extend(_run_unit_mesma_sessao(unidade, scraper.CONFIG[unidade], pw, existing_ids))
    merged = _merge_incremental(existing, novos)
    JSON_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    matriz = sum(1 for a in merged if a.get("unidade") == "matriz")
    filial = sum(1 for a in merged if a.get("unidade") == "filial")
    print(f"[INCREMENTAL] NOVOS_CAPTURADOS={len(novos)}", flush=True)
    print(f"[INCREMENTAL] TOTAL_PERSISTIDO={len(merged)} matriz={matriz} filial={filial}", flush=True)
    print("=" * 80, flush=True)


scraper.main = main_incremental

if __name__ == "__main__":
    main_incremental()
