"""Executor CGD: mesma sessao autenticada e captura incremental por contrato."""

import json
import os
import time
from pathlib import Path

import scraper
import scraper_runner
from playwright.sync_api import sync_playwright


_original_contract_bundle = scraper.contract_bundle
JSON_PATH = Path("dados_alunos.json")
_FALTA_TOKENS = ("faltou", "falta", "ausente", "nao compareceu", "não compareceu")
_PRESENTE_TOKENS = ("presente", "presenca", "presença", "compareceu")
_CF_MARKERS = (
    "verifying you are human",
    "just a moment",
    "checking your browser",
    "cf-chl-",
    "challenge-platform",
)


def _recalcular_frequencia(aluno):
    registros = aluno.get("frequencia_raw") or []
    faltas = 0
    presencas = 0
    for registro in registros:
        valores = []
        status = registro.get("status")
        if status:
            valores.append(scraper.low(status))
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
            dados = scraper.table_data(page)
            if any(rows for _, rows in dados):
                break
            page.wait_for_timeout(500)
        rows = scraper.extract_disciplines(page, url)
        if rows:
            rows, done, cur, fut = scraper.classify(rows)
            aluno["disciplinas"] = rows
            aluno["disciplinas_concluidas"] = done
            aluno["disciplinas_em_andamento"] = cur
            aluno["disciplinas_futuras"] = fut
            print(f"[{aluno.get('unidade')}] CURSOS_MODAL_CAPTURADOS cid={cid} registros={len(rows)}", flush=True)
    except Exception as exc:
        print(f"[{aluno.get('unidade')}] CURSOS_MODAL_ERRO cid={cid}: {exc!r}", flush=True)
    return aluno


def _contract_bundle_preservado(page, cid, u, reps):
    aluno = _original_contract_bundle(page, cid, u, reps)
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
            cookies = {c["name"] for c in page.context.cookies(scraper.CGD_URL)}
            print(f"[{unidade}] SESSAO_CGD_VALIDADA tentativa={tentativa} cf_clearance={'cf_clearance' in cookies} cf_bm={'__cf_bm' in cookies} url={page.url}", flush=True)
            return
        print(f"[{unidade}] CLOUDFLARE_AINDA_PRESENTE tentativa={tentativa} url={page.url}", flush=True)
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
                print(f"[FREQUENCIA_VALIDADA] cid={cid} tentativa={tentativa} registros={len(resultado['registros'])} faltas={resultado.get('faltas', 0)} presencas={resultado.get('presencas', 0)}", flush=True)
                return resultado
            ultimo = RuntimeError("parser retornou zero registros")
        except Exception as exc:
            ultimo = exc
        if tentativa < 5:
            print(f"[FREQUENCIA_AGUARDANDO] cid={cid} tentativa={tentativa}", flush=True)
    raise RuntimeError(f"FREQUENCIA_NAO_CAPTURADA_APOS_ESPERA cid={cid}: {ultimo}")


scraper.extract_frequency = _frequencia_com_espera


def _capturar_detalhes_mesmo_contexto(page, u, contracts, reps, existing_ids):
    """Processa apenas contratos novos ate o alvo cumulativo da unidade."""
    alvo = max(1, int(os.getenv("CGD_DETAIL_TARGET_PER_UNIT", "3")))
    diagnostic = os.getenv("CGD_DIAGNOSTICO", "0").lower() in ("1", "true", "yes", "sim")
    candidatos = []
    for contract in contracts:
        cid = scraper.contract_id(contract)
        if cid and cid not in existing_ids:
            candidatos.append(contract)

    faltam = max(0, alvo - len(existing_ids))
    selecionados = candidatos[:faltam]
    print(f"[{u}] INCREMENTAL_EXISTENTES={len(existing_ids)} ALVO_CUMULATIVO={alvo} NOVOS_NECESSARIOS={faltam} NOVOS_DISPONIVEIS={len(candidatos)} NOVOS_SELECIONADOS={len(selecionados)}", flush=True)
    if diagnostic:
        print(f"[{u}] DIAGNOSTICO_INCREMENTAL_ATIVO alvo={alvo}; limite agora significa alvo cumulativo, nao refazer os primeiros contratos", flush=True)

    resultados = []
    falhas = []
    for index, contract in enumerate(selecionados, 1):
        cid = scraper.contract_id(contract)
        try:
            print(f"[{u}] CONTRATO_INICIO_NOVO {index}/{len(selecionados)} cid={cid}", flush=True)
            aluno = scraper.contract_bundle(page, cid, u, reps)
            if not aluno:
                raise RuntimeError(f"CONTRATO_SEM_DADOS cid={cid}")
            if not (aluno.get("frequencia_raw") or []):
                raise RuntimeError(f"CONTRATO_SEM_FREQUENCIA cid={cid}")
            resultados.append(aluno)
            print(f"[{u}] CONTRATO_OK_NOVO {index}/{len(selecionados)} cid={cid} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} freq_registros={len(aluno.get('frequencia_raw') or [])}", flush=True)
        except Exception as exc:
            falhas.append(cid)
            print(f"[{u}] CONTRATO_ERRO_NOVO {index}/{len(selecionados)} cid={cid}: {exc!r}", flush=True)

    print(f"[{u}] DETALHAMENTO_INCREMENTAL_FINALIZADO novos_sucesso={len(resultados)} novos_falhas={len(falhas)} existentes_preservados={len(existing_ids)} alvo={alvo}", flush=True)
    return resultados


