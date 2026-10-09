"""Integra o antigo ingestor CFIS ao fluxo principal do scraper.

Fonte: dados_alunos.json produzido pelo mesmo processo.
Destino: Supabase via service_role no runner, nunca no frontend.
"""
import hashlib, json, os, re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from supabase import create_client

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"dados_alunos.json"
BATCH=max(50,int(os.getenv("CFIS_IMPORT_BATCH_SIZE","500")))

def text(*values):
    for v in values:
        if v is None: continue
        s=str(v).replace("\xa0"," ").strip()
        if s: return s
    return ""

def num(*values):
    for v in values:
        if v in (None,""): continue
        try:
            x=float(v)
            if x==x: return x
        except: pass
    return 0

def nullable_num(*values):
    for v in values:
        if v in (None,""): continue
        try:
            x=float(v)
            if x==x: return x
        except: pass
    return None

def date_value(*values):
    s=text(*values)
    if not s: return None
    if len(s)>=10 and s[2:3]=="/" and s[5:6]=="/": return f"{s[6:10]}-{s[3:5]}-{s[:2]}"
    if len(s)>=10 and s[4:5]=="-": return s[:10]
    return None

def timestamp(v):
    s=text(v)
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc).isoformat()
    except: return None

def uuid_from_key(key):
    h=hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"{h[:8]}-{h[8:12]}-5{h[13:16]}-8{h[17:20]}-{h[20:32]}"

def unidade(*values):
    s=text(*values).lower()
    if "matriz" in s: return "matriz"
    if "filial" in s: return "filial"
    return None

def _snapshot_text(raw, kinds=None):
    kinds = set(kinds or [])
    parts = []
    for snap in raw.get("rotas_cgd") or []:
        if not isinstance(snap, dict):
            continue
        if kinds and str(snap.get("rota") or "").lower() not in kinds:
            continue
        parts.append(text(snap.get("texto_corpo")))
    return " | ".join(x for x in parts if x)

def _label_from_snapshots(raw, labels):
    hay = " | ".join([
        text(raw.get("curso"), raw.get("turma"), raw.get("professor")),
        _snapshot_text(raw, {"contrato","disciplinas","horarios"}),
        text(raw.get("aluno_raw")),
    ])
    for label in labels:
        m = re.search(rf"\b{re.escape(label)}\s*[:\-]\s*([^|;\n]{2,120})", hay, re.I)
        if m:
            return text(m.group(1))
    return ""

def _first_snapshot_date(raw, labels):
    import re
    hay = " | ".join([
        _snapshot_text(raw, {"contrato","cadastro_aluno"}),
        text(raw.get("aluno_raw")),
    ])
    for label in labels:
        m = re.search(rf"\b{re.escape(label)}\s*[:\-]?\s*([^|;\n]{2,80})", hay, re.I)
        if m:
            d = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", m.group(1))
            if d:
                return d.group(0)
    return ""

def _months_snapshot(raw):
    import re
    hay = " | ".join([_snapshot_text(raw, {"contrato","cadastro_aluno"}), text(raw.get("aluno_raw"))])
    m = re.search(r"\b(\d{1,2})\s*mes(?:es)?\b", hay, re.I)
    return int(m.group(1)) if m else None

