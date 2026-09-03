"""Runner de protecao do scraper CGD.

Mantem o scraper principal intacto e aplica, antes da execucao:
- paginacao longa (ate 5000 paginas por fonte);
- diagnosticos pesados desligados durante a descoberta;
- retries de navegacao/paginacao;
- filtragem de contratos por situacao antes da coleta detalhada;
- descarte de resultados vazios/inativos.
"""
import os
import re
import time

import scraper

# A base da Matriz tem mais de 1000 paginas; 300 nao e um limite seguro.
scraper.MAX_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "5000"))
scraper.MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "10000"))

# Durante a descoberta de paginas nao precisamos gerar HTML/TXT/PNG de cada pagina.
_original_dump = scraper.dump
scraper.dump = lambda page, unidade, nome: None

# Abertura robusta: 3 tentativas para erros transitórios, sem perder o processo.
_original_open_page = scraper.open_page

def robust_open_page(page, url, unidade, nome, wait=1300):
    for tentativa in range(1, 4):
        try:
            ok = _original_open_page(page, url, unidade, nome, wait)
            if ok:
                return True
        except Exception as exc:
            print(f"[{unidade}] tentativa {tentativa}/3 falhou em {url}: {exc}")
        if tentativa < 3:
            time.sleep(2 * tentativa)
    print(f"[{unidade}] NAVEGACAO NAO RECUPERADA: {url}")
    return False

scraper.open_page = robust_open_page

# Paginação mais tolerante: usa a implementação existente, mas tenta novamente
# quando o CGD demora a responder. O erro nao apaga os contratos ja encontrados.
_original_next_page = scraper.next_page

def robust_next_page(page):
    for tentativa in range(1, 4):
        try:
            if _original_next_page(page):
                return True
        except Exception as exc:
            print(f"[PAGINACAO] tentativa {tentativa}/3: {exc}")
        if tentativa < 3:
            time.sleep(2 * tentativa)
    return False

scraper.next_page = robust_next_page

INACTIVE = (
    "inativo", "inativa", "encerrado", "encerrada", "cancelado", "cancelada",
    "suspenso", "suspensa", "rescindido", "rescindida", "finalizado", "finalizada",
    "concluido", "concluida", "concluído", "concluída"
)
ACTIVE = (
    "ativo", "ativa", "vigente", "em andamento", "em curso", "cursando"
)


def status_values(page):
    """Extrai somente valores que tenham forte relacao com situacao/status."""
    values = []

    # Tabelas: somente colunas explicitamente chamadas status/situacao.
    try:
        for heads, rows in scraper.table_data(page):
            for i, h in enumerate(heads):
                hlow = scraper.low(h)
                if any(k in hlow for k in ("status", "situação", "situacao", "estado")):
                    for row in rows:
                        if i < len(row):
                            values.append(scraper.norm(row[i]))
    except Exception:
        pass

    # Selects/input: aproveita o valor real do campo de situacao/status.
    try:
        for sel in (
            'select[name*="status" i]', 'select[name*="situacao" i]',
            'select[name*="situação" i]', 'select[id*="status" i]',
            'select[id*="situacao" i]', 'input[name*="status" i]',
            'input[name*="situacao" i]', 'input[id*="status" i]',
            'input[id*="situacao" i]'
        ):
            loc = page.locator(sel)
            for i in range(loc.count()):
                try:
                    if not loc.nth(i).is_visible():
                        continue
                    tag = loc.nth(i).evaluate("e => e.tagName")
                    value = loc.nth(i).input_value()
                    if value:
                        values.append(scraper.norm(value))
                except Exception:
                    pass
    except Exception:
        pass

    # Texto explicitamente rotulado: evita interpretar qualquer ocorrencia de
    # "ativo" no menu como situacao do contrato.
    txt = scraper.body(page)
    for pat in (
        r"(?:situação|situacao|status|estado)\s*[:\-]\s*([^\n|]{1,80})",
        r"(?:situação|situacao|status|estado)[^\n]{0,30}\b(ativo|inativo|encerrado|cancelado|suspenso|vigente)\b"
    ):
        for m in re.finditer(pat, txt, re.I):
            values.append(scraper.norm(m.group(1)))

    return [v for v in values if v]


def contract_is_active(page, cid, unidade):
    values = status_values(page)
    joined = " | ".join(scraper.low(v) for v in values)

    if any(term in joined for term in INACTIVE):
        print(f"[{unidade}] CONTRATO {cid}: INATIVO -> IGNORADO | status={values[:5]}")
        return False

    if any(term in joined for term in ACTIVE):
        print(f"[{unidade}] CONTRATO {cid}: ATIVO -> COLETAR | status={values[:5]}")
        return True

    # Nao assumir ativo quando o CGD nao informou claramente a situacao.
    print(f"[{unidade}] CONTRATO {cid}: STATUS NAO IDENTIFICADO -> IGNORADO | valores={values[:8]}")
    return False


_original_contract_bundle = scraper.contract_bundle

def active_contract_bundle(page, cid, unidade, reps):
    # Primeira visita e exclusivamente para validar a situacao.
    url = scraper.contract_url(cid)
    if not robust_open_page(page, url, unidade, f"validacao_status_{cid}", wait=900):
        print(f"[{unidade}] CONTRATO {cid}: nao foi possivel validar status -> IGNORADO")
        return None

    if not contract_is_active(page, cid, unidade):
        return None

    # Somente agora entram cursos, horarios, frequencias, aluno e reposicoes.
    return _original_contract_bundle(page, cid, unidade, reps)

scraper.contract_bundle = active_contract_bundle

_original_run_unit = scraper.run_unit

def active_run_unit(unidade, cfg, pw):
    result = _original_run_unit(unidade, cfg, pw)
    result = [item for item in result if item]
    print(f"[{unidade}] RESULTADOS VALIDOS APOS FILTRO DE ATIVOS: {len(result)}")
    return result

scraper.run_unit = active_run_unit

if __name__ == "__main__":
    scraper.main()
