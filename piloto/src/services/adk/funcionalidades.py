"""
Funcionalidades de mensagem do agente — aba "Função" › "Adicionar
funcionalidade" do Agent Console (tabela AgentFeature). O Inbound-Service
manda todas em agent.features: [{id, type, name, prompt, config, products?}].

Para cada tipo presente este módulo monta:
- a instrução (quando usar = `prompt` escrito pela empresa + detalhes);
- a ferramenta que o agente chama. A ferramenta só valida e devolve
  {"ok": True, "envio": {...}}; o runner lê essas respostas dos eventos do
  turno (não do state, pra não grudarem na sessão e reenviarem depois) e o
  consumer transforma cada envio em mensagem com montar_itens();
- o objeto `interactive` (ou texto) que o Outbound-Worker manda pra Meta.

Tipos: CATALOG, PRODUCT_CAROUSEL, CTA_URL, LIST, REPLY_BUTTONS,
MEDIA_CAROUSEL, LOCATION_REQUEST, CONTEXTUAL_REPLY, LINK_PREVIEW, FLOW e
PAYMENT (este só backend, sem tela no Console).
"""

import re
import uuid

from src.services.adk import integracoes

CATALOG = "CATALOG"
PRODUCT_CAROUSEL = "PRODUCT_CAROUSEL"
CTA_URL = "CTA_URL"
LIST = "LIST"
REPLY_BUTTONS = "REPLY_BUTTONS"
MEDIA_CAROUSEL = "MEDIA_CAROUSEL"
LOCATION_REQUEST = "LOCATION_REQUEST"
CONTEXTUAL_REPLY = "CONTEXTUAL_REPLY"
LINK_PREVIEW = "LINK_PREVIEW"
PAYMENT = "PAYMENT"
FLOW = "FLOW"

# Limites da Meta.
MAX_BODY = 1024
MAX_HEADER = 60
MAX_FOOTER = 60
MAX_BOTAO = 20
MAX_TITULO_SECAO = 24
MAX_CARD_BODY = 160
MAX_PRODUTOS_LISTA = 30
MIN_CARDS, MAX_CARDS = 2, 10
MAX_BOTOES_RESPOSTA = 3
MAX_BOTAO_FLOW = 30
MAX_TEXTO = 4096  # mensagem de texto comum (prévia de link)

# Tipos em que o texto da mensagem é o corpo do próprio envio (todos menos a
# resposta em contexto, que só cita uma mensagem). Regra geral: a resposta do
# agente vira esse corpo e sai só a mensagem da funcionalidade, nunca um
# texto e depois o envio (ver montar_itens).
TIPOS_COM_CORPO = {
    CATALOG, PRODUCT_CAROUSEL, MEDIA_CAROUSEL, CTA_URL, LIST, REPLY_BUTTONS,
    LOCATION_REQUEST, LINK_PREVIEW, PAYMENT, FLOW,
}

# Limite de caracteres do texto de cada tipo (a resposta inteira do agente
# entra nele, ver montar_itens) — vai fixo no prompt de cada funcionalidade.
# Acima disso o texto é cortado com "…".
LIMITE_TEXTO = {tipo: MAX_BODY for tipo in TIPOS_COM_CORPO} | {LINK_PREVIEW: MAX_TEXTO}


def _linha_limite(tipo: str) -> str:
    limite = LIMITE_TEXTO.get(tipo)
    if not limite:
        return ""
    return f"Limite do texto: até {limite} caracteres, contando a sua resposta inteira (passou disso, é cortado)."


TEXTO_PADRAO = {
    CATALOG: "Confira nosso catálogo:",
    PRODUCT_CAROUSEL: "Separei estas opções para você:",
    MEDIA_CAROUSEL: "Confira:",
    CTA_URL: "Acesse pelo botão abaixo:",
    LIST: "Escolha uma opção:",
    REPLY_BUTTONS: "Escolha uma opção:",
    LOCATION_REQUEST: "Pode me enviar a sua localização? Toque no botão abaixo.",
    PAYMENT: "Segue a cobrança:",
    FLOW: "Preencha pelo botão abaixo:",
}

ROTULO = {
    CATALOG: "Catálogo",
    PRODUCT_CAROUSEL: "Carrossel de produtos",
    CTA_URL: "Botão de link",
    LIST: "Lista de opções",
    REPLY_BUTTONS: "Botões de resposta",
    MEDIA_CAROUSEL: "Carrossel de mídia",
    LOCATION_REQUEST: "Solicitar localização",
    CONTEXTUAL_REPLY: "Respostas contextuais",
    LINK_PREVIEW: "Prévia de link",
    PAYMENT: "Cobrança",
    FLOW: "Formulário (Flow)",
}

FERRAMENTA = {
    CATALOG: "enviar_catalogo",
    PRODUCT_CAROUSEL: "enviar_carrossel_produtos",
    CTA_URL: "enviar_botao_link",
    LIST: "enviar_lista",
    REPLY_BUTTONS: "enviar_botoes_resposta",
    MEDIA_CAROUSEL: "enviar_carrossel_midia",
    LOCATION_REQUEST: "solicitar_localizacao",
    CONTEXTUAL_REPLY: "responder_em_contexto",
    LINK_PREVIEW: "enviar_previa_link",
    PAYMENT: "enviar_cobranca",
    FLOW: "enviar_flow",
}
FERRAMENTAS_DE_ENVIO = set(FERRAMENTA.values())


