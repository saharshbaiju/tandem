package io.github.saharshbaiju.tandem

import android.content.Context
import android.view.OrientationEventListener

/**
 * Tells how far the phone is turned from the way the screen is drawn now:
 * 0 (as drawn), 90 or 270 (on its side: the other orientation), or 180
 * (upside down). Tandem keeps its screen locked while streaming, so this is
 * what notices the phone being turned, for the "turn the monitor?" prompt.
 *
 * It works from the accelerometer (OrientationEventListener) plus the
 * display's rotation, so it is right on phones and tablets alike, whatever
 * way up the device's natural orientation is. A turn only counts once the
 * phone is within [SNAP] degrees of it, so holding it near 45 degrees
 * doesn't flicker between the two.
 *
 * [displayDegrees] is the display's current rotation in degrees (0, 90, 180,
 * 270); [onTurn] is called on the main thread when the turn changes.
 */
class OrientationWatcher(
    context: Context,
    private val displayDegrees: () -> Int,
    private val onTurn: (Int) -> Unit,
) : OrientationEventListener(context) {

    /** The phone's turn from the screen, in degrees clockwise; see the class doc. */
    var turn = 0
        private set

    override fun onOrientationChanged(orientation: Int) {
        if (orientation == ORIENTATION_UNKNOWN) return      // lying flat
        val next = turnFor(orientation, displayDegrees(), turn)
        if (next == turn) return
        turn = next
        onTurn(next)
    }

    /** Forget the last turn (the screen was just rotated to match it). */
    fun reset() {
        turn = 0
    }

    companion object {
        private const val SNAP = 30

        /**
         * The turn for a sensor [orientation] (degrees clockwise from the
         * natural orientation) with the display rotated [displayDegrees];
         * [previous] while the phone is between two turns.
         */
        fun turnFor(orientation: Int, displayDegrees: Int, previous: Int): Int {
            // OrientationEventListener counts from the natural orientation;
            // the display's rotation makes it count from the screen as drawn.
            val relative = (orientation + displayDegrees) % 360
            val nearest = ((relative + 45) / 90 * 90) % 360
            val off = Math.floorMod(relative - nearest + 180, 360) - 180
            return if (kotlin.math.abs(off) > SNAP) previous else nearest
        }
    }
}
