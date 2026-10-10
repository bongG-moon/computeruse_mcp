"""Screen-free image roundtrip check. No model/network calls or saved answers."""
from __future__ import annotations

import base64
import copy
import json
import secrets
import struct
import threading
import time
import uuid
import zlib


def _without_image_bytes(value, count, known_pixels=()):
    """Clean legacy structured/JSON/resource copies at the client boundary.

    Dimensions and ordinary UIA text remain useful; encoded pixels do not.
    This never changes the internal Driver response used for local matching.
    """
    if isinstance(value, list):
        return [_without_image_bytes(item, count, known_pixels) for item in value]
    if isinstance(value, dict):
        mime = value.get('mimeType', value.get('mime_type', ''))
        if value.get('type') == 'image' or isinstance(mime, str) and mime.startswith('image/'):
            count[0] += 1
            # An embedded screenshot object can also carry the dimensions or
            # physical window rectangle needed to diagnose a coordinate issue.
            geometry = {key: _without_image_bytes(value[key], count, known_pixels) for key in
                        ('width', 'height', 'screenshot_width', 'screenshot_height', 'window_bounds', 'capture_coverage') if key in value}
            return {'image_withheld': True, 'mimeType': mime or 'image/unknown', **geometry}
        result = {}
        for key, item in value.items():
            if key.lower() in {'screenshot', 'screenshot_base64', 'image_base64', 'png_base64', 'template_png'} and isinstance(item, str):
                count[0] += 1
                result[key] = '[image withheld]'
            else:
                result[key] = _without_image_bytes(item, count, known_pixels)
        return result
    if isinstance(value, str):
        # An independent JSON copy can contain an image even without a public
        # MCP ImageContent block (notably Driver diagnostics and zoom).
        stripped = value.lstrip()
        if stripped.startswith(('{', '[')):
            try:
                before = count[0]
                cleaned = _without_image_bytes(json.loads(value), count, known_pixels)
                return json.dumps(cleaned, ensure_ascii=False) if count[0] != before else value
            except (ValueError, TypeError, RecursionError):
                pass
        if value.startswith('data:image/') or value.startswith(('iVBORw0KGgo', '/9j/')) and len(value) > 80:
            count[0] += 1
            return '[image withheld]'
        for pixels in known_pixels:
            if pixels in value:
                count[0] += 1
                value = value.replace(pixels, '[image withheld]')
    return value

GLYPHS = {
    '2':['11110','00001','00001','01110','10000','10000','11111'],
    '3':['11110','00001','00001','01110','00001','00001','11110'],
    '4':['10010','10010','10010','11111','00010','00010','00010'],
    '5':['11111','10000','10000','11110','00001','00001','11110'],
    '6':['01110','10000','10000','11110','10001','10001','01110'],
    '7':['11111','00001','00010','00100','01000','01000','01000'],
    '8':['01110','10001','10001','01110','10001','10001','01110'],
    '9':['01110','10001','10001','01111','00001','00001','01110'],
    'A':['01110','10001','10001','11111','10001','10001','10001'],
    'B':['11110','10001','10001','11110','10001','10001','11110'],
    'C':['01111','10000','10000','10000','10000','10000','01111'],
    'D':['11110','10001','10001','10001','10001','10001','11110'],
    'E':['11111','10000','10000','11110','10000','10000','11111'],
    'F':['11111','10000','10000','11110','10000','10000','10000'],
    'H':['10001','10001','10001','11111','10001','10001','10001'],
    'K':['10001','10010','10100','11000','10100','10010','10001'],
    'L':['10000','10000','10000','10000','10000','10000','11111'],
    'M':['10001','11011','10101','10101','10001','10001','10001'],
    'N':['10001','11001','11001','10101','10011','10011','10001'],
    'P':['11110','10001','10001','11110','10000','10000','10000'],
    'R':['11110','10001','10001','11110','10100','10010','10001'],
    'T':['11111','00100','00100','00100','00100','00100','00100'],
    'W':['10001','10001','10001','10101','10101','11011','10001'],
    'X':['10001','10001','01010','00100','01010','10001','10001'],
    'Y':['10001','10001','01010','00100','00100','00100','00100'],
    'Z':['11111','00001','00010','00100','01000','10000','11111'],
}


