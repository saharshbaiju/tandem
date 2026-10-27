package io.github.saharshbaiju.tandem

import java.io.OutputStream

/**
 * Writes frames to the computer in the daemon's wire format (the same one
 * [FrameReader] reads):
 *
 *     4-byte type + 4-byte big-endian length + body
 *
 * Phone -> computer types:
 *   "HELO" / "AUTH"  the pairing handshake, see [SecureChannel]
 *   "TUCH"  one touch event, see [StreamSession.sendTouch] (linux/phone_input.py)
 *
 * Each frame goes out in a single write so the bytes of two frames can never
 * interleave; [send] is synchronized for the same reason.
 */
class FrameWriter(private val out: OutputStream) {

    @Synchronized
    fun send(type: String, body: ByteArray = ByteArray(0)) {
        require(type.length == 4) { "frame type must be 4 characters" }
        val frame = ByteArray(8 + body.size)
        type.toByteArray(Charsets.US_ASCII).copyInto(frame, 0)
        frame[4] = (body.size ushr 24).toByte()
        frame[5] = (body.size ushr 16).toByte()
        frame[6] = (body.size ushr 8).toByte()
        frame[7] = body.size.toByte()
        body.copyInto(frame, 8)
        out.write(frame)
        out.flush()
    }

    companion object {
        const val TYPE_TOUCH = "TUCH"
    }
}
