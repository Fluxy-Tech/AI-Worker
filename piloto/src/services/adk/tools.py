from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from google.adk.tools import ToolContext

from src.infra.agent_api import client as agent_api
from src.services.adk.infos import AGENT_TIMEZONE, STATE_METADADOS_ADICIONAIS

# Chave interna do state da sessão (NÃO entra em CHAVES_METADATA, então não
# vai pro Target.metadata) — faz agendar_evento remarcar em vez de duplicar.
STATE_EVENTO_CALENDARIO_ID = "evento_calendario_id"


def solicitar_atendimento_humano(
    tool_context: ToolContext,
    motivo: str,
    fila_sugerida: Optional[str] = None,
) -> dict:
    """Marca a conversa para encaminhamento a um atendente humano — siga as
    regras de quando e para onde encaminhar descritas na sua personalidade,
    se houver.

    Se você já souber exatamente o nome/departamento da fila certa pra esse
    caso (ex: seu fluxo define um setor e uma cidade específicos), preencha
    fila_sugerida com esse nome o mais parecido possível de como a fila
    realmente se chama — isso torna o direcionamento muito mais preciso do
    que depender só do motivo em texto livre."""
    tool_context.state["handoff_requested"] = True
    tool_context.state["handoff_reason"] = motivo
    if fila_sugerida:
        tool_context.state["handoff_suggested_queue"] = fila_sugerida
    return {"ok": True, "mensagem": "Encaminhamento para atendimento humano registrado."}


def encerrar_conversa(tool_context: ToolContext) -> dict:
    """Marca a conversa como encerrada. Use quando o assunto foi resolvido e o
    cliente se despediu ou confirmou que não precisa de mais nada."""
    tool_context.state["closing_requested"] = True
    return {"ok": True}


def parar_envio_campanhas(tool_context: ToolContext) -> dict:
    """Chame assim que o contato pedir explicitamente para não receber mais
    campanhas/mensagens em massa (ex: "não quero mais receber", "pare de me
    mandar mensagem", "me remova da lista") — marca o contato para ser
    excluído dos próximos disparos de campanha."""
    tool_context.state["block_campaigns_requested"] = True
    return {"ok": True, "mensagem": "Contato marcado para não receber mais campanhas."}


# ---------- FUNÇÕES PADRÃO: KANBAN (CRM) E CALENDÁRIO ----------
# Genéricas de propósito — qualquer agente pode incluir estas tools. O
# contato é sempre o da conversa (tool_context.user_id = Target.id), o modelo
# nunca escolhe pra quem cria.


def parse_data_hora(texto: str) -> datetime:
    """Aceita ISO com ou sem fuso ("2026-09-30T14:00", "2026-09-30 14:00",
    "2026-09-30T14:00:00-03:00"). Sem fuso = horário local (AGENT_TIMEZONE)."""
    try:
        quando = datetime.fromisoformat(texto.strip().replace(" ", "T", 1))
    except (AttributeError, ValueError):
        raise ValueError("Data/hora inválida — use o formato AAAA-MM-DDTHH:MM (ex: 2026-09-30T14:00).")
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=ZoneInfo(AGENT_TIMEZONE))
    return quando


def parse_data_hora_futura(texto: str) -> datetime:
    quando = parse_data_hora(texto)
    if quando < datetime.now(quando.tzinfo):
        raise ValueError("Essa data/horário já passou — confirme com o contato uma data futura.")
    return quando


def titulo_evento_contato(state) -> str:
    """Fallback quando o modelo não deu nome ao evento. Usa o metadado "nome"
    só se a empresa tiver criado um com essa chave no Agent Console."""
    nome = (state.get(STATE_METADADOS_ADICIONAIS) or {}).get("nome")
    return f"Conversa com {nome}" if nome else "Conversa com lead"


def descricao_lead(state) -> str:
    """Resumo determinístico dos dados coletados (metadados do Agent Console
    registrados nesta sessão + o horário agendado) — anexado ao evento."""
    dados = dict(state.get(STATE_METADADOS_ADICIONAIS) or {})
    if state.get("data_horario_contato"):
        dados["data_horario_contato"] = state["data_horario_contato"]
    linhas = [f"{chave.replace('_', ' ').capitalize()}: {valor}" for chave, valor in dados.items() if valor]
    return "\n".join(linhas)


