import io
import os
import tempfile
import traceback

import docx2txt
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from src.infra.agent_api.client import registrar_tokens, update_rag_document_status
from src.infra.pgvector.connection import contar_tokens_embedding, get_vector_store
from src.infra.s3.client import download_object

# unstructured.partition.auto seria a opção "genérica" (detecta o formato
# sozinho), mas seu import trava/segfaulta neste ambiente (conflito binário
# na cadeia spacy/thinc/blis que ele puxa). pypdf/docx2txt já eram
# dependências (herdadas do axel) e cobrem os 3 formatos aceitos no upload
# sem essa cadeia problemática.


def _extract_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(file_bytes: bytes) -> str:
    # docx2txt só aceita um caminho de arquivo, não bytes direto.
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        return docx2txt.process(tmp_path) or ""
    finally:
        os.unlink(tmp_path)


def _extract_text(file_bytes: bytes, file_name: str) -> str:
    extension = os.path.splitext(file_name)[1].lower()
    if extension == ".pdf":
        return _extract_pdf(file_bytes)
    if extension == ".docx":
        return _extract_docx(file_bytes)
    # .txt e qualquer outra coisa: melhor esforço como texto puro.
    return file_bytes.decode("utf-8", errors="ignore")


def run_ingestion(payload: dict) -> None:
    """Baixa o documento do S3, extrai o texto, quebra em chunks e grava no
    pgvector — chamado em background thread pelo consumidor da fila
    task.agent.<nome>.rag (ver services/queue/consumer.py#_on_rag_ingest). Sempre avisa o Agent-Api do
    resultado (READY/FAILED) ao final, mesmo em erro."""
    rag_document_id = payload["ragDocumentId"]

    try:
        file_bytes = download_object(payload["s3Key"])
        text = _extract_text(file_bytes, payload["fileName"])

        if not text.strip():
            raise ValueError("Não foi possível extrair texto do documento.")

        chunk_size = payload["chunkSize"]
        splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=min(50, chunk_size // 10))
        chunks = [c for c in splitter.split_text(text) if c.strip()]

        if not chunks:
            raise ValueError("Documento não gerou nenhum chunk de texto.")

        categories = payload.get("categories") or []
        metadatas = [
            {
                "agent_id": payload["agentId"],
                "ragDocumentId": rag_document_id,
                "categorias": categories,
                "fileName": payload["fileName"],
            }
            for _ in chunks
        ]

        get_vector_store().add_texts(texts=chunks, metadatas=metadatas)
        registrar_tokens(payload["agentId"], "OPENAI", contar_tokens_embedding(chunks))

        update_rag_document_status(rag_document_id, "READY", chunk_count=len(chunks))
        print(f"[julia] Ingestão concluída: ragDocumentId={rag_document_id} chunks={len(chunks)}")
    except Exception as e:
        print(f"[julia] Falha na ingestão de {rag_document_id}: {e}")
        traceback.print_exc()
        update_rag_document_status(rag_document_id, "FAILED", error_message=str(e)[:500])
