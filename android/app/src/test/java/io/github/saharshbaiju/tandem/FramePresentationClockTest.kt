package io.github.saharshbaiju.tandem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FramePresentationClockTest {
    private val start = 1_000_000_000L
    private val period = 1_000_000_000L / 120

    @Test fun arrivalJitterDoesNotChangePresentationIntervals() {
        val clock = FramePresentationClock(120)
        val arrivals = listOf(0L, period + 1_000_000L, 2*period - 1_000_000L, 3*period + 2_000_000L)
        val deadlines = arrivals.map { clock.nextNs(start + it) }
        assertEquals(start + 3*period, deadlines.first())
        for ((a, b) in deadlines.zipWithNext()) assertEquals(period, b-a)
    }

    @Test fun cachedGopBurstCannotAccumulateAStalePresentationQueue() {
        val clock = FramePresentationClock(120)
        var previous = 0L
        repeat(60) { i ->
            val now = start + i*10_000L
            val target = clock.nextNs(now)
            assertTrue("bounded headroom", target in now+2*period..now+4*period)
            assertTrue("deadlines do not regress", target >= previous)
            previous = target
        }
    }

    @Test fun idleInputStartsAFreshSchedule() {
        val clock = FramePresentationClock(120)
        clock.nextNs(start)
        val now = start + 1_000_000_000L
        assertEquals(now + 3*period, clock.nextNs(now))
    }

    @Test fun lateInputReanchorsBeforeItsDeadlineIsTooClose() {
        val clock = FramePresentationClock(120)
        clock.nextNs(start)
        val now = start + 3*period
        assertEquals(now + 3*period, clock.nextNs(now))
    }

    @Test fun lowUnknownAndOutOfRangeRatesKeepImmediateTimestamps() {
        for (fps in listOf(-1, 0, 1, 30, 60, 89, 241)) {
            val clock = FramePresentationClock(fps)
            assertEquals(start, clock.nextNs(start))
            assertEquals(start + 1_000_000L, clock.nextNs(start + 1_000_000L))
        }
    }

    @Test fun highRefreshRangeBoundariesGetThreePeriodsOfHeadroom() {
        for (fps in listOf(90, 120, 240)) {
            assertEquals(start + 3*(1_000_000_000L/fps), FramePresentationClock(fps).nextNs(start))
        }
    }

    @Test fun aNewCodecDoesNotInheritAnOldDeadline() {
        val previous = FramePresentationClock(120)
        previous.nextNs(start + 1_000_000_000L)
        assertEquals(start + 3*period, FramePresentationClock(120).nextNs(start))
    }

    @Test fun clockDiscontinuityReanchors() {
        val clock = FramePresentationClock(120)
        clock.nextNs(start + 100_000_000L)
        assertEquals(start + 3*period, clock.nextNs(start))
    }

    @Test fun sustainedNearNominalInputKeepsTheSameGrid() {
        val clock = FramePresentationClock(120)
        val first = clock.nextNs(start)
        for (i in 1..120) {
            val now = start + i*8_338_000L
            assertEquals(first + i*period, clock.nextNs(now))
        }
    }
}
