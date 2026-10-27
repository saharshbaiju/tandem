package io.github.saharshbaiju.tandem

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface

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

    /** Monospace paint for the characters themselves. */
    fun glyphPaint(color: Int) = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        this.color = color
        typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
    }

    /** The wordmark's letterforms: heavy and a little wide, like Archivo on the site. */
    fun wordPaint() = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        typeface = Typeface.create("sans-serif-black", Typeface.NORMAL)
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
