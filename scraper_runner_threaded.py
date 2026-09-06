"""Executor CGD: listagem rapida e detalhamento sequencial protegido."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import scraper
import scraper_runner
from playwright.sync_api import sync_playwright

JSON_PATH = Path("dados_alunos.json")
_CF_MARKERS = (
    "sorry, you have been blocked",
    "you have been blocked",
    "verifying you are human",
    "just a moment",
    "checking your browser",
    "cf-chl-",
    "challenge-platform",
)

# Mantem o parser robusto de frequencia ja validado no runner.
scraper.discover_contracts = scraper_runner.optimized_discover_contracts
_original_contract_bundle = scraper.contract_bundle


def _pagina_bloqueada(page):
    try:
        url = (page.url or "").lower()
        texto = scraper.low(page.locator("body").inner_text())[:20000]
        return any(marker in url or marker in texto for marker in _CF_MARKERS)
    except Exception:
        return False


def _validar_sessao(page, unidade):
    page.goto(scraper.CGD_URL, wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
    page.wait_for_timeout(1000)
    if _pagina_bloqueada(page):
        raise RuntimeError(f"[{unidade}] CLOUDFLARE_BLOQUEIO_SESSAO url={page.url}")
    print(f"[{unidade}] SESSAO_CGD_VALIDADA url={page.url}", flush=True)


def _frequencia_tolerante(page, cid):
    """Zero registros pode ser um estado real do aluno, nao falha de scraping."""
    try:
        return scraper_runner.robust_extract_frequency(page, cid)
    except RuntimeError as exc:
        if "FREQUENCIA_NAO_CAPTURADA" in str(exc):
            print(f"[FREQUENCIA] cid={cid} SEM_REGISTROS_REAIS", flush=True)
            return {"faltas": 0, "presencas": 0, "registros": []}
        raise


scraper.extract_frequency = _frequencia_tolerante


def _parse_datas(texto):
    datas = []
    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", texto or ""):
        try:
            d, mth, y = map(int, m.groups())
            if y < 100:
                y += 2000
            datas.append(datetime(y, mth, d).date())
        except ValueError:
            pass
    return datas


def _classificar_sem_frequencia(aluno):
    """Classifica zero frequencia sem confundir aluno novo com problema de atendimento."""
    if aluno.get("frequencia_raw"):
        return "COM_FREQUENCIA_REAL"

    hoje = datetime.utcnow().date()
    textos = " ".join(
        str(aluno.get(k) or "")
        for k in ("horarios", "aluno_raw", "disciplinas", "disciplinas_futuras")
    )
    datas = _parse_datas(textos)
    futuras = [d for d in datas if d >= hoje]
    if futuras:
        return "SEM_FREQUENCIA_AGUARDANDO_INICIO"
    return "SEM_FREQUENCIA_A_INVESTIGAR"


def _detalhar_um(page, unidade, contract, reps, index, total):
    cid = scraper.contract_id(contract)
    if not cid:
        return None
    print(f"[{unidade}] DETALHE {index}/{total} INICIO cid={cid}", flush=True)
    aluno = _original_contract_bundle(page, cid, unidade, reps)
    if not aluno:
        raise RuntimeError(f"[{unidade}] CONTRATO_SEM_RESULTADO cid={cid}")
    if _pagina_bloqueada(page):
        raise RuntimeError(f"[{unidade}] CLOUDFLARE_BLOQUEIO_DURANTE_DETALHE cid={cid}")

    aluno["frequencia_status"] = _classificar_sem_frequencia(aluno)
    if not aluno.get("frequencia_raw"):
        print(
            f"[{unidade}] DETALHE {index}/{total} OK_SEM_FREQUENCIA "
            f"cid={cid} nome={aluno.get('nome')} status={aluno['frequencia_status']}",
            flush=True,
        )
    else:
        print(
            f"[{unidade}] DETALHE {index}/{total} OK cid={cid} "
            f"nome={aluno.get('nome')} faltas={aluno.get('faltas')} "
            f"presencas={aluno.get('presencas')} "
            f"freq_registros={len(aluno.get('frequencia_raw') or [])}",
            flush=True,
        )
    return aluno


def _capturar_detalhes(page, unidade, contracts, reps, existing_ids):
    alvo = max(1, int(os.getenv("CGD_DETAIL_TARGET_PER_UNIT", "3")))
    candidatos = [
        c for c in contracts
        if scraper.contract_id(c) and scraper.contract_id(c) not in existing_ids
    ]
    faltam = max(0, alvo - len(existing_ids))
    print(
        f"[{unidade}] INCREMENTAL_EXISTENTES={len(existing_ids)} "
        f"ALVO_CUMULATIVO={alvo} NOVOS_NECESSARIOS={faltam} "
        f"NOVOS_DISPONIVEIS={len(candidatos)} "
        f"ESTRATEGIA=continuar_ate_atingir_alvo",
        flush=True,
    )
    if not candidatos or faltam == 0:
        return []

    resultados = []
    falhas = []
    intervalo = max(0, int(os.getenv("CGD_DETAIL_INTERVAL_MS", "1200")))

    for index, contract in enumerate(candidatos, 1):
        if len(resultados) >= faltam:
            break

        cid = scraper.contract_id(contract)
        try:
            aluno = _detalhar_um(page, unidade, contract, reps, index, len(candidatos))
            if aluno:
                resultados.append(aluno)
        except Exception as exc:
            falhas.append(cid)
            print(f"[{unidade}] DETALHE {index}/{len(candidatos)} ERRO cid={cid}: {exc!r}", flush=True)
            if _pagina_bloqueada(page):
                raise RuntimeError(
                    f"[{unidade}] BLOQUEIO_CGD_ABORTANDO_PARA_NAO_ESCALAR: cid={cid}"
                ) from exc

        if index < len(candidatos) and len(resultados) < faltam and intervalo:
            print(f"[{unidade}] PAUSA_PROTECAO {intervalo}ms", flush=True)
            page.wait_for_timeout(intervalo)

    print(
        f"[{unidade}] DETALHAMENTO_FINALIZADO novos_sucesso={len(resultados)} "
        f"novos_falhas={len(falhas)} existentes_preservados={len(existing_ids)} "
        f"alvo={alvo} candidatos_tentados={min(len(candidatos), len(resultados) + len(falhas))}",
        flush=True,
    )
    return resultados


def _run_unit(unidade, cfg, pw, existing_ids):
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
        return _capturar_detalhes(page, unidade, contracts, reps, existing_ids)
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
    print("SCRAPER CGD - COLETA REAL INCREMENTAL / DETALHAMENTO PROTEGIDO", flush=True)
    print("Fluxo: autenticar -> listagem HTTP paralela -> ignorar persistidos -> detalhar em uma sessao", flush=True)
    print("Detalhamento sequencial; zero frequencia e classificada, nao descartada automaticamente.", flush=True)
    print("=" * 80, flush=True)
    existing = _load_existing()
    novos = []
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            existing_ids = {
                str(a.get("contrato") or a.get("cgd_matricula_id") or "").strip()
                for a in existing
                if a.get("unidade") == unidade
            }
            existing_ids.discard("")
            try:
                novos.extend(_run_unit(unidade, scraper.CONFIG[unidade], pw, existing_ids))
            except Exception as exc:
                print(f"[{unidade}] ERRO FATAL UNIDADE: {exc!r}", flush=True)

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
