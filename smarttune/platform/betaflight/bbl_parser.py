"""
smarttune/platform/betaflight/bbl_parser.py

Betaflight Blackbox Log (BBL/BFL) 解析器 — 纯 Python 实现，零外部依赖。

BBL 格式概览:
  1. 文件头区: "H " 开头的文本行（键值对）
  2. 帧定义: "H Field I name:", "H Field P name:" 等定义每帧字段
  3. 数据帧: 二进制变长编码
     - I-frame (0x49): 关键帧，完整值
     - P-frame (0x50): 差值帧，相对于上一 I/P 帧
     - S-frame (0x53): 慢帧（GPS 等低频数据）
     - E-frame (0x45): 事件帧（飞行模式切换等）
     - H-frame (0x48): 头信息（文本行，出现在数据流中）

编码方式:
  - unsigned VB (Variable Byte): 7-bit 编码，MSB=1 表示还有后续字节
  - signed VB: unsigned VB + ZigZag 解码
  - neg_14bit: 14位值，高位为符号位
  - Elias delta / tag8_8SVB / tag2_3S32 / tag8_4S16 等预测器编码

参考:
  - https://github.com/betaflight/blackbox-log-viewer
  - https://github.com/betaflight/betaflight/blob/master/src/main/blackbox/blackbox.c
"""

from __future__ import annotations

import io
import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLIGHT_LOG_FIELD_UNSIGNED = 0
FLIGHT_LOG_FIELD_SIGNED = 1

# 帧类型标识字节
FRAME_TYPE_I = ord('I')
FRAME_TYPE_P = ord('P')
FRAME_TYPE_S = ord('S')  # Slow frame
FRAME_TYPE_E = ord('E')  # Event frame
FRAME_TYPE_H = ord('H')  # Header continuation

# 事件类型
EVENT_SYNC_BEEP = 0
EVENT_INFLIGHT_ADJUSTMENT = 13
EVENT_LOGGING_RESUME = 14
EVENT_FLIGHT_MODE = 30
EVENT_LOG_END = 255

# 预测器类型 — 决定 I-frame 中如何编码原始值
PREDICTOR_0 = 0             # 无预测，直接编码
PREDICTOR_PREVIOUS = 1      # 前一帧的值作为预测
PREDICTOR_STRAIGHT_LINE = 2 # 线性外推
PREDICTOR_AVERAGE_2 = 3     # 两帧均值
PREDICTOR_MINTHROTTLE = 4   # 最低油门
PREDICTOR_MOTOR_0 = 5       # motor[0] 值
PREDICTOR_INC = 6           # 递增 1
PREDICTOR_HOME_COORD = 7    # Home GPS 坐标
PREDICTOR_1500 = 8          # 常量 1500
PREDICTOR_VBATREF = 9       # 电池参考电压
PREDICTOR_LAST_MAIN_FRAME_TIME = 10  # 上一主帧时间

# 编码类型 — 决定如何从二进制流中读取值
ENCODING_SIGNED_VB = 0
ENCODING_UNSIGNED_VB = 1
ENCODING_NEG_14BIT = 3
ENCODING_TAG8_8SVB = 6
ENCODING_TAG2_3S32 = 7
ENCODING_TAG8_4S16 = 8
ENCODING_NULL = 9
ENCODING_TAG2_3SVARIABLE = 10


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FrameFieldDef:
    """BBL 帧字段定义。"""
    name: str
    signed: int = FLIGHT_LOG_FIELD_SIGNED
    predictor: int = PREDICTOR_0
    encoding: int = ENCODING_SIGNED_VB


@dataclass
class BBLHeader:
    """BBL 文件头解析结果。"""
    product: str = ""
    data_version: int = 0
    firmware_type: str = ""
    firmware_revision: str = ""
    firmware_date: str = ""
    board_info: str = ""
    craft_name: str = ""
    # I-frame 字段定义
    i_field_defs: List[FrameFieldDef] = field(default_factory=list)
    # P-frame 字段定义
    p_field_defs: List[FrameFieldDef] = field(default_factory=list)
    # S-frame (slow) 字段定义
    s_field_defs: List[FrameFieldDef] = field(default_factory=list)
    # 其他头信息
    properties: Dict[str, str] = field(default_factory=dict)
    # I-interval
    i_interval: int = 32
    # P-frame 跟 I-frame 用相同字段
    p_ratio: int = 1

    @property
    def field_names_i(self) -> List[str]:
        return [f.name for f in self.i_field_defs]

    @property
    def field_names_p(self) -> List[str]:
        return [f.name for f in self.p_field_defs]


