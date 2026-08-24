"""
FCM's HTTP v1 API (the ONLY current API - the legacy key-based API was
deprecated in June 2023 and fully removed in June 2024) requires an
OAuth2 bearer token obtained from a service account, not a simple API
key. Getting that token means: build a signed JWT asserting who you are
(RS256, signed with the service account's private key), then exchange
it with Google's OAuth token endpoint for a short-lived access token.

This is the ONE place in Layer 6 that uses a Google-provided library
(google-auth) rather than hand-rolled httpx calls - and it's used for
EXACTLY this narrow purpose (JWT signing + token exchange), nothing
else. This is not the kind of "wrapper" the no-LangChain/no-LiteLLM
requirement was about: google-auth doesn't decide what message to send,
doesn't pick a provider, doesn't cost anything, and hand-rolling RS256
JWT signing ourselves would be reinventing security-critical code that
Google's own library already gets right - there's no "our own logic"
upside to doing that badly ourselves. Every actual business decision in
this layer (what to send, to whom, with what priority, how to retry) is
in client.py/dispatcher_service.py, not here.
"""
import logging
import time
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]


class FCMCredentials:
    """
    Caches the access token and only refreshes when it's actually
    close to expiry - avoids a token-exchange round trip on every single
    notification send, which matters once a scheduled audit fires
    notifications to dozens of devices across multiple buildings at once.
    """

    def __init__(self, service_account_json_path: str):
        self._credentials = service_account.Credentials.from_service_account_file(
            service_account_json_path, scopes=_SCOPES
        )
        self._request = Request()

    def get_access_token(self) -> str:
        # Credentials.valid checks expiry internally (with a built-in
        # clock-skew margin) - refresh() is a no-op cost-wise if the
        # cached token is still valid, so it's safe to call this on
        # every send without worrying about refreshing too often.
        if not self._credentials.valid:
            self._credentials.refresh(self._request)
        return self._credentials.token

    @property
    def project_id(self) -> Optional[str]:
        return self._credentials.project_id
