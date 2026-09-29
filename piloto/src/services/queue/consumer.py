"""
Consumidor de UMA fila dedicada por agente (`task.agent.<nome>.create`) —
cada instância implantada deste worker atende um único Agent, escolhido pela
env `AGENT_NAME` (mesmo valor do `Agent.name` cadastrado no Agent Console).
O código continua genérico (personalidade/RAG vêm sempre frescos do payload,
nada fixo aqui) — só a fila que essa instância consome é fixa. Ver
Inbound-Service/src/infrastructure/queue/rabbitmq/publisher.ts
(resolveAgentQueueName) pra a sanitização do nome, que precisa bater com a
usada aqui (`_sanitize_agent_name`).

Contrato do payload (publicado pelo Inbound-Service, já deduplicado e
agrupado/debounced por sessão em janelas de 10s):

{
  "target": {"id", "waId", "name", "metadata"},
  "channel": {"id", "phoneNumberId", "wabaId", "serviceIslandId", "wordsToReset", "resetMessage"},
  "agent": {
    "id", "name", "defaultQueueId",
    "processingMessage",           # não usado aqui — enviado pelo Inbound-Service
    "transferMessage",              # não usado aqui — enviado pelo Desk-Worker
    "unsupportedFormatMessage",
    "outOfHoursMessage", "outOfHoursEnabled",
    "closingMessage", "closingEnabled",
    "errorMessage", "errorEnabled",
    "personality", "ragEnabled",    # só usados aqui (worker genérico)
    "metadataFields": [{"name", "nameToAgent", "rule"}],  # só os ativos — coletados via registrar_metadado
    "functions": [{"type", "runAtStart", "runAfterMetadata"}],  # CALENDAR_EVENT / KANBAN_CARD
  },
  "messagingSession": {"id", "startedAt"},
  "messages": [{"mongoMessageId", "externalMessageId", "type", "text", "timestamp"}, ...]
}

Publica em `outbound.message.send` (resposta normal / erro / formato não
suportado) ou `desk.ticket.create` (handoff para atendimento humano).
"""

import json
import os
import re
import threading
import traceback

from main import gerar_resposta, resetar_jornada
from src.infra.agent_api.client import (
    choose_handoff_queue,
    generate_free_error_message,
    get_service_island_queues,
    normalize_text,
    record_message_logs,
)
from src.infra.rabbitmq.connection import RabbitMQ
from src.services.queue.publisher import (
    QUEUE_DESK_TICKET_CREATE,
    publish_desk_ticket_create,
    publish_outbound_message,
)

RESET_CONFIRMATION_MESSAGE = (
    "Prontinho! Reiniciei nossa conversa e apaguei os dados que eu tinha guardado sobre você. "
    "Pode começar de novo quando quiser."
)


def _sanitize_agent_name(name: str) -> str:
    """Espelha resolveAgentQueueName do Inbound-Service (minúsculas, sem
    acento/pontuação/espaço) — tem que bater exatamente, senão a fila que
    este worker declara diverge da que o Inbound-Service publica."""
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())


_RAW_AGENT_NAME = os.getenv("AGENT_NAME")
if not _RAW_AGENT_NAME:
    raise RuntimeError(
        "AGENT_NAME não definida — cada instância do worker max atende um único "
        "agente e precisa saber qual (mesmo nome cadastrado no Agent Console)."
    )

AGENT_NAME = _sanitize_agent_name(_RAW_AGENT_NAME)
QUEUE = f"task.agent.{AGENT_NAME}.create"
DLQ = f"{QUEUE}.dlq"
# Ingestão de RAG dos documentos DESTE agente — publicada pelo Agent-Api
# (rag-ingest-publisher.ts#resolveAgentRagQueueName) quando alguém anexa um
# arquivo na aba RAG do agente no Agent Console.
RAG_QUEUE = f"task.agent.{AGENT_NAME}.rag"
RAG_DLQ = f"{RAG_QUEUE}.dlq"


def _log(payload: dict, msg: str) -> None:
    """Log com correlação por sessão/contato — todo log de uma mesma mensagem
    carrega o mesmo prefixo, pra dar pra rastrear com grep no stdout do
    worker (ex: `docker logs max | grep <session_id>`)."""
    target = payload.get("target") or {}
    session = payload.get("messagingSession") or {}
    print(f"[julia] [session={session.get('id')} wa={target.get('waId')}] {msg}")


def _base_outbound_payload(payload: dict) -> dict:
    return {
        "target": payload.get("target"),
        "channel": payload.get("channel"),
        "messagingSession": payload.get("messagingSession"),
        "origin": "AI",
    }


