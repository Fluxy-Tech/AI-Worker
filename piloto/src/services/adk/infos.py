import os

# Metadados que o próprio worker grava (sincronizados em Target.metadata ao
# fim de cada turno — ver runner.py). Não há coleta fixa: o que o agente
# pergunta ao contato vem só dos metadados criados no Agent Console
# (agent.metadataFields). Fica aqui só o que é preenchido por uma função:
#   data_horario_contato -> data/hora do evento agendado (dd/mm/aaaa hh:mm)
CHAVES_METADATA = ("data_horario_contato",)

# Chaves que um metadado do Agent Console não pode usar — já têm dono
# (CHAVES_METADATA) ou são controle interno do runner.
CHAVES_RESERVADAS = CHAVES_METADATA + ("contato_iniciado",)

# Funções fixas ligadas no card "Funções" do Agent Console (payload
# agent.functions: [{type, runAtStart, runAfterMetadata}]).
FUNCAO_EVENTO = "CALENDAR_EVENT"
FUNCAO_CARD = "KANBAN_CARD"

# Chave do state da sessão onde a tool registrar_metadado guarda os metadados
# configurados no Agent Console ({nameToAgent: valor}) — o runner mescla em
# Target.metadata junto com CHAVES_METADATA. Também é a fonte dos dados do
# lead no evento do calendário (ver tools.descricao_lead).
STATE_METADADOS_ADICIONAIS = "metadados_adicionais"

# Nome do agente
APP_NAME = os.getenv("GOOGLE_ADK_APP_NAME", "piloto")

# Fuso usado pra interpretar datas/horários que o contato fala ("amanhã às
# 14h") e pra mostrar a data atual pro modelo — o calendário grava em UTC.
AGENT_TIMEZONE = os.getenv("AGENT_TIMEZONE", "America/Sao_Paulo")

# Modelo Gemini usado pelo agente ADK — GOOGLE_ADK_MODEL sobrescreve o default
# fixado aqui. Se a env estiver setada em produção com um modelo descontinuado
# (ex: gemini-1.5-pro, removido pelo Google), esse default NÃO se aplica — a
# env sempre vence. Centralizado aqui (em vez de hardcoded em agent.py) pra
# não haver dois lugares pra manter em sincronia quando o modelo mudar de novo.
GOOGLE_ADK_MODEL = os.getenv("GOOGLE_ADK_MODEL", "gemini-3.8-flash")