def image_png(code):
    scale, width, height = 8, 376, 120
    pixels = bytearray(bytes((245,248,252)) * width * height)
    for index, letter in enumerate(code):
        for gy, row in enumerate(GLYPHS[letter]):
            for gx, bit in enumerate(row):
                if bit != '1':
                    continue
                for sy in range(scale):
                    for sx in range(scale):
                        x, y = 24 + index*56 + gx*scale + sx, 32 + gy*scale + sy
                        offset = (y*width+x)*3
                        pixels[offset:offset+3] = bytes((22,52,86))
    raw = b''.join(b'\0'+pixels[y*width*3:(y+1)*width*3] for y in range(height))
    def chunk(kind, data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


class ImageDelivery:
    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.pending = None
        self.last_status = 'not_tested'
        # MCP cannot infer the model behind a client. A screenshot must never
        # accidentally make a text-only model reject the entire conversation.
        self.delivery_mode = 'text'

    def status(self):
        with self.lock:
            return {'supported': True, 'tool': 'computer_check_image', 'last_roundtrip': self.last_status,
                    'delivery_mode': self.delivery_mode, 'images_sent_to_model': self.delivery_mode == 'vision',
                    'model_vision_guaranteed': False, 'scope': 'current_mcp_connection',
                    'screen_accessed': False, 'external_api_called': False}

    def configure(self, delivery_mode):
        """Explicit host capability selection; not a claim of image understanding."""
        if delivery_mode not in {'text', 'vision'}:
            raise ValueError('delivery_mode는 text 또는 vision이어야 합니다.')
        with self.lock:
            self.delivery_mode = delivery_mode
            self.pending = None
            self.last_status = 'not_tested'
        return self.status()

    def check(self, challenge_id=None, answer=None, delivery_mode=None):
        with self.lock:
            if delivery_mode is not None:
                if delivery_mode not in {'text', 'vision'} or challenge_id is not None or answer is not None:
                    raise ValueError('delivery_mode는 text 또는 vision으로 단독 지정하세요.')
                self.delivery_mode = delivery_mode
                self.pending = None
                self.last_status = 'not_tested'
            if challenge_id is None and answer is None:
                if self.delivery_mode != 'vision':
                    return {'status': 'text_safe', 'delivery_mode': 'text', 'images_sent_to_model': False,
                            'screen_accessed': False, 'external_api_called': False, 'task_verified': False,
                            'message': '텍스트 전용 모델에서도 바로 사용할 수 있습니다. 로컬 이미지 찾기와 녹화는 계속 작동하며 확인 화면은 이 PC에서 표시합니다.',
                            'next_step': '연결 모델이 이미지를 지원한다고 사용자가 확인한 경우에만 delivery_mode=vision으로 이미지 전달 시험을 시작하세요. 설정 파일 수정이나 재연결은 필요 없습니다.'}
                code = ''.join(secrets.choice(tuple(GLYPHS)) for _ in range(6))
                identity = uuid.uuid4().hex
                self.pending = (identity,code,self.clock()+300)
                return {'status':'awaiting_image_readback','challenge_id':identity,'expires_in_seconds':300,
                        'screen_accessed':False,'external_api_called':False,'task_verified':False,
                        'next_step':'반환된 이미지의 문자 6개를 읽고 같은 computer_check_image에 challenge_id와 answer로 제출하세요. 이미지가 보이지 않으면 성공했다고 하지 말고 클라이언트의 이미지 전달·모델 지원을 확인하세요.',
                        'image_content':{'type':'image','mimeType':'image/png','data':base64.b64encode(image_png(code)).decode()}}
            if not isinstance(challenge_id,str) or not isinstance(answer,str) or len(answer)>40:
                raise ValueError('challenge_id와 이미지에서 읽은 answer를 함께 전달하세요.')
            current = self.pending
            if current is None or current[0]!=challenge_id or self.clock()>current[2]:
                self.pending = None
                self.delivery_mode = 'text'
                self.last_status = 'expired_or_unknown'
                return {'status':'expired_or_unknown','roundtrip_verified':False,'screen_accessed':False,
                        'next_step':'새 이미지 확인을 시작하세요. 화면 작업은 실행하지 않았습니다.'}
            self.pending = None
            passed = secrets.compare_digest(answer.strip().upper().encode('utf-8'),current[1].encode('ascii'))
            self.last_status = 'passed' if passed else 'failed'
            if not passed:
                self.delivery_mode = 'text'
            return {'status':self.last_status,'roundtrip_verified':passed,'screen_accessed':False,
                    'external_api_called':False,'task_verified':False,'model_vision_guaranteed':False,
                    'message':'이번 이미지 응답을 정확히 읽었습니다. 실제 업무 화면의 인식 정확도는 별도입니다.' if passed else '이미지 답이 일치하지 않습니다. 클라이언트의 이미지 전달 또는 모델의 이미지 이해를 확인하세요.'}

    def filter_response(self, response, *, local_review=None):
        """One final boundary for *every* public tool, including Driver tools.

        Internal capture/matching retains its pixels. Never turn image bytes
        into base64 text or image resources as a fallback for a text model.
        """
        with self.lock:
            if self.delivery_mode == 'vision':
                return response
        if not isinstance(response, dict):
            return response
        content = response.get('content', [])
        images = [item for item in content if isinstance(item, dict) and item.get('type') == 'image']
        def image_resource(item):
            if not isinstance(item, dict):
                return False
            resource = item.get('resource', {})
            return (str(item.get('mimeType', '')).startswith('image/')
                    or isinstance(resource, dict) and str(resource.get('mimeType', '')).startswith('image/'))
        blocked = [item for item in content if item in images or image_resource(item)]
        removed = [len(blocked) - len(images)]
        pixels = tuple(item['data'] for item in images if isinstance(item.get('data'), str) and item['data'])
        # Remove normal image blocks first; sanitize all remaining wire fields,
        # including errors that contain only a legacy JSON/base64 screenshot.
        source = {**response, 'content': [item for item in content if item not in blocked]}
        answer = _without_image_bytes(source, removed, pixels)
        if not images and not removed[0]:
            return response
        metadata = answer.get('structuredContent')
        metadata = metadata if isinstance(metadata, dict) else {}
        delivery = {'delivery_mode': 'text', 'images_sent_to_model': False, 'image_count_withheld': len(images),
                    'embedded_image_copies_withheld': removed[0],
                    'model_image_understanding_verified': False,
                    'message': '텍스트 전용 연결이므로 화면 이미지를 모델에 보내지 않았습니다. 이미지 내용을 읽었다고 판단하지 마세요.'}
        if images and callable(local_review):
            delivery['local_review'] = local_review(images, metadata)
        metadata['image_delivery'] = delivery
        metadata['model_visual_evidence_available'] = False
        review = delivery.get('local_review', {})
        if review.get('review_id'):
            metadata['next_tool'] = review.get('next_tool', 'computer_review_checkpoint')
            metadata['next_step'] = review.get('message', '')
            metadata['local_review'] = review
        else:
            metadata['next_step'] = review.get('next_step', '모델에는 이미지가 전달되지 않았습니다. 현재 요소 정보 또는 로컬 편집창의 직접 선택으로 계속하세요.')
        # Keep useful accessibility/diagnostic text, remove any old JSON copy
        # of structuredContent so it cannot contradict the delivery status.
        # Compare to the already sanitized original copy, before adding our
        # delivery envelope. This avoids duplicate/conflicting JSON summaries.
        old = _without_image_bytes(response.get('structuredContent'), [0], pixels)
        remaining = []
        for item in answer.get('content', []):
            if not isinstance(item, dict) or item.get('type') == 'image':
                continue
            if item.get('type') == 'text' and isinstance(old, dict):
                try:
                    if json.loads(item.get('text', '')) == old:
                        continue
                except (ValueError, TypeError):
                    pass
            remaining.append(item)
        answer['structuredContent'] = metadata
        answer['content'] = [{'type': 'text', 'text': json.dumps(metadata, ensure_ascii=False)}, *remaining]
        return answer
