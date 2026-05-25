"""Minimal AT Protocol XRPC client — just enough to log in and post a reply.

stdlib only (urllib), so the responder adds no dependency beyond `websockets`.
Credentials are read from the environment by the caller and never stored here.

Auth uses an *app password* (Bluesky Settings → Privacy and security →
App passwords), never the account's real password.
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_PDS = "https://bsky.social"
POST_COLLECTION = "app.bsky.feed.post"


class BskyError(RuntimeError):
    pass


def _post_json(url: str, body: dict, token: Optional[str] = None) -> dict:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise BskyError(f"{exc.code} {exc.reason}: {detail}") from exc


class BskyClient:
    def __init__(self, identifier: str, app_password: str, pds: str = DEFAULT_PDS):
        self.pds = pds.rstrip("/")
        session = _post_json(
            f"{self.pds}/xrpc/com.atproto.server.createSession",
            {"identifier": identifier, "password": app_password},
        )
        self.did = session["did"]
        self.handle = session.get("handle", identifier)
        self.access_jwt = session["accessJwt"]
        self.refresh_jwt = session.get("refreshJwt")

    def _refresh_session(self) -> None:
        """Trade the refresh token for a fresh access token. The relay can run
        for days; access JWTs expire in well under that."""
        if not self.refresh_jwt:
            raise BskyError("session expired and no refresh token is available")
        session = _post_json(
            f"{self.pds}/xrpc/com.atproto.server.refreshSession",
            {}, token=self.refresh_jwt,
        )
        self.access_jwt = session["accessJwt"]
        self.refresh_jwt = session.get("refreshJwt", self.refresh_jwt)

    def _create_record(self, record: dict) -> dict:
        return _post_json(
            f"{self.pds}/xrpc/com.atproto.repo.createRecord",
            {"repo": self.did, "collection": POST_COLLECTION, "record": record},
            token=self.access_jwt,
        )

    def post(
        self,
        text: str,
        facets: Optional[List[Dict]] = None,
        langs: Optional[List[str]] = None,
    ) -> str:
        """Create a new top-level post. Returns the new post's AT URI."""
        record = {
            "$type": POST_COLLECTION,
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "langs": langs or ["en"],
        }
        if facets:
            record["facets"] = facets
        try:
            result = self._create_record(record)
        except BskyError as exc:
            # One transparent retry if the access token has expired.
            if "ExpiredToken" not in str(exc) and "expired" not in str(exc).lower():
                raise
            self._refresh_session()
            result = self._create_record(record)
        return result["uri"]

    def post_reply(
        self,
        text: str,
        parent: Dict[str, str],
        root: Optional[Dict[str, str]] = None,
        facets: Optional[List[Dict]] = None,
        langs: Optional[List[str]] = None,
    ) -> str:
        """Create a reply post. `parent`/`root` are {"uri","cid"} strong refs.

        Returns the new post's AT URI.
        """
        root = root or parent
        record = {
            "$type": POST_COLLECTION,
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "reply": {"root": root, "parent": parent},
            "langs": langs or ["en"],
        }
        if facets:
            record["facets"] = facets
        try:
            result = self._create_record(record)
        except BskyError as exc:
            # One transparent retry if the access token has expired.
            if "ExpiredToken" not in str(exc) and "expired" not in str(exc).lower():
                raise
            self._refresh_session()
            result = self._create_record(record)
        return result["uri"]
