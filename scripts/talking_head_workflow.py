#!/usr/bin/env python3
"""Repeatable Fish Audio + selectable lip-sync workflow (Aliyun by default).

Credentials come from the environment or --env-file: FISH_API_KEY for synthesis,
DASHSCOPE_API_KEY for Aliyun VideoRetalk, VOLC_API_KEY for Volc AI MediaKit.
Only the selected provider is called; failures never trigger another provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
VOLC_BASE = "https://mediakit.cn-beijing.volces.com"
ALIYUN_BASE = "https://dashscope.aliyuncs.com/api/v1"
ALIYUN_MODEL = "videoretalk"
PROVIDER_KEYS = {"aliyun": "DASHSCOPE_API_KEY", "volc": "VOLC_API_KEY"}
PROVIDER_NAMES = {"aliyun": "阿里 VideoRetalk", "volc": "火山 AI MediaKit"}
DEFAULT_FISH_MODEL = "s2.1-pro-free"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def load_env(path: pathlib.Path | None) -> None:
    if not path or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少 {name}。请在本机环境变量或 .env 中配置，不要把密钥写进技能文件。")
    return value


def redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def write_json(path: pathlib.Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    path.chmod(0o600)


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: pathlib.Path) -> dict[str, Any]:
    try:
        return json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)
        ], text=True))
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffprobe，请先安装 FFmpeg。") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"媒体无法解析: {path}") from exc


def media_summary(path: pathlib.Path) -> dict[str, Any]:
    data = probe(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "duration": float(fmt.get("duration", 0) or 0),
        "format": fmt.get("format_name"),
        "streams": [
            {k: stream.get(k) for k in ("codec_type", "codec_name", "width", "height", "avg_frame_rate") if k in stream}
            for stream in streams
        ],
    }


def http_json(method: str, url: str, *, headers: dict[str, str], payload: Any = None,
              timeout: int = 120, secrets: list[str] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code} {method} {url}: {redact(detail, secrets or [])}") from None
    except URLError as exc:
        raise RuntimeError(f"网络请求失败 {method} {url}: {exc.reason}") from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"接口返回不是 JSON: {url}") from exc
    return data


def fish_tts(api_key: str, voice_id: str, text: str, output: pathlib.Path,
             model: str, speed: float, secrets: list[str]) -> None:
    payload = {
        "text": text,
        "reference_id": voice_id,
        "format": "mp3",
        "mp3_bitrate": 192,
        "sample_rate": 44100,
        "normalize": True,
        "prosody": {"speed": speed, "volume": 0, "normalize_loudness": True},
    }
    req = Request(FISH_TTS_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": model,
    }, method="POST")
    try:
        with urlopen(req, timeout=300) as response:
            audio = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"Fish Audio HTTP {exc.code}: {redact(detail, secrets)}") from None
    except URLError as exc:
        raise RuntimeError(f"Fish Audio 网络请求失败: {exc.reason}") from None
    if len(audio) < 100 or not audio.startswith(b"ID3") and audio[:2] != b"\xff\xfb":
        raise RuntimeError("Fish Audio 未返回有效 MP3。")
    output.write_bytes(audio)
    output.chmod(0o600)
    media_summary(output)


def upload_media(api_key: str, path: pathlib.Path, label: str, secrets: list[str]) -> str:
    receipt = http_json("POST", VOLC_BASE + "/api/v1/tools-sync/request-media-upload-url", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    }, payload={"file_name": path.name}, secrets=secrets)
    if not receipt.get("success"):
        raise RuntimeError(f"火山申请上传地址失败: {redact(receipt, secrets)}")
    result = receipt.get("result") or {}
    upload_url = result.get("upload_url")
    file_id = result.get("file_id")
    if not upload_url or not file_id:
        raise RuntimeError(f"火山上传响应缺少地址或 file_id: {redact(receipt, secrets)}")
    raw_headers = result.get("upload_headers") or []
    if isinstance(raw_headers, dict):
        upload_headers = dict(raw_headers)
    else:
        upload_headers = {}
        for item in raw_headers:
            name = item.get("key") or item.get("name")
            if name:
                upload_headers[name] = item.get("value", "")
    upload_headers.setdefault("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    request = Request(upload_url, data=path.read_bytes(), headers=upload_headers, method=result.get("method", "PUT"))
    try:
        with urlopen(request, timeout=600) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"火山上传 {label} HTTP {exc.code}: {redact(detail, secrets)}") from None
    return file_id


def submit_lipsync(api_key: str, video_id: str, audio_id: str, token: str,
                   secrets: list[str]) -> dict[str, Any]:
    response = http_json("POST", VOLC_BASE + "/api/v1/tools/lip-sync", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    }, payload={
        "video_url": video_id,
        "audio_url": audio_id,
        "enable_video_loop": False,
        "client_token": token,
    }, secrets=secrets)
    if not response.get("success") or not response.get("task_id"):
        raise RuntimeError(f"火山口型任务提交失败: {redact(response, secrets)}")
    return response


def poll_lipsync(api_key: str, task_id: str, secrets: list[str], timeout: int,
                 status_callback) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        response = http_json("GET", VOLC_BASE + "/api/v1/tasks/" + quote(task_id, safe=""), headers={
            "Authorization": f"Bearer {api_key}",
        }, secrets=secrets)
        status_callback(response)
        if not response.get("success"):
            raise RuntimeError(f"火山任务查询失败: {redact(response, secrets)}")
        status = response.get("status")
        if status == "completed":
            result = response.get("result") or {}
            if not result.get("video_url", "").startswith("https://"):
                raise RuntimeError(f"火山任务完成但未返回 HTTPS 视频地址: {redact(response, secrets)}")
            return response
        if status in {"failed", "cancelled", "expired"}:
            raise RuntimeError(f"火山任务结束状态为 {status}: {redact(response, secrets)}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"火山任务超过 {timeout} 秒仍未完成；请使用记录中的 task_id 继续查询。")
        time.sleep(10)


def aliyun_upload(api_key: str, path: pathlib.Path, label: str, secrets: list[str]) -> str:
    receipt = http_json("GET", ALIYUN_BASE + "/uploads?" + urlencode({
        "action": "getPolicy", "model": ALIYUN_MODEL,
    }), headers={"Authorization": f"Bearer {api_key}"}, secrets=secrets)
    policy = receipt.get("data") or {}
    required = {"upload_host", "upload_dir", "oss_access_key_id", "signature", "policy",
                "x_oss_object_acl", "x_oss_forbid_overwrite"}
    missing = required - policy.keys()
    if missing:
        raise RuntimeError(f"阿里上传凭证缺少字段: {sorted(missing)}")
    # Use an ASCII name in the multipart header; keep the user's original locally.
    filename = uuid.uuid4().hex + path.suffix.lower()
    object_key = str(policy["upload_dir"]).rstrip("/") + "/" + filename
    fields = {
        "OSSAccessKeyId": str(policy["oss_access_key_id"]),
        "Signature": str(policy["signature"]),
        "policy": str(policy["policy"]),
        "x-oss-object-acl": str(policy["x_oss_object_acl"]),
        "x-oss-forbid-overwrite": str(policy["x_oss_forbid_overwrite"]),
        "key": object_key, "success_action_status": "200",
    }
    boundary = "----talking-head-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                       f'\r\n\r\n{value}\r\n').encode())
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    chunks.extend([
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"'
         f'\r\nContent-Type: {mime}\r\n\r\n').encode(),
        path.read_bytes(), f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = Request(str(policy["upload_host"]), data=b"".join(chunks), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }, method="POST")
    try:
        with urlopen(req, timeout=600) as response:
            response.read()
            if response.status != 200:
                raise RuntimeError(f"阿里上传 {label} HTTP {response.status}")
    except HTTPError as exc:
        # The storage server can echo signed form fields. Do not log its raw body.
        raise RuntimeError(f"阿里上传 {label} HTTP {exc.code}") from None
    except URLError:
        raise RuntimeError(f"阿里上传 {label} 网络请求失败") from None
    return "oss://" + object_key


def aliyun_submit(api_key: str, video_id: str, audio_id: str,
                  secrets: list[str]) -> dict[str, Any]:
    response = http_json("POST", ALIYUN_BASE + "/services/aigc/image2video/video-synthesis", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        "X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable",
    }, payload={
        "model": ALIYUN_MODEL,
        "input": {"video_url": video_id, "audio_url": audio_id},
        "parameters": {"video_extension": False},
    }, secrets=secrets)
    if not (response.get("output") or {}).get("task_id"):
        raise RuntimeError(f"阿里口型任务提交失败: {redact(response, secrets)}")
    return response


def aliyun_poll(api_key: str, task_id: str, secrets: list[str], timeout: int,
                status_callback) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        response = http_json("GET", ALIYUN_BASE + "/tasks/" + quote(task_id, safe=""), headers={
            "Authorization": f"Bearer {api_key}",
        }, secrets=secrets)
        status_callback(response)
        result = response.get("output") or {}
        status = result.get("task_status")
        if status == "SUCCEEDED":
            if not result.get("video_url", "").startswith(("https://", "http://")):
                raise RuntimeError("阿里任务完成但未返回有效的视频下载地址。")
            return response
        if status not in {"PENDING", "RUNNING", "PRE-PROCESSING", "POST-PROCESSING"}:
            raise RuntimeError(f"阿里任务查询失败或结束: {redact(response, secrets)}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"阿里任务超过 {timeout} 秒仍未完成；请使用记录中的 task_id 继续查询。")
        time.sleep(10)


def validate_aliyun_media(video: dict[str, Any], audio: dict[str, Any]) -> None:
    """Check documented VideoRetalk limits locally before sending any media."""
    for label, item, limit in [("视频", video, 300_000_000), ("音频", audio, 30_000_000)]:
        if not 2 < item["duration"] < 120 or item["bytes"] > limit:
            raise RuntimeError(f"阿里 VideoRetalk {label}须为 2～120 秒（不含端点），且不超过 {limit // 1_000_000} MB。")
    stream = next((s for s in video["streams"] if s.get("codec_type") == "video"), {})
    if stream.get("codec_name") not in {"h264", "hevc"}:
        raise RuntimeError("阿里 VideoRetalk 视频需使用 H.264 或 H.265 编码。")
    if not all(640 <= stream.get(k, 0) <= 2048 for k in ("width", "height")):
        raise RuntimeError("阿里 VideoRetalk 视频宽高均须在 640～2048 像素之间。")
    try:
        n, d = stream["avg_frame_rate"].split("/")
        fps = float(n) / float(d)
    except (ValueError, KeyError, ZeroDivisionError):
        fps = 0
    if not 15 <= fps <= 60:
        raise RuntimeError("阿里 VideoRetalk 视频帧率须为 15～60 fps。")
    if pathlib.Path(audio["path"]).suffix.lower() not in {".wav", ".mp3", ".aac"}:
        raise RuntimeError("阿里 VideoRetalk 配音须为 WAV、MP3 或 AAC。")
    if not any(s.get("codec_type") == "audio" for s in audio["streams"]):
        raise RuntimeError("配音文件没有音频流。")


def download(url: str, output: pathlib.Path) -> None:
    partial = output.with_suffix(output.suffix + ".part")
    try:
        with urlopen(url, timeout=600) as response, partial.open("wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except HTTPError as exc:
        raise RuntimeError(f"下载成片 HTTP {exc.code}") from None
    partial.replace(output)


def decode_check(path: pathlib.Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                                   capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg，请先安装 FFmpeg。") from exc
    return {"decode_exit": completed.returncode, "decode_errors": completed.stderr[:2000]}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fish Audio 配音 + 阿里/火山口型同步（默认阿里）")
    p.add_argument("--video", required=True, type=pathlib.Path, help="人物底片 MP4")
    p.add_argument("--voice-id", help="Fish Audio 模型 ID；使用 --audio 时可省略")
    p.add_argument("--text", help="台词文本；情绪指导可直接写成 [语气]台词")
    p.add_argument("--text-file", type=pathlib.Path, help="台词文件，与 --text 二选一")
    p.add_argument("--emotion", help="为没有情绪标签的台词前置 [情绪指导]")
    p.add_argument("--audio", type=pathlib.Path, help="已有配音文件；提供后跳过 Fish Audio")
    p.add_argument("--mode", choices=("audio", "lipsync", "all"), default="all",
                   help="audio=配音并复制底片，lipsync=使用已有音频做口型同步，all=生成配音并做口型同步")
    p.add_argument("--provider", choices=("aliyun", "volc"), default="aliyun",
                   help="口型服务：aliyun=阿里 VideoRetalk（默认），volc=火山 AI MediaKit（备用）；失败不自动切换")
    p.add_argument("--fish-model", default=DEFAULT_FISH_MODEL)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--output-dir", type=pathlib.Path, help="输出根目录；默认在视频旁创建")
    p.add_argument("--run-name", help="本次版本名；默认按时间生成")
    p.add_argument("--env-file", type=pathlib.Path, help="可选 .env 路径")
    p.add_argument("--poll-timeout", type=int, default=3600)
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    load_env(args.env_file)
    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise RuntimeError(f"视频不存在: {video}")
    if video.suffix.lower() != ".mp4":
        raise RuntimeError("本工作流使用 MP4 人物底片。")
    if args.audio and not args.audio.is_file():
        raise RuntimeError(f"配音不存在: {args.audio}")
    if args.mode in {"all", "audio"} and not args.audio:
        if not args.voice_id:
            raise RuntimeError("生成 Fish Audio 配音需要 --voice-id。")
        if bool(args.text) == bool(args.text_file):
            raise RuntimeError("--text 和 --text-file 必须二选一。")
    if args.mode == "lipsync" and not args.audio:
        raise RuntimeError("lipsync 模式需要 --audio 已有配音文件。")
    if args.audio is None:
        require_env("FISH_API_KEY")
    if args.mode in {"all", "lipsync"}:
        require_env(PROVIDER_KEYS[args.provider])
    if args.speed <= 0:
        raise RuntimeError("--speed 必须大于 0。")

    root = (args.output_dir or video.parent / "口播工作流").expanduser().resolve()
    name = args.run_name or "run_" + stamp()
    run = root / name
    if run.exists():
        run = root / (name + "_" + uuid.uuid4().hex[:6])
    run.mkdir(parents=True, exist_ok=False)
    secrets = [os.environ.get(name, "") for name in ("FISH_API_KEY", "DASHSCOPE_API_KEY", "VOLC_API_KEY")]
    state: dict[str, Any] = {
        "status": "running", "created_at": utc_now(), "run_dir": str(run),
        "mode": args.mode, "video": str(video), "voice_id": args.voice_id,
        "fish_model": args.fish_model, "speed": args.speed,
        "requested_provider": args.provider, "lipsync_provider": None,
        "enable_video_loop": False, "video_extension": False,
        "aliyun_api_called": False, "volc_api_called": False,
        "automatic_fallback": False,
    }
    write_json(run / "请求参数.json", {k: v for k, v in vars(args).items() if k not in {"env_file"}})
    state["video_input"] = media_summary(video)

    try:
        audio = args.audio.resolve() if args.audio else run / "配音.mp3"
        if not args.audio:
            text = args.text if args.text is not None else args.text_file.read_text(encoding="utf-8")
            text = text.strip()
            if args.emotion and not text.lstrip().startswith("["):
                text = f"[{args.emotion}]{text}"
            (run / "台词.txt").write_text(text, encoding="utf-8")
            fish_tts(require_env("FISH_API_KEY"), args.voice_id, text, audio,
                     args.fish_model, args.speed, secrets)
        state["audio_input"] = media_summary(audio)
        state["audio_path"] = str(audio)
        shutil.copy2(video, run / "底片.mp4")

        if args.mode == "audio":
            state.update({"status": "complete", "output_audio": str(audio), "output_video": str(run / "底片.mp4")})
            write_json(run / "文件检查.json", {"audio": media_summary(audio), "video": media_summary(run / "底片.mp4"),
                                             "audio_decode": decode_check(audio), "video_decode": decode_check(run / "底片.mp4")})
            write_json(run / "生成记录.json", state)
            print(json.dumps({"status": "complete", "run_dir": str(run), "audio": str(audio), "video": str(run / "底片.mp4")}, ensure_ascii=False))
            return 0

        provider = args.provider
        provider_name = PROVIDER_NAMES[provider]
        api_key = require_env(PROVIDER_KEYS[provider])
        if provider == "aliyun":
            validate_aliyun_media(state["video_input"], state["audio_input"])
        if state["audio_input"]["duration"] > state["video_input"]["duration"] + 0.1:
            print("注意：配音长于底片，关闭视频扩展后成片将按较短者截断。", flush=True)
        state.update({"lipsync_provider": provider, provider + "_api_called": True})
        write_json(run / "生成记录.json", state)
        upload = aliyun_upload if provider == "aliyun" else upload_media
        video_id = upload(api_key, video, "video", secrets)
        audio_id = upload(api_key, audio, "audio", secrets)
        state.update({"video_file_id": video_id, "audio_file_id": audio_id})
        if provider == "aliyun":
            submitted = aliyun_submit(api_key, video_id, audio_id, secrets)
            task_id = submitted["output"]["task_id"]
        else:
            token = "talking-head-" + uuid.uuid4().hex
            state["client_token"] = token
            write_json(run / "生成记录.json", state)
            submitted = submit_lipsync(api_key, video_id, audio_id, token, secrets)
            task_id = submitted["task_id"]
        state.update({"task_id": task_id, "submit_response": redact(submitted, secrets)})
        write_json(run / "生成记录.json", state)
        last_status = None
        def on_status(response: dict[str, Any]) -> None:
            nonlocal last_status
            status = ((response.get("output") or {}).get("task_status")
                      if provider == "aliyun" else response.get("status"))
            if status != last_status:
                print(f"{provider_name}任务状态: {status}", flush=True)
                last_status = status
            state["last_response"] = redact(response, secrets)
            write_json(run / "生成记录.json", state)
        poll = aliyun_poll if provider == "aliyun" else poll_lipsync
        completed = poll(api_key, task_id, secrets, args.poll_timeout, on_status)
        result = completed["output"] if provider == "aliyun" else completed["result"]
        output = run / "口型同步成片.mp4"
        download(result["video_url"], output)
        output_summary = media_summary(output)
        stream_types = {s.get("codec_type") for s in output_summary["streams"]}
        if not {"video", "audio"} <= stream_types or output_summary["duration"] <= 0:
            raise RuntimeError("成片缺少视频流或音频流，或时长无效。")
        check = decode_check(output)
        if check["decode_exit"] != 0:
            raise RuntimeError(f"成片解码检查失败: {check['decode_errors']}")
        state.update({"status": "complete", "completed_at": utc_now(), "output_video": str(output),
                      "output_summary": output_summary})
        write_json(run / "文件检查.json", {"input_video": state["video_input"], "input_audio": state["audio_input"],
                                         "output": output_summary, "decode_check": check,
                                         "video_speed_changed": False, "video_loop_enabled": False,
                                         "lipsync_provider": provider,
                                         "video_extension": False,
                                         "aliyun_api_called": state["aliyun_api_called"],
                                         "volc_api_called": state["volc_api_called"]})
        write_json(run / "生成记录.json", state)
        print(json.dumps({"status": "complete", "run_dir": str(run), "audio": str(audio), "video": str(output)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        state.update({"status": "failed", "failed_at": utc_now(), "error": redact(str(exc), secrets)})
        write_json(run / "生成记录.json", state)
        print(f"错误: {state['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
