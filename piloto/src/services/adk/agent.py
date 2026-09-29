from datetime import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.tools import ToolContext

from src.services.adk.infos import AGENT_TIMEZONE, CHAVES_METADATA, GOOGLE_ADK_MODEL, STATE_METADADOS_ADICIONAIS
from src.services.adk.rag_graph import consultar_base_de_conhecimento
from src.services.adk.tools import (
    atualizar_empresa_cliente,
    atualizar_nome_cliente,
    atualizar_uso_sistema_whatsapp,
    atualizar_volumetria_atendimento,
    consultar_eventos_calendario,
    criar_card_kanban,
    criar_evento_calendario,
    encerrar_conversa,
    parar_envio_campanhas,
    registrar_disponibilidade_contato,
    solicitar_atendimento_humano,
)

# Blocos montados em build_agent:
# - data_atual_block: data/hora atual — base pra converter "amanhã às 14h" em data
# - personality_block: instruções do Agent Console (empresa, o que pode falar,
#   personalidade) — é o que muda de um agente pro outro
# - dados_conhecidos_block: dados já salvos no metadata do contato (vem antes
#   do fluxo/coleta, que se referem a ele como "acima")
# - fluxo_block: primeiro contato ou conversa com histórico (ver _tem_historico)
# - coleta_dados_block: roteiro fixo de coleta de dados do contato
# - rag_block: uso da base de conhecimento (só com ragEnabled)
BASE_INSTRUCTION = """
Você é {nome}, atendendo via WhatsApp.
Responda de forma natural, clara e objetiva, sempre em português. Faça uma
pergunta por vez quando precisar de mais informação do contato — nunca
acumule várias perguntas na mesma mensagem. Nunca invente informações que
você não tenha certeza: sobre a empresa, produtos, serviços e preços, fale
apenas o que estiver nas instruções do agente abaixo ou na base de
conhecimento.

{data_atual_block}

{personality_block}

{dados_conhecidos_block}

{fluxo_block}

{coleta_dados_block}

{rag_block}

## Ferramentas disponíveis

- atualizar_nome_cliente / atualizar_empresa_cliente /
  atualizar_volumetria_atendimento / atualizar_uso_sistema_whatsapp: chame
  cada uma assim que o contato informar ou confirmar o dado correspondente
  (não precisa perguntar de novo o que já estiver na seção "Dados já
  registrados" acima).
- registrar_disponibilidade_contato: chame quando o contato informar a data
  e o horário em que pode conversar — ela já cria (ou remarca) o evento no
  calendário. Não chame criar_evento_calendario de novo para o mesmo horário.
- criar_card_kanban: cria/atualiza o card do contato no Kanban, com uma
  descrição do lead escrita por você.
- criar_evento_calendario: cria um evento no calendário para este contato
  (para outros compromissos além do horário de conversa acima).
- consultar_eventos_calendario: mostra os eventos já marcados com este
  contato e os horários ocupados da agenda — use antes de propor/confirmar
  um horário ou quando o contato perguntar o que já está marcado.
- solicitar_atendimento_humano: chame sempre que o contato pedir
  explicitamente para falar com uma pessoa, ou quando a dúvida estiver fora
  do que você consegue resolver com segurança. Se você já souber a fila
  certa, preencha o parâmetro fila_sugerida com o nome dela.
- encerrar_conversa: chame quando o contato se despedir, confirmar que não
  precisa de mais nada, ou logo depois de encaminhar o atendimento humano.
- parar_envio_campanhas: chame assim que o contato pedir explicitamente para
  não receber mais campanhas/mensagens em massa (ex: "não quero mais
  receber", "pare de me mandar mensagem", "me remova da lista").
"""

# Campo "personality" do Agent Console — texto livre onde a empresa define
# quem o agente representa, o que pode/não pode falar e o tom. O roteiro de
# coleta de dados e as ferramentas continuam fixos, fora do controle dele.
PERSONALITY_INSTRUCTION = """
## Instruções do agente

As instruções abaixo foram definidas pela empresa que você representa:
informações da empresa, o que você pode e o que não pode falar, e como deve
se comunicar. Siga-as em tudo, exceto no roteiro de coleta de dados e no uso
das ferramentas, que são obrigatórios.

{personality}
"""

