import os

# Metadados que a IA pode armazenar (sincronizados em Target.metadata ao fim
# de cada turno — ver runner.py). Roteiro de prospecção de leads:
#   nome                      -> nome da pessoa
#   nome_empresa              -> nome da empresa
#   volumetria_atendimento    -> 1 (baixa) a 10 (muito alta)
#   ja_usou_sistema_whatsapp  -> "Sim"/"Não" (+ qual sistema, se informar)
#   data_horario_contato      -> data/hora que o lead pode conversar (dd/mm/aaaa hh:mm)
CHAVES_METADATA = (
    "nome",
    "nome_empresa",
    "volumetria_atendimento",
    "ja_usou_sistema_whatsapp",
    "data_horario_contato",
)

# Chave do state da sessão onde a tool registrar_metadado guarda os metadados
# configurados no Agent Console ({nameToAgent: valor}) — o runner mescla em
# Target.metadata junto com CHAVES_METADATA.
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
