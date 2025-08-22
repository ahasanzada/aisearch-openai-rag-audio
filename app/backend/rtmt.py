# app/backend/rtmt.py
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

import aiohttp
from aiohttp import web
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger("voicerag")
telemetry = logging.getLogger("telemetry")

class ToolResultDirection(Enum):
    TO_SERVER = 1
    TO_CLIENT = 2

class ToolResult:
    text: Any
    destination: ToolResultDirection

    def __init__(self, text: Any, destination: ToolResultDirection):
        self.text = text
        self.destination = destination

    def to_text(self) -> str:
        if self.text is None:
            return ""
        return self.text if isinstance(self.text, str) else json.dumps(self.text, ensure_ascii=False)

class Tool:
    target: Callable[..., ToolResult]
    schema: Any
    def __init__(self, schema: Any, target: Any):
        self.target = target
        self.schema = schema

class RTToolCall:
    tool_call_id: str
    previous_id: Optional[str]
    def __init__(self, tool_call_id: str, previous_id: Optional[str]):
        self.tool_call_id = tool_call_id
        self.previous_id = previous_id

class RTMiddleTier:
    endpoint: str
    deployment: str
    key: Optional[str] = None

    tools: dict[str, Tool]
    model: Optional[str] = None
    system_message: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    disable_audio: Optional[bool] = None
    voice_choice: Optional[str] = None

    # REST API version (not model version). Default to the currently valid Realtime API.
    api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

    # Telemetry state
    _speech_start_ts: Optional[float]
    _tts_start_ts: Optional[float]
    _out_transcript_buf: list[str]
    _last_asr_text: Optional[str]

    # Server tool-calling state
    _tools_pending: dict[str, RTToolCall]
    _token_provider = None

    # Behavior toggles
    _allow_client_voice_switch: bool
    _vad_silence_ms: int
    _vad_threshold: float
    _vad_prefix_ms: int

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        credentials: AzureKeyCredential | DefaultAzureCredential,
        voice_choice: Optional[str] = None,
    ):
        # Endpoint must be absolute (e.g., wss://<resource>.<region>.openai.azure.com)
        self.endpoint = endpoint
        self.deployment = deployment
        self.voice_choice = voice_choice
        if voice_choice:
            logger.info("Realtime voice choice set to %s", voice_choice)

        if isinstance(credentials, AzureKeyCredential):
            self.key = credentials.key
        else:
            self._token_provider = get_bearer_token_provider(
                credentials, "https://cognitiveservices.azure.com/.default"
            )
            # warm up token cache
            try:
                self._token_provider()
            except Exception as e:
                logger.warning("Failed to warm up token provider: %s", e)

        # init state
        self.tools = {}
        self._tools_pending = {}
        self._speech_start_ts = None
        self._tts_start_ts = None
        self._out_transcript_buf = []
        self._last_asr_text = None

        # env toggles
        self._allow_client_voice_switch = os.getenv("ALLOW_CLIENT_VOICE_SWITCH", "false").lower() == "true"
        self._vad_silence_ms = int(os.getenv("VAD_SILENCE_MS", "350"))
        self._vad_threshold = float(os.getenv("VAD_THRESHOLD", "0.5"))
        self._vad_prefix_ms = int(os.getenv("VAD_PREFIX_MS", "200"))

    # -------------------------
    # Message processing (server -> client)
    # -------------------------
    async def _process_message_to_client(
        self, msg: aiohttp.WSMessage, client_ws: web.WebSocketResponse, server_ws: web.WebSocketResponse
    ) -> Optional[str]:
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None

        try:
            message = json.loads(msg.data)
        except Exception:
            return msg.data

        updated_message: Optional[str] = msg.data

        # Telemetry helpers
        def _log(evt: str, payload: dict[str, Any] | None = None):
            data = {"evt": evt}
            if payload:
                data.update(payload)
            telemetry.info(json.dumps(data, ensure_ascii=False))

        if message is not None and isinstance(message, dict) and "type" in message:
            t = message["type"]

            # Hide sensitive session details from client
            if t == "session.created":
                session = message.get("session", {})
                session["instructions"] = ""
                session["tools"] = []
                session["tool_choice"] = "none"
                # enforce server voice on client view
                if self.voice_choice:
                    session["voice"] = self.voice_choice
                session["max_response_output_tokens"] = None
                updated_message = json.dumps(message, ensure_ascii=False)

            # --- Audio / ASR / TTS telemetry ---
            elif t == "input_audio_buffer.speech_started":
                self._speech_start_ts = time.perf_counter()
                _log("user_speech_started", {"ts": datetime.now(timezone.utc).isoformat()})

            elif t == "input_audio_buffer.speech_stopped":
                dur_ms = None
                if self._speech_start_ts is not None:
                    dur_ms = int((time.perf_counter() - self._speech_start_ts) * 1000)
                self._speech_start_ts = None
                _log("user_speech_stopped", {"duration_ms": dur_ms})

            elif t == "conversation.item.input_audio_transcription.completed":
                # final ASR text for the user's speech turn
                # schema varies slightly; cover both
                asr_text = message.get("transcript") or message.get("item", {}).get("transcript")
                self._last_asr_text = asr_text
                _log("user_asr_final", {"text": asr_text})

            elif t == "response.audio_transcript.delta":
                self._out_transcript_buf.append(message.get("delta", ""))

            elif t == "response.audio_transcript.done":
                final_txt = "".join(self._out_transcript_buf)
                self._out_transcript_buf.clear()
                _log("assistant_tts_transcript", {"text": final_txt})

            elif t == "response.audio.delta":
                if self._tts_start_ts is None:
                    self._tts_start_ts = time.perf_counter()
                    _log("assistant_tts_started")

            elif t == "response.audio.done":
                if self._tts_start_ts is not None:
                    tts_ms = int((time.perf_counter() - self._tts_start_ts) * 1000)
                    self._tts_start_ts = None
                    _log("assistant_tts_finished", {"duration_ms": tts_ms})

            # If you also want pure text form:
            elif t == "response.text.delta":
                self._out_transcript_buf.append(message.get("delta", ""))

            elif t == "response.text.done":
                final_txt = "".join(self._out_transcript_buf)
                self._out_transcript_buf.clear()
                _log("assistant_text_final", {"text": final_txt})

            # --- Tool-calling plumbing (hide function_call traffic from client UI) ---
            elif t == "response.output_item.added":
                if "item" in message and message["item"].get("type") == "function_call":
                    updated_message = None

            elif t == "conversation.item.created":
                item = message.get("item", {})
                if item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    if call_id and call_id not in self._tools_pending:
                        self._tools_pending[call_id] = RTToolCall(call_id, message.get("previous_item_id"))
                    updated_message = None
                elif item.get("type") == "function_call_output":
                    updated_message = None

            elif t in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
                updated_message = None

            elif t == "response.output_item.done":
                item = message.get("item", {})
                if item.get("type") == "function_call" and item.get("name"):
                    try:
                        tool = self.tools[item["name"]]
                        args_s = item.get("arguments", "{}")
                        result = await tool.target(json.loads(args_s))
                    except Exception as e:
                        logger.exception("Tool %s failed: %s", item.get("name"), e)
                        result = ToolResult({"error": str(e)}, ToolResultDirection.TO_SERVER)

                    await server_ws.send_json(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": item["call_id"],
                                "output": result.to_text()
                                if result.destination == ToolResultDirection.TO_SERVER
                                else "",
                            },
                        }
                    )

                    tool_call = self._tools_pending.get(item["call_id"])
                    if result.destination == ToolResultDirection.TO_CLIENT and tool_call:
                        # Optional out-of-band to client (your UI can ignore)
                        await client_ws.send_json(
                            {
                                "type": "extension.middle_tier_tool_response",
                                "previous_item_id": tool_call.previous_id,
                                "tool_name": item["name"],
                                "tool_result": result.to_text(),
                            }
                        )
                    updated_message = None

            elif t == "response.done":
                # Log usage if present
                resp = message.get("response", {})
                usage = resp.get("usage") or {}
                _log(
                    "usage",
                    {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                        "last_user_asr": self._last_asr_text,
                    },
                )

                # If tools were invoked, ask the server to continue and produce final text/audio
                if len(self._tools_pending) > 0:
                    self._tools_pending.clear()
                    await server_ws.send_json({"type": "response.create"})

                # Strip function_call items from response before forwarding to client
                if "response" in message:
                    out = message["response"].get("output", [])
                    if isinstance(out, list) and any(o.get("type") == "function_call" for o in out if isinstance(o, dict)):
                        message["response"]["output"] = [o for o in out if o.get("type") != "function_call"]
                        updated_message = json.dumps(message, ensure_ascii=False)

        return updated_message

    # -------------------------
    # Message processing (client -> server)
    # -------------------------
    async def _process_message_to_server(self, msg: aiohttp.WSMessage, ws: web.WebSocketResponse) -> Optional[str]:
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None

        try:
            message = json.loads(msg.data)
        except Exception:
            return msg.data

        updated_message: Optional[str] = msg.data

        if isinstance(message, dict) and "type" in message and message["type"] == "session.update":
            session = message.get("session", {})

            # Server-enforced config
            if self.system_message is not None:
                session["instructions"] = self.system_message
            if self.temperature is not None:
                session["temperature"] = self.temperature
            if self.max_tokens is not None:
                session["max_response_output_tokens"] = self.max_tokens
            if self.disable_audio is not None:
                session["disable_audio"] = self.disable_audio

            # Voice: server override unless allowed to switch from client
            if self.voice_choice is not None and not self._allow_client_voice_switch:
                session["voice"] = self.voice_choice

            # Tools
            session["tool_choice"] = "auto" if len(self.tools) > 0 else "none"
            session["tools"] = [tool.schema for tool in self.tools.values()]

            # Sensible defaults for realtime UX: server VAD + ASR
            session.setdefault(
                "turn_detection",
                {
                    "type": "server_vad",
                    "silence_duration_ms": self._vad_silence_ms,
                    "prefix_padding_ms": self._vad_prefix_ms,
                    "threshold": self._vad_threshold,
                },
            )
            # Always set transcription language to Azerbaijani
            session["input_audio_transcription"] = {"model": "whisper-1", "language": "az"}

            message["session"] = session
            updated_message = json.dumps(message, ensure_ascii=False)

        return updated_message

    # -------------------------
    # WS proxying (backend <-> Azure)
    # -------------------------
    async def _forward_messages(self, ws: web.WebSocketResponse):
        # Use absolute base_url; aiohttp requires origin-only URL here.
        async with aiohttp.ClientSession(base_url=self.endpoint) as session:
            params = {"api-version": self.api_version, "deployment": self.deployment}
            headers: dict[str, str] = {}

            # Propagate client request id if present
            try:
                if "x-ms-client-request-id" in ws.headers:
                    headers["x-ms-client-request-id"] = ws.headers["x-ms-client-request-id"]
            except Exception:
                pass

            # Auth: support API key or Entra ID token
            if self.key:
                headers["api-key"] = self.key
                # Also add api-key to query for WS friendliness (still over wss://)
                params["api-key"] = self.key
            else:
                try:
                    headers["Authorization"] = f"Bearer {self._token_provider()}"
                except Exception as e:
                    logger.error("Failed to obtain bearer token: %s", e)
                    raise

            # Backoff loop to avoid reconnect storms (helps with RPM limits)
            backoff = 1.0
            while True:
                logger.info(
                    "Connecting to Azure Realtime: %s / %s (api-version=%s)",
                    self.endpoint,
                    self.deployment,
                    self.api_version,
                )
                try:
                    async with session.ws_connect(
                        "/openai/realtime", headers=headers, params=params, heartbeat=20
                    ) as target_ws:
                        logger.info("Realtime handshake OK")

                        async def from_client_to_server():
                            async for cmsg in ws:
                                if cmsg.type == aiohttp.WSMsgType.TEXT:
                                    new_msg = await self._process_message_to_server(cmsg, ws)
                                    if new_msg is not None:
                                        await target_ws.send_str(new_msg)
                                elif cmsg.type == aiohttp.WSMsgType.CLOSE:
                                    break
                                else:
                                    logger.debug("Unexpected client msg type: %s", cmsg.type)
                            try:
                                await target_ws.close()
                            except Exception:
                                pass

                        async def from_server_to_client():
                            async for smsg in target_ws:
                                if smsg.type == aiohttp.WSMsgType.TEXT:
                                    new_msg = await self._process_message_to_client(smsg, ws, target_ws)
                                    if new_msg is not None:
                                        await ws.send_str(new_msg)
                                elif smsg.type == aiohttp.WSMsgType.CLOSE:
                                    break
                                else:
                                    logger.debug("Unexpected server msg type: %s", smsg.type)

                        try:
                            await asyncio.gather(from_client_to_server(), from_server_to_client())
                        except ConnectionResetError:
                            pass
                        return  # clean exit after the two tasks complete

                except aiohttp.WSServerHandshakeError as e:
                    logger.error("Realtime handshake failed: %s (status=%s)", e, getattr(e, "status", "?"))
                    # 401/403 = auth; don't loop forever
                    if getattr(e, "status", None) in (401, 403):
                        raise
                    # exponential backoff for 404/409/429/5xx
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 60.0)
                    continue

                except Exception as e:
                    logger.exception("WS connect error: %s", e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    continue

    async def _websocket_handler(self, request: web.Request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await self._forward_messages(ws)
        return ws

    def attach_to_app(self, app: web.Application, path: str):
        app.router.add_get(path, self._websocket_handler)
