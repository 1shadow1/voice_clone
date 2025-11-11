# Voice Clone WebSocket TTS Demo

本项目演示如何通过 WebSocket 二进制协议调用字节跳动 OpenSpeech 文本转语音（TTS）服务，代码位于 `ws_test.py`，包含 `submit` 和 `query` 两种操作，并解析服务端返回的音频流。

## 环境要求
- Python `3.8+`（本地已在 `3.13` 验证）
- 依赖：`websockets`

安装依赖：

```
pip install websockets==15.0.1
```

说明：`asyncio` 是 Python 标准库，不需要单独安装；脚本注释中的 `pip install asyncio` 可忽略。

## 配置凭证
脚本与服务均支持从环境变量读取凭证（推荐），未设置时脚本会使用示例默认值：
- `OPENSPEECH_APPID`：控制台创建应用获得
- `OPENSPEECH_TOKEN`：控制台生成的 Access Token（Bearer Token）
- `OPENSPEECH_CLUSTER`：如 `volcano_icl`（确保服务已开通并与令牌权限匹配）
- `OPENSPEECH_VOICE_TYPE`：音色标识（需有权限）
- `OPENSPEECH_HOST`：`openspeech.bytedance.com` 或海外 `openspeech.byteoversea.com`

域名说明：
- 国内/火山引擎：`wss://openspeech.bytedance.com/api/v1/tts/ws_binary`
- 海外/BytePlus：`wss://openspeech.byteoversea.com/api/v1/tts/ws_binary`

令牌与域名需一致。如果你的令牌来自 BytePlus（海外），请切换到 `byteoversea.com` 域名。

## 鉴权要点
- WebSocket 握手时需要设置 HTTP 头：`Authorization: Bearer; <token>`（注意分号分隔）。
- 请求体（JSON）的 `app.token` 也应填写相同的令牌，`app.appid` 为你的应用 ID，`app.cluster` 为已开通的集群。
- 示例（代码内已设置）：

```
header = {"Authorization": f"Bearer; {token}"}
async with websockets.connect(api_url, additional_headers=header, ping_interval=None) as ws:
    ...
```

## 运行
在项目根目录执行：

```
python ws_test.py
```

脚本会依次执行：
- `submit`：提交任务，持续接收音频分片并写入文件
- `query`：查询任务结果或返回前端信息（视服务配置而定）

输出文件：
- `test_submit.mp3`
- `test_query.mp3`

环境变量支持（ws_test.py 已内置）：
```
export OPENSPEECH_APPID=你的appid
export OPENSPEECH_TOKEN=你的access_token
export OPENSPEECH_CLUSTER=volcano_icl
export OPENSPEECH_VOICE_TYPE=S_nLVvYpzH1
export OPENSPEECH_HOST=openspeech.bytedance.com
```
随后运行 `python ws_test.py` 即会自动读取上述环境变量。

## API 服务（流式与打断）
本项目提供基于 FastAPI 的 HTTP 接口，实现：
- 通过 API 请求触发流式 TTS（边生成边返回音频）
- 随时打断当前会话的生成任务，并重新开始新的任务（打断机制）

启动服务：

```
pip install -r requirements.txt
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

支持 `.env` 文件配置（推荐）：

```
# 在项目根目录创建 /srv/voice_clone/.env 并填入：
OPENSPEECH_APPID="你的appid"
OPENSPEECH_TOKEN="你的access_token"
OPENSPEECH_CLUSTER="volcano_icl"
OPENSPEECH_VOICE_TYPE="S_nLVvYpzH1"
OPENSPEECH_HOST="openspeech.bytedance.com" # 海外：openspeech.byteoversea.com

# 然后直接启动，不再需要在命令行注入这些环境变量：
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

说明：服务在启动时会优先读取项目目录的 `.env`（通过 `python-dotenv`），如果 `.env` 不存在则读取系统环境变量。

端口说明：如果 `8000` 被占用，可改用 `8013`，示例：
```
uvicorn api_server:app --host 127.0.0.1 --port 8013
```

