"""Executor CGD: listagem rapida, detalhe sequencial e metadados de contrato."""

import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

import scraper
import scraper_runner
from playwright.sync_api import sync_playwright

# O runner Windows pode iniciar o stdout/stderr em CP1252. O CGD pode retornar
# caracteres Unicode que nao existem nessa tabela, e um simples print() nao pode
# derrubar toda a coleta. Forcamos UTF-8 e, como ultima defesa, substituimos apenas
# caracteres impossiveis de representar no console.
def _configurar_saida_unicode():
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configurar_saida_unicode()

JSON_PATH = Path("dados_alunos.json")
_CF_MARKERS = (
    "sorry, you have been blocked", "you have been blocked", "verifying you are human",
    "just a moment", "checking your browser", "cf-chl-", "challenge-platform",
)

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
        raise RuntimeError(f"[{unidade}] BLOQUEIO_CGD_SESSAO url={page.url}")
    print(f"[{unidade}] SESSAO_CGD_VALIDADA url={page.url}", flush=True)


def _frequencia_tolerante(page, cid):
    try:
        return scraper_runner.robust_extract_frequency(page, cid)
    except RuntimeError as exc:
        if "FREQUENCIA_NAO_CAPTURADA" in str(exc):
            print(f"[FREQUENCIA] cid={cid} SEM_REGISTROS_REAIS", flush=True)
            return {"faltas": 0, "presencas": 0, "registros": []}
        raise


scraper.extract_frequency = _frequencia_tolerante


def _norm_date(value):
    text = scraper.norm(value)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mth, d).isoformat()
    except ValueError:
        return None


def _campo_por_rotulo(page, rotulos):
    """Tenta obter o valor de um campo pelo label/atributo antes do fallback textual."""
    wanted = [scraper.low(x) for x in rotulos]
    try:
        labels = page.locator("label")
        for i in range(labels.count()):
            label = labels.nth(i)
            texto = scraper.low(label.inner_text())
            if not any(w in texto for w in wanted):
                continue
            alvo = label.get_attribute("for")
            if alvo:
                loc = page.locator(f"#{alvo}")
                if loc.count():
                    try:
                        return scraper.norm(loc.first.input_value())
                    except Exception:
                        return scraper.norm(loc.first.inner_text())
            parent = label.locator("xpath=..")
            for sel in ("input", "select", "textarea"):
                loc = parent.locator(sel)
                if loc.count():
                    try:
                        v = loc.first.input_value()
                    except Exception:
                        v = loc.first.inner_text()
                    if scraper.norm(v):
                        return scraper.norm(v)
    except Exception:
        pass
    return None


def _campo_textual(texto, rotulos):
    texto = scraper.norm(texto)
    for rotulo in rotulos:
        padroes = (
            rf"(?:^|\s){re.escape(rotulo)}\s*[:\-]?\s*([^|\n]{{1,180}})",
            rf"{re.escape(rotulo)}\s+([^|\n]{{1,180}})",
        )
        for padrao in padroes:
            m = re.search(padrao, texto, re.I)
            if m:
                valor = scraper.norm(m.group(1))
                valor = re.split(r"\s+(?:Campanha|Pacote|Plano|Produto|Status|Situa[cç][aã]o|Data)\s*[:\-]?\s*", valor, maxsplit=1, flags=re.I)[0]
                if valor and len(valor) <= 180:
                    return valor
    return None


def _extrair_metadados_contrato(page, unidade, cid):
    """Extrai somente metadados que realmente pertencem ao contrato CGD.

    Nao usa datas aleatorias de frequencia/disciplinas para decidir se o aluno iniciou.
    """
    url = scraper.contract_url(cid)
    if not scraper.open_page(page, url, unidade, f"metadados_contrato_{cid}"):
        return {
            "campanha": None, "pacote": None, "status_contrato": None,
            "data_inicio": None, "data_fim": None, "contrato_raw": "",
        }
    texto = scraper.body(page)[:30000]
    campanha = _campo_por_rotulo(page, ("campanha", "campanha comercial")) or _campo_textual(texto, ("Campanha",))
    pacote = _campo_por_rotulo(page, ("pacote", "pacote comercial")) or _campo_textual(texto, ("Pacote",))
    status = _campo_por_rotulo(page, ("status", "situação", "situacao", "estado")) or _campo_textual(texto, ("Status", "Situação", "Situacao", "Estado"))
    inicio = _campo_por_rotulo(page, ("data de início", "data início", "inicio", "início")) or _campo_textual(texto, ("Data de início", "Data início", "Início", "Inicio"))
    fim = _campo_por_rotulo(page, ("data de fim", "data fim", "fim", "encerramento")) or _campo_textual(texto, ("Data de fim", "Data fim", "Fim", "Encerramento"))
    return {
        "campanha": scraper.norm(campanha) or None,
        "pacote": scraper.norm(pacote) or None,
        "status_contrato": scraper.norm(status) or None,
        "data_inicio": _norm_date(inicio),
        "data_fim": _norm_date(fim),
        "contrato_raw": texto,
    }


