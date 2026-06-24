# Knowledge Base MCP Server (Qdrant)

Read-only MCP server with hybrid search — dense semantic (all-MiniLM-L6-v2) +
sparse keyword (BM-25) — fused with Qdrant's built-in RRF.

## Stack

| Layer | Tool | Cost |
|---|---|---|
| MCP framework | FastMCP | Free |
| Vector DB | Qdrant Cloud | Free (1 GB) |
| Embeddings | FastEmbed (local, ONNX) | Free |
| Hosting | Railway | Free ($5 credit/mo) |
| ChatGPT | Deep Research connector | Free (Pro plan) |

---

## Setup

### 1. Install dependencies
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set up Qdrant Cloud
1. Go to https://cloud.qdrant.io → create a free cluster
2. Copy the cluster URL and API key from the Access tab
3. The collection is created automatically on first ingest

### 3. Configure .env
```bash
cp .env.example .env
# fill in QDRANT_URL, QDRANT_API_KEY, MCP_API_KEY
```

### 4. Upload documents
```bash
python ingest.py --file report.pdf
python ingest.py --file notes.md
python ingest.py --list
python ingest.py --delete <document_id>
```
Supported formats: pdf, docx, md, txt, rst

### 5. Test locally
```bash
python server.py
# → http://localhost:8000/mcp
```

### 6. Deploy to Railway
1. Push to GitHub (`.env` is gitignored)
2. Railway → New Project → Deploy from GitHub
3. Add env vars: `QDRANT_URL`, `QDRANT_API_KEY`, `MCP_API_KEY`
4. Copy your public URL

### 7. Connect to ChatGPT
- Settings → Connectors → Add → paste Railway URL + `/mcp/`
- Auth: Bearer token → your `MCP_API_KEY`
- Use via `+` → Deep Research → select your connector

---

## Tools (all read-only)

| Tool | Description |
|---|---|
| `search(query, top_k)` | Hybrid search. Returns point IDs. |
| `fetch(id)` | Get chunk content by point ID. |
| `list_documents()` | List all documents. |
| `get_document(id)` | Full text of a document by document_id. |

`search` + `fetch` follow the ChatGPT Deep Research contract.
