from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from google.adk.tools import ToolContext

from src.infra.agent_api import client as agent_api
from src.services.adk.infos import AGENT_TIMEZONE

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


# ---------- CAMPOS DO LEAD ----------


def atualizar_nome_cliente(tool_context: ToolContext, nome: str) -> dict:
    """Registra o nome do contato assim que ele informar ou confirmar — só
    pergunte o nome se ele ainda não estiver nos dados já conhecidos deste
    contato (ver o início da sua instrução)."""
    tool_context.state["nome"] = nome
    return {"nome": nome}


def atualizar_empresa_cliente(tool_context: ToolContext, nome_empresa: str) -> dict:
    """Registra o nome da empresa do contato assim que ele informar ou
    confirmar — só pergunte se ainda não estiver nos dados já conhecidos
    deste contato."""
    tool_context.state["nome_empresa"] = nome_empresa
    return {"nome_empresa": nome_empresa}


def atualizar_volumetria_atendimento(tool_context: ToolContext, nota: int) -> dict:
    """Registra a volumetria de atendimento da empresa do contato numa escala
    de 1 a 10, sendo 1 baixa e 10 muito alta. Se o contato responder de forma
    descritiva (ex: "bem alta"), converta para a nota mais próxima e confirme
    com ele."""
    try:
        nota = int(nota)
    except (TypeError, ValueError):
        return {"ok": False, "erro": "A nota precisa ser um número inteiro de 1 a 10."}
    if not 1 <= nota <= 10:
        return {"ok": False, "erro": "A nota precisa estar entre 1 e 10 — pergunte de novo ao contato."}
    tool_context.state["volumetria_atendimento"] = str(nota)
    return {"ok": True, "volumetria_atendimento": nota}


def atualizar_uso_sistema_whatsapp(
    tool_context: ToolContext, ja_usou: bool, qual_sistema: Optional[str] = None
) -> dict:
    """Registra se a empresa do contato já usou algum sistema de
    gerenciamento de WhatsApp para empresas. Se ele citar qual sistema,
    preencha qual_sistema."""
    valor = "Sim" if ja_usou else "Não"
    if ja_usou and qual_sistema:
        valor = f"Sim ({qual_sistema})"
    tool_context.state["ja_usou_sistema_whatsapp"] = valor
    return {"ok": True, "ja_usou_sistema_whatsapp": valor}


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
    nome = state.get("nome")
    empresa = state.get("nome_empresa")
    partes = [p for p in (nome, empresa) if p]
    return f"Conversa com {' - '.join(partes)}" if partes else "Conversa com lead"


def descricao_lead(state) -> str:
    """Resumo determinístico dos dados coletados — usado no evento e como
    descrição de fallback do card quando o modelo não escreveu uma."""
    rotulos = (
        ("nome", "Nome"),
        ("nome_empresa", "Empresa"),
        ("volumetria_atendimento", "Volumetria de atendimento (1-10)"),
        ("ja_usou_sistema_whatsapp", "Já usou sistema de WhatsApp"),
        ("data_horario_contato", "Melhor data/horário para conversar"),
    )
    linhas = [f"{rotulo}: {state.get(chave)}" for chave, rotulo in rotulos if state.get(chave)]
    return "\n".join(linhas)


def agendar_evento(tool_context: ToolContext, data_hora: str) -> dict:
    """Agenda o evento com o contato no dia e horário que ele escolheu.
    data_hora no formato AAAA-MM-DDTHH:MM, no horário local (ex:
    "2026-09-30T14:00") — converta expressões como "amanhã às 14h" usando a
    data atual informada na sua instrução. Se o contato der só o dia ou um
    período vago, pergunte o horário exato antes de chamar.

    A agenda é checada na hora: se o horário estiver indisponível, a resposta
    vem com disponivel=false e o motivo — peça outro dia/horário ao contato e
    chame de novo. Chamar de novo na mesma conversa remarca o evento já
    agendado (não cria outro)."""
    try:
        quando = parse_data_hora_futura(data_hora)
    except ValueError as e:
        return {"ok": False, "erro": str(e)}

    target_id = tool_context.user_id
    try:
        resultado = agent_api.agendar_evento_calendario(
            target_id,
            nome=titulo_evento_contato(tool_context.state),
            data_evento_iso=quando.isoformat(),
            descricao=descricao_lead(tool_context.state) or None,
            evento_id=tool_context.state.get(STATE_EVENTO_CALENDARIO_ID),
        )
    except agent_api.HorarioIndisponivelError as e:
        return {"ok": False, "disponivel": False, "motivo": str(e)}
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


def consultar_eventos_calendario(
    tool_context: ToolContext, data_inicio: Optional[str] = None, data_fim: Optional[str] = None
) -> dict:
    """Consulta os eventos já marcados com este contato e os horários já
    ocupados na agenda da empresa. Datas no formato AAAA-MM-DD ou
    AAAA-MM-DDTHH:MM (horário local); sem datas = próximos 30 dias (máx. 62
    dias por consulta). Use antes de propor/confirmar um horário."""
    tz = ZoneInfo(AGENT_TIMEZONE)
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

    def _local(iso: str) -> str:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).strftime("%d/%m/%Y %H:%M")

    return {
        "ok": True,
        "eventos_do_contato": [
            {
                "titulo": e.get("name"),
                "data_hora": _local(e["dateEvent"]),
                "descricao": e.get("description"),
                "status": e.get("status") or ("ENCERRADO" if e.get("isClosed") else "AGENDADO"),
            }
            for e in resultado.get("contactEvents", [])
        ],
        "horarios_ocupados": [_local(e["dateEvent"]) for e in resultado.get("busySlots", [])],
    }
