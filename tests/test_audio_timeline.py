import random

import numpy as np

from airpy.audio import RATE, Timeline


def frames(n, start=0):
    """Stereo block with increasing values (every frame different)."""
    values = (np.arange(start, start + n) % 30000).astype(">i2")
    return np.stack((values, -values), axis=1).astype(">i2")


def test_unwritten_positions_read_as_silence():
    assert Timeline().read(0, 100) == bytes(400)


def test_roundtrip_and_silence_outside_the_written_range():
    timeline = Timeline()
    block = frames(2000)
    timeline.write(block, end_pos=50_000)
    assert timeline.read(48_000, 2000) == block.tobytes()
    assert timeline.read(48_000 - 100, 100) == bytes(400)  # before it: silence
    assert timeline.read(50_000, 100) == bytes(400)        # after it: silence


def test_contiguous_blocks_join_seamlessly():
    timeline = Timeline()
    a, b = frames(441, 0), frames(441, 441)
    timeline.write(a, end_pos=10_000 + 441)
    timeline.write(b, end_pos=10_000 + 882)
    assert timeline.read(10_000, 882) == np.concatenate((a, b)).astype(">i2").tobytes()


def test_gap_is_filled_with_silence_not_stale_audio():
    timeline = Timeline()
    timeline.write(frames(1000), end_pos=2000)
    timeline.write(frames(1000), end_pos=2000 + RATE)  # 1 s gap -> re-anchor
    assert timeline.read(2000, 1000) == bytes(4000)
    assert timeline.read(2000 + RATE - 1000, 1000) == frames(1000).tobytes()


def test_ring_buffer_wraps_around():
    timeline = Timeline(seconds=0.1)  # ~4410 frames
    position, counter = 100_000, 0
    for _ in range(40):
        timeline.write(frames(441, counter), end_pos=position + 441)
        position += 441
        counter += 441
    assert timeline.read(position - 1000, 1000) == frames(1000, counter - 1000).tobytes()


def test_clear_resets():
    timeline = Timeline()
    timeline.write(frames(500), end_pos=1000)
    timeline.clear()
    assert timeline.read(500, 500) == bytes(2000)


def test_clock_drift_is_absorbed_without_audible_jumps():
    """The timeline must stay aligned while the sound card clock drifts +-300 ppm from the system clock."""
    for ppm in (+300, -300, 0):
        timeline = Timeline()
        rng = random.Random(1)
        now, block, worst, jumps = 10_000_000.0, 441, 0.0, 0
        for i in range(6000):  # 60 sn
            now += block * (1 + ppm * 1e-6)
            before = timeline._wpos
            timeline.write(frames(block), int(now + rng.uniform(-0.5, 0.5) * block))
            if before is not None and abs(timeline._wpos - (before + block)) > 1:
                jumps += 1
            if i > 500:
                worst = max(worst, abs(timeline._error))
        assert jumps == 0, f"{ppm} ppm: re-anchored"
        assert worst < RATE * 0.005, f"{ppm} ppm: error {worst / RATE * 1000:.1f} ms"