接口说明：
- `POST /api/tts/stream`：发起流式合成并返回 `audio/mpeg` 流
  - 请求体（JSON）：
    - `text`（必填）：要合成的文本
    - `session_id`（可选）：会话标识；如传入相同 `session_id` 将打断先前生成并重启
    - `voice_type`（可选）：覆盖默认音色
  - 响应：`200` 流式返回音频，响应头附带 `X-Session-Id`
- `POST /api/tts/interrupt`：打断指定 `session_id` 的当前生成任务
  - 请求体（JSON）：`{ "session_id": "..." }`
  - 响应：`{ "ok": true }` 或 `404`（会话不存在）
- `GET /api/healthz`：服务健康检查

示例（curl 下载到文件）：

```
# 启动流式合成，并写入 out.mp3
curl --no-buffer -X POST \
  http://localhost:8000/api/tts/stream \
  -H 'Content-Type: application/json' \
  -d '{"text":"字节跳动语音合成。","session_id":"demo"}' \
  --output out.mp3

# 在生成过程中打断
curl -X POST http://localhost:8000/api/tts/interrupt \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo"}'

# 使用相同 session_id 重新开始新任务（会自动打断旧任务）
curl --no-buffer -X POST \
  http://localhost:8000/api/tts/stream \
  -H 'Content-Type: application/json' \
  -d '{"text":"新的文本","session_id":"demo"}' \
--output out2.mp3
```

示例（长文本流式与延迟打断）：
```
# 长文本（英文）流式合成，并写入到 output/long_stream_1.mp3
BODY='{"text":"This is a test content used to test the technological breakthroughs and specific implementation of artificial intelligence in the field of sound cloning. The technical content includes streaming, interruption and restart, etc.", "session_id":"session-long-1"}'
curl --no-buffer -D /tmp/headers_long.txt \
  -o output/long_stream_1.mp3 \
  -X POST 'http://127.0.0.1:8013/api/tts/stream' \
  -H 'Content-Type: application/json' \
  --data "$BODY"

# 后台启动长文本流式请求，2 秒后打断并检查文件大小
BODY='{"text":"This is a test content used to test the technological breakthroughs and specific implementation of artificial intelligence in the field of sound cloning. The technical content includes streaming, interruption and restart, etc.", "session_id":"session-long-2sec"}'
(curl -sS -o output/long_stream_2sec.mp3 -X POST 'http://127.0.0.1:8013/api/tts/stream' -H 'Content-Type: application/json' --data "$BODY" & echo $! > /tmp/stream_pid.txt)
sleep 2
curl -sS -X POST 'http://127.0.0.1:8013/api/tts/interrupt' -H 'Content-Type: application/json' --data '{"session_id":"session-long-2sec"}'
ls -lh output/long_stream_2sec.mp3
```

说明：
- 短文本往往一次性返回，较难演示中断；使用更长文本或延后打断更直观。
- 中断发生后，如果音频片段尚未写入，输出文件可能不存在或较小，这属正常现象。
- 在保存为 mp3 之前，建议先检查响应头：若 `content-type` 为 `application/json`，说明是错误信息（例如未配置凭证），不要将其保存为 `.mp3`。

示例（Python 客户端流式保存）：

```
import requests

url = 'http://localhost:8000/api/tts/stream'
payload = {"text": "流式音频测试", "session_id": "demo"}
with requests.post(url, json=payload, stream=True) as r:
    r.raise_for_status()
    sid = r.headers.get('X-Session-Id')
    print('session id:', sid)
    with open('out.mp3', 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

# 打断当前任务
requests.post('http://localhost:8000/api/tts/interrupt', json={"session_id": "demo"})
```

实现要点：
- 每个 `session_id` 绑定一个中断事件；打断接口会置位该事件，流式生成立即结束。
- 流式端点在收到相同 `session_id` 会先打断旧任务，再启动新任务，确保“随时打断并重启”。
- WebSocket 连接按官方协议发送 `submit` 请求，服务端以分片返回；每片追加到 HTTP 响应流中。

