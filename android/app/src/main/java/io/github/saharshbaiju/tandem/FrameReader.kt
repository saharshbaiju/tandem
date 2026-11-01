package io.github.saharshbaiju.tandem

import java.io.InputStream

/**
 * Reads the daemon's framed wire protocol (see tandemd.py):
 *
 *     4-byte type + 4-byte big-endian length + body
 *
 * Type "NALU" carries one Annex-B NAL (with start code); "ASX1" carries the
 * Linux-side settings as JSON; "INPT" says whether touch input is allowed;
 * "PING" is an empty keepalive after 1 s of silence; "CHAL" / "OKAY" / "DENY"
 * belong to the pairing handshake ([SecureChannel]). DENY can also come
 * after OKAY (all phone slots taken). Framing (rather than scanning Annex-B
 * start codes) lets the daemon interleave config updates with video at any
 * time.
 *
 * Each body is a fresh array: NAL bodies are handed to the decode thread and
 * kept in an access unit, so a reused buffer would be overwritten under it.
 */
class FrameReader(private val src: InputStream) {

    /** A decoded frame. [type] is the 4-char tag; [body] is the payload. */
    class Frame(val type: String, val body: ByteArray)

    private val header = ByteArray(8)

    /** Blocks until a full frame is read; returns null at end of stream. */
    @Throws(java.io.IOException::class)
    fun next(): Frame? {
        if (!readFully(header, 8)) return null
        val len = ((header[4].toInt() and 0xFF) shl 24) or
            ((header[5].toInt() and 0xFF) shl 16) or
            ((header[6].toInt() and 0xFF) shl 8) or
            (header[7].toInt() and 0xFF)
        if (len < 0 || len > MAX_FRAME) throw java.io.IOException("corrupt frame from the computer")
        val body = ByteArray(len)
        if (!readFully(body, len)) return null
        return Frame(typeOf(header), body)
    }

    /** The 4-char tag; the common ones without allocating a String per frame. */
    private fun typeOf(h: ByteArray): String {
        for (known in KNOWN_TYPES) {
            if (h[0] == known[0].code.toByte() && h[1] == known[1].code.toByte() &&
                h[2] == known[2].code.toByte() && h[3] == known[3].code.toByte()) return known
        }
        return String(h, 0, 4, Charsets.US_ASCII)
    }

    private fun readFully(dst: ByteArray, len: Int): Boolean {
        var off = 0
        while (off < len) {
            val n = src.read(dst, off, len - off)
            if (n < 0) return false
            off += n
        }
        return true
    }

    companion object {
        const val TYPE_NAL = "NALU"
        const val TYPE_CONFIG = "ASX1"
        const val TYPE_PING = "PING"   // keepalive, empty body
        const val TYPE_INPUT_STATE = "INPT"   // answer to HELO: touch allowed?
        private const val MAX_FRAME = 32 * 1024 * 1024   // sanity cap
        private val KNOWN_TYPES = arrayOf(TYPE_NAL, TYPE_PING, TYPE_CONFIG, TYPE_INPUT_STATE)
    }
}
