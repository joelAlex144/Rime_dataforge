"""Mint LiveKit access tokens for the voice-input room.

Two callers need a token: the browser tab (publishes the mic, identity
"listener") and the voice bridge bot (subscribes to that audio, identity
"voice-bridge"). Both join the same room -- one room per server session id,
so a token minted here is only ever good for this session's own room.

This file only creates tokens. It does not touch anything in delivery_layer/,
server.py's WebSocket protocol, or the existing interrupt/ask handling --
the bridge that uses these tokens talks to the server the same way the
browser's text UI already does (see voice/bridge.py).
"""
from __future__ import annotations

import os

from livekit import api


def room_name_for_session(session_id: str) -> str:
    return f"policy-reader-{session_id}"


def mint_token(session_id: str, identity: str, *, can_publish: bool, can_subscribe: bool) -> str:
    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("LIVEKIT_API_KEY / LIVEKIT_API_SECRET not set")
    room = room_name_for_session(session_id)
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_data=False,
    )
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
    )
    return token.to_jwt()
