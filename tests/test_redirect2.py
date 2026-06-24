import urllib.request, json, urllib.error
from urllib.parse import urlparse, parse_qs

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
    consent_url = e.headers.get('Location')
    # It redirects to consent_url
    # We must POST to consent_url with action=allow
    
    try:
        # submit consent
        post_req = urllib.request.Request(consent_url, data=b'action=allow', headers={'Content-Type': 'application/x-www-form-urlencoded'})
        urllib.request.urlopen(post_req)
    except urllib.error.HTTPError as e2:
        auth0_url = e2.headers.get('Location')
        print(auth0_url)
        # Parse auth0 url
        parsed = urlparse(auth0_url)
        qs = parse_qs(parsed.query)
        print("Redirect URI sent to Auth0:", qs.get('redirect_uri'))