# Limites do Agent-Api (internal.routes.ts#calendarEventScheduleSchema).
LIMITE_NOME_EVENTO = 200
LIMITE_DESCRICAO_EVENTO = 5000


def _descricao_evento(descricao: str, state) -> str | None:
    """Descrição escrita pelo modelo + os dados coletados do lead, pra quem
    for atender ter tudo no evento."""
    partes = [p for p in ((descricao or "").strip(), descricao_lead(state)) if p]
    if len(partes) == 2:
        partes[1] = f"Dados do lead:\n{partes[1]}"
    return "\n\n".join(partes)[:LIMITE_DESCRICAO_EVENTO] or None


def agendar_evento(
    tool_context: ToolContext, data_hora: str, nome: str, descricao: str, evento_id: Optional[str] = None
) -> dict:
    """Agenda o evento com o contato no dia e horário que ele escolheu, ou
    remarca um evento dele para uma nova data futura.
    data_hora no formato AAAA-MM-DDTHH:MM, no horário local (ex:
    "2026-09-30T14:00") — converta expressões como "amanhã às 14h" usando a
    data atual informada na sua instrução. Se o contato der só o dia ou um
    período vago, pergunte o horário exato antes de chamar.

    evento_id: para remarcar um evento que o contato já tem, o id dele
    (de consultar_eventos_calendario). Vazio = remarca o evento agendado
    nesta conversa, se houver, ou cria um novo.

    nome: título curto e claro do evento, que diga o que é e com quem (ex:
    "Demonstração da plataforma - Ana (Loja X)").
    descricao: 1 a 3 frases dizendo o objetivo do evento e o contexto da
    conversa que a equipe precisa saber antes (interesse, dúvidas, o que foi
    combinado). Os dados coletados do contato já são anexados
    automaticamente — não precisa repeti-los.

    A agenda é checada na hora: se o horário estiver indisponível, a resposta
    vem com disponivel=false e o motivo — peça outro dia/horário ao contato e
    chame de novo. Chamar de novo na mesma conversa remarca o evento já
    agendado (não cria outro) e atualiza nome e descrição."""
    try:
        quando = parse_data_hora_futura(data_hora)
    except ValueError as e:
        return {"ok": False, "erro": str(e)}

    target_id = tool_context.user_id
    try:
        resultado = agent_api.agendar_evento_calendario(
            target_id,
            nome=((nome or "").strip() or titulo_evento_contato(tool_context.state))[:LIMITE_NOME_EVENTO],
            data_evento_iso=quando.isoformat(),
            descricao=_descricao_evento(descricao, tool_context.state),
            evento_id=(evento_id or "").strip() or tool_context.state.get(STATE_EVENTO_CALENDARIO_ID),
        )
    except agent_api.HorarioIndisponivelError as e:
        return {"ok": False, "disponivel": False, "motivo": str(e)}
    except agent_api.EventoInvalidoError as e:
        return {"ok": False, "erro": str(e)}
    except Exception as e:
        print(f"[piloto] Falha ao agendar evento do contato {target_id}: {e}")
        return {"ok": False, "erro": "Não foi possível acessar a agenda agora."}

    evento = resultado.get("event") or {}
    if evento.get("id"):
        tool_context.state[STATE_EVENTO_CALENDARIO_ID] = evento["id"]
    tool_context.state["data_horario_contato"] = quando.strftime("%d/%m/%Y %H:%M")

    resposta = {
        "ok": True,
        "disponivel": True,
        "acao": "remarcado" if resultado.get("rescheduled") else "agendado",
        "data_hora": tool_context.state["data_horario_contato"],
    }
    responsavel = (evento.get("user") or {}).get("name")
    if responsavel:
        resposta["responsavel"] = responsavel
    return resposta


STATUS_EVENTO = {"RESCHEDULED": "REMARCADO", "CANCELED": "CANCELADO", "FINISHED": "ENCERRADO"}


def _data_local(iso: str) -> str:
    tz = ZoneInfo(AGENT_TIMEZONE)
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).strftime("%d/%m/%Y %H:%M")


