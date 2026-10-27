package io.github.saharshbaiju.tandem

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.TimeAnimator
import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RenderEffect
import android.graphics.RuntimeShader
import android.os.Build
import android.util.AttributeSet
import android.view.View
import android.view.animation.PathInterpolator
import android.widget.FrameLayout
import androidx.annotation.RequiresApi
import kotlin.math.exp
import kotlin.math.hypot
import kotlin.math.max

/**
 * The connect screen's container, able to "dissolve like water" into the live
 * desktop underneath it (the video SurfaceView sits behind this view).
 *
 *  1. [startRipple] — waves spread from the tap point and keep pulsing, like
 *     drops on water, while we connect.
 *  2. [reveal] — once the first frame is decoded, a hole with a rippling rim
 *     opens from the same point and swallows the whole screen, uncovering the
 *     extended display. Then the view hides itself.
 *  3. [settle] — connection failed: the waves calm down, nothing is revealed.
 *
 * Android 13+: a real AGSL RuntimeShader displaces the UI's pixels along the
 * wave (refraction) and cuts the hole. Older versions: expanding rings drawn on
 * the canvas plus a clipped circular reveal.
 */
class RippleRevealLayout @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : FrameLayout(context, attrs) {

    private val density = resources.displayMetrics.density
    private var originX = 0f
    private var originY = 0f
    private var elapsed = 0f          // seconds since the ripple started
    private var amp = 0f              // wave amplitude, px
    private var revealRadius = 0f     // hole radius, px (0 = closed)
    private var calming = false
    private var ticker: TimeAnimator? = null
    private var revealAnim: ValueAnimator? = null