## 快速开始（HTTP 流式）

1. 安装依赖并配置凭证：
   ```bash
   pip install -r requirements.txt
   # 在项目根目录创建 .env（推荐）
   cat > .env <<'EOF'
   OPENSPEECH_APPID="你的appid"
   OPENSPEECH_TOKEN="你的access_token"
   OPENSPEECH_CLUSTER="volcano_icl"
   OPENSPEECH_VOICE_TYPE="S_nLVvYpzH1"
   OPENSPEECH_HOST="openspeech.bytedance.com"
   EOF
   ```

2. 启动服务（示例使用 8014 端口）：
   ```bash
   uvicorn api_server:app --host 127.0.0.1 --port 8014
   ```

3. 最小化请求（HTTP 行流式，服务端与客户端各保存单文件）：
   ```bash
   SEGMENTS='{"text":"你好"}\n{"text":"我是一名人工助手"}\n{"text":"请问有什么可以帮你的吗？"}'
   printf "%s\n" "$SEGMENTS" | curl -sS -D output/segments_headers.txt \
     -X POST 'http://127.0.0.1:8014/api/tts/stream_segments?session_id=quickstart&save_path=output/quickstart.mp3' \
     -H 'Content-Type: application/x-ndjson' \
     --data-binary @- --output output/quickstart_client.mp3

   # 验证输出与响应头
   sed -n '1,80p' output/segments_headers.txt
   file output/quickstart.mp3 output/quickstart_client.mp3
   ffprobe -v error -show_entries stream=codec_name,codec_type,sample_rate,channels,bit_rate \
     -of default=noprint_wrappers=1:nokey=1 output/quickstart_client.mp3 | head -n 10
   ```

4. 跨请求追加（首次覆盖、随后追加）：
   ```bash
   # 第一次覆盖（创建/清空目标文件）
   printf '%s\n' '{"text":"追加第一段"}' | curl -s -N -X POST \
     'http://127.0.0.1:8014/api/tts/stream_segments?session_id=session_demo_append&voice_type=S_nLVvYpzH1&save_path=output/append_demo.mp3&save_mode=overwrite' \
     -H 'Content-Type: application/x-ndjson' --data-binary @- \
     --output output/append_client1.mp3 -D output/append_headers1.txt

   # 第二次追加（在已有文件末尾追加）
   printf '%s\n' '{"text":"追加第二段"}' | curl -s -N -X POST \
     'http://127.0.0.1:8014/api/tts/stream_segments?session_id=session_demo_append&voice_type=S_nLVvYpzH1&save_path=output/append_demo.mp3&save_mode=append' \
     -H 'Content-Type: application/x-ndjson' --data-binary @- \
     --output output/append_client2.mp3 -D output/append_headers2.txt

   # 验证文件大小增长（证明已追加）
   stat -c '%s' output/append_demo.mp3
   ```

## 多流合并（单文件）
本项目提供两种“流式输入 + 流式输出 + 单文件保存”的方式，用于一次请求内顺序合成多个文本片段，并将所有音频分片合并为一个文件：

- `POST /api/tts/stream_segments`（HTTP 行流式）
  - 请求体：按行发送文本片段；建议使用 `Content-Type: application/x-ndjson`，每行可以是纯文本或一行 NDJSON（例如：`{"text":"你好"}`）。
  - 响应：`audio/mpeg` 的连续字节流；若传入 `save_path`，服务端会将所有片段的音频分片顺序写入该路径，并在响应头返回 `X-Save-Path`。
  - 查询参数（可选）：`session_id`、`voice_type`、`save_path`、`save_mode`。
    - `save_mode=overwrite|append`：跨请求保存模式；`overwrite` 在响应开始前截断目标文件，`append` 将本次生成的音频追加到既有文件末尾。