def _message_log_ids(payload: dict) -> list[str]:
    """mongoMessageId é o id preferido (mensagem já salva no Mongo pelo
    Inbound-Service); cai pro externalMessageId só se vier ausente."""
    messages = payload.get("messages") or []
    return [m.get("mongoMessageId") or m.get("externalMessageId") for m in messages if m.get("mongoMessageId") or m.get("externalMessageId")]


def _log_message_stage(payload: dict, stagio: str) -> None:
    ids = _message_log_ids(payload)
    if not ids:
        return
    record_message_logs([{"messageId": mid, "messageLog": f"AI Worker - {_RAW_AGENT_NAME}", "stagio": stagio} for mid in ids])


def _handle_unsupported_format(channel, payload: dict, agent: dict) -> None:
    _log(payload, "formato nao suportado -> outbound com unsupportedFormatMessage")
    outbound = _base_outbound_payload(payload)
    outbound["answer"] = {"text": agent.get("unsupportedFormatMessage", ""), "audio": "", "image": ""}
    outbound["finishesProcessing"] = True
    publish_outbound_message(channel, outbound)
    _log_message_stage(payload, "end")


def _handle_generation_error(channel, payload: dict, agent: dict, error: Exception) -> None:
    _log(payload, f"ERRO ao gerar resposta do agente {agent.get('name', AGENT_NAME)}: {error}")
    print(traceback.format_exc())

    if agent.get("errorEnabled") and agent.get("errorMessage"):
        text = agent["errorMessage"]
    else:
        text = generate_free_error_message(agent.get("name", "Assistente"), openai_api_key=agent.get("openaiToken"))

    outbound = _base_outbound_payload(payload)
    outbound["answer"] = {"text": text, "audio": "", "image": ""}
    outbound["finishesProcessing"] = True
    publish_outbound_message(channel, outbound)
    _log_message_stage(payload, "end")


def _is_reset_keyword(pergunta: str, whatsapp_channel: dict) -> bool:
    """Determinístico, roda ANTES do LLM — a mensagem do contato precisa
    bater EXATAMENTE (normalizada, sem acento/maiúscula/pontuação) com uma
    das palavras-chave configuradas no canal (Channel.wordsToReset),
    pra não disparar reset por engano numa frase que só contenha a palavra."""
    palavras = whatsapp_channel.get("wordsToReset") or []
    if not palavras:
        return False
    pergunta_normalizada = normalize_text(pergunta)
    return any(pergunta_normalizada == normalize_text(p) for p in palavras)


def _handle_reset_journey(channel, payload: dict, target: dict, whatsapp_channel: dict) -> None:
    sessoes_apagadas = resetar_jornada(target)
    _log(payload, f"reset de jornada: {sessoes_apagadas} sessao(oes) ADK apagada(s) + metadados limpos")

    mensagem = whatsapp_channel.get("resetMessage") or RESET_CONFIRMATION_MESSAGE
    outbound = _base_outbound_payload(payload)
    outbound["answer"] = {"text": mensagem, "audio": "", "image": ""}
    outbound["finishesProcessing"] = True
    publish_outbound_message(channel, outbound)
    _log_message_stage(payload, "end")


def _handle_handoff(channel, payload: dict, agent: dict, reason: str | None, suggested_queue: str | None = None) -> None:
    whatsapp_channel = payload.get("channel") or {}
    service_island_id = whatsapp_channel.get("serviceIslandId")
    
    _log(payload, f"handoff solicitado, motivo='{reason}' fila_sugerida='{suggested_queue}' ilha={service_island_id}")

    queues: list[dict] = []
    if service_island_id:
        try:
            queues = get_service_island_queues(service_island_id) # Pega dados de uma fila de acordo com o id da ilha de atendimento
        except Exception as e:
            _log(payload, f"Falha ao buscar filas da ilha {service_island_id}: {e}")

    queue_id = choose_handoff_queue( # Pega informações da fila e caso não encontre retorne a default
        queues, reason or "", agent.get("defaultQueueId"), suggested_queue, openai_api_key=agent.get("openaiToken") 
    )
    
    _log(payload, f"handoff -> desk.ticket.create, queueId={queue_id}")

    desk_payload = _base_outbound_payload(payload)
    desk_payload["agent"] = {"id": agent.get("id"), "name": agent.get("name")}
    desk_payload["queueId"] = queue_id
    desk_payload["handoffReason"] = reason

    publish_desk_ticket_create(channel, desk_payload)
    _log_message_stage(payload, "end")