# ---------- helpers ----------


def _cortar(texto: str, limite: int) -> str:
    texto = (texto or "").strip()
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def listar(agent_info: dict, *tipos: str) -> list[dict]:
    return [f for f in agent_info.get("features") or [] if not tipos or f.get("type") in tipos]


def ativa(agent_info: dict, tipo: str) -> dict | None:
    return next(iter(listar(agent_info, tipo)), None)


def _por_id(agent_info: dict, funcionalidade_id: str, tipo: str) -> dict | None:
    funcionalidade_id = (funcionalidade_id or "").strip()
    candidatas = listar(agent_info, tipo)
    encontrada = next((f for f in candidatas if f["id"] == funcionalidade_id), None)
    # Uma só do tipo: aceita mesmo que o modelo erre o id.
    return encontrada or (candidatas[0] if len(candidatas) == 1 else None)


def _config(funcionalidade: dict) -> dict:
    return funcionalidade.get("config") or {}


def _esgotado(produto: dict) -> bool:
    return (produto.get("availability") or "").lower() in {"out of stock", "discontinued"}


def _erro(mensagem: str) -> dict:
    return {"ok": False, "erro": mensagem}


def _ok(envio: dict) -> dict:
    return {"ok": True, "mensagem": "Envio registrado: a sua resposta sai como texto desta mensagem.", "envio": envio}


def _ids_disponiveis(agent_info: dict, tipo: str) -> str:
    return ", ".join(f["id"] for f in listar(agent_info, tipo))


# ---------- instrução ----------

# O bloco é montado só com as regras que se aplicam às funcionalidades
# cadastradas no agente (ver build_funcionalidades_block).
REGRAS_INTRO = """
## Funcionalidades de mensagem

Além do texto, você pode usar as funcionalidades do WhatsApp abaixo, com as
ferramentas indicadas em cada uma. Use cada funcionalidade só na situação
descrita em "Quando usar" (definida pela empresa) e informe sempre o id dela.
"""

# Só com alguma funcionalidade de envio (respostas contextuais não contam).
REGRAS_ENVIO = """
- Resposta com envio sai numa mensagem só: todo o texto da sua resposta vai
  no corpo da mensagem da funcionalidade, nunca como mensagem separada antes
  dela. Escreva essa resposta curta, como o texto da própria funcionalidade,
  dentro do "Limite do texto" dela, e deixe o parâmetro texto vazio.
- Não repita por escrito o que já vai no envio (produtos, opções, links).
- No máximo um envio de cada funcionalidade por resposta.
"""

# Só quando o agente também tem metadados para coletar (ver
# agent._build_coleta_dados_block) — sem isso as duas instruções brigam.
REGRA_SEM_COLETA_NO_ENVIO = """- Resposta com envio de funcionalidade = resposta sem coleta de dados. Na
  mesma resposta em que chamar uma ferramenta de envio, NÃO faça nenhuma
  pergunta da "Coleta de dados obrigatória": nem no seu texto, nem no
  parâmetro texto da ferramenta. Essa resposta fala só do envio. Retome a
  coleta na próxima resposta que for só texto livre, sem funcionalidade.
"""


# Marcador que o Inbound-Service grava em Target.metadata quando um Flow com
# "salvar como metadados" é respondido: "flow_respondido_<id>" = data/hora
# (FLOW_ANSWERED_METADATA_PREFIX em interactive-reply-service.ts).
CHAVE_FLOW_RESPONDIDO = "flow_respondido_"

# Texto da resposta do Flow que o Inbound-Service manda pro agente:
# '📝 Respondeu o formulário "<nome>" (id: <id>)...'.
RE_FLOW_RESPONDIDO = re.compile(r'📝 Respondeu o formulário ".*?" \(id: ([^\s)]+)\)')


def chave_flow_respondido(flow_id: str) -> str:
    return f"{CHAVE_FLOW_RESPONDIDO}{flow_id}"


def flows_respondidos_no_texto(texto: str) -> set[str]:
    """Ids dos Flows respondidos nas mensagens deste turno."""
    return set(RE_FLOW_RESPONDIDO.findall(texto or ""))


def flows_da_coleta(agent_info: dict) -> list[dict]:
    """Flows com "salvar como metadados" ligado: entram na "Coleta de dados
    obrigatória" (ver agent._build_coleta_dados_block), não no bloco de
    funcionalidades."""
    return [f for f in listar(agent_info, FLOW) if _config(f).get("saveAsMetadata")]


def _de_midia(agent_info: dict) -> list[dict]:
    """Funcionalidades do bloco "Funcionalidades de mensagem": todas menos os
    Flows da coleta."""
    da_coleta = {f["id"] for f in flows_da_coleta(agent_info)}
    return [f for f in listar(agent_info) if f["id"] not in da_coleta]