    private val shader: Any? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) RuntimeShader(AGSL) else null

    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = 0xFFFFFFFF.toInt()
    }
    private val hole = Path()

    init {
        setWillNotDraw(false)
    }

    val isRippling: Boolean get() = ticker != null

    fun startRipple(x: Float, y: Float) {
        stopAll()
        visibility = View.VISIBLE
        alpha = 1f
        originX = x; originY = y
        elapsed = 0f; revealRadius = 0f; calming = false
        amp = MAX_AMP_DP * density
        ticker = TimeAnimator().apply {
            setTimeListener { _, total, delta ->
                elapsed = total / 1000f
                if (calming) {
                    amp *= exp(-delta / 220f)
                    if (amp < 0.3f) { stopAll(); return@setTimeListener }
                }
                applyFrame()
            }
            start()
        }
    }

    /** Open the hole and uncover what's behind; [onDone] runs when fully gone. */
    fun reveal(onDone: () -> Unit) {
        if (ticker == null) startRipple(width / 2f, height / 2f)
        calming = false
        val far = listOf(0f to 0f, width.toFloat() to 0f, 0f to height.toFloat(),
            width.toFloat() to height.toFloat())
            .maxOf { (cx, cy) -> hypot(cx - originX, cy - originY) } + EDGE_DP * 3 * density
        revealAnim?.cancel()
        revealAnim = ValueAnimator.ofFloat(0f, far).apply {
            duration = 900
            interpolator = PathInterpolator(0.3f, 0f, 0.1f, 1f)   // emphasized decelerate
            addUpdateListener { revealRadius = it.animatedValue as Float }
            addListener(object : AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: Animator) {
                    stopAll()
                    visibility = View.GONE
                    onDone()
                }
            })
            start()
        }
    }

    /** Let the waves die out without revealing anything (connect failed). */
    fun settle() {
        revealAnim?.cancel()
        revealRadius = 0f
        calming = true
    }

    /** Back to a plain, fully visible connect screen. */
    fun reset() {
        stopAll()
        visibility = View.VISIBLE
    }

    private fun stopAll() {
        ticker?.cancel(); ticker = null
        revealAnim?.removeAllListeners(); revealAnim?.cancel(); revealAnim = null
        amp = 0f; revealRadius = 0f; calming = false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) setRenderEffect(null)
        invalidate()
    }

    private fun applyFrame() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) applyShader()
        else invalidate()
    }

    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private fun applyShader() {
        val s = shader as RuntimeShader
        s.setFloatUniform("origin", originX, originY)
        s.setFloatUniform("size", width.toFloat(), height.toFloat())
        s.setFloatUniform("time", elapsed)
        s.setFloatUniform("amp", amp)
        s.setFloatUniform("reveal", revealRadius)
        s.setFloatUniform("edge", EDGE_DP * density)
        s.setFloatUniform("density", density)
        setRenderEffect(RenderEffect.createRuntimeShaderEffect(s, "content"))
    }

    // ------------------------------------------------- pre-Android 13 fallback

    override fun dispatchDraw(canvas: Canvas) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU || ticker == null) {
            super.dispatchDraw(canvas)
            return
        }
        val save = canvas.save()
        if (revealRadius > 0f) {
            hole.reset()
            hole.addCircle(originX, originY, revealRadius, Path.Direction.CW)
            canvas.clipOutPath(hole)
        }
        super.dispatchDraw(canvas)
        canvas.restoreToCount(save)

        // Concentric rings travelling outward, re-emitted every PERIOD seconds.
        val speed = WAVE_SPEED_DP * density
        val strength = amp / (MAX_AMP_DP * density)
        for (i in 0 until 3) {
            val since = elapsed - i * PERIOD / 3f
            if (since < 0f) continue
            val t = since % PERIOD
            val r = t * speed
            val a = (1f - t / PERIOD) * strength
            ringPaint.strokeWidth = (3f + 5f * a) * density
            ringPaint.alpha = (a * 90).toInt().coerceIn(0, 255)
            canvas.drawCircle(originX, originY, max(r, revealRadius), ringPaint)
        }
        if (revealRadius > 0f) {
            ringPaint.strokeWidth = 6f * density
            ringPaint.alpha = 110
            canvas.drawCircle(originX, originY, revealRadius, ringPaint)
        }
    }

    companion object {
        private const val MAX_AMP_DP = 7f       // peak displacement of the waves
        private const val EDGE_DP = 22f         // softness of the reveal rim
        private const val WAVE_SPEED_DP = 520f  // dp per second
        private const val PERIOD = 1.6f         // seconds between "drops"

        /**
         * Water: each drop emits a decaying wave train behind its front. Pixels
         * are sampled from a point displaced along the radius (refraction) and
         * crests get a soft highlight. The reveal cuts a hole whose rim wobbles
         * with the waves; transparent pixels show the video underneath.
         */
        private const val AGSL = """
            uniform shader content;
            uniform float2 origin;
            uniform float2 size;
            uniform float time;
            uniform float amp;
            uniform float reveal;
            uniform float edge;
            uniform float density;

            float wave(float dist, float t) {
                float front = t * 520.0 * density;
                if (dist > front) return 0.0;
                float x = (front - dist) / density;          // dp behind the front
                return sin(x * 0.11) * exp(-x * 0.012) * exp(-t * 0.9);
            }

            half4 main(float2 p) {
                float2 d = p - origin;
                float dist = length(d);
                float2 dir = dist > 0.001 ? d / dist : float2(0.0, 0.0);

                // three drops, staggered, repeating every 1.6 s
                float w = 0.0;
                for (int i = 0; i < 3; i++) {
                    float t = time - float(i) * 0.5333;
                    if (t > 0.0) w += wave(dist, mod(t, 1.6));
                }
                float falloff = exp(-dist / (900.0 * density));
                float disp = w * amp * falloff;
                float2 q = clamp(p - dir * disp, float2(0.0), size - 1.0);
                half4 c = content.eval(q);
                c.rgb += half3(max(w, 0.0) * 0.07 * (amp / (7.0 * density))) * c.a;

                if (reveal > 0.0) {
                    float wob = sin(atan(d.y, d.x) * 7.0 + time * 6.0) * 5.0 * density
                              + w * 9.0 * density;
                    float e = reveal + wob;
                    float a = smoothstep(e - 1.0, e + edge, dist);
                    float rim = exp(-pow((dist - e - edge * 0.5) / (6.0 * density), 2.0));
                    c *= a;
                    c.rgb += half3(rim * 0.45 * a);
                }
                return c;
            }
        """
    }
}
