package io.github.saharshbaiju.tandem

import android.view.MotionEvent
import android.view.View

/** One finger's touch event. [x]/[y] are fractions (0..1) of the video picture. */
data class TouchPoint(val action: Int, val finger: Int, val x: Float, val y: Float) {

    /** The TUCH body: action u8, finger u8, x u16, y u16, big-endian (linux/phone_input.py). */
    fun encode(): ByteArray {
        val scaledX = (x.coerceIn(0f, 1f) * COORD_SCALE).toInt()
        val scaledY = (y.coerceIn(0f, 1f) * COORD_SCALE).toInt()
        return byteArrayOf(
            action.toByte(), finger.coerceIn(0, 255).toByte(),
            (scaledX ushr 8).toByte(), scaledX.toByte(),
            (scaledY ushr 8).toByte(), scaledY.toByte(),
        )
    }

    private companion object {
        const val COORD_SCALE = 65535f
    }
}

/**
 * Turns touches on the video view into TUCH frames for the computer.
 *
 * Positions are sent as fractions of the view. The view is letterboxed to the
 * video's exact aspect ratio ([AspectRatioSurfaceView]), so a fraction of the
 * view is the same fraction of the computer's virtual monitor — no knowledge of
 * either resolution is needed here.
 *
 * Every finger is forwarded as-is (multi-touch included); the desktop apps
 * themselves interpret taps, long-presses, scrolls and pinches, just as with a
 * real touchscreen. All pointers of one MotionEvent are handed over together,
 * so they leave in one write.
 */
class TouchForwarder(private val send: (List<TouchPoint>) -> Unit) {

    /** Forward [event] (in [view]'s coordinates). */
    fun forward(view: View, event: MotionEvent) {
        val width = view.width.toFloat().takeIf { it > 0f } ?: return
        val height = view.height.toFloat().takeIf { it > 0f } ?: return

        fun point(action: Int, index: Int) =
            TouchPoint(action, event.getPointerId(index), event.getX(index) / width, event.getY(index) / height)

        val points = when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN ->
                listOf(point(StreamSession.TOUCH_DOWN, event.actionIndex))
            MotionEvent.ACTION_MOVE ->
                (0 until event.pointerCount).map { point(StreamSession.TOUCH_MOVE, it) }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP ->
                listOf(point(StreamSession.TOUCH_UP, event.actionIndex))
            MotionEvent.ACTION_CANCEL ->
                listOf(TouchPoint(StreamSession.TOUCH_CANCEL, 0, 0f, 0f))
            else -> return
        }
        send(points)
    }
}

/**
 * Touch events waiting for the sender thread, bounded and coalesced: a MOVE
 * replaces a still-unsent MOVE of the same finger (only the latest position
 * matters), while DOWN / UP / CANCEL are kept in order. So a slow link sends
 * where the finger is now, never a stale replay of where it was. Not
 * thread-safe: [StreamSession] guards it with its sender lock.
 */
class TouchQueue {
    private val pending = ArrayList<TouchPoint>()

    fun isEmpty() = pending.isEmpty()

    fun add(p: TouchPoint) {
        when (p.action) {
            StreamSession.TOUCH_MOVE -> {
                val last = pending.indexOfLast { it.finger == p.finger }
                if (last >= 0 && pending[last].action == StreamSession.TOUCH_MOVE) {
                    pending[last] = p
                    return
                }
            }
            // every finger is lifted: their pending moves no longer matter
            StreamSession.TOUCH_CANCEL -> pending.removeAll { it.action == StreamSession.TOUCH_MOVE }
        }
        pending += p
        if (pending.size > MAX_PENDING) {
            // Only reachable with a dead link and frantic tapping: drop the
            // oldest move first, the oldest event of any kind if there is none.
            val move = pending.indexOfFirst { it.action == StreamSession.TOUCH_MOVE }
            pending.removeAt(if (move >= 0) move else 0)
        }
    }

    /** Move everything pending into [frames] as TUCH frames, oldest first. */
    fun drainTo(frames: MutableList<Pair<String, ByteArray>>) {
        for (p in pending) frames += FrameWriter.TYPE_TOUCH to p.encode()
        pending.clear()
    }

    private companion object {
        const val MAX_PENDING = 64
    }
}
