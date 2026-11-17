package io.github.saharshbaiju.tandem

import org.junit.Assert.assertEquals
import org.junit.Test

/** The turn maths behind the "turn the monitor too?" prompt. */
class OrientationWatcherTest {
    private fun turn(sensor: Int, display: Int, previous: Int = 0) =
        OrientationWatcher.turnFor(sensor, display, previous)

    @Test fun phoneHeldTheWayTheScreenIsDrawnIsNoTurn() {
        assertEquals(0, turn(270, 90))      // landscape app, phone in landscape
        assertEquals(0, turn(90, 270))      // the other landscape
        assertEquals(0, turn(0, 0))         // portrait app, phone upright
    }

    @Test fun landscapeAppPhoneHeldUprightIsAQuarterTurn() {
        assertEquals(90, turn(0, 90))
        assertEquals(270, turn(0, 270))
    }

    @Test fun portraitAppPhoneOnItsSideIsAQuarterTurn() {
        assertEquals(90, turn(90, 0))
        assertEquals(270, turn(270, 0))
    }

    @Test fun nearlyThereCountsAndHalfwayKeepsThePreviousTurn() {
        assertEquals(90, turn(20, 90))      // 110: within 30 of 90
        assertEquals(0, turn(320, 90, previous = 0))    // 50: halfway, keep
        assertEquals(90, turn(320, 90, previous = 90))
    }

    @Test fun wrapsAround() {
        assertEquals(0, turn(355, 0))
        assertEquals(0, turn(268, 90))
    }
}
