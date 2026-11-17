package io.github.saharshbaiju.tandem

import android.content.Context
import android.util.AttributeSet
import android.view.SurfaceView
import android.view.View
import android.view.ViewGroup.MarginLayoutParams

/**
 * A [SurfaceView] that letterboxes its content to a target aspect ratio instead
 * of stretching it to fill its bounds.
 *
 * A plain SurfaceView scales whatever the decoder renders to the full view
 * rectangle, so a 16:9 desktop shown on a 16:10 / 4:3 tablet panel comes out
 * visibly "expanded" (stretched). By overriding [onMeasure] we shrink the view
 * to the largest box of the requested aspect ratio that fits inside the parent,
 * keeping it centred (the layout's gravity handles centring). The black margins
 * become true letterbox/pillarbox bars.
 *
 * Call [setAspectRatio] whenever the real stream resolution is known (the
 * decoder reports it via its output format) so a live resolution change
 * actually re-shapes the picture.
 *
 * Display cutouts ([setSafeInsets]): when letterboxing leaves spare room, the
 * picture slides within it so a punch-hole camera doesn't cover part of the
 * desktop. It is never shrunk for that (a monitor sized to the full panel
 * would go blurry), so a picture that fills the panel stays where it is.
 */
class AspectRatioSurfaceView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = 0,
) : SurfaceView(context, attrs, defStyle) {

    // width / height of the video. 0 => unknown: fall back to filling the parent.
    private var aspectNumerator = 0
    private var aspectDenominator = 0

    // display cutout insets, in pixels of the parent
    private var safeLeft = 0
    private var safeTop = 0
    private var safeRight = 0
    private var safeBottom = 0

    /** Areas at the parent's edges to keep the picture out of, if there's room. */
    fun setSafeInsets(left: Int, top: Int, right: Int, bottom: Int) {
        if (left == safeLeft && top == safeTop && right == safeRight && bottom == safeBottom) return
        safeLeft = left; safeTop = top; safeRight = right; safeBottom = bottom
        requestLayout()
    }

    /** Set the video's pixel size; triggers a re-layout if it changed. */
    fun setAspectRatio(width: Int, height: Int) {
        if (width <= 0 || height <= 0) return
        if (width == aspectNumerator && height == aspectDenominator) return
        aspectNumerator = width
        aspectDenominator = height
        requestLayout()
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val availW = MeasureSpec.getSize(widthMeasureSpec)
        val availH = MeasureSpec.getSize(heightMeasureSpec)

        if (aspectNumerator == 0 || aspectDenominator == 0 || availW == 0 || availH == 0) {
            super.onMeasure(widthMeasureSpec, heightMeasureSpec)
            return
        }

        // Fit the video rectangle inside the available space, preserving aspect.
        // Compare cross-products to avoid float rounding: is the view wider than
        // the video (pillarbox) or taller (letterbox)?
        val viewRatioWide = availW.toLong() * aspectDenominator
        val videoRatioWide = availH.toLong() * aspectNumerator

        var w = availW
        var h = availH
        if (viewRatioWide > videoRatioWide) {
            // available area is wider than the video -> limit by height (pillarbox)
            w = (availH * aspectNumerator) / aspectDenominator
        } else {
            // available area is taller than the video -> limit by width (letterbox)
            h = (availW * aspectDenominator) / aspectNumerator
        }

        setMeasuredDimension(w, h)
    }

    override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
        super.onLayout(changed, left, top, right, bottom)
        val parentView = parent as? View ?: return
        val lp = layoutParams as? MarginLayoutParams
        translationX = offsetInto(left, width, lp?.leftMargin ?: 0,
                                  parentView.width - (lp?.rightMargin ?: 0), parentView.width,
                                  safeLeft, safeRight)
        translationY = offsetInto(top, height, lp?.topMargin ?: 0,
                                  parentView.height - (lp?.bottomMargin ?: 0), parentView.height,
                                  safeTop, safeBottom)
    }

    /**
     * Shift from where the layout put the view ([current]) so that it is
     * centred in the area between [areaStart] and [areaEnd] (the parent minus
     * this view's margins: the space above the phone's keyboard while it is
     * open), moved as little as possible to stay clear of the cutout insets.
     *
     * FrameLayout's centring doesn't do the first part by itself: it centres
     * in the whole parent and then shifts by the margins, which pushes a
     * picture with a big bottom margin off the top of the screen.
     */
    private fun offsetInto(current: Int, size: Int, areaStart: Int, areaEnd: Int, parentSize: Int,
                           startInset: Int, endInset: Int): Float {
        val centred = areaStart + (areaEnd - areaStart - size) / 2f
        val lowest = maxOf(areaStart, startInset).toFloat()
        val highest = (minOf(areaEnd, parentSize - endInset) - size).toFloat()
        val target = if (lowest > highest) centred else centred.coerceIn(lowest, highest)
        return target - current
    }
}
