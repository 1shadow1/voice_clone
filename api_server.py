import os
import uuid
import json
import gzip
import asyncio
from typing import AsyncGenerator, Dict, Optional

import websockets
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel


# ======= Config =======
APP_ID = os.getenv("OPENSPEECH_APPID", "")
ACCESS_TOKEN = os.getenv("OPENSPEECH_TOKEN", "")
CLUSTER = os.getenv("OPENSPEECH_CLUSTER", "volcano_icl")
DEFAULT_VOICE_TYPE = os.getenv("OPENSPEECH_VOICE_TYPE", "S_nLVvYpzH1")
HOST = os.getenv("OPENSPEECH_HOST", "openspeech.bytedance.com")
API_URL = f"wss://{HOST}/api/v1/tts/ws_binary"


# ======= Protocol constants (same as demo) =======
MESSAGE_TYPES = {11: "audio-only server response", 12: "frontend server response", 15: "error message from server"}
MESSAGE_TYPE_SPECIFIC_FLAGS = {0: "no sequence number", 1: "sequence number > 0",
                               2: "last message from server (seq < 0)", 3: "sequence number < 0"}
MESSAGE_SERIALIZATION_METHODS = {0: "no serialization", 1: "JSON", 15: "custom type"}
MESSAGE_COMPRESSIONS = {0: "no compression", 1: "gzip", 15: "custom compression method"}

# version: b0001 (4 bits), header size: b0001, message type: client request, JSON + gzip
default_header = bytearray(b'\x11\x10\x11\x00')


def build_request_json(text: str, voice_type: str, operation: str, reqid: str) -> dict:
    return {
        "app": {
            "appid": APP_ID,
            "token": ACCESS_TOKEN,
            "cluster": CLUSTER,
        },
        "user": {
            "uid": "voice_clone_api"
        },
        "audio": {
            "voice_type": voice_type,
            "encoding": "mp3",
            "speed_ratio": 1.0,
            "volume_ratio": 1.0,
            "pitch_ratio": 1.0,
        },
        "request": {
            "reqid": reqid,
            "text": text,
            "text_type": "plain",
            "operation": operation,
        }
    }


def pack_full_client_request(payload_json: dict) -> bytes:
    payload_bytes = str.encode(json.dumps(payload_json))
    payload_bytes = gzip.compress(payload_bytes)
    full_client_request = bytearray(default_header)
    full_client_request.extend((len(payload_bytes)).to_bytes(4, 'big'))  # payload size(4 bytes)
    full_client_request.extend(payload_bytes)
    return bytes(full_client_request)


def parse_header_and_payload(res: bytes):
    protocol_version = res[0] >> 4
    header_size = res[0] & 0x0f
    message_type = res[1] >> 4
    message_type_specific_flags = res[1] & 0x0f
    serialization_method = res[2] >> 4
    message_compression = res[2] & 0x0f
    reserved = res[3]
    header_extensions = res[4:header_size * 4]
    payload = res[header_size * 4:]
    return {
        "protocol_version": protocol_version,
        "header_size": header_size,
        "message_type": message_type,
        "flags": message_type_specific_flags,
        "serialization_method": serialization_method,
        "compression": message_compression,
        "reserved": reserved,
        "header_extensions": header_extensions,
        "payload": payload,
    }


# ======= Interrupt management =======
sessions: Dict[str, asyncio.Event] = {}


async def stream_tts_generator(session_id: str, text: str, voice_type: str) -> AsyncGenerator[bytes, None]:
    cancel_event = sessions.get(session_id)
    if cancel_event is None:
        cancel_event = asyncio.Event()
        sessions[session_id] = cancel_event

    reqid = str(uuid.uuid4())
    submit_json = build_request_json(text=text, voice_type=voice_type, operation="submit", reqid=reqid)
    request_bytes = pack_full_client_request(submit_json)

    header = {"Authorization": f"Bearer; {ACCESS_TOKEN}"}
    ws = None
    try:
        async with websockets.connect(API_URL, additional_headers=header, ping_interval=None) as ws:
            await ws.send(request_bytes)
            while True:
                if cancel_event.is_set():
                    # Close connection and end stream
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    break

                res = await ws.recv()
                parsed = parse_header_and_payload(res)
                mt = parsed["message_type"]
                compression = parsed["compression"]
                payload = parsed["payload"]

                if mt == 0xb:  # audio-only server response
                    if parsed["flags"] == 0:
                        # ACK with zero payload
                        continue
                    sequence_number = int.from_bytes(payload[:4], "big", signed=True)
                    payload_size = int.from_bytes(payload[4:8], "big", signed=False)
                    audio_chunk = payload[8:]
                    # Yield mp3 bytes to client
                    if audio_chunk:
                        yield audio_chunk
                    if sequence_number < 0:
                        break
                elif mt == 0xf:  # error
                    code = int.from_bytes(payload[:4], "big", signed=False)
                    msg_size = int.from_bytes(payload[4:8], "big", signed=False)
                    error_msg = payload[8:]
                    if compression == 1:
                        error_msg = gzip.decompress(error_msg)
                    msg_text = str(error_msg, "utf-8")
                    raise HTTPException(status_code=401 if code == 45000010 else 400, detail=msg_text)
                elif mt == 0xc:  # frontend message
                    body = payload[4:]
                    if compression == 1:
                        body = gzip.decompress(body)
                    # Optionally, could emit as custom metadata; ignore for audio stream
                    continue
                else:
                    raise HTTPException(status_code=500, detail="Undefined message type from TTS server")
    finally:
        # Cleanup session
        sessions.pop(session_id, None)


# ======= FastAPI app and endpoints =======
app = FastAPI(title="Voice Clone TTS API", version="0.1.0")


class StreamRequest(BaseModel):
    text: str
    session_id: Optional[str] = None
    voice_type: Optional[str] = None


@app.post("/api/tts/stream")
async def tts_stream(req: StreamRequest):
    if not ACCESS_TOKEN or not APP_ID:
        raise HTTPException(status_code=500, detail="Server isn't configured: missing APPID/TOKEN env")
    session_id = req.session_id or str(uuid.uuid4())
    # If same session exists, interrupt it first to allow restart
    existing = sessions.get(session_id)
    if existing:
        existing.set()
        await asyncio.sleep(0)
    # Ensure session record exists
    sessions[session_id] = asyncio.Event()
    voice_type = req.voice_type or DEFAULT_VOICE_TYPE
    gen = stream_tts_generator(session_id=session_id, text=req.text, voice_type=voice_type)
    headers = {"X-Session-Id": session_id}
    return StreamingResponse(gen, media_type="audio/mpeg", headers=headers)


class InterruptRequest(BaseModel):
    session_id: str


@app.post("/api/tts/interrupt")
async def tts_interrupt(req: InterruptRequest):
    ev = sessions.get(req.session_id)
    if not ev:
        return JSONResponse(status_code=404, content={"ok": False, "error": "session not found"})
    ev.set()
    return {"ok": True}


@app.get("/api/healthz")
async def healthz():
    return {"status": "ok"}