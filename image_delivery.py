"""Screen-free image roundtrip check. No model/network calls or saved answers."""
from __future__ import annotations

import base64
import secrets
import struct
import threading
import time
import uuid
import zlib

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

    def status(self):
        with self.lock:
            return {'supported': True, 'tool': 'computer_check_image', 'last_roundtrip': self.last_status,
                    'model_vision_guaranteed': False, 'scope': 'current_mcp_connection',
                    'screen_accessed': False, 'external_api_called': False}

    def check(self, challenge_id=None, answer=None):
        with self.lock:
            if challenge_id is None and answer is None:
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
                return {'status':'expired_or_unknown','roundtrip_verified':False,'screen_accessed':False,
                        'next_step':'새 이미지 확인을 시작하세요. 화면 작업은 실행하지 않았습니다.'}
            self.pending = None
            passed = secrets.compare_digest(answer.strip().upper().encode('utf-8'),current[1].encode('ascii'))
            self.last_status = 'passed' if passed else 'failed'
            return {'status':self.last_status,'roundtrip_verified':passed,'screen_accessed':False,
                    'external_api_called':False,'task_verified':False,'model_vision_guaranteed':False,
                    'message':'이번 이미지 응답을 정확히 읽었습니다. 실제 업무 화면의 인식 정확도는 별도입니다.' if passed else '이미지 답이 일치하지 않습니다. 클라이언트의 이미지 전달 또는 모델의 이미지 이해를 확인하세요.'}
