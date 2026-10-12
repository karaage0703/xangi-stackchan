"""Execute the firmware's frame selection with queued/in-flight fake frames."""

import subprocess
from pathlib import Path


def test_fresh_capture_ownership_and_stale_queue(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "fresh_capture.cpp"
    source.write_text(r"""
#include <cassert>
#include <sys/time.h>
#include "fresh_capture.h"
struct Frame { timeval timestamp; };
int main() {
    Frame frames[] = {{{1, 0}}, {{2, 0}}, {{3, 99}}, {{3, 101}}};
    unsigned discarded = 99;
    int gets = 0, releases = 0;
    auto release = [&](Frame* f) {
        assert(f == &frames[releases]);
        ++releases;
    };
    // Two buffered frames plus an in-flight frame begun before the request.
    auto* fresh = getFreshCameraFrame(3000100,
        [&]() { return &frames[gets++]; }, release, discarded);
    assert(fresh == &frames[3]);
    assert(gets == 4 && releases == 3 && discarded == 3);
    // The accepted frame remains owned by the caller until encoding finishes.
    release(fresh);
    assert(releases == 4);

    // A frame beginning exactly at the request time is valid.
    gets = releases = 0;
    fresh = getFreshCameraFrame(1000000,
        [&]() { ++gets; return &frames[0]; }, release, discarded);
    assert(fresh == &frames[0] && discarded == 0 && releases == 0);

    // Acquisition failure after a stale frame does not double-return a buffer.
    gets = releases = 0;
    fresh = getFreshCameraFrame(3000100,
        [&]() -> Frame* { return gets++ == 0 ? &frames[0] : nullptr; },
        release, discarded);
    assert(!fresh && gets == 2 && releases == 1 && discarded == 1);

    // Bad timestamps terminate instead of spinning or returning stale pixels.
    Frame invalid{{0, 0}};
    gets = releases = 0;
    fresh = getFreshCameraFrame(3000100,
        [&]() { ++gets; return &invalid; },
        [&](Frame* f) { assert(f == &invalid); ++releases; }, discarded);
    assert(!fresh && gets == 8 && releases == 8 && discarded == 8);

    // Timestamp arithmetic remains valid beyond 32-bit microsecond rollover.
    Frame later{{5000, 100}};
    fresh = getFreshCameraFrame(INT64_C(5000000000),
        [&]() { return &later; }, [&](Frame*) { assert(false); }, discarded);
    assert(fresh == &later && discarded == 0);
}
""")
    binary = tmp_path / "fresh_capture"
    subprocess.run(
        [
            "c++",
            "-std=c++11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(root / "firmware/examples/cores3/main"),
            str(source),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