def _classificar_sem_frequencia(aluno):
    if aluno.get("frequencia_raw"):
        return "COM_FREQUENCIA_REAL"
    inicio = aluno.get("data_inicio")
    status = scraper.low(aluno.get("status_contrato"))
    hoje = datetime.utcnow().date()
    if inicio:
        try:
            inicio_date = date.fromisoformat(inicio)
            if inicio_date > hoje:
                return "SEM_FREQUENCIA_AGUARDANDO_INICIO"
        except ValueError:
            pass
    if any(x in status for x in ("cancel", "encerr", "inativ", "arquiv")):
        return "SEM_FREQUENCIA_CONTRATO_ENCERRADO"
    return "SEM_FREQUENCIA_A_INVESTIGAR"


def _marcar_populacao(aluno):
    """Nao aplica filtro arbitrario de curso/campanha.

    A flag identifica candidatos para a etapa de selecao inteligente; a regra definitiva
    sera validada contra dashboard/campanha/pacote do CGD antes de restringir a coleta.
    """
    status = scraper.low(aluno.get("status_contrato") or aluno.get("status"))
    ativo = not any(x in status for x in ("cancel", "encerr", "inativ", "arquiv"))
    aluno["monitoramento_candidato"] = bool(ativo)
    aluno["selecao_populacao_status"] = "CANDIDATO_PRE_VALIDACAO" if ativo else "FORA_CONTRATO_ATIVO"
    return aluno


def _detalhar_um(page, unidade, contract, reps, index, total):
    cid = scraper.contract_id(contract)
    if not cid:
        return None
    print(f"[{unidade}] DETALHE {index}/{total} INICIO cid={cid}", flush=True)
    aluno = _original_contract_bundle(page, cid, unidade, reps)
    if not aluno:
        raise RuntimeError(f"[{unidade}] CONTRATO_SEM_RESULTADO cid={cid}")
    if _pagina_bloqueada(page):
        raise RuntimeError(f"[{unidade}] BLOQUEIO_CGD_DURANTE_DETALHE cid={cid}")

    meta = _extrair_metadados_contrato(page, unidade, cid)
    aluno.update(meta)
    aluno["frequencia_status"] = _classificar_sem_frequencia(aluno)
    _marcar_populacao(aluno)

    print(
        f"[{unidade}] METADADOS cid={cid} campanha={aluno.get('campanha')!r} "
        f"pacote={aluno.get('pacote')!r} inicio={aluno.get('data_inicio')!r} "
        f"status={aluno.get('status_contrato')!r} frequencia={aluno.get('frequencia_status')}",
        flush=True,
    )
    if not aluno.get("frequencia_raw"):
        print(f"[{unidade}] DETALHE {index}/{total} OK_SEM_FREQUENCIA cid={cid}", flush=True)
    else:
        print(
            f"[{unidade}] DETALHE {index}/{total} OK cid={cid} "
            f"faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} "
            f"freq_registros={len(aluno.get('frequencia_raw') or [])}", flush=True,
        )
    return aluno


def _capturar_detalhes(page, unidade, contracts, reps, existing_ids):
    alvo = max(1, int(os.getenv("CGD_DETAIL_TARGET_PER_UNIT", "3")))
    candidatos = [c for c in contracts if scraper.contract_id(c) and scraper.contract_id(c) not in existing_ids]
    faltam = max(0, alvo - len(existing_ids))
    print(
        f"[{unidade}] INCREMENTAL_EXISTENTES={len(existing_ids)} ALVO_CUMULATIVO={alvo} "
        f"NOVOS_NECESSARIOS={faltam} NOVOS_DISPONIVEIS={len(candidatos)} "
        f"ESTRATEGIA=continuar_ate_atingir_alvo",
        flush=True,
    )
    if not candidatos or faltam == 0:
        return []
    resultados, falhas = [], []
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
                raise RuntimeError(f"[{unidade}] BLOQUEIO_CGD_ABORTANDO cid={cid}") from exc
        if index < len(candidatos) and len(resultados) < faltam and intervalo:
            page.wait_for_timeout(intervalo)
    print(
        f"[{unidade}] DETALHAMENTO_FINALIZADO novos_sucesso={len(resultados)} "
        f"novos_falhas={len(falhas)} existentes_preservados={len(existing_ids)} "
        f"alvo={alvo} candidatos_tentados={min(len(candidatos), len(resultados)+len(falhas))}", flush=True,
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
    print("SCRAPER CGD - COLETA REAL INCREMENTAL / METADADOS + FREQUENCIA", flush=True)
    print("Fluxo: autenticar -> listagem -> ignorar persistidos -> detalhe -> metadados contrato", flush=True)
    print("Zero frequencia permanece na base e recebe classificacao explicita.", flush=True)
    print("=" * 80, flush=True)
    existing = _load_existing()
    novos = []
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            existing_ids = {
                str(a.get("contrato") or a.get("cgd_matricula_id") or "").strip()
                for a in existing if a.get("unidade") == unidade
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
