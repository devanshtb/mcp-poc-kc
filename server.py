"""
server.py  —  Knowledge base MCP server (read-only)
Hybrid search via Qdrant: dense (all-MiniLM-L6-v2) + Qdrant/BM25 + RRF.

Tools:
  search(query, top_k)   — hybrid search, returns IDs  [ChatGPT Deep Research]
  fetch(id)              — get chunk content by point ID [ChatGPT Deep Research]
  list_documents()       — list all documents
  get_document(id)       — full text of a document by document_id
"""

import os
import asyncio
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastembed import TextEmbedding, SparseTextEmbedding
from fastmcp import FastMCP
from fastmcp.server.dependencies import CurrentAccessToken
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider
from qdrant_client import AsyncQdrantClient, models

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

QDRANT_URL     = os.environ["QDRANT_URL"]
QDRANT_API_KEY = os.environ["QDRANT_API_KEY"]
COLLECTION     = "knowledge_base"
DENSE_MODEL    = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL   = "Qdrant/bm25"
TOP_K_DEFAULT  = 5

# ── Globals ───────────────────────────────────────────────────────────────────

qdrant: AsyncQdrantClient = None   # type: ignore

# ── Embedding helpers (sync, run in executor) ─────────────────────────────────

_dense_model  = None
_sparse_model = None

def _load_models():
    global _dense_model, _sparse_model
    _dense_model  = TextEmbedding(DENSE_MODEL)
    _sparse_model = SparseTextEmbedding(SPARSE_MODEL)

def _embed(query: str):
    dense_vec  = list(_dense_model.embed([query]))[0].tolist()
    sparse_obj = list(_sparse_model.embed([query]))[0]
    return dense_vec, sparse_obj

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(server):
    global qdrant

    print("Loading embedding models (first run downloads ~150 MB)...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _load_models)
    print("Models ready.")

    qdrant = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    print("Qdrant client ready.")

    yield

    await qdrant.close()
    print("Qdrant client closed.")

# ── FastMCP server setup ──────────────────────────────────────────────────────

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
KEYCLOAK_REALM_URL = os.environ.get("KEYCLOAK_REALM_URL")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID")

if KEYCLOAK_REALM_URL and KEYCLOAK_CLIENT_ID:
    auth = KeycloakAuthProvider(
        realm_url=KEYCLOAK_REALM_URL,
        base_url=BASE_URL,
        audience=KEYCLOAK_CLIENT_ID
    )
    print("Keycloak provider configured.")
else:
    print("Warning: Keycloak environment variables missing. Running without authentication enforcement.")
    auth = None

mcp = FastMCP("Knowledge Base", lifespan=lifespan, auth=auth)

# ── RBAC Utility ─────────────────────────────────────────────────────────────

def get_dept_pos_filter(token: AccessToken | None) -> models.Filter | None:
    """Return a Qdrant Filter based on user's department and position."""
    if not token or not token.claims:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="department_position_pairs",
                    match=models.MatchAny(any=["0-0"]),
                )
            ]
        )
    
    claims_lower = {k.lower(): v for k, v in token.claims.items()}
    dept_id = str(claims_lower.get("departmentid", 0))
    pos_id = str(claims_lower.get("positionid", 0))

    scopes = token.scopes if token else []
    roles = token.claims.get("realm_access", {}).get("roles", [])
    if "admin" in scopes or "Admin" in scopes or "admin" in roles or "Admin" in roles:
        return None  # Admin can access everything
    
    allowed_pairs = [
        f"{dept_id}-{pos_id}",  # Exact match
        f"0-{pos_id}",          # Wildcard department
        f"{dept_id}-0",         # Wildcard position
        "0-0"                   # Public documents
    ]
    
    return models.Filter(
        must=[
            models.FieldCondition(
                key="department_position_pairs",
                match=models.MatchAny(any=allowed_pairs),
            )
        ]
    )

# ── Tool 1: search ─────────────────────────────────────────────────────────────

