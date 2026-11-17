package io.github.saharshbaiju.tandem

import android.content.Context
import android.text.InputType
import android.util.AttributeSet
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.BaseInputConnection
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputConnection

/**
 * Typing on the computer: the phone's on-screen keyboard, a keyboard plugged
 * into the phone, and the key bar's Esc / Tab / Ctrl / Alt / Super / arrows.
 *
 * Everything becomes keysym presses through [send] (keysym, pressed). Ctrl,
 * Alt and Super on the key bar are "latched": tap Ctrl, then C, and the
 * computer gets Ctrl+C; the latch then lets go by itself. [onLatchChanged]
 * tells the key bar to redraw.
 */
class RemoteKeyboard(
    private val send: (keysym: Int, pressed: Boolean) -> Unit,
    private val onLatchChanged: () -> Unit,
) {
    /** Modifier keysyms latched on the key bar, applied to the next key. */
    val latched = LinkedHashSet<Int>()

    /** Keys down on a physical keyboard: key code -> (keysym, latched modifiers it took). */
    private val held = HashMap<Int, Pair<Int, List<Int>>>()

    /** The on-screen keyboard's text edits, as key presses. */
    val text = TextToKeys { tap(it) }

    /** Press and release [keysym], with whatever modifiers are latched. */
    fun tap(keysym: Int) {
        val modifiers = takeLatched(keysym)
        modifiers.forEach { send(it, true) }
        send(keysym, true)
        send(keysym, false)
        modifiers.asReversed().forEach { send(it, false) }
    }

    /**
     * The key bar's Ctrl / Alt / Super: the first tap latches it for the next
     * key. A second tap on a latched Ctrl or Alt cancels it; on a latched
     * Super it presses Super on its own (the app launcher / overview on most
     * desktops).
     */
    fun toggleLatch(modifier: Int) {
        if (latched.remove(modifier)) {
            if (modifier == Keysyms.SUPER_L) {
                send(modifier, true)
                send(modifier, false)
            }
        } else {
            latched += modifier
        }
        onLatchChanged()
    }

    /**
     * A key event from a physical keyboard or from the on-screen keyboard's
     * keys (Backspace, Enter). True if it was sent to the computer.
     * Repeats are dropped: the computer repeats a held key itself.
     */
    fun onKeyEvent(event: KeyEvent): Boolean {
        if (isPhoneKey(event.keyCode)) return false
        when (event.action) {
            KeyEvent.ACTION_DOWN -> {
                if (event.repeatCount > 0) return held.containsKey(event.keyCode)
                val keysym = keysymOf(event) ?: return false
                held.remove(event.keyCode)?.let { (old, mods) -> release(old, mods) }
                val modifiers = takeLatched(keysym)
                modifiers.forEach { send(it, true) }
                send(keysym, true)
                held[event.keyCode] = keysym to modifiers
                return true
            }
            KeyEvent.ACTION_UP -> {
                val (keysym, modifiers) = held.remove(event.keyCode) ?: return false
                release(keysym, modifiers)
                return true
            }
            KeyEvent.ACTION_MULTIPLE -> {
                val chars = event.characters ?: return false
                text.commit(chars)
                return true
            }
        }
        return false
    }

    /** Let go of everything (the keyboard closed, or the link dropped). */
    fun releaseAll() {
        for ((keysym, modifiers) in held.values) release(keysym, modifiers)
        held.clear()
        text.finishComposing()
        if (latched.isNotEmpty()) {
            latched.clear()
            onLatchChanged()
        }
    }

    private fun release(keysym: Int, modifiers: List<Int>) {
        send(keysym, false)
        modifiers.asReversed().forEach { send(it, false) }
    }

    private fun takeLatched(keysym: Int): List<Int> {
        if (Keysyms.isModifier(keysym) || latched.isEmpty()) return emptyList()
        val modifiers = latched.toList()
        latched.clear()
        onLatchChanged()
        return modifiers
    }

    private fun keysymOf(event: KeyEvent): Int? {
        Keysyms.forKeyCode(event.keyCode)?.let { return it }
        // A character key: what it types with Shift / Caps Lock / AltGr, but
        // not with Ctrl, Alt or Meta (those are sent as keys of their own, and
        // Android gives no character for Ctrl+C).
        var ignored = KeyEvent.META_CTRL_MASK or KeyEvent.META_META_MASK
        // Left Alt is a shortcut key; right Alt is AltGr, which picks characters.
        if (event.metaState and KeyEvent.META_ALT_LEFT_ON != 0) ignored = ignored or KeyEvent.META_ALT_MASK
        val meta = event.metaState and ignored.inv()
        val unicode = event.getUnicodeChar(meta)
        if (unicode == 0 || unicode and android.view.KeyCharacterMap.COMBINING_ACCENT != 0) return null
        return Keysyms.forCodePoint(unicode)
    }

    /** Keys that belong to the phone, never to the computer. */
    private fun isPhoneKey(keyCode: Int) = when (keyCode) {
        KeyEvent.KEYCODE_BACK, KeyEvent.KEYCODE_HOME, KeyEvent.KEYCODE_APP_SWITCH,
        KeyEvent.KEYCODE_VOLUME_UP, KeyEvent.KEYCODE_VOLUME_DOWN, KeyEvent.KEYCODE_VOLUME_MUTE,
        KeyEvent.KEYCODE_POWER, KeyEvent.KEYCODE_CAMERA, KeyEvent.KEYCODE_SEARCH,
        KeyEvent.KEYCODE_ASSIST, KeyEvent.KEYCODE_VOICE_ASSIST -> true
        else -> false
    }
}