- `GET /ws/tts/stream_segments`（WebSocket 双向流式）
  - 客户端逐条发送“文本帧”（纯文本或 `{"text":"..."}`），服务端以“二进制帧”持续返回音频分片，客户端本地与服务端的 `save_path` 均可形成单一合并文件。
  - 查询参数与 HTTP 端点一致：`session_id`、`voice_type`、`save_path`。
  - 当客户端发送空文本帧或关闭连接时，表示输入结束；服务端完成收尾后关闭连接。

### 示例：HTTP 行流式（客户端与服务端各保存单文件）

```
# 启动服务（示例使用 8014 端口，也可用 8000/8013）
uvicorn api_server:app --host 127.0.0.1 --port 8014

# 逐行发送多个片段，服务端合并保存到 output/merged.mp3，客户端保存到 output/merged_client.mp3
SEGMENTS="你好！\n很高兴认识你！\n这是一次流式请求验证。"
printf "%s\n" "$SEGMENTS" | curl -sS -D headers_segments.txt \
  -X POST "http://127.0.0.1:8014/api/tts/stream_segments?session_id=multi&save_path=output/merged.mp3" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @- \
  --output output/merged_client.mp3

# 验证
sed -n "1,80p" headers_segments.txt     # 应包含 content-type: audio/mpeg、x-session-id、x-save-path
ls -lh output/merged.mp3 output/merged_client.mp3
file output/merged.mp3 output/merged_client.mp3
```

#### API 参考：POST /api/tts/stream_segments

- 路径与方法：`POST /api/tts/stream_segments`
- 请求内容：按行发送文本片段，`Content-Type: application/x-ndjson`
  - 每行可为纯文本（服务端按默认音色合成）或 NDJSON（推荐）：`{"text":"..."}`
- 查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | `string` | 否 | 自动生成 | 会话标识；相同 `session_id` 的请求会打断前次任务并重启 |
| `voice_type` | `string` | 否 | `.env` 或默认 | 覆盖默认音色 |
| `save_path` | `string` | 否 | 无 | 服务端将所有音频分片顺序写入该文件路径 |
| `save_mode` | `string` | 否 | `overwrite` | 跨请求保存模式：`overwrite` 在响应开始前截断目标文件；`append` 追加到既有文件末尾 |

- 响应头（成功时）：

| 响应头 | 示例值 | 说明 |
| --- | --- | --- |
| `Content-Type` | `audio/mpeg` | HTTP 流式返回 MP3 音频字节 |
| `X-Session-Id` | `session_demo` | 本次会话 ID |
| `X-Save-Path` | `/srv/voice_clone/output/merged.mp3` | 服务端合并写入的文件路径（传入 `save_path` 时返回） |
| `X-Save-Mode` | `append`/`overwrite` | 服务端实际采用的保存策略（传入 `save_mode` 时返回） |

- 错误响应：`Content-Type: application/json`，包含错误码与信息；若收到该类型，请勿保存为 `.mp3`。

#### 持续追加（同一请求）
在同一 HTTP 连接中持续追加片段，有两种常见方式：

1) 命名管道（适合长时间、交互式追加）
```bash
mkfifo /tmp/tts_segments.fifo
curl -v -N -X POST 'http://127.0.0.1:8014/api/tts/stream_segments?session_id=session-demo&voice_type=S_nLVvYpzH1&save_path=output/pipe_merged.mp3' \
  -H 'Content-Type: application/x-ndjson' --data-binary @/tmp/tts_segments.fifo \
  --output output/pipe_merged_client.mp3 --dump-header output/pipe_headers.txt

# 另一个终端逐行写入 NDJSON 文本
printf '%s\n' '{"text":"你好人类"}' > /tmp/tts_segments.fifo
printf '%s\n' '{"text":"我是一名人工助手"}' > /tmp/tts_segments.fifo
printf '%s\n' '{"text":"请问有什么可以帮你的吗？"}' > /tmp/tts_segments.fifo

# 完成后关闭写端或删除管道以结束响应
rm -f /tmp/tts_segments.fifo
```

