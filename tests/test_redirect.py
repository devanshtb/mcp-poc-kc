import urllib.request, json, urllib.error

req = urllib.request.Request(
    'https://mcp-poc-mbv3.onrender.com/register', 
    data=b'{"client_name": "test", "redirect_uris": ["https://example.com/callback"]}', 
    headers={'Content-Type': 'application/json'}
)
res = urllib.request.urlopen(req)
data = json.loads(res.read())
client_id = data['client_id']

auth_url = f'https://mcp-poc-mbv3.onrender.com/authorize?client_id={client_id}&response_type=code&redirect_uri=https://example.com/callback&code_challenge=xyz123&code_challenge_method=S256&state=state123'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(NoRedirect)
urllib.request.install_opener(opener)

try:
    urllib.request.urlopen(auth_url)
except urllib.error.HTTPError as e:
    if e.code in (301, 302, 303, 307, 308):
        print(e.headers.get('Location'))
    else:
        print(f"Error {e.code}: {e.read().decode()}")
