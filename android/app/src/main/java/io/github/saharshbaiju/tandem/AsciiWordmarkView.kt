package io.github.saharshbaiju.tandem

import android.content.Context
import android.graphics.Canvas
import android.graphics.Rect
import android.util.AttributeSet
import android.view.View
import com.google.android.material.color.MaterialColors
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min

/**
 * The connect screen's title: "Tandem" in ASCII characters, the same look as
 * the splash and the website. A fixed number of character rows tall; as wide
 * as the word needs, narrower (smaller letters) if the column is narrower.
 * Static, so the characters are worked out once per size, not per frame.
 */
class AsciiWordmarkView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    private val density = resources.displayMetrics.density
    private val glyphs = AsciiType.glyphPaint(
        MaterialColors.getColor(this, com.google.android.material.R.attr.colorOnSurface),
    ).apply { textSize = GLYPH_DP * density }
    private val cellW = glyphs.measureText("M")
    private val cellH = glyphs.textSize * 1.12f
    private val word = AsciiType.wordPaint()
    private val bounds = Rect()

    private var cols = 0
    private var rows = ROWS
    private var cells = FloatArray(0)
    private var line = CharArray(0)

    init {
        if (contentDescription == null) contentDescription = AsciiType.WORD
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val maxW = MeasureSpec.getSize(widthMeasureSpec).takeIf {
            MeasureSpec.getMode(widthMeasureSpec) != MeasureSpec.UNSPECIFIED
        } ?: Int.MAX_VALUE
        // Letters as tall as the rows allow; fewer columns if that's too wide.
        val h = rows * cellH
        word.textSize = 100f
        word.getTextBounds(AsciiType.WORD, 0, AsciiType.WORD.length, bounds)
        word.textSize *= h * FILL / bounds.height()
        val wordW = word.measureText(AsciiType.WORD)
        val fitW = min(wordW + 2 * cellW, maxW.toFloat())
        if (wordW + 2 * cellW > fitW) word.textSize *= (fitW - 2 * cellW) / wordW
        cols = max(1, ceil(fitW / cellW).toInt())
        setMeasuredDimension(ceil(cols * cellW).toInt(), ceil(h).toInt())
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        word.getTextBounds(AsciiType.WORD, 0, AsciiType.WORD.length, bounds)
        val cx = cols * cellW / 2f
        val baseline = rows * cellH / 2f - bounds.exactCenterY()
        cells = AsciiType.rasterize(cols, rows, cellW, cellH) {
            it.drawText(AsciiType.WORD, cx, baseline, word)
        }
        line = CharArray(cols)
    }

    override fun onDraw(canvas: Canvas) {
        if (cells.size != cols * rows) return
        val baseline = -glyphs.ascent() + cellH * 0.06f
        for (r in 0 until rows) {
            for (c in 0 until cols) line[c] = AsciiType.charFor(cells[r * cols + c])
            canvas.drawText(line, 0, cols, 0f, r * cellH + baseline, glyphs)
        }
    }

    private companion object {
        const val GLYPH_DP = 6.5f     // character size: big enough to read as type, small enough to read as a word
        const val ROWS = 9
        const val FILL = 0.92f        // the word's height, as a share of the rows
    }
}
