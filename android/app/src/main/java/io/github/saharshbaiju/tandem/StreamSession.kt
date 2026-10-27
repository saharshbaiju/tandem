package io.github.saharshbaiju.tandem

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.util.Log
import android.view.Surface
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.SSLException
import javax.net.ssl.SSLHandshakeException
import org.json.JSONObject

/** Linux-side settings advertised by the daemon's banner (see tandemd.py). */
data class ServerConfig(
    val side: String,
    val width: Int,
    val height: Int,
    val fps: Int,
    val scale: Double,
    val quality: String,
    val bitrate: String,
    val codec: String,
    val host: String,
    val backend: String,
)

/**
 * One streaming session: connect TCP -> read framed NALs -> feed a hardware
 * H.264 decoder that renders straight onto [surface].
 *
 * Latency principles:
 *   * No app-side buffering: each access unit is queued the moment it's
 *     complete and every decoded frame is released to the Surface immediately.
 *   * KEY_LOW_LATENCY on Android 11+ (the encoder emits no B-frames).
 *
 * Reliability principles (each fixed a real "connects then drops" bug):
 *   * Every callback carries `this`, so the activity can ignore a stale
 *     session that finishes after a newer one already started. Previously an
 *     old session's onStopped nulled the new one, a third decoder was created
 *     on the same Surface, failed, and the app looped connect/disconnect.
 *   * A read timeout (the daemon PINGs every second) detects dead links.
 *   * Decoder input buffers are sized for the real stream, not a 1080p guess,
 *     so big keyframes at native phone resolution aren't silently dropped.
 *   * A new SPS (resolution change) reconfigures the decoder.
 *
 * Security: the TCP connection is wrapped in TLS and both sides prove they
 * know the pairing key before anything else is exchanged ([SecureChannel]).
 * No video is decoded and no touch is sent until the computer has proved it
 * is the one we paired with.
 *
 * Touch input travels the other way on the same connection: the computer
 * sends INPT saying whether this phone may send touches, and [sendTouch]
 * then queues TUCH frames. Writes happen on a dedicated sender thread, so the
 * UI thread never touches the network (Conscrypt allows one reader and one
 * writer thread on a TLS socket).
 */
