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
export OPENSPEECH_APPID=你的appid
export OPENSPEECH_TOKEN=你的access_token
export OPENSPEECH_CLUSTER=volcano_icl   # 可根据控制台实际开通集群调整
export OPENSPEECH_VOICE_TYPE=S_nLVvYpzH1 # 可选，不设置则用代码内的默认
export OPENSPEECH_HOST=openspeech.bytedance.com # 或 openspeech.byteoversea.com（海外）

uvicorn api_server:app --host 0.0.0.0 --port 8000
```

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