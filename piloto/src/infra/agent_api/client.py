import json
import os
import unicodedata

import httpx
from openai import OpenAI

BASE_URL = os.getenv("AGENT_API_BASE_URL", "http://localhost:7073")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")


def normalize_text(text: str) -> str:
    """minúsculas, sem acento, só alfanumérico — pra casar 'Goioerê' com
    'Goioere', ou 'Venda de Novos — Toledo' com 'Venda de novos Toledo'."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    return "".join(ch for ch in sem_acento.lower() if ch.isalnum())


_normalize = normalize_text

# Cacheado por api_key (não um singleton fixo) — cada instância deste worker
# atende só um agente, então normalmente há só 1 entrada, mas cachear por key
# evita reconstruir o client à toa e permite que o token venha do payload de
# cada mensagem (Agent Console, criptografado no banco) em vez de fixo no env.
_openai_clients: dict[str | None, OpenAI] = {}


def _get_openai_client(api_key: str | None = None) -> OpenAI:
    key = api_key or os.getenv("OPENAI_API_KEY")
    if key not in _openai_clients:
        _openai_clients[key] = OpenAI(api_key=key)
    return _openai_clients[key]


def get_service_island_queues(service_island_id: str) -> list[dict]:
    """Lista as filas da ilha de atendimento ligada ao WhatsApp Channel do
    contato — usada na hora do handoff pra decidir o destino do ticket."""
    response = httpx.get(
        f"{BASE_URL}/internal/service-islands/{service_island_id}/queues",
        headers={"x-internal-api-key": INTERNAL_API_KEY},
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get("result", [])


def choose_handoff_queue(
    queues: list[dict],
    reason: str,
    default_queue_id: str | None,
    suggested_queue_name: str | None = None,
    openai_api_key: str | None = None,
) -> str | None:
    """Decide para qual fila da ilha o ticket de handoff deve ir, dado o motivo
    do transbordo. Cai para a fila padrão do agente (ou a primeira disponível)
    se não conseguir decidir com segurança.

    Se o agente já souber exatamente o nome da fila (suggested_queue_name —
    ex: personalidades com fluxo próprio de setor+cidade), tenta um match
    exato (ignorando acento/maiúscula/pontuação) ANTES de cair pro
    classificador por IA — muito mais confiável que depender só do "motivo"
    livre quando há muitas filas parecidas."""
    if not queues:
        return default_queue_id
    if len(queues) == 1:
        return queues[0]["id"]

    fallback = default_queue_id or queues[0]["id"]

    if suggested_queue_name:
        normalized_suggestion = _normalize(suggested_queue_name)
        for q in queues:
            if _normalize(q.get("name", "")) == normalized_suggestion:
                return q["id"]
        # Sem match exato: aceita conter/ser contido (ex: sugestão sem a
        # cidade, ou fila com um sufixo a mais) — ainda mais confiável que a
        # classificação livre por IA quando o nome bate quase todo.
        for q in queues:
            normalized_name = _normalize(q.get("name", ""))
            if normalized_name and (normalized_name in normalized_suggestion or normalized_suggestion in normalized_name):
                return q["id"]

    try:
        options = [{"id": q["id"], "name": q.get("name", "")} for q in queues]
        response = _get_openai_client(openai_api_key).chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Escolha a fila de atendimento mais adequada para o motivo de "
                        "transbordo abaixo, entre as opções fornecidas. Responda só com "
                        'JSON no formato {"queue_id": "<id escolhido>"}.'
                    ),
                },
                {"role": "user", "content": json.dumps({"motivo": reason, "filas": options}, ensure_ascii=False)},
            ],
        )
        content = response.choices[0].message.content or "{}"
        chosen_id = json.loads(content).get("queue_id")
        valid_ids = {q["id"] for q in queues}
        return chosen_id if chosen_id in valid_ids else fallback
    except Exception as e:
        print(f"[julia] Falha ao escolher fila de handoff, usando fallback: {e}")
        return fallback


def generate_free_error_message(agent_name: str, openai_api_key: str | None = None) -> str:
    """Usada quando a Mensagem de erro está DESATIVADA na config do agente —
    "a IA pode gerar qualquer resposta" nesse cenário."""
    try:
        response = _get_openai_client(openai_api_key).chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.5,
            max_tokens=80,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Você é {agent_name}, assistente de atendimento via WhatsApp. "
                        "Aconteceu um erro interno ao processar a última mensagem do "
                        "cliente. Gere uma mensagem curta, cordial, em português, "
                        "pedindo desculpas e sugerindo tentar novamente em instantes."
                    ),
                },
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return text or "Desculpe, tivemos um problema para responder agora. Tente novamente em instantes."
    except Exception as e:
        print(f"[julia] Fallback de mensagem de erro também falhou: {e}")
        return "Desculpe, tivemos um problema para responder agora. Tente novamente em instantes."


def gerar_resumo_conversa(transcricao: str, openai_api_key: str | None = None) -> str | None:
    """Resumo curto de como foi a conversa, pro comentário do card no Kanban
    (função KANBAN_CARD). None se falhar — o comentário sai só com os dados."""
    try:
        response = _get_openai_client(openai_api_key).chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você resume conversas de WhatsApp entre um agente de IA e um lead "
                        "para a equipe comercial. Escreva em português, em 2 a 5 frases, "
                        "como foi a conversa: o que o contato buscava, o interesse "
                        "demonstrado, dúvidas ou objeções e o que ficou combinado. Não "
                        "repita dados cadastrais (nome, empresa etc.) nem cite mensagens "
                        "literalmente."
                    ),
                },
                {"role": "user", "content": transcricao},
            ],
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception as e:
        print(f"[julia] Falha ao gerar resumo da conversa pro card: {e}")
        return None


def sincronizar_metadados_contato(target_id: str, metadata: dict) -> None:
    """Envia o snapshot acumulado do que o agente aprendeu do contato nesta
    conversa (nome, cidade etc.) pro Agent-Api, pra persistir entre sessões e
    aparecer no Agent Console (Target.metadata) — mesmo padrão que o axel já
    usa. Best-effort: uma falha aqui não deve derrubar a conversa."""
    try:
        httpx.patch(
            f"{BASE_URL}/internal/targets/{target_id}/metadata",
            json={"metadata": metadata},
            headers={"x-internal-api-key": INTERNAL_API_KEY},
            timeout=10,
        )
    except Exception as e:
        print(f"[julia] Falha ao sincronizar metadados do contato {target_id}: {e}")


def resetar_metadados_contato(target_id: str) -> None:
    """Apaga TODOS os metadados salvos do contato (Target.metadata = {}) —
    usado no reset de jornada por palavra-chave (ver consumer.py). Diferente
    de sincronizar_metadados_contato, que faz merge: aqui substitui por
    vazio. Best-effort: uma falha aqui não deve impedir a resposta de
    confirmação do reset."""
    try:
        httpx.delete(
            f"{BASE_URL}/internal/targets/{target_id}/metadata",
            headers={"x-internal-api-key": INTERNAL_API_KEY},
            timeout=10,
        )
    except Exception as e:
        print(f"[julia] Falha ao resetar metadados do contato {target_id}: {e}")


def bloquear_campanhas_contato(target_id: str) -> None:
    """Avisa o Agent-Api que este contato pediu pra não receber mais campanhas
    (Target.blockCampaigns = true), pra excluí-lo dos próximos disparos.
    Best-effort: uma falha aqui não deve derrubar a conversa."""
    try:
        httpx.patch(
            f"{BASE_URL}/internal/targets/{target_id}/block-campaigns",
            headers={"x-internal-api-key": INTERNAL_API_KEY},
            timeout=10,
        )
    except Exception as e:
        print(f"[julia] Falha ao bloquear campanhas do contato {target_id}: {e}")


def record_message_logs(logs: list[dict]) -> None:
    """Grava linhas de MessageLog (rastreamento de uma mensagem passando pelos
    serviços da mensageria — ver Agent-Api/prisma/schema.prisma e
    MENSAGERIA.md). AI-Worker não tem acesso direto ao Postgres, por isso
    passa pelo Agent-Api em vez de escrever com Prisma como os demais
    serviços. Best-effort: uma falha aqui nunca pode derrubar o
    processamento da mensagem."""
    if not logs:
        return
    try:
        httpx.post(
            f"{BASE_URL}/internal/message-logs",
            json={"logs": logs},
            headers={"x-internal-api-key": INTERNAL_API_KEY},
            timeout=10,
        )
    except Exception as e:
        print(f"[julia] Falha ao gravar MessageLog: {e}")


def update_rag_document_status(
    rag_document_id: str,
    status: str,
    chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Avisa o Agent-Api que a ingestão de um documento de RAG terminou (READY)
    ou falhou (FAILED) — pra tela mostrar o status certo. Best-effort: uma
    falha aqui só vira log, a ingestão em si já rodou."""
    body: dict = {"status": status}
    if chunk_count is not None:
        body["chunkCount"] = chunk_count
    if error_message is not None:
        body["errorMessage"] = error_message

    try:
        httpx.patch(
            f"{BASE_URL}/internal/rag-documents/{rag_document_id}/status",
            json=body,
            headers={"x-internal-api-key": INTERNAL_API_KEY},
            timeout=10,
        )
    except Exception as e:
        print(f"[julia] Falha ao atualizar status do RagDocument {rag_document_id}: {e}")