@dataclass
class BBLFrame:
    """一帧解码后的数据。"""
    frame_type: str  # 'I', 'P', 'S', 'E'
    values: Dict[str, int]  # 字段名 → 原始值
    time_us: int = 0  # 微秒时间戳


@dataclass
class BBLEvent:
    """事件帧数据。"""
    event_type: int
    time_us: int = 0
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BBLLogSegment:
    """单段飞行日志 (一个 BBL 文件可含多段)。"""
    header: BBLHeader
    frames: List[BBLFrame] = field(default_factory=list)
    events: List[BBLEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Binary stream reader
# ---------------------------------------------------------------------------

class BBLStreamReader:
    """BBL 二进制流读取器 — 封装变长编码解码。"""

    def __init__(self, data: bytes, offset: int = 0):
        self._data = data
        self._pos = offset
        self._len = len(data)

    @property
    def pos(self) -> int:
        return self._pos

    @pos.setter
    def pos(self, value: int):
        self._pos = value

    @property
    def remaining(self) -> int:
        return self._len - self._pos

    def has_data(self, n: int = 1) -> bool:
        return self._pos + n <= self._len

    def read_byte(self) -> int:
        if self._pos >= self._len:
            raise EOFError("Unexpected end of BBL data")
        b = self._data[self._pos]
        self._pos += 1
        return b

    def peek_byte(self) -> int:
        if self._pos >= self._len:
            raise EOFError("Unexpected end of BBL data")
        return self._data[self._pos]

    def read_bytes(self, n: int) -> bytes:
        if self._pos + n > self._len:
            raise EOFError("Unexpected end of BBL data")
        result = self._data[self._pos:self._pos + n]
        self._pos += n
        return result

    def read_unsigned_vb(self) -> int:
        """读取 unsigned Variable Byte 编码值。

        每字节低 7 位为数据，最高位=1 表示后续还有字节。
        最多读取 5 字节 (35-bit 值)。
        """
        result = 0
        shift = 0
        for _ in range(5):
            b = self.read_byte()
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                return result
            shift += 7
        return result

    def read_signed_vb(self) -> int:
        """读取 signed Variable Byte 编码值 (ZigZag)。"""
        raw = self.read_unsigned_vb()
        # ZigZag 解码: 偶数→正, 奇数→负
        return (raw >> 1) ^ -(raw & 1)

    def read_neg_14bit(self) -> int:
        """读取 neg_14bit 编码值。

        2 字节, 14-bit 值 + 符号位。
        如果值 < 16384，直接读 unsigned VB。
        """
        val = self.read_unsigned_vb()
        if val >= 16384:
            val -= 16384
            val = -val
        return val

    def read_tag2_3s32(self) -> List[int]:
        """读取 tag2_3S32 编码 — 3 个 signed 值打包。

        第一字节的低 2 位是 tag:
          00: 3 个值都是 0
          01: 3 个 4-bit signed 值打包在 2 字节
          10: 3 个 8-bit signed 值打包在 3 字节
          11: 3 个独立的 signed VB
        """
        header = self.read_byte()
        tag = header & 0x03

        if tag == 0:
            return [0, 0, 0]

        elif tag == 1:
            # 3 个 4-bit signed 值：第一字节的高 6 位 + 第二字节的低 6 位
            byte2 = self.read_byte()
            combined = (header >> 2) | (byte2 << 6)
            values = []
            for i in range(3):
                nibble = (combined >> (i * 4)) & 0x0F
                # 4-bit signed: if bit3 set, negative
                if nibble & 0x08:
                    nibble -= 16
                values.append(nibble)
            return values

        elif tag == 2:
            # 3 个 8-bit signed 值
            byte2 = self.read_byte()
            byte3 = self.read_byte()
            combined = (header >> 2) | (byte2 << 6) | (byte3 << 14)
            values = []
            for i in range(3):
                val = (combined >> (i * 8)) & 0xFF
                if val & 0x80:
                    val -= 256
                values.append(val)
            return values

        else:  # tag == 3
            return [self.read_signed_vb() for _ in range(3)]

    def read_tag8_4s16(self) -> List[int]:
        """读取 tag8_4S16 编码 — 4 个值，tag 字节指示每个字段宽度。

        tag 字节每 2 位描述对应字段：
          00: 值为 0
          01: 4-bit signed
          10: 8-bit signed
          11: 16-bit signed
        """
        tag = self.read_byte()
        values = []
        for i in range(4):
            field_tag = (tag >> (i * 2)) & 0x03
            if field_tag == 0:
                values.append(0)
            elif field_tag == 1:
                # 4-bit signed in lower nibble of a byte
                raw = self.read_byte()
                # Only use the raw byte value, sign extend from 8 bits
                if raw & 0x80:
                    raw -= 256
                values.append(raw)
            elif field_tag == 2:
                raw = self.read_byte()
                if raw & 0x80:
                    raw -= 256
                values.append(raw)
            else:
                # 16-bit little-endian signed
                lo = self.read_byte()
                hi = self.read_byte()
                val = lo | (hi << 8)
                if val & 0x8000:
                    val -= 65536
                values.append(val)
        return values

    def read_tag8_8svb(self, count: int) -> List[int]:
        """读取 tag8_8SVB 编码 — N 个值，tag 字节的每一位指示是否跟一个 signed VB。

        tag 的第 i 位 = 0 → 值为 0
        tag 的第 i 位 = 1 → 从流中读取 signed VB
        """
        tag = self.read_byte()
        values = []
        for i in range(min(count, 8)):
            if tag & (1 << i):
                values.append(self.read_signed_vb())
            else:
                values.append(0)
        # 如果 count > 8，剩余字段直接读 signed VB
        for _ in range(count - 8):
            values.append(self.read_signed_vb())
        return values

    def read_tag2_3svariable(self) -> List[int]:
        """读取 tag2_3SVARIABLE 编码 — 类似 tag2_3s32 但用于变宽值。"""
        # 实际上和 tag2_3s32 非常相似，BF 源码中用法一致
        return self.read_tag2_3s32()

    def skip_to_frame(self) -> Optional[int]:
        """跳过损坏数据，定位到下一个有效帧起始。

        返回帧类型字节，或 None 如果到达末尾。
        """
        valid_frame_types = {FRAME_TYPE_I, FRAME_TYPE_P, FRAME_TYPE_S,
                             FRAME_TYPE_E, FRAME_TYPE_H}
        while self._pos < self._len:
            b = self._data[self._pos]
            if b in valid_frame_types:
                return b
            self._pos += 1
        return None

    def read_line(self) -> Optional[str]:
        """读取一行文本 (到 \\n)，返回去除换行的字符串。"""
        start = self._pos
        while self._pos < self._len:
            if self._data[self._pos] == 0x0A:  # \n
                line = self._data[start:self._pos].decode("ascii", errors="replace")
                self._pos += 1
                return line.rstrip('\r')
            self._pos += 1
        # 到末尾了
        if self._pos > start:
            return self._data[start:self._pos].decode("ascii", errors="replace").rstrip('\r')
        return None


# ---------------------------------------------------------------------------
# Header parser
# ---------------------------------------------------------------------------

def parse_header(reader: BBLStreamReader) -> BBLHeader:
    """解析 BBL 文件头 — 所有 "H " 开头的文本行。

    头区以第一个非 "H " 开头的字节结束（即遇到 I/P/E 帧标记）。
    """
    header = BBLHeader()

    # 收集原始字段名/签名/预测器/编码
    i_field_names: List[str] = []
    i_field_signed: List[int] = []
    i_field_predictor: List[int] = []
    i_field_encoding: List[int] = []

    p_field_names: List[str] = []
    p_field_signed: List[int] = []
    p_field_predictor: List[int] = []
    p_field_encoding: List[int] = []

    s_field_names: List[str] = []
    s_field_signed: List[int] = []
    s_field_predictor: List[int] = []
    s_field_encoding: List[int] = []

    while reader.has_data():
        # 检查是否还是 H 行
        if reader.peek_byte() != ord('H'):
            break

        line = reader.read_line()
        if line is None:
            break

        # 跳过不以 "H " 开头的行
        if not line.startswith("H "):
            continue

        content = line[2:]  # 去掉 "H "
        if ':' not in content:
            continue

        key, _, value = content.partition(':')
        key = key.strip()
        value = value.strip()

        header.properties[key] = value

        # 解析已知字段
        if key == "Product":
            header.product = value
        elif key == "Data version":
            try:
                header.data_version = int(value)
            except ValueError:
                pass
        elif key == "Firmware type":
            header.firmware_type = value
        elif key == "Firmware revision":
            header.firmware_revision = value
        elif key == "Firmware date":
            header.firmware_date = value
        elif key == "Board information":
            header.board_info = value
        elif key == "Craft name":
            header.craft_name = value
        elif key == "I interval":
            try:
                header.i_interval = int(value)
            except ValueError:
                pass
        elif key == "P interval":
            # 格式: "1/N" 或直接数字
            if '/' in value:
                parts = value.split('/')
                try:
                    header.p_ratio = int(parts[1])
                except (ValueError, IndexError):
                    pass
            else:
                try:
                    header.p_ratio = int(value)
                except ValueError:
                    pass
        # 帧字段定义
        elif key == "Field I name":
            i_field_names = value.split(',')
        elif key == "Field I signed":
            i_field_signed = _parse_int_list(value)
        elif key == "Field I predictor":
            i_field_predictor = _parse_int_list(value)
        elif key == "Field I encoding":
            i_field_encoding = _parse_int_list(value)
        elif key == "Field P predictor":
            p_field_predictor = _parse_int_list(value)
        elif key == "Field P encoding":
            p_field_encoding = _parse_int_list(value)
        elif key == "Field S name":
            s_field_names = value.split(',')
        elif key == "Field S signed":
            s_field_signed = _parse_int_list(value)
        elif key == "Field S predictor":
            s_field_predictor = _parse_int_list(value)
        elif key == "Field S encoding":
            s_field_encoding = _parse_int_list(value)

    # P-frame 字段名和签名复用 I-frame 的定义
    p_field_names = i_field_names[:]
    p_field_signed = i_field_signed[:]

    # 组装字段定义
    n_i = len(i_field_names)
    header.i_field_defs = [
        FrameFieldDef(
            name=i_field_names[i] if i < len(i_field_names) else f"field_{i}",
            signed=i_field_signed[i] if i < len(i_field_signed) else 0,
            predictor=i_field_predictor[i] if i < len(i_field_predictor) else 0,
            encoding=i_field_encoding[i] if i < len(i_field_encoding) else 0,
        )
        for i in range(n_i)
    ]

    n_p = len(p_field_names)
    header.p_field_defs = [
        FrameFieldDef(
            name=p_field_names[i] if i < len(p_field_names) else f"field_{i}",
            signed=p_field_signed[i] if i < len(p_field_signed) else 0,
            predictor=p_field_predictor[i] if i < len(p_field_predictor) else 0,
            encoding=p_field_encoding[i] if i < len(p_field_encoding) else 0,
        )
        for i in range(n_p)
    ]

    n_s = len(s_field_names)
    header.s_field_defs = [
        FrameFieldDef(
            name=s_field_names[i] if i < len(s_field_names) else f"field_{i}",
            signed=s_field_signed[i] if i < len(s_field_signed) else 0,
            predictor=s_field_predictor[i] if i < len(s_field_predictor) else 0,
            encoding=s_field_encoding[i] if i < len(s_field_encoding) else 0,
        )
        for i in range(n_s)
    ]

    return header


def _parse_int_list(s: str) -> List[int]:
    """解析逗号分隔的整数列表。"""
    result = []
    for part in s.split(','):
        part = part.strip()
        if part:
            try:
                result.append(int(part))
            except ValueError:
                result.append(0)
    return result


# ---------------------------------------------------------------------------
# Frame decoders
# ---------------------------------------------------------------------------

def _read_field_value(reader: BBLStreamReader, encoding: int,
                      field_count: int = 1) -> List[int]:
    """根据编码类型读取一个或多个字段值。

    部分编码 (tag8_4S16, tag2_3S32 等) 一次解码多个值。
    返回值列表。
    """
    if encoding == ENCODING_SIGNED_VB:
        return [reader.read_signed_vb()]
    elif encoding == ENCODING_UNSIGNED_VB:
        return [reader.read_unsigned_vb()]
    elif encoding == ENCODING_NEG_14BIT:
        return [reader.read_neg_14bit()]
    elif encoding == ENCODING_TAG8_8SVB:
        return reader.read_tag8_8svb(field_count)
    elif encoding == ENCODING_TAG2_3S32:
        return reader.read_tag2_3s32()
    elif encoding == ENCODING_TAG8_4S16:
        return reader.read_tag8_4s16()
    elif encoding == ENCODING_TAG2_3SVARIABLE:
        return reader.read_tag2_3svariable()
    elif encoding == ENCODING_NULL:
        return [0]
    else:
        # 未知编码 fallback
        return [reader.read_signed_vb()]


def _apply_predictor(predictor: int, raw_value: int,
                     prev_values: Dict[str, int],
                     field_name: str,
                     all_prev: Dict[str, int],
                     header: BBLHeader) -> int:
    """根据预测器类型，将原始编码值转换为实际值。"""
    if predictor == PREDICTOR_0:
        return raw_value
    elif predictor == PREDICTOR_PREVIOUS:
        return raw_value + prev_values.get(field_name, 0)
    elif predictor == PREDICTOR_STRAIGHT_LINE:
        # 需要两帧历史，简化为 previous
        return raw_value + prev_values.get(field_name, 0)
    elif predictor == PREDICTOR_AVERAGE_2:
        return raw_value + prev_values.get(field_name, 0)
    elif predictor == PREDICTOR_MINTHROTTLE:
        minthrottle = int(header.properties.get("minthrottle", "1070"))
        return raw_value + minthrottle
    elif predictor == PREDICTOR_MOTOR_0:
        return raw_value + all_prev.get("motor[0]", 0)
    elif predictor == PREDICTOR_INC:
        return prev_values.get(field_name, 0) + 1 + raw_value
    elif predictor == PREDICTOR_1500:
        return raw_value + 1500
    elif predictor == PREDICTOR_VBATREF:
        vbatref = int(header.properties.get("vbatref", "0"))
        return raw_value + vbatref
    elif predictor == PREDICTOR_LAST_MAIN_FRAME_TIME:
        return raw_value + prev_values.get("loopIteration", 0)
    else:
        return raw_value


def decode_i_frame(reader: BBLStreamReader, header: BBLHeader,
                   prev_values: Dict[str, int]) -> Dict[str, int]:
    """解码 I-frame (关键帧)。

    I-frame 包含所有字段的完整值，使用各字段的 I 编码。
    """
    values: Dict[str, int] = {}
    field_defs = header.i_field_defs

    i = 0
    while i < len(field_defs):
        fdef = field_defs[i]
        encoding = fdef.encoding

        # 多值编码一次读多个字段
        if encoding in (ENCODING_TAG2_3S32, ENCODING_TAG2_3SVARIABLE):
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, values, header)
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_4S16:
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, values, header)
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_8SVB:
            # 计算连续同编码字段数
            count = 0
            while i + count < len(field_defs) and field_defs[i + count].encoding == ENCODING_TAG8_8SVB:
                count += 1
            raw_values = reader.read_tag8_8svb(count)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, values, header)
            i += count
        else:
            raw_values = _read_field_value(reader, encoding)
            raw = raw_values[0]
            values[fdef.name] = _apply_predictor(
                fdef.predictor, raw, prev_values, fdef.name, values, header)
            i += 1

    return values