2) 分批追加（在同一请求里分段延迟发送）
```bash
bash -c 'printf "%s\n" {"text":"你好人类"}; sleep 1; printf "%s\n" {"text":"我是一名人工助手"}; sleep 1; printf "%s\n" {"text":"请问有什么可以帮你的吗？"}' \
  | curl -v -N -X POST 'http://127.0.0.1:8014/api/tts/stream_segments?session_id=session-demo&voice_type=S_nLVvYpzH1&save_path=output/batch_merged.mp3' \
    -H 'Content-Type: application/x-ndjson' --data-binary @- \
    --output output/batch_merged_client.mp3 --dump-header output/batch_headers.txt
```

逐条慢速推送（更贴近“实时”）：

```
for seg in "你好！" "很高兴认识你！" "这是一次流式请求"; do
  printf "%s\n" "$seg"; sleep 1;
done | curl -sS -D headers_segments_slow.txt \
  -X POST "http://127.0.0.1:8014/api/tts/stream_segments?session_id=multi&save_path=output/merged_slow.mp3" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @- \
  --output output/merged_slow_client.mp3
```

### 示例：跨请求覆盖/追加保存

```bash
# 第一次请求：覆盖写入（创建或清空目标文件）
printf '%s\n' '{"text":"追加第一段"}' \
  | curl -s -N -X POST 'http://127.0.0.1:8014/api/tts/stream_segments?session_id=session-demo-append&voice_type=S_nLVvYpzH1&save_path=output/append_demo.mp3&save_mode=overwrite' \
    -H 'Content-Type: application/x-ndjson' --data-binary @- \
    --output output/append_client1.mp3 -D output/append_headers1.txt

# 第二次请求：追加写入（在已有文件末尾追加新片段）
printf '%s\n' '{"text":"追加第二段"}' \
  | curl -s -N -X POST 'http://127.0.0.1:8014/api/tts/stream_segments?session_id=session-demo-append&voice_type=S_nLVvYpzH1&save_path=output/append_demo.mp3&save_mode=append' \
    -H 'Content-Type: application/x-ndjson' --data-binary @- \
    --output output/append_client2.mp3 -D output/append_headers2.txt

# 验证文件大小有增长
stat -c '%s' output/append_demo.mp3
```

### 示例：WebSocket 双向流式（客户端与服务端各保存单文件）

```
pip install websockets==15.0.1

python - << 'PY'
import asyncio, websockets, pathlib, json

async def main():
    uri = "ws://127.0.0.1:8014/ws/tts/stream_segments?session_id=ws-demo&save_path=output/ws_merged.mp3"
    out = pathlib.Path('output/ws_client_merged.mp3')
    out.parent.mkdir(parents=True, exist_ok=True)
    async with websockets.connect(uri, ping_interval=None) as ws:
        # 逐段发送文本帧
        for seg in ["你好", "很高兴认识你！", "这是一次更长文本用于验证双向流。"]:
            await ws.send(seg)
        # 发送空文本帧表示输入结束
        await ws.send("")

        # 接收音频分片并写入本地合并文件
        with out.open('wb') as f:
            try:
                while True:
                    msg = await ws.recv()
                    if isinstance(msg, bytes):
                        f.write(msg)
                    else:
                        print('text msg:', msg)
            except websockets.exceptions.ConnectionClosedOK:
                pass
    print('client saved:', str(out.resolve()))

asyncio.run(main())
PY

ls -lh output/ws_merged.mp3 output/ws_client_merged.mp3
file output/ws_merged.mp3 output/ws_client_merged.mp3
```

