"""Camada de sincronizacao incremental robusta sobre o executor CGD existente.

Mantem a descoberta incremental de novos contratos, mas tambem revisita uma
pequena janela rotativa de alunos ja persistidos. Assim, mudancas recentes no
CGD podem chegar a base sem transformar cada execucao em uma coleta completa.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import scraper
import scraper_runner_threaded as runner
from playwright.sync_api import sync_playwright

JSON_PATH = Path("dados_alunos.json")


def _agora_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contrato(aluno):
    return str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()


def _timestamp(aluno):
    valor = str(aluno.get("ultima_sincronizacao_cgd") or "").strip()
    if not valor:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _load_existing():
    if not JSON_PATH.exists():
        return []
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("dados_alunos.json nao possui raiz lista")
    return data


def _selecionar_refresh(existing, unidade, limite):
    candidatos = [a for a in existing if a.get("unidade") == unidade and _contrato(a)]
    candidatos.sort(key=_timestamp)
    return candidatos[:max(0, limite)]


def _atualizar_existentes(page, unidade, alunos, reps):
    resultados = []
    total = len(alunos)
    for index, antigo in enumerate(alunos, 1):
        cid = _contrato(antigo)
        try:
            atualizado = runner._detalhar_um(page, unidade, antigo, reps, index, total)
            if atualizado:
                atualizado["ultima_sincronizacao_cgd"] = _agora_iso()
                atualizado["primeira_captura_cgd"] = antigo.get("primeira_captura_cgd") or antigo.get("ultima_sincronizacao_cgd") or atualizado["ultima_sincronizacao_cgd"]
                resultados.append(atualizado)
                print(f"[{unidade}] REFRESH_OK cid={cid}", flush=True)
        except Exception as exc:
            print(f"[{unidade}] REFRESH_ERRO cid={cid}: {exc!r}", flush=True)
            if runner._pagina_bloqueada(page):
                raise
    return resultados


def _merge(existing, novos, atualizados):
    por_id = {}
    ordem = []
    for aluno in existing:
        cid = _contrato(aluno)
        chave = cid or f"sem-contrato-{len(ordem)}"
        if chave not in por_id:
            ordem.append(chave)
        por_id[chave] = aluno

    for aluno in novos + atualizados:
        cid = _contrato(aluno)
        if not cid:
            continue
        anterior = por_id.get(cid, {})
        combinado = dict(anterior)
        for chave, valor in aluno.items():
            if valor is None or valor == "":
                if chave in combinado and combinado[chave] not in (None, ""):
                    continue
            combinado[chave] = valor
        por_id[cid] = combinado
        if cid not in ordem:
            ordem.append(cid)

    return [por_id[chave] for chave in ordem]


def _run_unit(unidade, cfg, pw, existing):
    headless = os.getenv("CGD_HEADLESS", "0").lower() in ("1", "true", "yes", "sim")
    refresh_limit = max(0, int(os.getenv("CGD_REFRESH_PER_UNIT", "20")))
    alvo = max(1, int(os.getenv("CGD_DETAIL_TARGET_PER_UNIT", "750")))

    existing_unit = [a for a in existing if a.get("unidade") == unidade and _contrato(a)]
    existing_ids = {_contrato(a) for a in existing_unit}
    novos_necessarios = max(0, alvo - len(existing_unit))
    refresh_alunos = _selecionar_refresh(existing, unidade, refresh_limit)

    print(
        f"[{unidade}] ESTRATEGIA=INCREMENTAL_NOVOS+REFRESH_ROTATIVO "
        f"existentes={len(existing_unit)} alvo={alvo} novos_necessarios={novos_necessarios} "
        f"refresh_planejado={len(refresh_alunos)}",
        flush=True,
    )

    browser = pw.chromium.launch(channel="msedge", headless=headless)
    context = browser.new_context()
    page = context.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        runner._validar_sessao(page, unidade)
        contracts = scraper.discover_contracts(page, unidade, cfg["destino"])
        print(f"[{unidade}] CONTRATOS_DISCOVERED={len(contracts)}", flush=True)
        reps = scraper.get_replacements(page, unidade)
        print(f"[{unidade}] REPOSICOES_GLOBAIS_CAPTURADAS={len(reps)}", flush=True)
        runner._validar_sessao(page, unidade)

        candidatos = [c for c in contracts if scraper.contract_id(c) and scraper.contract_id(c) not in existing_ids]
        novos = []
        if novos_necessarios:
            novos = runner._capturar_detalhes(page, unidade, candidatos, reps, existing_ids)
            agora = _agora_iso()
            for aluno in novos:
                aluno["ultima_sincronizacao_cgd"] = agora
                aluno.setdefault("primeira_captura_cgd", agora)

        atualizados = _atualizar_existentes(page, unidade, refresh_alunos, reps) if refresh_alunos else []
        return novos, atualizados
    finally:
        context.close()
        browser.close()


def main():
    print("=" * 80, flush=True)
    print("SCRAPER CGD - INCREMENTAL ROBUSTO: NOVOS + REFRESH ROTATIVO", flush=True)
    print("Dados existentes nao sao tratados como congelados; uma janela rotativa e sincronizada.", flush=True)
    print("=" * 80, flush=True)

    existing = _load_existing()
    todos_novos = []
    todos_atualizados = []

    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            try:
                novos, atualizados = _run_unit(unidade, scraper.CONFIG[unidade], pw, existing)
                todos_novos.extend(novos)
                todos_atualizados.extend(atualizados)
                print(f"[{unidade}] RESULTADO novos={len(novos)} refresh_ok={len(atualizados)}", flush=True)
            except Exception as exc:
                print(f"[{unidade}] ERRO FATAL UNIDADE: {exc!r}", flush=True)

    merged = _merge(existing, todos_novos, todos_atualizados)
    JSON_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    matriz = sum(1 for a in merged if a.get("unidade") == "matriz")
    filial = sum(1 for a in merged if a.get("unidade") == "filial")
    print(f"[INCREMENTAL_ROBUSTO] NOVOS_CAPTURADOS={len(todos_novos)}", flush=True)
    print(f"[INCREMENTAL_ROBUSTO] REFRESH_CAPTURADOS={len(todos_atualizados)}", flush=True)
    print(f"[INCREMENTAL_ROBUSTO] TOTAL_PERSISTIDO={len(merged)} matriz={matriz} filial={filial}", flush=True)
    print("=" * 80, flush=True)


if __name__ == "__main__":
    main()
