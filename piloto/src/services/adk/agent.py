from datetime import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.tools import ToolContext

from src.services.adk.infos import (
    AGENT_TIMEZONE,
    CHAVES_METADATA,
    CHAVES_RESERVADAS,
    FUNCAO_EVENTO,
    GOOGLE_ADK_MODEL,
    STATE_METADADOS_ADICIONAIS,
)
from src.services.adk import funcionalidades
from src.services.adk.rag_graph import consultar_base_de_conhecimento
from src.services.adk.tools import (
    agendar_evento,
    consultar_eventos_calendario,
    desmarcar_evento,
    encerrar_conversa,
    parar_envio_campanhas,
    solicitar_atendimento_humano,
)

# O prompt é só a Identidade do Agent Console (nome + personalidade) e os
# blocos das funções ligadas no agente — nenhum texto fixo além disso. A
# ordem é sempre esta, e cada bloco entra só com a sua condição (vazio = fica
# de fora):
# 1. identidade: "Você é {nome}." + personalidade (sempre)
# 2. dados_conhecidos_block: metadados já salvos do contato (só com coleta)
# 3. coleta_dados_block: metadados do card "Metadados" do Console + Flows com
#    "salvar como metadados" + como encerrar (só com coleta e/ou agendamento)
# 4. funcionalidades_block: demais funcionalidades de mensagem cadastradas no
#    Console (catálogo, botões, listas, carrosséis...) — ver funcionalidades.py
# 5. data_atual_block + funcoes_block: agendamento e agenda do contato
#    (consultar, remarcar, desmarcar) só com CALENDAR_EVENT — a data é a base
#    pra converter "amanhã às 14h". Desligado, entra só o aviso de que o
#    agente não tem acesso à agenda. O card do Kanban roda no runner, sem
#    instrução.
# 6. rag_block (só com ragEnabled) e rgbs_block: quebra da resposta em várias
#    mensagens com [QB] (só com RGBS ligado — o consumer faz o split e envia
#    uma por vez)
# As ferramentas padrão (atendimento humano, encerrar, parar campanhas) não
# têm texto no prompt: o modelo as conhece pelas docstrings em tools.py.
IDENTIDADE_INSTRUCTION = "Você é {nome}."

# RGBS (Receives, Generates, Breaks and Sends) ligado no Agent Console — o
# marcador precisa bater com RGBS_MARKER do consumer, que faz o split.
RGBS_INSTRUCTION = """
## Formato da resposta: mensagens separadas

Escreva a resposta como uma sequência de mensagens curtas de WhatsApp, como
uma pessoa digitando. Separe cada mensagem com o marcador [QB] (exatamente
assim: maiúsculas, entre colchetes). Cada trecho entre marcadores é enviado
como uma mensagem separada.

- Quebre por ideia: cumprimento, informação, pergunta.
- Use de 1 a 4 mensagens por resposta; uma resposta curta pode ser uma
  mensagem só, sem marcador.
- Nunca coloque [QB] no começo ou no fim da resposta, nem dois seguidos.
- Nunca quebre no meio de uma frase, de uma lista ou de um link.
- Resposta em que você envia uma funcionalidade de mensagem ou um formulário
  sai numa mensagem só, no corpo da funcionalidade: não use [QB] nela.

Exemplo: Oi, Maria! Tudo bem?[QB]Consigo te ajudar com isso, sim.[QB]Qual o melhor horário pra você?
"""

RAG_INSTRUCTION = """
Você tem uma base de conhecimento anexada. Sempre que a pergunta do cliente
puder envolver informações específicas dessa base chame consultar_conhecimento ANTES de
responder, e responda só com base no que a ferramenta retornar. Se a ferramenta
não retornar nada relevante, diga isso ao cliente em vez de inventar uma
resposta.
"""

# Coleta dos metadados criados no card "Metadados" do Agent Console (só os
# ativos, via payload agent.metadataFields) e dos Flows com "salvar como
# metadados" — não existe dado fixo: sem nenhum dos dois, esta seção não
# entra. Dirigida pelo bloco "Dados já registrados" (_build_known_data_block),
# não por qual turno da conversa está.
COLETA_DADOS_INSTRUCTION = """
## Coleta de dados obrigatória

{partes}
"""