PERSONALITY_VAZIA = (
    "Nenhuma instrução específica foi configurada. Não fale sobre produtos, "
    "serviços, preços ou informações da empresa; se o contato perguntar, "
    "ofereça atendimento humano."
)

RAG_INSTRUCTION = """
Você tem uma base de conhecimento anexada. Sempre que a pergunta do cliente
puder envolver informações específicas dessa base chame consultar_conhecimento ANTES de
responder, e responda só com base no que a ferramenta retornar. Se a ferramenta
não retornar nada relevante, diga isso ao cliente em vez de inventar uma
resposta.
"""

# Saudação obrigatória — só entra na instrução quando o contato NÃO tem
# histórico de conversa ainda (ver _tem_historico).
PRIMEIRO_CONTATO_INSTRUCTION = """
## Primeiro contato

Este é o PRIMEIRO contato com esta pessoa — não há histórico de conversa
anterior. Apresente-se de forma breve e cordial, explique que precisa
confirmar alguns dados rápidos, e comece a coleta de dados descrita abaixo.
"""

# Conversa normal — quando o contato já tem histórico (ver _tem_historico).
FLUXO_CONTINUO_INSTRUCTION = """
## Conversa com histórico

Este contato já falou com você antes — continue de forma fluida e natural,
sem repetir a apresentação inicial. Siga direto para a coleta de dados
descrita abaixo, pulando o que já estiver em "Dados já registrados".
"""

# Roteiro de coleta de dados — sempre presente, independente de já ter
# histórico ou não. Dirigido pelo bloco "Dados já registrados"
# (_build_known_data_block), não por qual turno da conversa está.
COLETA_DADOS_INSTRUCTION = """
## Coleta de dados obrigatória

Confirme estes dados sobre o contato, um de cada vez (pule qualquer um que
já esteja em "Dados já registrados" acima):

1. Nome da pessoa -> atualizar_nome_cliente
2. Nome da empresa -> atualizar_empresa_cliente
3. Volumetria de atendimento da empresa, de 1 a 10 (1 = baixa, 10 = muito
   alta) -> atualizar_volumetria_atendimento
4. Se já usou algum sistema de gerenciamento de WhatsApp para empresas
   -> atualizar_uso_sistema_whatsapp
5. Uma data e um horário em que pode conversar
   -> registrar_disponibilidade_contato (já cria o evento no calendário)

Assim que o contato informar cada dado, chame a ferramenta correspondente
imediatamente.

Card no Kanban: assim que o contato responder o nome da empresa, chame
criar_card_kanban com a descrição do lead. A cada dado novo coletado depois
disso, chame criar_card_kanban de novo para atualizar a descrição.

{metadados_adicionais_block}
Quando todos os dados estiverem registrados (e o evento criado), confirme
com o contato a data e o horário combinados e chame encerrar_conversa. Não
continue fazendo perguntas depois disso.
"""

# Metadados configurados no card "Metadados" do Agent Console (só os
# ativos, via payload agent.metadataFields) — entram no mesmo roteiro de
# coleta, depois dos dados fixos acima.
METADADOS_ADICIONAIS_INSTRUCTION = """
Dados adicionais: colete também estes dados, um de cada vez, seguindo a
regra de cada um (pule os que já estiverem em "Dados já registrados"). Assim
que o contato informar um deles, chame registrar_metadado com a chave
exata indicada:

{itens}
"""


"""
------------------------------------

Abaixo temos funções de funcionamento padrão de agentes

------------------------------------
"""

def _tem_historico(target_info: dict) -> bool: # Checar o metadado para ver se a variavel existe de contato iniciado
    """Verdadeiro só depois que ESTE contato já trocou pelo menos uma mensagem
    com o agente antes — marcado de forma determinística pelo runner ao fim de
    todo turno (metadata.contato_iniciado), não por uma tool que o modelo
    poderia esquecer de chamar. Note que target_info.name (nome de perfil do
    WhatsApp) NÃO conta como histórico: ele existe mesmo no primeiro
    contato."""
    metadata = target_info.get("metadata") or {}
    return bool(metadata.get("contato_iniciado"))


