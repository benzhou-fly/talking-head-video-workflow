# 工作流 API 配置说明

本技能使用 Fish Audio 配音，口型同步默认使用阿里云百炼 VideoRetalk，也可选择火山 AI MediaKit 作为备用。

## 一、选择口型服务和配置密钥

| 用途 | 命令参数 | 所需环境变量 |
| --- | --- | --- |
| 阿里 VideoRetalk（默认） | 省略 `--provider`，或 `--provider aliyun` | `DASHSCOPE_API_KEY` |
| 火山 AI MediaKit（备用） | `--provider volc` | `VOLC_API_KEY` |
| Fish Audio 新配音 | `--voice-id` 配合 `--text` 或 `--text-file` | `FISH_API_KEY` |

同一次运行只调用所选口型服务，**不会在失败后自动切换**。`--mode audio` 不调用口型服务；`--audio` 复用已有音频，不需要 Fish 密钥。

在本机单独的 `.env` 文件中配置需要的密钥，然后通过 `--env-file /path/to/.env` 加载。技能目录提供 `.env.example` 作为模板：

```text
FISH_API_KEY=your_fish_audio_api_key
DASHSCOPE_API_KEY=your_dashscope_api_key
VOLC_API_KEY=your_volc_ai_mediakit_api_key
```

阿里密钥须为**百炼北京地域的 API Key**，不是阿里云通用 AccessKey。火山需要 AI MediaKit API Key。未使用的服务可留空或不填写；已有同名环境变量优先于 `.env`。

密钥不能放入技能分享包、代码仓库或台词。脚本会遮盖已加载的密钥；临时上传签名不写入记录。含素材路径和临时结果链接的生成记录仅保留在本机，不要随技能分发。

## 二、Fish Audio 文本转语音

```text
POST https://api.fish.audio/v1/tts
```

请求头：

- `Authorization: Bearer $FISH_API_KEY`
- `Content-Type: application/json`
- `model: s2.1-pro-free`：TTS 引擎，使用 `--fish-model` 可替换。

请求体：

```json
{
  "text": "[语气坚定]这里是要说的台词。",
  "reference_id": "用户指定的音色模型ID",
  "format": "mp3",
  "sample_rate": 44100,
  "mp3_bitrate": 192,
  "normalize": true,
  "prosody": {"speed": 1.0, "volume": 0, "normalize_loudness": true}
}
```

`reference_id` 是音色 ID，与请求头的 TTS 引擎 `model` 不同。`normalize` 控制文本规范化；`prosody.normalize_loudness` 控制响度归一化。`--speed` 设置合成语速，不改变视频速度。

## 三、阿里 VideoRetalk（默认）

### 输入限制

工作流统一接收 MP4 人物底片。阿里 VideoRetalk 要求：

- 视频：大于 2 秒且小于 120 秒，文件不超过 300 MB；H.264/H.265 编码；15～60 fps；宽高均为 640～2048 像素。
- 音频：WAV、MP3 或 AAC；大于 2 秒且小于 120 秒；不超过 30 MB；建议清晰单人声。
- 视频需有人物正面近景，避免大角度侧脸和人脸过小。人物参考图为可选项，本脚本不传。

脚本在上传前检查格式、时长、尺寸、帧率和大小，不会自动变速或缩放素材。

### 1. 申请上传凭证

```text
GET https://dashscope.aliyuncs.com/api/v1/uploads?action=getPolicy&model=videoretalk
Authorization: Bearer $DASHSCOPE_API_KEY
```

从响应 `data` 读取 `upload_host`、`upload_dir`、`oss_access_key_id`、`signature`、`policy`、`x_oss_object_acl`、`x_oss_forbid_overwrite`。

### 2. 上传本地文件

对 `upload_host` 发送 `multipart/form-data` POST，字段为：

```text
OSSAccessKeyId = data.oss_access_key_id
Signature = data.signature
policy = data.policy
x-oss-object-acl = data.x_oss_object_acl
x-oss-forbid-overwrite = data.x_oss_forbid_overwrite
key = data.upload_dir/唯一文件名
success_action_status = 200
file = 文件原始二进制内容
```

视频和音频分别上传。上传后得到 `oss://<key>` 输入地址；临时存储文件有效期按官方规则为 48 小时。

### 3. 提交口型任务