def _on_message(channel, method, properties, body):
    payload = {}
    try:
        payload = json.loads(body)
        agent = payload.get("agent") or {}
        messages = payload.get("messages") or []
        target = payload.get("target") or {}
        messaging_session = payload.get("messagingSession") or {}

        _log_message_stage(payload, "start")

        _log(
            payload,
            f"RECEBIDA de {QUEUE}: agent={agent.get('name')} ({agent.get('id')}) "
            f"{len(messages)} msg(s) tipos={[m.get('type') for m in messages]}",
        )

        non_text = [m for m in messages if (m.get("type") or "").upper() != "TEXT"]
        if non_text or not messages:
            _handle_unsupported_format(channel, payload, agent)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            _log(payload, "ACK (formato nao suportado)")
            return

        # Mensagens agrupadas viram uma única pergunta, respeitando a ordem de
        # recebimento (regra de agrupamento do Inbound-Service).
        pergunta = "\n".join(m.get("text", "") for m in messages).strip()

        whatsapp_channel = payload.get("channel") or {}
        if _is_reset_keyword(pergunta, whatsapp_channel):
            _log(payload, f"pergunta='{pergunta[:200]}' bateu com palavra-chave de reset -> resetando jornada")
            _handle_reset_journey(channel, payload, target, whatsapp_channel)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            _log(payload, "ACK (reset de jornada)")
            return

        _log(payload, f"pergunta='{pergunta[:200]}' -> chamando ADK")

        try:
            resultado = gerar_resposta(pergunta, target, agent, session=messaging_session) # Chama gerador de resposta
        except Exception as e:
            _log(payload, f"ADK falhou: {e}")
            print(traceback.format_exc())
            _handle_generation_error(channel, payload, agent, e)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            _log(payload, "ACK (erro na geracao)")
            return

        _log(
            payload,
            f"ADK respondeu: handoff={resultado.handoff_requested} "
            f"texto='{(resultado.texto or '')[:200]}'",
        )

        if resultado.handoff_requested: # Verifica se vai mandar para atendimento humano
            _handle_handoff(channel, payload, agent, resultado.handoff_reason, resultado.handoff_suggested_queue)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            _log(payload, "ACK (handoff)")
            return

        outbound = _base_outbound_payload(payload)
        outbound["answer"] = {"text": resultado.texto or "", "audio": "", "image": ""}
        outbound["finishesProcessing"] = True
        publish_outbound_message(channel, outbound)
        _log_message_stage(payload, "end")

        channel.basic_ack(delivery_tag=method.delivery_tag)
        _log(payload, "ACK (resposta normal publicada em outbound.message.send)")
    except Exception as e:
        _log(payload, f"ERRO NAO TRATADO ao processar mensagem do agente generico: {e}")
        print(traceback.format_exc())
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def _on_rag_ingest(channel, method, properties, body):
    """Ingestão roda numa thread (extração/embedding pode levar minutos e
    travaria o consumo de mensagens do agente) e o ACK sai na hora — o
    resultado (READY/FAILED) sempre volta pro Agent-Api via
    PATCH /internal/rag-documents/:id/status, mesmo em erro."""
    try:
        payload = json.loads(body)
        required = ("ragDocumentId", "agentId", "organizationId", "s3Key", "fileName", "chunkSize")
        missing = [field for field in required if field not in payload]
        if missing:
            print(f"[rag] payload inválido em {RAG_QUEUE}, campos ausentes: {missing}")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        from src.services.rag_ingestion.ingest import run_ingestion

        print(f"[rag] RECEBIDA de {RAG_QUEUE}: documento {payload['ragDocumentId']} ({payload['fileName']})")
        threading.Thread(target=run_ingestion, args=(payload,), daemon=True).start()
        channel.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"[rag] ERRO ao receber ingestão de {RAG_QUEUE}: {e}")
        print(traceback.format_exc())
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def _declare_queue_with_dlq(channel, queue: str, dlq: str) -> None:
    channel.queue_declare(queue=dlq, durable=True)
    channel.queue_declare(
        queue=queue,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": dlq,
        },
    )


def start_consumer() -> None:
    rabbitmq = RabbitMQ()
    channel = rabbitmq.connect()

    _declare_queue_with_dlq(channel, QUEUE, DLQ)
    _declare_queue_with_dlq(channel, RAG_QUEUE, RAG_DLQ)

    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=QUEUE, on_message_callback=_on_message)
    channel.basic_consume(queue=RAG_QUEUE, on_message_callback=_on_rag_ingest)

    print(f"Aguardando mensagens na fila {QUEUE} e ingestões de RAG na fila {RAG_QUEUE}")
    channel.start_consuming()
