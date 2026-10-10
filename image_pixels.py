"""Small bounded PNG pixel operations for local evidence, without extra packages.

Only the non-interlaced 8-bit PNG formats already accepted by image_targets are
decoded. Pixels never leave the caller or acquire a desktop input permission.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import uuid
import zlib

from operations import OperationError


_UNSUPPORTED_NATIVE = set()


def _native_crop(encoded, regions, *, helper=None):
    """Fixed local decoder; bounded files/process, no UI and no network.

    Older installed helpers lack --crop. Cache that version's absence and use
    the portable decoder; updating the helper automatically clears that fact.
    """
    if os.name != 'nt': return None
    helper = Path(helper) if helper is not None else Path(__file__).with_name('Computer Use MCP 이미지 도구.exe')
    if not helper.is_file(): return None
    from learning import ElementLibrary
    ElementLibrary._reject_link(helper, file=True)
    stat = helper.stat()
    version = (str(helper), stat.st_mtime_ns, stat.st_size)
    if version in _UNSUPPORTED_NATIVE: return None
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        with tempfile.TemporaryDirectory(prefix='computer-image-crop-') as directory:
            request, response = Path(directory)/'request.json', Path(directory)/'response.json'
            request.write_text(json.dumps({'nonce': nonce, 'screenshot_png': encoded, 'regions': regions}), encoding='utf-8')
            process = subprocess.run([str(helper), '--crop', str(request), str(response)], capture_output=True,
                timeout=4, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if process.returncode == 2 and not response.is_file():
                _UNSUPPORTED_NATIVE.clear(); _UNSUPPORTED_NATIVE.add(version)
                return None
            if process.returncode or not response.is_file() or response.stat().st_size > 24_000_000:
                raise OperationError('로컬 이미지 영역 처리에 실패했습니다.', 'image_crop_failed')
            result = json.loads(response.read_text(encoding='utf-8-sig'))
            crops = result.get('crops')
            from image_targets import png_dimensions
            if (result.get('nonce') != nonce or result.get('status') != 'cropped' or not isinstance(crops, list)
                    or len(crops) != len(regions) or any(png_dimensions(crop) != (region['width'], region['height'])
                        for crop, region in zip(crops, regions))):
                raise OperationError('이미지 영역 처리 결과가 요청한 영역과 다릅니다.', 'image_crop_invalid')
            return crops
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        if isinstance(error, OperationError): raise
        raise OperationError('로컬 이미지 영역을 처리하지 못했습니다.', 'image_crop_failed') from error


def decode_png(encoded):
    from image_targets import png_dimensions
    width, height = png_dimensions(encoded)
    raw = base64.b64decode(encoded, validate=True)
    color = raw[25]
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    stride = width * channels
    position, compressed = 8, bytearray()
    while position < len(raw):
        size = struct.unpack('>I', raw[position:position+4])[0]
        if raw[position+4:position+8] == b'IDAT':
            compressed.extend(raw[position+8:position+8+size])
        position += size + 12
    expected = height * (stride + 1)
    decoder = zlib.decompressobj()
    try:
        filtered = decoder.decompress(compressed, expected + 1)
    except zlib.error:
        raise OperationError('PNG 픽셀을 읽지 못했습니다.', 'invalid_image') from None
    if (len(filtered) != expected or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data):
        raise OperationError('PNG 픽셀 길이가 이미지 크기와 다릅니다.', 'invalid_image')
    rows, previous = [], bytearray(stride)
    for y in range(height):
        offset = y * (stride + 1)
        kind = filtered[offset]
        row = bytearray(filtered[offset+1:offset+1+stride])
        if kind not in range(5):
            raise OperationError('지원하지 않는 PNG 필터입니다.', 'invalid_image')
        if kind:
            for index in range(stride):
                left = row[index-channels] if index >= channels else 0
                up = previous[index]
                corner = previous[index-channels] if index >= channels else 0
                if kind == 1: predictor = left
                elif kind == 2: predictor = up
                elif kind == 3: predictor = (left + up) // 2
                else:
                    estimate = left + up - corner
                    distances = abs(estimate-left), abs(estimate-up), abs(estimate-corner)
                    predictor = (left, up, corner)[distances.index(min(distances))]
                row[index] = (row[index] + predictor) & 255
        rows.append(bytes(row))
        previous = row
    return width, height, color, channels, rows


def encode_png(width, height, color, rows):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)
    raw = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, color, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(b''.join(b'\0'+row for row in rows))) + chunk(b'IEND', b''))
    return base64.b64encode(raw).decode('ascii')


def validate_region(region, width, height, *, maximum=4096):
    if (not isinstance(region, dict) or set(region) != {'x', 'y', 'width', 'height'}
            or any(type(value) is not int for value in region.values())
            or not 1 <= region['width'] <= maximum or not 1 <= region['height'] <= maximum
            or region['x'] < 0 or region['y'] < 0
            or region['x'] + region['width'] > width or region['y'] + region['height'] > height):
        raise OperationError('현재 이미지 안의 x, y, width, height 영역을 지정하세요.', 'invalid_image_region')
    return dict(region)


def crop_regions(encoded, regions):
    from image_targets import png_dimensions
    width, height = png_dimensions(encoded)
    if not isinstance(regions, list) or not 1 <= len(regions) <= 16:
        raise OperationError('이미지 영역은 1~16개로 지정하세요.', 'invalid_image_region')
    regions = [validate_region(region, width, height) for region in regions]
    native = _native_crop(encoded, regions)
    if native is not None: return native
    width, height, color, channels, rows = decode_png(encoded)
    result = []
    for region in regions:
        left, right = region['x']*channels, (region['x']+region['width'])*channels
        selected = [row[left:right] for row in rows[region['y']:region['y']+region['height']]]
        result.append(encode_png(region['width'], region['height'], color, selected))
    return result


def crop_png(encoded, region):
    return crop_regions(encoded, [region])[0]


def pixel_digest(encoded):
    width, height, color, channels, rows = decode_png(encoded)
    digest = hashlib.sha256(struct.pack('>IIB', width, height, color))
    for row in rows:
        digest.update(row)
    return digest.hexdigest()


def target_from_region(encoded, region, *, anchor=None):
    """Create the same immutable image target used by recording and replay."""
    from image_targets import png_dimensions, validate_image_target
    width, height = png_dimensions(encoded)
    region = validate_region(region, width, height, maximum=512)
    return validate_image_target({'format': 'computer-image-target/v1',
        'template_png': crop_png(encoded, region), 'width': region['width'], 'height': region['height'],
        'anchor': anchor or {'x': .5, 'y': .5}, 'capture_window': {'width': width, 'height': height},
        'min_score': .98, 'ambiguity_margin': .03})
