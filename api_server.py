import os
import uuid
import json
import gzip
import asyncio
from typing import AsyncGenerator, Dict, Optional
from pathlib import Path

try:
    # 优先从项目目录的 .env 文件加载环境变量
    # 说明：在模块导入阶段调用，确保后续 os.getenv 能读取到配置
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(dotenv_path=_ENV_PATH, override=False)
    else:
        # 若 .env 不存在，仍允许通过系统环境变量运行
        load_dotenv(override=False)
except Exception:
    # 如果未安装 python-dotenv 或加载失败，不影响通过系统环境变量运行
    pass

import websockets
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import logging

logger = logging.getLogger("voice_clone_api")


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
    """
    构造 TTS WebSocket 请求的业务 JSON。

    输入参数：
    - text: 合成的文本内容（纯文本）
    - voice_type: 声音类型标识（服务端支持的模型/音色）
    - operation: 操作类型，通常为 "submit" 表示提交请求
    - reqid: 本次请求的唯一标识符

    输出结果：
    - dict: 按照服务端协议字段组织的 JSON 对象

    关键逻辑：
    - 将凭证（appid/token/cluster）和音频参数（编码、语速、音量、音高）放入对应的字段
    - 请求体包含文本、文本类型和操作类型
    """
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
    """
    将业务 JSON 序列化并按协议打包为客户端完整请求字节。

    输入参数：
    - payload_json: 业务请求 JSON 字典

    输出结果：
    - bytes: 包含协议头和压缩后的负载的完整请求字节数组

    关键逻辑：
    - JSON 序列化后进行 gzip 压缩
    - 构造默认协议头并附带 4 字节负载长度，再拼接压缩后的负载
    """
    payload_bytes = str.encode(json.dumps(payload_json))
    payload_bytes = gzip.compress(payload_bytes)
    full_client_request = bytearray(default_header)
    full_client_request.extend((len(payload_bytes)).to_bytes(4, 'big'))  # payload size(4 bytes)
    full_client_request.extend(payload_bytes)
    return bytes(full_client_request)


def parse_header_and_payload(res: bytes):
    """
    解析服务端返回的二进制消息，拆分协议头与负载。

    输入参数：
    - res: 原始二进制响应数据

    输出结果：
    - dict: 包含协议版本、头大小、消息类型、标记、序列化方式、压缩方式、保留位、头扩展和负载数据的字典

    关键逻辑：
    - 前 4 字节为固定协议头，后续根据头大小提取扩展，再读取负载区
    - 提取消息类型与特定标记，用于后续分支处理（音频、错误、前端消息等）
    """
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
    """
    流式生成 TTS 音频的异步生成器。

    输入参数：
    - session_id: 会话标识，用于中断/重启控制
    - text: 需要合成的长文本内容
    - voice_type: 声音类型标识

    输出结果：
    - AsyncGenerator[bytes, None]: 迭代返回 MP3 音频片段字节，用于 HTTP 流式响应

    关键逻辑：
    - 建立到 TTS WebSocket 的连接并发送提交请求
    - 轮询接收服务端消息：
      * 音频消息（type 0xb）：解析序列号和片段大小，按片输出
      * 错误消息（type 0xf）：解压错误文本并抛出 HTTP 异常
      * 前端消息（type 0xc）：可忽略，仅用于状态通知
    - 支持外部中断：当会话对应的事件被设置时，主动关闭 WebSocket 连接并结束生成
    - 当序列号为负数（最后一包）时结束流
    """
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
    save_path: Optional[str] = None


@app.post("/api/tts/stream")
async def tts_stream(req: StreamRequest):
    """
    流式 TTS 合成接口。

    输入参数：
    - req: StreamRequest，请求体包含 text、可选的 session_id 与 voice_type

    输出结果：
    - StreamingResponse: 媒体类型为 audio/mpeg 的流式响应，头部包含 X-Session-Id

    关键逻辑：
    - 在未配置凭证时返回 500 错误，避免客户端误保存 JSON 为 MP3
    - 若传入的 session_id 已存在，先中断旧流以允许重启
    - 为当前会话创建中断事件并启动生成器
    """
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

    # 可选的服务端保存：当 save_path 提供时，同时写入文件并打印保存路径
    save_header: Dict[str, str] = {}
    if req.save_path:
        try:
            # 规范化路径并确保目录存在
            base_dir = Path(__file__).parent
            save_path = Path(req.save_path)
            if not save_path.is_absolute():
                save_path = (base_dir / save_path).resolve()
            save_path.parent.mkdir(parents=True, exist_ok=True)

            # 包装生成器：边写入文件边向客户端返回流
            def wrap_with_save(original_gen: AsyncGenerator[bytes, None], fp: Path):
                """
                将原始音频生成器包装为“写文件+转发”的生成器。

                输入参数：
                - original_gen: 原始音频分片异步生成器
                - fp: 保存文件的绝对路径

                输出结果：
                - AsyncGenerator[bytes, None]: 每次迭代返回同样的音频分片，同时写入到文件

                关键逻辑：
                - 在 finally 中关闭文件句柄并打印保存路径
                - 即便发生中断或错误，只要有已写入的部分，也会打印路径供排查
                """
                async def _wrapped() -> AsyncGenerator[bytes, None]:
                    f = open(fp, "wb")
                    try:
                        async for chunk in original_gen:
                            if chunk:
                                f.write(chunk)
                                yield chunk
                    finally:
                        try:
                            f.close()
                        except Exception:
                            pass
                        abs_path = str(fp)
                        # 控制台打印保存路径
                        print(f"[TTS] 音频已保存: {abs_path}")
                        logger.info("audio saved to %s", abs_path)
                return _wrapped()

            gen = wrap_with_save(gen, save_path)
            save_header["X-Save-Path"] = str(save_path)
        except Exception as e:
            # 保存失败不影响流式返回，仅在日志中提示
            logger.error("failed to prepare save_path: %s", e)

    headers = {"X-Session-Id": session_id, **save_header}
    return StreamingResponse(gen, media_type="audio/mpeg", headers=headers)


class InterruptRequest(BaseModel):
    session_id: str


@app.post("/api/tts/interrupt")
async def tts_interrupt(req: InterruptRequest):
    """
    中断指定会话的流式合成。

    输入参数：
    - req: InterruptRequest，请求体包含 session_id

    输出结果：
    - JSON: {"ok": True} 或 404（会话未找到）

    关键逻辑：
    - 读取全局会话事件并调用 set() 通知生成器结束
    """
    ev = sessions.get(req.session_id)
    if not ev:
        return JSONResponse(status_code=404, content={"ok": False, "error": "session not found"})
    ev.set()
    return {"ok": True}


@app.get("/api/healthz")
async def healthz():
    """
    服务健康检查接口。

    输入参数：
    - 无

    输出结果：
    - JSON: {"status": "ok"}

    关键逻辑：
    - 仅用于验证服务可用性，不依赖外部 TTS 服务
    """
    return {"status": "ok"}