# ---------- FUNÇÕES PADRÃO DE AGENTE: KANBAN (CRM) E CALENDÁRIO ----------
# Diferente das chamadas best-effort acima, estas LEVANTAM exceção em falha —
# quem chama (tools do ADK / runner) decide o que fazer com o erro e pode
# devolver o motivo pro modelo.


def _raise_for_api_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        message = response.json().get("message")
    except Exception:
        message = None
    raise RuntimeError(message or f"Agent-Api respondeu {response.status_code}")


class HorarioIndisponivelError(RuntimeError):
    """409 do agendamento — a mensagem diz o motivo (ninguém livre / outro
    evento no horário), pra o agente pedir outro horário ao contato."""


def agendar_evento_calendario(
    target_id: str,
    nome: str,
    data_evento_iso: str,
    descricao: str | None = None,
    evento_id: str | None = None,
) -> dict:
    """Função CALENDAR_EVENT: agenda (ou remarca evento_id) num horário livre,
    com um responsável entre os usuários liberados no Kanban. Retorna
    {"event": {..., "user": {"id", "name"} | None}, "rescheduled": bool}."""
    body: dict = {"name": nome, "dateEvent": data_evento_iso}
    if descricao:
        body["description"] = descricao
    if evento_id:
        body["eventId"] = evento_id
    response = httpx.post(
        f"{BASE_URL}/internal/targets/{target_id}/calendar-events/schedule",
        json=body,
        headers={"x-internal-api-key": INTERNAL_API_KEY},
        timeout=10,
    )
    if response.status_code == 409:
        raise HorarioIndisponivelError(response.json().get("message") or "Horário indisponível.")
    _raise_for_api_error(response)
    return response.json().get("result") or {}


