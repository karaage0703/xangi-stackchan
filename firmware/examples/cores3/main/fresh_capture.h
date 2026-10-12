#pragma once

#include <stdint.h>

// A queued frame (including one already being acquired) can predate CAPTURE.
// Compare the driver's first-DMA timestamp with the request's monotonic time.
// Bound retries even if a sensor/driver keeps returning invalid timestamps.
template <typename Get, typename Release>
auto getFreshCameraFrame(int64_t requested_us, Get get, Release release,
                         unsigned& discarded) -> decltype(get()) {
    discarded = 0;
    for (unsigned attempt = 0; attempt < 8; ++attempt) {
        auto* fb = get();
        if (!fb) return nullptr;
        const int64_t frame_us = int64_t(fb->timestamp.tv_sec) * 1000000
                               + fb->timestamp.tv_usec;
        if (frame_us > 0 && frame_us >= requested_us) return fb;
        release(fb);
        ++discarded;
    }
    return nullptr;
}