class StreamSession(
    private val target: ConnectTarget,
    private val appVersion: String,
    private val surface: Surface,
    private val listener: Listener,
) : Runnable {

    interface Listener {
        fun onConnected(session: StreamSession, host: String)
        fun onFirstFrame(session: StreamSession)
        fun onStreamInfo(session: StreamSession, width: Int, height: Int)
        fun onServerConfig(session: StreamSession, config: ServerConfig)
        /** The computer's answer to our HELO: may we send touches, and if not, why. */
        fun onInputState(session: StreamSession, touchAllowed: Boolean, reason: String)
        /**
         * The session ended. [final] means retrying can't help (wrong pairing
         * code, a different computer, an outdated version): don't reconnect.
         */
        fun onStopped(session: StreamSession, reason: String?, final: Boolean)
    }

    @Volatile private var running = true
    @Volatile private var socket: Socket? = null
    private val hosts = target.hosts
    private val port = target.port
    private var codec: MediaCodec? = null
    private var configuredSps: ByteArray? = null
    private var sps: ByteArray? = null
    private var pps: ByteArray? = null
    private var serverConfig: ServerConfig? = null
    private var firstFrame = false
    private var ptsUs = 0L
    private val info = MediaCodec.BufferInfo()
    private val au = java.io.ByteArrayOutputStream(1 shl 18)   // current access unit

    /** Phone -> computer frames; set once connected. */
    @Volatile private var writer: FrameWriter? = null
    /** Serialises writes off the UI thread, in the order they were queued. */
    private val sender = Executors.newSingleThreadExecutor { r -> Thread(r, "stream-sender") }

    /** The host that answered (one of [hosts]). */
    @Volatile var connectedHost: String? = null
        private set

    private val thread = Thread(this, "stream-session").apply { start() }

    fun stop() {
        running = false
        try { socket?.close() } catch (_: Exception) {}
    }

    /**
     * Queue one touch event. [x] and [y] are fractions (0..1) of the video
     * picture; [finger] is Android's pointer id. Silently dropped before the
     * connection is up or after it ended.
     */
    fun sendTouch(action: Int, finger: Int, x: Float, y: Float) {
        val body = ByteArray(6)
        val scaledX = (x.coerceIn(0f, 1f) * COORD_SCALE).toInt()
        val scaledY = (y.coerceIn(0f, 1f) * COORD_SCALE).toInt()
        body[0] = action.toByte()
        body[1] = finger.coerceIn(0, 255).toByte()
        body[2] = (scaledX ushr 8).toByte(); body[3] = scaledX.toByte()
        body[4] = (scaledY ushr 8).toByte(); body[5] = scaledY.toByte()
        sendInBackground(FrameWriter.TYPE_TOUCH, body)
    }

    private fun sendInBackground(type: String, body: ByteArray) {
        val w = writer ?: return
        try {
            sender.execute {
                try { w.send(type, body) } catch (_: IOException) {
                    // the reader notices a dead connection and ends the session
                }
            }
        } catch (_: RejectedExecutionException) {
            // session already released
        }
    }

    /** Stop and wait for the decoder to be released (call before the Surface dies). */
    fun stopAndJoin() {
        stop()
        try { thread.join(2000) } catch (_: InterruptedException) {}
    }

    override fun run() {
        var reason: String? = null
        var final = false
        try {
            val raw = connectAny()
            socket = raw
            if (!running) return
            raw.soTimeout = HANDSHAKE_TIMEOUT_MS
            val (s, certFingerprint) = SecureChannel.wrap(raw, connectedHost ?: "", port,
                target.pinnedFingerprint)
            socket = s
            val reader = FrameReader(s.getInputStream().buffered(1 shl 16))
            val frameWriter = FrameWriter(s.getOutputStream())
            SecureChannel.authenticate(reader, frameWriter, target.pairingKey,
                certFingerprint, appVersion)
            if (!running) return
            s.soTimeout = READ_TIMEOUT_MS
            writer = frameWriter
            listener.onConnected(this, connectedHost ?: "")

            while (running) {
                val f = reader.next() ?: throw IOException("the computer closed the connection")
                when (f.type) {
                    FrameReader.TYPE_NAL -> handleNal(nalType(f.body), f.body)
                    FrameReader.TYPE_CONFIG -> parseConfig(f.body)?.let {
                        serverConfig = it
                        listener.onServerConfig(this, it)
                    }
                    FrameReader.TYPE_INPUT_STATE -> parseInputState(f.body)
                    FrameReader.TYPE_PING -> {}   // keepalive: resets the read timeout
                    // unknown frame types are ignored for forward compatibility
                }
            }
        } catch (e: SecureChannel.PairingException) {
            reason = e.message
            final = true
        } catch (e: SSLHandshakeException) {
            // A pinned certificate that doesn't match lands here too.
            reason = if (SecureChannel.CERT_MISMATCH in (e.cause?.message ?: e.message ?: ""))
                CERT_CHANGED else OUTDATED_COMPUTER
            final = true
            Log.w(TAG, "TLS handshake failed", e)
        } catch (e: SSLException) {
            // Before the handshake finished: almost always a version before 0.5 answering
            // in plain text. Afterwards: the link broke, worth retrying.
            if (writer == null) { reason = OUTDATED_COMPUTER; final = true }
            else reason = "connection lost (${e.message})"
            Log.w(TAG, "TLS error", e)
        } catch (e: SocketTimeoutException) {
            reason = "connection lost (no data for ${READ_TIMEOUT_MS / 1000}s)"
        } catch (e: MediaCodec.CodecException) {
            reason = "video decoder error"
            Log.e(TAG, "codec error", e)
        } catch (e: Exception) {
            if (running) {
                reason = e.message ?: e.javaClass.simpleName
                Log.w(TAG, "session ended: $reason")
            }
        } finally {
            release()
            listener.onStopped(this, if (running) reason else null, final && running)
        }
    }

    /**
     * Try every candidate address in parallel (USB-tether IP, Wi-Fi IP, ...)
     * and keep whichever answers first, so the QR "just works" on any link.
     */
    private fun connectAny(): Socket {
        val winner = AtomicReference<Socket?>(null)
        val lastError = AtomicReference<Exception?>(null)
        val refusedBy = AtomicReference<String?>(null)
        val remaining = AtomicInteger(hosts.size)
        val done = CountDownLatch(1)
        for (h in hosts) {
            Thread({
                val s = Socket()
                try {
                    s.tcpNoDelay = true
                    s.receiveBufferSize = 1 shl 20
                    s.connect(InetSocketAddress(h, port), CONNECT_TIMEOUT_MS)
                    if (winner.compareAndSet(null, s)) {
                        connectedHost = h
                        done.countDown()
                    } else {
                        s.close()
                    }
                } catch (e: Exception) {
                    // "refused" is the most useful diagnosis (computer reachable,
                    // extender not running): never let a timeout overwrite it.
                    if (e is java.net.ConnectException && "refused" in (e.message ?: "")) {
                        refusedBy.compareAndSet(null, h)
                    }
                    lastError.set(e)
                    try { s.close() } catch (_: Exception) {}
                } finally {
                    if (remaining.decrementAndGet() == 0) done.countDown()
                }
            }, "connect-$h").start()
        }
        done.await(CONNECT_TIMEOUT_MS + 500L, TimeUnit.MILLISECONDS)
        val s = winner.get()
        if (s == null || !running) {
            s?.close()
            refusedBy.get()?.let { throw IOException("connection refused by $it") }
            val why = lastError.get()?.message ?: "timed out"
            throw IOException("couldn't reach ${hosts.joinToString()} : $port ($why)")
        }
        return s
    }

    /** Parse an ASX1 config frame body (JSON) into [ServerConfig]. */
    private fun parseConfig(body: ByteArray): ServerConfig? = try {
        val o = JSONObject(String(body, Charsets.UTF_8))
        ServerConfig(
            side = o.optString("side", ""),
            width = o.optInt("width", 0),
            height = o.optInt("height", 0),
            fps = o.optInt("fps", 0),
            scale = o.optDouble("scale", 0.0),
            quality = o.optString("quality", ""),
            bitrate = o.optString("bitrate", ""),
            codec = o.optString("codec", ""),
            host = o.optString("host", ""),
            backend = o.optString("backend", ""),
        )
    } catch (e: Exception) {
        Log.w(TAG, "bad config frame", e)
        null
    }

    /** Parse an INPT frame body: {"touch": bool, "reason": "..."}. */
    private fun parseInputState(body: ByteArray) {
        try {
            val o = JSONObject(String(body, Charsets.UTF_8))
            listener.onInputState(this, o.optBoolean("touch", false), o.optString("reason", ""))
        } catch (e: Exception) {
            Log.w(TAG, "bad input-state frame", e)
        }
    }

    /** NAL type = header byte & 0x1F, located right after the start code. */
    private fun nalType(nal: ByteArray): Int {
        if (nal.size < 5) return -1
        val hdrIdx = if (nal[2].toInt() == 1) 3 else 4   // 00 00 01 vs 00 00 00 01
        return nal[hdrIdx].toInt() and 0x1F
    }

    private fun handleNal(type: Int, nal: ByteArray) {
        when (type) {
            NAL_SPS -> {
                sps = nal
                // A different SPS means the resolution/profile changed: rebuild
                // the decoder at the keyframe that follows.
                if (codec != null && !nal.contentEquals(configuredSps)) releaseCodec()
            }
            NAL_PPS -> pps = nal
        }
        if (codec == null) {
            val sp = sps; val pp = pps
            if (sp != null && pp != null && type == NAL_IDR) {
                configure(sp, pp)
                // the first unit we queue is a complete SPS+PPS+IDR
                au.reset(); au.write(sp); au.write(pp)
            } else {
                return  // keep gathering until we can start on a keyframe
            }
        }

        // Hardware decoders (e.g. Qualcomm C2) reject partial input: they need a
        // whole *access unit* (one frame) per buffer, not one NAL at a time.
        // Accumulate NALs and flush when a VCL slice (type 1..5) ends the frame.
        au.write(nal)
        if (type in 1..5) {
            feedAndDrain(au.toByteArray())
            au.reset()
        }
    }

    private fun configure(sp: ByteArray, pp: ByteArray) {
        // Width/height are hints (the decoder reads the true size from the SPS);
        // the banner tells us the real size so input buffers are big enough.
        val w = serverConfig?.width?.takeIf { it > 0 } ?: 1920
        val h = serverConfig?.height?.takeIf { it > 0 } ?: 1080
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h)
        fmt.setByteBuffer("csd-0", ByteBuffer.wrap(sp))
        fmt.setByteBuffer("csd-1", ByteBuffer.wrap(pp))
        fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, maxOf(w * h, 1 shl 20))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        }
        fmt.setInteger(MediaFormat.KEY_PRIORITY, 0)          // realtime
        val c = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        c.configure(fmt, surface, null, 0)
        c.start()
        codec = c
        configuredSps = sp
        Log.i(TAG, "decoder configured for ${w}x$h")
    }

    private fun feedAndDrain(frame: ByteArray) {
        val c = codec ?: return
        // Get an input buffer, draining outputs while we wait so we never stall.
        var inIdx = c.dequeueInputBuffer(0)
        while (inIdx < 0 && running) {
            drain(c)
            inIdx = c.dequeueInputBuffer(2000)
        }
        if (inIdx < 0) return
        val buf: ByteBuffer = c.getInputBuffer(inIdx) ?: return
        if (frame.size > buf.capacity()) {
            Log.w(TAG, "access unit ${frame.size}B > input buffer ${buf.capacity()}B, dropped")
            c.queueInputBuffer(inIdx, 0, 0, ptsUs, 0)
            return
        }
        buf.clear()
        buf.put(frame)
        c.queueInputBuffer(inIdx, 0, frame.size, ptsUs, 0)
        ptsUs += 16_666   // ordering only; frames render on arrival
        drain(c)
    }

    private fun drain(c: MediaCodec) {
        while (true) {
            val outIdx = c.dequeueOutputBuffer(info, 0)
            when {
                outIdx >= 0 -> {
                    // render = true -> hand the frame to the Surface immediately
                    c.releaseOutputBuffer(outIdx, true)
                    if (!firstFrame && info.size >= 0) {
                        firstFrame = true
                        listener.onFirstFrame(this)
                    }
                }
                outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    val f = c.outputFormat
                    try {
                        // crop is the real picture (1080 vs the padded 1088)
                        val w = if (f.containsKey("crop-right"))
                            f.getInteger("crop-right") - f.getInteger("crop-left") + 1
                        else f.getInteger(MediaFormat.KEY_WIDTH)
                        val h = if (f.containsKey("crop-bottom"))
                            f.getInteger("crop-bottom") - f.getInteger("crop-top") + 1
                        else f.getInteger(MediaFormat.KEY_HEIGHT)
                        listener.onStreamInfo(this, w, h)
                    } catch (_: Exception) {
                    }
                }
                else -> break // TRY_AGAIN_LATER / buffers changed
            }
        }
    }

    private fun releaseCodec() {
        try { codec?.stop() } catch (_: Exception) {}
        try { codec?.release() } catch (_: Exception) {}
        codec = null
        configuredSps = null
        au.reset()
    }

    private fun release() {
        releaseCodec()
        sender.shutdownNow()
        writer = null
        try { socket?.close() } catch (_: Exception) {}
    }

    companion object {
        private const val TAG = "StreamSession"
        const val NAL_IDR = 5
        const val NAL_SPS = 7
        const val NAL_PPS = 8
        private const val CONNECT_TIMEOUT_MS = 3000
        private const val READ_TIMEOUT_MS = 4000
        private const val HANDSHAKE_TIMEOUT_MS = 5000

        const val CERT_CHANGED = "This isn't the computer you paired with: its security " +
            "certificate is different. If you reinstalled Tandem there, scan its QR code again."
        const val OUTDATED_COMPUTER = "Couldn't set up an encrypted connection. Update Tandem " +
            "on the computer (re-run the install command), then scan its QR code again."

        // TUCH actions (mirrored in linux/phone_input.py)
        const val TOUCH_DOWN = 0
        const val TOUCH_MOVE = 1
        const val TOUCH_UP = 2
        const val TOUCH_CANCEL = 3
        private const val COORD_SCALE = 65535f
    }
}
