import httpx
import oauthlib.oauth1  # NB: `import oauthlib` alone does not expose the oauth1 submodule


class _OAuth1Auth(httpx.Auth):
    """Signs each request with OAuth 1.0a HMAC-SHA1.

    Attached to the connector's httpx client as `auth=`, so connector methods
    never build headers themselves and an injected mock client (with no auth)
    is left unsigned — which is fine, mocks do not verify signatures.
    """

    # httpx reads the body inside auth_flow to fold it into the signature base
    # string. For the connector's form-encoded `data=` requests the body is
    # already available; this flag is defensive against a future switch to a
    # streaming body, which would otherwise raise httpx.RequestNotRead.
    requires_request_body = True

    def __init__(self, consumer_key, consumer_secret, oauth_token, oauth_token_secret):
        self._client = oauthlib.oauth1.Client(
            consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=oauth_token,
            resource_owner_secret=oauth_token_secret,
            signature_method=oauthlib.oauth1.SIGNATURE_HMAC_SHA1,
            signature_type=oauthlib.oauth1.SIGNATURE_TYPE_AUTH_HEADER,
        )

    def auth_flow(self, request):
        body = request.content.decode() if request.content else None
        _uri, headers, _body = self._client.sign(
            str(request.url),
            http_method=request.method,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        request.headers["Authorization"] = headers["Authorization"]
        yield request