### 注意事项与最佳实践
- 片段长度：极短片段（如单字或标点）可能被上游 TTS 拒绝或耗时较长，建议合并为更自然的短句；HTTP 端点内已做最小长度聚合以提高成功率。
- 客户端超时：流式生成需要时间，请将超时设置为 30–90 秒，根据网络与上游速度调整。
- 错误响应：若响应头为 `content-type: application/json`，说明返回的是错误信息（例如鉴权失败、参数不合法），不要保存为 `.mp3`。
- 服务端保存：提供 `save_path` 时，服务端会边合成边写入单文件，并在完成后在控制台打印绝对路径，同时在响应头中返回 `X-Save-Path`。
- 鉴权与域名：确保 `.env` 中的凭证与所选域名一致（`bytedance.com` vs `byteoversea.com`），并已开通对应音色与集群权限。

## 协议与解析简述
本示例采用官方二进制协议：
- 头部 4 字节（每位含义如下）：
  - 协议版本（4 bit）：`0b0001`
  - 头部大小（4 bit）：`0b0001` 表示 4 字节，无扩展
  - 消息类型（4 bit）：`0b0001` 为完整客户端请求；`0b1011` 为音频返回；`0b1111` 为错误消息
  - 消息类型特定标志（4 bit）：
    - 音频返回中：`>0` 为序号递增；`<0` 表示最后一帧；`0` 可作 ACK
  - 序列化方法（4 bit）：`0b0001` 表示 JSON
  - 压缩（4 bit）：`0b0001` 表示 gzip（本示例对 payload 使用 gzip）
- 负载：根据消息类型解析，如果是音频返回，读取序号与负载大小后拼接为 mp3 文件。

解析逻辑见 `parse_response`：
- 类型 `0xb`（音频）：写入音频分片，遇到负序号表示最后一帧，结束任务
- 类型 `0xf`（错误）：解压并打印错误信息与代码
- 类型 `0xc`（前端消息）：按需打印或处理

## 兼容性与注意事项
- websockets 版本：
  - `>=15.0` 使用参数名 `additional_headers`
  - 旧版本使用 `extra_headers`
  - 本项目已适配 `additional_headers`
- 事件循环：
  - 入口采用 `asyncio.run(main())`，避免 `get_event_loop` 在 Python 3.13 的弃用警告
- 压缩：
  - 示例默认对 JSON 负载使用 `gzip` 压缩，与头部位标一致

## 常见问题排查
- 401/鉴权失败（`invalid auth token`）：
  - 令牌无效/过期，或与 `appid`、`cluster`、域名不匹配
  - 检查是否使用了正确域名（`bytedance.com` vs `byteoversea.com`）
  - 确认控制台应用已开通 TTS 服务与对应音色权限
  - API 模式下，若鉴权失败，流会快速结束；请检查服务端日志与响应文本
- `TypeError: ... unexpected keyword argument 'extra_headers'`：
  - 使用 websockets 15.x 时需改为 `additional_headers`
- 无当前事件循环/弃用警告：
  - 使用 `asyncio.run(main())`
- 有错误消息但无音频文件：
  - 当服务返回错误类型（0xf），脚本不会写入音频；请先修复鉴权或参数问题
- 保存的 mp3 无法播放：
  - 检查文件类型是否为 `JSON text data`（`file your.mp3`）；若是，说明保存了错误响应而非音频流。
  - 确认已在 `.env` 或系统环境中配置了 `OPENSPEECH_*` 凭证；并确保令牌与域名匹配。

## 自定义与扩展
- 修改合成文本：`request.text`
- 调整音频参数：`encoding`、`speed_ratio`、`volume_ratio`、`pitch_ratio`
- 切换操作：`request.operation` 为 `submit` 或 `query`
- 脚本 `ws_test.py` 已支持从环境变量读取凭证，无需将令牌写入代码（参见上文环境变量示例）。

## 安全建议
- 不要将真实令牌提交到版本库（已添加 `.gitignore`）。
- 令牌具有权限，请妥善保管并定期轮换。

## 参考
- 火山引擎鉴权文档（Bearer/HMAC）：`https://www.volcengine.com/docs/6561/1105162`
- BytePlus WebSocket TTS 文档：`https://docs.byteplus.com/en/docs/speech/docs-websocket-api`