def descrever_flow_coleta(flow: dict) -> str:
    config = _config(flow)
    linha = (
        f'- {flow.get("name") or ROTULO[FLOW]} (id: {flow["id"]}): abre o formulário '
        f'"{config.get("flowName")}" pelo botão "{config.get("ctaText")}".'
    )
    if (flow.get("prompt") or "").strip():
        linha += f" Quando usar: {flow['prompt'].strip()}"
    return f"{linha} {_linha_limite(FLOW)}"


def tem_envio(agent_info: dict) -> bool:
    """Há alguma funcionalidade que envia mensagem especial (respostas
    contextuais só citam a mensagem, não contam)."""
    return any(f.get("type") != CONTEXTUAL_REPLY for f in listar(agent_info))


# Anexada à "Coleta de dados obrigatória" quando o agente tem funcionalidades
# de envio — sem isso as duas instruções brigam (a coleta manda perguntar).
COLETA_SEM_FUNCIONALIDADE = """
Faça as perguntas desta coleta só em respostas de texto livre. Nunca pergunte
nenhum destes dados na mesma resposta em que enviar um formulário ou outra
funcionalidade de mensagem: deixe para a próxima resposta.
"""


def _build_regras(funcionalidades: list[dict], tem_coleta: bool) -> str:
    tipos = list(dict.fromkeys(f.get("type") for f in funcionalidades))
    partes = [REGRAS_INTRO]
    if any(t != CONTEXTUAL_REPLY for t in tipos):
        partes.append(REGRAS_ENVIO)
        if tem_coleta:
            partes.append(REGRA_SEM_COLETA_NO_ENVIO)
    return "".join(partes)


def _detalhes(funcionalidade: dict) -> list[str]:
    tipo = funcionalidade["type"]
    config = _config(funcionalidade)
    linhas: list[str] = []

    if tipo in (CATALOG, PRODUCT_CAROUSEL):
        produtos = funcionalidade.get("products") or []
        if tipo == PRODUCT_CAROUSEL and config.get("source") == "manual":
            linhas.append("Produtos fixos (definidos pela empresa); não precisa informar SKUs.")
        elif tipo == PRODUCT_CAROUSEL and config.get("source") == "integration":
            linhas.append("Produtos vêm do sistema da empresa; não precisa informar SKUs.")
        elif produtos:
            linhas.append("Produtos (SKU | nome | preço):")
            linhas += [
                f"- {p['retailerId']} | {p.get('name') or p['retailerId']}"
                f"{' | ' + p['price'] if p.get('price') else ''}{' | ESGOTADO' if _esgotado(p) else ''}"
                for p in produtos
            ]
        else:
            linhas.append(
                'Lista de produtos indisponível agora: use só tipo "catalogo_completo".'
                if tipo == CATALOG
                else "Lista de produtos indisponível agora: não use este carrossel."
            )
        if tipo == CATALOG:
            linhas.append(
                'Tipos: "catalogo_completo" (catálogo inteiro) ou "produtos" (1 a 30 SKUs da lista). '
                "Nunca invente SKU nem ofereça esgotados."
            )
        elif config.get("source", "agent") == "agent":
            linhas.append("Escolha de 2 a 10 SKUs da lista.")
    elif tipo == CTA_URL:
        linhas.append(f'Botão "{config.get("displayText")}" abre: {config.get("url")}')
    elif tipo == LIST:
        opcoes = [r["title"] for s in config.get("sections") or [] for r in s.get("rows") or []]
        linhas.append(f"Opções da lista: {', '.join(opcoes)}")
    elif tipo == REPLY_BUTTONS:
        if config.get("mode") == "fixed":
            linhas.append(f"Botões permitidos (use só estes, até 3): {', '.join(config.get('buttons') or [])}")
        else:
            linhas.append("Você cria os botões: de 1 a 3, títulos curtos (até 20 caracteres) e diferentes entre si.")
        if (config.get("rules") or "").strip():
            linhas.append(f"Regras da empresa para os botões: {config['rules'].strip()}")
    elif tipo == MEDIA_CAROUSEL:
        if config.get("source") == "integration":
            linhas.append("Os cards vêm do sistema da empresa.")
        else:
            titulos = [c.get("bodyText") or c.get("buttonText") for c in config.get("cards") or []]
            linhas.append(f"Cards fixos: {', '.join(t for t in titulos if t)}")
    elif tipo == LOCATION_REQUEST:
        linhas.append('A localização enviada fica salva no metadado "localization" do contato.')
    elif tipo == LINK_PREVIEW:
        linhas.append(f"Link: {config.get('url')}")
    elif tipo == CONTEXTUAL_REPLY:
        linhas.append(
            'As mensagens do contato chegam numeradas ("[mensagem 1] ..."). Quando responder a uma '
            "mensagem específica entre várias, chame responder_em_contexto "
            "com o número dela: sua resposta sai citando essa mensagem."
        )
    elif tipo == FLOW:
        # Os de "salvar como metadados" ficam na coleta (descrever_flow_coleta).
        linhas.append(f'Abre o formulário "{config.get("flowName")}" pelo botão "{config.get("ctaText")}".')
        linhas.append("Quando o contato enviar, as respostas chegam como mensagem dele.")
    elif tipo == PAYMENT:
        metodos = ", ".join(config.get("methods") or [])
        linhas.append(f"Métodos aceitos: {metodos}. Informe o valor total em centavos (ex: R$ 12,34 = 1234).")
    return linhas