```text
POST https://dashscope.aliyuncs.com/api/v1/services/aigc/image2video/video-synthesis
Authorization: Bearer $DASHSCOPE_API_KEY
Content-Type: application/json
X-DashScope-Async: enable
X-DashScope-OssResourceResolve: enable
```

使用 `oss://` 地址时必须设置 `X-DashScope-OssResourceResolve`。

```json
{
  "model": "videoretalk",
  "input": {
    "video_url": "oss://上传目录/视频.mp4",
    "audio_url": "oss://上传目录/配音.mp3"
  },
  "parameters": {"video_extension": false}
}
```

`video_extension=false` 关闭倒放/正放交替扩展。素材不等长时按较短者输出；音频长于视频的部分会被截断。成功提交后读取 `output.task_id`。

### 4. 查询任务并下载

```text
GET https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}
Authorization: Bearer $DASHSCOPE_API_KEY
```

读取 `output.task_status`：

- `PENDING`、`PRE-PROCESSING`、`RUNNING`、`POST-PROCESSING`：每 10 秒继续查询。
- `SUCCEEDED`：从 `output.video_url` 下载结果，链接有效期为完成后 24 小时。
- `FAILED`、`UNKNOWN` 或其他异常状态：停止，保留任务 ID 与错误，不自动重提或切换。

阿里提交接口本流程未使用幂等 token。若提交请求超时且没有收到任务 ID，先在百炼控制台核对任务，不能直接把网络超时当作未创建任务。

## 四、火山 AI MediaKit（可选备用）

用 `--provider volc` 显式选择。只需配置火山密钥，无需阿里密钥。

### 1. 获取上传地址

```text
POST https://mediakit.cn-beijing.volces.com/api/v1/tools-sync/request-media-upload-url
Authorization: Bearer $VOLC_API_KEY
Content-Type: application/json
```

```json
{"file_name": "视频.mp4"}
```

按响应的 `result.method`（通常为 PUT）和 `result.upload_headers`，把文件二进制内容上传到 `result.upload_url`。上传完成后使用 `result.file_id`，即 `mediakit://...` 地址。视频和音频分别上传。

### 2. 提交口型任务

```text
POST https://mediakit.cn-beijing.volces.com/api/v1/tools/lip-sync
Authorization: Bearer $VOLC_API_KEY
Content-Type: application/json
```

```json
{
  "video_url": "mediakit://视频文件地址",
  "audio_url": "mediakit://音频文件地址",
  "enable_video_loop": false,
  "client_token": "本次提交的唯一ASCII标识"
}
```

`enable_video_loop=false` 关闭视频循环。素材不等长时按较短者输出。`client_token` 会保存在本地记录中，火山可用同一 token 做同一请求的幂等控制；重新执行整条命令会创建新运行，并不是恢复原任务。成功后读取 `task_id`。

### 3. 查询与下载

```text
GET https://mediakit.cn-beijing.volces.com/api/v1/tasks/{task_id}
Authorization: Bearer $VOLC_API_KEY
```

每 10 秒查询一次。`completed` 时从 `result.video_url` 下载；`failed`、`cancelled`、`expired` 时停止。下载链接有效期为 24 小时，请及时保存。

## 五、版本记录与备用切换

每次生成使用独立版本目录。`生成记录.json` 包含 `lipsync_provider`、`task_id`、`aliyun_api_called`、`volc_api_called` 和服务原始状态。`audio` 模式的 `lipsync_provider` 为 `null`，两家调用标记均为 `false`。

任务已提交但查询失败或等待超时，应使用原服务和任务 ID 继续查询，不重复生成配音、上传和提交。两家任务 ID 不可混用。

用户决定改用火山时，复用已有音频即可：

```bash
python3 scripts/talking_head_workflow.py \
  --video "/path/to/raw.mp4" \
  --audio "/path/to/配音.mp3" \
  --mode lipsync --provider volc \
  --env-file "/path/to/.env"
```

下载后检查音视频流、时长及 FFmpeg 完整解码。检查通过代表文件完整可解码，口型观感仍需播放确认。

## 官方文档

- [阿里 VideoRetalk](https://help.aliyun.com/zh/model-studio/videoretalk-api)
- [阿里上传文件获取临时 URL](https://help.aliyun.com/zh/model-studio/get-temporary-file-url)
- [火山视频口型对齐](https://docs.volcengine.com/docs/Intelligentprocessing/submit-video-lip-alignment-task-api?lang=zh)
- [Fish Audio 文档索引](https://docs.fish.audio/llms.txt)
