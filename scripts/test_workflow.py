#!/usr/bin/env python3
"""Offline provider routing/contract tests. No real credentials or network calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from email.parser import BytesParser
from email.policy import default
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

import talking_head_workflow as workflow


class Response(io.BytesIO):
    status = 200


class Gateway:
    """Simulate the documented wire contracts, rejecting unexpected requests."""
    def __init__(self, case, provider, fail=False, timeout=False):
        self.case, self.provider = case, provider
        self.fail, self.timeout = fail, timeout
        self.calls = []
        self.uploads = 0
        self.polls = 0
        self.submissions = 0

    def __call__(self, request, timeout=120):
        url = request if isinstance(request, str) else request.full_url
        method = 'GET' if isinstance(request, str) else request.get_method()
        self.calls.append((method, url))
        headers = {} if isinstance(request, str) else {k.lower(): v for k, v in request.header_items()}
        data = None if isinstance(request, str) else request.data
        c = self.case
        if url == workflow.FISH_TTS_URL:
            c.assertEqual(headers['authorization'], 'Bearer fish-test-key')
            body = json.loads(data)
            c.assertEqual(body['reference_id'], 'test-voice')
            c.assertIn('[语气坚定]', body['text'])
            return Response(c.audio.read_bytes())
        if url == 'https://result.invalid/final.mp4':
            c.assertNotIn('authorization', headers)
            return Response(c.video.read_bytes())
        if url.startswith(workflow.ALIYUN_BASE):
            c.assertEqual(self.provider, 'aliyun', 'unexpected provider switch')
            c.assertEqual(headers['authorization'], 'Bearer aliyun-test-key')
            if '/uploads?' in url:
                c.assertEqual(method, 'GET')
                c.assertIn('model=videoretalk', url)
                return Response(json.dumps({'data': {
                    'upload_host': 'https://upload.invalid/oss', 'upload_dir': 'temporary',
                    'oss_access_key_id': 'temporary-access', 'signature': 'signed-test',
                    'policy': 'policy-test', 'x_oss_object_acl': 'private',
                    'x_oss_forbid_overwrite': 'true',
                }}).encode())
            if url.endswith('/video-synthesis'):
                c.assertEqual(method, 'POST')
                self.submissions += 1
                body = json.loads(data)
                c.assertEqual(body['model'], 'videoretalk')
                c.assertIs(body['parameters']['video_extension'], False)
                c.assertTrue(body['input']['video_url'].startswith('oss://temporary/'))
                c.assertTrue(body['input']['audio_url'].startswith('oss://temporary/'))
                c.assertEqual(headers['x-dashscope-async'], 'enable')
                c.assertEqual(headers['x-dashscope-ossresourceresolve'], 'enable')
                if self.fail:
                    raise HTTPError(url, 403, 'denied', {}, io.BytesIO(b'aliyun-test-key invalid'))
                return Response(b'{"output":{"task_id":"ali-task","task_status":"PENDING"}}')
            if '/tasks/' in url:
                self.polls += 1
                if self.timeout:
                    raise TimeoutError('simulated polling timeout')
                result = {'task_id': 'ali-task', 'task_status': 'RUNNING' if self.polls == 1 else 'SUCCEEDED'}
                if self.polls > 1:
                    result['video_url'] = 'https://result.invalid/final.mp4'
                return Response(json.dumps({'output': result}).encode())
        if url.startswith(workflow.VOLC_BASE):
            c.assertEqual(self.provider, 'volc', 'unexpected provider switch')
            c.assertEqual(headers['authorization'], 'Bearer volc-test-key')
            if url.endswith('/request-media-upload-url'):
                body = json.loads(data)
                c.assertIn('file_name', body)
                return Response(json.dumps({'success': True, 'result': {
                    'upload_url': 'https://upload.invalid/volc', 'file_id': 'mediakit://file',
                    'method': 'PUT', 'upload_headers': [{'key': 'X-Test-Signature', 'value': 'signed'}],
                }}).encode())
            if url.endswith('/lip-sync'):
                self.submissions += 1
                body = json.loads(data)
                c.assertIs(body['enable_video_loop'], False)
                c.assertTrue(body['client_token'].isascii())
                c.assertEqual(body['video_url'], 'mediakit://file')
                return Response(b'{"success":true,"task_id":"volc-task"}')
            if '/tasks/' in url:
                self.polls += 1
                return Response(json.dumps({'success': True, 'task_id': 'volc-task',
                    'status': 'running' if self.polls == 1 else 'completed',
                    'result': {'video_url': 'https://result.invalid/final.mp4'}}).encode())
        if urlsplit(url).netloc == 'upload.invalid':
            self.uploads += 1
            expected = c.video.read_bytes() if self.uploads == 1 else c.audio.read_bytes()
            c.assertNotIn('authorization', headers, 'Do not forward API keys to upload hosts')
            if self.provider == 'aliyun':
                c.assertEqual(method, 'POST')
                message = BytesParser(policy=default).parsebytes(
                    ('Content-Type: ' + headers['content-type'] + '\r\n\r\n').encode() + data)
                parts = {p.get_param('name', header='content-disposition'): p.get_payload(decode=True)
                         for p in message.iter_parts()}
                c.assertEqual(parts['file'], expected)
                c.assertEqual(parts['Signature'], b'signed-test')
                c.assertEqual(parts['success_action_status'], b'200')
                c.assertEqual(parts['x-oss-object-acl'], b'private')
                c.assertTrue(parts['key'].startswith(b'temporary/'))
            else:
                c.assertEqual(method, 'PUT')
                c.assertEqual(data, expected)
                c.assertEqual(headers['x-test-signature'], 'signed')
            return Response(b'')
        raise AssertionError('Unexpected request: ' + url)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise RuntimeError('Offline integration tests require ffmpeg and ffprobe')
        cls.temp = tempfile.TemporaryDirectory(prefix='talking-head-provider-test-')
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / '测试底片.mp4'
        cls.audio = cls.root / '配音.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=gray:s=640x640:r=25',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '3',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', str(cls.video)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(cls.video), '-vn', '-c:a', 'libmp3lame',
                        '-b:a', '192k', str(cls.audio)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def execute(self, provider='aliyun', explicit=True, mode='lipsync', fail=False, timeout=False):
        run_root = self.root / self._testMethodName
        argv = ['workflow', '--video', str(self.video), '--output-dir', str(run_root),
                '--run-name', 'test', '--mode', mode]
        if explicit:
            argv += ['--provider', provider]
        if mode == 'all':
            argv += ['--voice-id', 'test-voice', '--text', '[语气坚定]测试台词。']
        else:
            argv += ['--audio', str(self.audio)]
        env = {'PATH': os.environ.get('PATH', '')}
        if mode != 'audio':
            env[workflow.PROVIDER_KEYS[provider]] = provider + '-test-key'
        if mode == 'all':
            env['FISH_API_KEY'] = 'fish-test-key'
        gateway = Gateway(self, provider, fail=fail, timeout=timeout)
        with patch.object(sys, 'argv', argv), patch.dict(os.environ, env, clear=True), \
             patch.object(workflow, 'urlopen', gateway), patch.object(workflow.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = workflow.main()
        state = json.loads((run_root / 'test' / '生成记录.json').read_text())
        return result, state, gateway, run_root / 'test'

    def test_default_aliyun_end_to_end(self):
        rc, state, gateway, run = self.execute(explicit=False)
        self.assertEqual(rc, 0)
        self.assertEqual(state['lipsync_provider'], 'aliyun')
        self.assertTrue(state['aliyun_api_called'])
        self.assertFalse(state['volc_api_called'])
        self.assertEqual(gateway.submissions, 1)
        self.assertEqual(gateway.uploads, 2)
        self.assertEqual(gateway.polls, 2)
        self.assertEqual((run / '口型同步成片.mp4').read_bytes(), self.video.read_bytes())
        self.assertEqual(json.loads((run / '文件检查.json').read_text())['lipsync_provider'], 'aliyun')

    def test_explicit_aliyun(self):
        rc, state, _, _ = self.execute()
        self.assertEqual(rc, 0)
        self.assertEqual(state['task_id'], 'ali-task')

    def test_volc_without_aliyun_key(self):
        rc, state, gateway, run = self.execute('volc')
        self.assertEqual(rc, 0)
        self.assertEqual(state['lipsync_provider'], 'volc')
        self.assertFalse(state['aliyun_api_called'])
        self.assertTrue(state['volc_api_called'])
        self.assertEqual(gateway.submissions, 1)
        self.assertEqual(json.loads((run / '文件检查.json').read_text())['lipsync_provider'], 'volc')

    def test_audio_mode_needs_no_lipsync_keys(self):
        rc, state, gateway, _ = self.execute(mode='audio')
        self.assertEqual(rc, 0)
        self.assertIsNone(state['lipsync_provider'])
        self.assertFalse(state['aliyun_api_called'])
        self.assertFalse(state['volc_api_called'])
        self.assertEqual(gateway.calls, [])

    def test_fish_and_default_aliyun(self):
        rc, state, gateway, run = self.execute(mode='all', explicit=False)
        self.assertEqual(rc, 0)
        self.assertEqual(state['lipsync_provider'], 'aliyun')
        self.assertTrue((run / '配音.mp3').is_file())
        self.assertEqual(sum(url == workflow.FISH_TTS_URL for _, url in gateway.calls), 1)

    def test_failure_does_not_fallback_or_expose_key(self):
        rc, state, gateway, run = self.execute(fail=True)
        self.assertEqual(rc, 1)
        self.assertEqual(gateway.submissions, 1)
        self.assertFalse(state['volc_api_called'])
        self.assertIn('[REDACTED]', state['error'])
        self.assertNotIn('aliyun-test-key', (run / '生成记录.json').read_text())

    def test_polling_timeout_keeps_task_id(self):
        rc, state, gateway, _ = self.execute(timeout=True)
        self.assertEqual(rc, 1)
        self.assertEqual(state['task_id'], 'ali-task')
        self.assertEqual(state['lipsync_provider'], 'aliyun')
        self.assertEqual(gateway.submissions, 1)
        self.assertFalse(state['volc_api_called'])

    def test_missing_selected_key_fails_before_network(self):
        argv = ['workflow', '--video', str(self.video), '--audio', str(self.audio), '--mode', 'lipsync']
        with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'VOLC_API_KEY': 'unused'}, clear=True), \
             patch.object(workflow, 'urlopen', side_effect=AssertionError('unexpected network')):
            with self.assertRaisesRegex(RuntimeError, 'DASHSCOPE_API_KEY'):
                workflow.main()

    def test_aliyun_rejects_out_of_range_input(self):
        video = workflow.media_summary(self.video)
        audio = workflow.media_summary(self.audio)
        video['streams'][0]['width'] = 320
        with self.assertRaisesRegex(RuntimeError, '640'):
            workflow.validate_aliyun_media(video, audio)


if __name__ == '__main__':
    unittest.main(verbosity=2)