def build_funcionalidades_block(agent_info: dict, tem_coleta: bool) -> str:
    """Só as funcionalidades cadastradas no agente (agent.features — FLOW já
    chega filtrado pelo WABA/status no Inbound-Service), sem os Flows da
    coleta. Sem nenhuma, o bloco não entra no prompt."""
    funcionalidades = _de_midia(agent_info)
    if not funcionalidades:
        return ""
    partes = [_build_regras(funcionalidades, tem_coleta)]
    for f in funcionalidades:
        partes.append(f"### {f.get('name') or ROTULO.get(f['type'], f['type'])} (id: {f['id']})")
        partes.append(f"Tipo: {ROTULO.get(f['type'], f['type'])} | ferramenta: {FERRAMENTA.get(f['type'])}")
        if _linha_limite(f["type"]):
            partes.append(_linha_limite(f["type"]))
        if (f.get("prompt") or "").strip():
            partes.append(f"Quando usar: {f['prompt'].strip()}")
        partes += _detalhes(f)
        partes.append("")
    return "\n".join(partes)


# ---------- ferramentas ----------


def build_tools(agent_info: dict, target_info: dict | None = None) -> list:
    tipos = {f.get("type") for f in listar(agent_info)}
    contexto = {"agent_id": agent_info.get("id"), "target": target_info or {}}
    tools = []

    if CATALOG in tipos:

        def enviar_catalogo(funcionalidade_id: str, tipo: str, skus: list[str], texto: str) -> dict:
            """Envia um catálogo de produtos da empresa no WhatsApp.

            Args:
                funcionalidade_id: id da funcionalidade de catálogo.
                tipo: "catalogo_completo" (catálogo inteiro) ou "produtos" (só os SKUs).
                skus: SKUs da lista do catálogo (1 a 30 no tipo "produtos"; no
                    completo, opcional: o primeiro vira a foto de capa).
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, CATALOG)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids de catálogo: {_ids_disponiveis(agent_info, CATALOG)}")
            conhecidos = {p["retailerId"] for p in f.get("products") or []}
            validos = list(dict.fromkeys(str(s).strip() for s in skus or [] if str(s).strip() in conhecidos))
            if tipo == "catalogo_completo":
                return _ok({"tipo": CATALOG, "id": f["id"], "modo": tipo, "skus": validos[:1], "texto": texto or ""})
            if tipo == "produtos" and validos:
                return _ok({"tipo": CATALOG, "id": f["id"], "modo": tipo, "skus": validos[:MAX_PRODUTOS_LISTA], "texto": texto or ""})
            return _erro('Use tipo "catalogo_completo" ou "produtos" com SKUs da lista do catálogo.')

        tools.append(enviar_catalogo)

    if PRODUCT_CAROUSEL in tipos:

        def enviar_carrossel_produtos(funcionalidade_id: str, skus: list[str], texto: str) -> dict:
            """Envia um carrossel de produtos (2 a 10 cards com foto, nome e preço do catálogo).

            Args:
                funcionalidade_id: id da funcionalidade de carrossel de produtos.
                skus: 2 a 10 SKUs da lista (ignorado quando os produtos são fixos ou vêm do sistema da empresa).
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, PRODUCT_CAROUSEL)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids: {_ids_disponiveis(agent_info, PRODUCT_CAROUSEL)}")
            config = _config(f)
            origem = config.get("source") or "agent"
            if origem == "manual":
                escolhidos = list(config.get("productIds") or [])
            elif origem == "integration":
                escolhidos = list(integracoes.carregar_produtos_carrossel(f, contexto) or [])
                if not escolhidos:
                    return _erro("A integração do carrossel com o sistema da empresa não está configurada.")
            else:
                conhecidos = {p["retailerId"] for p in f.get("products") or []}
                escolhidos = [s for s in dict.fromkeys(str(s).strip() for s in skus or []) if s in conhecidos]
            escolhidos = escolhidos[:MAX_CARDS]
            if len(escolhidos) < MIN_CARDS:
                return _erro("O carrossel precisa de 2 a 10 SKUs válidos da lista.")
            return _ok({"tipo": PRODUCT_CAROUSEL, "id": f["id"], "skus": escolhidos, "texto": texto or ""})

        tools.append(enviar_carrossel_produtos)

    if CTA_URL in tipos:

        def enviar_botao_link(funcionalidade_id: str, texto: str) -> dict:
            """Envia uma mensagem com um botão que abre um link.

            Args:
                funcionalidade_id: id da funcionalidade de botão de link.
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, CTA_URL)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids: {_ids_disponiveis(agent_info, CTA_URL)}")
            return _ok({"tipo": CTA_URL, "id": f["id"], "texto": texto or ""})

        tools.append(enviar_botao_link)

    if LIST in tipos:

        def enviar_lista(funcionalidade_id: str, texto: str) -> dict:
            """Envia uma lista de opções (menu) para o contato escolher.

            Args:
                funcionalidade_id: id da funcionalidade de lista.
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, LIST)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids: {_ids_disponiveis(agent_info, LIST)}")
            return _ok({"tipo": LIST, "id": f["id"], "texto": texto or ""})

        tools.append(enviar_lista)

    if REPLY_BUTTONS in tipos:

        def enviar_botoes_resposta(funcionalidade_id: str, botoes: list[str], texto: str) -> dict:
            """Envia uma mensagem com até 3 botões de resposta rápida.

            Args:
                funcionalidade_id: id da funcionalidade de botões de resposta.
                botoes: títulos dos botões (1 a 3, até 20 caracteres cada).
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, REPLY_BUTTONS)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids: {_ids_disponiveis(agent_info, REPLY_BUTTONS)}")
            config = _config(f)
            titulos = [t for t in dict.fromkeys(str(b).strip() for b in botoes or []) if t]
            if config.get("mode") == "fixed":
                permitidos = config.get("buttons") or []
                por_nome = {p.lower(): p for p in permitidos}
                titulos = [por_nome[t.lower()] for t in titulos if t.lower() in por_nome] or list(permitidos)
            titulos = [_cortar(t, MAX_BOTAO) for t in titulos][:MAX_BOTOES_RESPOSTA]
            if not titulos:
                return _erro("Informe de 1 a 3 botões.")
            if len({t.lower() for t in titulos}) != len(titulos):
                return _erro("Os botões precisam ter títulos diferentes.")
            return _ok({"tipo": REPLY_BUTTONS, "id": f["id"], "botoes": titulos, "texto": texto or ""})

        tools.append(enviar_botoes_resposta)

    if MEDIA_CAROUSEL in tipos:

        def enviar_carrossel_midia(funcionalidade_id: str, texto: str) -> dict:
            """Envia um carrossel de cards com imagem/vídeo e botão de link.

            Args:
                funcionalidade_id: id da funcionalidade de carrossel de mídia.
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, MEDIA_CAROUSEL)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids: {_ids_disponiveis(agent_info, MEDIA_CAROUSEL)}")
            config = _config(f)
            if config.get("source") == "integration":
                cards = list(integracoes.carregar_cards_carrossel_midia(f, contexto) or [])
                if not cards:
                    return _erro("A integração do carrossel com o sistema da empresa não está configurada.")
            else:
                cards = list(config.get("cards") or [])
            cards = cards[:MAX_CARDS]
            if len(cards) < MIN_CARDS:
                return _erro("O carrossel precisa de 2 a 10 cards.")
            return _ok({"tipo": MEDIA_CAROUSEL, "id": f["id"], "cards": cards, "texto": texto or ""})

        tools.append(enviar_carrossel_midia)

    if LOCATION_REQUEST in tipos:

        def solicitar_localizacao(texto: str) -> dict:
            """Pede a localização do contato com o botão "Enviar localização" do WhatsApp.

            Args:
                texto: texto do pedido; vazio = usa a sua resposta.
            """
            f = ativa(agent_info, LOCATION_REQUEST)
            return _ok({"tipo": LOCATION_REQUEST, "id": f["id"], "texto": texto or ""})

        tools.append(solicitar_localizacao)

    if LINK_PREVIEW in tipos:

        def enviar_previa_link(funcionalidade_id: str, texto: str) -> dict:
            """Envia um link com prévia (título e imagem do site).

            Args:
                funcionalidade_id: id da funcionalidade de prévia de link.
                texto: texto que acompanha o link; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, LINK_PREVIEW)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids: {_ids_disponiveis(agent_info, LINK_PREVIEW)}")
            return _ok({"tipo": LINK_PREVIEW, "id": f["id"], "texto": texto or ""})

        tools.append(enviar_previa_link)

    if FLOW in tipos:

        def enviar_flow(funcionalidade_id: str, texto: str) -> dict:
            """Envia um formulário do WhatsApp (Flow), aberto por um botão na mensagem.

            Args:
                funcionalidade_id: id da funcionalidade de formulário.
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = _por_id(agent_info, funcionalidade_id, FLOW)
            if not f:
                return _erro(f"Funcionalidade não encontrada. Ids: {_ids_disponiveis(agent_info, FLOW)}")
            return _ok({"tipo": FLOW, "id": f["id"], "texto": texto or ""})

        tools.append(enviar_flow)

    if CONTEXTUAL_REPLY in tipos:

        def responder_em_contexto(numero_mensagem: int) -> dict:
            """Faz a sua resposta sair citando uma mensagem do contato.

            Args:
                numero_mensagem: número da mensagem ("[mensagem N]") que você está respondendo.
            """
            if not isinstance(numero_mensagem, int) or numero_mensagem < 1:
                return _erro("Informe o número da mensagem (1, 2, 3...).")
            return _ok({"tipo": CONTEXTUAL_REPLY, "numero": numero_mensagem})

        tools.append(responder_em_contexto)

    if PAYMENT in tipos:

        def enviar_cobranca(metodo: str, valor_centavos: int, descricao: str, texto: str) -> dict:
            """Envia uma cobrança no WhatsApp (Pix, link de pagamento ou boleto).

            Args:
                metodo: "pix", "payment_link" ou "boleto".
                valor_centavos: valor total em centavos (R$ 12,34 = 1234).
                descricao: o que está sendo cobrado (até 60 caracteres).
                texto: texto da mensagem; vazio = usa a sua resposta.
            """
            f = ativa(agent_info, PAYMENT)
            config = _config(f)
            if metodo not in (config.get("methods") or []):
                return _erro(f"Método não habilitado. Use: {', '.join(config.get('methods') or [])}.")
            if not isinstance(valor_centavos, int) or valor_centavos <= 0:
                return _erro("Informe o valor em centavos (inteiro maior que zero).")
            dados = integracoes.gerar_cobranca(f, metodo, valor_centavos, descricao or "", contexto)
            if not dados:
                return _erro("A integração de pagamento com o sistema da empresa não está configurada.")
            return _ok(
                {
                    "tipo": PAYMENT,
                    "id": f["id"],
                    "metodo": metodo,
                    "valor_centavos": valor_centavos,
                    "descricao": descricao or "",
                    "dados": dados,
                    "texto": texto or "",
                }
            )

        tools.append(enviar_cobranca)

    return tools