@mcp.tool
async def search(query: str, token: AccessToken = CurrentAccessToken(), top_k: int = TOP_K_DEFAULT) -> dict:
    """
    Hybrid search across the knowledge base (semantic + keyword).
    Returns a list of point IDs ranked by relevance.
    Use `fetch` to get the content for each ID.

    Args:
        query:  Natural language search query.
        top_k:  Number of results (default 5, max 20).
    """
    import sys, json
    print("\n" + "=" * 60)
    print("Step 3: Tool Execution (ChatGPT ↔ MCP Server on Render)")
    print("Action: ChatGPT sends a POST request to your Render MCP Server to execute a tool (like search).")
    print("Data Passed:")
    decoded_claims = token.claims if token and token.claims else "None"
    print(f"Headers: Authorization: Bearer [HIDDEN]")
    if decoded_claims != "None":
        print(f"Decoded Token Claims: {json.dumps(decoded_claims)}")
    else:
        print("Decoded Token Claims: None")
    print(f"Body: The search query (e.g., {json.dumps({'query': query, 'top_k': top_k})}).")
    print("=" * 60 + "\n")
    sys.stdout.flush()

    top_k = min(top_k, 20)

    loop = asyncio.get_event_loop()
    dense_vec, sparse_obj = await loop.run_in_executor(None, _embed, query)

    results = await qdrant.query_points(
        collection_name=COLLECTION,
        prefetch=[
            # Dense (semantic) candidates
            models.Prefetch(
                query=dense_vec,
                using="dense",
                limit=top_k * 3,
            ),
            # Sparse (SPLADE / keyword) candidates
            models.Prefetch(
                query=models.SparseVector(
                    indices=sparse_obj.indices.tolist(),
                    values=sparse_obj.values.tolist(),
                ),
                using="sparse",
                limit=top_k * 3,
            ),
        ],
        # Qdrant's built-in RRF fusion across both prefetch results
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=get_dept_pos_filter(token),
        limit=top_k,
        with_payload=True,
    )

    ids = [str(point.id) for point in results.points]
    
    print("\n" + "=" * 60)
    print("Step 5/6: Tool Response (MCP Server ↔ ChatGPT) [Search]")
    print("Action: Returning search results (matching IDs) from Qdrant to ChatGPT.")
    print(f"Data Returned: {json.dumps({'ids': ids})}")
    print("=" * 60 + "\n")
    sys.stdout.flush()

    return {"ids": ids}

# ── Tool 2: fetch ─────────────────────────────────────────────────────────────

@mcp.tool
async def fetch(id: str, token: AccessToken = CurrentAccessToken()) -> dict:
    """
    Fetch the full content of a chunk by its point ID.
    Use IDs returned by `search`.

    Args:
        id: Point ID (UUID string from `search`).
    """
    import sys, json
    print("\n" + "=" * 60)
    print("Step 3: Tool Execution (ChatGPT ↔ MCP Server on Render) [Fetch]")
    print("Action: ChatGPT sends a POST request to your Render MCP Server to execute a tool (like fetch).")
    print("Data Passed:")
    decoded_claims = token.claims if token and token.claims else "None"
    print(f"Headers: Authorization: Bearer [HIDDEN]")
    if decoded_claims != "None":
        print(f"Decoded Token Claims: {json.dumps(decoded_claims)}")
    else:
        print("Decoded Token Claims: None")
    print(f"Body: The fetch query (e.g., {json.dumps({'id': id})}).")
    print("=" * 60 + "\n")
    sys.stdout.flush()

    results = await qdrant.retrieve(
        collection_name=COLLECTION,
        ids=[id],
        with_payload=True,
        with_vectors=False,
    )

    if not results:
        return {"error": f"Point {id} not found."}

    payload = results[0].payload or {}
    
    # Enforce RBAC
    doc_pairs = payload.get("department_position_pairs", ["0-0"])
    
    if not token or not token.claims:
        dept_id = "0"
        pos_id = "0"
        scopes = []
        roles = []
    else:
        claims_lower = {k.lower(): v for k, v in token.claims.items()}
        dept_id = str(claims_lower.get("departmentid", 0))
        pos_id = str(claims_lower.get("positionid", 0))
        scopes = token.scopes
        roles = token.claims.get("realm_access", {}).get("roles", [])
        
    allowed_pairs = [
        f"{dept_id}-{pos_id}",
        f"0-{pos_id}",
        f"{dept_id}-0",
        "0-0"
    ]
    
    is_admin = "admin" in scopes or "Admin" in scopes or "admin" in roles or "Admin" in roles
    has_access = any(pair in doc_pairs for pair in allowed_pairs)
    
    if not is_admin and not has_access:
        return {"error": f"Unauthorized. Point requires one of pairs: {doc_pairs}."}

    response_data = {
        "id":           id,
        "content":      payload.get("content", ""),
        "document_id":  payload.get("document_id", ""),
        "title":        payload.get("title", ""),
        "source":       payload.get("source", ""),
        "chunk_index":  payload.get("chunk_index", 0),
        "metadata":     {
            k: v for k, v in payload.items()
            if k not in ("document", "document_id", "title", "source", "chunk_index")
        },
    }
    
    import sys, json
    print("\n" + "=" * 60)
    print("Step 5/6: Tool Response (MCP Server ↔ ChatGPT) [Fetch]")
    print("Action: Returning fetched document content back to ChatGPT.")
    # Truncate content in logs so it doesn't spam the console too much
    log_data = response_data.copy()
    if len(log_data["content"]) > 100:
        log_data["content"] = log_data["content"][:100] + "... [TRUNCATED FOR LOGS]"
        
    print(f"Data Returned: {json.dumps(log_data)}")
    print("=" * 60 + "\n")
    sys.stdout.flush()
    
    return response_data

import logging
import sys

logger = logging.getLogger("mcp.auth")
logger.setLevel(logging.INFO)
# Also log to stdout so Render catches it immediately
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
logger.addHandler(handler)