def decode_p_frame(reader: BBLStreamReader, header: BBLHeader,
                   prev_values: Dict[str, int]) -> Dict[str, int]:
    """解码 P-frame (差值帧)。

    P-frame 的值是相对于上一帧 (I 或 P) 的差值。
    """
    values: Dict[str, int] = {}
    field_defs = header.p_field_defs

    i = 0
    while i < len(field_defs):
        fdef = field_defs[i]
        encoding = fdef.encoding

        if encoding in (ENCODING_TAG2_3S32, ENCODING_TAG2_3SVARIABLE):
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, prev_values, header)
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_4S16:
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, prev_values, header)
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_8SVB:
            count = 0
            while i + count < len(field_defs) and field_defs[i + count].encoding == ENCODING_TAG8_8SVB:
                count += 1
            raw_values = reader.read_tag8_8svb(count)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, prev_values, header)
            i += count
        else:
            raw_values = _read_field_value(reader, encoding)
            raw = raw_values[0]
            values[fdef.name] = _apply_predictor(
                fdef.predictor, raw, prev_values, fdef.name, prev_values, header)
            i += 1

    return values


def decode_s_frame(reader: BBLStreamReader, header: BBLHeader,
                   prev_slow: Dict[str, int]) -> Dict[str, int]:
    """解码 S-frame (慢帧 — GPS 等低频数据)。"""
    values: Dict[str, int] = {}
    field_defs = header.s_field_defs

    i = 0
    while i < len(field_defs):
        fdef = field_defs[i]
        encoding = fdef.encoding

        if encoding in (ENCODING_TAG2_3S32, ENCODING_TAG2_3SVARIABLE):
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_slow, fd.name, prev_slow, header)
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_4S16:
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_slow, fd.name, prev_slow, header)
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_8SVB:
            count = 0
            while i + count < len(field_defs) and field_defs[i + count].encoding == ENCODING_TAG8_8SVB:
                count += 1
            raw_values = reader.read_tag8_8svb(count)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_slow, fd.name, prev_slow, header)
            i += count
        else:
            raw_values = _read_field_value(reader, encoding)
            raw = raw_values[0]
            values[fdef.name] = _apply_predictor(
                fdef.predictor, raw, prev_slow, fdef.name, prev_slow, header)
            i += 1

    return values


