"""Auditoria/atualizacao da base incremental usando a sessao real do CGD.

Nao altera a descoberta de 831 paginas nem a regra incremental. Reprocessa somente
os contratos ja persistidos que precisam de auditoria, preservando o registro por
contrato e capturando tambem Dashboard, Pacotes de cursos e relatorio por pacote.
"""
import json
import re
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import scraper
import scraper_runner_threaded as runner

ALUNOS = Path("dados_alunos.json")
DASHBOARD = Path("dados_dashboard_cgd.json")
PACOTES = Path("dados_pacotes_cursos.json")


def norm(v):
    return " ".join(str(v or "").replace("\xa0", " ").split())


def low(v):
    return norm(v).lower()


def parse_date(v):
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", str(v or ""))
    if not m:
        return None
    y = int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def control_candidates(page):
    """Retorna candidatos de campos com contexto suficiente para identificar rotulos."""
    out = []
    controls = page.locator("input,select,textarea")
    for i in range(controls.count()):
        c = controls.nth(i)
        try:
            attrs = " | ".join(filter(None, [
                c.get_attribute("name"), c.get_attribute("id"),
                c.get_attribute("placeholder"), c.get_attribute("aria-label"),
            ]))
            value = c.input_value() if c.get_attribute("type") != "file" else ""
            parent = c.locator("xpath=..")
            context = norm(parent.inner_text())
            if not context:
                context = attrs
            out.append({"attrs": norm(attrs), "value": norm(value), "context": context[:500]})
        except Exception:
            pass
    return out


def find_field(page, terms):
    wanted = [low(x) for x in terms]
    # 1) label explicitamente associado ao controle.
    try:
        labels = page.locator("label")
        for i in range(labels.count()):
            label = labels.nth(i)
            lt = low(label.inner_text())
            if not any(t in lt for t in wanted):
                continue
            target = label.get_attribute("for")
            if target:
                c = page.locator(f"#{target}")
                if c.count():
                    try:
                        v = c.first.input_value()
                    except Exception:
                        v = c.first.inner_text()
                    if norm(v):
                        return norm(v)
            parent = label.locator("xpath=..")
            for sel in ("input", "select", "textarea"):
                c = parent.locator(sel)
                if c.count():
                    try:
                        v = c.first.input_value()
                    except Exception:
                        v = c.first.inner_text()
                    if norm(v):
                        return norm(v)
    except Exception:
        pass
    # 2) atributos + contexto do controle.
    for item in control_candidates(page):
        hay = low(" | ".join((item["attrs"], item["context"])))
        if any(t in hay for t in wanted) and item["value"]:
            return item["value"]
    # 3) linhas de tabela/descricao com rotulo na primeira coluna.
    try:
        rows = page.locator("tr")
        for i in range(rows.count()):
            cells = rows.nth(i).locator("th,td")
            if cells.count() < 2:
                continue
            label = low(cells.nth(0).inner_text())
            if any(t in label for t in wanted):
                value = norm(cells.nth(1).inner_text())
                if value:
                    return value
    except Exception:
        pass
    return None


def extract_metadata(page, unidade, cid):
    if not scraper.open_page(page, scraper.contract_url(cid), unidade, f"auditoria_contrato_{cid}"):
        return {}
    text = page.locator("body").inner_text(timeout=5000)
    campaign = find_field(page, ("campanha", "campanha comercial"))
    package = find_field(page, ("pacote de cursos", "pacote", "plano de cursos"))
    status = find_field(page, ("status", "situação", "situacao", "estado"))
    start = find_field(page, ("data de início", "data início", "inicio", "início"))
    end = find_field(page, ("data de fim", "data fim", "fim", "encerramento"))
    # Mantem evidencia textual do contrato para auditoria posterior.
    return {
        "campanha": norm(campaign) or None,
        "pacote": norm(package) or None,
        "status_contrato": norm(status) or None,
        "data_inicio": parse_date(start),
        "data_fim": parse_date(end),
        "contrato_raw": norm(text)[:30000],
    }


def classify_zero(aluno):
    if aluno.get("frequencia_raw"):
        return "COM_FREQUENCIA_REAL"
    status = low(aluno.get("status_contrato"))
    today = datetime.utcnow().date()
    inicio = aluno.get("data_inicio")
    fim = aluno.get("data_fim")
    if inicio:
        try:
            if date.fromisoformat(inicio) > today:
                return "SEM_FREQUENCIA_AGUARDANDO_INICIO"
        except ValueError:
            pass
    if any(x in status for x in ("cancel", "encerr", "inativ", "arquiv")):
        return "SEM_FREQUENCIA_CONTRATO_ENCERRADO"
    if fim:
        try:
            if date.fromisoformat(fim) < today:
                return "SEM_FREQUENCIA_CONTRATO_ENCERRADO"
        except ValueError:
            pass
    return "SEM_FREQUENCIA_A_INVESTIGAR"