# ---------- montagem das mensagens ----------


def _interactive_catalogo(f: dict, envio: dict, texto: str) -> tuple[dict, str]:
    catalog_id = _config(f)["catalogId"]
    nomes = {p["retailerId"]: p.get("name") or p["retailerId"] for p in f.get("products") or []}
    skus = envio.get("skus") or []
    texto = _cortar(texto or TEXTO_PADRAO[CATALOG], MAX_BODY)

    if envio.get("modo") == "catalogo_completo":
        capa = skus[0] if skus else next((p["retailerId"] for p in f.get("products") or [] if not _esgotado(p)), None)
        action: dict = {"name": "catalog_message"}
        if capa:
            action["parameters"] = {"thumbnail_product_retailer_id": capa}
        return {"type": "catalog_message", "body": {"text": texto}, "action": action}, f"🛍️ Catálogo enviado: {texto}"

    resumo = f"🛍️ Produtos enviados: {', '.join(nomes.get(s, s) for s in skus)}"
    if len(skus) == 1:
        return {
            "type": "product",
            "body": {"text": texto},
            "action": {"catalog_id": catalog_id, "product_retailer_id": skus[0]},
        }, resumo
    titulo = f.get("name") or _config(f).get("catalogName") or "Produtos"
    return {
        "type": "product_list",
        "header": {"type": "text", "text": _cortar(titulo, MAX_HEADER)},
        "body": {"text": texto},
        "action": {
            "catalog_id": catalog_id,
            "sections": [
                {"title": _cortar(titulo, MAX_TITULO_SECAO), "product_items": [{"product_retailer_id": s} for s in skus]}
            ],
        },
    }, resumo


