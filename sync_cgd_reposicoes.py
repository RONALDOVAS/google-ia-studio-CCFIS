import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

OUT = Path("dados_reposicoes.json")
LOGIN_URL = os.getenv("CGD_LOGIN_URL", "https://app.cgd.com.br")

UNIDADES = [
    ("MATRIZ", os.getenv("CGD_USER_MATRIZ"), os.getenv("CGD_PASS_MATRIZ"), os.getenv("CGD_MATRIZ_URL")),
    ("FILIAL", os.getenv("CGD_USER_FILIAL"), os.getenv("CGD_PASS_FILIAL"), os.getenv("CGD_FILIAL_URL")),
]

KEYWORDS = ("reposição", "reposicao", "agendamento", "recuperação", "recuperacao")


def txt(el):
    try:
        return " ".join(el.inner_text().split()).strip()
    except Exception:
        return ""


def first_visible(page, selectors):
    for selector in selectors:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=1200):
                return el
        except Exception:
            pass
    return None


def login(page, user, password):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    login_el = first_visible(page, [
        'input[type="email"]', 'input[name*="user" i]', 'input[name*="login" i]',
        'input[id*="user" i]', 'input[id*="login" i]', 'input[placeholder*="usu" i]'
    ])
    pass_el = first_visible(page, [
        'input[type="password"]', 'input[name*="senha" i]', 'input[name*="pass" i]',
        'input[id*="senha" i]', 'input[id*="pass" i]'
    ])
    if not login_el or not pass_el:
        raise RuntimeError("Campos de login do CGD não encontrados")
    login_el.fill(user)
    pass_el.fill(password)
    button = first_visible(page, [
        'button[type="submit"]', 'input[type="submit"]',
        'button:has-text("Entrar")', 'button:has-text("Acessar")', 'button:has-text("Login")'
    ])
    if not button:
        raise RuntimeError("Botão de login do CGD não encontrado")
    button.click()
    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass
    page.wait_for_timeout(3000)


def descobrir_alvo(page, configured_url):
    if configured_url:
        return configured_url
    elements = page.locator("a,button,[role='menuitem'],[role='button']")
    for i in range(min(elements.count(), 2000)):
        try:
            el = elements.nth(i)
            if not el.is_visible(timeout=300):
                continue
            label = " ".join([
                txt(el), el.get_attribute("href") or "", el.get_attribute("title") or "",
                el.get_attribute("aria-label") or ""
            ]).lower()
            if any(k in label for k in KEYWORDS):
                href = el.get_attribute("href") or ""
                if href:
                    return urljoin(page.url, href)
                try:
                    el.click()
                    page.wait_for_timeout(2500)
                    return page.url
                except Exception:
                    pass
        except Exception:
            pass
    return page.url


def parse_date(value):
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", value)
    if m:
        year = int(m.group(3))
        if year < 100:
            year += 2000
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{year:04d}"
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", value)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return ""


def parse_times(value):
    return re.findall(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", value)


def extract(page, unidade):
    records = []
    tables = page.locator("table")
    for ti in range(tables.count()):
        rows = tables.nth(ti).locator("tbody tr")
        for ri in range(rows.count()):
            row = rows.nth(ri)
            cells = [txt(row.locator("td").nth(i)) for i in range(row.locator("td").count())]
            raw = " | ".join(cells)
            date = parse_date(raw)
            if not date or len(cells) < 2:
                continue
            times = parse_times(raw)
            start = times[0] if times else "16:00"
            end = times[1] if len(times) > 1 else "18:00"
            contract = ""
            name = ""
            for cell in cells:
                compact = re.sub(r"[.\-\s]", "", cell)
                if not contract and re.fullmatch(r"\d{4,}", compact):
                    contract = cell.strip()
                if not name and len(cell.split()) >= 2 and not re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", cell):
                    low = cell.lower()
                    if not any(k in low for k in ("agendada", "realizada", "cancelada", "reposição", "reposicao")):
                        name = cell.strip()
            if not name:
                name = cells[0].strip()
            discipline = "Módulo Geral"
            for cell in cells:
                if any(k in cell.lower() for k in ("informática", "informatica", "módulo", "modulo", "disciplina")):
                    discipline = cell.strip()
                    break
            key = f"{unidade}|{contract}|{name}|{date}|{start}|{discipline}".lower()
            rid = "cgd_rep_" + hashlib.sha256(key.encode()).hexdigest()[:32]
            records.append({
                "id": rid,
                "aluno_id": None,
                "aluno_nome": name,
                "contrato": contract or None,
                "unidade": unidade,
                "data": date,
                "horario_inicio": start,
                "horario_fim": end,
                "duracao_horas": 2,
                "disciplina": discipline,
                "professor": "Ronaldo Vasconcelos",
                "status": "agendada",
                "tipo": "laboratorio",
                "observacao": "Sincronizado diretamente do CGD",
                "updated_at": datetime.now().isoformat(),
            })
    return records


def main():
    all_records = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for unidade, user, password, configured_url in UNIDADES:
            if not user or not password:
                print(f"[{unidade}] credenciais ausentes; ignorada")
                continue
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            try:
                login(page, user, password)
                target = descobrir_alvo(page, configured_url)
                if target and target != page.url:
                    page.goto(target, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                records = extract(page, unidade)
                for record in records:
                    all_records[record["id"]] = record
                print(f"[{unidade}] {len(records)} reposições encontradas em {page.url}")
            except Exception as exc:
                print(f"[{unidade}] ERRO: {exc}")
            finally:
                context.close()
        browser.close()

    payload = {
        "updated_at": datetime.now().isoformat(),
        "source": "CGD",
        "records": list(all_records.values()),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"TOTAL REPOSIÇÕES: {len(payload['records'])}")


if __name__ == "__main__":
    main()