def comentar_card_crm(
    target_id: str,
    comentario: str,
    prioridade: str | None = None,
    estagio_id: str | None = None,
) -> dict:
    """Função KANBAN_CARD: comenta no card do contato como "Agente de IA"
    (cria o card se ainda não existir). prioridade (LOW/MEDIUM/HIGH/URGENT) e
    estagio_id (estágio escolhido no Console; None = "Início") só valem
    quando o card é criado nesta chamada."""
    body = {"comment": comentario}
    if prioridade:
        body["priority"] = prioridade
    if estagio_id:
        body["stageId"] = estagio_id
    response = httpx.post(
        f"{BASE_URL}/internal/targets/{target_id}/crm-card/comments",
        json=body,
        headers={"x-internal-api-key": INTERNAL_API_KEY},
        timeout=10,
    )
    _raise_for_api_error(response)
    return response.json().get("result") or {}


def listar_estagios_kanban(target_id: str) -> list[dict]:
    """Estágios (esteiras) do Kanban da empresa do contato, na ordem da tela:
    [{"id", "nameStage", "position", "isDefault"}, ...]."""
    response = httpx.get(
        f"{BASE_URL}/internal/targets/{target_id}/crm-stages",
        headers={"x-internal-api-key": INTERNAL_API_KEY},
        timeout=10,
    )
    _raise_for_api_error(response)
    return response.json().get("result") or []


def consultar_eventos_calendario(target_id: str, de_iso: str | None = None, ate_iso: str | None = None) -> dict:
    """Eventos do contato + horários já ocupados da empresa no período (padrão
    da API: agora → +30 dias; máx. 62 dias). Retorna
    {"contactEvents": [...], "busySlots": [...]} — eventos de outros contatos
    vêm só com data/status."""
    params: dict = {}
    if de_iso:
        params["from"] = de_iso
    if ate_iso:
        params["to"] = ate_iso
    response = httpx.get(
        f"{BASE_URL}/internal/targets/{target_id}/calendar-events",
        params=params,
        headers={"x-internal-api-key": INTERNAL_API_KEY},
        timeout=10,
    )
    _raise_for_api_error(response)
    return response.json().get("result") or {"contactEvents": [], "busySlots": []}
