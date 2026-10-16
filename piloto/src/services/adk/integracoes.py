"""
Pontos de integração com o sistema do cliente — usados pelas funcionalidades
configuradas com origem "integration" no Agent Console (carrosséis) e pela
cobrança (PAYMENT, só backend). Por padrão não há integração: as funções
devolvem vazio/None e a ferramenta responde ao agente que a integração não
está configurada.

Para ligar um cliente, implemente aqui a busca no sistema dele (API, banco
etc.) usando os dados da funcionalidade (`funcionalidade["config"]`,
`funcionalidade["id"]`, `funcionalidade["name"]`) e do contexto da conversa.

`contexto` sempre traz:
    {"agent_id": str, "target": {"id", "waId", "name", "metadata"}}
"""


def carregar_cards_carrossel_midia(funcionalidade: dict, contexto: dict) -> list[dict]:
    """Cards do carrossel de mídia vindos do sistema do cliente.

    Cada card: {"mediaType": "image" | "video", "mediaUrl": str (URL pública),
    "bodyText": str (até 160), "buttonText": str (até 20), "url": str}.
    Precisa devolver de 2 a 10 cards; vazio = integração não configurada."""
    return []


def carregar_produtos_carrossel(funcionalidade: dict, contexto: dict) -> list[str]:
    """SKUs (product_retailer_id) do carrossel de produtos vindos do sistema
    do cliente — todos do catálogo da funcionalidade (config.catalogId).
    Precisa devolver de 2 a 10; vazio = integração não configurada."""
    return []


def gerar_cobranca(funcionalidade: dict, metodo: str, valor_centavos: int, descricao: str, contexto: dict) -> dict | None:
    """Gera a cobrança no PSP do cliente e devolve os dados do método:

    - "pix": {"codigo": str (Pix copia e cola dinâmico)} — chave/tipo/nome do
      recebedor vêm de config (pixKey, pixKeyType, merchantName), mas podem
      ser sobrescritos aqui com "chave", "tipo_chave", "recebedor".
    - "payment_link": {"link": str}
    - "boleto": {"linha_digitavel": str}

    Pode incluir "referencia" (id do pedido no sistema do cliente; até 60
    caracteres: letras, números, "_", "-" e "."). None = sem integração."""
    return None