def decode_e_frame(reader: BBLStreamReader, header: BBLHeader) -> BBLEvent:
    """解码 E-frame (事件帧)。"""
    event_type = reader.read_unsigned_vb()
    event = BBLEvent(event_type=event_type)

    if event_type == EVENT_SYNC_BEEP:
        event.data["time"] = reader.read_unsigned_vb()
    elif event_type == EVENT_FLIGHT_MODE:
        event.data["flags"] = reader.read_unsigned_vb()
        event.data["last_flags"] = reader.read_unsigned_vb()
    elif event_type == EVENT_INFLIGHT_ADJUSTMENT:
        adj_func = reader.read_unsigned_vb()
        event.data["function"] = adj_func
        if adj_func > 127:
            # Float adjustment
            raw = reader.read_bytes(4)
            event.data["value"] = struct.unpack('<f', raw)[0]
        else:
            event.data["value"] = reader.read_signed_vb()
    elif event_type == EVENT_LOGGING_RESUME:
        event.data["iteration"] = reader.read_unsigned_vb()
        event.data["time"] = reader.read_unsigned_vb()
    elif event_type == EVENT_LOG_END:
        # End of log marker — may have optional string
        pass
    # 其他事件类型暂时跳过

    return event


# ---------------------------------------------------------------------------
# Top-level BBL parser
# ---------------------------------------------------------------------------