def _run_unit_mesma_sessao(u, cfg, pw, existing_ids):
    headless = os.getenv("CGD_HEADLESS", "0").lower() in ("1", "true", "yes", "sim")
    print(f"[{u}] MODO_SESSAO_UNICA Edge headless={headless}", flush=True)
    browser = pw.chromium.launch(channel="msedge", headless=headless)
    context = browser.new_context()
    page = context.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], u)
        _validar_sessao(page, u)
        contracts = scraper.discover_contracts(page, u, cfg["destino"])
        print(f"[{u}] CONTRATOS_DISCOVERED={len(contracts)}", flush=True)
        reps = scraper.get_replacements(page, u)
        print(f"[{u}] REPOSICOES_GLOBAIS_CAPTURADAS={len(reps)}", flush=True)
        _validar_sessao(page, u)
        return _capturar_detalhes_mesmo_contexto(page, u, contracts, reps, existing_ids)
    except Exception as exc:
        print(f"[{u}] ERRO FATAL SESSAO_UNICA: {exc!r}", flush=True)
        return []
    finally:
        try:
            context.close()
        finally:
            browser.close()


def _run_unit_wrapper(u, cfg, pw, existing_ids):
    return _run_unit_mesma_sessao(u, cfg, pw, existing_ids)


def _load_existing():
    if not JSON_PATH.exists():
        return []
    try:
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("dados_alunos.json nao possui raiz lista")
        print(f"[INCREMENTAL] DADOS_EXISTENTES={len(data)}", flush=True)
        return data
    except Exception as exc:
        raise RuntimeError(f"Falha lendo dados_alunos.json existente: {exc}") from exc


def _merge_incremental(existing, novos):
    merged = {}
    for aluno in existing:
        cid = str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()
        if cid:
            merged[cid] = aluno
    for aluno in novos:
        cid = str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()
        if cid:
            merged[cid] = aluno
    return list(merged.values())


def main_incremental():
    print("=" * 80, flush=True)
    print("SCRAPER CGD - COLETA REAL INCREMENTAL POR CONTRATO / UNIDADE", flush=True)
    print("Fluxo: autenticar -> descobrir contratos -> ignorar contratos ja persistidos -> capturar somente novos", flush=True)
    print("Contagem do CGD tratada como dinamica; o alvo e cumulativo por unidade.", flush=True)
    print("=" * 80, flush=True)

    existing = _load_existing()
    novos = []
    with sync_playwright() as pw:
        for u in ("matriz", "filial"):
            existing_ids = {str(a.get("contrato") or a.get("cgd_matricula_id") or "").strip() for a in existing if a.get("unidade") == u}
            existing_ids.discard("")
            novos.extend(_run_unit_wrapper(u, scraper.CONFIG[u], pw, existing_ids))

    merged = _merge_incremental(existing, novos)
    JSON_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    por_unidade = {u: sum(1 for a in merged if a.get("unidade") == u) for u in ("matriz", "filial")}
    print(f"[INCREMENTAL] NOVOS_CAPTURADOS={len(novos)}", flush=True)
    print(f"[INCREMENTAL] TOTAL_PERSISTIDO={len(merged)} matriz={por_unidade['matriz']} filial={por_unidade['filial']}", flush=True)
    print("=" * 80, flush=True)


scraper.main = main_incremental

if __name__ == "__main__":
    main_incremental()
