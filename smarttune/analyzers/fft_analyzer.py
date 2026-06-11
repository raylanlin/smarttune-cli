"""
smarttune/fft_analyzer.py — FFT 频谱分析模块

分析 IMU 振动数据，识别振动源，给出 ArduPilot 陷波滤波器参数建议。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from scipy.signal import find_peaks
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# 内部异常
# ---------------------------------------------------------------------------

class FFTAnalyzerError(Exception):
    """FFTAnalyzer 基异常。"""
    pass


class InsufficientDataError(FFTAnalyzerError):
    """数据点数不足，无法进行有效 FFT 分析。"""
    pass


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _identify_source(
    freq_hz: float, peaks: List[Dict], idx: int,
    bands: Optional[Dict[str, Tuple[float, float]]] = None,
) -> str:
    """
    根据频率范围和相邻谐波关系推断振动源类型。

    Parameters
    ----------
    freq_hz : float
        峰值的中心频率。
    peaks : list
        所有已识别峰值的列表（按频率升序）。
    idx : int
        当前峰在 peaks 中的索引。
    bands : dict, optional
        从 knowledge base 读取的频率带配置。
        key → (min_hz, max_hz)，fallback 使用硬编码默认值。

    Returns
    -------
    str
        振动源标签
    """
    # fallback 硬编码频带（当 KB 无配置时使用）
    _defaults: Dict[str, Tuple[float, float]] = {
        "motor": (60.0, 250.0),
        "prop_blade_pass": (251.0, 400.0),
        "structural_resonance": (401.0, 500.0),
        "high_freq_resonance": (500.0, 2000.0),
        "motor_low": (30.0, 60.0),
    }
    b = {**_defaults, **bands} if bands else _defaults  # C12 修复：KB 部分覆盖时用默认值补齐，避免 KeyError
    # 检查是否为已知基频的谐波（2x, 3x）
    if idx > 0:
        prev_freq = peaks[idx - 1]["freq"]
        ratio = freq_hz / prev_freq
        if 1.9 <= ratio <= 2.1:
            src = peaks[idx - 1].get("source", "")
            if src in ("motor", "prop_blade_pass"):
                return src  # 继承基频的源类型
        if idx > 1:
            prev2_freq = peaks[idx - 2]["freq"]
            ratio2 = freq_hz / prev2_freq
            if 2.9 <= ratio2 <= 3.1:
                src = peaks[idx - 2].get("source", "")
                if src in ("motor", "prop_blade_pass"):
                    return src

    # 按频率范围分类（不重叠的分区，从 KB 或 fallback 读取）
    # 电机基频
    if b["motor"][0] <= freq_hz <= b["motor"][1]:
        return "motor"
    # 螺旋桨叶频
    if b["prop_blade_pass"][0] <= freq_hz <= b["prop_blade_pass"][1]:
        return "prop_blade_pass"
    # 结构谐振
    if b["structural_resonance"][0] <= freq_hz <= b["structural_resonance"][1]:
        return "structural_resonance"
    # 高频谐振（原 bearing_wear — 多旋翼 >500Hz 窄带峰更有可能是结构谐振而非轴承）
    if freq_hz > b["high_freq_resonance"][0]:
        return "high_freq_resonance"
    # 低频电机（备用，大型机/农业机电机基频 30-60Hz）
    if b["motor_low"][0] <= freq_hz < b["motor_low"][1]:
        return "motor"
    return "unknown"


# 内部 5 级标签 → 全库统一 Assessment 枚举值（models/analysis_result.py）
_ASSESSMENT_MAP = {
    "EXCELLENT": "EXCELLENT",
    "GOOD":      "GOOD",
    "MARGINAL":  "MARGINAL",
    "SEVERE":    "POOR",
    "CRITICAL":  "UNUSABLE",
}


def _vibration_level(value_mss: float, thresholds: List[Dict]) -> str:
    """根据阈值表返回振动等级标签（内部 5 级：含 SEVERE/CRITICAL）。"""
    if not thresholds:
        # 无阈值表时，用硬编码默认值
        if value_mss < 0.5:
            return "EXCELLENT"
        elif value_mss < 1.0:
            return "GOOD"
        elif value_mss < 3.0:
            return "MARGINAL"
        elif value_mss < 10.0:
            return "SEVERE"
        else:
            return "CRITICAL"
    for entry in thresholds:
        lo, hi = entry["range"]
        if lo <= value_mss < hi:
            return entry["level"]
    # 超出所有 range → 取最后一个 level
    return thresholds[-1]["level"]


# ---------------------------------------------------------------------------
# FFTAnalyzer
# ---------------------------------------------------------------------------

class FFTAnalyzer:
    """
    IMU 振动频谱分析器。

    使用 FFT 分析 IMU 陀螺仪/加速度计数据，识别振动频率峰值，
    推断振动源（电机/螺旋桨/结构），并给出 ArduPilot 陷波滤波器参数。

    Parameters
    ----------
    knowledge : Dict, optional
        已解析的日志解析器实例。
    knowledge : Dict
        filter_rules.json 加载后的知识库字典。
    overlap : float, optional
        FFT 窗口重叠比例（默认 0.5）。设置 ``15/16`` (≈0.9375) 可对齐
        WebTools 的 93.75% 重叠策略。

    Raises
    ------
    InsufficientDataError
        IMU 数据点少于 2 个，无法执行 FFT。

    Attributes
    ----------
    gyro_sample_rate : float
        陀螺仪采样率（Hz），从数据时间戳估算。
    accel_sample_rate : float
        加速度计采样率（Hz）。
    """

    # 最小 FFT 点数（不够则报错）
    _MIN_SAMPLES = 16

    # WebTools 默认 overlap = window_size / 16 hop → 15/16 ≈ 93.75%
    WEBTOOLS_OVERLAP = 15.0 / 16.0

    def __init__(
        self,
        knowledge: Optional[Dict[str, Any]] = None,
        overlap: float = 0.5,
    ) -> None:
        self._kb = knowledge or {}
        self._default_overlap = overlap
        self._platform: str = ""    # analyze() 时从 FlightData.platform 填充

        # 内部状态（由 analyze() 填充）
        self._gyro_data: Optional[Dict[str, np.ndarray]] = None
        self._accel_data: Optional[Dict[str, np.ndarray]] = None
        self._gyro_sample_rate: float = 0.0
        self._accel_sample_rate: float = 0.0
        self._vibration_mss: float = 0.0
        self._freqs: Optional[np.ndarray] = None
        self._magnitudes: Optional[np.ndarray] = None

    # -----------------------------------------------------------------------
    # 公开 API
    # -----------------------------------------------------------------------

    def analyze(self, flight_data=None) -> Dict[str, Any]:
        """
        执行完整振动分析。

        Parameters
        ----------
        flight_data : FlightData
            统一飞行数据结构。

        Returns
        -------
        Dict[str, Any]
            输出结构（符合 filter_rules.json output_schema）：
            - ``vibration_level`` : str
            - ``vibration_value_mss`` : float
            - ``peak_frequencies`` : List[Dict]
            - ``recommendations`` : Dict
            - ``warnings`` : List[str]
            - ``mechanical_checks_required`` : List[str]
        """
        self._platform = getattr(flight_data, "platform", "") or ""
        self._load_from_flight_data(flight_data)
        self._compute_sample_rates()
        self._compute_vibration_level()
        self._compute_fft_from_gyro()
        peaks = self._find_peak_frequencies()

        warnings: List[str] = []
        mech_checks: List[str] = []

        vt = self._kb.get("vibration_thresholds", {}) if self._kb else {}
        vib_level = _vibration_level(
            self._vibration_mss,
            vt.get("levels") or vt.get("fallback_levels", []),
        )

        if vib_level in ("SEVERE", "CRITICAL"):
            warnings.append(
                f"振动等级 {vib_level} — 禁止在自动模式下飞行，需先机械检修。"
            )
            mech_checks.extend([
                "螺旋桨动平衡",
                "电机安装垫圈硬度",
                "机臂/框架裂纹",
                "IMU 缓震垫",
                "电池/负载安装方式",
            ])

        recs = self._build_notch_recommendation(peaks, vib_level)

        # 平台分支：PX4 静态陷波无 mode/REF/HMC/ATT 概念，
        # 输出只保留 PX4 可表达的参数并转换语义。
        if self._platform == "px4":
            recs = self._adapt_recommendation_px4(recs, peaks, warnings)
        else:
            # C3 修复配套（仅 ArduPilot 语义）：mode 2 的 REF 必须由用户
            # 设为悬停油门值，本工具无法推断
            if recs.get("filter.notch1.mode") == 2 and recs.get("filter.notch1.enable") == 1:
                warnings.append(
                    "陷波建议使用油门跟踪模式 (mode 2)：INS_HNTCH_REF 需设为悬停油门参考值"
                    "（0~1，可取 MOT_THST_HOVER 学习值），未设置时陷波不会随油门跟踪。"
                )

        # 如果有多个峰值且无谐波关系，建议进一步诊断
        if len(peaks) >= 2 and vib_level not in ("EXCELLENT", "GOOD"):
            warnings.append(
                f"检测到 {len(peaks)} 个显著峰值，建议使用 notch mode=4 (FFT跟踪) "
                "或为每个峰值配置独立陷波（notch2 / secondary filter）。"
            )

        return {
            # 等级标签统一：对外输出映射到全库 Assessment 枚举值
            # （SEVERE→POOR、CRITICAL→UNUSABLE），原始 5 级标签保留在
            # vibration_level_raw 供阈值调试/知识库规则引用。
            "vibration_level": _ASSESSMENT_MAP.get(vib_level, vib_level),
            "vibration_level_raw": vib_level,
            "vibration_value_mss": round(self._vibration_mss, 3),
            "peak_frequencies": peaks,
            "recommendations": recs,
            "warnings": warnings,
            "mechanical_checks_required": mech_checks,
        }

    def compute_fft(
        self,
        gyro_data: np.ndarray,
        sample_rate: float,
        window_size: Optional[int] = None,
        overlap: float = 0.5,
        scaling: str = "dbfs",
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        对一维时序数据计算单边幅度谱。

        Parameters
        ----------
        gyro_data : np.ndarray
            时域信号（shape (N,)）。
        sample_rate : float
            采样率（Hz）。
        window_size : int, optional
            FFT 窗口大小（必须为 2 的幂）。默认 = 全部数据。
        overlap : float
            窗口重叠比例（0~1），默认 0.5（WebTools 用 0.5）。
        scaling : str
            ``"dbfs"`` (dBFS, RMS 基准), ``"linear"`` (2/N 线性),
            ``"psd"`` (功率谱密度 dB)。

        Returns
        -------
        Tuple[freqs, magnitudes]

        Raises
        ------
        InsufficientDataError
            数据点不足。
        """
        n = gyro_data.size
        if n < self._MIN_SAMPLES:
            raise InsufficientDataError(
                f"数据点不足（{n} < {self._MIN_SAMPLES}），无法进行有效 FFT。"
            )

        # 默认窗口大小 = 整个数据长度（对齐旧行为）
        if window_size is None:
            window_size = n

        # 确保窗口不超过数据
        window_size = min(window_size, n)

        # Hanning 窗
        window = np.hanning(window_size)
        # 窗口校正因子
        window_sum = np.sum(window)
        window_sum_sq = np.sum(window ** 2)

        hop = max(1, int(window_size * (1 - overlap)))
        freqs = np.fft.rfftfreq(window_size, d=1.0 / sample_rate)
        num_bins = len(freqs)

        # 分窗 FFT + 平均
        signal = gyro_data - np.mean(gyro_data)
        acc = np.zeros(num_bins)
        count = 0

        pos = 0
        while pos + window_size <= n:
            seg = signal[pos:pos + window_size] * window
            fft_vals = np.fft.rfft(seg)
            mag = np.abs(fft_vals)

            if scaling == "linear":
                # 2/N 归一化（WebTools 默认）
                mag = mag * 2.0 / window_sum
            elif scaling == "psd":
                # PSD: |X|^2 / (fs * S2)  其中 S2 = sum(w^2)
                mag = (mag ** 2) / (sample_rate * window_sum_sq)
            # else dbfs: 不在循环中归一化

            acc += mag
            count += 1
            pos += hop

        if count == 0:
            return freqs.astype(np.float64), np.full(num_bins, -120.0)

        avg = acc / count

        if scaling == "dbfs":
            rms = np.sqrt(np.mean(signal ** 2))
            if rms < 1e-12:
                db = np.full(num_bins, -120.0)
            else:
                db = 20.0 * np.log10(avg / (window_size * rms) + 1e-12)
            return freqs.astype(np.float64), db.astype(np.float64)
        elif scaling == "psd":
            db = 10.0 * np.log10(np.maximum(avg, 1e-30))
            return freqs.astype(np.float64), db.astype(np.float64)
        else:
            # linear — 返回线性幅度
            return freqs.astype(np.float64), avg.astype(np.float64)

    def find_peak_frequencies(
        self,
        freqs: np.ndarray,
        magnitudes: np.ndarray,
        bands: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        在幅度谱中定位显著峰值。

        Parameters
        ----------
        freqs : np.ndarray
            频率数组（Hz）。
        magnitudes : np.ndarray
            对应的幅度（dBFS）。

        Returns
        -------
        List[Dict[str, Any]]
            每个峰值含字段：
            ``freq_hz``, ``magnitude_db``, ``source``, ``is_harmonic``。
            按频率升序排列。
        """
        if magnitudes is None or magnitudes.size == 0:
            return []

        # 噪声基底估计：中位数（比最低 20% 均值更鲁棒）
        # 理由：最低 20% 均值在多峰背景下会把次峰纳入估计，导致基底偏高；
        # 中位数对异常值不敏感，且不依赖分位数选择，更接近 ArduPilot AP_GyroFFT 的在线 median 估计。
        noise_floor = float(np.median(magnitudes))

        # 峰值阈值：高于噪声基底 30dB（filter_rules.json）
        threshold_db = noise_floor + 30.0

        # scipy find_peaks：prominence 保证峰值突出于周围背景
        if _HAS_SCIPY:
            peak_indices, properties = find_peaks(
                magnitudes,
                height=threshold_db,
                prominence=15.0,
                distance=3,
            )
        else:
            # 纯 numpy 回退（仅用高度阈值）
            peak_indices = np.where(
                (magnitudes[1:-1] > magnitudes[:-2]) &
                (magnitudes[1:-1] > magnitudes[2:]) &
                (magnitudes[1:-1] > threshold_db)
            )[0] + 1

        if peak_indices.size == 0:
            return []

        # 构建峰列表（按频率升序）
        raw_peaks = sorted(
            [(int(i), float(freqs[i]), float(magnitudes[i]))
             for i in peak_indices],
            key=lambda x: x[1],
        )

        peaks: List[Dict[str, Any]] = []
        prev_src = "unknown"
        for i, (idx, freq_hz, mag_db) in enumerate(raw_peaks):
            src = _identify_source(freq_hz, peaks, i, bands=bands)
            is_harmonic = (
                src in ("motor", "prop_blade_pass") and
                i > 0 and
                any(
                    1.9 <= freq_hz / p["freq"] <= 2.1 or
                    2.9 <= freq_hz / p["freq"] <= 3.1
                    for p in peaks
                )
            )
            peaks.append({
                "freq": round(freq_hz, 1),
                "magnitude_db": round(mag_db, 1),
                "source": src,
                "is_harmonic": is_harmonic,
            })

        return peaks

    def recommend_notch_filter(
        self,
        peaks: List[Dict[str, Any]],
        vib_level: str,
    ) -> Dict[str, Any]:
        """
        根据已识别峰值和振动等级生成陷波滤波器参数建议。

        Parameters
        ----------
        peaks : List[Dict]
            ``find_peak_frequencies()`` 返回的峰值列表。
        vib_level : str
            振动等级标签。

        Returns
        -------
        Dict[str, Any]
            ArduPilot INS_HNTCH_* 参数建议，含辅助字段：
            - ``INS_HNTCH_ENABLE`` : int
            - ``INS_HNTCH_MODE`` : int
            - ``INS_HNTCH_FREQ`` : float
            - ``INS_HNTCH_BW`` : float
            - ``INS_HNTCH_ATT`` : int
            - ``INS_HNTCH_REF`` : float（mode 2/3/4）
            - ``INS_HNTCH_HMC`` : int
            - ``INS_GYRO_FILTER`` : int
            - ``INS_ACCEL_FILTER`` : int
        """
        kb = self._kb

        # 默认值（可被覆盖）
        mode = 1
        freq = 80.0
        bw = 40.0
        att = 40
        ref = 0.0
        hmc = 0
        gyro_filt = 60
        accel_filt = 10

        if not peaks:
            # 无显著峰值
            if vib_level in ("EXCELLENT", "GOOD"):
                return {
                    "filter.notch1.enable": 0,
                    "filter.notch1.mode": 0,
                    "filter.notch1.freq": 0.0,
                    "filter.notch1.bw": 0.0,
                    "filter.notch1.att": 0,
                    "filter.notch1.ref": 0.0,
                    "filter.notch1.hmc": 0,
                    "filter.gyro_lpf": gyro_filt,
                    "filter.accel_lpf": accel_filt,
                }
            # 有等级但无峰值 → 保守设置
            freq = 80.0
            bw = 40.0
            att = 60 if vib_level in ("MARGINAL", "SEVERE") else 40
            hmc = 1
        else:
            # 主峰（最高幅值）
            primary = max(peaks, key=lambda p: p["magnitude_db"])
            freq = primary["freq"]
            primary_src = primary["source"]

            # 谐波检测
            harmonics = [p for p in peaks if p["is_harmonic"]]
            has_harmonics = len(harmonics) > 0

            # 带宽策略
            if primary_src == "structural_resonance":
                # 结构谐振：宽 BW
                bw = max(5.0, freq * 0.8)
            elif primary_src == "motor":
                # 电机噪声：BW = FREQ/2（标准）
                bw = max(5.0, freq / 2.0)
            else:
                bw = max(5.0, freq / 2.0)

            # ATT 策略
            if vib_level in ("MARGINAL", "SEVERE", "CRITICAL"):
                att = 60
            else:
                att = 40

            # 谐波开关（INS_HNTCH_HMC）— 从 KB 读取电机/浆叶的推荐值，回退硬编码
            # KB 的 HMC 规则：motor/prop_blade_pass → 1，其他 → 0
            hmc_rules = kb.get("HMC", [])
            hmc_motor = 1  # 默认：电机/浆叶噪声总有谐波成分，开启 HMC
            hmc_other = 0
            for rule in hmc_rules:
                if "multirotor" in rule.get("rule", "").lower() or "motor" in rule.get("rule", "").lower():
                    rec = rule.get("recommendation", "")
                    if "HMC=1" in rec or "harmonics" in rec.lower():
                        hmc_motor = 1
                elif "fixed-frequency" in rule.get("rule", "").lower():
                    rec = rule.get("recommendation", "")
                    if "HMC=0" in rec:
                        hmc_other = 0

            if primary_src in ("motor", "prop_blade_pass"):
                hmc = hmc_motor
            else:
                hmc = hmc_other

            # 多峰策略
            # C3 修复：INS_HNTCH_REF 语义 ——
            #   mode 1 (静态):      REF 无意义，置 0
            #   mode 2 (油门跟踪):  REF = 悬停油门参考值 (0~1, 如 MOT_THST_HOVER)，
            #                       无法从振动谱推断 → 置 0 并由 analyze() 追加警告
            #   mode 4 (FFT 跟踪): REF 作缩放系数，常规值 1.0
            # 旧实现把中心频率（如 93.7）写进 REF，越界且语义错误。
            if len(peaks) >= 3:
                # 3+ 峰值 → FFT 跟踪模式（mode 4）
                mode = 4
                ref = 1.0
            elif len(peaks) == 2:
                # 2 峰 → 油门跟踪 + 建议启用 notch2
                mode = 2
                ref = 0.0
            else:
                # 单峰
                if primary_src in ("motor", "prop_blade_pass"):
                    mode = 2  # 油门动态跟踪
                    ref = 0.0
                else:
                    mode = 1  # 静态
                    ref = 0.0

            # GYRO_FILTER 调整（按振动等级降档）
            # 从 KB 读取合法范围以进行 clamp
            gtt = kb.get("gyro_filter_tuning_table", {})
            gyro_filt_min = int(gtt.get("minimum_gyro_filter", 10))
            gyro_filt_max = int(gtt.get("maximum_gyro_filter", 256))
            if vib_level == "MARGINAL":
                gyro_filt = 40
            elif vib_level == "SEVERE":
                gyro_filt = 20
            elif vib_level == "CRITICAL":
                gyro_filt = 10
            else:
                gyro_filt = 60
            # clamp 到 KB 定义的合法范围
            gyro_filt = max(gyro_filt_min, min(gyro_filt_max, gyro_filt))

        return {
            "filter.notch1.enable": 1,
            "filter.notch1.mode": mode,
            "filter.notch1.freq": round(freq, 1),
            "filter.notch1.bw": round(bw, 1),
            "filter.notch1.att": att,
            "filter.notch1.ref": round(ref, 1),
            "filter.notch1.hmc": hmc,
            "filter.gyro_lpf": gyro_filt,
            "filter.accel_lpf": accel_filt,
        }

    def _adapt_recommendation_px4(
        self,
        recs: Dict[str, Any],
        peaks: List[Dict[str, Any]],
        warnings: List[str],
    ) -> Dict[str, Any]:
        """把 AP 语义的陷波建议转换为 PX4 可表达的参数集。

        PX4 静态陷波（IMU_GYRO_NF0/NF1）只有中心频率 + 带宽：
        - mode/REF/HMC/ATT 无对应概念 → 从输出中移除
        - 陷波启用 = freq > 0（无独立 enable 参数，保留 generic
          enable 键供上层判断，但映射层不会输出它）
        - 第二峰值 → filter.notch2.*（IMU_GYRO_NF1_*）
        - 电机噪声需要跟踪时输出警告推荐 ESC RPM 动态陷波
        """
        out: Dict[str, Any] = {
            "filter.notch1.enable": recs.get("filter.notch1.enable", 0),
            "filter.notch1.freq": recs.get("filter.notch1.freq", 0.0),
            "filter.notch1.bw": recs.get("filter.notch1.bw", 0.0),
            "filter.gyro_lpf": recs.get("filter.gyro_lpf", 40),
            "filter.accel_lpf": recs.get("filter.accel_lpf", 30),
        }

        # 第二峰值（非谐波）→ NF1
        if len(peaks) >= 2:
            sorted_peaks = sorted(peaks, key=lambda p: -p["magnitude_db"])
            second = next(
                (p for p in sorted_peaks[1:] if not p.get("is_harmonic")), None,
            )
            if second is not None:
                out["filter.notch2.enable"] = 1
                out["filter.notch2.freq"] = round(second["freq"], 1)
                out["filter.notch2.bw"] = round(max(5.0, second["freq"] / 2.0), 1)

        # 动态跟踪需求警告：静态陷波不随油门漂移
        motor_peaks = [p for p in peaks if p.get("source") in ("motor", "prop_blade_pass")]
        if motor_peaks and out["filter.notch1.enable"]:
            warnings.append(
                "PX4 静态陷波（IMU_GYRO_NF0）不随油门跟踪电机基频漂移："
                "建议把 BW 设宽以覆盖巡航~满油门频段，或升级 PX4 v1.14+ "
                "启用 ESC RPM 动态陷波（DShot telemetry + IMU_GYRO_DNF_*）。"
            )

        return out

    def get_spectrum_data(self) -> Dict[str, Any]:
        """
        返回完整的频谱数据（供可视化使用）。

        必须在调用 ``analyze()`` 之后再调用本方法，确保内部状态已就绪。

        Returns
        -------
        Dict[str, Any]
            - ``freqs`` : List[float]，频率轴（Hz）
            - ``magnitudes`` : List[float]，幅度（dBFS）
            - ``sample_rate`` : float，采样率（Hz）
            - ``peaks`` : List[Dict]，峰值列表（与 ``analyze()`` 一致）
            - ``vibration_level`` : str
            - ``vibration_value_mss`` : float
        """
        peaks = self._find_peak_frequencies()
        _raw_level = _vibration_level(
            self._vibration_mss,
            self._kb.get("vibration_thresholds", {}).get("levels") or self._kb.get("vibration_thresholds", {}).get("fallback_levels", []),
        )
        return {
            "freqs":            self._freqs.tolist() if self._freqs is not None else [],
            "magnitudes":       self._magnitudes.tolist() if self._magnitudes is not None else [],
            "sample_rate":     self._gyro_sample_rate,
            "peaks":           peaks,
            "vibration_level": _ASSESSMENT_MAP.get(_raw_level, _raw_level),
            "vibration_level_raw": _raw_level,
            "vibration_value_mss": round(self._vibration_mss, 3),
        }

    # -----------------------------------------------------------------------
    # 内部实现
    # -----------------------------------------------------------------------

    def _load_from_flight_data(self, flight_data) -> None:
        """从 FlightData 加载 IMU 数据到内部字典格式。"""
        if flight_data is None:
            raise InsufficientDataError("No flight data provided")

        gyro = flight_data.gyro
        accel = flight_data.accel
        imu_ts = flight_data.imu_timestamp_s

        if gyro is None or accel is None or imu_ts is None:
            raise InsufficientDataError("No IMU data in flight log")
        if len(gyro) < self._MIN_SAMPLES:
            raise InsufficientDataError(
                f"IMU data too short ({len(gyro)} samples, need {self._MIN_SAMPLES}+)"
            )

        # Build the internal dict format expected by downstream methods
        self._gyro_data = {
            "time": imu_ts,
            "GyrX": gyro[:, 0],
            "GyrY": gyro[:, 1],
            "GyrZ": gyro[:, 2],
            "AccX": accel[:, 0],
            "AccY": accel[:, 1],
            "AccZ": accel[:, 2],
        }
        self._accel_data = self._gyro_data  # Same source

    def _compute_sample_rates(self) -> None:
        """从时间戳估算采样率。"""
        t_gyro = self._gyro_data["time"]
        if t_gyro.size < 2:
            self._gyro_sample_rate = 0.0
            self._accel_sample_rate = 0.0
            return

        durations = np.diff(t_gyro)
        # 去掉极端异常值（丢帧产生的跳变）
        valid = durations[(durations > 0) & (durations < 1.0)]
        if valid.size > 0:
            mean_dt = float(np.median(valid))
            self._gyro_sample_rate = 1.0 / mean_dt if mean_dt > 0 else 0.0
        else:
            self._gyro_sample_rate = 0.0
        self._accel_sample_rate = self._gyro_sample_rate

    def _compute_vibration_level(self) -> None:
        """
        计算振动等级（m/s² RMS）。

        等效于 ArduPilot VIBE：去均值后三轴 RMS，排除重力偏置。
        """
        acc = self._accel_data
        n = acc["AccX"].size
        if n == 0:
            self._vibration_mss = 0.0
            return

        # 去均值（排除重力偏置），再计算三轴 RMS
        ax = acc["AccX"] - np.mean(acc["AccX"])
        ay = acc["AccY"] - np.mean(acc["AccY"])
        az = acc["AccZ"] - np.mean(acc["AccZ"])
        self._vibration_mss = float(
            np.sqrt(np.mean(ax ** 2) + np.mean(ay ** 2) + np.mean(az ** 2))
        )

    def _compute_fft_from_gyro(self) -> None:
        """对三轴陀螺仪数据分别做 FFT，取三轴最大值合成幅度谱。

        设计说明：
        1. 对每轴独立做 FFT（避免欧几里得范数引入的 DC 和非线性混叠）
        2. 取三轴逐频率最大值包络（保留各轴独立峰值特征）
        3. 使用 ~1 秒窗口（2 的幂）
        4. overlap 固定 50%（Welch 标准）——峰值检测目标是定位频率，
           不需要 93.75% overlap 带来的过度平滑，且 50% 计算量仅为 1/8。
           注：WebTools 的 93.75% overlap 服务于阶跃反卷积平均，
           与此处峰值检测目的不同，不应照搬。
        """
        gyro = self._gyro_data
        sr = self._gyro_sample_rate

        if sr <= 0 or gyro["GyrX"].size < self._MIN_SAMPLES:
            self._freqs = np.array([], dtype=np.float64)
            self._magnitudes = np.array([], dtype=np.float64)
            return

        # 窗口大小：~1 秒，向上取 2 的幂，最小 64
        n = gyro["GyrX"].size
        win_target = int(sr)
        window_size = 64
        while window_size < win_target:
            window_size *= 2
        window_size = min(window_size, n)

        # 对三轴分别做 FFT；峰值检测用 50% overlap（Welch 标准）
        _PEAK_DETECT_OVERLAP = 0.5
        all_mags = []
        freqs = None
        for key in ("GyrX", "GyrY", "GyrZ"):
            data = gyro[key]
            if data.size < self._MIN_SAMPLES:
                continue
            f, m = self.compute_fft(
                data, sr,
                window_size=window_size,
                overlap=_PEAK_DETECT_OVERLAP,
            )
            if freqs is None:
                freqs = f
            all_mags.append(m)

        if not all_mags or freqs is None:
            self._freqs = np.array([], dtype=np.float64)
            self._magnitudes = np.array([], dtype=np.float64)
            return

        # 取三轴最大值包络（dBFS 域取 max — 保留每轴最显著的峰）
        self._freqs = freqs
        self._magnitudes = np.max(np.array(all_mags), axis=0)

    def _find_peak_frequencies(self) -> List[Dict[str, Any]]:
        """执行峰值查找 + 源识别。"""
        if self._freqs is None or self._freqs.size == 0:
            return []
        return self.find_peak_frequencies(
            self._freqs, self._magnitudes,
            bands=self._kb.get("frequency_bands") if self._kb else None,
        )

    def _build_notch_recommendation(
        self,
        peaks: List[Dict[str, Any]],
        vib_level: str,
    ) -> Dict[str, Any]:
        """供 analyze() 内部调用，包装公开方法。"""
        return self.recommend_notch_filter(peaks, vib_level)
