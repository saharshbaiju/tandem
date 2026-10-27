package io.github.saharshbaiju.tandem

import android.animation.TimeAnimator
import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.provider.Settings
import android.util.AttributeSet
import android.view.MotionEvent
import android.view.View
import android.view.animation.PathInterpolator
import kotlin.math.exp
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Launch splash, the same as the website's: the "Tandem" wordmark in ASCII
 * characters on black. Drops spread from the centre (or wherever you tap) and
 * bend the letters like water, then a hole with a bright rim opens and
 * uncovers the connect screen. Then the view hides itself.
 *
 * A port of the website's `Splash` (website/index.html), whose wave maths
 * comes from [RippleRevealLayout]'s shader. Distances are in dp, so it looks
 * the same on every screen density.
 *
 * Text is drawn a row at a time in a monospace font, so a frame is a few
 * dozen drawText calls, not one per character.
 */
class AsciiSplashView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private val density = resources.displayMetrics.density

    private val textPaint = AsciiType.glyphPaint(Color.WHITE)
    private val bgPaint = Paint().apply { color = Color.BLACK }
    private val hole = Path().apply { fillType = Path.FillType.EVEN_ODD }

    // Character grid; one wordmark brightness sample per cell.
    private var cellW = 0f
    private var cellH = 0f
    private var cols = 0
    private var rows = 0
    private var word = FloatArray(0)
    private var line = CharArray(0)

    private enum class Phase { WAITING, RIPPLING, DONE }

    private var phase = Phase.DONE
    private var rippleStart = 0L       // uptime ms
    private var originX = 0f
    private var originY = 0f
    private var onDone: (() -> Unit)? = null
    private var ticker: TimeAnimator? = null
    private val autoStart = Runnable { startRipple(width / 2f, height / 2f) }

    /**
     * Show the splash over everything, then call [done] once it has opened.
     * Skipped (straight to [done]) when the system has animations turned off.
     */
    fun run(done: () -> Unit) {
        onDone = done
        if (animationsOff()) return finish()
        visibility = VISIBLE
        phase = Phase.WAITING
        ticker = TimeAnimator().apply {
            setTimeListener { _, _, _ -> if (phase == Phase.RIPPLING) invalidate() }
            start()
        }
        postDelayed(autoStart, AUTO_START_MS)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        if (w > 0 && h > 0) layoutGrid(w, h)
    }

    /** Size the character grid to the view and paint the wordmark into it. */
    private fun layoutGrid(w: Int, h: Int) {
        val phone = min(w, h) / density < 600
        textPaint.textSize = (if (phone) 9f else 13f) * density
        cellW = textPaint.measureText("M")
        cellH = textPaint.textSize * 1.12f
        cols = max(1, (w / cellW).toInt())
        rows = max(1, (h / cellH).toInt())
        line = CharArray(cols)

        // The wordmark at one pixel per cell, like the website's offscreen canvas.
        val p = AsciiType.wordPaint().apply { textSize = min(w * 0.26f, h * 0.32f) }
        p.textSize *= min(1f, w * (if (phone) 0.88f else 0.78f) / p.measureText(AsciiType.WORD))
        val baseline = h / 2f - p.textSize * 0.1f - (p.ascent() + p.descent()) / 2f
        word = AsciiType.rasterize(cols, rows, cellW, cellH) {
            it.drawText(AsciiType.WORD, w / 2f, baseline, p)
        }
    }

    private fun startRipple(x: Float, y: Float) {
        if (phase != Phase.WAITING) return
        removeCallbacks(autoStart)
        phase = Phase.RIPPLING
        originX = x
        originY = y
        rippleStart = android.os.SystemClock.uptimeMillis()
    }

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        if (phase == Phase.DONE) return false
        if (e.actionMasked == MotionEvent.ACTION_DOWN) startRipple(e.x, e.y)
        return true                                    // nothing underneath until it opens
    }

    override fun onDraw(canvas: Canvas) {
        if (phase == Phase.DONE || cols == 0) return
        val t = if (phase == Phase.WAITING) 0f
                else (android.os.SystemClock.uptimeMillis() - rippleStart) / 1000f

        var radius = 0f
        if (t >= REVEAL_AT_S) {
            val k = (t - REVEAL_AT_S) / REVEAL_S
            if (k >= 1f) return finish()
            val far = maxOf(
                hypot(originX, originY), hypot(width - originX, originY),
                hypot(originX, height - originY), hypot(width - originX, height - originY),
            ) + 80 * density
            radius = max(1f, EASE_REVEAL.getInterpolation(k) * far)
        }

        // the black surface everywhere except the hole
        if (radius > 0f) {
            hole.rewind()
            hole.addRect(0f, 0f, width.toFloat(), height.toFloat(), Path.Direction.CW)
            hole.addCircle(originX, originY, radius, Path.Direction.CW)
            canvas.drawPath(hole, bgPaint)
        } else canvas.drawPaint(bgPaint)

        val baseline = -textPaint.ascent() + cellH * 0.06f
        for (r in 0 until rows) {
            for (c in 0 until cols) line[c] = AsciiType.charFor(brightness(c, r, t, radius))
            canvas.drawText(line, 0, cols, 0f, r * cellH + baseline, textPaint)
        }
    }

    /** One cell: the wordmark refracted by the waves, crests, and the hole's rim. */
    private fun brightness(c: Int, r: Int, t: Float, radius: Float): Float {
        val px = (c + 0.5f) * cellW
        val py = (r + 0.5f) * cellH
        val dx = px - originX
        val dy = py - originY
        val dist = hypot(dx, dy)
        if (radius > 0f && dist < radius) return 0f

        var w = 0f
        if (t > 0f) for (i in 0 until 3) {
            val ti = t - i * TRAIN_GAP_S
            if (ti > 0f) w += wave(dist / density, ti % TRAIN_PERIOD_S)
        }
        // refraction: sample the wordmark from a point pushed along the radius
        val disp = w * AMP_DP * 2.2f * density
        val ux = if (dist > 0f) dx / dist else 0f
        val uy = if (dist > 0f) dy / dist else 0f
        val sc = ((px - ux * disp) / cellW - 0.5f).roundToInt().coerceIn(0, cols - 1)
        val sr = ((py - uy * disp) / cellH - 0.5f).roundToInt().coerceIn(0, rows - 1)
        var b = word[sr * cols + sc]
        b = max(b, max(w, 0f) * 0.55f)                               // crests show as characters
        if (radius > 0f) {
            val rim = (dist - radius - 10 * density) / density
            b = max(b, exp(-rim * rim / 120f))                        // bright rim
        }
        return b
    }

    private fun finish() {
        phase = Phase.DONE
        removeCallbacks(autoStart)
        ticker?.cancel()
        ticker = null
        visibility = GONE
        onDone?.invoke()
        onDone = null
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        removeCallbacks(autoStart)
        ticker?.cancel()
    }

    private fun animationsOff() = Settings.Global.getFloat(
        context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f

    companion object {
        // Timings (shorter than the website's: an app should get out of the way).
        private const val AUTO_START_MS = 450L      // ripple by itself if not tapped
        private const val REVEAL_AT_S = 0.8f        // drops breathe, then the water opens
        private const val REVEAL_S = 0.7f
        private const val AMP_DP = 7f
        private const val TRAIN_GAP_S = 0.5333f     // three wave trains, staggered
        private const val TRAIN_PERIOD_S = 1.6f

        private val EASE_REVEAL = PathInterpolator(0.3f, 0f, 0.1f, 1f)

        /** One expanding wave train (RippleRevealLayout's wave()); front ~520 dp/s. */
        private fun wave(distDp: Float, t: Float): Float {
            val front = t * 520f
            if (distDp > front) return 0f
            val x = front - distDp
            return sin(x * 0.11f) * exp(-x * 0.012f) * exp(-t * 0.9f)
        }
    }
}
