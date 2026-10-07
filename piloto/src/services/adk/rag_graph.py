from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from src.infra.agent_api.client import registrar_tokens
from src.infra.pgvector.connection import contar_tokens_embedding, get_vector_store


class RagState(TypedDict):
    pergunta: str  # pergunta original do usuário, usada como query na busca vetorial
    agent_id: str  # id do agente, usado para filtrar a busca só no conhecimento dele
    openai_api_key: str | None  # api key usada para gerar o embedding da busca (pode vir None)
    trechos: list[str]  # lista de textos retornados pela busca vetorial (preenchido em _retrieve)
    contexto: str  # trechos já concatenados em um único texto (preenchido em _compile_context)


def _retrieve(state: RagState) -> dict:
    vector_store = get_vector_store(api_key=state.get("openai_api_key"))
    resultados = vector_store.similarity_search(
        query=state["pergunta"],
        k=4, # quantidade de topicos que vai puxar do rag
        filter={"agent_id": state["agent_id"]},
    )
    registrar_tokens(state["agent_id"], "OPENAI", contar_tokens_embedding([state["pergunta"]]))
    return {"trechos": [doc.page_content for doc in resultados]}


def _compile_context(state: RagState) -> dict:
    contexto = "\n\n---\n\n".join(state["trechos"]) if state["trechos"] else ""
    return {"contexto": contexto}


# Grafo pequeno de propósito — retrieve (busca vetorial no pgvector, filtrada
# por agent_id) → compile_context (junta os trechos num texto só). Compilado
# uma vez no import, reaproveitado a cada chamada da tool consultar_conhecimento.

_graph = (
    StateGraph(RagState)  # cria o builder do grafo, tipado com o RagState acima
    .add_node("retrieve", _retrieve)  # registra o nó "retrieve": roda a busca vetorial e preenche "trechos"
    .add_node("compile_context", _compile_context)  # registra o nó "compile_context": junta "trechos" em "contexto"
    .add_edge(START, "retrieve")  # início do grafo sempre entra pelo nó "retrieve"
    .add_edge("retrieve", "compile_context")  # depois de buscar, segue para compilar o contexto
    .add_edge("compile_context", END)  # depois de compilar, o grafo termina
    .compile()  # compila o grafo em um objeto executável (invoke/ainvoke)
)


async def consultar_base_de_conhecimento(pergunta: str, agent_id: str, openai_api_key: str | None = None) -> str:
    resultado = await _graph.ainvoke(
        {"pergunta": pergunta, "agent_id": agent_id, "openai_api_key": openai_api_key, "trechos": [], "contexto": ""}
    )
    return resultado.get("contexto", "")
