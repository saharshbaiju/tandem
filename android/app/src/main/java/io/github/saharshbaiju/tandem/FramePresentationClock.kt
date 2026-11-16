package io.github.saharshbaiju.tandem

/**
 * Bounded presentation headroom for high-refresh input. Feeder-thread only.
 *
 * Regular deadlines absorb arrival/decode jitter without queuing an entire
 * replayed GOP into the future. Idle or late input starts a fresh schedule.
 * Lower rates retain immediate presentation to avoid adding their longer periods.
 */
internal class FramePresentationClock(fps: Int) {
    private val periodNs = if (fps in 90..240) 1_000_000_000L / fps else 0L
    private var lastArrivalNs = 0L
    private var lastTargetNs = 0L

    fun nextNs(now: Long): Long {
        if (periodNs == 0L) return now
        val candidate = lastTargetNs + periodNs
        val reset = lastTargetNs == 0L || now < lastArrivalNs ||
            now - lastArrivalNs > 4 * periodNs || candidate < now + 2 * periodNs
        val target = if (reset) now + 3 * periodNs else minOf(candidate, now + 4 * periodNs)
        lastArrivalNs = now
        lastTargetNs = target
        return target
    }
}