def normalize(raw):
    cid=text(raw.get("contrato"),raw.get("cgd_matricula_id"),raw.get("matricula"),raw.get("id_aluno"))
    nome=text(raw.get("nome"),raw.get("aluno"),raw.get("nome_aluno"))
    un=unidade(raw.get("unidade"),raw.get("filial"))
    curso=text(raw.get("curso")) or _label_from_snapshots(raw, ("Curso", "Curso do aluno", "Curso contratado"))
    inicio=date_value(raw.get("data_inicio"),raw.get("data_matricula")) or date_value(_first_snapshot_date(raw, ("Data de início","Data de inicio","Início","Inicio","Data matrícula","Data matricula")))
    turma=text(raw.get("turma_nome"),raw.get("turma")) or _label_from_snapshots(raw, ("Turma", "Turma atual", "Turma do aluno"))
    professor=text(raw.get("professor_nome"),raw.get("professor")) or _label_from_snapshots(raw, ("Professor", "Professor responsável", "Professor responsavel"))
    mes=text(raw.get("mes_referencia_faltas"),raw.get("mes_referencia"))
    if not mes:
        from datetime import date as _date
        mes=_date.today().strftime("%m/%Y")
    meses=nullable_num(raw.get("meses_contrato_total"),raw.get("meses_contrato"),_months_snapshot(raw))
    if meses is None:
        inicio_tmp=date_value(raw.get("data_inicio"),raw.get("data_matricula")) or date_value(_first_snapshot_date(raw, ("Data de início","Data de inicio","Início","Inicio","Data matrícula","Data matricula")))
        fim_tmp=date_value(raw.get("data_termino_contrato"),raw.get("data_fim_contrato")) or date_value(_first_snapshot_date(raw, ("Data de término","Data de termino","Término","Termino","Data fim","Data final")))
        if inicio_tmp and fim_tmp:
            try:
                from datetime import date as _date
                a,b=_date.fromisoformat(inicio_tmp),_date.fromisoformat(fim_tmp)
                meses=max(1,(b.year-a.year)*12+b.month-a.month+(1 if b.day>=a.day else 0))
            except Exception:
                meses=None
    if meses is None:
        meses=12
    disciplinas_raw=raw.get("disciplinas") if isinstance(raw.get("disciplinas"),list) else []
    total_grade=nullable_num(raw.get("total_disciplinas_grade"),raw.get("total_disciplinas"))
    if total_grade is None: total_grade=len(disciplinas_raw)
    erros=[]
    for value,label in ((cid,"contrato"),(nome,"nome"),(un,"unidade"),(curso,"curso"),(inicio,"data_inicio"),(turma,"turma_nome"),(professor,"professor_nome"),(mes,"mes_referencia_faltas")):
        if not value: erros.append(label)
    if meses is None: erros.append("meses_contrato_total")
    if erros: return None,[],erros
    faltas=num(raw.get("faltas_totais"),raw.get("faltas_acumuladas"),raw.get("faltas"))
    faltas_mes=num(raw.get("faltas_mes_atual"),raw.get("faltas_mes"))
    repos=max(0,num(raw.get("reposicoes_realizadas"),raw.get("reposicoesRealizadas")))
    anterior=max(0,faltas-faltas_mes)
    abate=min(repos,anterior)
    faltas_efetivas=max(0,faltas_mes-max(0,repos-abate))
    manual=bool(raw.get("bloqueio_manual_override"))
    agora=datetime.now(timezone.utc).isoformat()
    aluno={
      "id":uuid_from_key(f"{un}:{cid}"),"cgd_matricula_id":cid,"nome":nome,"contrato":cid,
      "email":text(raw.get("email")) or None,"telefone":text(raw.get("telefone"),raw.get("celular")) or None,
      "curso":curso,"turma_nome":turma,"professor_responsavel_id":text(raw.get("professor_responsavel_id")) or None,
      "professor_nome":professor,"data_inicio":inicio,"data_termino_contrato":date_value(raw.get("data_termino_contrato"),raw.get("data_fim_contrato")),
      "dias_contrato_total":nullable_num(raw.get("dias_contrato_total")),"meses_contrato_total":meses,
      "ultima_aula":date_value(raw.get("ultima_aula")),"ultimo_acesso":timestamp(raw.get("ultimo_acesso")),
      "faltas_totais":max(0,faltas),"faltas_mes_atual":max(0,faltas_mes),"mes_referencia_faltas":mes,
      "reposicoes_realizadas":repos,"dias_em_curso":max(0,num(raw.get("dias_em_curso"),raw.get("dias_curso"),raw.get("dias"))),
      "criticidade":raw.get("criticidade") or "normal","tratativa_sugerida":raw.get("tratativa_sugerida") or raw.get("tratativa") or "normal",
      "status_tratativa":raw.get("status_tratativa") or "pendente","status_matricula":raw.get("status_matricula") or ("bloqueado_faltas" if (not manual and faltas_efetivas>=3) else "ativo"),
      "bloqueado_automaticamente":False if manual else faltas_efetivas>=3,
      "bloqueio_manual_override":manual,
      "motivo_bloqueio":(f"Bloqueio automático: {faltas_efetivas} faltas efetivas no mês {mes}." if not manual and faltas_efetivas>=3 else raw.get("motivo_bloqueio")),
      "total_disciplinas_grade":max(0,int(total_grade)),"disciplinas_concluidas":len(raw.get("disciplinas_concluidas") or []) if isinstance(raw.get("disciplinas_concluidas"),list) else int(num(raw.get("disciplinas_concluidas"),raw.get("disciplinas_concluidas_count"))),
      "unidade":un,"updated_at":agora
    }
    ds=[]
    for i,d in enumerate(disciplinas_raw):
        if not isinstance(d,dict): continue
        dn=text(d.get("nome"),d.get("disciplina"))
        carga=num(d.get("carga_horaria"))
        if not dn or carga<=0: continue
        curs=max(0,num(d.get("horas_cursadas"),d.get("horas_cumpridas")))
        esp=max(0,num(d.get("horas_esperadas"),d.get("horas_planejadas")))
        perc=max(0,min(100,curs/carga*100))
        exc=max(0,curs-carga)
        st=text(d.get("status")).lower()
        status="concluida" if "concl" in st else ("em_andamento" if "andamento" in st else "pendente")
        ritmo="concluida" if status=="concluida" else ("excesso_tempo" if exc>0 else ("avanco_lento" if perc<50 and esp>=20 else "normal"))
        ds.append({
          "id":uuid_from_key(f"{aluno['id']}:disciplina:{i}:{dn}"),"aluno_id":aluno["id"],"nome":dn,
          "carga_horaria":round(carga),"status":status,"nota":nullable_num(d.get("nota")),
          "frequencia_percent":nullable_num(d.get("frequencia_percent"),d.get("frequencia")),
          "data_conclusao":date_value(d.get("data_conclusao")),"ordem":int(num(d.get("ordem"),i+1)),
          "horas_cursadas":curs,"horas_esperadas":esp,"percentual_avanco":perc,"horas_excedentes":exc,
          "ultrapassou_carga":exc>0,"ritmo":ritmo,"updated_at":agora
        })
    return aluno,ds,[]

