"""
ingest.py  —  Admin-only script to add documents to the knowledge base.
Never exposed via MCP. Run locally only.

Usage:
    python ingest.py --file path/to/document.pdf
    python ingest.py --file path/to/document.txt
    python ingest.py --file path/to/document.docx
    python ingest.py --file path/to/document.md
    python ingest.py --list
    python ingest.py --delete <document_id>
"""

import argparse
import os
import sys
import uuid
from pathlib import Path
from dotenv import load_dotenv
from fastembed import TextEmbedding, SparseTextEmbedding
from qdrant_client import QdrantClient, models

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

QDRANT_URL     = os.environ["QDRANT_URL"]
QDRANT_API_KEY = os.environ["QDRANT_API_KEY"]
COLLECTION     = "knowledge_base"
DENSE_MODEL    = "sentence-transformers/all-MiniLM-L6-v2"   # 384-dim
SPARSE_MODEL   = "Qdrant/bm25"               # Bm25
DENSE_DIM      = 384
CHUNK_SIZE     = 500
CHUNK_OVERLAP  = 50

# ── Embedding models (loaded once at startup) ─────────────────────────────────

print("Loading embedding models (first run downloads ~150 MB)...")
dense_model  = TextEmbedding(DENSE_MODEL)
sparse_model = SparseTextEmbedding(SPARSE_MODEL)
print("Models ready.")

# ── Qdrant client ─────────────────────────────────────────────────────────────

client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

# ── Collection setup ──────────────────────────────────────────────────────────

def ensure_collection():
    """Create the collection if it doesn't already exist."""
    if client.collection_exists(COLLECTION):
        return

    print(f"Creating collection '{COLLECTION}'...")
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            "dense": models.VectorParams(
                size=DENSE_DIM,
                distance=models.Distance.COSINE,
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                modifier=models.Modifier.IDF,
            )
        },
    )
    
    print("Creating payload indices...")
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="department_position_pairs",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="document_id",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    client.create_payload_index(
        collection_name=COLLECTION,
        field_name="companies",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    print("Collection created.")

# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(file_path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)

    elif suffix == ".docx":
        from docx import Document
        doc = Document(str(file_path))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())

    elif suffix in (".md", ".txt", ".rst"):
        return file_path.read_text(encoding="utf-8")

    else:
        raise ValueError(f"Unsupported file type: {suffix}. Supported: pdf, docx, md, txt, rst")

# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str) -> list[str]:
    words       = text.split()
    word_size   = int(CHUNK_SIZE * 0.75)
    word_overlap = int(CHUNK_OVERLAP * 0.75)
    chunks, start = [], 0

    while start < len(words):
        chunk = " ".join(words[start : start + word_size])
        if chunk.strip():
            chunks.append(chunk)
        start += word_size - word_overlap

    return chunks

# ── Ingest ────────────────────────────────────────────────────────────────────

def cmd_ingest(file_path_str: str, dept: str = "0", pos: str = "0", company: str = "0"):
    file_path = Path(file_path_str)
    if not file_path.exists():
        print(f"Error: file not found: {file_path}")
        sys.exit(1)

    ensure_collection()

    print(f"\n📄 Ingesting: {file_path.name}")

    print("  Extracting text...")
    text = extract_text(file_path)
    print(f"  Extracted {len(text):,} characters")

    if not text.strip():
        print("  Error: no text extracted.")
        sys.exit(1)

    print("  Chunking...")
    chunks = chunk_text(text)
    print(f"  {len(chunks)} chunks")

    # Shared document_id groups all chunks from this file together
    document_id = str(uuid.uuid4())
    title = file_path.stem.replace("_", " ").replace("-", " ").title()
    companies = [c.strip() for c in company.split(",")] if company else ["0"]

    print("  Generating embeddings...")
    dense_vecs  = list(dense_model.embed(chunks))
    sparse_vecs = list(sparse_model.embed(chunks))

    print("  Uploading to Qdrant...")
    points = [
        models.PointStruct(
            id=str(uuid.uuid4()),
            vector={
                "dense":  dense_vecs[i].tolist(),
                "sparse": models.SparseVector(
                    indices=sparse_vecs[i].indices.tolist(),
                    values=sparse_vecs[i].values.tolist(),
                ),
            },
            payload={
                "document_id": document_id,
                "title":       title,
                "source":      file_path.name,
                "chunk_index": i,
                "file_type":   file_path.suffix.lstrip("."),
                "content":     chunks[i],
                "department_position_pairs": [f"{dept}-{pos}"],
                "companies":   companies,
            },
        )
        for i in range(len(chunks))
    ]

    # Upload in batches of 64 to avoid request size limits
    batch_size = 64
    for start in range(0, len(points), batch_size):
        client.upsert(
            collection_name=COLLECTION,
            points=points[start : start + batch_size],
        )
        print(f"  Uploaded {min(start + batch_size, len(points))}/{len(points)} chunks...")

    print(f"\n✅ Done!")
    print(f"   Document ID : {document_id}")
    print(f"   Title       : {title}")
    print(f"   Chunks      : {len(chunks)}")

# ── List ──────────────────────────────────────────────────────────────────────

def cmd_list():
    ensure_collection()

    # Scroll through all points and group by document_id
    seen: dict[str, dict] = {}
    offset = None

    while True:
        result, offset = client.scroll(
            collection_name=COLLECTION,
            with_payload=True,
            with_vectors=False,
            limit=100,
            offset=offset,
        )
        for point in result:
            p = point.payload or {}
            did = p.get("document_id", "unknown")
            if did not in seen:
                seen[did] = {"title": p.get("title",""), "source": p.get("source",""), "chunks": 0}
            seen[did]["chunks"] += 1

        if offset is None:
            break

    if not seen:
        print("Knowledge base is empty.")
        return

    print(f"\n{'Document ID':<38}  {'Title':<30}  {'Chunks':>6}  Source")
    print("-" * 90)
    for did, info in seen.items():
        print(f"{did:<38}  {info['title'][:30]:<30}  {info['chunks']:>6}  {info['source']}")

# ── Delete ────────────────────────────────────────────────────────────────────

def cmd_delete(document_id: str):
    ensure_collection()

    client.delete(
        collection_name=COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="document_id",
                        match=models.MatchValue(value=document_id),
                    )
                ]
            )
        ),
    )
    print(f"✅ Deleted all chunks for document: {document_id}")

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Knowledge base admin tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file",   type=str, help="Path to file to ingest")
    parser.add_argument("--dept",  type=str, default="0", help="Department ID required to access this document")
    parser.add_argument("--pos",   type=str, default="0", help="Position ID required to access this document")
    parser.add_argument("--company", type=str, default="0", help="Comma-separated list of companies for this document (e.g. 'CompanyA,CompanyB')")
    group.add_argument("--list",   action="store_true", help="List all documents")
    group.add_argument("--delete", type=str, metavar="DOCUMENT_ID", help="Delete by document ID")

    args = parser.parse_args()

    if args.file:
        cmd_ingest(args.file, args.dept, args.pos, args.company)
    elif args.list:
        cmd_list()
    elif args.delete:
        cmd_delete(args.delete)