def _proximo_evento_ativo(target_id: str) -> str:
    """Data local do próximo evento não cancelado do contato ("" = nenhum) —
    o que fica em data_horario_contato depois de desmarcar um evento."""
    eventos = agent_api.consultar_eventos_calendario(target_id).get("contactEvents", [])
    ativos = [e for e in eventos if e.get("status") != "CANCELED" and not e.get("isClosed")]
    return _data_local(ativos[0]["dateEvent"]) if ativos else ""


def desmarcar_evento(tool_context: ToolContext, evento_id: str) -> dict:
    """Desmarca (cancela) um evento futuro do contato, quando ele pedir.
    evento_id: o id do evento (de consultar_eventos_calendario). Confirme com
    o contato qual evento é antes de chamar. Se ele quiser só mudar a data,
    use agendar_evento com o evento_id em vez de desmarcar."""
    target_id = tool_context.user_id
    try:
        agent_api.desmarcar_evento_calendario(target_id, (evento_id or "").strip())
    except agent_api.EventoInvalidoError as e:
        return {"ok": False, "erro": str(e)}
    except Exception as e:
        print(f"[piloto] Falha ao desmarcar evento do contato {target_id}: {e}")
        return {"ok": False, "erro": "Não foi possível acessar a agenda agora."}

    if tool_context.state.get(STATE_EVENTO_CALENDARIO_ID) == evento_id:
        tool_context.state[STATE_EVENTO_CALENDARIO_ID] = None
    try:
        tool_context.state["data_horario_contato"] = _proximo_evento_ativo(target_id)
    except Exception as e:
        print(f"[piloto] Falha ao atualizar o horario combinado do contato {target_id}: {e}")
    return {"ok": True, "acao": "desmarcado"}


def consultar_eventos_calendario(
    tool_context: ToolContext, data_inicio: Optional[str] = None, data_fim: Optional[str] = None
) -> dict:
    """Consulta a agenda deste contato: os eventos dele (id, data/horário,
    status e com quem é — o responsável) e os horários já ocupados na agenda
    da empresa. Datas no formato AAAA-MM-DD ou AAAA-MM-DDTHH:MM (horário
    local); sem datas = próximos 30 dias (máx. 62 dias por consulta). Use
    para responder o contato sobre os eventos dele, antes de propor/confirmar
    um horário e para achar o id de um evento a remarcar ou desmarcar."""
    try:
        de = parse_data_hora(data_inicio) if data_inicio else None
        ate = parse_data_hora(data_fim) if data_fim else None
    except ValueError as e:
        return {"ok": False, "erro": str(e)}
    # Data final só com o dia ("2026-10-05") vira o fim daquele dia.
    if ate and data_fim and len(data_fim.strip()) == 10:
        ate = ate.replace(hour=23, minute=59, second=59)

    try:
        resultado = agent_api.consultar_eventos_calendario(
            tool_context.user_id,
            de.isoformat() if de else None,
            ate.isoformat() if ate else None,
        )
    except Exception as e:
        print(f"[julia] Falha ao consultar eventos do contato {tool_context.user_id}: {e}")
        return {"ok": False, "erro": "Não foi possível consultar a agenda agora."}

    return {
        "ok": True,
        "eventos_do_contato": [
            {
                "id": e.get("id"),
                "titulo": e.get("name"),
                "data_hora": _data_local(e["dateEvent"]),
                "responsavel": e.get("responsible"),
                "descricao": e.get("description"),
                "status": "ENCERRADO" if e.get("isClosed") else STATUS_EVENTO.get(e.get("status"), "AGENDADO"),
            }
            for e in resultado.get("contactEvents", [])
        ],
        "horarios_ocupados": [_data_local(e["dateEvent"]) for e in resultado.get("busySlots", [])],
    }


def consultar_estagios_kanban(tool_context: ToolContext) -> dict:
    """Lista os estágios (esteiras) do Kanban da empresa, na ordem em que
    aparecem na tela. O estágio padrão é onde os leads novos entram."""
    try:
        estagios = agent_api.listar_estagios_kanban(tool_context.user_id)
    except Exception as e:
        print(f"[julia] Falha ao listar estágios do Kanban do contato {tool_context.user_id}: {e}")
        return {"ok": False, "erro": "Não foi possível consultar o Kanban agora."}
    return {
        "ok": True,
        "estagios": [
            {"nome": e.get("nameStage"), "posicao": e.get("position"), "padrao": bool(e.get("isDefault"))}
            for e in estagios
        ],
    }
