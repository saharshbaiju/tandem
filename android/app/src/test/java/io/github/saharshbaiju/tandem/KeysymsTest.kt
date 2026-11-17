package io.github.saharshbaiju.tandem

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

class KeysymsTest {

    private fun typed(block: TextToKeys.() -> Unit): List<Int> {
        val keys = ArrayList<Int>()
        TextToKeys { keys += it }.block()
        return keys
    }

    private fun chars(s: String) = s.codePoints().toArray().map { Keysyms.forCodePoint(it) }

    @Test fun latin1IsItsOwnKeysymAndTheRestIsUnicode() {
        assertEquals('a'.code, Keysyms.forCodePoint('a'.code))
        assertEquals(0xE9, Keysyms.forCodePoint('é'.code))
        assertEquals(0x010020AC, Keysyms.forCodePoint('€'.code))
        assertEquals(0x0101F600, Keysyms.forCodePoint(0x1F600))
        assertEquals(Keysyms.RETURN, Keysyms.forCodePoint('\n'.code))
    }

    @Test fun encodeIsBigEndianWithAPressedByte() {
        assertArrayEquals(byteArrayOf(0x01, 0x00, 0x20, 0xAC.toByte(), 1), Keysyms.encode(0x010020AC, true))
        assertArrayEquals(byteArrayOf(0, 0, 0xFF.toByte(), 0x0D, 0), Keysyms.encode(Keysyms.RETURN, false))
    }

    @Test fun plainCommitTypesEachCharacter() {
        assertEquals(chars("hi!"), typed { commit("hi!") })
    }

    @Test fun composingOnlySendsTheDifference() {
        assertEquals(chars("hel"), typed { setComposing("h"); setComposing("he"); setComposing("hel") })
    }

    @Test fun autocorrectReplacesOnlyWhatChanged() {
        // swipe gives "teh", the keyboard then commits "the "
        val expected = chars("teh") + List(2) { Keysyms.BACKSPACE } + chars("he ")
        assertEquals(expected, typed { setComposing("teh"); commit("the ") })
    }

    @Test fun finishedWordIsNotRetypedByTheNextOne() {
        assertEquals(chars("okgo"), typed { setComposing("ok"); finishComposing(); setComposing("go") })
    }

    @Test fun deleteShortensTheComposingWord() {
        val keys = typed { setComposing("cat"); delete(1, 0); setComposing("car") }
        assertEquals(chars("cat") + Keysyms.BACKSPACE + chars("r"), keys)
    }

    @Test fun emojiIsOneBackspace() {
        val smile = String(Character.toChars(0x1F600))
        val keys = typed { setComposing("a$smile"); setComposing("a") }
        assertEquals(chars("a") + Keysyms.forCodePoint(0x1F600) + Keysyms.BACKSPACE, keys)
    }
}
