package io.github.saharshbaiju.tandem

import android.view.MotionEvent
import android.view.View

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
 * real touchscreen.
 */
class TouchForwarder(private val send: (action: Int, finger: Int, x: Float, y: Float) -> Unit) {

    /** Forward [event] (in [view]'s coordinates). */
    fun forward(view: View, event: MotionEvent) {
        val width = view.width.toFloat().takeIf { it > 0f } ?: return
        val height = view.height.toFloat().takeIf { it > 0f } ?: return

        fun sendPointer(action: Int, index: Int) =
            send(action, event.getPointerId(index), event.getX(index) / width, event.getY(index) / height)

        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN ->
                sendPointer(StreamSession.TOUCH_DOWN, event.actionIndex)
            MotionEvent.ACTION_MOVE ->
                for (i in 0 until event.pointerCount) sendPointer(StreamSession.TOUCH_MOVE, i)
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP ->
                sendPointer(StreamSession.TOUCH_UP, event.actionIndex)
            MotionEvent.ACTION_CANCEL ->
                send(StreamSession.TOUCH_CANCEL, 0, 0f, 0f)
        }
    }
}
