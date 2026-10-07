"""Real stdio progress/image transport acceptance; owned synthetic Windows fixture only.

--run is required to launch a fixture. Uses no real business data or LLM.
Do not run alongside another native GUI acceptance test.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import queue
import subprocess
import time

from generic_validation import prepare
from live_validation import Client, DRIVER, decoded
from vendor.windows import OwnedProcess


def run(manifest, bundle=None):
    folder = Path(manifest['folder'])
    app = manifest['apps'][0]
    report = {'passed': False, 'llm_used': False, 'business_result_verified': False}
    proc = None
    owned = None
    client = None
    try:
        proc = subprocess.Popen([app['exe'], app['title'], app['receipt']], cwd=folder,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        owned = OwnedProcess(proc)
        if bundle:
            from build_portable import runtime_environment
            client = Client(folder/'config.json', bundle/'server.py', bundle/'runtime/python.exe',
                            runtime_environment(bundle/'runtime'))
        else:
            client = Client(folder/'config.json')

        def call(name, arguments=None):
            result = client.request('tools/call', {'name': name, 'arguments': arguments or {}}, timeout=40)
            assert not result.get('isError'), result
            return decoded(result)

        image_result = client.request('tools/call', {'name': 'computer_check_image', 'arguments': {}})
        images = [part for part in image_result['content'] if part['type'] == 'image']
        assert len(images) == 1 and images[0]['mimeType'] == 'image/png'
        assert base64.b64decode(images[0]['data']).startswith(b'\x89PNG\r\n\x1a\n')
        assert decoded(image_result)['screen_accessed'] is False
        report['image_transport'] = {'png_delivered': True, 'screen_accessed': False,
                                     'model_readback_tested': False}
        call('computer_begin', {'program_ids': [app['id']],
            'task_description': '새로 만든 합성 시험 창에서 고정 대기 중 진행 조회의 응답성만 확인합니다.'})
        deadline = time.monotonic()+10
        while True:
            windows = call('list_windows', {'on_screen_only': True}).get('windows', [])
            matches = [w for w in windows if w.get('title') == app['title'] and w['pid'] == proc.pid]
            if len(matches) == 1:
                break
            assert time.monotonic() < deadline, 'Owned fixture window not found'
            time.sleep(.1)
        target = {'program_id': app['id'], **{key: matches[0][key] for key in ('pid', 'window_id')}}
        task = {'id': 'progress-native014', 'name': '진행 조회 합성 시험',
            'instructions': '대기 중 별도 진행 조회를 확인합니다.', 'expected': '진행 조회가 실행 종료 전에 응답합니다.',
            'program_ids': [app['id']], 'steps': [{'program_id': app['id'], 'operation': 'delay', 'duration_ms': 2000}]}
        call('computer_save_task', task)
        client.index += 1
        run_id = client.index
        client.send({'jsonrpc': '2.0', 'id': run_id, 'method': 'tools/call', 'params': {
            'name': 'computer_run_task', 'arguments': {'task_id': task['id'], 'targets': [target]},
            '_meta': {'progressToken': 'native014'}}})
        notices, responses, order = [], {}, []
        started = time.monotonic()
        status_id = checkpoint_id = None
        status_started = None
        while run_id not in responses:
            value = client.messages.get(timeout=15)
            if value.get('method') == 'notifications/progress':
                assert value['params']['progressToken'] == 'native014'
                notices.append(value['params'])
                if status_id is None and '단계' in value['params'].get('message', ''):
                    client.index += 1
                    status_id = client.index
                    client.index += 1
                    checkpoint_id = client.index
                    status_started = time.monotonic()
                    for request_id, name in ((status_id, 'computer_activity'), (checkpoint_id, 'computer_task_progress')):
                        client.send({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                                     'params': {'name': name, 'arguments': {}}})
                continue
            assert 'error' not in value, value
            responses[value['id']] = value['result']
            order.append(value['id'])
            if value['id'] == status_id:
                report['live_read_latency_ms'] = round((time.monotonic()-status_started)*1000, 2)
        assert status_id in responses and checkpoint_id in responses, 'Read calls were blocked behind execution'
        assert order[-1] == run_id
        activity = decoded(responses[status_id])
        assert activity['active'] is True and activity['step_total'] == 1
        assert activity['screen_state_verified'] is False and activity['task_verified'] is False
        assert not responses[checkpoint_id].get('isError')
        assert not responses[run_id].get('isError'), responses[run_id]
        result = decoded(responses[run_id])
        assert result['business_result_verified'] is False
        assert len(notices) >= 3
        assert not call('computer_activity')['active']
        report.update(passed=True, progress_notifications=len(notices), replies_before_completion=True,
                      elapsed_ms=round((time.monotonic()-started)*1000, 2), final_status=result['status'])
        call('computer_end')
    finally:
        if client:
            client.close()
        if owned:
            owned.close()
        if proc:
            proc.wait(timeout=10)
        report['owned_fixture_closed'] = proc is None or proc.poll() is not None
        (folder/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent/'.data/status014')
    parser.add_argument('--driver', type=Path, default=DRIVER)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    prepared = prepare(args.folder, args.driver)
    print(json.dumps(run(prepared, args.bundle) if args.run else prepared, ensure_ascii=False, indent=2))
