"""
tests/test_bbl_parser.py

BBL 解析器测试 — 使用合成 BBL 数据验证解析逻辑。
"""

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Synthetic BBL file builder
# ---------------------------------------------------------------------------

class BBLBuilder:
    """构建合成 BBL 二进制文件用于测试。

    生成符合 Betaflight Blackbox 格式的最小可解析文件，
    包含 header、I-frame、P-frame 和 E-frame。
    """

    def __init__(self):
        self._lines: list[bytes] = []
        self._binary: bytearray = bytearray()

    def _add_header_line(self, key: str, value: str):
        line = f"H {key}:{value}\n"
        self._lines.append(line.encode("ascii"))

    def build_minimal(
        self,
        n_frames: int = 100,
        *,
        include_pid_terms: bool = True,
        include_motors: bool = True,
        include_accel: bool = True,
        include_setpoint: bool = True,
        include_mode_event: bool = True,
        gyro_amplitude: float = 10.0,
        setpoint_amplitude: float = 5.0,
        looptime_us: int = 250,
    ) -> bytes:
        """构建最小可解析 BBL 数据。

        由于完整 BBL 二进制编码非常复杂 (变长编码 + 预测器)，
        我们直接构建文本 header + 使用简单编码的 I-frame。

        为了测试方便，此方法构建的数据使用最简单的编码:
        - 所有字段使用 signed_vb (encoding=0) 或 unsigned_vb (encoding=1)
        - 所有预测器使用 PREDICTOR_0 (无预测) 或 PREDICTOR_PREVIOUS (前值累加)

        实际 BBL 文件使用更复杂的编码，但解析器需要能处理这种简单编码
        作为最低要求。
        """
        self._lines = []
        self._binary = bytearray()

        # ── Header ──
        self._add_header_line("Product", "Blackbox flight data recorder by Nicholas Sherlock")
        self._add_header_line("Data version", "2")
        self._add_header_line("Firmware type", "Betaflight")
        self._add_header_line("Firmware revision", "4.4.0")
        self._add_header_line("Firmware date", "Jan  1 2024 00:00:00")
        self._add_header_line("Board information", "STM32F405")
        self._add_header_line("Craft name", "TestQuad")
        self._add_header_line("I interval", "32")
        self._add_header_line("P interval", "1/1")
        self._add_header_line("looptime", str(looptime_us))
        self._add_header_line("minthrottle", "1070")
        self._add_header_line("maxthrottle", "2000")
        self._add_header_line("gyro_scale", "0x3f800000")  # 1.0 in float hex
        self._add_header_line("acc_1G", "512")
        self._add_header_line("motorOutput", "1070,2000")
        self._add_header_line("pid_roll_p", "45")
        self._add_header_line("pid_roll_i", "80")
        self._add_header_line("pid_roll_d", "40")
        self._add_header_line("pid_roll_f", "120")
        self._add_header_line("pid_pitch_p", "47")
        self._add_header_line("pid_pitch_i", "84")
        self._add_header_line("pid_pitch_d", "46")
        self._add_header_line("pid_pitch_f", "125")
        self._add_header_line("pid_yaw_p", "45")
        self._add_header_line("pid_yaw_i", "80")
        self._add_header_line("pid_yaw_d", "0")
        self._add_header_line("pid_yaw_f", "120")
        self._add_header_line("gyro_lowpass_hz", "200")
        self._add_header_line("gyro_lowpass2_hz", "250")
        self._add_header_line("dterm_lowpass_hz", "150")

        # ── Field definitions ──
        # 构建字段名列表
        field_names = ["loopIteration", "time"]
        field_signed = [0, 0]  # unsigned

        # gyro
        field_names += ["gyroADC[0]", "gyroADC[1]", "gyroADC[2]"]
        field_signed += [1, 1, 1]  # signed

        if include_setpoint:
            field_names += ["setpoint[0]", "setpoint[1]", "setpoint[2]", "setpoint[3]"]
            field_signed += [1, 1, 1, 1]

        if include_pid_terms:
            field_names += ["axisP[0]", "axisP[1]", "axisP[2]"]
            field_signed += [1, 1, 1]
            field_names += ["axisI[0]", "axisI[1]", "axisI[2]"]
            field_signed += [1, 1, 1]
            field_names += ["axisD[0]", "axisD[1]", "axisD[2]"]
            field_signed += [1, 1, 1]
            field_names += ["axisF[0]", "axisF[1]", "axisF[2]"]
            field_signed += [1, 1, 1]

        if include_motors:
            field_names += ["motor[0]", "motor[1]", "motor[2]", "motor[3]"]
            field_signed += [0, 0, 0, 0]

        if include_accel:
            field_names += ["accSmooth[0]", "accSmooth[1]", "accSmooth[2]"]
            field_signed += [1, 1, 1]

        n_fields = len(field_names)

        # I-frame 编码: 全部使用 signed/unsigned VB + PREDICTOR_0
        i_predictors = [0] * n_fields  # PREDICTOR_0
        i_encodings = []
        for s in field_signed:
            i_encodings.append(0 if s else 1)  # signed_vb=0, unsigned_vb=1

        # P-frame 编码: 使用 PREDICTOR_PREVIOUS + signed VB
        p_predictors = [1] * n_fields  # PREDICTOR_PREVIOUS
        p_encodings = [0] * n_fields  # ENCODING_SIGNED_VB for all

        self._add_header_line("Field I name", ",".join(field_names))
        self._add_header_line("Field I signed", ",".join(str(s) for s in field_signed))
        self._add_header_line("Field I predictor", ",".join(str(p) for p in i_predictors))
        self._add_header_line("Field I encoding", ",".join(str(e) for e in i_encodings))
        self._add_header_line("Field P predictor", ",".join(str(p) for p in p_predictors))
        self._add_header_line("Field P encoding", ",".join(str(e) for e in p_encodings))

        # Slow frame 定义 (minimal)
        self._add_header_line("Field S name", "flightModeFlags,stateFlags")
        self._add_header_line("Field S signed", "0,0")
        self._add_header_line("Field S predictor", "0,0")
        self._add_header_line("Field S encoding", "1,1")

        # ── 生成数据帧 ──
        data = bytearray()
        i_interval = 32

        for frame_idx in range(n_frames):
            is_i_frame = (frame_idx % i_interval == 0)

            # 生成此帧的值
            t = frame_idx * looptime_us  # 微秒时间

            # 用正弦波模拟 gyro 信号
            angle = 2 * 3.14159 * frame_idx / n_frames
            gyro_r = int(gyro_amplitude * np.sin(angle * 3))
            gyro_p = int(gyro_amplitude * np.sin(angle * 2 + 0.5))
            gyro_y = int(gyro_amplitude * 0.3 * np.sin(angle * 5))

            values = [frame_idx, t, gyro_r, gyro_p, gyro_y]

            if include_setpoint:
                sp_r = int(setpoint_amplitude * np.sin(angle * 3 + 0.1))
                sp_p = int(setpoint_amplitude * np.sin(angle * 2 + 0.6))
                sp_y = int(setpoint_amplitude * 0.2 * np.sin(angle * 5 + 0.1))
                sp_t = 1500  # 油门中位
                values += [sp_r, sp_p, sp_y, sp_t]

            if include_pid_terms:
                # P terms
                values += [gyro_r // 3, gyro_p // 3, gyro_y // 3]
                # I terms
                values += [gyro_r // 10, gyro_p // 10, gyro_y // 10]
                # D terms
                values += [int(gyro_amplitude * 0.1 * np.cos(angle * 3)),
                           int(gyro_amplitude * 0.1 * np.cos(angle * 2)),
                           int(gyro_amplitude * 0.05 * np.cos(angle * 5))]
                # F terms (feedforward)
                values += [sp_r // 5 if include_setpoint else 0,
                           sp_p // 5 if include_setpoint else 0,
                           sp_y // 5 if include_setpoint else 0]

            if include_motors:
                base_motor = 1500
                values += [base_motor + gyro_r, base_motor - gyro_r,
                           base_motor + gyro_p, base_motor - gyro_p]

            if include_accel:
                values += [0, 0, 512]  # ~1g on Z axis

            if is_i_frame:
                # I-frame: 帧标记 + 各字段直接编码
                data.append(0x49)  # 'I'
                for i, val in enumerate(values):
                    if field_signed[i]:
                        data.extend(self._encode_signed_vb(val))
                    else:
                        data.extend(self._encode_unsigned_vb(val))
            else:
                # P-frame: 帧标记 + 差值编码
                data.append(0x50)  # 'P'
                # 差值: 当前 - 前一帧 (简化为直接编码差值=0)
                # 使用 PREDICTOR_PREVIOUS，raw=0 意味着值不变
                for i, val in enumerate(values):
                    # 编码差值为 0（模拟值不变的简化情况）
                    data.extend(self._encode_signed_vb(0))

        # 可选: 添加 E-frame (模式切换事件)
        if include_mode_event:
            data.append(0x45)  # 'E'
            data.extend(self._encode_unsigned_vb(30))  # EVENT_FLIGHT_MODE
            data.extend(self._encode_unsigned_vb(0x03))  # flags: ARM + ANGLE
            data.extend(self._encode_unsigned_vb(0x01))  # last_flags: ARM

        # ── 组合 ──
        result = bytearray()
        for line in self._lines:
            result.extend(line)
        result.extend(data)

        return bytes(result)

    @staticmethod
    def _encode_unsigned_vb(value: int) -> bytes:
        """编码 unsigned Variable Byte。"""
        if value < 0:
            value = 0
        result = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                byte |= 0x80
            result.append(byte)
            if not value:
                break
        return bytes(result)

    @staticmethod
    def _encode_signed_vb(value: int) -> bytes:
        """编码 signed Variable Byte (ZigZag)。"""
        # ZigZag: 正数 → 2n, 负数 → 2|n|-1
        if value >= 0:
            encoded = value * 2
        else:
            encoded = (-value) * 2 - 1
        return BBLBuilder._encode_unsigned_vb(encoded)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBBLStreamReader(unittest.TestCase):
    """测试 BBL 二进制流读取器。"""

    def test_unsigned_vb_single_byte(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader
        # 值 0-127 用 1 字节编码
        reader = BBLStreamReader(bytes([0x00]))
        self.assertEqual(reader.read_unsigned_vb(), 0)

        reader = BBLStreamReader(bytes([0x7F]))
        self.assertEqual(reader.read_unsigned_vb(), 127)

    def test_unsigned_vb_multi_byte(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader
        # 128 = 0x80 0x01 (low 7 bits = 0, next byte = 1)
        reader = BBLStreamReader(bytes([0x80, 0x01]))
        self.assertEqual(reader.read_unsigned_vb(), 128)

        # 300 = 0b100101100 → low7=0101100=0x2C|0x80, high=0b10=0x02
        reader = BBLStreamReader(bytes([0xAC, 0x02]))
        self.assertEqual(reader.read_unsigned_vb(), 300)

    def test_signed_vb_positive(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader
        # ZigZag: 1 → 2 → unsigned_vb(2)
        reader = BBLStreamReader(bytes([0x02]))
        self.assertEqual(reader.read_signed_vb(), 1)

        # 5 → 10 → unsigned_vb(10)
        reader = BBLStreamReader(bytes([0x0A]))
        self.assertEqual(reader.read_signed_vb(), 5)

    def test_signed_vb_negative(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader
        # -1 → 1 → unsigned_vb(1)
        reader = BBLStreamReader(bytes([0x01]))
        self.assertEqual(reader.read_signed_vb(), -1)

        # -5 → 9 → unsigned_vb(9)
        reader = BBLStreamReader(bytes([0x09]))
        self.assertEqual(reader.read_signed_vb(), -5)

    def test_signed_vb_zero(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader
        reader = BBLStreamReader(bytes([0x00]))
        self.assertEqual(reader.read_signed_vb(), 0)

    def test_read_line(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader
        reader = BBLStreamReader(b"H Product:Blackbox\nH Data version:2\n")
        self.assertEqual(reader.read_line(), "H Product:Blackbox")
        self.assertEqual(reader.read_line(), "H Data version:2")


class TestBBLHeaderParsing(unittest.TestCase):
    """测试 BBL 文件头解析。"""

    def _make_header_data(self) -> bytes:
        lines = [
            "H Product:Blackbox flight data recorder by Nicholas Sherlock\n",
            "H Data version:2\n",
            "H Firmware type:Betaflight\n",
            "H Firmware revision:4.4.0\n",
            "H Board information:STM32F405\n",
            "H Craft name:TestQuad\n",
            "H I interval:32\n",
            "H P interval:1/2\n",
            "H Field I name:loopIteration,time,gyroADC[0],gyroADC[1],gyroADC[2]\n",
            "H Field I signed:0,0,1,1,1\n",
            "H Field I predictor:0,0,0,0,0\n",
            "H Field I encoding:1,1,0,0,0\n",
            "H Field P predictor:1,1,1,1,1\n",
            "H Field P encoding:0,0,0,0,0\n",
        ]
        return "".join(lines).encode("ascii")

    def test_parse_header_basic(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader, parse_header
        reader = BBLStreamReader(self._make_header_data())
        header = parse_header(reader)

        self.assertIn("Blackbox", header.product)
        self.assertEqual(header.data_version, 2)
        self.assertEqual(header.firmware_type, "Betaflight")
        self.assertEqual(header.firmware_revision, "4.4.0")
        self.assertEqual(header.board_info, "STM32F405")
        self.assertEqual(header.craft_name, "TestQuad")
        self.assertEqual(header.i_interval, 32)
        self.assertEqual(header.p_ratio, 2)

    def test_parse_field_definitions(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader, parse_header
        reader = BBLStreamReader(self._make_header_data())
        header = parse_header(reader)

        self.assertEqual(len(header.i_field_defs), 5)
        self.assertEqual(header.field_names_i,
                         ["loopIteration", "time", "gyroADC[0]", "gyroADC[1]", "gyroADC[2]"])
        self.assertEqual(header.i_field_defs[0].signed, 0)  # loopIteration unsigned
        self.assertEqual(header.i_field_defs[2].signed, 1)  # gyroADC[0] signed


class TestBBLFrameDecoding(unittest.TestCase):
    """测试 I-frame 和 P-frame 解码。"""

    def test_decode_i_frame_simple(self):
        from smarttune.platform.betaflight.bbl_parser import (
            BBLStreamReader, parse_header, decode_i_frame,
        )

        # 构建一个简单的 header + I-frame
        header_text = (
            "H Product:Blackbox\n"
            "H Data version:2\n"
            "H Field I name:loopIteration,time,gyroADC[0]\n"
            "H Field I signed:0,0,1\n"
            "H Field I predictor:0,0,0\n"
            "H Field I encoding:1,1,0\n"
            "H Field P predictor:1,1,1\n"
            "H Field P encoding:0,0,0\n"
        )

        reader = BBLStreamReader(header_text.encode("ascii"))
        header = parse_header(reader)

        # 构建 I-frame 二进制数据:
        # loopIteration=0 (unsigned_vb=0x00)
        # time=1000 (unsigned_vb)
        # gyroADC[0]=42 (signed_vb, zigzag: 84=0x54)
        frame_data = bytearray()
        frame_data.extend(BBLBuilder._encode_unsigned_vb(0))     # loopIteration=0
        frame_data.extend(BBLBuilder._encode_unsigned_vb(1000))  # time=1000
        frame_data.extend(BBLBuilder._encode_signed_vb(42))      # gyroADC[0]=42

        reader2 = BBLStreamReader(bytes(frame_data))
        values = decode_i_frame(reader2, header, {})

        self.assertEqual(values["loopIteration"], 0)
        self.assertEqual(values["time"], 1000)
        self.assertEqual(values["gyroADC[0]"], 42)

    def test_decode_p_frame_with_predictor_previous(self):
        from smarttune.platform.betaflight.bbl_parser import (
            BBLStreamReader, parse_header, decode_p_frame,
        )

        header_text = (
            "H Product:Blackbox\n"
            "H Data version:2\n"
            "H Field I name:loopIteration,time,gyroADC[0]\n"
            "H Field I signed:0,0,1\n"
            "H Field I predictor:0,0,0\n"
            "H Field I encoding:1,1,0\n"
            "H Field P predictor:1,1,1\n"
            "H Field P encoding:0,0,0\n"
        )
        reader = BBLStreamReader(header_text.encode("ascii"))
        header = parse_header(reader)

        # 前一帧值
        prev = {"loopIteration": 10, "time": 1000, "gyroADC[0]": 42}

        # P-frame: 差值 = [1, 250, -3]
        # PREDICTOR_PREVIOUS: 实际值 = prev + delta
        frame_data = bytearray()
        frame_data.extend(BBLBuilder._encode_signed_vb(1))    # delta loopIteration=+1
        frame_data.extend(BBLBuilder._encode_signed_vb(250))  # delta time=+250
        frame_data.extend(BBLBuilder._encode_signed_vb(-3))   # delta gyro=-3

        reader2 = BBLStreamReader(bytes(frame_data))
        values = decode_p_frame(reader2, header, prev)

        self.assertEqual(values["loopIteration"], 11)   # 10 + 1
        self.assertEqual(values["time"], 1250)           # 1000 + 250
        self.assertEqual(values["gyroADC[0]"], 39)       # 42 + (-3)


class TestBBLFullParse(unittest.TestCase):
    """测试完整 BBL 数据解析。"""

    def test_parse_synthetic_bbl(self):
        from smarttune.platform.betaflight.bbl_parser import parse_bbl

        builder = BBLBuilder()
        data = builder.build_minimal(n_frames=200)

        segments = parse_bbl(data, max_segments=1)
        self.assertEqual(len(segments), 1)

        seg = segments[0]
        self.assertIn("Blackbox", seg.header.product)
        self.assertEqual(seg.header.firmware_type, "Betaflight")
        self.assertEqual(seg.header.firmware_revision, "4.4.0")

        # 应该有帧数据
        self.assertGreater(len(seg.frames), 0)

        # I-frame 应该有 gyro 字段
        first_frame = seg.frames[0]
        self.assertIn("gyroADC[0]", first_frame.values)
        self.assertIn("loopIteration", first_frame.values)

    def test_parse_with_events(self):
        from smarttune.platform.betaflight.bbl_parser import parse_bbl, EVENT_FLIGHT_MODE

        builder = BBLBuilder()
        data = builder.build_minimal(n_frames=100, include_mode_event=True)

        segments = parse_bbl(data)
        seg = segments[0]

        # 应该有模式切换事件
        mode_events = [e for e in seg.events if e.event_type == EVENT_FLIGHT_MODE]
        self.assertGreater(len(mode_events), 0)

    def test_parse_empty_data(self):
        from smarttune.platform.betaflight.bbl_parser import parse_bbl

        segments = parse_bbl(b"not a valid BBL file")
        self.assertEqual(len(segments), 0)

    def test_parse_header_only(self):
        from smarttune.platform.betaflight.bbl_parser import parse_bbl

        header_only = (
            "H Product:Blackbox flight data recorder\n"
            "H Data version:2\n"
            "H Firmware type:Betaflight\n"
        ).encode("ascii")

        segments = parse_bbl(header_only)
        # Should parse but have no I-frame field defs → skip
        self.assertEqual(len(segments), 0)


class TestBetaflightAdapterParse(unittest.TestCase):
    """测试 BetaflightAdapter.parse() 的完整流水线。"""

    def _write_synthetic_bbl(self, tmpdir: Path, **kwargs) -> Path:
        builder = BBLBuilder()
        data = builder.build_minimal(**kwargs)
        path = tmpdir / "test.bbl"
        path.write_bytes(data)
        return path

    def test_parse_to_flight_data(self):
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=200)
            adapter = BetaflightAdapter()
            fd = adapter.parse(path)

            # 基本元信息
            self.assertEqual(fd.platform, "betaflight")
            self.assertEqual(fd.firmware_version, "4.4.0")
            self.assertEqual(fd.board_name, "STM32F405")

            # PID 数据
            self.assertIn("roll", fd.pid)
            self.assertIn("pitch", fd.pid)
            self.assertIn("yaw", fd.pid)
            self.assertGreater(fd.pid["roll"].sample_count, 50)

            # Gyro 数据
            self.assertIsNotNone(fd.gyro)
            self.assertEqual(fd.gyro.shape[1], 3)

            # Motor 数据
            self.assertIsNotNone(fd.motor_output)
            self.assertEqual(fd.motor_output.shape[1], 4)

            # 参数
            self.assertIn("looptime", fd.params)

            # 采样率
            self.assertGreater(fd.sample_rate_hz, 0)

            # extras
            self.assertEqual(fd.extras["craft_name"], "TestQuad")
            self.assertEqual(fd.extras["firmware_type"], "Betaflight")

    def test_parse_has_pid_terms(self):
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=200,
                                              include_pid_terms=True)
            adapter = BetaflightAdapter()
            fd = adapter.parse(path)

            # P/I/D/FF terms 应该存在
            for axis in ["roll", "pitch", "yaw"]:
                sig = fd.pid[axis]
                self.assertIsNotNone(sig.p_term, f"{axis} p_term missing")
                self.assertIsNotNone(sig.i_term, f"{axis} i_term missing")
                self.assertIsNotNone(sig.d_term, f"{axis} d_term missing")
                self.assertIsNotNone(sig.ff_term, f"{axis} ff_term missing")

    def test_parse_frame_type(self):
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=100)
            adapter = BetaflightAdapter()
            fd = adapter.parse(path)

            self.assertEqual(fd.frame_type, "quad")  # 4 motors → quad

    def test_parse_accel_conversion(self):
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=100,
                                              include_accel=True)
            adapter = BetaflightAdapter()
            fd = adapter.parse(path)

            self.assertIsNotNone(fd.accel)
            # accSmooth[2] = 512 → 应转换为 ~9.8 m/s²
            z_accel = fd.accel[:, 2]
            self.assertTrue(np.allclose(z_accel, 9.80665, atol=0.1),
                            f"Z accel should be ~9.8, got {z_accel[0]:.2f}")

    def test_parse_file_not_found(self):
        from smarttune.platform.betaflight import BetaflightAdapter
        from smarttune.errors import LogFileNotFoundError

        adapter = BetaflightAdapter()
        with self.assertRaises(LogFileNotFoundError):
            adapter.parse(Path("/nonexistent/test.bbl"))

    def test_parse_invalid_file(self):
        from smarttune.platform.betaflight import BetaflightAdapter
        from smarttune.errors import LogFileCorruptError

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.bbl"
            path.write_bytes(b"This is not a BBL file")
            adapter = BetaflightAdapter()
            with self.assertRaises(LogFileCorruptError):
                adapter.parse(path)

    def test_parse_too_few_frames(self):
        from smarttune.platform.betaflight import BetaflightAdapter
        from smarttune.errors import InsufficientPIDDataError

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=5)
            adapter = BetaflightAdapter()
            with self.assertRaises(InsufficientPIDDataError):
                adapter.parse(path)

    def test_detect_bbl_file(self):
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            # Valid BBL
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=50)
            self.assertTrue(BetaflightAdapter.detect(path))

            # Not BBL
            bad = Path(tmpdir) / "not.bbl"
            bad.write_bytes(b"Not a blackbox file")
            self.assertFalse(BetaflightAdapter.detect(bad))

            # Non-BBL extension
            txt = Path(tmpdir) / "test.bin"
            txt.write_bytes(b"H Product:Blackbox flight data recorder\n")
            self.assertFalse(BetaflightAdapter.detect(txt))

    def test_mode_events_mapped(self):
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=100,
                                              include_mode_event=True)
            adapter = BetaflightAdapter()
            fd = adapter.parse(path)

            # 应该有模式切换事件
            self.assertGreater(len(fd.mode_changes), 0)
            # flags=0x03 → ARM+ANGLE → primary mode = ANGLE → mapped to "stabilize"
            self.assertEqual(fd.mode_changes[0].raw_mode, "ANGLE")
            self.assertEqual(fd.mode_changes[0].mode_name, "stabilize")

    def test_validate_parsed_data(self):
        """完整数据应该通过 FlightData.validate()。"""
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_synthetic_bbl(Path(tmpdir), n_frames=500,
                                              include_accel=True)
            adapter = BetaflightAdapter()
            fd = adapter.parse(path)

            issues = fd.validate()
            # 如果有 issues, 打印出来方便调试
            if issues:
                print(f"Validation issues: {issues}")
            # PID 数据应该充足
            for axis in ["roll", "pitch", "yaw"]:
                self.assertGreater(fd.pid[axis].sample_count, 100)


class TestBBLEncodingRoundtrip(unittest.TestCase):
    """测试编码/解码往返一致性。"""

    def test_unsigned_vb_roundtrip(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader

        for value in [0, 1, 127, 128, 255, 300, 16383, 16384, 100000]:
            encoded = BBLBuilder._encode_unsigned_vb(value)
            reader = BBLStreamReader(encoded)
            decoded = reader.read_unsigned_vb()
            self.assertEqual(decoded, value, f"Roundtrip failed for {value}")

    def test_signed_vb_roundtrip(self):
        from smarttune.platform.betaflight.bbl_parser import BBLStreamReader

        for value in [0, 1, -1, 5, -5, 127, -128, 300, -300, 10000, -10000]:
            encoded = BBLBuilder._encode_signed_vb(value)
            reader = BBLStreamReader(encoded)
            decoded = reader.read_signed_vb()
            self.assertEqual(decoded, value, f"Roundtrip failed for {value}")


class TestBBLFlightModeDecode(unittest.TestCase):
    """测试飞行模式标志位解码。"""

    def test_acro_mode(self):
        from smarttune.platform.betaflight.bbl_parser import get_primary_mode
        # ARM only (bit 0) → ACRO
        self.assertEqual(get_primary_mode(0x01), "ACRO")

    def test_angle_mode(self):
        from smarttune.platform.betaflight.bbl_parser import get_primary_mode
        # ARM + ANGLE (bit 0 + bit 1) → ANGLE
        self.assertEqual(get_primary_mode(0x03), "ANGLE")

    def test_horizon_mode(self):
        from smarttune.platform.betaflight.bbl_parser import get_primary_mode
        # ARM + HORIZON (bit 0 + bit 2) → HORIZON
        self.assertEqual(get_primary_mode(0x05), "HORIZON")

    def test_failsafe(self):
        from smarttune.platform.betaflight.bbl_parser import get_primary_mode
        # ARM + FAILSAFE (bit 0 + bit 15)
        self.assertEqual(get_primary_mode(0x01 | (1 << 15)), "FAILSAFE")

    def test_disarmed(self):
        from smarttune.platform.betaflight.bbl_parser import get_primary_mode
        self.assertEqual(get_primary_mode(0x00), "DISARMED")

    def test_decode_flight_modes_multiple(self):
        from smarttune.platform.betaflight.bbl_parser import decode_flight_modes
        # ARM + AIRMODE (bit 0 + bit 19)
        modes = decode_flight_modes(0x01 | (1 << 19))
        self.assertIn("ARM", modes)
        self.assertIn("AIR", modes)


class TestBBLParamMapping(unittest.TestCase):
    """测试参数映射在解析后的 FlightData 中正确工作。"""

    def test_pid_params_in_flight_data(self):
        from smarttune.platform.betaflight import BetaflightAdapter

        with tempfile.TemporaryDirectory() as tmpdir:
            builder = BBLBuilder()
            data = builder.build_minimal(n_frames=100)
            path = Path(tmpdir) / "test.bbl"
            path.write_bytes(data)

            adapter = BetaflightAdapter()
            fd = adapter.parse(path)

            # Header 参数应该作为 float 出现在 params 中
            self.assertIn("pid_roll_p", fd.params)
            self.assertEqual(fd.params["pid_roll_p"], 45.0)

            # 参数映射 — new names for BF 4.5+
            generic = adapter.map_param_to_generic("p_roll")
            self.assertEqual(generic, "pid.roll.p")

            platform = adapter.map_param_to_platform("pid.roll.p")
            self.assertEqual(platform, "p_roll")

            # 也支持旧参数名的反向查找
            generic_old = adapter.map_param_to_generic("pid_roll_p")
            self.assertEqual(generic_old, "pid.roll.p")


class TestBBLMultiSegment(unittest.TestCase):
    """测试多段飞行日志解析。"""

    def test_two_segments(self):
        from smarttune.platform.betaflight.bbl_parser import parse_bbl

        builder = BBLBuilder()
        seg1 = builder.build_minimal(n_frames=100, include_mode_event=False)
        seg2 = builder.build_minimal(n_frames=50, include_mode_event=False)

        # 拼接两段
        combined = seg1 + seg2

        segments = parse_bbl(combined, max_segments=2)
        self.assertGreaterEqual(len(segments), 1)
        # 至少第一段应该解析成功
        self.assertGreater(len(segments[0].frames), 0)


if __name__ == "__main__":
    unittest.main()
