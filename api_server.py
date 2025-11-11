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
from fastapi import FastAPI, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import logging
from logging.handlers import RotatingFileHandler
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("voice_clone_api")
logger.setLevel(logging.INFO)
try:
    _BASE_DIR = Path(__file__).parent
    _LOG_DIR = _BASE_DIR / "output"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = _LOG_DIR / "server.log"
    _FH = RotatingFileHandler(str(_LOG_PATH), maxBytes=2097152, backupCount=3)
    _FH.setLevel(logging.INFO)
    _FH.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        logger.addHandler(_FH)
except Exception:
    # 日志初始化失败不影响主流程
    pass
logger.setLevel(logging.INFO)
# 初始化文件级日志；在项目 output 目录落盘，便于排查 HTTP 端点行为
try:
    _base_dir = Path(__file__).parent
    _log_dir = _base_dir / "output"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_path = _log_dir / "server.log"
    _fh = RotatingFileHandler(str(_log_path), maxBytes=2 * 1024 * 1024, backupCount=3)
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        logger.addHandler(_fh)
except Exception:
    # 日志初始化失败不影响主流程
    pass


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


@app.post("/api/tts/stream_segments")
async def tts_stream_segments(request: Request, session_id: Optional[str] = None, voice_type: Optional[str] = None, save_path: Optional[str] = None, save_mode: Optional[str] = None):
    """
    流式输入多段文本并合并为单一音频流（多流、单文件）。

    输入参数（通过查询字符串传入，可选）：
    - session_id: 基础会话标识，用于标注本次合成流（实际每段内部使用子会话）
    - voice_type: 覆盖默认音色类型
    - save_path: 服务端保存的目标文件路径（相对路径相对于项目根目录）
    - save_mode: 保存模式（"overwrite" 或 "append"，默认 "overwrite"）。
      - overwrite: 跨请求前一次文件内容会被清空，本次生成的音频覆盖写入
      - append: 跨请求将新生成的音频分片追加到已有文件末尾

    请求体（流式）：
    - 支持按行输入文本片段；每行可以是纯文本，或 NDJSON 的一行对象，如 {"text":"你好"}
      建议使用 Content-Type: application/x-ndjson 并以换行分隔每一段。

    输出结果：
    - StreamingResponse: 媒体类型为 audio/mpeg 的连续音频流；当提供 save_path 时，响应头包含 X-Save-Path。

    关键逻辑：
    - 从 HTTP 请求体的流中逐行读取片段；对每个片段，调用上游 TTS 生成器顺序产出音频分片
    - 将所有片段的音频分片连续写入到 HTTP 响应与（可选的）服务端文件，形成单一文件
    - 最终关闭文件并打印保存路径
    """
    if not ACCESS_TOKEN or not APP_ID:
        raise HTTPException(status_code=500, detail="Server isn't configured: missing APPID/TOKEN env")

    base_dir = Path(__file__).parent
    base_session = session_id or str(uuid.uuid4())
    voice_type = voice_type or DEFAULT_VOICE_TYPE

    # 解析 save_path 并准备文件
    save_abs: Optional[Path] = None
    if save_path:
        save_abs = Path(save_path)
        if not save_abs.is_absolute():
            save_abs = (base_dir / save_abs).resolve()
        save_abs.parent.mkdir(parents=True, exist_ok=True)

    # 规范化保存模式
    save_mode = (save_mode or "overwrite").lower()
    if save_mode not in ("overwrite", "append"):
        save_mode = "overwrite"
    if save_abs:
        logger.info(f"[HTTP] 保存模式: {save_mode}, 目标文件: {str(save_abs)}")
        # 覆盖模式下：在流式响应前预先截断文件，避免后续写入被历史内容影响
        if save_mode == "overwrite":
            try:
                with open(save_abs, "wb") as wf:
                    pass
                logger.info(f"[HTTP] 目标文件已截断(覆盖写入): {str(save_abs)}")
            except Exception as fe:
                logger.error(f"[HTTP] 目标文件截断失败: {fe}")

    async def iter_segments() -> AsyncGenerator[str, None]:
        buffer = b""
        async for chunk in request.stream():
            if not chunk:
                continue
            buffer += chunk
            while True:
                idx = buffer.find(b"\n")
                if idx == -1:
                    break
                line = buffer[:idx].decode("utf-8", errors="ignore").strip()
                buffer = buffer[idx + 1:]
                if not line:
                    continue
                # 支持纯文本或 NDJSON
                text = line
                if line.startswith("{") and line.endswith("}"):
                    try:
                        obj = json.loads(line)
                        text = str(obj.get("text", ""))
                    except Exception:
                        text = line
                if text:
                    yield text
        # 处理剩余缓冲
        tail = buffer.decode("utf-8", errors="ignore").strip()
        if tail:
            text = tail
            if tail.startswith("{") and tail.endswith("}"):
                try:
                    obj = json.loads(tail)
                    text = str(obj.get("text", ""))
                except Exception:
                    text = tail
            if text:
                yield text

    async def merged_audio_prepared(prepared_segments: list[str]) -> AsyncGenerator[bytes, None]:
        """
        合并音频的生成器（重构版）。

        设计思路：
        - 在开始 StreamingResponse 之前，已完成首段的预检（拿到首个音频片段），确保客户端不会出现“200但0字节”的长时间等待。
        - 对每段增加详细日志记录；错误直接抛出，由上层决定是否终止或返回 JSON。
        - 可选将所有片段的字节写入服务端文件，实现单文件保存。

        参数：
        - prepared_segments: 已聚合与清洗的文本段列表。

        返回：
        - 异步字节生成器：连续输出所有段的 mp3 字节。
        """
        f = None
        # 统一采用追加写入，避免首段预检阶段已写入的数据被覆盖。
        # 若需要覆盖行为，已在开始阶段通过 save_mode == overwrite 进行一次性截断。
        if save_abs:
            try:
                f = open(save_abs, "ab")
            except Exception as fe:
                logger.error(f"[HTTP] 打开文件失败(合并生成器初始化): {fe}")

        async def flush_text(text_to_speak: str, idx: int):
            """
            生成指定文本段的音频并逐片输出。

            输入：
            - text_to_speak: 文本段
            - idx: 段序号

            输出：
            - 异步字节迭代（mp3 分片）
            """
            seg_session = f"{base_session}-{idx}"
            logger.info(f"[HTTP] 开始生成段 {idx}: len={len(text_to_speak)} text='{text_to_speak[:50]}'")
            gen = stream_tts_generator(session_id=seg_session, text=text_to_speak, voice_type=voice_type)
            try:
                async for chunk in gen:
                    if not chunk:
                        continue
                    if f:
                        try:
                            f.write(chunk)
                        except Exception as fe:
                            logger.error(f"[HTTP] 文件写入失败 段{idx}: {fe}")
                    logger.info(f"[HTTP] 段{idx} 输出 {len(chunk)} 字节")
                    yield chunk
                logger.info(f"[HTTP] 完成段 {idx}")
            except HTTPException as e:
                logger.error(f"[HTTP] 段 {idx} 生成失败: {e.detail}")
                raise

        try:
            # 顺序生成每段
            for i, seg in enumerate(prepared_segments, start=1):
                async for chunk in flush_text(seg, i):
                    yield chunk
        finally:
            if f:
                try:
                    f.close()
                except Exception:
                    pass
                logger.info(f"[HTTP] 合并音频已保存: {str(save_abs)}")
    # --- 重构：在开始流式返回前，先收集片段并做首段预检 ---
    # 1) 收集并清洗片段
    min_len = 2
    raw_segments: list[str] = []
    async for s in iter_segments():
        raw_segments.append(s)
    # 聚合过短片段
    prepared_segments: list[str] = []
    pending = ""
    for s in raw_segments:
        pending = (pending + s).strip()
        if len(pending) < min_len:
            continue
        prepared_segments.append(pending)
        pending = ""
    if pending.strip():
        prepared_segments.append(pending.strip())

    if not prepared_segments:
        # 没有有效片段，直接返回 JSON 错误，避免启动空的音频流
        logger.error("[HTTP] 无有效文本片段，返回 400")
        return JSONResponse(status_code=400, content={"error": "no valid text segments"})

    # 2) 首段预检：尝试拿到第一个音频分片
    first_seg = prepared_segments[0]
    seg_session = f"{base_session}-1"
    gen = stream_tts_generator(session_id=seg_session, text=first_seg, voice_type=voice_type)

    async def prime_first_chunk():
        """
        拉取首段的首个音频分片，若失败则抛出异常。

        目的：
        - 确保客户端在收到 200 音频响应后能尽快读到字节，避免“200但0字节”的卡顿。
        - 若上游鉴权/限流等导致失败，直接返回 JSON，便于用户排查。
        """
        try:
            # 设置预检超时，避免长时间等待
            async def _next_chunk():
                async for c in gen:
                    if c:
                        return c
                return None
            return await asyncio.wait_for(_next_chunk(), timeout=20.0)
        except HTTPException as e:
            logger.error(f"[HTTP] 首段预检失败: {e.detail}")
            raise
        except asyncio.TimeoutError:
            logger.error("[HTTP] 首段预检超时 20s")
            raise HTTPException(status_code=504, detail="upstream timeout on first segment")

    # 执行预检
    try:
        first_chunk = await prime_first_chunk()
        if not first_chunk:
            logger.error("[HTTP] 上游未返回任何音频字节")
            return JSONResponse(status_code=502, content={"error": "upstream produced no audio"})
    except HTTPException as e:
        # 预检失败直接返回 JSON 错误
        code = e.status_code if hasattr(e, "status_code") else 500
        return JSONResponse(status_code=code, content={"error": e.detail})

    # 3) 预检成功后开始流式返回：先输出首个分片，再继续其余内容与其他段
    async def merged_audio_after_prime() -> AsyncGenerator[bytes, None]:
        # 写首个分片到文件并返回
        if save_abs:
            try:
                with open(save_abs, "ab") as wf:
                    wf.write(first_chunk)
            except Exception as fe:
                logger.error(f"[HTTP] 首段首片写入失败: {fe}")
        yield first_chunk

        # 继续当前首段剩余片段
        async def flush_remaining_current():
            try:
                async for c in gen:
                    if not c:
                        continue
                    if save_abs:
                        try:
                            with open(save_abs, "ab") as wf:
                                wf.write(c)
                        except Exception as fe:
                            logger.error(f"[HTTP] 文件写入失败(首段剩余): {fe}")
                    yield c
            except HTTPException as e:
                logger.error(f"[HTTP] 首段剩余生成失败: {e.detail}")
                return

        async for c in flush_remaining_current():
            yield c

        # 处理其它段
        if len(prepared_segments) > 1:
            rest = prepared_segments[1:]
            async for c in merged_audio_prepared(rest):
                yield c

    headers = {"X-Session-Id": base_session}
    if save_abs:
        headers["X-Save-Path"] = str(save_abs)
        headers["X-Save-Mode"] = save_mode
    return StreamingResponse(merged_audio_after_prime(), media_type="audio/mpeg", headers=headers)