def _interactive_carrossel_produtos(f: dict, envio: dict, texto: str) -> tuple[dict, str]:
    catalog_id = _config(f)["catalogId"]
    nomes = {p["retailerId"]: p.get("name") or p["retailerId"] for p in f.get("products") or []}
    skus = envio["skus"]
    return {
        "type": "carousel",
        "body": {"text": _cortar(texto or TEXTO_PADRAO[PRODUCT_CAROUSEL], MAX_BODY)},
        "action": {
            "cards": [
                {
                    "card_index": i,
                    "type": "product",
                    "action": {"product_retailer_id": sku, "catalog_id": catalog_id},
                }
                for i, sku in enumerate(skus)
            ]
        },
    }, f"🛍️ Carrossel de produtos enviado: {', '.join(nomes.get(s, s) for s in skus)}"


def _interactive_carrossel_midia(envio: dict, texto: str) -> tuple[dict, str]:
    cards = []
    for i, card in enumerate(envio["cards"]):
        tipo_midia = "video" if card.get("mediaType") == "video" else "image"
        item = {
            "card_index": i,
            "type": "cta_url",
            "header": {"type": tipo_midia, tipo_midia: {"link": card["mediaUrl"]}},
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": _cortar(card["buttonText"], MAX_BOTAO), "url": card["url"]},
            },
        }
        if (card.get("bodyText") or "").strip():
            item["body"] = {"text": _cortar(card["bodyText"], MAX_CARD_BODY)}
        cards.append(item)
    titulos = [c.get("bodyText") or c.get("buttonText") for c in envio["cards"]]
    return {
        "type": "carousel",
        "body": {"text": _cortar(texto or TEXTO_PADRAO[MEDIA_CAROUSEL], MAX_BODY)},
        "action": {"cards": cards},
    }, f"🖼️ Carrossel enviado: {', '.join(t for t in titulos if t)}"