COLETA_METADADOS = """Colete estes dados do contato, um de cada vez, seguindo a regra de cada um
(pule qualquer um que já esteja em "Dados já registrados" acima). Assim que o
contato informar um deles, chame registrar_metadado com a chave exata
indicada:

{itens}"""

# O Inbound-Service grava as respostas em Target.metadata e manda pro agente
# a mensagem "📝 Respondeu o formulário ..." (interactive-reply-service.ts).
COLETA_FLOWS = """Estes formulários (Flows) {tambem}fazem parte da coleta: envie cada um com
enviar_flow, informando o id, no momento do "Quando usar" dele{sem_momento}, e
espere o contato responder antes de seguir. Um formulário está respondido
quando chega a mensagem "📝 Respondeu o formulário ..."; as respostas são
salvas sozinhas nos metadados do contato, então não pergunte de novo o que
ele preencheu, não chame registrar_metadado para elas e não reenvie um
formulário já respondido:

{itens}"""

ENCERRAMENTO_INSTRUCTION = """
## Encerramento

{encerramento}
"""

# Condição de "terminou" de cada combinação coleta x agendamento — entra no
# {condicao} dos textos de encerramento abaixo.
CONDICAO_COLETA = "a coleta de dados estiver concluída"
CONDICAO_COLETA_E_AGENDAMENTO = 'a coleta de dados estiver concluída e o evento estiver agendado (ver "Funções" abaixo)'
CONDICAO_AGENDAMENTO = 'o evento estiver agendado (ver "Funções" abaixo)'

ENCERRAMENTO_SEM_AGENDAMENTO = """Quando {condicao}, agradeça, diga que a equipe vai
dar continuidade e chame encerrar_conversa. Não continue fazendo perguntas
depois disso."""

ENCERRAMENTO_COM_AGENDAMENTO = """Quando {condicao}, confirme com o contato a data e
o horário combinados e chame encerrar_conversa. Não continue fazendo
perguntas depois disso."""

# Switch "Enviar para o atendimento humano ao terminar" do card "Funções"
# (payload agent.handoffAfterFunctions) — troca o encerrar_conversa do fim
# do roteiro pelo encaminhamento a um atendente.
ENCERRAMENTO_HANDOFF = """Quando {condicao}, chame solicitar_atendimento_humano
com o motivo "{motivo}" para encaminhar o contato a um atendente. Não chame
encerrar_conversa e não continue fazendo perguntas depois disso."""

# Função CALENDAR_EVENT ligada no Console. O momento vem dos switches
# runAtStart / runAfterMetadata (os dois podem estar ligados).
FUNCAO_EVENTO_INSTRUCTION = """
## Funções

### Agendamento de evento

Use agendar_evento para marcar um evento com o contato: pergunte o dia e o
horário que ele prefere e chame a ferramenta. Dê sempre um nome ao evento
(curto, dizendo o que é e com quem) e uma descrição com o objetivo e o
contexto da conversa, escritos por você. A agenda é checada na hora —
se vier disponivel=false, explique de forma breve que esse horário não está
livre e peça outro. Se a resposta trouxer "responsavel", você pode dizer ao
contato com quem será. Chamar de novo na mesma conversa remarca o mesmo
evento. consultar_eventos_calendario mostra os horários já ocupados, útil
para sugerir alternativas.

{momentos}

### Agenda do contato

O contato pode perguntar sobre os eventos dele: use consultar_eventos_calendario
e informe o dia, o horário e com quem é cada evento (o "responsavel"). Fale
só dos eventos dele, nunca dos horários de outras pessoas.

- Remarcar: se ele quiser mudar a data de um evento, confirme qual é e a nova
  data (sempre futura) e chame agendar_evento com o evento_id dele.
- Desmarcar: se ele quiser cancelar, confirme qual evento é e chame
  desmarcar_evento com o evento_id. Depois, pergunte se ele quer marcar outra
  data.
"""

