"""PCM energy heuristic and zero-shot direction in the shared audio/text space."""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

ENERGETIC_PROMPTS = (
    "energetic loud fast driving intense music",
    "high energy dance rock electronic beat",
    "powerful aggressive upbeat track",
)
CALM_PROMPTS = (
    "calm quiet slow ambient peaceful music",
    "low energy relaxing soft ballad",
    "gentle acoustic background music",
)


def _tempo(onsets: np.ndarray, fps: float) -> float:
    """Map a credible autocorrelation peak from 60–200 BPM onto 0–1."""
    lo = max(1, int(np.ceil(fps * 60 / 200)))
    hi = min(int(fps), onsets.size // 2)
    if hi < lo or np.count_nonzero(onsets) < 3:
        return 0.0
    x = onsets - onsets.mean()
    power = x @ x
    if power <= 0:
        return 0.0
    ac = np.array([x[:-lag] @ x[lag:] / power for lag in range(lo, hi + 1)])
    best = ac.max()
    if best < 0.25:
        return 0.0
    peaks = (ac >= np.r_[-np.inf, ac[:-1]]) & (ac >= np.r_[ac[1:], -np.inf])
    # Prefer the first near-best peak rather than a multiple of the beat period.
    lag = lo + int(np.flatnonzero(peaks & (ac >= 0.9 * best))[0])
    return float(np.clip((60 * fps / lag - 60) / 140, 0, 1))


def energy_score(wav: np.ndarray, sr: int) -> float:
    """Estimate energy in [0, 1] from mono floating-point PCM (full scale ±1).

    Combines RMS/crest density, band-limited attack rate, and rhythmic tempo.
    Silence, empty input and invalid sample rates give 0; non-finite samples are treated as zero.
    """
    if sr <= 0 or not wav.size:
        return 0.0
    pcm = np.where(np.isfinite(wav), wav, 0).astype(np.float64)
    rms = np.sqrt(np.mean(pcm * pcm))
    if rms < 1e-5:
        return 0.0
    db = 20 * np.log10(rms)
    loudness = np.clip((db + 40) / 34, 0, 1)
    crest = 20 * np.log10(np.max(np.abs(pcm)) / rms)
    compression = np.clip(1 - crest / 3, 0, 1)
    # Leave half of this component available to loud but dynamic material (e.g. drum transients).
    density = loudness * (0.52 + 0.48 * compression)

    frame, hop = max(2, round(0.04 * sr)), max(1, round(0.01 * sr))
    if pcm.size < frame + hop:
        return float(0.65 * density)
    frames = np.lib.stride_tricks.sliding_window_view(pcm, frame)[::hop]
    window = np.hanning(frame)
    power = np.abs(np.fft.rfft(frames * window, axis=-1)) ** 2
    freqs = np.fft.rfftfreq(frame, d=1 / sr)
    flux = np.zeros(len(frames) - 1)
    for low, high in ((60, 250), (2000, 8000)):
        band = np.sqrt(power[:, (freqs >= low) & (freqs <= high)].sum(axis=1)) / max(1, window.sum())
        envelope = np.log1p(band / max(1e-4, 0.02 * band.max()))
        change = np.maximum(np.diff(envelope), 0)
        median = np.median(change)
        threshold = max(0.25, median + 4 * np.median(np.abs(change - median)))
        flux = np.maximum(flux, np.maximum(change - threshold, 0))

    onsets = np.zeros_like(flux)
    refractory = max(1, round(0.07 * sr / hop))
    last = -refractory
    for i in np.flatnonzero((flux > 0) & (flux > np.r_[0, flux[:-1]]) & (flux >= np.r_[flux[1:], 0])):
        if i - last >= refractory:
            onsets[i] = flux[i]
            last = i
        elif flux[i] > onsets[last]:
            onsets[last], onsets[i] = 0, flux[i]
            last = i
    attack_rate = min(1.0, np.count_nonzero(onsets) / (4 * pcm.size / sr))
    tempo = _tempo(onsets, sr / hop)
    # Density gets most of the weight: loud compressed material can be energetic without a clear pulse.
    return float(np.clip(0.65 * density + 0.23 * attack_rate + 0.12 * tempo, 0, 1))


@dataclass(frozen=True)
class EnergyProjection:
    raw: float

    @property
    def score(self) -> float:
        """Map the known cosine range [-1, 1] to [0, 1], not a calibrated probability."""
        return (1 + self.raw) / 2


def build_energy_anchor(embed_text: Callable[[list[str]], np.ndarray]) -> np.ndarray:
    """Build the unit vector from the calm centroid towards the energetic centroid."""
    texts = [*ENERGETIC_PROMPTS, *CALM_PROMPTS]
    vectors = np.asarray(embed_text(texts), dtype=np.float64)
    if vectors.ndim != 2 or vectors.shape[0] != len(texts) or not np.isfinite(vectors).all():
        raise ValueError("invalid text embeddings for energy anchor")
    high, low = np.split(vectors, [len(ENERGETIC_PROMPTS)])
    delta = high.mean(axis=0) - low.mean(axis=0)
    norm = np.linalg.norm(delta)
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("degenerate energy anchor")
    return delta / norm


def project_energy(vector: np.ndarray, anchor: np.ndarray) -> EnergyProjection | None:
    """Project a stored audio vector onto a unit anchor; invalid vectors have no score."""
    v = np.asarray(vector, dtype=np.float64)
    if v.ndim != 1 or v.shape != anchor.shape or not np.isfinite(v).all():
        return None
    norm = np.linalg.norm(v)
    if not np.isfinite(norm) or norm <= 1e-12:
        return None
    return EnergyProjection(float(np.clip((v / norm) @ anchor, -1, 1)))
