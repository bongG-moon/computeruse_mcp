"""Read-only MSAA list recovery for legacy image recordings; no stored HWND edits."""
import copy
import json
from pathlib import Path
import subprocess
import time

from operations import OperationError
from image_targets import match_image, png_dimensions
from image_pixels import crop_png

HELPER = 'Computer Use MCP MSAA 목록.exe'


def read_lists(runtime, target, timeout=2):
    from vendor.guard import validate_arguments
    runtime.check_active()
    guard = runtime.guard
    validate_arguments('get_window_state', target, guard.policy, guard.process_resolver, guard.window_resolver)
    path = Path(__file__).with_name(HELPER)
    if not path.is_file():
        raise OperationError('MSAA 목록 도우미가 없습니다. 전체 설치본을 업데이트하세요.', 'msaa_helper_missing')
    process = subprocess.Popen([str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + timeout
    payload = json.dumps(target) + '\n'
    try:
        while True:
            runtime.check_active()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise OperationError('MSAA 목록 응답 시간이 초과됐습니다. 입력하지 않았습니다.', 'msaa_timeout')
            try:
                output, _ = process.communicate(payload, timeout=min(.1, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
        data = json.loads(output)
        if process.returncode or data.get('ok') is not True:
            raise OperationError('MSAA 목록 속성을 읽지 못했습니다.', data.get('code', 'msaa_read_failed'))
        if (any(data.get(k) != v for k,v in target.items()) or data.get('complete') is not True
                or data.get('read_only') is not True or not isinstance(data.get('lists'), list)
                or len(data['lists']) > 32):
            raise OperationError('MSAA 목록 범위를 확인하지 못했습니다.', 'msaa_invalid_response')
        runtime.check_active()
        validate_arguments('get_window_state', target, guard.policy, guard.process_resolver, guard.window_resolver)
        return data
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def regions(data, size):
    """Only accept a unique physical-pixel frame contract; never guess DPI."""
    frames = {tuple(f.get(k) for k in ('x','y','width','height')) for f in data.get('frames', [])
              if isinstance(f, dict) and all(type(f.get(k)) is int for k in ('x','y','width','height'))
              and (f.get('width'), f.get('height')) == size}
    if len(frames) != 1:
        raise OperationError('MSAA 영역과 캡처의 좌표계를 확인하지 못했습니다.', 'msaa_geometry_mismatch')
    left, top, width, height = next(iter(frames))
    found = []
    for row in data['lists']:
        r = row.get('screen_rect', {})
        if row.get('role') != 'List' or any(type(r.get(k)) is not int for k in ('x','y','width','height')):
            raise OperationError('MSAA 목록 위치가 올바르지 않습니다.', 'msaa_invalid_response')
        x,y,w,h = r['x']-left,r['y']-top,r['width'],r['height']
        if w<8 or h<8 or x<0 or y<0 or x+w>width or y+h>height:
            continue
        rect={'x':x,'y':y,'width':w,'height':h}
        if rect not in found: found.append(rect)
    return found


def match_lists(runtime, image, png, data):
    size=png_dimensions(png)
    deadline=time.monotonic()+3
    class Budget:
        def __getattr__(self, key): return getattr(runtime,key)
        def check_active(self):
            runtime.check_active()
            if time.monotonic()>=deadline:
                raise OperationError('MSAA 목록 이미지 확인 시간이 초과됐습니다.', 'msaa_match_timeout')
    bounded=Budget()
    candidates=regions(data,size)
    if len(candidates)>4:
        raise OperationError('보이는 MSAA 목록이 많아 자동 선택하지 않았습니다.', 'msaa_scope_limit')
    matches=[]
    ambiguous=False
    for rect in candidates:
        # The old popup capture dimensions describe the dropdown, not its new
        # hosting parent. Keep all pixels/thresholds; don't scale to parent size.
        cropped=crop_png(png,rect,runtime=runtime)
        answer=match_image(bounded,image,cropped)
        if answer.get('status')=='ambiguous': ambiguous=True
        if answer.get('status')!='matched': continue
        answer=copy.deepcopy(answer)
        answer['x']+=rect['x']; answer['y']+=rect['y']
        answer['rect']['x']+=rect['x']; answer['rect']['y']+=rect['y']
        answer['screenshot']={'width':size[0],'height':size[1]}
        matches.append(answer)
    if len(matches)!=1 or ambiguous:
        return {'status':'ambiguous' if matches or ambiguous else 'not_found', 'candidate_count':len(matches),
                'backend':'msaa-list-image', 'input_dispatched':False}
    return {**matches[0], 'backend':'msaa-list-image'}


def recover(runtime, owner, rows, image, probe):
    """Match the existing next-step image inside a visible legacy list."""
    if not image: return None
    roots=[owner]
    for row in probe.snapshot().get('windows', []):
        target={k:row[k] for k in ('pid','window_id')}
        if (target not in roots and target['pid']==owner['pid'] and row.get('visible') is True
                ):
            # Only proven related roots; never scan other same-process apps.
            if probe.ownership_path(target['window_id'],owner['window_id']): roots.append(target)
    if len(roots)>4:
        raise OperationError('관련 창이 많아 MSAA 자동 복구를 확정하지 않았습니다.', 'msaa_scope_limit')
    matches=[]
    for target in roots:
        try: data=read_lists(runtime,target)
        except OperationError as error:
            if error.code=='msaa_provider_unavailable': continue
            raise
        if not data['lists']: continue
        capture=runtime.capture_checkpoint(target)
        images=[x['data'] for x in capture.get('content',[]) if x.get('type')=='image']
        if capture.get('isError') or len(images)!=1:
            raise OperationError('MSAA 목록의 현재 화면을 확인하지 못했습니다.', 'msaa_capture_failed')
        matched=match_lists(runtime,image,images[0],data)
        if matched['status']=='ambiguous':
            raise OperationError('기존 녹화 이미지에 맞는 목록이 여러 개입니다.', 'recording_window_ambiguous')
        if matched['status']=='matched': matches.append(target)
    if len(matches)>1:
        raise OperationError('여러 창의 목록이 녹화 이미지와 일치합니다.', 'recording_window_ambiguous')
    return matches[0] if matches else None