/**
 * An invisible view the on-screen keyboard types into. It holds no text:
 * every edit goes straight to [keyboard] and on to the computer.
 *
 * The editor is described to the keyboard so it stays compact and literal:
 * never fullscreen in landscape (the desktop must stay in view), no
 * suggestion strip and no automatic capitals (the computer's own apps do
 * their own completion), Enter as a plain key.
 */
class KeyboardTargetView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : View(context, attrs) {

    var keyboard: RemoteKeyboard? = null

    init {
        isFocusable = true
        isFocusableInTouchMode = true
    }

    override fun onCheckIsTextEditor() = true

    override fun onCreateInputConnection(outAttrs: EditorInfo): InputConnection? {
        val kb = keyboard ?: return null
        outAttrs.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        // Incognito: what is typed on the computer (passwords too) is not the
        // phone keyboard's to learn from.
        outAttrs.imeOptions = EditorInfo.IME_FLAG_NO_EXTRACT_UI or EditorInfo.IME_FLAG_NO_FULLSCREEN or
            EditorInfo.IME_ACTION_NONE or EditorInfo.IME_FLAG_NO_ENTER_ACTION or
            EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
        outAttrs.initialSelStart = 0
        outAttrs.initialSelEnd = 0
        return Connection(this, kb)
    }

    /**
     * The on-screen keyboard's view of our "text field". It is always empty
     * apart from the word being composed, so Backspace on an empty field
     * reaches us as a key event, which most keyboards send then.
     */
    private class Connection(view: View, private val kb: RemoteKeyboard) :
        BaseInputConnection(view, false) {

        override fun commitText(text: CharSequence, newCursorPosition: Int): Boolean {
            kb.text.commit(text)
            return true
        }

        override fun setComposingText(text: CharSequence, newCursorPosition: Int): Boolean {
            kb.text.setComposing(text)
            return true
        }

        override fun finishComposingText(): Boolean {
            kb.text.finishComposing()
            return true
        }

        override fun deleteSurroundingText(beforeLength: Int, afterLength: Int): Boolean {
            kb.text.delete(beforeLength, afterLength)
            return true
        }

        override fun deleteSurroundingTextInCodePoints(beforeLength: Int, afterLength: Int) =
            deleteSurroundingText(beforeLength, afterLength)

        override fun sendKeyEvent(event: KeyEvent): Boolean {
            kb.onKeyEvent(event)
            return true
        }

        override fun performEditorAction(actionCode: Int): Boolean {
            kb.tap(Keysyms.RETURN)
            return true
        }

        override fun getTextBeforeCursor(length: Int, flags: Int): CharSequence =
            kb.text.composing.takeLast(length)

        override fun getTextAfterCursor(length: Int, flags: Int): CharSequence = ""

        override fun getCursorCapsMode(reqModes: Int) = 0
    }
}