# Agendamento desligado no Console: sem as ferramentas de agenda, e o modelo
# não pode repassar nem prometer nada de eventos (mesmo que o contato saiba
# de um evento marcado pela equipe).
SEM_AGENDA_INSTRUCTION = """
## Agenda

Você não tem acesso à agenda de eventos. Não informe nem confirme datas,
horários ou responsáveis de eventos, e não marque, remarque ou desmarque
nenhum. Se o contato pedir algo da agenda, diga que a equipe vai ajudar com
isso.
"""

EVENTO_NO_INICIO = """Quando: no início da conversa — logo depois de cumprimentar/se apresentar e
ANTES da coleta de dados. Só siga para a coleta depois que o evento estiver
agendado (ou se o contato disser que não quer agendar)."""

EVENTO_APOS_COLETA = """Quando: depois que a coleta de dados estiver concluída, antes de encerrar a
conversa."""

# Agendamento ligado sem nenhum metadado cadastrado: não há coleta para
# esperar, então o momento "após a coleta" vira "logo no início".
EVENTO_SEM_COLETA = """Quando: logo depois de cumprimentar/se apresentar, assim que entender o que
o contato precisa."""

EVENTO_CONFIRMAR_APOS_COLETA = """Depois da coleta, confirme com o contato se o dia e o horário agendados
continuam bons; se ele quiser mudar, chame agendar_evento de novo."""

"""
------------------------------------

Abaixo temos funções de funcionamento padrão de agentes

------------------------------------
"""

