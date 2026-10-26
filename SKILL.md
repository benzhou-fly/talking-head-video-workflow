---
name: talking-head-video-workflow
description: 用 Fish Audio 指定音色和台词制作可反复换稿的口播视频，支持只生成配音与原速底片，或调用阿里 VideoRetalk（默认）及火山 AI MediaKit（可选备用）完成口型同步。适用于本地人物视频配音、换台词、情绪指导和口型对齐。
metadata:
  short-description: Fish Audio 配音，默认阿里口型同步，火山可选备用
---

# 口播视频工作流

把本地人物底片和台词做成可重复迭代的口播版本。每次运行创建独立版本目录，换台词不会覆盖旧结果。

## 服务选择

- 配音使用 Fish Audio。`--voice-id` 指定音色 ID；`--fish-model` 指定 TTS 引擎，两者不同。情绪提示可写入台词，例如 `[轻微讽刺]`。
- **口型同步默认阿里云百炼 VideoRetalk**：省略 `--provider` 或传入 `--provider aliyun`，读取 `DASHSCOPE_API_KEY`。
- **火山 AI MediaKit 作为可选备用**：用户说“用火山”“切换火山备用”时传入 `--provider volc`，读取 `VOLC_API_KEY`。用户明确选择的服务优先。
- 每次只上传到并调用所选口型服务。脚本失败不会自动切换服务，也不会自动重复提交付费任务。要换到备用服务时，可以复用已有配音，跳过 Fish 生成。
- 保持原视频速度，关闭扩展：阿里 `video_extension=false`，火山 `enable_video_loop=false`。时长不一致时两家都按较短者输出；音频长于底片时先说明配音会被截断，遵循用户已经选择的时长策略。
- `audio` 模式不调用任何口型服务；已有配音通过 `--audio` 传入时不调用 Fish。

## 本机准备

需要 Python 3.9+、FFmpeg 和 FFprobe，可联网访问相应服务。执行脚本只依赖 Python 标准库。

参考 [`.env.example`](.env.example)，在本机单独配置 `.env`，通过 `--env-file` 指定；已有环境变量优先。不需要填写未使用服务的密钥：

- `FISH_API_KEY`：仅在生成新配音时需要。
- `DASHSCOPE_API_KEY`：阿里口型同步使用，须为百炼北京地域 API Key。
- `VOLC_API_KEY`：仅在选择火山口型同步时需要。

密钥不写进技能、台词、生成记录或聊天。视频和配音会上传到所选服务的临时存储。

## 使用方式

以下命令在技能目录中执行；也可以使用脚本的绝对路径。

默认阿里：生成新配音并完成口型同步。

```bash
python3 scripts/talking_head_workflow.py \
  --video "/path/to/raw.mp4" \
  --voice-id "fish-voice-id" \
  --text-file "/path/to/script.txt" \
  --mode all \
  --env-file "/path/to/.env"
```

选择火山备用：复用已有配音，直接做口型同步。

```bash
python3 scripts/talking_head_workflow.py \
  --video "/path/to/raw.mp4" \
  --audio "/path/to/voice.mp3" \
  --mode lipsync \
  --provider volc \
  --env-file "/path/to/.env"
```

同样可在 `all` 模式追加 `--provider volc`，或在 `lipsync` 模式省略该参数使用阿里。

只生成配音和原速底片：

```bash
python3 scripts/talking_head_workflow.py \
  --video "/path/to/raw.mp4" \
  --voice-id "fish-voice-id" \
  --text "[语气坚定]这里是新的台词" \
  --mode audio \
  --env-file "/path/to/.env"
```

`--emotion` 为没有开头标签的台词添加整段情绪提示。`--run-name` 指定版本名，`--output-dir` 指定版本根目录。脚本不修改或变速原视频；`audio` 模式目前复制完整底片，若用户要按配音剪底片，应另用 FFmpeg 原速裁切后再传入。

## 执行与故障处理

1. 根据用户要求确定模式、素材、音色和口型服务，沿用当前对话中已经确定的选择；没有选择时默认阿里。已有音频不需要音色 ID 或新台词。
2. 检查所选服务的输入限制，见 [API 配置说明](references/api-contract.md)。阿里尺寸、格式、帧率和时长检查在上传前执行；不合格时说明原因，按用户要求调整素材或切换服务。
3. 运行脚本，查看版本目录的 `请求参数.json`、`生成记录.json`、`文件检查.json`。生成记录包含实际服务、任务 ID、调用标记及错误。
4. 任务已提交但轮询或下载失败时，按记录中的服务和任务 ID 查询原任务，不直接重跑整条命令。阿里提交未返回 ID 时，先到控制台核对是否已创建任务，避免重复计费。需要换到火山时使用 `--audio` 复用配音。
5. 检查成片包含音视频流、时长正常且完整解码通过。回复文件链接、实际服务及截断情况；解码通过不能证明口型主观效果完美。

执行脚本：[scripts/talking_head_workflow.py](scripts/talking_head_workflow.py)。离线回归检查：`python3 scripts/test_workflow.py`，使用合成测试素材与模拟接口，不上传素材、不消耗 API 额度。
