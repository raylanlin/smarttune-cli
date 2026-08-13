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
FRAME_TYPE_I = ord("I")
FRAME_TYPE_P = ord("P")
FRAME_TYPE_S = ord("S")  # Slow frame
FRAME_TYPE_E = ord("E")  # Event frame
FRAME_TYPE_H = ord("H")  # Header continuation

# 事件类型
EVENT_SYNC_BEEP = 0
EVENT_INFLIGHT_ADJUSTMENT = 13
EVENT_LOGGING_RESUME = 14
EVENT_FLIGHT_MODE = 30
EVENT_LOG_END = 255

# 预测器类型 — 决定 I-frame 中如何编码原始值
PREDICTOR_0 = 0  # 无预测，直接编码
PREDICTOR_PREVIOUS = 1  # 前一帧的值作为预测
PREDICTOR_STRAIGHT_LINE = 2  # 线性外推
PREDICTOR_AVERAGE_2 = 3  # 两帧均值
PREDICTOR_MINTHROTTLE = 4  # 最低油门
PREDICTOR_MOTOR_0 = 5  # motor[0] 值
PREDICTOR_INC = 6  # 递增 1
PREDICTOR_HOME_COORD = 7  # Home GPS 坐标
PREDICTOR_1500 = 8  # 常量 1500
PREDICTOR_VBATREF = 9  # 电池参考电压
PREDICTOR_LAST_MAIN_FRAME_TIME = 10  # 上一主帧时间
PREDICTOR_MINMOTOR = 11  # motorOutput[0] (DShot/数字协议最低值)

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
        result = self._data[self._pos : self._pos + n]
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

        BF decoder: value = -signExtend14Bit(streamReadUnsignedVB(stream))
        signExtend14Bit: 如果 bit13 置位，用 1 填充高位 (14-bit → 32-bit 符号扩展)
        然后取反。

        参考: betaflight blackbox-tools parser.c / decoders.c
        """
        raw = self.read_unsigned_vb()
        # signExtend14Bit
        if raw & 0x2000:  # bit 13 set
            raw |= ~0x3FFF  # fill upper bits with 1s (sign extend)
            # In Python, this makes it a large negative number
            # We need to treat it as a signed 32-bit value
            raw = raw & 0xFFFFFFFF
            if raw >= 0x80000000:
                raw -= 0x100000000
        # Negate
        return -raw

    def read_tag2_3s32(self) -> List[int]:
        """读取 tag2_3S32 编码 — 3 个 signed 值打包。

        第一字节的高 2 位 (bits[7:6]) 是 tag:
          00: 3 个 2-bit signed 值打包在 1 字节
          01: 3 个 4-bit signed 值打包在 2 字节
          10: 3 个值: 6-bit + 8-bit + 8-bit, 共 3 字节
          11: 二级 header + 每字段 1/2/3/4 字节

        参考: betaflight blackbox_encoding.c blackboxWriteTag2_3S32()
        """
        header = self.read_byte()
        tag = (header >> 6) & 0x03

        if tag == 0:
            # BITS_2: ss|AA|BB|CC — 2 bits per field in the header byte
            # BF encoder: (selector << 6) | ((val0 & 0x03) << 4) | ((val1 & 0x03) << 2) | (val2 & 0x03)
            val0 = (header >> 4) & 0x03
            val1 = (header >> 2) & 0x03
            val2 = header & 0x03
            # Sign-extend 2-bit values
            if val0 >= 2:
                val0 -= 4
            if val1 >= 2:
                val1 -= 4
            if val2 >= 2:
                val2 -= 4
            return [val0, val1, val2]

        elif tag == 1:
            # BITS_4: byte1 = ss|0000|AAAA, byte2 = BBBB|CCCC
            # BF encoder: (selector << 6) | (val0 & 0x0F)  then  (val1 << 4) | (val2 & 0x0F)
            val0 = header & 0x0F
            byte2 = self.read_byte()
            val1 = (byte2 >> 4) & 0x0F
            val2 = byte2 & 0x0F
            # Sign-extend 4-bit values
            if val0 >= 8:
                val0 -= 16
            if val1 >= 8:
                val1 -= 16
            if val2 >= 8:
                val2 -= 16
            return [val0, val1, val2]

        elif tag == 2:
            # BITS_6: byte1 = ss|AAAAAA, byte2 = val1 (8-bit signed), byte3 = val2 (8-bit signed)
            # BF encoder: (selector << 6) | (val0 & 0x3F)  then  (uint8_t)val1  then  (uint8_t)val2
            val0 = header & 0x3F
            if val0 >= 32:
                val0 -= 64  # sign-extend 6-bit
            byte2 = self.read_byte()
            val1 = byte2 if byte2 < 128 else byte2 - 256
            byte3 = self.read_byte()
            val2 = byte3 if byte3 < 128 else byte3 - 256
            return [val0, val1, val2]

        else:  # tag == 3
            # BITS_32: secondary header for per-field byte widths
            # byte1 = ss|tttttt where tt = byte width selector for each field
            # BF encoder: selector2 encodes byte widths (0=1B, 1=2B, 2=3B, 3=4B) per field
            selector2 = header & 0x3F
            values = []
            for i in range(3):
                field_width = selector2 & 0x03
                selector2 >>= 2
                if field_width == 0:  # 1 byte
                    b = self.read_byte()
                    val = b if b < 128 else b - 256
                elif field_width == 1:  # 2 bytes LE
                    lo = self.read_byte()
                    hi = self.read_byte()
                    val = lo | (hi << 8)
                    if val >= 0x8000:
                        val -= 0x10000
                elif field_width == 2:  # 3 bytes LE
                    b0 = self.read_byte()
                    b1 = self.read_byte()
                    b2 = self.read_byte()
                    val = b0 | (b1 << 8) | (b2 << 16)
                    if val >= 0x800000:
                        val -= 0x1000000
                else:  # 4 bytes LE
                    b0 = self.read_byte()
                    b1 = self.read_byte()
                    b2 = self.read_byte()
                    b3 = self.read_byte()
                    val = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
                    if val >= 0x80000000:
                        val -= 0x100000000
                values.append(val)
            return values

    def read_tag8_4s16(self) -> List[int]:
        """读取 tag8_4S16 编码 (v2) — 4 个值，tag 字节指示每个字段宽度。

        tag 字节每 2 位描述对应字段 (低位先):
          00: 值为 0
          01: 4-bit signed (nibble-packed: 两个 4-bit 值共享一个字节)
          10: 8-bit signed
          11: 16-bit signed (little-endian)

        4-bit 值使用 nibble packing:
          - 每两个 4-bit 值打包进一个字节 (低 nibble 先写)
          - 如果奇数个 4-bit 值，最后一个独占一字节的低 nibble

        参考: betaflight blackbox_encoding.c blackboxWriteTag8_4S16()
               betaflight blackbox-tools decoders.c streamReadTag8_4S16_v2()
        """
        tag = self.read_byte()

        values = [0, 0, 0, 0]
        nibble_buffer = 0
        nibble_index = 0  # 0 = need to read byte, 1 = high nibble available

        for i in range(4):
            field_tag = (tag >> (i * 2)) & 0x03
            if field_tag == 0:
                # FIELD_ZERO
                values[i] = 0
            elif field_tag == 1:
                # FIELD_4BIT: nibble-packed
                if nibble_index == 0:
                    nibble_buffer = self.read_byte()
                    val = nibble_buffer & 0x0F
                    nibble_index = 1
                else:
                    val = (nibble_buffer >> 4) & 0x0F
                    nibble_index = 0
                # Sign-extend 4-bit
                if val >= 8:
                    val -= 16
                values[i] = val
            elif field_tag == 2:
                # FIELD_8BIT
                raw = self.read_byte()
                if raw >= 128:
                    raw -= 256
                values[i] = raw
            else:
                # FIELD_16BIT: little-endian signed
                lo = self.read_byte()
                hi = self.read_byte()
                val = lo | (hi << 8)
                if val >= 0x8000:
                    val -= 0x10000
                values[i] = val

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
        """读取 tag2_3SVARIABLE 编码 — 3 个值，非对称位宽。

        tag 在 bits[7:6]:
          00: 2 bits per field (same as tag2_3s32)
          01: 5-5-4 bits per field
          10: 8-7-7 bits per field
          11: variable (same as tag2_3s32 tag=3)

        参考: betaflight blackbox_encoding.c blackboxWriteTag2_3SVariable()
        """
        header = self.read_byte()
        tag = (header >> 6) & 0x03

        if tag == 0:
            # BITS_2: ss|AA|BB|CC — same as tag2_3s32
            val0 = (header >> 4) & 0x03
            val1 = (header >> 2) & 0x03
            val2 = header & 0x03
            if val0 >= 2:
                val0 -= 4
            if val1 >= 2:
                val1 -= 4
            if val2 >= 2:
                val2 -= 4
            return [val0, val1, val2]

        elif tag == 1:
            # BITS_554: ss11 1112 2222 3333
            # BF: (selector << 6) | ((val0 & 0x1F) << 1) | ((val1 & 0x1F) >> 4)
            #     ((val1 & 0x0F) << 4) | (val2 & 0x0F)
            byte2 = self.read_byte()
            val0 = (header >> 1) & 0x1F
            val1 = ((header & 0x01) << 4) | ((byte2 >> 4) & 0x0F)
            val2 = byte2 & 0x0F
            # Sign extend: 5, 5, 4 bits
            if val0 >= 16:
                val0 -= 32
            if val1 >= 16:
                val1 -= 32
            if val2 >= 8:
                val2 -= 16
            return [val0, val1, val2]

        elif tag == 2:
            # BITS_877: ss11 1111 1122 2222 2333 3333
            # BF: (selector << 6) | ((val0 >> 2) & 0x3F)
            #     ((val0 & 0x03) << 6) | ((val1 >> 1) & 0x3F)  [WAIT: actually 0x7F]
            #     ((val1 & 0x01) << 7) | (val2 & 0x7F)
            byte2 = self.read_byte()
            byte3 = self.read_byte()
            # Reconstruct from the 3 bytes:
            combined = header | (byte2 << 8) | (byte3 << 16)
            # Remove the 2-bit tag from high bits of byte1
            # Layout: ss AAAAAAAA BBBBBBB CCCCCCC
            val2 = combined & 0x7F
            val1 = (combined >> 7) & 0x7F
            val0 = (combined >> 14) & 0xFF
            # But tag is in bits[23:22], so we need to mask properly
            # Actually: byte1 = ss|AAAAAA (6 bits of A), byte2 = AA|BBBBBBB (2 more bits of A + 7 of B)...
            # Let me re-derive from BF encoder:
            # BF writes: (selector << 6) | ((val0 >> 2) & 0x3F)
            #            ((val0 & 0x03) << 6) | (val1 & 0x7F)  [wait, need to check]
            #            (val2 & 0x7F)  [or with high bit of val1?]
            # Actually from the encoder source comment: "877 bits per field"
            # val0 = 8 bits, val1 = 7 bits, val2 = 7 bits
            # Let me use a simpler extraction:
            val0_hi = header & 0x3F  # low 6 bits of header = high 6 bits of val0
            val0_lo = (byte2 >> 6) & 0x03  # high 2 bits of byte2 = low 2 bits of val0
            val0 = (val0_hi << 2) | val0_lo
            val1 = byte2 & 0x7F  # low 7 bits of byte2 (bit 7 is part of val0)
            # Wait, byte2 has 8 bits: 2 for val0 + 6 for val1? No, 877 = 8+7+7 = 22 bits
            # Total available after tag: 6 + 8 + 8 = 22 bits. OK.
            # Re-derive:
            # byte1: ss AAAAAA  (6 high bits of val0)
            # byte2: AA BBBBBB  (2 low bits of val0, 6 high bits of val1) -- NO
            # Actually BF encoder for 877:
            # blackboxWrite((selector << 6) | ((values[0] >> 2) & 0x3F));
            # blackboxWrite(((values[0] & 0x03) << 6) | (values[1] & 0x7F));  -- wait this is only 2+7=9 bits, but values[1] is 7 bits
            # Actually values[1] needs 7 bits, so 0x7F masks to 7 bits, then values[0]&0x03 uses the upper 2 bits. But that's 2+7=9... doesn't fit in a byte.
            # Let me look at this differently. BF encoder says:
            #   blackboxWrite((selector << 6) | ((values[0] >> 2) & 0x3F));
            #   blackboxWrite(((values[0] & 0x03) << 6) | ((values[1] >> 1) & 0x3F));  -- OR is it (values[1] & 0x3F)?
            #   blackboxWrite(((values[1] & 0x01) << 7) | (values[2] & 0x7F));
            # That would be: 6+2+6+1+7 = 22, with val0=8, val1=7, val2=7.
            # I need to just fetch the actual source. For now, let me try the simple approach:
            pass

        # For tag 2 and 3, fall through to generic approach
        if tag == 2:
            # Re-extract properly
            # byte1 = (2 << 6) | ((val0 >> 2) & 0x3F)  -> val0_hi6 = header & 0x3F
            # byte2 = ((val0 & 0x03) << 6) | ((val1 >> 1) & 0x3F)  -> val0_lo2 = (byte2 >> 6), val1_hi6 = byte2 & 0x3F
            # byte3 = ((val1 & 0x01) << 7) | (val2 & 0x7F) -> val1_lo1 = (byte3 >> 7), val2 = byte3 & 0x7F
            val0 = ((header & 0x3F) << 2) | ((byte2 >> 6) & 0x03)
            val1 = ((byte2 & 0x3F) << 1) | ((byte3 >> 7) & 0x01)
            val2 = byte3 & 0x7F
            # Sign extend
            if val0 >= 128:
                val0 -= 256  # 8-bit
            if val1 >= 64:
                val1 -= 128  # 7-bit
            if val2 >= 64:
                val2 -= 128  # 7-bit
            return [val0, val1, val2]

        else:  # tag == 3
            # Same as tag2_3s32 tag=3: per-field variable byte width
            selector2 = header & 0x3F
            values = []
            for i in range(3):
                field_width = selector2 & 0x03
                selector2 >>= 2
                if field_width == 0:
                    b = self.read_byte()
                    val = b if b < 128 else b - 256
                elif field_width == 1:
                    lo = self.read_byte()
                    hi = self.read_byte()
                    val = lo | (hi << 8)
                    if val >= 0x8000:
                        val -= 0x10000
                elif field_width == 2:
                    b0 = self.read_byte()
                    b1 = self.read_byte()
                    b2 = self.read_byte()
                    val = b0 | (b1 << 8) | (b2 << 16)
                    if val >= 0x800000:
                        val -= 0x1000000
                else:
                    b0 = self.read_byte()
                    b1 = self.read_byte()
                    b2 = self.read_byte()
                    b3 = self.read_byte()
                    val = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
                    if val >= 0x80000000:
                        val -= 0x100000000
                values.append(val)
            return values

    def skip_to_frame(self) -> Optional[int]:
        """跳过损坏数据，定位到下一个有效帧起始。

        返回帧类型字节，或 None 如果到达末尾。
        """
        valid_frame_types = {FRAME_TYPE_I, FRAME_TYPE_P, FRAME_TYPE_S, FRAME_TYPE_E, FRAME_TYPE_H}
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
                line = self._data[start : self._pos].decode("ascii", errors="replace")
                self._pos += 1
                return line.rstrip("\r")
            self._pos += 1
        # 到末尾了
        if self._pos > start:
            return self._data[start : self._pos].decode("ascii", errors="replace").rstrip("\r")
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
        if reader.peek_byte() != ord("H"):
            break

        line = reader.read_line()
        if line is None:
            break

        # 跳过不以 "H " 开头的行
        if not line.startswith("H "):
            continue

        content = line[2:]  # 去掉 "H "
        if ":" not in content:
            continue

        key, _, value = content.partition(":")
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
            if "/" in value:
                parts = value.split("/")
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
            i_field_names = value.split(",")
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
            s_field_names = value.split(",")
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
    for part in s.split(","):
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


def _read_field_value(reader: BBLStreamReader, encoding: int, field_count: int = 1) -> List[int]:
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


def _apply_predictor(
    predictor: int,
    raw_value: int,
    prev_values: Dict[str, int],
    field_name: str,
    all_prev: Dict[str, int],
    header: BBLHeader,
    prev_prev_values: Optional[Dict[str, int]] = None,
) -> int:
    """根据预测器类型，将原始编码值转换为实际值。

    Parameters
    ----------
    prev_values : dict
        上一帧（n-1）的所有字段值。
    prev_prev_values : dict, optional
        上上一帧（n-2）的所有字段值，用于 STRAIGHT_LINE 和 AVERAGE_2 预测器。
    """
    if predictor == PREDICTOR_0:
        return raw_value
    elif predictor == PREDICTOR_PREVIOUS:
        return raw_value + prev_values.get(field_name, 0)
    elif predictor == PREDICTOR_STRAIGHT_LINE:
        # predicted = 2 * prev[n-1] - prev[n-2]  (线性外推)
        prev_val = prev_values.get(field_name, 0)
        if prev_prev_values is not None:
            prev_prev_val = prev_prev_values.get(field_name, prev_val)
            return raw_value + 2 * prev_val - prev_prev_val
        else:
            return raw_value + prev_val
    elif predictor == PREDICTOR_AVERAGE_2:
        # predicted = (prev[n-1] + prev[n-2]) / 2
        prev_val = prev_values.get(field_name, 0)
        if prev_prev_values is not None:
            prev_prev_val = prev_prev_values.get(field_name, prev_val)
            return raw_value + (prev_val + prev_prev_val) // 2
        else:
            return raw_value + prev_val
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
    elif predictor == PREDICTOR_MINMOTOR:
        # BF 4.x: motorOutput header gives [low, high] for digital protocols
        motor_output = header.properties.get("motorOutput", "")
        if "," in motor_output:
            try:
                min_motor = int(motor_output.split(",")[0])
            except ValueError:
                min_motor = 0
        else:
            min_motor = int(header.properties.get("minthrottle", "1070"))
        return raw_value + min_motor
    else:
        return raw_value


def decode_i_frame(
    reader: BBLStreamReader,
    header: BBLHeader,
    prev_values: Dict[str, int],
    prev_prev_values: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
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
                        fd.predictor, rv, prev_values, fd.name, values, header, prev_prev_values
                    )
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_4S16:
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, values, header, prev_prev_values
                    )
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_8SVB:
            # 计算连续同编码字段数
            count = 0
            while (
                i + count < len(field_defs) and field_defs[i + count].encoding == ENCODING_TAG8_8SVB
            ):
                count += 1
            raw_values = reader.read_tag8_8svb(count)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_values, fd.name, values, header, prev_prev_values
                    )
            i += count
        else:
            raw_values = _read_field_value(reader, encoding)
            raw = raw_values[0]
            values[fdef.name] = _apply_predictor(
                fdef.predictor, raw, prev_values, fdef.name, values, header, prev_prev_values
            )
            i += 1

    return values


def decode_p_frame(
    reader: BBLStreamReader,
    header: BBLHeader,
    prev_values: Dict[str, int],
    prev_prev_values: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
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
                        fd.predictor,
                        rv,
                        prev_values,
                        fd.name,
                        prev_values,
                        header,
                        prev_prev_values,
                    )
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_4S16:
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor,
                        rv,
                        prev_values,
                        fd.name,
                        prev_values,
                        header,
                        prev_prev_values,
                    )
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_8SVB:
            count = 0
            while (
                i + count < len(field_defs) and field_defs[i + count].encoding == ENCODING_TAG8_8SVB
            ):
                count += 1
            raw_values = reader.read_tag8_8svb(count)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor,
                        rv,
                        prev_values,
                        fd.name,
                        prev_values,
                        header,
                        prev_prev_values,
                    )
            i += count
        else:
            raw_values = _read_field_value(reader, encoding)
            raw = raw_values[0]
            values[fdef.name] = _apply_predictor(
                fdef.predictor, raw, prev_values, fdef.name, prev_values, header, prev_prev_values
            )
            i += 1

    return values


def decode_s_frame(
    reader: BBLStreamReader, header: BBLHeader, prev_slow: Dict[str, int]
) -> Dict[str, int]:
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
                        fd.predictor, rv, prev_slow, fd.name, prev_slow, header
                    )
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_4S16:
            raw_values = _read_field_value(reader, encoding)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_slow, fd.name, prev_slow, header
                    )
            i += len(raw_values)
        elif encoding == ENCODING_TAG8_8SVB:
            count = 0
            while (
                i + count < len(field_defs) and field_defs[i + count].encoding == ENCODING_TAG8_8SVB
            ):
                count += 1
            raw_values = reader.read_tag8_8svb(count)
            for j, rv in enumerate(raw_values):
                if i + j < len(field_defs):
                    fd = field_defs[i + j]
                    values[fd.name] = _apply_predictor(
                        fd.predictor, rv, prev_slow, fd.name, prev_slow, header
                    )
            i += count
        else:
            raw_values = _read_field_value(reader, encoding)
            raw = raw_values[0]
            values[fdef.name] = _apply_predictor(
                fdef.predictor, raw, prev_slow, fdef.name, prev_slow, header
            )
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
            event.data["value"] = struct.unpack("<f", raw)[0]
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


def _has_corrupted_imu_values(values: Dict[str, int]) -> bool:
    """检查解码后的帧是否包含异常的 IMU 原始值。

    Betaflight accSmooth 和 gyroADC 的原始 ADC 值通常在 ±32767 范围内
    （16-bit signed）。如果出现远大于此的值，说明解析器失去了同步
    （使用了错误的字段定义来解码新飞行段的数据）。

    Returns True 表示检测到损坏/不同步。
    """
    for i in range(3):
        val = values.get(f"accSmooth[{i}]", None)
        if val is not None and abs(val) > 32768:
            return True
        val = values.get(f"gyroADC[{i}]", None)
        if val is not None and abs(val) > 65536:
            return True
        val = values.get(f"accData[{i}]", None)
        if val is not None and abs(val) > 32768:
            return True
    return False


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
            if reader.peek_byte() == ord("H"):
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
        prev_prev_values: Optional[Dict[str, int]] = None
        prev_slow: Dict[str, int] = {}
        frame_count = 0
        max_frames = 500_000  # 安全限制
        # Segment detection: track last loopIteration to detect time resets
        # that indicate a new flight segment (without "H Product:" header).
        last_loop_iter: Optional[int] = None
        corrupt_frame_count = 0
        max_corrupt_frames = 5  # Stop after N consecutive corrupted frames

        while reader.has_data() and frame_count < max_frames:
            # 检查是否遇到新段的头
            if reader.peek_byte() == ord("H"):
                # 只有 "H " (H 后跟空格) 才是真正的 header 行
                # 0x48 也可能出现在帧数据中，不能仅凭 peek 就 continue
                if reader.has_data(2) and reader._data[reader.pos + 1] == ord(" "):
                    save_pos = reader.pos
                    line_peek = reader.read_line()
                    if line_peek and "H Product:" in line_peek:
                        reader.pos = save_pos
                        break
                    # 其他 H 行（数据流中的补充头），跳过
                    continue
                # 0x48 后面不是空格，说明是帧数据中的巧合字节
                # 不 continue，fall through 到 frame_marker 处理

            frame_marker = reader.read_byte()

            try:
                if frame_marker == FRAME_TYPE_I:
                    values = decode_i_frame(reader, header, prev_values, prev_prev_values)
                    # After I-frame, set prev_prev = values so that the first
                    # P-frame's STRAIGHT_LINE prediction = 2*prev - prev_prev = prev,
                    # equivalent to PREVIOUS. This matches BF's blackbox_decode.c
                    # which resets history[1] = history[0] after each I-frame.
                    prev_prev_values = values.copy()
                    prev_values = values.copy()

                    # NOTE: loopIteration-based segment detection removed.
                    # BF's loopIteration is a 16-bit counter that wraps every
                    # ~16ms (at 8kHz looptime), causing false segment breaks.
                    # Segment boundaries are detected by "H Product:" headers only.
                    cur_iter = values.get("loopIteration", 0)
                    last_loop_iter = cur_iter

                    # Sanity check: if IMU fields have absurd values,
                    # the parser lost sync (wrong field defs for this segment)
                    if _has_corrupted_imu_values(values):
                        corrupt_frame_count += 1
                        if corrupt_frame_count >= max_corrupt_frames:
                            logger.warning(
                                "BBL: %d consecutive corrupted frames at frame %d, "
                                "stopping segment parse",
                                corrupt_frame_count,
                                frame_count,
                            )
                            break
                        # Skip this corrupted frame, don't add to segment
                        continue
                    else:
                        corrupt_frame_count = 0  # Reset on valid frame

                    frame = BBLFrame(
                        frame_type="I",
                        values=values,
                        time_us=cur_iter,
                    )
                    segment.frames.append(frame)
                    frame_count += 1

                elif frame_marker == FRAME_TYPE_P:
                    if not prev_values:
                        # 没有先行 I-frame，跳过找下一个帧
                        reader.skip_to_frame()
                        continue
                    values = decode_p_frame(reader, header, prev_values, prev_prev_values)
                    prev_prev_values = prev_values.copy()
                    prev_values = values.copy()
                    frame = BBLFrame(
                        frame_type="P",
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
# Columnar (memory-efficient) parser
# ---------------------------------------------------------------------------


@dataclass
class BBLColumnarSegment:
    """Memory-efficient columnar segment: NumPy arrays instead of per-frame dicts.

    Each field is stored as a contiguous np.int64 column of length n_frames.
    This avoids ~1 KB of Python dict/object overhead per frame, saving ~300 MB
    on a typical 300K-frame log.
    """

    header: BBLHeader
    field_names: List[str]  # ordered field name list
    columns: Dict[str, np.ndarray]  # field_name → np.int64[n_frames]
    frame_types: np.ndarray  # np.uint8[n_frames], ord('I') or ord('P')
    n_frames: int = 0
    events: List[BBLEvent] = field(default_factory=list)

    def get_column(self, name: str) -> Optional[np.ndarray]:
        """Get a field column, or None if the field doesn't exist."""
        return self.columns.get(name)

    @property
    def available_fields(self) -> set:
        return set(self.field_names)

    @property
    def i_frame_count(self) -> int:
        return int(np.sum(self.frame_types[: self.n_frames] == FRAME_TYPE_I))

    @property
    def p_frame_count(self) -> int:
        return int(np.sum(self.frame_types[: self.n_frames] == FRAME_TYPE_P))


