# Architecture & Implementation Summary: FastMCP RBAC Integration

## 1. Overview of Today's Work
Today, we successfully upgraded the FastMCP Knowledge Base server from a fully public endpoint to a secure, identity-aware system using strict **Role-Based Access Control (RBAC)**. 

### Key Accomplishments:
* **OAuth2 Integration:** Connected the FastMCP server to **Auth0** to cryptographically verify users connecting via ChatGPT.
* **Database Upgrades:** Updated the `ingest.py` script to inject a `role` metadata field into every vector chunk stored in Qdrant (e.g., tagging the Engineering Doc with the `engineer` role).
* **Token Parsing:** Modified `server.py` to extract custom `permissions` claims directly from Auth0 JWT Access Tokens.
* **Vector Filtering:** Implemented a Qdrant `Filter` layer (`get_role_filter`) that cross-references the user's Auth0 permissions against the document's assigned role.
* **Debug Telemetry:** Built custom server-side logging that uses the Auth0 Access Token to hit the `/userinfo` endpoint, successfully printing the user's email address directly into the Render terminal logs.
* **Network Flow Analysis:** Mapped out the exact JSON-RPC payloads and HTTP headers passed between OpenAI's servers and the Render backend.

---

## 2. How We Implemented RBAC

The RBAC system relies on a three-way handshake between Auth0, FastMCP, and Qdrant.

### Step A: The Auth0 Configuration
Instead of just assigning a "Role" to a user in Auth0, we configured Auth0 to inject **Permissions** directly into the Access Token. 
When the user logs in, Auth0 generates a token with a custom claim:
```json
"permissions": ["engineer"]
```

### Step B: The Ingestion Pipeline
When documents are parsed and uploaded to the vector database, we embed the required role directly into the payload alongside the text chunk.
```python
"payload": {
    "title": "Engineering Doc",
    "role": "engineer",
    "content": "..."
}
```

### Step C: The FastMCP Query Interceptor
In `server.py`, we intercept the Auth0 token before any database query is run. We extract the `permissions` array and dynamically construct a Qdrant query filter.
```python
def get_role_filter(token: AccessToken | None) -> models.Filter | None:
    scopes = token.claims.get("permissions", [])
    
    # Allow access if the document is public, OR if the document's role matches the user's scope
    return models.Filter(
        should=[
            models.FieldCondition(key="role", match=models.MatchValue(value="public")),
            models.FieldCondition(key="role", match=models.MatchAny(any=scopes))
        ]
    )
```
This filter is passed into `qdrant.scroll()` and `qdrant.search()`, ensuring the database physically cannot return unauthorized documents.

---

## 3. Why We Selected This Approach

> [!TIP]
> **Why this matters:** Security in AI applications is fundamentally different than standard web apps. The AI model is highly susceptible to "Prompt Injection." If you fetch all documents and ask the AI to filter them, a malicious user can trick the AI into revealing hidden data.

### Reason 1: Defense in Depth (Database-Level Security)
By pushing the role filter directly into the Qdrant vector search, we achieve **Zero-Trust Security**. 
We are not fetching all documents and relying on Python or ChatGPT to filter them out later. Instead, the database engine itself simply refuses to return vector chunks that the user does not have permission to see. Even if the AI model goes rogue, the restricted data never reaches it.

### Reason 2: Completely Stateless Architecture
Our Render backend does not use databases to store sessions or user accounts. We completely trust the cryptography of the JWT Access Token provided by Auth0. Because the token contains the `permissions` claim inherently, our server is **stateless**. This means Render can spin up 1,000 instances of our server under heavy load, and none of them need to sync session data.

### Reason 3: Native ChatGPT Integration
We leveraged the standard **OAuth2 Authorization Code Flow**. Because ChatGPT natively supports OAuth2 "Connected Apps", we didn't have to build any custom UI. We offloaded all login screens, multi-factor authentication (MFA), and password resets entirely to Auth0, resulting in a perfectly seamless, enterprise-grade user experience.
