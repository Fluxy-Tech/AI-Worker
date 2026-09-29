"""Adaptador só para `adk web` — o worker de produção monta o Agent do zero a
cada mensagem (ver src/services/adk/runner.py:_executar -> build_agent), com
agent_config/target_info vindos do payload da fila. Aqui expomos um
root_agent fixo com um agent_config e target_info fake, pra poder testar a
instrução/tools no `adk web` sem depender do RabbitMQ, do Postgres (sessão
ADK) nem do Agent-Api rodando local.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from src.services.adk.agent import build_agent

_agent_config = {
    "id": "agent-teste-local",
    "name": "Assistente Teste",
    "personality": "Simpática, objetiva e prestativa. Trate o cliente pelo nome quando souber.",
    "ragEnabled": False,
    # Mesmo formato de agent.metadataFields no payload (só os ativos).
    "metadataFields": [
        {"name": "Cidade", "nameToAgent": "cidade", "rule": "Pergunte em qual cidade o contato mora."},
    ],
    # Mesmo formato de agent.functions no payload (card "Funções" do Console).
    "functions": [
        {"type": "CALENDAR_EVENT", "runAtStart": False, "runAfterMetadata": True},
    ],
    # True = encaminha pro atendimento humano ao terminar; False = encerra.
    "handoffAfterFunctions": False,
}

# Troque metadata/name pra simular um contato que já tem histórico e/ou dados
# salvos (ver _tem_historico e _build_known_data_block em src/services/adk/agent.py).
_target_info_teste = {
    "id": "user-teste-local",
    "name": "Contato Teste",
    "metadata": {},
}

root_agent = build_agent(_agent_config, _target_info_teste)
