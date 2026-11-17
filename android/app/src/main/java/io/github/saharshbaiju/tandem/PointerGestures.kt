package io.github.saharshbaiju.tandem

import android.os.Handler
import android.os.Looper
import android.view.MotionEvent
import android.view.View
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.roundToInt

/** One PNTR frame: what pointer mode sends (linux/phone_input.py). */
data class PointerEvent(
    val action: Int, val button: Int = 0,
    val x: Float = 0f, val y: Float = 0f,
    val dx: Int = 0, val dy: Int = 0,
) {
    /** action u8, button u8, x u16, y u16, dx i16, dy i16, big-endian. */
    fun encode(): ByteArray {
        val sx = (x.coerceIn(0f, 1f) * 65535f).toInt()
        val sy = (y.coerceIn(0f, 1f) * 65535f).toInt()
        val cdx = dx.coerceIn(-32768, 32767)
        val cdy = dy.coerceIn(-32768, 32767)
        return byteArrayOf(
            action.toByte(), button.toByte(),
            (sx ushr 8).toByte(), sx.toByte(), (sy ushr 8).toByte(), sy.toByte(),
            (cdx shr 8).toByte(), cdx.toByte(), (cdy shr 8).toByte(), cdy.toByte(),
        )
    }

    companion object {
        const val MOVE = 0
        const val DOWN = 1
        const val UP = 2
        const val SCROLL = 3
        const val LEFT = 1
        const val RIGHT = 2
    }
}

/**
 * Pointer mode: the phone works as a mouse on the computer, for the many
 * Linux apps that only drag with a mouse (Chrome's tabs, window title bars,
 * text selection). The pointer goes where the finger is:
 *
 *   tap                      left click (two quick taps: double click)
 *   touch, then move         press the left button there and drag
 *   touch and hold           right click
 *   two-finger tap           right click
 *   two-finger drag          scroll (the content follows the fingers)
 *
 * A touch only becomes a click or a drag once it is clear which one it is
 * (it moved past [slopPx], it was lifted, or it was held), so a tap never
 * starts a small accidental drag. [send] gets the PNTR events in order.
 */
class PointerGestures(
    private val slopPx: Float,
    private val scrollStepPx: Float,
    private val send: (PointerEvent) -> Unit,
) {
    private enum class Mode { IDLE, PENDING, DRAGGING, HELD, TWO_FINGER, DONE }

    private val ui = Handler(Looper.getMainLooper())
    private var mode = Mode.IDLE
    private var startX = 0f; private var startY = 0f          // view pixels
    private var startFx = 0f; private var startFy = 0f        // fractions
    private var lastCx = 0f; private var lastCy = 0f          // two-finger centroid
    private var scrollRestX = 0f; private var scrollRestY = 0f
    private var twoFingerMoved = false
    private var width = 1f; private var height = 1f

    private val longPress = Runnable {
        if (mode == Mode.PENDING) {
            click(PointerEvent.RIGHT, startFx, startFy)
            mode = Mode.HELD
        }
    }

    fun onTouch(view: View, e: MotionEvent) {
        width = view.width.toFloat().coerceAtLeast(1f)
        height = view.height.toFloat().coerceAtLeast(1f)
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                startX = e.x; startY = e.y
                startFx = e.x / width; startFy = e.y / height
                mode = Mode.PENDING
                send(PointerEvent(PointerEvent.MOVE, x = startFx, y = startFy))
                ui.postDelayed(longPress, LONG_PRESS_MS)
            }
            MotionEvent.ACTION_POINTER_DOWN -> when (mode) {
                Mode.PENDING, Mode.HELD -> {
                    ui.removeCallbacks(longPress)
                    mode = Mode.TWO_FINGER
                    twoFingerMoved = false
                    scrollRestX = 0f; scrollRestY = 0f
                    centroid(e).let { (cx, cy) -> lastCx = cx; lastCy = cy }
                }
                else -> {}                     // a third finger, or mid-drag: ignored
            }
            MotionEvent.ACTION_MOVE -> onMove(e)
            MotionEvent.ACTION_POINTER_UP -> if (mode == Mode.TWO_FINGER) {
                if (!twoFingerMoved) click(PointerEvent.RIGHT, startFx, startFy)
                mode = Mode.DONE                 // until every finger is up
            }
            MotionEvent.ACTION_UP -> {
                ui.removeCallbacks(longPress)
                val fx = e.x / width; val fy = e.y / height
                when (mode) {
                    Mode.PENDING -> click(PointerEvent.LEFT, fx, fy)
                    Mode.DRAGGING -> send(PointerEvent(PointerEvent.UP, PointerEvent.LEFT, fx, fy))
                    else -> {}
                }
                mode = Mode.IDLE
            }
            MotionEvent.ACTION_CANCEL -> cancel()
        }
    }

    /** Let go of a drag (the stream stopped, the mode was switched). */
    fun cancel() {
        ui.removeCallbacks(longPress)
        if (mode == Mode.DRAGGING) send(PointerEvent(PointerEvent.UP, PointerEvent.LEFT, startFx, startFy))
        mode = Mode.IDLE
    }

    private fun onMove(e: MotionEvent) {
        val fx = e.x / width; val fy = e.y / height
        when (mode) {
            Mode.PENDING -> if (hypot(e.x - startX, e.y - startY) > slopPx) {
                ui.removeCallbacks(longPress)
                // the drag starts where the finger first touched
                send(PointerEvent(PointerEvent.DOWN, PointerEvent.LEFT, startFx, startFy))
                send(PointerEvent(PointerEvent.MOVE, x = fx, y = fy))
                startFx = fx; startFy = fy
                mode = Mode.DRAGGING
            }
            Mode.DRAGGING -> {
                startFx = fx; startFy = fy
                send(PointerEvent(PointerEvent.MOVE, x = fx, y = fy))
            }
            Mode.HELD -> send(PointerEvent(PointerEvent.MOVE, x = fx, y = fy))
            Mode.TWO_FINGER -> scrollBy(e)
            else -> {}
        }
    }

    private fun scrollBy(e: MotionEvent) {
        if (e.pointerCount < 2) return
        val (cx, cy) = centroid(e)
        // Content follows the fingers: fingers moving up scroll down.
        scrollRestX += lastCx - cx
        scrollRestY += lastCy - cy
        lastCx = cx; lastCy = cy
        if (!twoFingerMoved && hypot(scrollRestX, scrollRestY) < slopPx) return
        twoFingerMoved = true
        val dx = (scrollRestX / scrollStepPx * NOTCH).roundToInt()
        val dy = (scrollRestY / scrollStepPx * NOTCH).roundToInt()
        if (abs(dx) + abs(dy) == 0) return
        scrollRestX -= dx * scrollStepPx / NOTCH
        scrollRestY -= dy * scrollStepPx / NOTCH
        send(PointerEvent(PointerEvent.SCROLL, x = startFx, y = startFy, dx = dx, dy = dy))
    }

    private fun click(button: Int, fx: Float, fy: Float) {
        send(PointerEvent(PointerEvent.DOWN, button, fx, fy))
        send(PointerEvent(PointerEvent.UP, button, fx, fy))
    }

    private fun centroid(e: MotionEvent): Pair<Float, Float> {
        val n = e.pointerCount.coerceAtMost(2)
        var x = 0f; var y = 0f
        for (i in 0 until n) { x += e.getX(i); y += e.getY(i) }
        return x / n to y / n
    }

    private companion object {
        const val LONG_PRESS_MS = 500L
        const val NOTCH = 120
    }
}