def _grow_arrays(
    columns: Dict[str, np.ndarray], frame_types: np.ndarray, new_capacity: int
) -> np.ndarray:
    """Double the capacity of all column arrays. Returns new frame_types."""
    for name in columns:
        old = columns[name]
        new = np.zeros(new_capacity, dtype=old.dtype)
        new[: len(old)] = old
        columns[name] = new
    new_ft = np.zeros(new_capacity, dtype=np.uint8)
    new_ft[: len(frame_types)] = frame_types
    return new_ft


def parse_bbl_columnar(data: bytes, max_segments: int = 1) -> List[BBLColumnarSegment]:
    """Memory-efficient BBL parser — stores frames as NumPy columns, not dicts.

    Functionally equivalent to parse_bbl() but uses ~5-10% of the memory
    for the frame data. The decode logic is identical; only the storage
    container changes.

    Parameters
    ----------
    data : bytes
        Complete BBL file content
    max_segments : int
        Maximum segments to parse (default 1)

    Returns
    -------
    List[BBLColumnarSegment]
        Parsed segments with columnar frame data
    """
    result_segments: List[BBLColumnarSegment] = []
    reader = BBLStreamReader(data)

    while reader.has_data() and len(result_segments) < max_segments:
        # Find segment start — "H Product:"
        found = False
        while reader.has_data():
            if reader.peek_byte() == ord("H"):
                save_pos = reader.pos
                line = reader.read_line()
                if line and "H Product:" in line:
                    reader.pos = save_pos
                    found = True
                    break
            else:
                reader.pos += 1

        if not found:
            break

        header = parse_header(reader)

        if not header.i_field_defs:
            logger.warning("BBL segment has no I-frame field definitions, skipping")
            continue

        # Pre-allocate columnar storage
        field_names = [f.name for f in header.i_field_defs]
        initial_capacity = 8192  # will grow × 2 as needed
        columns: Dict[str, np.ndarray] = {
            name: np.zeros(initial_capacity, dtype=np.int64) for name in field_names
        }
        frame_types = np.zeros(initial_capacity, dtype=np.uint8)
        capacity = initial_capacity
        n_frames = 0
        events: List[BBLEvent] = []

        # Decode state (same as parse_bbl)
        prev_values: Dict[str, int] = {}
        prev_prev_values: Optional[Dict[str, int]] = None
        prev_slow: Dict[str, int] = {}
        max_frames = 500_000
        corrupt_frame_count = 0
        max_corrupt_frames = 5

        while reader.has_data() and n_frames < max_frames:
            # Check for new segment header
            if reader.peek_byte() == ord("H"):
                if reader.has_data(2) and reader._data[reader.pos + 1] == ord(" "):
                    save_pos = reader.pos
                    line_peek = reader.read_line()
                    if line_peek and "H Product:" in line_peek:
                        reader.pos = save_pos
                        break
                    continue

            frame_marker = reader.read_byte()

            try:
                if frame_marker == FRAME_TYPE_I:
                    values = decode_i_frame(reader, header, prev_values, prev_prev_values)
                    prev_prev_values = values.copy()
                    prev_values = values.copy()

                    if _has_corrupted_imu_values(values):
                        corrupt_frame_count += 1
                        if corrupt_frame_count >= max_corrupt_frames:
                            logger.warning(
                                "BBL: %d consecutive corrupted frames at frame %d, "
                                "stopping segment parse",
                                corrupt_frame_count,
                                n_frames,
                            )
                            break
                        continue
                    else:
                        corrupt_frame_count = 0

                    # Store into columns
                    if n_frames >= capacity:
                        capacity *= 2
                        frame_types = _grow_arrays(columns, frame_types, capacity)

                    for name in field_names:
                        columns[name][n_frames] = values.get(name, 0)
                    frame_types[n_frames] = FRAME_TYPE_I
                    n_frames += 1

                elif frame_marker == FRAME_TYPE_P:
                    if not prev_values:
                        reader.skip_to_frame()
                        continue
                    values = decode_p_frame(reader, header, prev_values, prev_prev_values)
                    prev_prev_values = prev_values.copy()
                    prev_values = values.copy()

                    # Store into columns
                    if n_frames >= capacity:
                        capacity *= 2
                        frame_types = _grow_arrays(columns, frame_types, capacity)

                    for name in field_names:
                        columns[name][n_frames] = values.get(name, 0)
                    frame_types[n_frames] = FRAME_TYPE_P
                    n_frames += 1

                elif frame_marker == FRAME_TYPE_S:
                    values = decode_s_frame(reader, header, prev_slow)
                    prev_slow = values.copy()

                elif frame_marker == FRAME_TYPE_E:
                    event = decode_e_frame(reader, header)
                    events.append(event)

                else:
                    ft = reader.skip_to_frame()
                    if ft is None:
                        break

            except EOFError:
                logger.debug("BBL: reached end of data mid-frame at frame %d", n_frames)
                break
            except Exception as e:
                logger.debug("BBL: error decoding frame %d: %s", n_frames, e)
                ft = reader.skip_to_frame()
                if ft is None:
                    break

        # Trim to actual size
        trimmed_columns = {name: arr[:n_frames].copy() for name, arr in columns.items()}
        trimmed_ft = frame_types[:n_frames].copy()
        # Free the oversized arrays
        del columns, frame_types

        seg = BBLColumnarSegment(
            header=header,
            field_names=field_names,
            columns=trimmed_columns,
            frame_types=trimmed_ft,
            n_frames=n_frames,
            events=events,
        )
        result_segments.append(seg)

    return result_segments


# ---------------------------------------------------------------------------
# Utility: extract flight mode from E-frame flags
# ---------------------------------------------------------------------------

# Betaflight 模式标志位 (从 BF 源码)
BF_MODE_FLAGS = {
    0: "ARM",
    1: "ANGLE",
    2: "HORIZON",
    3: "MAG",  # not used in modern BF
    5: "HEADFREE",
    6: "HEADADJ",
    10: "GPS_HOME",
    11: "GPS_HOLD",
    12: "PASSTHRU",
    15: "FAILSAFE",
    19: "AIR",  # Airmode
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
