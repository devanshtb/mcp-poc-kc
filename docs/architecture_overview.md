# Knowledge Base MCP Server: Architecture & Code Flow Overview

This document provides a detailed breakdown of how the Knowledge Base MCP server operates, including its primary execution flows and how authentication is handled.

The codebase is split into two distinct parts with different lifecycles: **Ingestion** (adding knowledge) and **Serving** (exposing tools to an AI).

---

## 1. The Ingestion Flow (`ingest.py`)

This script is an admin/offline tool used to process documents and store them in the Qdrant vector database.

*   **Where it Starts:** The entry point is at the bottom of the script (`if __name__ == "__main__":`). It starts when executed via the command line, e.g., `python ingest.py --file path/to/document.pdf --role hr`.
*   **How it Works (The Flow):**
    1.  **Initialization:** It loads environment variables (`.env`) for Qdrant connection details. Upon startup, it loads the `fastembed` embedding models into memory (one Dense semantic model, one Sparse keyword model) and initializes a synchronous `QdrantClient`.
    2.  **Argument Parsing:** It parses the CLI command to determine the action (`--file` for ingest, `--list` to show docs, or `--delete` to remove a doc).
    3.  **Processing (Ingestion):** If ingesting a file, it enters the `cmd_ingest()` function:
        *   **Extraction:** Extracts raw text based on the file extension using libraries like `pypdf` (for PDFs) or `docx` (for Word documents).
        *   **Chunking:** Breaks the long text into smaller chunks (500 words each with 50 words of overlap) using `chunk_text()`.
        *   **Embedding:** Runs the text chunks through the AI models to create numerical vectors (both dense and sparse).
        *   **Uploading:** Wraps the vectors and metadata (source file name, document ID, and the required RBAC `role`) into "Points" and batch uploads them to Qdrant.
*   **Where it Ends:** Once the chunks are successfully uploaded (or listed/deleted), the script prints a success message and terminates entirely. It does not stay running.

---

## 2. The Server Flow (`server.py`)

This is the main application. It acts as an API server implementing the Model Context Protocol (MCP), allowing AI assistants (like ChatGPT) to call specific search tools.

*   **Where it Starts:** The entry point is at the bottom (`if __name__ == "__main__":`). Run via `python server.py`, it starts the `FastMCP` server, listening on a specific port (default `8000`).
*   **Server Startup & Lifespan:**
    1.  **Configuration:** Loads Qdrant and Auth0 configuration from the `.env` file.
    2.  **Lifespan Hook (`@asynccontextmanager async def lifespan(server)`):** Before accepting requests, this asynchronous function runs. It loads the embedding models into memory (preventing them from blocking the server later) and establishes an `AsyncQdrantClient` connection to Qdrant.
    3.  **Auth0 Setup:** If Auth0 credentials are provided, it configures an authentication provider to intercept incoming requests and validate access tokens.
*   **How it Works (The Serving Loop):**
    *   The server stays alive indefinitely, waiting for an AI client to call its defined `@mcp.tool` endpoints. It exposes 4 tools:
        1.  `search(query, top_k)`: Embeds the query, searches Qdrant using a hybrid approach (semantic + keyword), applies a Role-Based Access Control (RBAC) filter using Auth0 token scopes, and returns Document Chunk IDs.
        2.  `fetch(id)`: Retrieves the full text of a specific chunk ID from Qdrant, verifies the user's role against the document's `role`, and returns the text.
        3.  `list_documents()`: Returns a list of all documents the user is authorized to see.
        4.  `get_document(id)`: Retrieves the full reconstructed text of an entire document.
*   **Where it Ends:** The server runs in a continuous loop until manually terminated (e.g., `Ctrl+C`). Upon receiving a shutdown signal, the `lifespan` context manager resumes, allowing it to gracefully close the connection to Qdrant (`await qdrant.close()`) before fully exiting.

---

## 3. Authentication & The Auth0 Token

In every tool function parameter, you will see `token: AccessToken = CurrentAccessToken()`. This represents the Auth0 JWT (JSON Web Token), which is handled automatically by the FastMCP framework.

### How it is Injected
The parameter relies on a default dependency injection: `= CurrentAccessToken()`. Developers do not manually pass this token when calling the function. 

Instead, when an AI client sends a request to the MCP server, it includes the Auth0 token in the request headers. The `FastMCP` framework intercepts this, utilizes the configured `Auth0Provider`, validates the token, and automatically injects the parsed token into the function.

### What is inside the `token` object?
It is not a raw string, but an `AccessToken` object (from `fastmcp.server.auth`) that has already decoded the Auth0 token, providing easy access to the JWT data:

*   **`token.scopes`**: A list of scopes the user was granted (e.g., `["read:documents", "admin"]`).
*   **`token.claims`**: A dictionary of the raw JSON payload inside the JWT. Used to extract custom Auth0 claims, such as `token.claims["permissions"]`.
*   **`token.token`**: The raw, unparsed string of the JWT token itself (which can be used to make direct API calls to endpoints like the `AUTH0_DOMAIN/userinfo`).

### Unauthenticated Access
If the user isn't authenticated or the `.env` is missing Auth0 variables, the server creates the `FastMCP` app with `auth=None`. In this case, `CurrentAccessToken()` returns `None`. The code safely handles this scenario (e.g., `scopes = token.scopes if token else []`), treating the user as an unauthenticated "public" user.
