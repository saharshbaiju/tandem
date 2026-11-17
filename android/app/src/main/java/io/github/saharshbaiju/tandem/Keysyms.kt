package io.github.saharshbaiju.tandem

import android.view.KeyEvent

/**
 * X keysyms: how keys travel to the computer (KEYS frames, linux/keyboard.py).
 *
 * The phone sends what the user meant ("a", "É", Return, Control_L), never a
 * key position, because it can't know the computer's keyboard layout; the
 * computer finds the key in its own layout.
 */
object Keysyms {
    const val BACKSPACE = 0xFF08
    const val TAB = 0xFF09
    const val RETURN = 0xFF0D
    const val ESCAPE = 0xFF1B
    const val HOME = 0xFF50
    const val LEFT = 0xFF51
    const val UP = 0xFF52
    const val RIGHT = 0xFF53
    const val DOWN = 0xFF54
    const val PAGE_UP = 0xFF55
    const val PAGE_DOWN = 0xFF56
    const val END = 0xFF57
    const val INSERT = 0xFF63
    const val MENU = 0xFF67
    const val DELETE = 0xFFFF
    const val SHIFT_L = 0xFFE1
    const val SHIFT_R = 0xFFE2
    const val CONTROL_L = 0xFFE3
    const val CONTROL_R = 0xFFE4
    const val CAPS_LOCK = 0xFFE5
    const val ALT_L = 0xFFE9
    const val ALT_R = 0xFFEA
    const val SUPER_L = 0xFFEB
    const val SUPER_R = 0xFFEC
    const val F1 = 0xFFBE

    private const val UNICODE_BASE = 0x01000000

    /** The keysym that types code point [cp]: Latin-1 is its own keysym, the rest Unicode. */
    fun forCodePoint(cp: Int): Int = when (cp) {
        '\n'.code, '\r'.code -> RETURN
        '\t'.code -> TAB
        '\b'.code -> BACKSPACE
        in 0x20..0x7E, in 0xA0..0xFF -> cp
        else -> UNICODE_BASE or cp
    }

    fun isModifier(keysym: Int) = keysym in SHIFT_L..SUPER_R || keysym == 0xFE03

    /** Keys that aren't characters, by Android key code; null for the rest. */
    fun forKeyCode(keyCode: Int): Int? = when (keyCode) {
        KeyEvent.KEYCODE_DEL -> BACKSPACE
        KeyEvent.KEYCODE_FORWARD_DEL -> DELETE
        KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER -> RETURN
        KeyEvent.KEYCODE_TAB -> TAB
        KeyEvent.KEYCODE_ESCAPE -> ESCAPE
        KeyEvent.KEYCODE_DPAD_LEFT -> LEFT
        KeyEvent.KEYCODE_DPAD_RIGHT -> RIGHT
        KeyEvent.KEYCODE_DPAD_UP -> UP
        KeyEvent.KEYCODE_DPAD_DOWN -> DOWN
        KeyEvent.KEYCODE_MOVE_HOME -> HOME
        KeyEvent.KEYCODE_MOVE_END -> END
        KeyEvent.KEYCODE_PAGE_UP -> PAGE_UP
        KeyEvent.KEYCODE_PAGE_DOWN -> PAGE_DOWN
        KeyEvent.KEYCODE_INSERT -> INSERT
        KeyEvent.KEYCODE_MENU -> MENU
        KeyEvent.KEYCODE_SHIFT_LEFT -> SHIFT_L
        KeyEvent.KEYCODE_SHIFT_RIGHT -> SHIFT_R
        KeyEvent.KEYCODE_CTRL_LEFT -> CONTROL_L
        KeyEvent.KEYCODE_CTRL_RIGHT -> CONTROL_R
        KeyEvent.KEYCODE_ALT_LEFT -> ALT_L
        KeyEvent.KEYCODE_ALT_RIGHT -> ALT_R
        KeyEvent.KEYCODE_META_LEFT -> SUPER_L
        KeyEvent.KEYCODE_META_RIGHT -> SUPER_R
        KeyEvent.KEYCODE_CAPS_LOCK -> CAPS_LOCK
        KeyEvent.KEYCODE_SPACE -> ' '.code
        in KeyEvent.KEYCODE_F1..KeyEvent.KEYCODE_F12 -> F1 + (keyCode - KeyEvent.KEYCODE_F1)
        else -> null
    }

    /** The KEYS body: keysym u32 + pressed u8, big-endian (linux/phone_input.py). */
    fun encode(keysym: Int, pressed: Boolean): ByteArray = byteArrayOf(
        (keysym ushr 24).toByte(), (keysym ushr 16).toByte(),
        (keysym ushr 8).toByte(), keysym.toByte(), if (pressed) 1 else 0,
    )
}

/**
 * Turns what an on-screen keyboard does to a text field into key presses.
 *
 * Keyboards don't just type characters: with swipe typing or autocorrect they
 * show a word while it is being written ("composing"), then change it or
 * replace it. The computer only ever sees key presses, so this keeps the text
 * it has typed for the current word and, on every change, presses Backspace
 * for what no longer matches and types the rest.
 *
 * [key] sends one keysym (press and release). Not thread-safe; the main thread
 * uses it.
 */
class TextToKeys(private val key: (Int) -> Unit) {
    /** What the computer shows for the word being composed. */
    var composing = ""
        private set

    /** The keyboard shows [text] as the word being composed. */
    fun setComposing(text: CharSequence) {
        replaceComposingWith(text.toString())
    }

    /** The keyboard commits [text]: it replaces the composing word, if any. */
    fun commit(text: CharSequence) {
        replaceComposingWith(text.toString())
        composing = ""
    }

    /** The composing word is accepted as it is. */
    fun finishComposing() {
        composing = ""
    }

    /** Delete [before] characters before the cursor and [after] after it. */
    fun delete(before: Int, after: Int) {
        // Deleting into the composing word shortens it on the computer too.
        val fromComposing = minOf(before, composing.length)
        composing = composing.dropLast(fromComposing)
        repeat(before) { key(Keysyms.BACKSPACE) }
        repeat(after) { key(Keysyms.DELETE) }
    }

    private fun replaceComposingWith(text: String) {
        val common = composing.commonPrefixWith(text).let {
            // never split a surrogate pair
            if (it.isNotEmpty() && Character.isHighSurrogate(it.last())) it.dropLast(1) else it
        }
        val removed = composing.substring(common.length)
        repeat(removed.codePointCount(0, removed.length)) { key(Keysyms.BACKSPACE) }
        text.substring(common.length).codePoints().forEach { key(Keysyms.forCodePoint(it)) }
        composing = text
    }
}
