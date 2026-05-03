"""
滤波器可视化模块。

实现 Bode Plot（幅度+相位）和滤波前后对比图。
"""

from typing import Dict, Any, Optional, Tuple
import numpy as np


def generate_bode_plot_data(
    freqs: np.ndarray,
    sample_rate: float,
    gyro_filter_hz: float = 0.0,
    notch_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    生成 Bode Plot 数据。

    Parameters
    ----------
    freqs : np.ndarray
        频率数组（Hz）。
    sample_rate : float
        采样率（Hz）。
    gyro_filter_hz : float
        LPF 截止频率。
    notch_params : Optional[Dict[str, Any]]
        陷波滤波器参数。

    Returns
    -------
    Dict[str, Any]
        包含 freqs, magnitude_db, phase_deg 的字典。
    """
    from smarttune.analyzers.filter_transfer import compute_filter_response

    mag_db, phase_deg = compute_filter_response(
        freqs, sample_rate, gyro_filter_hz, notch_params
    )

    # 相位展开
    phase_unwrapped = np.unwrap(np.deg2rad(phase_deg))
    phase_deg = np.rad2deg(phase_unwrapped)

    return {
        "freqs": freqs.tolist(),
        "magnitude_db": mag_db.tolist(),
        "phase_deg": phase_deg.tolist(),
    }


def generate_filter_comparison_data(
    freqs: np.ndarray,
    magnitudes_db: np.ndarray,
    sample_rate: float,
    current_params: Dict[str, float],
    recommended_params: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    生成滤波前后对比数据。

    Parameters
    ----------
    freqs : np.ndarray
        频率数组（Hz）。
    magnitudes_db : np.ndarray
        原始幅度谱（dB）。
    sample_rate : float
        采样率。
    current_params : Dict[str, float]
        当前滤波器参数（GYRO_FILTER, NOTCH_FREQ, NOTCH_BW 等）。
    recommended_params : Optional[Dict[str, float]]
        推荐滤波器参数（可选）。

    Returns
    -------
    Dict[str, Any]
        包含原始谱、当前滤波后、推荐滤波后的数据。
    """
    from smarttune.analyzers.filter_transfer import simulate_filtered_spectrum

    # 当前滤波器效果
    current_notch = None
    if current_params.get("notch_freq", 0) > 0:
        current_notch = {
            "center_hz": current_params.get("notch_freq", 0),
            "bandwidth_hz": current_params.get("notch_bw", 0),
            "attenuation_db": current_params.get("notch_att", 10),
            "harmonics": current_params.get("harmonics", 3),
        }

    current_filtered = simulate_filtered_spectrum(
        freqs,
        magnitudes_db,
        sample_rate,
        current_params.get("gyro_filter", 0),
        current_notch,
    )

    result = {
        "freqs": freqs.tolist(),
        "original": magnitudes_db.tolist(),
        "current_filtered": current_filtered.tolist(),
    }

    # 推荐滤波器效果
    if recommended_params:
        rec_notch = None
        if recommended_params.get("notch_freq", 0) > 0:
            rec_notch = {
                "center_hz": recommended_params.get("notch_freq", 0),
                "bandwidth_hz": recommended_params.get("notch_bw", 0),
                "attenuation_db": recommended_params.get("notch_att", 10),
                "harmonics": recommended_params.get("harmonics", 3),
            }

        rec_filtered = simulate_filtered_spectrum(
            freqs,
            magnitudes_db,
            sample_rate,
            recommended_params.get("gyro_filter", 0),
            rec_notch,
        )
        result["recommended_filtered"] = rec_filtered.tolist()

    return result


def plot_bode(
    freqs: np.ndarray,
    magnitude_db: np.ndarray,
    phase_deg: np.ndarray,
    output_path: str,
    title: str = "Filter Bode Plot",
) -> str:
    """
    绘制 Bode Plot（幅度+相位）。

    Parameters
    ----------
    freqs : np.ndarray
        频率数组（Hz）。
    magnitude_db : np.ndarray
        幅度（dB）。
    phase_deg : np.ndarray
        相位（度）。
    output_path : str
        输出路径。
    title : str
        图表标题。

    Returns
    -------
    str
        保存的图片路径。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: F401
    except Exception:
        # Catches ImportError, AttributeError (numpy ABI mismatch), etc.
        return ""

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)

    # 幅度图
    ax1.plot(freqs, magnitude_db, "b-", linewidth=1.2)
    ax1.set_ylabel("Magnitude (dB)")
    ax1.set_title(title)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(-60, 5)

    # 关键频率线
    ax1.axhline(-3, color="r", linestyle="--", alpha=0.5, label="-3dB")

    # 相位图
    ax2.plot(freqs, phase_deg, "g-", linewidth=1.2)
    ax2.set_xlabel("Frequency (Hz)")
    ax2.set_ylabel("Phase (deg)")
    ax2.grid(True, alpha=0.3)

    # 相位范围阴影
    ax2.axhspan(-45, 45, alpha=0.1, color="green", label="±45°")

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path


def plot_filter_comparison(
    freqs: np.ndarray,
    original_db: np.ndarray,
    current_filtered_db: np.ndarray,
    recommended_filtered_db: Optional[np.ndarray] = None,
    output_path: str = "output/filter_comparison.png",
    title: str = "Filter Comparison",
) -> str:
    """
    绘制滤波前后对比图。

    Parameters
    ----------
    freqs : np.ndarray
        频率数组（Hz）。
    original_db : np.ndarray
        原始幅度谱（dB）。
    current_filtered_db : np.ndarray
        当前滤波后幅度谱（dB）。
    recommended_filtered_db : Optional[np.ndarray]
        推荐滤波后幅度谱（dB）。
    output_path : str
        输出路径。
    title : str
        图表标题。

    Returns
    -------
    str
        保存的图片路径。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: F401
    except Exception:
        return ""

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.plot(freqs, original_db, "b-", linewidth=1.0, alpha=0.7, label="Original")
    ax.plot(freqs, current_filtered_db, "r-", linewidth=1.2, label="Current Filtered")

    if recommended_filtered_db is not None:
        ax.plot(freqs, recommended_filtered_db, "g--", linewidth=1.2, label="Recommended")

    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Magnitude (dB)")
    ax.set_title(title)
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path

def plot_spectrogram(
    time_data: np.ndarray,
    freq_data: np.ndarray,
    intensity_db: np.ndarray,
    output_path: str = "output/spectrogram.png",
    title: str = "Spectrogram",
    tracking_freq: Optional[np.ndarray] = None,
) -> str:
    """
    绘制时频谱图（Spectrogram）。

    Parameters
    ----------
    time_data : np.ndarray
        时间数组（秒）。
    freq_data : np.ndarray
        频率数组（Hz）。
    intensity_db : np.ndarray
        强度矩阵（dB），shape = (n_freq, n_time)。
    output_path : str
        输出路径。
    title : str
        图表标题。
    tracking_freq : Optional[np.ndarray]
        跟踪频率曲线。

    Returns
    -------
    str
        保存的图片路径。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: F401
    except Exception:
        return ""

    fig, ax = plt.subplots(figsize=(14, 6))

    im = ax.pcolormesh(time_data, freq_data, intensity_db, shading="auto", cmap="viridis")
    plt.colorbar(im, ax=ax, label="Magnitude (dB)")

    if tracking_freq is not None and len(tracking_freq) == len(time_data):
        ax.plot(time_data, tracking_freq, "r-", linewidth=1.5, label="Notch Tracking")
        ax.legend(loc="upper right")

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_title(title)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close(fig)

    return output_path


def compute_spectrogram_data(
    signal: np.ndarray,
    sample_rate: float,
    window_size: int = 256,
    overlap: float = 0.5,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    计算时频谱数据。

    Parameters
    ----------
    signal : np.ndarray
        时域信号。
    sample_rate : float
        采样率。
    window_size : int
        FFT 窗口大小。
    overlap : float
        窗口重叠比例。

    Returns
    -------
    Tuple[time_arr, freq_arr, intensity_db]
    """
    from scipy import signal as sp_signal

    hop = int(window_size * (1 - overlap))
    f, t, Zxx = sp_signal.stft(
        signal,
        fs=sample_rate,
        nperseg=window_size,
        noverlap=window_size - hop,
    )

    intensity_db = 20 * np.log10(np.abs(Zxx) + 1e-9)

    return t, f, intensity_db
