package io.github.saharshbaiju.tandem

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint

/**
 * The ASCII look shared by the splash ([AsciiSplashView]) and the connect
 * screen's title ([AsciiWordmarkView]), the same as the website's: a word is
 * painted at one pixel per character cell, and each cell's brightness picks a
 * character from a density ramp.
 */
object AsciiType {
    const val WORD = "Tandem"
    private const val RAMP = " .:-=+*#%@"

    /** The character for brightness [b] (0 = blank, 1 = densest). */
    fun charFor(b: Float) = RAMP[(b * RAMP.length).toInt().coerceIn(0, RAMP.length - 1)]

    /*
     * Both fonts ship inside the app (res/font, subset to the characters drawn
     * here), never the phone's own: a row of characters is drawn in one call,
     * so the grid only lines up if every character has the same width, and
     * phone makers replace "monospace" (AOSP has no bold one) and drop the
     * "sans-serif-black" alias. On a Moto One that broke the splash and the
     * title. Geist Mono Bold and Archivo Black are the website's families (OFL).
     */

    /** Monospace paint for the characters themselves (Geist Mono Bold). */
    fun glyphPaint(context: Context, color: Int) = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        this.color = color
        typeface = context.resources.getFont(R.font.tandem_ascii_mono)
    }

    /** The wordmark's letterforms: heavy and a little wide, like Archivo on the site. */
    fun wordPaint(context: Context) = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        typeface = context.resources.getFont(R.font.tandem_wordmark)
        textScaleX = 1.15f
        textAlign = Paint.Align.CENTER
    }

    /**
     * Brightness per cell (row-major, 0..1) of whatever [draw] paints in pixel
     * space, sampled on a [cols] × [rows] grid of [cellW] × [cellH] px cells,
     * so the letters keep their proportions whatever the cell shape.
     */
    fun rasterize(cols: Int, rows: Int, cellW: Float, cellH: Float, draw: (Canvas) -> Unit): FloatArray {
        val bmp = Bitmap.createBitmap(cols, rows, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        c.drawColor(Color.BLACK)
        c.scale(1f / cellW, 1f / cellH)
        draw(c)
        val px = IntArray(cols * rows)
        bmp.getPixels(px, 0, cols, 0, 0, cols, rows)
        bmp.recycle()
        return FloatArray(px.size) { Color.red(px[it]) / 255f }
    }
}
