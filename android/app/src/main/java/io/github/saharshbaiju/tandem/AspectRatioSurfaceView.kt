package io.github.saharshbaiju.tandem

import android.content.Context
import android.util.AttributeSet
import android.view.SurfaceView

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
 * decoder reports it via INFO_OUTPUT_FORMAT_CHANGED) so a live resolution change
 * actually re-shapes the picture.
 */
class AspectRatioSurfaceView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyle: Int = 0,
) : SurfaceView(context, attrs, defStyle) {

    // width / height of the video. 0 => unknown: fall back to filling the parent.
    private var aspectNumerator = 0
    private var aspectDenominator = 0

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
}