@app.websocket("/ws/tts/stream_segments")
async def ws_tts_stream_segments(websocket: WebSocket):
    """
    WebSocket 双向流式：客户端以逐段文本消息发送，服务端边合成边返回音频分片，最终合并保存为单文件。

    查询参数（通过 WebSocket URL 传入）：
    - session_id: 会话基准 ID（每段内部派生子会话）
    - voice_type: 覆盖默认音色类型
    - save_path: 服务端保存的目标文件路径（相对路径按项目根目录解析）

    客户端消息：
    - 文本帧（纯文本或 JSON 行 {"text":"..."}），每条视为一个语音段。
    - 发送完所有段后主动关闭连接，或发送空文本结束。

    服务端返回：
    - 二进制帧：上游 TTS 音频分片（mp3 字节），按段顺序连续推送。
    - 最后一条返回不会特别标记；连接关闭代表推送结束。

    关键逻辑：
    - 接受连接后，按查询参数准备可选的保存文件；每个音频分片会同步写入文件与发送给客户端。
    - 对每个收到的文本消息，串行调用上游生成器；若某段失败，记录并继续后续段。
    """
    await websocket.accept()
    if not ACCESS_TOKEN or not APP_ID:
        await websocket.close(code=1011)
        return

    qp = websocket.query_params
    base_session = qp.get("session_id") or str(uuid.uuid4())
    voice_type = qp.get("voice_type") or DEFAULT_VOICE_TYPE
    save_path = qp.get("save_path")

    base_dir = Path(__file__).parent
    f = None
    save_abs: Optional[Path] = None
    if save_path:
        save_abs = Path(save_path)
        if not save_abs.is_absolute():
            save_abs = (base_dir / save_abs).resolve()
        save_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            f = open(save_abs, "wb")
        except Exception:
            f = None

    seg_idx = 0
    try:
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            text = msg.strip()
            if not text:
                break
            if text.startswith("{") and text.endswith("}"):
                try:
                    obj = json.loads(text)
                    text = str(obj.get("text", "")).strip()
                except Exception:
                    pass
            if not text:
                continue

            seg_idx += 1
            seg_session = f"{base_session}-{seg_idx}"
            try:
                gen = stream_tts_generator(session_id=seg_session, text=text, voice_type=voice_type)
                async for chunk in gen:
                    if not chunk:
                        continue
                    # 发给客户端
                    await websocket.send_bytes(chunk)
                    # 可选写入文件
                    if f:
                        try:
                            f.write(chunk)
                        except Exception:
                            pass
            except HTTPException as e:
                # 将错误以文本消息返回，继续下一段
                try:
                    await websocket.send_text(f"ERROR: {e.detail}")
                except Exception:
                    pass
                continue
    finally:
        if f:
            try:
                f.close()
            except Exception:
                pass
            print(f"[TTS][WS] 合并音频已保存: {str(save_abs)}")
        try:
            await websocket.close()
        except Exception:
            pass