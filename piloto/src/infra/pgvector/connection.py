import os

import tiktoken
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector

load_dotenv()

DATABASE_URL = os.getenv("URL_PGVECTOR")

# Cacheado por api_key (mesmo padrão do _get_openai_client em agent_api/client.py)
# — o token normalmente vem do payload da mensagem (Agent Console, por agente),
# com fallback pro env quando o agente ainda não tem um configurado.
_embeddings_cache: dict[str | None, OpenAIEmbeddings] = {}


def get_embeddings(api_key: str | None = None) -> OpenAIEmbeddings:
    key = api_key or os.getenv("OPENAI_API_KEY")
    if key not in _embeddings_cache:
        _embeddings_cache[key] = OpenAIEmbeddings(model="text-embedding-3-small", api_key=key)
    return _embeddings_cache[key]


_EMBEDDING_ENCODING = None


def contar_tokens_embedding(textos: list[str]) -> int:
    """Tokens que a OpenAI cobra pra gerar o embedding desses textos — o
    OpenAIEmbeddings do langchain não devolve o `usage` da resposta, então a
    contagem é feita localmente com o mesmo tokenizer do modelo
    (text-embedding-3-small → cl100k_base). 0 se o tokenizer falhar: a
    métrica de consumo é best-effort, nunca pode derrubar busca/ingestão."""
    global _EMBEDDING_ENCODING
    try:
        if _EMBEDDING_ENCODING is None:
            _EMBEDDING_ENCODING = tiktoken.encoding_for_model("text-embedding-3-small")
        return sum(len(_EMBEDDING_ENCODING.encode(texto)) for texto in textos)
    except Exception as e:
        print(f"[julia] Falha ao contar tokens de embedding: {e}")
        return 0


# Coleção única compartilhada por TODOS os agentes genéricos (diferente do
# atlas, que tem uma coleção por agente) — como o mesmo processo "max" atende
# qualquer agente/organização através da fila task.agent.generic.create
# (nenhum worker dedicado por nome), não dá pra nomear a coleção pelo nome do
# agente (colidiria entre organizações). Todo chunk carrega "agent_id" no
# metadata e toda busca filtra por ele.
GENERIC_AGENTS_COLLECTION = "documentos_agentes_generic"


def get_vector_store(collection_name: str = GENERIC_AGENTS_COLLECTION, api_key: str | None = None) -> PGVector:
    return PGVector(
        embeddings=get_embeddings(api_key),
        collection_name=collection_name,
        connection=DATABASE_URL,
        use_jsonb=True,
    )