@mcp.tool
async def list_documents(token: AccessToken = CurrentAccessToken()) -> dict:
    """
    List all documents in the Qdrant knowledge base.
    Only returns documents the user is authorized to see based on their Keycloak claims.
    """
    if token:
        try:
            import urllib.request
            import json
            KEYCLOAK_REALM_URL = os.environ.get("KEYCLOAK_REALM_URL", "")
            userinfo_url = f"{KEYCLOAK_REALM_URL.rstrip('/')}/protocol/openid-connect/userinfo"
            req = urllib.request.Request(userinfo_url)
            req.add_header("Authorization", f"Bearer {token.token}")
            with urllib.request.urlopen(req) as response:
                userinfo = json.loads(response.read())
                email = userinfo.get("email", "Unknown")
                
                # Log the authorization details explicitly to Render logs
                logger.info("=" * 60)
                logger.info(f"AUTH LOG - User Email: {email}")
                logger.info(f"AUTH LOG - Token Scopes: {token.scopes}")
                logger.info(f"AUTH LOG - Token Claims: {token.claims}")
                logger.info("=" * 60)
                
                # Force flush standard output just to be absolutely certain
                print(f"\n[RAW PRINT] Auth User Email: {email}\n")
                sys.stdout.flush()
        except Exception as e:
            logger.error(f"Failed to fetch userinfo: {e}")
            
    f = get_dept_pos_filter(token)
    
    seen: dict[str, dict] = {}
    offset = None

    while True:
        result, offset = await qdrant.scroll(
            collection_name=COLLECTION,
            scroll_filter=f,
            with_payload=True,
            with_vectors=False,
            limit=100,
            offset=offset,
        )
        for point in result:
            p = point.payload or {}
            did = p.get("document_id", "unknown")
            if did not in seen:
                seen[did] = {
                    "document_id": did,
                    "title":       p.get("title", ""),
                    "source":      p.get("source", ""),
                    "chunk_count": 0,
                }
            seen[did]["chunk_count"] += 1

        if offset is None:
            break

    documents = list(seen.values())
    return {"documents": documents, "total": len(documents)}

# ── Tool 4: get_document ──────────────────────────────────────────────────────

@mcp.tool
async def get_document(id: str, token: AccessToken = CurrentAccessToken()) -> dict:
    """
    Retrieve the full reassembled text of a document by its document_id.
    Use `list_documents` to get valid document IDs.

    Args:
        id: The document_id (UUID from `list_documents`).
    """
    import sys, json
    print("\n" + "=" * 60)
    print("Step 3: Tool Execution (ChatGPT ↔ MCP Server on Render) [Get Document]")
    print("Action: ChatGPT sends a POST request to your Render MCP Server to execute a tool (get_document).")
    print("Data Passed:")
    decoded_claims = token.claims if token and token.claims else "None"
    print(f"Headers: Authorization: Bearer [HIDDEN]")
    if decoded_claims != "None":
        print(f"Decoded Token Claims: {json.dumps(decoded_claims)}")
    else:
        print("Decoded Token Claims: None")
    print(f"Body: The get_document query (e.g., {json.dumps({'id': id})}).")
    print("=" * 60 + "\n")
    sys.stdout.flush()

    all_chunks = []
    offset = None
    
    role_filter = get_dept_pos_filter(token)
    must_conditions = [
        models.FieldCondition(
            key="document_id",
            match=models.MatchValue(value=id),
        )
    ]
    if role_filter and role_filter.must:
        must_conditions.extend(role_filter.must)

    while True:
        result, offset = await qdrant.scroll(
            collection_name=COLLECTION,
            scroll_filter=models.Filter(must=must_conditions),
            with_payload=True,
            with_vectors=False,
            limit=100,
            offset=offset,
        )
        all_chunks.extend(result)
        if offset is None:
            break

    if not all_chunks:
        return {"error": f"Document '{id}' not found."}

    # Sort by chunk_index and reassemble
    all_chunks.sort(key=lambda p: (p.payload or {}).get("chunk_index", 0))
    first_payload = all_chunks[0].payload or {}
    full_text = "\n\n".join((p.payload or {}).get("content", "") for p in all_chunks)

    response_data = {
        "document_id": id,
        "title":       first_payload.get("title", ""),
        "source":      first_payload.get("source", ""),
        "content":     full_text,
        "chunk_count": len(all_chunks),
    }
    
    import sys, json
    print("\n" + "=" * 60)
    print("Step 5/6: Tool Response (MCP Server ↔ ChatGPT) [Get Document]")
    print("Action: Returning full assembled document content back to ChatGPT.")
    log_data = response_data.copy()
    if len(log_data["content"]) > 100:
        log_data["content"] = log_data["content"][:100] + "... [TRUNCATED FOR LOGS]"
    print(f"Data Returned: {json.dumps(log_data)}")
    print("=" * 60 + "\n")
    sys.stdout.flush()

    return response_data

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="http", host="0.0.0.0", port=port)