LABELS_METADATA = {
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


def funcoes_ativas(agent_info: dict) -> dict[str, dict]:
    """Funções fixas ligadas no Console (payload agent.functions), por tipo:
    {"CALENDAR_EVENT": {"inicio": bool, "apos_coleta": bool, "estagio": None}, ...}.
    estagio = crmStageId (só KANBAN_CARD; None = estágio padrão). Só entram
    as que têm pelo menos um momento ligado."""
    ativas = {}
    for funcao in agent_info.get("functions") or []:
        inicio = bool(funcao.get("runAtStart"))
        apos_coleta = bool(funcao.get("runAfterMetadata"))
        if funcao.get("type") and (inicio or apos_coleta):
            ativas[funcao["type"]] = {"inicio": inicio, "apos_coleta": apos_coleta, "estagio": funcao.get("crmStageId")}
    return ativas


def _build_funcao_evento_block(evento: dict | None, tem_coleta: bool) -> str:
    if not evento:
        return SEM_AGENDA_INSTRUCTION
    momentos = []
    if not tem_coleta:
        # EVENTO_NO_INICIO fala em "antes da coleta": sem coleta, os dois
        # momentos viram o mesmo texto.
        momentos.append(EVENTO_SEM_COLETA)
    elif evento["inicio"]:
        momentos.append(EVENTO_NO_INICIO)
        if evento["apos_coleta"]:
            momentos.append(EVENTO_CONFIRMAR_APOS_COLETA)
    else:
        momentos.append(EVENTO_APOS_COLETA)
    return FUNCAO_EVENTO_INSTRUCTION.format(momentos="\n\n".join(momentos))


def campos_metadados(agent_info: dict) -> list[dict]:
    """Metadados ativos do Agent Console (payload agent.metadataFields) — a
    única fonte do que o agente coleta. Chaves reservadas (CHAVES_RESERVADAS)
    ficam de fora: já são gravadas pelo próprio worker."""
    campos = []
    for campo in agent_info.get("metadataFields") or []:
        chave = (campo.get("nameToAgent") or "").strip()
        if chave and chave not in CHAVES_RESERVADAS:
            campos.append({"chave": chave, "nome": campo.get("name") or chave, "regra": (campo.get("rule") or "").strip()})
    return campos


def _build_coleta_dados_block(campos: list[dict], flows: list[dict], encerramento: str | None, tem_envio: bool) -> str:
    partes = []
    if campos or flows:
        coleta = []
        if campos:
            itens = "\n".join(f"- {c['chave']} ({c['nome']}): {c['regra']}" for c in campos)
            coleta.append(COLETA_METADADOS.format(itens=itens))
        if flows:
            coleta.append(COLETA_FLOWS.format(
                tambem="também " if campos else "",
                sem_momento=" (sem isso, depois dos dados acima)" if campos else "",
                itens="\n".join(funcionalidades.descrever_flow_coleta(f) for f in flows),
            ))
        partes.append(COLETA_DADOS_INSTRUCTION.format(partes="\n\n".join(coleta)))
        if tem_envio:
            partes.append(funcionalidades.COLETA_SEM_FUNCIONALIDADE)
    if encerramento:
        partes.append(ENCERRAMENTO_INSTRUCTION.format(encerramento=encerramento))
    return "".join(partes)


def _build_encerramento(tem_coleta: bool, evento: dict | None, handoff: bool) -> str | None:
    """Como fechar a conversa quando a coleta e/ou o agendamento terminam. Sem
    nenhum dos dois não há um "fim do roteiro": o agente encerra pelas regras
    gerais das ferramentas."""
    if tem_coleta and evento:
        condicao, motivo = CONDICAO_COLETA_E_AGENDAMENTO, "Coleta de dados e agendamento concluídos"
    elif tem_coleta:
        condicao, motivo = CONDICAO_COLETA, "Coleta de dados concluída"
    elif evento:
        condicao, motivo = CONDICAO_AGENDAMENTO, "Agendamento concluído"
    else:
        return None
    if handoff:
        return ENCERRAMENTO_HANDOFF.format(condicao=condicao, motivo=motivo)
    modelo = ENCERRAMENTO_COM_AGENDAMENTO if evento else ENCERRAMENTO_SEM_AGENDAMENTO
    return modelo.format(condicao=condicao)


def _build_known_data_block(target_info: dict, campos: list[dict], evento: dict | None) -> str: # Função de coletar os metadados e passar para o agente
    """Dados que já existem no cadastro do contato (metadados do Agent Console
    já registrados em conversa anterior e, com o agendamento ligado, o
    horário combinado) — evita que o agente pergunte de novo algo que já
    sabe, mesmo numa sessão nova. Só entra o que o agente coleta: chaves do
    metadata fora disso (e o nome do perfil do WhatsApp) ficam de fora."""
    metadata = target_info.get("metadata") or {}

    dados: dict[str, str] = {}
    for chave in CHAVES_METADATA if evento else ():
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

    campos = campos_metadados(agent_info)
    flows_coleta = funcionalidades.flows_da_coleta(agent_info)
    tem_coleta = bool(campos or flows_coleta)

    funcoes = funcoes_ativas(agent_info)
    evento = funcoes.get(FUNCAO_EVENTO)
    encerramento = _build_encerramento(tem_coleta, evento, bool(agent_info.get("handoffAfterFunctions")))

    # Ordem fixa (ver o comentário do topo): personalidade, dados conhecidos,
    # coleta (que se refere aos dados como "acima"), funcionalidades,
    # agendamento, RAG e RGBS. Blocos vazios (função desligada) ficam de fora.
    blocos = [
        IDENTIDADE_INSTRUCTION.format(nome=nome),
        personality,
        _build_known_data_block(target_info, campos, evento) if tem_coleta else "",
        _build_coleta_dados_block(campos, flows_coleta, encerramento, funcionalidades.tem_envio(agent_info)),
        funcionalidades.build_funcionalidades_block(agent_info, tem_coleta),
        _build_data_atual_block() if evento else "",
        _build_funcao_evento_block(evento, tem_coleta),
        RAG_INSTRUCTION if rag_enabled else "",
        RGBS_INSTRUCTION if (agent_info.get("rgbs") or {}).get("enabled") else "",
    ]
    instruction = "\n\n".join(b.strip() for b in blocos if b and b.strip())

    tools = [
        solicitar_atendimento_humano,
        encerrar_conversa,
        parar_envio_campanhas,
    ]

    if rag_enabled:
        tools.append(_build_rag_tool(agent_info["id"], agent_info.get("openaiToken")))

    if campos:
        tools.append(_build_metadado_tool(campos))

    if evento:
        tools.extend([agendar_evento, consultar_eventos_calendario, desmarcar_evento])

    tools.extend(funcionalidades.build_tools(agent_info, target_info))

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
