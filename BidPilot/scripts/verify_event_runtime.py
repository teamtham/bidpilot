"""Sanitized live provider probes. No real deals or communications."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / '.env')
import qwenwork_api
import studio_assistant

turns = ['We need social videos for a cafe launch.', 'Two shoot days.']
results = []
client = qwenwork_api.Client()
for provider in ('qwenwork', 'gemini'):
    for trial, text in enumerate(turns, 1):
        start = time.monotonic()
        try:
            if provider == 'qwenwork':
                result = client.api('/v1/sessions', 'POST', {
                    'initial_prompt': {'text': 'Synthetic short buyer turn. Ask a useful scope question; no prices or booking. Buyer: ' + text},
                    'name': 'BidPilot benchmark', 'mode': 'flash', 'locale': 'en', 'ttl_seconds': 600})
                sid = result['session_id']
                tid = result.get('first_turn_id')
                path = '/v1/sessions/' + sid
                deadline = time.monotonic() + 90
                reply = False
                status = 'timeout'
                try:
                    while time.monotonic() < deadline:
                        if not tid:
                            items = client.api(path + '/turns').get('items', [])
                            tid = items[-1]['turn_id'] if items else None
                        if tid:
                            try:
                                turn = client.api(path + '/turns/' + tid)
                            except qwenwork_api.APIError as exc:
                                if str(exc) != 'http_404':
                                    raise
                                time.sleep(1)
                                continue
                            if turn['status'] == 'completed':
                                reply = bool(turn.get('final_message'))
                                status = 'completed' if reply else 'invalid_answer'
                                break
                            if turn['status'] in ('failed', 'interrupted', 'action_required'):
                                status = turn['status']
                                break
                        time.sleep(1)
                finally:
                    try:
                        client.api(path, 'DELETE')
                    except qwenwork_api.APIError:
                        pass
            else:
                result = studio_assistant.generate([{'text': json.dumps({'turns': [{'sender': 'buyer', 'body': text}], 'facts': [], 'brief_missing': [], 'media_analysis_id': None})}])
                reply = bool(result.get('reply'))
                status = 'completed' if reply else 'invalid_answer'
            elapsed = time.monotonic() - start
            results.append({'provider': provider, 'trial': trial, 'session_kind': 'new', 'status': status,
                            'completed_reply_seconds': elapsed if reply else None,
                            'first_visible_text_seconds': elapsed if reply else None, 'request_seconds': elapsed})
        except Exception as exc:
            results.append({'provider': provider, 'trial': trial, 'session_kind': 'new', 'status': 'failed',
                            'error': str(exc) if isinstance(exc, qwenwork_api.APIError) else type(exc).__name__,
                            'request_seconds': time.monotonic() - start, 'completed_reply_seconds': None,
                            'first_visible_text_seconds': None})
print(json.dumps({'synthetic': True, 'streaming': False, 'trials': results}, indent=2))
