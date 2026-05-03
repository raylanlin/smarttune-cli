"""
magfit.py - 磁力计校准参数拟合模块

基于 ArduPilot 飞行日志后处理，拟合 COMPASS 参数（OFS / DIA / ODI / MOT），
计算 fitness 误差，并按知识库规则输出诊断建议。

算法概述
--------
MAGFit 通过以下流程工作：
1. 从日志提取 GPS 位置、姿态（attitude quaternion）和磁力计原始读数
2. 用 WMM 近似模型计算"期望"地磁场在机体坐标系下的分量
3. 用 scipy.optimize.least_squares 拟合补偿参数
4. 计算 fitness（RMS 残差，单位 mGauss）并输出诊断

References
----------
- ArduPilot MAGFit: https://ardupilot.org/copter/docs/common-magfit.html
- MAGFit WebTool:   https://firmware.ardupilot.org/Tools/WebTools/MAGFit/
- 知识库:           files/output/magfit-knowledge-base.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import least_squares
from smarttune.errors import MAGFitError

# ---------------------------------------------------------------------------
# 内部异常
# ---------------------------------------------------------------------------

# MAGFitError 从 errors.py 导入，已继承自 APTuneError
class CoverageError(MAGFitError):
    """轨迹覆盖不足，无法可靠拟合。"""
    pass


class FitError(MAGFitError):
    """参数拟合失败。"""
    pass


# ---------------------------------------------------------------------------
# 地理 / 地磁辅助
# ---------------------------------------------------------------------------

# 尝试使用 WMM 精确模型；fallback 到简化 IGRF 近似
try:
    from smarttune.analyzers.wmm import get_earth_field_ned_ut, get_earth_field
    _HAS_WMM = True
except ImportError:
    _HAS_WMM = False


# IGRF/WMM 近似地磁场强度（微特斯拉），用于典型中纬度地区
# 实参时建议通过 WMM 库（pyIGRF / geomag）替换此常数
TYPICAL_FIELD_UT = 50.0  # μT → 换算 1 mGauss = 0.1 μT


def earth_field_ned(lat_deg: float, lon_deg: float, alt_m: float) -> np.ndarray:
    """
    根据经纬度返回 NED 地磁场向量 (μT)。

    优先使用 WMM 查表；无模块时 fallback 到 IGRF 近似。
    """
    if _HAS_WMM and lat_deg != 0.0 and lon_deg != 0.0:
        result = get_earth_field_ned_ut(lat_deg, lon_deg)
        if result is not None:
            return result
    # fallback
    return igrf_approximation(lat_deg, alt_m)


def igrf_approximation(lat_deg: float, alt_m: float) -> np.ndarray:
    """
    用简化 IGRF 模型估算当地地磁场 NED 分量（μT）。

    简化假设：
    - 总强度按高度衰减 0.015 μT/km
    - 倾角按纬度分段线性近似（中国/东南亚典型值 ~45°–60°）
    - 偏角忽略（对拟合影响主要在水平方向，FITNESS 指标不敏感于此）

    Parameters
    ----------
    lat_deg : float
        纬度（度），北正南负。
    alt_m : float
        椭圆高度（米）。

    Returns
    -------
    np.ndarray, shape (3,)
        [B_N, B_E, B_D] — NED 坐标系下的磁场分量（μT）。
    """
    # 典型总强度（随纬度轻微变化）
    intensity_ut = 50.0 - 0.015 * (alt_m / 1000.0)

    # 磁倾角近似（度）
    if lat_deg >= 30:
        dip_deg = 45.0 + 0.55 * min(lat_deg - 30.0, 40.0)
    else:
        dip_deg = 35.0 + lat_deg * 0.33

    dip_rad = np.deg2rad(dip_deg)
    # 水平分量强度
    h_intensity = intensity_ut * np.cos(dip_rad)
    # 垂直分量（NED D 为正 = 向下）
    d_intensity = intensity_ut * np.sin(dip_rad)

    return np.array([h_intensity, 0.0, d_intensity], dtype=np.float64)


def ned_to_body(ned: np.ndarray, q: np.ndarray) -> np.ndarray:
    """
    将 NED 坐标系向量转换到 Body 机体坐标系。

    Parameters
    ----------
    ned : np.ndarray, shape (3,)
        NED 坐标系下的向量。
    q   : np.ndarray, shape (4,)
        四元数 [w, x, y, z]（scipy / ArduPilot 标准）。

    Returns
    -------
    np.ndarray, shape (3,)
        Body 坐标系下的向量。
    """
    # 标准 quaternion → rotation matrix:
    #   R = [[1-2(y²+z²),  2(xy-wz),   2(xz+wy) ],
    #        [2(xy+wz),    1-2(x²+z²), 2(yz-wx) ],
    #        [2(xz-wy),    2(yz+wx),   1-2(x²+y²)]]
    w, a, b, c = q[0], q[1], q[2], q[3]
    nx, ny, nz = ned[0], ned[1], ned[2]
    ww, aa, bb, cc = w*w, a*a, b*b, c*c
    wb, wc, ab, ac, bc = w*b, w*c, a*b, a*c, b*c

    body = np.empty(3, dtype=np.float64)
    body[0] = (ww + aa - bb - cc) * nx + 2*(ab - wc) * ny + 2*(ac + wb) * nz
    wa = w * a
    body[1] = 2*(ab + wc) * nx + (ww - aa + bb - cc) * ny + 2*(bc - wa) * nz
    body[2] = 2*(ac - wb) * nx + 2*(bc + wa) * ny + (ww - aa - bb + cc) * nz
    return body


# ---------------------------------------------------------------------------
# 诊断阈值（从知识库提取）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fibonacci lattice attitude binning — 与 WebTools MAGFit 对齐
# ---------------------------------------------------------------------------

NUM_ATTITUDE_BINS = 80
_GOLDEN_RATIO = (1.0 + np.sqrt(5.0)) / 2.0


def _build_fibonacci_lattice(n: int = NUM_ATTITUDE_BINS) -> np.ndarray:
    """
    生成 Fibonacci lattice 均匀球面采样点。

    Parameters
    ----------
    n : int
        采样点数量（默认 80，与 WebTools 一致）。

    Returns
    -------
    np.ndarray, shape (n, 3)
        单位球面上的点坐标 [x, y, z]。
    """
    bins = np.empty((n, 3), dtype=np.float64)
    for i in range(n):
        k = i + 0.5
        phi = np.arccos(1.0 - 2.0 * k / n)
        theta = np.pi * (1.0 + np.sqrt(5.0)) * k
        bins[i, 0] = np.cos(theta) * np.sin(phi)
        bins[i, 1] = np.sin(theta) * np.sin(phi)
        bins[i, 2] = np.cos(phi)
    return bins


def _assign_bins(expected_field: np.ndarray, lattice: np.ndarray) -> np.ndarray:
    """
    将每个期望磁场向量分配到最近的 Fibonacci lattice bin。

    Parameters
    ----------
    expected_field : np.ndarray, shape (N, 3)
        机体坐标系期望磁场向量。
    lattice : np.ndarray, shape (M, 3)
        Fibonacci lattice 采样点。

    Returns
    -------
    np.ndarray, shape (N,), dtype int
        每个样本对应的 bin 索引。
    """
    # 归一化为单位向量
    norms = np.linalg.norm(expected_field, axis=1, keepdims=True)
    norms = np.where(norms < 1e-12, 1.0, norms)
    unit = expected_field / norms

    # 用矩阵乘法找最近 bin（最大内积 = 最小角距）
    # dot product: (N, 3) @ (3, M) → (N, M)
    dots = unit @ lattice.T
    return np.argmax(dots, axis=1)


def _compute_bin_weights(bin_indices: np.ndarray, num_bins: int = NUM_ATTITUDE_BINS) -> Tuple[np.ndarray, float]:
    """
    根据 bin 分配计算样本权重和覆盖率。

    与 WebTools get_weights() 完全对齐：
    - 权重 = mean_bin_size / count[bin]
    - 覆盖率 = unique_bins / total_bins

    Parameters
    ----------
    bin_indices : np.ndarray, shape (N,)
        每个样本的 bin 索引。
    num_bins : int
        总 bin 数量。

    Returns
    -------
    Tuple[np.ndarray, float]
        (weights, coverage)
        weights: shape (N,), 每个样本的权重
        coverage: 0~1, 覆盖的 bin 占总 bin 的比例
    """
    n = len(bin_indices)
    counts = np.zeros(num_bins, dtype=np.float64)
    for idx in bin_indices:
        counts[idx] += 1.0

    num_unique = np.sum(counts > 0)
    total = float(n)
    mean_bin_size = total / max(num_unique, 1)
    coverage = float(num_unique) / num_bins

    weights = np.empty(n, dtype=np.float64)
    for i in range(n):
        c = counts[bin_indices[i]]
        weights[i] = mean_bin_size / max(c, 1.0)

    return weights, coverage


THRESHOLDS: Dict[str, Any] = {
    "compass_parameters": {
        "OFS_warning":   600.0,
        "OFS_critical": 1000.0,
        "DIA_min":         0.5,
        "DIA_max":         1.5,
        "DIA_dev_threshold": 0.15,   # |DIA - 1.0| > 此值触发警告
        "ODI_threshold":   0.3,
        "ODI_critical":    0.5,
        "MOT_warning":    50.0,
        "MOT_critical":   100.0,
        "SCALE_min":       0.9,
        "SCALE_max":       1.1,
    },
    "fitness_thresholds": {
        "excellent":    5.0,   # < 5 mGauss — 极佳
        "good":        15.0,   # < 15 mGauss — 良好
        "acceptable":  30.0,   # < 30 mGauss — 可接受
        "marginal":    50.0,   # < 50 mGauss — 勉强
        "poor":       100.0,   # < 100 mGauss — 较差
    },
    "flight_coverage": {
        "min_samples":         500,
        "good_samples":       1000,
        "yaw_coverage_min":     300.0,   # 度
        "yaw_coverage_good":    360.0,
        "pitch_range_min":       40.0,   # 度
        "roll_range_min":        40.0,
    },
}


# ---------------------------------------------------------------------------
# 数据类：拟合结果
# ---------------------------------------------------------------------------

@dataclass
class FitResult:
    """MAGFit 拟合结果的完整快照。"""
    # 原始参数（日志中记录的 COMPASS_OFS_* 等）
    ofs: np.ndarray          # shape (3,) — [X, Y, Z]
    dia: np.ndarray           # shape (3,) — [X, Y, Z]
    odi: np.ndarray           # shape (3,) — [X, Y, Z]
    mot: np.ndarray           # shape (3,) — [X, Y, Z]
    scale: float
    # 拟合质量
    fitness_mgauss: float
    assessment: str           # EXCELLENT / GOOD / ACCEPTABLE / MARGINAL / POOR / BAD
    # 诊断消息列表
    warnings: List[str] = field(default_factory=list)
    # 轨迹覆盖元数据
    coverage: Dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MAGFit 主类
# ---------------------------------------------------------------------------

class MAGFit:
    """
    磁力计参数拟合器。

    Parameters
    ----------
    knowledge : Dict, optional
        知识库字典。
    """

    def __init__(
        self,
        knowledge: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._kb = knowledge or {}

        # ── 内部状态 ──────────────────────────────────────────────
        self._mag_data: Dict[str, np.ndarray] = {}
        self._att_data: Dict[str, np.ndarray] = {}
        self._params:   Dict[str, float] = {}

        # GPS / baro
        self._lat: float = 0.0    # 度
        self._lon: float = 0.0    # 度
        self._alt_m: float = 0.0   # 米（WGS84 高度）

        # 同步后有效样本
        self._mag_synced: Optional[np.ndarray] = None   # shape (N, 3)
        self._field_expected: Optional[np.ndarray] = None  # shape (N, 3)
        self._throttle: Optional[np.ndarray] = None     # shape (N,)
        self._weights: Optional[np.ndarray] = None      # shape (N,)

        self._result: Optional[FitResult] = None

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def analyze(self, flight_data) -> FitResult:
        """
        执行完整分析流程。

        Parameters
        ----------
        flight_data : FlightData
            统一飞行数据结构。

        Returns
        -------
        FitResult
        """
        self._load_data(flight_data)
        self._sync_samples()
        coverage = self._check_coverage()
        self._compute_expected_field()
        result = self._fit_parameters(coverage)
        self._result = result
        return result

    def check_coverage(self, flight_data) -> Dict[str, float]:
        """仅检查飞行轨迹覆盖度，不执行拟合。"""
        self._load_data(flight_data)
        self._sync_samples()
        return self._check_coverage()

    # ------------------------------------------------------------------
    # Step 1: 数据加载
    # ------------------------------------------------------------------

    def _load_data(self, flight_data) -> None:
        """从 FlightData 提取所需数据。"""
        self._params = dict(flight_data.params)

        # 磁力计数据
        if flight_data.mag is not None and flight_data.mag_timestamp_s is not None:
            self._mag_data = {
                "time": flight_data.mag_timestamp_s,
                "MagX": flight_data.mag[:, 0],
                "MagY": flight_data.mag[:, 1],
                "MagZ": flight_data.mag[:, 2],
            }
        else:
            self._mag_data = {"time": np.array([]), "MagX": np.array([]), "MagY": np.array([]), "MagZ": np.array([])}

        # 姿态数据 — 从 extras 获取或留空（magfit 内部会处理）
        self._att_data = flight_data.extras.get("attitude", {
            "time": np.array([]), "Roll": np.array([]), "Pitch": np.array([]), "Yaw": np.array([]),
        })

        # GPS 位置
        gps = flight_data.extras.get("gps_position", {})
        self._lat = gps.get("lat", self._params.get("GPS_LAT", 22.5))
        self._lon = gps.get("lon", self._params.get("GPS_LON", 114.0))
        self._alt_m = gps.get("alt", self._params.get("ALT_M", 50.0))
                self._alt_m = 50.0
        else:
            self._lat = self._params.get("GPS_LAT", 22.5)
            self._lon = self._params.get("GPS_LON", 114.0)
            self._alt_m = self._params.get("ALT_M", 50.0)

        # AHR2 四元数姿态（如果 adapter 放到了 extras 里）
        self._ahr2_data = flight_data.extras.get("ahr2_data", None)

        # 电池电流数据
        self._bat_data = None
        if flight_data.battery_current is not None and len(flight_data.battery_current) > 50:
            self._bat_data = {
                "time": flight_data.battery_timestamp_s,
                "Curr": flight_data.battery_current,
            }

    # ------------------------------------------------------------------
    # Step 2: 样本同步
    # ------------------------------------------------------------------

    def _sync_samples(self) -> None:
        """
        将磁力计、姿态数据按时间最近邻对齐。

        优先使用 AHR2 四元数（更精确），fallback 到 ATT 欧拉角。
        """
        mag_time = self._mag_data["time"]

        if mag_time.size < 50:
            raise CoverageError("日志中磁力计数据不足（< 50 样本），无法拟合。")

        # 选择姿态源：优先 AHR2 四元数
        use_ahr2 = (self._ahr2_data is not None and self._ahr2_data["time"].size > 50)

        if use_ahr2:
            att_time = self._ahr2_data["time"]
        else:
            att_time = self._att_data["time"]

        if att_time.size < 50:
            raise CoverageError("日志中姿态数据不足（< 50 样本），无法拟合。")

        # 以磁力计时间戳为基准，用 searchsorted 对齐姿态
        idx_att = np.searchsorted(att_time, mag_time)
        idx_att = np.clip(idx_att, 1, len(att_time) - 1)

        t0 = att_time[idx_att - 1]
        t1 = att_time[idx_att]
        dt = t1 - t0
        dt = np.where(dt > 1e-9, dt, 1.0)
        w1 = np.clip((mag_time - t0) / dt, 0.0, 1.0)
        w0 = 1.0 - w1

        if use_ahr2:
            # SLERP 近似为线性插值（小角度足够精确）
            q1 = w0 * self._ahr2_data["Q1"][idx_att - 1] + w1 * self._ahr2_data["Q1"][idx_att]
            q2 = w0 * self._ahr2_data["Q2"][idx_att - 1] + w1 * self._ahr2_data["Q2"][idx_att]
            q3 = w0 * self._ahr2_data["Q3"][idx_att - 1] + w1 * self._ahr2_data["Q3"][idx_att]
            q4 = w0 * self._ahr2_data["Q4"][idx_att - 1] + w1 * self._ahr2_data["Q4"][idx_att]
            # normalize
            norms = np.sqrt(q1**2 + q2**2 + q3**2 + q4**2)
            norms = np.where(norms < 1e-9, 1.0, norms)
            q = np.stack([q1/norms, q2/norms, q3/norms, q4/norms], axis=1)

            # 从四元数提取欧拉角用于覆盖分析
            roll  = np.arctan2(2*(q1*q2 + q3*q4), 1 - 2*(q2**2 + q3**2))
            pitch = np.arcsin(np.clip(2*(q1*q3 - q4*q2), -1, 1))
            yaw   = np.arctan2(2*(q1*q4 + q2*q3), 1 - 2*(q3**2 + q4**2))
            roll  = np.rad2deg(roll)
            pitch = np.rad2deg(pitch)
            yaw   = np.rad2deg(yaw)
        else:
            # 从 ATT 欧拉角插值
            roll  = w0 * self._att_data["Roll"][idx_att - 1]  + w1 * self._att_data["Roll"][idx_att]
            pitch = w0 * self._att_data["Pitch"][idx_att - 1] + w1 * self._att_data["Pitch"][idx_att]
            yaw   = w0 * self._att_data["Yaw"][idx_att - 1]   + w1 * self._att_data["Yaw"][idx_att]

            q = self._euler_to_quat(
                np.deg2rad(yaw), np.deg2rad(pitch), np.deg2rad(roll),
            )

        self._mag_synced = np.stack([self._mag_data[k] for k in ("MagX", "MagY", "MagZ")], axis=1)
        self._quat       = q
        self._roll       = roll
        self._pitch      = pitch
        self._yaw        = yaw
        self._ofs_current = np.array([
            float(np.median(self._mag_data["OfsX"])),
            float(np.median(self._mag_data["OfsY"])),
            float(np.median(self._mag_data["OfsZ"])),
        ], dtype=np.float64)

        self._mot_current = np.array([
            float(np.median(self._mag_data["MOfsX"])),
            float(np.median(self._mag_data["MOfsY"])),
            float(np.median(self._mag_data["MOfsZ"])),
        ], dtype=np.float64)

        # 油门/电机补偿：优先用电池电流，fallback 到伪油门
        if self._bat_data is not None and self._bat_data["Curr"].size > 50:
            # 线性插值电池电流到磁力计时间
            bat_time = self._bat_data["time"]
            bat_curr = self._bat_data["Curr"]
            idx_bat = np.searchsorted(bat_time, mag_time)
            idx_bat = np.clip(idx_bat, 1, len(bat_time) - 1)
            bt0 = bat_time[idx_bat - 1]
            bt1 = bat_time[idx_bat]
            bdt = bt1 - bt0
            bdt = np.where(bdt > 1e-9, bdt, 1.0)
            bw1 = np.clip((mag_time - bt0) / bdt, 0.0, 1.0)
            throttle = (1 - bw1) * bat_curr[idx_bat - 1] + bw1 * bat_curr[idx_bat]
            # 归一化
            tmax = np.max(np.abs(throttle))
            if tmax > 0:
                throttle = throttle / tmax
            self._throttle = throttle
        else:
            # throttle fallback: ATT 伪油门 — 需要用独立 clip 防止越界
            idx_att2 = np.clip(idx_att, 1, len(self._att_data["time"]) - 1)
            throttle = np.abs(self._att_data["PitchIn"][idx_att2 - 1]) + \
                       np.abs(self._att_data["RollIn"][idx_att2 - 1])
            throttle = throttle / (np.max(throttle) + 1e-9)
            self._throttle = throttle

    # ------------------------------------------------------------------
    # Step 3: 计算期望磁场
    # ------------------------------------------------------------------

    def _compute_expected_field(self) -> None:
        """
        用 WMM/IGRF 模型计算机体坐标系期望磁场。

        body_expected = R(q)ᵀ · B_ned

        同时计算 Fibonacci lattice bin 分配和权重（对齐 WebTools）。
        """
        if self._mag_synced is None or not len(self._mag_synced):
            raise FitError("无有效样本，无法计算期望磁场。")

        # 当地地磁场 NED 向量（μT）— 使用 WMM 或 IGRF
        b_ned = earth_field_ned(self._lat, self._lon, self._alt_m)  # (3,)

        N = len(self._mag_synced)
        field_expected = np.empty((N, 3), dtype=np.float64)

        for i in range(N):
            # R(q)ᵀ · b_ned  →  body 坐标系
            field_expected[i] = ned_to_body(b_ned, self._quat[i])

        # 若当地磁场接近零（赤道附近特殊情况），使用测量值的中位数强度做归一化
        b_ned_mag = np.linalg.norm(b_ned)
        if b_ned_mag < 10.0:
            measured_mag = np.linalg.norm(self._mag_synced, axis=1)
            field_expected = field_expected * (float(np.median(measured_mag)) / (b_ned_mag + 1e-9))

        self._field_expected = field_expected

        # ── Fibonacci lattice bin 分配 ──────────────────────────
        lattice = _build_fibonacci_lattice(NUM_ATTITUDE_BINS)
        self._bin_indices = _assign_bins(field_expected, lattice)
        self._bin_weights, self._bin_coverage = _compute_bin_weights(
            self._bin_indices, NUM_ATTITUDE_BINS
        )

    # ------------------------------------------------------------------
    # Step 4: 飞行轨迹覆盖度检查
    # ------------------------------------------------------------------

    def _check_coverage(self) -> Dict[str, float]:
        """
        计算并验证飞行轨迹覆盖度。

        Returns
        -------
        Dict[str, float]
            - ``samples``: 有效样本数
            - ``yaw_coverage_deg``: 偏航角覆盖（度·样本）
            - ``yaw_range_deg``: 偏航角总范围
            - ``pitch_range_deg``: 俯仰角范围
            - ``roll_range_deg``: 横滚角范围
            - ``coverage_ok``: 是否满足最低要求
            - ``coverage_quality``: "good" | "acceptable" | "poor"
        """
        if self._mag_synced is None:
            self._sync_samples()

        N = len(self._mag_synced)
        yaw_deg   = self._yaw
        pitch_deg = self._pitch
        roll_deg  = self._roll

        # 偏航角解绕（unwrap），避免 359°→1° 跳变
        yaw_unwrap = np.unwrap(np.deg2rad(yaw_deg))
        yaw_range  = np.rad2deg(yaw_unwrap.max() - yaw_unwrap.min())

        # yaw_coverage_deg = 偏航角范围 × 样本密度（近似：范围 × 样本数 / 2π）
        yaw_coverage_deg = yaw_range

        pitch_range = float(np.ptp(pitch_deg))   # ptp = max - min
        roll_range  = float(np.ptp(roll_deg))

        tc = THRESHOLDS["flight_coverage"]
        coverage_ok = (
            N >= tc["min_samples"] and
            yaw_coverage_deg >= tc["yaw_coverage_min"] and
            pitch_range >= tc["pitch_range_min"] and
            roll_range >= tc["roll_range_min"]
        )

        if N >= tc["good_samples"] and yaw_coverage_deg >= tc["yaw_coverage_good"]:
            quality = "good"
        elif coverage_ok:
            quality = "acceptable"
        else:
            quality = "poor"

        return {
            "samples":          float(N),
            "yaw_coverage_deg": float(yaw_coverage_deg),
            "yaw_range_deg":    float(yaw_range),
            "pitch_range_deg":  float(pitch_range),
            "roll_range_deg":   float(roll_range),
            "coverage_ok":      coverage_ok,
            "coverage_quality": quality,
        }

    # ------------------------------------------------------------------
    # Step 5: 参数拟合
    # ------------------------------------------------------------------

    def _fit_parameters(self, coverage: Dict[str, float]) -> FitResult:
        """
        非线性最小二乘拟合 COMPASS 参数。

        使用 Fibonacci lattice bin 加权（对齐 WebTools），确保
        姿态覆盖不均匀时，稀疏姿态区域获得更高权重。

        参数向量 x[12]：
        [ofs_x, ofs_y, ofs_z, dia_x, dia_y, dia_z,
         odi_x, odi_y, odi_z, mot_x, mot_y, mot_z]
        """
        mag   = self._mag_synced   # (N, 3)
        B_exp = self._field_expected
        thr   = self._throttle

        # bin 权重（Fibonacci lattice 加权）
        bin_weights = getattr(self, '_bin_weights', None)
        if bin_weights is not None and len(bin_weights) == len(mag):
            # sqrt(weight) 用于残差加权（因为 least_squares 最小化 sum(r^2)）
            sqrt_w = np.sqrt(bin_weights)
            # 扩展为 (N, 3) 以匹配残差维度
            sqrt_w_3d = np.column_stack([sqrt_w, sqrt_w, sqrt_w])
        else:
            sqrt_w_3d = np.ones((len(mag), 3), dtype=np.float64)

        # x0: [OFS(3), DIA(3), ODI(3), MOT(3)] = 12 elements
        x0 = np.concatenate([
            self._ofs_current,       # 3
            np.array([1.0, 1.0, 1.0]),  # 3  DIA
            np.zeros(3),               # 3  ODI (off-diagonal coupling per axis)
            self._mot_current,       # 3
        ])  # total 12

        # 边界约束
        bounds_lo = np.array([
            -2000, -2000, -2000,   # OFS
              0.3,   0.3,   0.3,   # DIA
              -1.0,  -1.0,  -1.0,  # ODI
             -200,  -200,  -200,   # MOT
        ], dtype=np.float64)
        bounds_hi = np.array([
             2000,  2000,  2000,   # OFS
              2.0,   2.0,   2.0,   # DIA
              1.0,   1.0,   1.0,   # ODI
              200,   200,   200,   # MOT
        ], dtype=np.float64)

        def residuals(x: np.ndarray) -> np.ndarray:
            ofs = x[0:3]
            dia = x[3:6]
            odi_vec = x[6:9]   # [ODI_X, ODI_Y, ODI_Z] per-axis coupling
            mot = x[9:12]

            compensated = (mag + ofs) * dia
            compensated[:, 0] += odi_vec[1] * mag[:, 1] + odi_vec[2] * mag[:, 2]
            compensated[:, 1] += odi_vec[0] * mag[:, 0] + odi_vec[2] * mag[:, 2]
            compensated[:, 2] += odi_vec[0] * mag[:, 0] + odi_vec[1] * mag[:, 1]
            compensated += np.outer(thr, mot)

            # 加权残差（mGauss，1 μT = 10 mGauss）
            raw_residual = (compensated - B_exp) * 10.0
            return (raw_residual * sqrt_w_3d).ravel()

        try:
            sol = least_squares(
                residuals,
                x0,
                bounds=(bounds_lo, bounds_hi),
                method="trf",
                ftol=1e-8,
                xtol=1e-8,
                gtol=1e-8,
                max_nfev=2000,
                verbose=0,
            )
        except Exception as exc:
            raise FitError(f"least_squares 拟合失败: {exc}") from exc

        x_fit = sol.x
        ofs_fit  = x_fit[0:3]
        dia_fit  = x_fit[3:6]
        odi_fit  = x_fit[6:9]
        mot_fit  = x_fit[9:12]
        scale_fit = float(np.median(dia_fit))

        # 计算 fitness（未加权 RMSE — 权重仅影响拟合方向，不影响报告误差）
        raw_res = np.empty((len(mag), 3), dtype=np.float64)
        compensated = (mag + ofs_fit) * dia_fit
        compensated[:, 0] += odi_fit[1] * mag[:, 1] + odi_fit[2] * mag[:, 2]
        compensated[:, 1] += odi_fit[0] * mag[:, 0] + odi_fit[2] * mag[:, 2]
        compensated[:, 2] += odi_fit[0] * mag[:, 0] + odi_fit[1] * mag[:, 1]
        compensated += np.outer(thr, mot_fit)
        raw_res = (compensated - B_exp) * 10.0
        fitness = float(np.sqrt(np.mean(raw_res ** 2)))

        # 添加 bin 覆盖率到 coverage
        bin_cov = getattr(self, '_bin_coverage', 0.0)
        coverage["bin_coverage"] = bin_cov

        # 评估等级（优先从 KB 读取，回退到硬编码默认值）
        ft = THRESHOLDS["fitness_thresholds"]
        fitness_levels = self._kb.get("fitness_thresholds", {}).get("levels")
        if fitness_levels:
            # Pro KB 格式：[{level, max}, ...]
            for lv_data in fitness_levels:
                if "max" in lv_data and fitness < lv_data["max"]:
                    assessment = lv_data["level"]
                    break
                elif "min" in lv_data and fitness >= lv_data["min"]:
                    assessment = lv_data["level"]
            else:
                assessment = fitness_levels[-1].get("level", "BAD")
        else:
            # 硬编码回退（无 Pro KB 时使用）
            if fitness < ft["excellent"]:
                assessment = "EXCELLENT"
            elif fitness < ft["good"]:
                assessment = "GOOD"
            elif fitness < ft["acceptable"]:
                assessment = "ACCEPTABLE"
            elif fitness < ft["marginal"]:
                assessment = "MARGINAL"
            elif fitness < ft["poor"]:
                assessment = "POOR"
            else:
                assessment = "BAD"

        # 诊断警告
        warnings = self._diagnose(
            ofs_fit, dia_fit, odi_fit, mot_fit,
            fitness, coverage,
        )

        return FitResult(
            ofs=ofs_fit,
            dia=dia_fit,
            odi=odi_fit,
            mot=mot_fit,
            scale=scale_fit,
            fitness_mgauss=fitness,
            assessment=assessment,
            warnings=warnings,
            coverage=coverage,
        )

    # ------------------------------------------------------------------
    # Step 6: 诊断
    # ------------------------------------------------------------------

    def _diagnose(
        self,
        ofs: np.ndarray,
        dia: np.ndarray,
        odi: np.ndarray,
        mot: np.ndarray,
        fitness: float,
        coverage: Dict[str, float],
    ) -> List[str]:
        """
        根据知识库决策树生成诊断警告和建议。

        Parameters
        ----------
        ofs, dia, odi, mot : np.ndarray
            拟合后的参数向量。
        fitness : float
            拟合 RMS 残差（mGauss）。
        coverage : Dict[str, float]
            轨迹覆盖度结果。

        Returns
        -------
        List[str]
            诊断消息列表。
        """
        tp  = THRESHOLDS["compass_parameters"]
        ft  = THRESHOLDS["fitness_thresholds"]
        msgs: List[str] = []

        # ── Fitness 等级描述 ────────────────────────────────────
        msgs.append(f"[MAGFit] Fitness = {fitness:.3f} mGauss → {self._fitness_label(fitness)}")

        # ── OFS 检查 ────────────────────────────────────────────
        ofs_max = float(np.max(np.abs(ofs)))
        if ofs_max > tp["OFS_critical"]:
            msgs.append(f"[严重] 硬铁偏移过大：max(|OFS|) = {ofs_max:.0f} > {tp['OFS_critical']:.0f}，建议拆除机体附近的强磁性材料或外置磁力计。")
        elif ofs_max > tp["OFS_warning"]:
            msgs.append(f"[警告] 硬铁偏移偏高：max(|OFS|) = {ofs_max:.0f} > {tp['OFS_warning']:.0f}，建议检查是否存在扬声器、磁钢等干扰源。")

        # ── DIA 检查 ───────────────────────────────────────────
        for i, axis in enumerate("XYZ"):
            dev = abs(dia[i] - 1.0)
            if dev > tp["DIA_max"] - 1.0:
                msgs.append(f"[警告] {axis} 轴软铁对角线异常：DIA_{axis} = {dia[i]:.3f}，建议检查电池或磁性材料分布。")
            elif dev > tp["DIA_dev_threshold"]:
                msgs.append(f"[提示] {axis} 轴灵敏度偏差：|DIA_{axis} - 1.0| = {dev:.3f}，存在轻微软铁不对称。")

        # ── ODI 检查 ───────────────────────────────────────────
        odi_max = float(np.max(np.abs(odi)))
        if odi_max > tp["ODI_critical"]:
            msgs.append(f"[警告] 软铁非对角线过大：max(|ODI|) = {odi_max:.3f} > {tp['ODI_critical']:.3f}，常见于电池中心偏离机体中心，建议重新布局。")
        elif odi_max > tp["ODI_threshold"]:
            msgs.append(f"[提示] 软铁轴间耦合存在：max(|ODI|) = {odi_max:.3f}，电池或电机位置可能不对称。")

        # ── MOT 检查 ───────────────────────────────────────────
        mot_max = float(np.max(np.abs(mot)))
        if mot_max > tp["MOT_critical"]:
            msgs.append(f"[严重] 电机干扰补偿过大：max(|MOT|) = {mot_max:.1f} > {tp['MOT_critical']:.1f}，警告：电机挂载可能发生位移，飞行中补偿失效风险高。")
        elif mot_max > tp["MOT_warning"]:
            msgs.append(f"[警告] 电机干扰明显：max(|MOT|) = {mot_max:.1f}，建议检查电调/电机 wires 与磁力计的距离，加固挂载。")

        # ── 轨迹覆盖检查 ───────────────────────────────────────
        if not coverage["coverage_ok"]:
            msgs.append(f"[错误] 轨迹覆盖不足（{coverage['coverage_quality']}），建议按知识库第 4 节重新飞行： yaw > {THRESHOLDS['flight_coverage']['yaw_coverage_min']:.0f}°, pitch/roll > ±30°。")
        elif coverage["coverage_quality"] == "acceptable":
            msgs.append(f"[提示] 轨迹覆盖一般，建议飞行更多姿态变化以提高拟合精度（当前 yaw={coverage['yaw_coverage_deg']:.0f}°）。")

        # ── 综合建议 ───────────────────────────────────────────
        if fitness >= ft["marginal"]:
            if ofs_max > tp["OFS_warning"]:
                msgs.append("建议：优先拆除硬铁干扰源（OFS过大），再优化软铁布局，最后补偿电机干扰。")
            elif odi_max > tp["ODI_threshold"] or mot_max > tp["MOT_warning"]:
                msgs.append("建议：重新布局电池/电机，使用外置磁力计或将磁力计装在 GPS mast（> 10cm）。")

        return msgs

    def _fitness_label(self, fitness: float) -> str:
        """返回 fitness 数值对应的等级描述。"""
        ft = THRESHOLDS["fitness_thresholds"]
        if fitness < ft["excellent"]:
            return "EXCELLENT — 极佳，几乎无干扰"
        elif fitness < ft["good"]:
            return "GOOD — 良好，参数可用"
        elif fitness < ft["acceptable"]:
            return "ACCEPTABLE — 可接受"
        elif fitness < ft["marginal"]:
            return "MARGINAL — 勉强，建议优化"
        elif fitness < ft["poor"]:
            return "POOR — 较差，谨慎使用"
        else:
            return "BAD — 很差，不建议飞行"

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    @staticmethod
    def _euler_to_quat(yaw: np.ndarray, pitch: np.ndarray, roll: np.ndarray) -> np.ndarray:
        """
        欧拉角（yaw-pitch-roll）转四元数 [w, x, y, z]。

        Parameters
        ----------
        yaw, pitch, roll : np.ndarray, shape (N,)
            单位：弧度。

        Returns
        -------
        np.ndarray, shape (N, 4)
            四元数。
        """
        # 左手系（ArduPilot NED）：Z-Y-X 顺序
        cy = np.cos(yaw   * 0.5)
        sy = np.sin(yaw   * 0.5)
        cp = np.cos(pitch * 0.5)
        sp = np.sin(pitch * 0.5)
        cr = np.cos(roll  * 0.5)
        sr = np.sin(roll  * 0.5)

        qw = cr * cp * cy + sr * sp * sy
        qx = sr * cp * cy - cr * sp * sy
        qy = cr * sp * cy + sr * cp * sy
        qz = cr * cp * sy - sr * sp * cy

        # normalize 防止数值累积误差
        q_out = np.stack([qw, qx, qy, qz], axis=1)
        norms = np.linalg.norm(q_out, axis=1, keepdims=True)
        norms = np.where(norms < 1e-9, 1.0, norms)
        return q_out / norms
