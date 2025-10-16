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
在 `ws_test.py` 顶部填写以下变量：
- `appid`：控制台创建应用获得
- `token`：控制台生成的 Access Token（Bearer Token）
- `cluster`：如 `volcano_icl`（确保服务已开通并与令牌权限匹配）
- `voice_type`：可用的音色标识（需有权限）

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
- 如需避免将令牌写入代码，建议使用环境变量：

```
import os
appid = os.getenv("OPENSPEECH_APPID")
token = os.getenv("OPENSPEECH_TOKEN")
cluster = os.getenv("OPENSPEECH_CLUSTER", "volcano_icl")
voice_type = os.getenv("OPENSPEECH_VOICE_TYPE")
```

## 安全建议
- 不要将真实令牌提交到版本库（已添加 `.gitignore`）。
- 令牌具有权限，请妥善保管并定期轮换。

## 参考
- 火山引擎鉴权文档（Bearer/HMAC）：`https://www.volcengine.com/docs/6561/1105162`
- BytePlus WebSocket TTS 文档：`https://docs.byteplus.com/en/docs/speech/docs-websocket-api`