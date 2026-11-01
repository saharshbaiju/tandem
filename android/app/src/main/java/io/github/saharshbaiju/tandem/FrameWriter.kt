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
 *   "TUCH"  one touch event, see [StreamSession.sendTouches] (linux/phone_input.py)
 *   "KEYQ"  empty: the decoder needs a keyframe now (it dropped data, recovered
 *           from an error or skipped a backlog). The daemon replays its cached
 *           keyframe or asks the encoder for one; older daemons ignore it.
 *   "PING"  empty: sent after 1 s without anything else, so the computer can
 *           tell a live phone from a vanished one. Older daemons ignore it.
 *
 * Each call goes out in a single write so the bytes of two frames can never
 * interleave; [send] / [sendAll] are synchronized for the same reason.
 */
class FrameWriter(private val out: OutputStream) {

    @Synchronized
    fun send(type: String, body: ByteArray = ByteArray(0)) = sendAll(listOf(type to body))

    /** Several frames in one write (one TLS record where they fit), then flush. */
    @Synchronized
    fun sendAll(frames: List<Pair<String, ByteArray>>) {
        if (frames.isEmpty()) return
        val buf = ByteArray(frames.sumOf { 8 + it.second.size })
        var off = 0
        for ((type, body) in frames) {
            require(type.length == 4) { "frame type must be 4 characters" }
            type.toByteArray(Charsets.US_ASCII).copyInto(buf, off)
            buf[off + 4] = (body.size ushr 24).toByte()
            buf[off + 5] = (body.size ushr 16).toByte()
            buf[off + 6] = (body.size ushr 8).toByte()
            buf[off + 7] = body.size.toByte()
            body.copyInto(buf, off + 8)
            off += 8 + body.size
        }
        out.write(buf)
        out.flush()
    }

    companion object {
        const val TYPE_TOUCH = "TUCH"
        const val TYPE_KEYFRAME_REQUEST = "KEYQ"
        const val TYPE_PING = "PING"
    }
}