def _invalid_record_diagnostic(index, raw, missing):
    fields = {
        "contrato": text(raw.get("contrato"), raw.get("cgd_matricula_id"), raw.get("matricula"), raw.get("id_aluno")),
        "nome": text(raw.get("nome"), raw.get("aluno"), raw.get("nome_aluno")),
        "unidade": text(raw.get("unidade"), raw.get("filial")),
        "curso": text(raw.get("curso")),
        "data_inicio": text(raw.get("data_inicio"), raw.get("data_matricula")),
        "turma_nome": text(raw.get("turma_nome"), raw.get("turma")),
        "professor_nome": text(raw.get("professor_nome"), raw.get("professor")),
        "mes_referencia_faltas": text(raw.get("mes_referencia_faltas"), raw.get("mes_referencia")),
        "meses_contrato_total": text(raw.get("meses_contrato_total"), raw.get("meses_contrato")),
    }
    expected = {
        "contrato": "ID do contrato CGD não vazio e único",
        "nome": "nome real do aluno",
        "unidade": "matriz ou filial",
        "curso": "nome do curso extraído do contrato/matrícula",
        "data_inicio": "data válida ISO (AAAA-MM-DD) ou brasileira (DD/MM/AAAA)",
        "turma_nome": "turma real ou fallback explícito SEM TURMA quando a não alocação estiver comprovada",
        "professor_nome": "professor real ou fallback explícito NÃO ALOCADO quando a não alocação estiver comprovada",
        "mes_referencia_faltas": "mês de referência das faltas",
        "meses_contrato_total": "duração do contrato ou valor padrão definido pelo integrador",
    }
    print(
        "INGESTAO_PRIMEIRO_INVALIDO="
        + json.dumps({
            "indice": index, "campos_vazios": missing,
            "valores_recebidos": fields,
            "esperado_pelo_schema": {key: expected.get(key) for key in missing},
        }, ensure_ascii=False),
        flush=True,
    )


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def main():
    if not DATA.exists():
        raise SystemExit("dados_alunos.json não encontrado")
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("dados_alunos.json precisa ser lista")

    total_lido = len(raw)
    alunos, disciplinas, errors, seen = [], [], [], set()
    raw_by_id = {}
    first_invalid_logged = False
    for i, record in enumerate(raw):
        if not isinstance(record, dict):
            errors.append({"indice": i, "contrato": None, "motivo": ["registro_invalido"]})
            continue
        aluno, rows, missing = normalize(record)
        cid = text(record.get("contrato"), record.get("cgd_matricula_id"), record.get("matricula"), record.get("id_aluno"))
        if not aluno:
            errors.append({"indice": i, "contrato": cid or None, "motivo": missing})
            if not first_invalid_logged:
                _invalid_record_diagnostic(i, record, missing)
                first_invalid_logged = True
            continue
        if aluno["cgd_matricula_id"] in seen:
            errors.append({"indice": i, "contrato": cid, "motivo": ["contrato_duplicado"]})
            print(f"INGESTAO_REGISTRO_REJEITADO indice={i} contrato={cid} motivo=contrato_duplicado", flush=True)
            continue
        seen.add(aluno["cgd_matricula_id"])
        alunos.append(aluno)
        disciplinas.extend(rows)
        raw_by_id[aluno["id"]] = record

    print(
        f"INGESTAO_TOTAL_LIDO={total_lido} VALIDOS={len(alunos)} "
        f"INVALIDOS={sum(1 for e in errors if e.get('motivo') != ['contrato_duplicado'])} "
        f"DUPLICADOS={sum(1 for e in errors if e.get('motivo') == ['contrato_duplicado'])} "
        f"DISCIPLINAS_NORMALIZADAS={len(disciplinas)}",
        flush=True,
    )
    if errors:
        print("INGESTAO_ERROS_VALIDACAO=" + json.dumps(errors[:100], ensure_ascii=False), flush=True)
    if not alunos:
        print("INGESTAO_PERSISTIDOS_SUCESSO=0", flush=True)
        raise SystemExit("INGESTAO_SUPABASE_SEM_REGISTROS_PERSISTIVEIS")

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios")
    sb = create_client(url, key)

    persisted_ids = set()
    persisted_students = 0
    persisted_disciplines = 0
    persistence_errors = []
    for batch_number, batch in enumerate(_chunks(alunos, BATCH), 1):
        batch_ids = {a["id"] for a in batch}
        try:
            sb.table("alunos").upsert(batch, on_conflict="cgd_matricula_id").execute()
            persisted_ids.update(batch_ids)
            persisted_students += len(batch)
            print(
                f"SUPABASE_ALUNOS_LOTE={batch_number} "
                f"SUCESSO={len(batch)} ACUMULADO={persisted_students}/{len(alunos)}",
                flush=True,
            )
        except Exception as exc:
            message = f"alunos lote {batch_number}: {type(exc).__name__}: {exc}"
            persistence_errors.append(message)
            print(f"SUPABASE_ERRO={message}", flush=True)
            continue

        batch_disciplines = [d for d in disciplinas if d.get("aluno_id") in batch_ids]
        discipline_ok = True
        for d_batch_number, d_batch in enumerate(_chunks(batch_disciplines, BATCH), 1):
            try:
                # Upsert é idempotente e ocorre antes de limpar linhas antigas.
                sb.table("aluno_disciplinas").upsert(d_batch, on_conflict="id").execute()
                persisted_disciplines += len(d_batch)
                print(
                    f"SUPABASE_DISCIPLINAS_LOTE_ALUNOS={batch_number} "
                    f"SUBLOTE={d_batch_number} SUCESSO={len(d_batch)} "
                    f"ACUMULADO={persisted_disciplines}/{len(disciplinas)}",
                    flush=True,
                )
            except Exception as exc:
                discipline_ok = False
                message = f"aluno_disciplinas lote alunos={batch_number} sublote={d_batch_number}: {type(exc).__name__}: {exc}"
                persistence_errors.append(message)
                print(f"SUPABASE_ERRO={message}", flush=True)
                break

        if not discipline_ok:
            print(
                f"SUPABASE_LIMPEZA_ANTIGAS_PULADA lote_alunos={batch_number} "
                "motivo=upsert_disciplinas_incompleto",
                flush=True,
            )
            continue

        # Só remove disciplinas antigas depois que os novos registros foram
        # aceitos. Limita cada operação para evitar URLs enormes no PostgREST.
        cleanup_students = [
            a for a in batch
            if bool(raw_by_id.get(a["id"], {}).get("detalhamento_completo"))
        ]
        for cleanup_batch in _chunks(cleanup_students, min(BATCH, 100)):
            cleanup_ids = [a["id"] for a in cleanup_batch]
            keep_ids = [
                d["id"] for d in batch_disciplines
                if d.get("aluno_id") in set(cleanup_ids)
            ]
            try:
                query = sb.table("aluno_disciplinas").delete().in_("aluno_id", cleanup_ids)
                if keep_ids:
                    query = query.not_.in_("id", keep_ids)
                query.execute()
            except Exception as exc:
                message = f"limpeza disciplinas antigas alunos={cleanup_ids[:5]}: {type(exc).__name__}: {exc}"
                persistence_errors.append(message)
                print(f"SUPABASE_ERRO={message}", flush=True)

    print(
        f"INGESTAO_TOTAL_LIDO={total_lido} VALIDOS={len(alunos)} "
        f"PERSISTIDOS_SUCESSO={persisted_students} "
        f"DISCIPLINAS_NORMALIZADAS={len(disciplinas)} "
        f"DISCIPLINAS_UPSERT_SUCESSO={persisted_disciplines} "
        f"ERROS_PERSISTENCIA={len(persistence_errors)} ERROS_VALIDACAO={len(errors)}",
        flush=True,
    )
    if persistence_errors:
        print("INGESTAO_ERROS_PERSISTENCIA=" + json.dumps(persistence_errors[:100], ensure_ascii=False), flush=True)
    if persisted_students == 0:
        raise SystemExit("INGESTAO_SUPABASE_SEM_REGISTROS_PERSISTIDOS")
    if persistence_errors or errors:
        raise SystemExit("INGESTAO_SUPABASE_CONCLUIDA_COM_ERROS")
    print(
        f"INGESTAO_INTEGRADA_SUPABASE=OK ALUNOS_PERSISTIDOS={persisted_students} "
        f"DISCIPLINAS_PERSISTIDAS={persisted_disciplines}",
        flush=True,
    )


if __name__ == "__main__":
    main()