def _interactive_cta(f: dict, texto: str) -> tuple[dict, str]:
    config = _config(f)
    interactive: dict = {
        "type": "cta_url",
        "body": {"text": _cortar(texto or TEXTO_PADRAO[CTA_URL], MAX_BODY)},
        "action": {
            "name": "cta_url",
            "parameters": {"display_text": _cortar(config["displayText"], MAX_BOTAO), "url": config["url"]},
        },
    }
    if config.get("headerText"):
        interactive["header"] = {"type": "text", "text": _cortar(config["headerText"], MAX_HEADER)}
    if config.get("footerText"):
        interactive["footer"] = {"text": _cortar(config["footerText"], MAX_FOOTER)}
    return interactive, f"{interactive['body']['text']}\n🔗 [{config['displayText']}] {config['url']}"


def _interactive_lista(f: dict, texto: str) -> tuple[dict, str]:
    config = _config(f)
    secoes = [
        {
            "title": _cortar(s["title"], MAX_TITULO_SECAO),
            "rows": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    **({"description": r["description"]} if r.get("description") else {}),
                }
                for r in s["rows"]
            ],
        }
        for s in config.get("sections") or []
    ]
    interactive: dict = {
        "type": "list",
        "body": {"text": _cortar(texto or TEXTO_PADRAO[LIST], MAX_BODY)},
        "action": {"button": _cortar(config["buttonText"], MAX_BOTAO), "sections": secoes},
    }
    if config.get("headerText"):
        interactive["header"] = {"type": "text", "text": _cortar(config["headerText"], MAX_HEADER)}
    if config.get("footerText"):
        interactive["footer"] = {"text": _cortar(config["footerText"], MAX_FOOTER)}
    opcoes = [r["title"] for s in secoes for r in s["rows"]]
    return interactive, f"{interactive['body']['text']}\n📋 [{config['buttonText']}] {', '.join(opcoes)}"


def _slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")[:40] or uuid.uuid4().hex[:8]


def _interactive_botoes(envio: dict, texto: str) -> tuple[dict, str]:
    botoes = [
        {"type": "reply", "reply": {"id": f"btn_{i + 1}_{_slug(t)}", "title": t}} for i, t in enumerate(envio["botoes"])
    ]
    corpo = _cortar(texto or TEXTO_PADRAO[REPLY_BUTTONS], MAX_BODY)
    return {"type": "button", "body": {"text": corpo}, "action": {"buttons": botoes}}, (
        f"{corpo}\n🔘 {' | '.join(envio['botoes'])}"
    )


def _interactive_localizacao(f: dict, texto: str) -> tuple[dict, str]:
    corpo = _cortar(texto or _config(f).get("bodyText") or TEXTO_PADRAO[LOCATION_REQUEST], MAX_BODY)
    return {"type": "location_request_message", "body": {"text": corpo}, "action": {"name": "send_location"}}, (
        f"{corpo}\n📍 [Enviar localização]"
    )


def _interactive_flow(f: dict, texto: str) -> tuple[dict, str]:
    """Flow: flow_token leva o id da funcionalidade, pro Inbound-Service saber
    se salva as respostas como metadados. Rascunho sai em modo draft."""
    config = _config(f)
    parametros: dict = {
        "flow_message_version": "3",
        "flow_token": f"sturnus:{f['id']}:{uuid.uuid4().hex[:12]}",
        "flow_id": config["flowId"],
        "flow_cta": _cortar(config["ctaText"], MAX_BOTAO_FLOW),
        "flow_action": "navigate",
    }
    if config.get("firstScreen"):
        parametros["flow_action_payload"] = {"screen": config["firstScreen"]}
    if config.get("status") == "DRAFT":
        parametros["mode"] = "draft"
    interactive: dict = {
        "type": "flow",
        "body": {"text": _cortar(texto or config.get("bodyText") or TEXTO_PADRAO[FLOW], MAX_BODY)},
        "action": {"name": "flow", "parameters": parametros},
    }
    if config.get("headerText"):
        interactive["header"] = {"type": "text", "text": _cortar(config["headerText"], MAX_HEADER)}
    if config.get("footerText"):
        interactive["footer"] = {"text": _cortar(config["footerText"], MAX_FOOTER)}
    return interactive, f"{interactive['body']['text']}\n📝 [{config['ctaText']}] {config.get('flowName') or 'Formulário'}"