def invariant_frequency(aluno):
    records = aluno.get("frequencia_raw") or []
    if not records:
        return {"ok": True, "registros": 0}
    valid = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        status = low(r.get("status") or r.get("classificacao"))
        if any(x in status for x in ("falta", "presente")):
            valid += 1
    return {"ok": valid == len(records), "registros": len(records), "registros_validos": valid}


def tables(page):
    out = []
    for heads, rows in scraper.table_data(page):
        out.append({"cabecalhos": heads, "linhas": rows})
    return out


def snapshot_route(page, unidade, path, label):
    url = scraper.CGD_URL.rstrip("/") + path
    if not scraper.open_page(page, url, unidade, label, 500):
        return {"unidade": unidade, "url": url, "ok": False, "texto": "", "tabelas": []}
    return {
        "unidade": unidade,
        "url": page.url,
        "ok": True,
        "texto": norm(page.locator("body").inner_text(timeout=5000))[:50000],
        "tabelas": tables(page),
        "capturado_em": datetime.utcnow().isoformat() + "Z",
    }


def main():
    alunos = json.loads(ALUNOS.read_text(encoding="utf-8"))
    if not isinstance(alunos, list) or not alunos:
        raise SystemExit("dados_alunos.json vazio/invalido")
    by_unit = {u: [a for a in alunos if a.get("unidade") == u] for u in ("matriz", "filial")}
    dashboard, packages = [], []
    refresh = 0
    freq_real_before = sum(bool(a.get("frequencia_raw")) for a in alunos)

    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            cfg = scraper.CONFIG[unidade]
            browser = pw.chromium.launch(channel="msedge", headless=False)
            context = browser.new_context()
            page = context.new_page()
            try:
                scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
                runner._validar_sessao(page, unidade)
                dashboard.append(snapshot_route(page, unidade, "/dashboard", "dashboard_referencia"))
                packages.append(snapshot_route(page, unidade, "/pacotes-cursos", "pacotes_cursos"))
                packages.append(snapshot_route(page, unidade, "/relatorios/individuais/matriculados-por-pacotes", "relatorio_matriculados_por_pacote"))
                reps = scraper.get_replacements(page, unidade)
                for idx, aluno in enumerate(by_unit[unidade], 1):
                    cid = str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()
                    if not cid:
                        continue
                    print(f"[{unidade}] AUDITORIA {idx}/{len(by_unit[unidade])} cid={cid}", flush=True)
                    atualizado = runner._original_contract_bundle(page, cid, unidade, reps)
                    if not atualizado:
                        print(f"[{unidade}] AUDITORIA_ERRO cid={cid}: bundle vazio", flush=True)
                        continue
                    meta = extract_metadata(page, unidade, cid)
                    atualizado.update(meta)
                    atualizado["frequencia_status"] = classify_zero(atualizado)
                    inv = invariant_frequency(atualizado)
                    atualizado["frequencia_validacao_interna"] = inv
                    # A auditoria nao aceita um parser que produza registros com status desconhecido.
                    if not inv["ok"]:
                        raise RuntimeError(f"[{unidade}] FREQUENCIA_REGISTRO_INVALIDO cid={cid}: {inv}")
                    # Preserva campos calculados/compatibilidade do registro anterior.
                    for key in ("criticidade", "dias_desde_ultimo_acesso"):
                        if key in aluno and not atualizado.get(key):
                            atualizado[key] = aluno.get(key)
                    aluno.clear(); aluno.update(atualizado)
                    refresh += 1
            finally:
                context.close(); browser.close()

    DASHBOARD.write_text(json.dumps({"source": "CGD", "unidades": dashboard}, ensure_ascii=False, indent=2), encoding="utf-8")
    PACOTES.write_text(json.dumps({"source": "CGD", "unidades": packages}, ensure_ascii=False, indent=2), encoding="utf-8")
    ALUNOS.write_text(json.dumps(alunos, ensure_ascii=False, indent=2), encoding="utf-8")
    real_after = sum(bool(a.get("frequencia_raw")) for a in alunos)
    sem = [a for a in alunos if not a.get("frequencia_raw")]
    print(f"AUDITORIA CONTRATOS ATUALIZADOS: {refresh}/{len(alunos)}", flush=True)
    print(f"FREQUENCIA REAL ANTES: {freq_real_before} DE {len(alunos)}", flush=True)
    print(f"FREQUENCIA REAL DEPOIS: {real_after} DE {len(alunos)}", flush=True)
    print(f"SEM FREQUENCIA CLASSIFICADOS: {sum(bool(a.get('frequencia_status')) for a in sem)} DE {len(sem)}", flush=True)
    print(f"DASHBOARD REFERENCIA CAPTURADO: {sum(bool(x.get('ok')) for x in dashboard)}/{len(dashboard)}", flush=True)
    print(f"FONTES PACOTES CAPTURADAS: {sum(bool(x.get('ok')) for x in packages)}/{len(packages)}", flush=True)


if __name__ == "__main__":
    main()