LABELS_METADATA = {
    "nome": "Nome",
    "nome_empresa": "Empresa",
    "volumetria_atendimento": "Volumetria de atendimento (1-10)",
    "ja_usou_sistema_whatsapp": "Já usou sistema de gerenciamento de WhatsApp",
    "data_horario_contato": "Data/horário combinado para conversar",
}

DIAS_SEMANA = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")


def _build_data_atual_block() -> str:
    """Sem isso o modelo não tem como transformar "amanhã às 14h" ou "sexta"
    numa data de verdade pro calendário."""
    agora = datetime.now(ZoneInfo(AGENT_TIMEZONE))
    return (
        f"Data e hora atual: {DIAS_SEMANA[agora.weekday()]}, "
        f"{agora.strftime('%d/%m/%Y %H:%M')} (fuso {AGENT_TIMEZONE})."
    )


def _campos_metadados(agent_info: dict) -> list[dict]:
    """Metadados ativos do Agent Console (payload agent.metadataFields). Chaves
    que colidem com os dados fixos (CHAVES_METADATA) ficam de fora — esses já
    têm ferramenta e roteiro próprios."""
    campos = []
    for campo in agent_info.get("metadataFields") or []:
        chave = (campo.get("nameToAgent") or "").strip()
        if chave and chave not in CHAVES_METADATA:
            campos.append({"chave": chave, "nome": campo.get("name") or chave, "regra": (campo.get("rule") or "").strip()})
    return campos


def _build_metadados_adicionais_block(campos: list[dict]) -> str:
    if not campos:
        return ""
    itens = "\n".join(f"- {c['chave']} ({c['nome']}): {c['regra']}" for c in campos)
    return METADADOS_ADICIONAIS_INSTRUCTION.format(itens=itens)


def _build_known_data_block(target_info: dict, campos: list[dict]) -> str: # Função de coletar os metadados e passar para o agente
    """Dados que já existem no cadastro do contato (nome vindo do perfil do
    WhatsApp, qualquer um dos CHAVES_METADATA ou dos metadados do Agent
    Console já registrado em conversa anterior) — evita que o agente pergunte
    de novo algo que já sabe, mesmo numa sessão nova."""
    metadata = target_info.get("metadata") or {}
    nome = target_info.get("name") or metadata.get("nome")

    dados: dict[str, str] = {}
    if nome:
        dados["nome"] = nome
    for chave in CHAVES_METADATA:
        if chave == "nome":
            continue
        valor = metadata.get(chave)
        if valor:
            dados[chave] = valor

    labels = dict(LABELS_METADATA)
    for campo in campos:
        valor = metadata.get(campo["chave"])
        if valor:
            dados[campo["chave"]] = valor
            labels[campo["chave"]] = f"{campo['nome']} ({campo['chave']})"

    if not dados:
        return "Nenhum dado deste contato foi registrado ainda."

    linhas = ["Dados já registrados deste contato — não pergunte de novo o que já está aqui:"]
    for chave, valor in dados.items():
        linhas.append(f"- {labels.get(chave, chave)}: {valor}")
    return "\n".join(linhas)


def _build_metadado_tool(campos: list[dict]):
    """Closure pelo mesmo motivo da tool de RAG: as chaves válidas mudam por
    agente (vêm do payload). Grava em STATE_METADADOS_ADICIONAIS, que o runner
    sincroniza em Target.metadata ao fim do turno."""
    chaves_validas = {c["chave"] for c in campos}

    def registrar_metadado(tool_context: ToolContext, chave: str, valor: str) -> dict:
        """Registra um dos dados adicionais que você deve coletar do contato
        (listados em "Dados adicionais" na sua instrução). Use a chave exata
        indicada lá e o valor informado/confirmado pelo contato."""
        chave = (chave or "").strip()
        if chave not in chaves_validas:
            return {"ok": False, "erro": f"Chave inválida. Use uma destas: {', '.join(sorted(chaves_validas))}."}
        valor = str(valor or "").strip()
        if not valor:
            return {"ok": False, "erro": "O valor não pode ser vazio."}
        # Reatribui o dict inteiro (em vez de mutar) pro ADK registrar o delta
        # de state e persistir na sessão.
        metadados = dict(tool_context.state.get(STATE_METADADOS_ADICIONAIS) or {})
        metadados[chave] = valor
        tool_context.state[STATE_METADADOS_ADICIONAIS] = metadados
        return {"ok": True, chave: valor}

    return registrar_metadado