def montar_order_details(
    config: dict,
    metodo: str,
    valor_centavos: int,
    descricao: str,
    dados: dict,
    texto: str,
    referencia: str | None = None,
) -> dict:
    """Mensagem de cobrança (order_details / review_and_pay, Brasil). Valores
    em centavos com offset 100; total = subtotal (sem imposto/frete aqui)."""
    referencia = re.sub(r"[^A-Za-z0-9_.-]", "", referencia or dados.get("referencia") or uuid.uuid4().hex)[:60]
    if metodo == "pix":
        pagamento = {
            "type": "pix_dynamic_code",
            "pix_dynamic_code": {
                "code": dados["codigo"],
                "merchant_name": dados.get("recebedor") or config.get("merchantName") or "",
                "key": dados.get("chave") or config.get("pixKey") or "",
                "key_type": dados.get("tipo_chave") or config.get("pixKeyType") or "EVP",
            },
        }
    elif metodo == "payment_link":
        pagamento = {"type": "payment_link", "payment_link": {"uri": dados["link"]}}
    else:
        pagamento = {"type": "boleto", "boleto": {"digitable_line": dados["linha_digitavel"]}}

    valor = {"value": valor_centavos, "offset": 100}
    return {
        "type": "order_details",
        "body": {"text": _cortar(texto or TEXTO_PADRAO[PAYMENT], MAX_BODY)},
        "action": {
            "name": "review_and_pay",
            "parameters": {
                "reference_id": referencia,
                "type": config.get("goodsType") or "digital-goods",
                "payment_type": "br",
                "payment_settings": [pagamento],
                "currency": "BRL",
                "total_amount": valor,
                "order": {
                    "status": "pending",
                    "tax": {"value": 0, "offset": 100},
                    "items": [
                        {
                            "retailer_id": referencia,
                            "name": _cortar(descricao or "Pedido", 60),
                            "amount": valor,
                            "quantity": 1,
                        }
                    ],
                    "subtotal": valor,
                },
            },
        },
    }


def _formatar_reais(centavos: int) -> str:
    return f"R$ {centavos / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _juntar_corpo(partes: list[str], texto: str) -> str:
    """Resposta + parâmetro texto da ferramenta, sem repetir quando um já
    contém o outro."""
    resposta = "\n\n".join(partes)
    if not texto or texto in resposta:
        return resposta
    if resposta in texto:
        return texto
    return f"{resposta}\n\n{texto}"


def montar_itens(agent_info: dict, partes_texto: list[str], envios: list[dict], ids_mensagens: list[str]) -> list[dict]:
    """Junta o texto da resposta (já quebrado pelo RGBS, se ligado) com os
    envios das ferramentas, na ordem em que saem. Item = {"text"} ou
    {"text", "interactive"} ou {"text", "previewUrl"}; o primeiro pode levar
    "contextMessageId" (resposta em contexto). Texto do item interativo é o
    resumo que fica no histórico."""
    partes = [p for p in partes_texto if p.strip()]
    contexto_id = None
    itens_envio: list[dict] = []

    for envio in envios:
        tipo = envio.get("tipo")
        if tipo == CONTEXTUAL_REPLY:
            numero = envio.get("numero") or 0
            if 1 <= numero <= len(ids_mensagens):
                contexto_id = ids_mensagens[numero - 1]
            continue

        f = next((x for x in listar(agent_info, tipo) if x["id"] == envio.get("id")), None)
        if not f:
            continue
        texto = envio.get("texto") or ""
        # Regra geral: a resposta inteira (todas as partes do RGBS) vira o
        # corpo do primeiro envio. Sai só a mensagem da funcionalidade, nunca
        # um texto e depois o envio.
        if tipo in TIPOS_COM_CORPO and partes:
            texto = _juntar_corpo(partes, texto)
            partes = []

        if tipo == CATALOG:
            interactive, resumo = _interactive_catalogo(f, envio, texto)
        elif tipo == PRODUCT_CAROUSEL:
            interactive, resumo = _interactive_carrossel_produtos(f, envio, texto)
        elif tipo == MEDIA_CAROUSEL:
            interactive, resumo = _interactive_carrossel_midia(envio, texto)
        elif tipo == CTA_URL:
            interactive, resumo = _interactive_cta(f, texto)
        elif tipo == LIST:
            interactive, resumo = _interactive_lista(f, texto)
        elif tipo == REPLY_BUTTONS:
            interactive, resumo = _interactive_botoes(envio, texto)
        elif tipo == LOCATION_REQUEST:
            interactive, resumo = _interactive_localizacao(f, texto)
        elif tipo == FLOW:
            interactive, resumo = _interactive_flow(f, texto)
        elif tipo == LINK_PREVIEW:
            url = _config(f)["url"]
            corpo = f"{_cortar(texto, MAX_TEXTO - len(url) - 2)}\n\n{url}" if texto else url
            itens_envio.append({"text": corpo, "previewUrl": True})
            continue
        elif tipo == PAYMENT:
            interactive = montar_order_details(
                _config(f), envio["metodo"], envio["valor_centavos"], envio["descricao"], envio["dados"], texto
            )
            resumo = f"💳 Cobrança enviada ({envio['metodo']}): {_formatar_reais(envio['valor_centavos'])} - {envio['descricao']}"
        else:
            continue
        itens_envio.append({"text": resumo, "interactive": interactive})

    itens = [{"text": p} for p in partes] + itens_envio
    if contexto_id and itens:
        itens[0]["contextMessageId"] = contexto_id
    return itens


def numerar_mensagens(agent_info: dict, textos: list[str]) -> str:
    """Pergunta enviada ao agente: com respostas contextuais ligadas, cada
    mensagem do lote vai numerada pra ele poder citar uma delas."""
    if not ativa(agent_info, CONTEXTUAL_REPLY):
        return "\n".join(textos).strip()
    return "\n".join(f"[mensagem {i + 1}] {t}" for i, t in enumerate(textos)).strip()