def parse_bbl(data: bytes, max_segments: int = 1) -> List[BBLLogSegment]:
    """解析 BBL 二进制数据，返回日志段列表。

    一个 BBL 文件可以包含多次飞行记录（多段），
    每段以新的 "H Product:" 头开始。

    Parameters
    ----------
    data : bytes
        完整 BBL 文件内容
    max_segments : int
        最多解析几段 (默认 1，即只解析第一段飞行)

    Returns
    -------
    List[BBLLogSegment]
        解析后的日志段列表
    """
    segments: List[BBLLogSegment] = []
    reader = BBLStreamReader(data)

    while reader.has_data() and len(segments) < max_segments:
        # 寻找段起始 — "H Product:"
        found = False
        while reader.has_data():
            if reader.peek_byte() == ord('H'):
                # 可能是 header 行
                save_pos = reader.pos
                line = reader.read_line()
                if line and "H Product:" in line:
                    reader.pos = save_pos  # 回退，让 parse_header 重新读
                    found = True
                    break
                # 不是 Product 行，继续
            else:
                reader.pos += 1

        if not found:
            break

        # 解析头区
        header = parse_header(reader)
        segment = BBLLogSegment(header=header)

        if not header.i_field_defs:
            logger.warning("BBL segment has no I-frame field definitions, skipping")
            continue

        # 解析数据帧
        prev_values: Dict[str, int] = {}
        prev_slow: Dict[str, int] = {}
        frame_count = 0
        max_frames = 500_000  # 安全限制

        while reader.has_data() and frame_count < max_frames:
            # 检查是否遇到新段的头
            if reader.peek_byte() == ord('H'):
                save_pos = reader.pos
                line_peek = reader.read_line()
                if line_peek and "H Product:" in line_peek:
                    reader.pos = save_pos
                    break
                # 其他 H 行（数据流中的补充头），跳过
                continue

            frame_marker = reader.read_byte()

            try:
                if frame_marker == FRAME_TYPE_I:
                    values = decode_i_frame(reader, header, prev_values)
                    prev_values = values.copy()
                    frame = BBLFrame(
                        frame_type='I',
                        values=values,
                        time_us=values.get("loopIteration", 0),
                    )
                    segment.frames.append(frame)
                    frame_count += 1

                elif frame_marker == FRAME_TYPE_P:
                    if not prev_values:
                        # 没有先行 I-frame，跳过找下一个帧
                        reader.skip_to_frame()
                        continue
                    values = decode_p_frame(reader, header, prev_values)
                    prev_values = values.copy()
                    frame = BBLFrame(
                        frame_type='P',
                        values=values,
                        time_us=values.get("loopIteration", 0),
                    )
                    segment.frames.append(frame)
                    frame_count += 1

                elif frame_marker == FRAME_TYPE_S:
                    values = decode_s_frame(reader, header, prev_slow)
                    prev_slow = values.copy()
                    # S-frame 不加入主帧列表，但保存供后续使用
                    frame_count += 1

                elif frame_marker == FRAME_TYPE_E:
                    event = decode_e_frame(reader, header)
                    segment.events.append(event)

                else:
                    # 未知帧类型或损坏数据，跳到下一个帧
                    ft = reader.skip_to_frame()
                    if ft is None:
                        break

            except EOFError:
                logger.debug("BBL: reached end of data mid-frame at frame %d", frame_count)
                break
            except Exception as e:
                logger.debug("BBL: error decoding frame %d: %s", frame_count, e)
                # 尝试恢复
                ft = reader.skip_to_frame()
                if ft is None:
                    break

        segments.append(segment)

    return segments