def _build_rag_tool(agent_id: str, openai_api_key: str | None): # Retorna os chunks encontratos de acordo com a pergunta do usuario
    """A tool de RAG é montada como closure (capturando agent_id/openai_api_key)
    em vez de uma função de módulo fixa — como o worker atende agentes/
    organizações diferentes através da mesma fila genérica, não existe um
    agent_id fixo pra hardcodar num tools.py estático. openai_api_key vem do
    Agent Console (token por agente, criptografado no banco); None cai no
    fallback do env em get_embeddings (ver infra/pgvector/connection.py)."""

    async def consultar_conhecimento(tool_context: ToolContext, pergunta: str) -> dict:
        """Busca na base de conhecimento anexada a este agente informações
        relevantes pra responder à pergunta do cliente."""
        contexto = await consultar_base_de_conhecimento(pergunta, agent_id, openai_api_key)
        if not contexto:
            return {"contexto": "", "aviso": "Nada relevante encontrado na base de conhecimento."}
        return {"contexto": contexto}

    return consultar_conhecimento


def build_agent(agent_info: dict, target_info: dict | None = None) -> Agent:
    """Monta o Agent do ADK do zero a cada mensagem (não é um root_agent fixo
    como no axel) — instrução e ferramentas variam por agent_info (personality/
    ragEnabled) e target_info (nome/histórico já conhecidos), que vêm
    frescos no payload de cada mensagem da fila."""
    target_info = target_info or {}
    nome = agent_info.get("name") or "o assistente virtual"
    personality = (agent_info.get("personality") or "").strip()
    rag_enabled = bool(agent_info.get("ragEnabled"))

    if _tem_historico(target_info):
        fluxo_block = FLUXO_CONTINUO_INSTRUCTION
    else:
        fluxo_block = PRIMEIRO_CONTATO_INSTRUCTION

    campos = _campos_metadados(agent_info)
    dados_conhecidos_block = _build_known_data_block(target_info, campos)
    personality_block = PERSONALITY_INSTRUCTION.format(personality=personality or PERSONALITY_VAZIA)
    rag_block = RAG_INSTRUCTION if rag_enabled else ""
    coleta_dados_block = COLETA_DADOS_INSTRUCTION.format(
        metadados_adicionais_block=_build_metadados_adicionais_block(campos),
    )

    instruction = BASE_INSTRUCTION.format(
        nome=nome,
        data_atual_block=_build_data_atual_block(),
        fluxo_block=fluxo_block,
        coleta_dados_block=coleta_dados_block,
        dados_conhecidos_block=dados_conhecidos_block,
        personality_block=personality_block,
        rag_block=rag_block,
    )

    tools = [
        atualizar_nome_cliente,
        atualizar_empresa_cliente,
        atualizar_volumetria_atendimento,
        atualizar_uso_sistema_whatsapp,
        registrar_disponibilidade_contato,
        criar_card_kanban,
        criar_evento_calendario,
        consultar_eventos_calendario,
        solicitar_atendimento_humano,
        encerrar_conversa,
        parar_envio_campanhas,
    ]

    if rag_enabled:
        tools.append(_build_rag_tool(agent_info["id"], agent_info.get("openaiToken")))

    if campos:
        tools.append(_build_metadado_tool(campos))

    return Agent(
        # Nome interno do ADK — fixo, não é o nome de exibição do agente (que
        # pode ter espaços/acentos, ex: "Assistente Virtual"). O nome de
        # exibição só entra na instrução (`nome` acima).
        name="recepcionista_agent",
        model=GOOGLE_ADK_MODEL,
        description="Agente de atendimento via WhatsApp.",
        # Como função (InstructionProvider) e não string: o ADK só troca
        # {chave} por valores do state em instrução string, e levantaria
        # KeyError se a personalidade ou a regra de um metadado (texto livre
        # do Agent Console) tivesse algo como "{cidade}".
        instruction=lambda _ctx: instruction,
        tools=tools,
    )
