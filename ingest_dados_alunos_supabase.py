"""Integra o antigo ingestor CFIS ao fluxo principal do scraper.

Fonte: dados_alunos.json produzido pelo mesmo processo.
Destino: Supabase via service_role no runner, nunca no frontend.
"""
import hashlib, json, os
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

def date_value(v):
    s=text(v)
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

def unidade(v):
    s=text(v).lower()
    if "matriz" in s: return "matriz"
    if "filial" in s: return "filial"
    return None

def normalize(raw):
    cid=text(raw.get("contrato"),raw.get("cgd_matricula_id"),raw.get("matricula"),raw.get("id_aluno"))
    nome=text(raw.get("nome"),raw.get("aluno"),raw.get("nome_aluno"))
    un=unidade(raw.get("unidade"),raw.get("filial"))
    curso=text(raw.get("curso"))
    inicio=date_value(raw.get("data_inicio"),raw.get("data_matricula"))
    turma=text(raw.get("turma_nome"),raw.get("turma"))
    professor=text(raw.get("professor_nome"),raw.get("professor"))
    mes=text(raw.get("mes_referencia_faltas"),raw.get("mes_referencia"))
    meses=nullable_num(raw.get("meses_contrato_total"),raw.get("meses_contrato"))
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
      "criticidade":raw.get("criticidade") or None,"tratativa_sugerida":raw.get("tratativa_sugerida") or raw.get("tratativa") or None,
      "status_tratativa":raw.get("status_tratativa") or None,"status_matricula":raw.get("status_matricula") or None,
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

def main():
    if not DATA.exists(): raise SystemExit("dados_alunos.json não encontrado")
    raw=json.loads(DATA.read_text(encoding="utf-8"))
    if not isinstance(raw,list): raise SystemExit("dados_alunos.json precisa ser lista")
    alunos=[]; disciplinas=[]; errors=[]; seen=set()
    for i,r in enumerate(raw):
        if not isinstance(r,dict): errors.append((i,["registro_invalido"])); continue
        a,d,e=normalize(r)
        if not a: errors.append((i,e)); continue
        if a["cgd_matricula_id"] in seen: raise SystemExit(f"Contrato duplicado: {a['cgd_matricula_id']}")
        seen.add(a["cgd_matricula_id"]); alunos.append(a); disciplinas.extend(d)
    print(f"INGESTAO_INTEGRADA_ALUNOS={len(alunos)} DISCIPLINAS={len(disciplinas)} INVALIDOS={len(errors)}",flush=True)
    url=os.getenv("SUPABASE_URL"); key=os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key: raise SystemExit("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios")
    sb=create_client(url,key)
    for i in range(0,len(alunos),BATCH):
        batch=alunos[i:i+BATCH]
        sb.table("alunos").upsert(batch,on_conflict="cgd_matricula_id").execute()
        print(f"ALUNOS_PERSISTIDOS={min(i+BATCH,len(alunos))}/{len(alunos)}",flush=True)
    ids=[a["id"] for a in alunos]
    for i in range(0,len(ids),BATCH):
        sb.table("aluno_disciplinas").delete().in_("aluno_id",ids[i:i+BATCH]).execute()
    for i in range(0,len(disciplinas),BATCH):
        batch=disciplinas[i:i+BATCH]
        sb.table("aluno_disciplinas").insert(batch).execute()
        print(f"DISCIPLINAS_PERSISTIDAS={min(i+BATCH,len(disciplinas))}/{len(disciplinas)}",flush=True)
    print("INGESTAO_INTEGRADA_SUPABASE=OK",flush=True)

if __name__=="__main__": main()
