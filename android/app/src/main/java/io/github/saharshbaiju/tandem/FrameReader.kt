package io.github.saharshbaiju.tandem

import java.io.InputStream

/**
 * Reads the daemon's framed wire protocol (see tandemd.py):
 *
 *     4-byte type + 4-byte big-endian length + body
 *
 * Type "NALU" carries one Annex-B NAL (with start code); "ASX1" carries the
 * Linux-side settings as JSON; "INPT" says whether touch input is allowed;
 * "CHAL" / "OKAY" / "DENY" belong to the pairing handshake ([SecureChannel]).
 * Framing (rather than scanning Annex-B start codes) lets the daemon
 * interleave config updates with video at any time.
 */
class FrameReader(private val src: InputStream) {

    /** A decoded frame. [type] is the 4-char tag; [body] is the payload. */
    class Frame(val type: String, val body: ByteArray)

    private val header = ByteArray(8)

    /** Blocks until a full frame is read; returns null at end of stream. */
    fun next(): Frame? {
        if (!readFully(header, 8)) return null
        val len = ((header[4].toInt() and 0xFF) shl 24) or
            ((header[5].toInt() and 0xFF) shl 16) or
            ((header[6].toInt() and 0xFF) shl 8) or
            (header[7].toInt() and 0xFF)
        if (len < 0 || len > MAX_FRAME) return null   // corrupt / desynced
        val body = ByteArray(len)
        if (!readFully(body, len)) return null
        val type = String(header, 0, 4, Charsets.US_ASCII)
        return Frame(type, body)
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
    }
}