# ---------------------------------------------------------------------------
# Utility: extract flight mode from E-frame flags
# ---------------------------------------------------------------------------

# Betaflight 模式标志位 (从 BF 源码)
BF_MODE_FLAGS = {
    0: "ARM",
    1: "ANGLE",
    2: "HORIZON",
    3: "MAG",         # not used in modern BF
    5: "HEADFREE",
    6: "HEADADJ",
    10: "GPS_HOME",
    11: "GPS_HOLD",
    12: "PASSTHRU",
    15: "FAILSAFE",
    19: "AIR",         # Airmode
    28: "3D",
    36: "TURTLE",
}


def decode_flight_modes(flags: int) -> List[str]:
    """将 BF 模式标志位解码为模式名称列表。"""
    modes = []
    for bit, name in BF_MODE_FLAGS.items():
        if flags & (1 << bit):
            modes.append(name)
    return modes


def get_primary_mode(flags: int) -> str:
    """从模式标志位确定主要飞行模式。"""
    if not (flags & 1):
        return "DISARMED"
    if flags & (1 << 1):
        return "ANGLE"
    if flags & (1 << 2):
        return "HORIZON"
    if flags & (1 << 15):
        return "FAILSAFE"
    # 默认 = ACRO (armed, 无 ANGLE/HORIZON)
    return "ACRO"
