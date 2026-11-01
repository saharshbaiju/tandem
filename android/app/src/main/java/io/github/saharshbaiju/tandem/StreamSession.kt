package io.github.saharshbaiju.tandem

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.Process
import android.os.SystemClock
import android.util.Log
import android.view.Surface
import java.io.IOException
import java.io.InputStream
import java.net.ConnectException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import javax.net.ssl.SSLException
import javax.net.ssl.SSLSocket
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
) {
    /** The encoder's target bitrate in bits per second, or 0 when the banner doesn't say. */
    val bitrateBps: Long get() = parseBitrate(bitrate)

    /** This app decodes H.264 only; an empty codec is a daemon from before the field existed. */
    val isH264: Boolean get() = codec.isBlank() || codec.lowercase() in setOf("h264", "avc", "h.264")

    companion object {
        /** "12M" / "800k" / "12000000" -> bits per second (0 if unparseable). */
        fun parseBitrate(text: String): Long {
            val t = text.trim().lowercase().removeSuffix("bps").removeSuffix("b")
            if (t.isEmpty()) return 0
            val multiplier = when (t.last()) {
                'k' -> 1_000L
                'm' -> 1_000_000L
                'g' -> 1_000_000_000L
                else -> 1L
            }
            val number = (if (multiplier == 1L) t else t.dropLast(1)).toDoubleOrNull() ?: return 0
            return (number * multiplier).toLong().coerceAtLeast(0)
        }
    }
}

/**
 * One streaming session: connect -> TLS + pairing proof -> read framed NALs
 * -> assemble whole access units -> hardware H.264 decoder ([VideoDecoder])
 * rendering straight onto [surface].
 *
 * Threads (each with one job, so none can stall another):
 *   * "stream-session" reads the socket. It never waits on the decoder, so
 *     the read timeout (the daemon PINGs after 1 s of silence) always notices
 *     a dead link, and it can see a receive backlog building up.
 *   * "stream-decode" turns NALs into access units and queues them into the
 *     decoder; it owns the codec and its recovery.
 *   * "stream-decoder" is MediaCodec's callback thread: every decoded frame is
 *     released to the Surface the moment it exists (async mode). Previously
 *     output was only polled right after the next input, so each frame was
 *     held until the following one arrived and the last frame of a drag
 *     stayed hidden on a static desktop.
 *   * "stream-sender" writes phone -> computer frames (touch, KEYQ, PING).
 *
 * Latency principles:
 *   * No app-side buffering: an access unit is queued as soon as it is known
 *     to be complete, and frames render on decode (render = true, with the
 *     input timestamp taken from the monotonic clock, so it is never "in the
 *     future" for the compositor).
 *   * Low-latency decoder hints where the decoder supports them.
 *   * If we fall behind (receive backlog, or the decoder lagging) we skip to
 *     the next keyframe and ask for one (KEYQ) instead of playing old frames.
 *
 * Recovery without reconnecting: a dropped access unit, a decoder error or a
 * stalled decoder puts the session into "resync": frames are skipped up to
 * the next IDR, and a KEYQ asks the computer for one now (a daemon that
 * understands it replays its cached keyframe; an older one ignores it). If no
 * keyframe arrives in time, the session ends (not final) and the app
 * reconnects, which always starts with a keyframe.
 *
 * Reliability principles (each fixed a real "connects then drops" bug):
 *   * Every callback carries `this`, so the activity can ignore a stale
 *     session that finishes after a newer one already started.
 *   * "Final" (don't retry) is an explicit decision: only a certificate
 *     mismatch, a DENY the computer marks permanent, a bad pairing proof or a
 *     computer that doesn't speak TLS at all. A reset or EOF mid-handshake
 *     (flaky Wi-Fi, daemon restarting) is retried like any other drop.
 *
 * Security: the TCP connection is wrapped in TLS and both sides prove they
 * know the pairing key before anything else is exchanged ([SecureChannel]).
 * No video is decoded and no touch is sent until the computer has proved it
 * is the one we paired with.
 */
class StreamSession(
    private val target: ConnectTarget,
    private val appVersion: String,
    private val surface: Surface,
    private val listener: Listener,
    /** The network whose subnet holds [InetAddress], so the socket can be bound to it. */
    private val networkFor: (InetAddress) -> Network? = { null },
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
         * [retryAfterMs] > 0 is the computer's hint for the earliest retry.
         */
        fun onStopped(session: StreamSession, reason: String?, final: Boolean, retryAfterMs: Long)
    }

    /** Why a session ended, as decided by [classify]. */
    private class Outcome(val reason: String?, val final: Boolean, val retryAfterMs: Long = 0)

    /** Ends the session from inside (decoder thread, banner check) with a chosen outcome. */
    private class SessionEnd(message: String, val final: Boolean, val retryAfterMs: Long = 0) :
        IOException(message)

    /** A TLS connection (over [raw]) whose certificate already passed the pin. */
    private class Connected(val host: String, val raw: Socket, val tls: SSLSocket,
                            val fingerprint: ByteArray)

    @Volatile private var running = true
    @Volatile private var socket: Socket? = null
    @Volatile private var serverConfig: ServerConfig? = null
    @Volatile private var firstFrameShown = false
    /** The reader is dropping a stale receive backlog (a keyframe now would be skipped too). */
    @Volatile private var skippingBacklog = false
    private val pendingEnd = AtomicReference<SessionEnd?>(null)

    @Volatile private var sender: Sender? = null
    private var feeder: Feeder? = null

    /** The host that answered (one of the target's candidate hosts). */
    @Volatile var connectedHost: String? = null
        private set

    private val thread = Thread(this, "stream-session").apply { start() }

    fun stop() {
        running = false
        try { socket?.close() } catch (_: Exception) {}
    }

    /** Stop and wait for the decoder to be released (call before the Surface dies). */
    fun stopAndJoin() {
        stop()
        try { thread.join(2000) } catch (_: InterruptedException) {}
    }

    /**
     * Queue touch points (one MotionEvent's worth). x/y are fractions (0..1)
     * of the video picture. Dropped before the connection is up or after it
     * ended. Moves are coalesced while the link is slow (see [TouchQueue]).
     */
    fun sendTouches(points: List<TouchPoint>) {
        sender?.queueTouches(points)
    }

    // ------------------------------------------------------------------ reader

    override fun run() {
        raiseThreadPriority(Process.THREAD_PRIORITY_DISPLAY)
        var outcome = Outcome(null, false)
        try {
            val conn = connectAny()
            connectedHost = conn.host
            val s = conn.tls
            socket = s
            if (!running) return
            val input = s.getInputStream().buffered(1 shl 16)
            val reader = FrameReader(input)
            val frameWriter = FrameWriter(s.getOutputStream())
            SecureChannel.authenticate(reader, frameWriter, target.pairingKey,
                conn.fingerprint, appVersion)
            if (!running) return
            s.soTimeout = READ_TIMEOUT_MS

            // available() on the plain TCP stream under TLS = bytes still in
            // the kernel receive queue (it is never read from directly).
            val rawInput = try { conn.raw.getInputStream() } catch (_: IOException) { null }
            val backlog = BacklogMeter(input, rawInput)
            // sender first: the decode thread may ask for a keyframe at once
            sender = Sender(frameWriter).also { it.start() }
            val f = Feeder().also { feeder = it; it.start() }
            listener.onConnected(this, conn.host)

            while (running) {
                val frame = reader.next() ?: throw IOException("the computer closed the connection")
                when (frame.type) {
                    FrameReader.TYPE_NAL -> {
                        val skip = backlog.shouldSkip(frame.body.size, serverConfig)
                        if (skip && !skippingBacklog) f.skipBacklog()
                        // Ask for the keyframe once the stale data is read past,
                        // so the replayed one isn't skipped along with it.
                        if (!skip && skippingBacklog) sender?.requestKeyframe()
                        if (!skip) f.offer(frame.body)
                        skippingBacklog = skip
                    }
                    FrameReader.TYPE_CONFIG -> parseConfig(frame.body)?.let {
                        if (!it.isH264) throw SessionEnd(unsupportedCodec(it.codec), final = true)
                        serverConfig = it
                        listener.onServerConfig(this, it)
                    }
                    FrameReader.TYPE_INPUT_STATE -> parseInputState(frame.body)
                    FrameReader.TYPE_PING -> {}   // keepalive: resets the read timeout
                    // After OKAY the computer can still refuse us (all phone slots
                    // taken); say why instead of retrying blindly on the EOF.
                    SecureChannel.TYPE_DENY -> throw SecureChannel.parseDeny(frame.body)
                    // unknown frame types are ignored for forward compatibility
                }
            }
        } catch (e: Exception) {
            outcome = classify(pendingEnd.get() ?: e)
            if (running && outcome.reason != null) Log.w(TAG, "session ended: ${outcome.reason}", e)
        } finally {
            release()
            listener.onStopped(this, if (running) outcome.reason else null,
                outcome.final && running, outcome.retryAfterMs)
        }
    }

    /** Decide what an exception means for the user: message, and whether to retry. */
    private fun classify(e: Exception): Outcome = when {
        e is SessionEnd -> Outcome(e.message, e.final, e.retryAfterMs)
        e is SecureChannel.DeniedException -> Outcome(e.message, !e.retryable,
            if (e.retryable) e.retryAfterMs.takeIf { it > 0 } ?: DENIED_RETRY_MS else 0)
        e is SecureChannel.PairingException -> Outcome(e.message, true)
        SecureChannel.isCertificateMismatch(e) -> Outcome(CERT_CHANGED, true)
        // Only an answer that isn't TLS at all means "too old": a version
        // before 0.5 speaks plain text. Resets and EOFs are just a bad link.
        sender == null && SecureChannel.isNotTls(e) -> Outcome(OUTDATED_COMPUTER, true)
        e is SSLException && sender == null ->
            Outcome("couldn't set up the encrypted connection (${e.message})", false)
        e is SSLException -> Outcome("connection lost (${e.message})", false)
        e is SocketTimeoutException && sender != null ->
            Outcome("connection lost (no data for ${READ_TIMEOUT_MS / 1000}s)", false)
        else -> Outcome(e.message ?: e.javaClass.simpleName, false)
    }

    /** End the session from another thread with [end] as the reason. */
    private fun abort(end: SessionEnd) {
        pendingEnd.compareAndSet(null, end)
        try { socket?.close() } catch (_: Exception) {}
    }

    private fun release() {
        sender?.shutdown()
        try { socket?.close() } catch (_: Exception) {}
        // The feeder owns the codec: it releases it on its way out, before the
        // activity may destroy the Surface.
        feeder?.shutdownAndJoin()
        sender?.join(500)
        sender = null
    }

    // ---------------------------------------------------------------- connect

    /**
     * Race every candidate address (USB-tether IP, Wi-Fi IP, last address that
     * worked, ...) and keep the first one whose TLS certificate passes the pin.
     * A host that connects but fails TLS or the pin (a stale address now used
     * by something else) falls through to the next one instead of ending the
     * attempt. Every socket that loses the race is closed, including ones that
     * connect after we stopped waiting.
     */
    private fun connectAny(): Connected {
        val hosts = target.candidateHosts
        val connected = LinkedBlockingQueue<Pair<Int, Socket>>()
        val errors = arrayOfNulls<Exception>(hosts.size)
        val pending = AtomicInteger(hosts.size)
        val gaveUp = AtomicBoolean(false)

        hosts.forEachIndexed { i, host ->
            Thread({
                val s = Socket()
                try {
                    // DNS (for a typed hostname) can't be bounded here; the
                    // deadline below is, and a late result closes itself.
                    val address = InetAddress.getByName(host)
                    bindToNetwork(s, address)
                    s.tcpNoDelay = true
                    // DSCP AF41 (as the daemon marks video): Wi-Fi WMM sends
                    // touches in the video queue, not best effort. Ignored
                    // by networks that don't honour it.
                    try { s.trafficClass = DSCP_AF41 } catch (_: Exception) {}
                    s.connect(InetSocketAddress(address, port), CONNECT_TIMEOUT_MS)
                    val entry = i to s
                    connected.put(entry)
                    // Whoever removes the entry closes it: us here if the
                    // caller already gave up, otherwise the caller.
                    if (gaveUp.get() && connected.remove(entry)) closeQuietly(s)
                } catch (e: Exception) {
                    errors[i] = e
                    closeQuietly(s)
                } finally {
                    pending.decrementAndGet()
                }
            }, "connect-$host").apply { isDaemon = true }.start()
        }

        val deadline = SystemClock.elapsedRealtime() + CONNECT_TIMEOUT_MS + 500L
        try {
            while (running) {
                val next = connected.poll(50, TimeUnit.MILLISECONDS)
                if (next == null) {
                    if (pending.get() == 0 && connected.isEmpty()) break
                    if (SystemClock.elapsedRealtime() > deadline) break
                    continue
                }
                val (i, raw) = next
                try {
                    socket = raw   // so stop() can abort a handshake in progress
                    raw.soTimeout = HANDSHAKE_TIMEOUT_MS
                    val (tls, fingerprint) = SecureChannel.wrap(raw, hosts[i], port,
                        target.pinnedFingerprint)
                    return Connected(hosts[i], raw, tls, fingerprint)
                } catch (e: Exception) {
                    errors[i] = e
                    closeQuietly(raw)
                    if (running) Log.w(TAG, "${hosts[i]}: TLS failed, trying other addresses", e)
                }
            }
        } finally {
            gaveUp.set(true)
            while (true) closeQuietly((connected.poll() ?: break).second)
        }
        if (!running) throw IOException("stopped")
        throw connectFailure(hosts, errors)
    }

    /**
     * The most useful error from a failed race: a computer that answered
     * (TLS / certificate / pairing problem) beats "refused" (reachable, Tandem
     * not running), which beats a timeout. Ties go to the most preferred host.
     */
    private fun connectFailure(hosts: List<String>, errors: Array<Exception?>): Exception {
        errors.firstOrNull { it is SSLException || it is SecureChannel.PairingException }
            ?.let { return it }
        hosts.indices.firstOrNull { i ->
            val e = errors[i]
            e is ConnectException && "refused" in (e.message ?: "")
        }?.let { return IOException("connection refused by ${hosts[it]}") }
        val why = errors.firstOrNull { it != null }?.message ?: "timed out"
        return IOException("couldn't reach ${hosts.joinToString()} : $port ($why)")
    }

    /**
     * Bind to the network that holds [address] on its subnet. An unbound
     * socket follows the default network, which is mobile data when the Wi-Fi
     * (a travel router, a PC hotspot) has no internet, and the LAN address is
     * then unreachable. No match (USB tether, loopback): leave it unbound.
     */
    private fun bindToNetwork(s: Socket, address: InetAddress) {
        if (address.isLoopbackAddress) return
        val network = try { networkFor(address) } catch (_: Exception) { null } ?: return
        try { network.bindSocket(s) } catch (e: IOException) {
            Log.i(TAG, "couldn't bind to $network, using the default network", e)
        }
    }

    private val port get() = target.port

    // ------------------------------------------------------------- frames in

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

    /**
     * Watches how much received data is waiting unread (in the kernel and in
     * the TLS/stream buffers). Persistently more than ~0.4 s of video means
     * we're behind real time: skip what's queued and ask for a fresh keyframe
     * rather than replaying seconds-old frames with laggy touch.
     */
    private class BacklogMeter(private val buffered: InputStream, private val rawInput: InputStream?) {
        private var lastCheckNs = 0L
        private var overSinceNs = 0L
        private var skipping = false
        private var lastSkipNs = 0L
        private var windowStartNs = System.nanoTime()
        private var windowBytes = 0L
        private var measuredBytesPerSec = 0L

        /** True while NAL frames should be dropped unread-into-the-decoder. */
        fun shouldSkip(frameBytes: Int, config: ServerConfig?): Boolean {
            val now = System.nanoTime()
            measureRate(frameBytes, now)
            if (!skipping && now - lastCheckNs < CHECK_INTERVAL_NS) return false
            lastCheckNs = now
            val waiting = unreadBytes()
            val bytesPerSec = (config?.bitrateBps?.div(8)?.takeIf { it > 0 })
                ?: measuredBytesPerSec.takeIf { it > 0 } ?: DEFAULT_BYTES_PER_SEC
            val limit = maxOf(MIN_BACKLOG_BYTES, (bytesPerSec * BACKLOG_SECONDS).toLong())
            if (skipping) {
                // keep skipping until the old data has been read past
                if (waiting < limit / 2) skipping = false
                return skipping
            }
            if (waiting < limit) { overSinceNs = 0; return false }
            if (overSinceNs == 0L) overSinceNs = now
            // A single big keyframe in flight isn't a backlog; a sustained one
            // is. And don't loop: the replayed keyframe is itself a burst.
            if (now - overSinceNs < SUSTAIN_NS || now - lastSkipNs < MIN_SKIP_INTERVAL_NS) return false
            Log.w(TAG, "receive backlog ${waiting / 1024} KiB (limit ${limit / 1024}): skipping ahead")
            skipping = true
            overSinceNs = 0
            lastSkipNs = now
            return true
        }

        private fun measureRate(frameBytes: Int, now: Long) {
            windowBytes += frameBytes
            val elapsed = now - windowStartNs
            if (elapsed >= RATE_WINDOW_NS) {
                measuredBytesPerSec = windowBytes * 1_000_000_000L / elapsed
                windowBytes = 0
                windowStartNs = now
            }
        }

        private fun unreadBytes(): Long = try {
            buffered.available().toLong() + (rawInput?.available()?.toLong() ?: 0L)
        } catch (_: IOException) {
            0L
        }

        companion object {
            private const val CHECK_INTERVAL_NS = 50_000_000L
            private const val SUSTAIN_NS = 200_000_000L
            private const val MIN_SKIP_INTERVAL_NS = 3_000_000_000L
            private const val RATE_WINDOW_NS = 2_000_000_000L
            private const val MIN_BACKLOG_BYTES = 768L * 1024
            private const val BACKLOG_SECONDS = 0.4
            private const val DEFAULT_BYTES_PER_SEC = 12_000_000L / 8
        }
    }

    // ------------------------------------------------------------------ decode

    /** One received NAL waiting for the decode thread. */
    private class QueuedNal(val nal: ByteArray, val arrivedNs: Long)

    /**
     * The "stream-decode" thread: assembles NALs into access units, feeds the
     * decoder and recovers it locally (resync to the next IDR + KEYQ).
     */
    private inner class Feeder : Thread("stream-decode") {
        private val queue = LinkedBlockingQueue<QueuedNal>()
        private val queuedBytes = AtomicLong(0)
        @Volatile private var active = true

        private val assembler = AccessUnitAssembler()
        private var sps: ByteArray? = null
        private var pps: ByteArray? = null
        private var waitingForIdr = true
        private var resyncSinceMs = 0L
        private var lastCatchUpMs = 0L
        private var lagGraceUntilMs = 0L
        private var lastKeyframeAskMs = 0L
        private val codecError = AtomicReference<MediaCodec.CodecException?>(null)

        private lateinit var decoder: VideoDecoder

        fun offer(nal: ByteArray) {
            if (queuedBytes.addAndGet(nal.size.toLong()) > MAX_QUEUED_BYTES) {
                // The decoder isn't keeping up at all: don't grow without bound.
                // The stale data was all in this queue (nothing left in the
                // socket to read past), so the keyframe can be asked for now.
                skipBacklog()
                sender?.requestKeyframe()
                return
            }
            queue.put(QueuedNal(nal, System.nanoTime()))
        }

        /** The reader is skipping a receive backlog: drop what's queued, resync. */
        fun skipBacklog() {
            queue.clear()
            queuedBytes.set(0)
            queue.put(DISCONTINUITY)
        }

        fun shutdownAndJoin() {
            active = false
            queue.offer(DISCONTINUITY)   // wake it
            try { join(1500) } catch (_: InterruptedException) {}
        }

        override fun run() {
            raiseThreadPriority(Process.THREAD_PRIORITY_URGENT_DISPLAY)
            decoder = VideoDecoder(surface, object : VideoDecoder.Events {
                override fun onFrameRendered() {
                    if (!firstFrameShown) {
                        firstFrameShown = true
                        listener.onFirstFrame(this@StreamSession)
                    }
                }
                override fun onPictureSize(width: Int, height: Int) =
                    listener.onStreamInfo(this@StreamSession, width, height)
                override fun onCodecError(e: MediaCodec.CodecException) {
                    codecError.set(e)
                }
            })
            try {
                while (active && running) {
                    // In multi-slice streams wait briefly for the next slice;
                    // otherwise wake regularly for the health checks.
                    val waitMs = if (assembler.hasPicture) SLICE_WAIT_MS else HEALTH_POLL_MS
                    val item = queue.poll(waitMs, TimeUnit.MILLISECONDS)
                    checkHealth()
                    when {
                        item == null -> assembler.take()?.let { submit(it) }
                        item === DISCONTINUITY -> if (active) {
                            // the reader asks for the keyframe when it's done skipping
                            assembler.clear()
                            startResync("skipping a receive backlog", askForKeyframe = false)
                        }
                        else -> {
                            queuedBytes.addAndGet(-item.nal.size.toLong())
                            if (isLagging(item)) catchUp() else process(item.nal)
                        }
                    }
                }
            } catch (e: IOException) {
                abort(e as? SessionEnd ?: SessionEnd(e.message ?: "video decoder error", final = false))
            } catch (e: Exception) {
                Log.e(TAG, "decode thread failed", e)
                abort(SessionEnd("video decoder error", final = false))
            } finally {
                decoder.shutdown()
            }
        }

        private fun process(nal: ByteArray) {
            val type = H264.type(nal)
            when (type) {
                H264.NAL_SPS -> sps = nal
                H264.NAL_PPS -> pps = nal
            }
            assembler.add(nal, type)?.let { submit(it) }
            // A single-slice picture is complete the moment its slice arrives;
            // don't wait for the next frame to find that out. With more NALs
            // already queued, the next one decides exactly.
            if (H264.isVcl(type) && assembler.picturesAreSingleSlice && queue.isEmpty()) {
                assembler.take()?.let { submit(it) }
            }
        }

        private fun submit(au: AccessUnit) {
            if (waitingForIdr && !au.keyframe) return   // useless without its reference
            if (au.keyframe) {
                val sp = sps ?: return
                val pp = pps ?: return
                // A different SPS means the resolution/profile changed.
                if (decoder.isConfigured && !sp.contentEquals(decoder.configuredSps)) decoder.release()
                if (!decoder.isConfigured) {
                    val cfg = serverConfig
                    decoder.configure(sp, pp, cfg?.width ?: 0, cfg?.height ?: 0, cfg?.fps ?: 0)
                    lagGraceUntilMs = SystemClock.elapsedRealtime() + LAG_GRACE_AFTER_CONFIGURE_MS
                }
                if (waitingForIdr) {
                    Log.i(TAG, "resynced at a keyframe")
                    // The daemon answers KEYQ by replaying the GOP so far as one
                    // burst; decoding it briefly queues up. That isn't lag.
                    lagGraceUntilMs = maxOf(lagGraceUntilMs,
                        SystemClock.elapsedRealtime() + LAG_GRACE_AFTER_CONFIGURE_MS)
                }
                waitingForIdr = false
                resyncSinceMs = 0
            }
            if (!decoder.isConfigured) return
            val result = try {
                decoder.queue(au, INPUT_WAIT_MS)
            } catch (e: IllegalStateException) {   // CodecException included
                resetDecoder("the decoder rejected input (${e.message})")
                return
            }
            when (result) {
                VideoDecoder.QueueResult.QUEUED -> {}
                VideoDecoder.QueueResult.NO_INPUT_BUFFER ->
                    resetDecoder("no free decoder input buffer for $INPUT_WAIT_MS ms")
                // Not the decoder's fault: it is recreated with bigger buffers.
                VideoDecoder.QueueResult.TOO_BIG -> resetDecoder(
                    "access unit of ${au.size} B didn't fit the decoder's input buffer",
                    blameDecoder = false)
            }
        }

        /** Codec errors (from the callback thread), the stall watchdog, resync timeout. */
        private fun checkHealth() {
            codecError.getAndSet(null)?.let { e ->
                if (e.isTransient) {
                    // The codec is still fine; only the picture may be damaged.
                    Log.w(TAG, "transient decoder error: ${e.diagnosticInfo}")
                    startResync("transient decoder error")
                } else {
                    Log.w(TAG, "decoder error (recoverable=${e.isRecoverable}): ${e.diagnosticInfo}", e)
                    resetDecoder("decoder error")
                }
            }
            if (decoder.isConfigured && decoder.stalledMs() > STALL_MS) {
                resetDecoder("decoder produced no frame for $STALL_MS ms")
            }
            // Keep asking while waiting: a KEYQ can be swallowed by the daemon's
            // own rate limit, or its replay skipped along with a backlog.
            if (waitingForIdr && resyncSinceMs != 0L && !skippingBacklog &&
                SystemClock.elapsedRealtime() - lastKeyframeAskMs >= KEYFRAME_REASK_MS) {
                askForKeyframe()
            }
            // A daemon too old for KEYQ (or a backend with no keyframe source)
            // may not send an IDR for a long time on a static desktop. A
            // reconnect always starts with one, so fall back to that.
            if (waitingForIdr && firstFrameShown && resyncSinceMs != 0L &&
                SystemClock.elapsedRealtime() - resyncSinceMs > RESYNC_TIMEOUT_MS) {
                throw SessionEnd("lost the picture; reconnecting for a fresh one", final = false)
            }
        }

        /** The decoder is behind the network: the oldest queued NAL is stale. */
        private fun isLagging(item: QueuedNal): Boolean {
            if (waitingForIdr || !firstFrameShown) return false
            if ((System.nanoTime() - item.arrivedNs) / 1_000_000 < MAX_DECODE_LAG_MS) return false
            val now = SystemClock.elapsedRealtime()
            return now > lagGraceUntilMs && now - lastCatchUpMs > MIN_CATCH_UP_INTERVAL_MS
        }

        private fun catchUp() {
            lastCatchUpMs = SystemClock.elapsedRealtime()
            queue.clear()
            queuedBytes.set(0)
            assembler.clear()
            startResync("decoder fell more than $MAX_DECODE_LAG_MS ms behind")
        }

        /** Throw the codec away; the next keyframe recreates it. */
        private fun resetDecoder(why: String, blameDecoder: Boolean = true) {
            Log.w(TAG, "resetting the decoder: $why")
            val producedNothing = decoder.framesRendered == 0L
            decoder.release()
            // A decoder that never produced a frame for this stream is broken
            // for it (capability limits, vendor bugs): try the next one.
            if (blameDecoder && producedNothing) decoder.preferNextDecoder()
            startResync(why)
        }

        private fun startResync(why: String, askForKeyframe: Boolean = true) {
            if (!waitingForIdr) Log.i(TAG, "resync: $why")
            waitingForIdr = true
            if (resyncSinceMs == 0L) resyncSinceMs = SystemClock.elapsedRealtime()
            if (askForKeyframe) askForKeyframe()
        }

        private fun askForKeyframe() {
            lastKeyframeAskMs = SystemClock.elapsedRealtime()
            sender?.requestKeyframe()
        }
    }

    // ------------------------------------------------------------------- send

    /**
     * The "stream-sender" thread: everything the phone sends after pairing.
     * Pending frames go out together in one TLS write. Touch moves are
     * coalesced, so a link hiccup can't build a backlog that later replays an
     * old drag path. An empty PING after 1 s of silence lets the computer
     * notice a phone that vanished (daemons that predate it ignore it).
     */
    private inner class Sender(private val out: FrameWriter) : Thread("stream-sender") {
        private val lock = Object()
        private val touches = TouchQueue()
        private var keyframeWanted = false
        private var lastKeyframeRequestMs = 0L
        @Volatile private var active = true

        fun queueTouches(points: List<TouchPoint>) = synchronized(lock) {
            if (!active) return
            for (p in points) touches.add(p)
            lock.notifyAll()
        }

        /**
         * Ask the computer for a keyframe. Requests merge, and at most one goes
         * out per [KEYFRAME_REQUEST_INTERVAL_MS]: one made sooner is delayed,
         * not dropped (dropping lost the request that mattered, e.g. the one
         * at the end of a skipped backlog right after a catch-up's).
         */
        fun requestKeyframe() = synchronized(lock) {
            keyframeWanted = true
            lock.notifyAll()
        }

        /** How long until a wanted KEYQ may go out (0 = now). Call with [lock] held. */
        private fun keyframeDelayMs(now: Long): Long =
            if (!keyframeWanted) Long.MAX_VALUE
            else (lastKeyframeRequestMs + KEYFRAME_REQUEST_INTERVAL_MS - now).coerceAtLeast(0)

        fun shutdown() = synchronized(lock) {
            active = false
            lock.notifyAll()
        }

        override fun run() {
            var lastSendMs = SystemClock.elapsedRealtime()
            try {
                while (active) {
                    val frames = ArrayList<Pair<String, ByteArray>>()
                    synchronized(lock) {
                        while (active && touches.isEmpty()) {
                            val now = SystemClock.elapsedRealtime()
                            val keyframeIn = keyframeDelayMs(now)
                            if (keyframeIn == 0L) break
                            val heartbeatIn = HEARTBEAT_MS - (now - lastSendMs)
                            if (heartbeatIn <= 0) break
                            lock.wait(minOf(keyframeIn, heartbeatIn))
                        }
                        val now = SystemClock.elapsedRealtime()
                        if (keyframeDelayMs(now) == 0L) {
                            frames += FrameWriter.TYPE_KEYFRAME_REQUEST to EMPTY
                            keyframeWanted = false
                            lastKeyframeRequestMs = now
                        }
                        touches.drainTo(frames)
                    }
                    if (!active) break
                    if (frames.isEmpty()) frames += FrameWriter.TYPE_PING to EMPTY
                    out.sendAll(frames)
                    lastSendMs = SystemClock.elapsedRealtime()
                }
            } catch (_: IOException) {
                // the reader notices a dead connection and ends the session
            } catch (_: InterruptedException) {
            }
        }
    }

    companion object {
        private const val TAG = "StreamSession"
        private const val CONNECT_TIMEOUT_MS = 3000
        /** The daemon PINGs after 1 s of silence, so 4 s without data is a dead link. */
        private const val READ_TIMEOUT_MS = 4000
        private const val HANDSHAKE_TIMEOUT_MS = 5000
        private const val HEARTBEAT_MS = 1000L
        private const val DSCP_AF41 = 0x88

        /** How long to wait for another slice of the current picture (multi-slice streams). */
        private const val SLICE_WAIT_MS = 8L
        private const val HEALTH_POLL_MS = 100L
        /** Longest wait for a free decoder input buffer before the decoder counts as wedged. */
        private const val INPUT_WAIT_MS = 500L
        /** Input queued but no frame out for this long: the decoder is stuck. */
        private const val STALL_MS = 2000L
        /** Waiting for a keyframe this long after a resync: reconnect instead. */
        private const val RESYNC_TIMEOUT_MS = 4000L
        private const val MAX_DECODE_LAG_MS = 250L
        private const val MIN_CATCH_UP_INTERVAL_MS = 3000L
        private const val MAX_QUEUED_BYTES = 16L * 1024 * 1024
        /** At most this often (the daemon rate-limits KEYQ too). */
        private const val KEYFRAME_REQUEST_INTERVAL_MS = 500L
        /** Still waiting for a keyframe after a KEYQ: ask again this often. */
        private const val KEYFRAME_REASK_MS = 1500L
        /** Retry delay after a temporary DENY that gave no hint of its own. */
        private const val DENIED_RETRY_MS = 5000L
        /** After (re)configuring, the queue backs up while the codec starts: not lag. */
        private const val LAG_GRACE_AFTER_CONFIGURE_MS = 1000L

        private val EMPTY = ByteArray(0)
        private val DISCONTINUITY = QueuedNal(ByteArray(0), 0)

        const val CERT_CHANGED = "This isn't the computer you paired with: its security " +
            "certificate is different. If you reinstalled Tandem there, scan its QR code again."
        const val OUTDATED_COMPUTER = "Couldn't set up an encrypted connection. Update Tandem " +
            "on the computer (re-run the install command), then scan its QR code again."

        fun unsupportedCodec(codec: String) = "The computer streams $codec video, but this app " +
            "decodes H.264 only. Set the codec to h264 in tandem on the computer."

        // TUCH actions (mirrored in linux/phone_input.py)
        const val TOUCH_DOWN = 0
        const val TOUCH_MOVE = 1
        const val TOUCH_UP = 2
        const val TOUCH_CANCEL = 3

        // Not named setPriority: inside a Thread subclass that name resolves to
        // Thread.setPriority (Java range 1-10), which throws on these Linux nice values.
        private fun raiseThreadPriority(priority: Int) {
            try { Process.setThreadPriority(priority) } catch (e: Exception) {
                Log.i(TAG, "couldn't raise thread priority", e)
            }
        }

        private fun closeQuietly(s: Socket?) {
            try { s?.close() } catch (_: Exception) {}
        }
    }
}

// ============================================================== H.264 helpers

/** Just enough H.264 (Annex-B) parsing to cut access units and size the decoder. */
internal object H264 {
    const val NAL_IDR = 5
    const val NAL_SPS = 7
    const val NAL_PPS = 8

    /** Index of the NAL header byte, just past the 00 00 01 / 00 00 00 01 start code; -1 if none. */
    fun headerIndex(nal: ByteArray): Int {
        var i = 0
        while (i < nal.size && nal[i].toInt() == 0) i++
        return if (i >= 2 && i < nal.size - 1 && nal[i].toInt() == 1) i + 1 else -1
    }

    fun type(nal: ByteArray): Int {
        val h = headerIndex(nal)
        return if (h < 0) -1 else nal[h].toInt() and 0x1F
    }

    /** A coded slice (part of a picture). */
    fun isVcl(type: Int) = type in 1..5

    /** Non-VCL types that, after a picture's slices, begin the next access unit (spec 7.4.1.2.3). */
    fun beginsAccessUnit(type: Int) = type in 6..9 || type in 14..18

    /** first_mb_in_slice of a slice NAL: 0 = a new picture starts here; -1 if unreadable. */
    fun firstMbInSlice(nal: ByteArray): Int {
        val h = headerIndex(nal)
        if (h < 0) return -1
        return try { BitReader(nal, h + 1).ue() } catch (_: IllegalStateException) { -1 }
    }

    /**
     * The coded picture size (whole macroblocks, before cropping) from an SPS
     * NAL, or null if it can't be parsed. Used to size decoder input buffers
     * from the stream itself; the banner can lag a live resolution change.
     */
    fun codedSize(sps: ByteArray): Pair<Int, Int>? {
        val h = headerIndex(sps)
        if (h < 0) return null
        return try {
            val r = BitReader(sps, h + 1)
            val profile = r.u(8)
            r.u(8); r.u(8)            // constraint flags, level
            r.ue()                     // seq_parameter_set_id
            if (profile in HIGH_PROFILES) {
                val chroma = r.ue()
                if (chroma == 3) r.u(1)            // separate_colour_plane_flag
                r.ue(); r.ue(); r.u(1)             // bit depths, qpprime bypass
                if (r.u(1) == 1) {                 // seq_scaling_matrix_present_flag
                    for (i in 0 until if (chroma == 3) 12 else 8) {
                        if (r.u(1) == 1) skipScalingList(r, if (i < 6) 16 else 64)
                    }
                }
            }
            r.ue()                                 // log2_max_frame_num_minus4
            when (r.ue()) {                        // pic_order_cnt_type
                0 -> r.ue()
                1 -> {
                    r.u(1); r.se(); r.se()
                    repeat(r.ue()) { r.se() }
                }
            }
            r.ue(); r.u(1)                         // max_num_ref_frames, gaps flag
            val widthMbs = r.ue() + 1
            val heightUnits = r.ue() + 1
            val frameMbsOnly = r.u(1)
            (widthMbs * 16) to ((2 - frameMbsOnly) * heightUnits * 16)
        } catch (_: IllegalStateException) {
            null
        }
    }

    private fun skipScalingList(r: BitReader, size: Int) {
        var last = 8
        var next = 8
        repeat(size) {
            if (next != 0) next = (last + r.se() + 256) % 256
            if (next != 0) last = next
        }
    }

    private val HIGH_PROFILES = setOf(100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)

    /** Reads RBSP bits, skipping emulation-prevention bytes (00 00 03 -> 00 00). */
    private class BitReader(private val data: ByteArray, private var pos: Int) {
        private var zeros = 0
        private var current = 0
        private var bitsLeft = 0

        private fun nextByte(): Int {
            while (true) {
                check(pos < data.size) { "end of NAL" }
                val b = data[pos++].toInt() and 0xFF
                if (zeros >= 2 && b == 3) { zeros = 0; continue }
                zeros = if (b == 0) zeros + 1 else 0
                return b
            }
        }

        fun u(bits: Int): Int {
            var v = 0
            repeat(bits) {
                if (bitsLeft == 0) { current = nextByte(); bitsLeft = 8 }
                bitsLeft--
                v = (v shl 1) or ((current shr bitsLeft) and 1)
            }
            return v
        }

        /** Exp-Golomb unsigned. */
        fun ue(): Int {
            var leadingZeros = 0
            while (u(1) == 0) {
                leadingZeros++
                check(leadingZeros <= 31) { "bad exp-Golomb code" }
            }
            return ((1 shl leadingZeros) - 1) + u(leadingZeros)
        }

        /** Exp-Golomb signed. */
        fun se(): Int {
            val k = ue()
            return if (k and 1 == 1) (k + 1) / 2 else -(k / 2)
        }
    }
}

/** One picture's NALs, queued into the decoder as a single input buffer. */
internal class AccessUnit(val nals: List<ByteArray>, val size: Int, val keyframe: Boolean)

/**
 * Groups NALs (the daemon sends one per frame) into access units. Hardware
 * decoders such as Qualcomm C2 need a whole picture per input buffer, and a
 * picture can be several slices (x264 sliced-threads). A new access unit
 * starts at an AUD/SPS/PPS/SEI after a slice, or at a slice whose
 * first_mb_in_slice is 0, never at every slice.
 */
internal class AccessUnitAssembler {
    private val nals = ArrayList<ByteArray>()
    private var size = 0
    private var keyframe = false

    /** The pending access unit holds at least one slice. */
    var hasPicture = false
        private set

    private var slices = 0
    private var multiSlice = false
    private var singleSliceSeen = false

    /**
     * A picture is known to be one slice, so it is complete as soon as its
     * slice arrives. Until a whole picture has been seen (or once one came in
     * several slices) the end of a picture is only known from the next NAL.
     */
    val picturesAreSingleSlice: Boolean get() = singleSliceSeen && !multiSlice

    /** Add [nal]; returns the previous access unit if [nal] begins the next one. */
    fun add(nal: ByteArray, type: Int): AccessUnit? {
        var completed: AccessUnit? = null
        if (H264.isVcl(type)) {
            val firstMb = H264.firstMbInSlice(nal)
            if (firstMb > 0) multiSlice = true
            if (hasPicture && firstMb <= 0) completed = take()
            hasPicture = true
            slices++
        } else if (hasPicture && H264.beginsAccessUnit(type)) {
            completed = take()
        }
        nals += nal
        size += nal.size
        if (type == H264.NAL_IDR) keyframe = true
        return completed
    }

    /** The pending access unit, if it holds a picture; resets for the next one. */
    fun take(): AccessUnit? {
        if (!hasPicture) return null
        if (slices == 1) singleSliceSeen = true
        val au = AccessUnit(ArrayList(nals), size, keyframe)
        clear()
        return au
    }

    fun clear() {
        nals.clear()
        size = 0
        slices = 0
        keyframe = false
        hasPicture = false
    }
}

// =================================================================== decoder

/**
 * The H.264 decoder, in MediaCodec's asynchronous mode: decoded frames are
 * released to the Surface from the callback thread the moment they exist,
 * independent of when the next input arrives.
 *
 * Portability: the decoder is chosen from MediaCodecList (ones that report
 * support for the stream's size first, then hardware, then ones that also
 * report the frame rate, then low-latency capable), and each configuration is tried with the low-latency
 * extras, then without (some vendors reject unknown keys), then on the next
 * decoder. Only [configure], [queue], [release] and friends are called, all
 * from one thread (the decode thread); callbacks run on their own thread.
 */
internal class VideoDecoder(private val surface: Surface, private val events: Events) {

    interface Events {
        fun onFrameRendered()
        fun onPictureSize(width: Int, height: Int)
        fun onCodecError(e: MediaCodec.CodecException)
    }

    enum class QueueResult { QUEUED, NO_INPUT_BUFFER, TOO_BIG }

    private val callbackThread =
        HandlerThread("stream-decoder", Process.THREAD_PRIORITY_URGENT_DISPLAY).apply { start() }
    private val handler = Handler(callbackThread.looper)

    /** Current codec; callbacks from any other (released) instance are ignored. */
    @Volatile private var codec: MediaCodec? = null
    private val freeInputs = LinkedBlockingQueue<Int>()

    var configuredSps: ByteArray? = null
        private set
    val isConfigured: Boolean get() = codec != null

    private val rendered = AtomicLong(0)
    /** Frames rendered by the current codec instance. */
    val framesRendered: Long get() = rendered.get()

    @Volatile private var lastOutputNs = 0L
    private var firstUnansweredInputNs = 0L
    private var unansweredInputs = 0
    private var lastInputNs = 0L
    private var lastPtsUs = 0L

    private var candidates: List<MediaCodecInfo> = emptyList()
    private var candidatesFor = ""
    private var candidateIndex = 0
    /** Raised after an access unit didn't fit, so the next configure asks for more. */
    private var minInputSize = 0

    private val callback = object : MediaCodec.Callback() {
        override fun onInputBufferAvailable(mc: MediaCodec, index: Int) {
            if (mc === codec) freeInputs.offer(index)
        }

        override fun onOutputBufferAvailable(mc: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
            if (mc !== codec) return
            try {
                // render = true: shown at the next vsync. The input PTS is the
                // monotonic clock at queue time, so it is never in the future.
                mc.releaseOutputBuffer(index, true)
            } catch (_: IllegalStateException) {
                return   // released meanwhile
            }
            lastOutputNs = System.nanoTime()
            rendered.incrementAndGet()
            events.onFrameRendered()
        }

        override fun onError(mc: MediaCodec, e: MediaCodec.CodecException) {
            if (mc === codec) events.onCodecError(e)
        }

        override fun onOutputFormatChanged(mc: MediaCodec, format: MediaFormat) {
            if (mc !== codec) return
            try {
                // crop is the real picture (1080 vs the padded 1088)
                val w = if (format.containsKey("crop-right"))
                    format.getInteger("crop-right") - format.getInteger("crop-left") + 1
                else format.getInteger(MediaFormat.KEY_WIDTH)
                val h = if (format.containsKey("crop-bottom"))
                    format.getInteger("crop-bottom") - format.getInteger("crop-top") + 1
                else format.getInteger(MediaFormat.KEY_HEIGHT)
                events.onPictureSize(w, h)
            } catch (_: Exception) {
            }
        }
    }

    /**
     * Create and start a decoder for this SPS/PPS. [hintWidth]/[hintHeight]/
     * [fps] come from the banner (0 = unknown). Throws IOException when no
     * decoder on the phone accepts the stream.
     */
    fun configure(sps: ByteArray, pps: ByteArray, hintWidth: Int, hintHeight: Int, fps: Int) {
        release()
        val (spsW, spsH) = H264.codedSize(sps) ?: (0 to 0)
        val w = spsW.takeIf { it > 0 } ?: hintWidth.takeIf { it > 0 } ?: 1920
        val h = spsH.takeIf { it > 0 } ?: hintHeight.takeIf { it > 0 } ?: 1080
        val key = "${w}x$h@$fps"
        if (key != candidatesFor) {
            candidates = chooseDecoders(w, h, fps)
            candidatesFor = key
            candidateIndex = 0
        }
        if (candidates.isEmpty()) throw IOException("this phone has no H.264 video decoder")

        var lastError: Exception? = null
        for (attempt in candidates.indices) {
            val index = (candidateIndex + attempt) % candidates.size
            val info = candidates[index]
            for (tuned in booleanArrayOf(true, false)) {
                var c: MediaCodec? = null
                try {
                    c = MediaCodec.createByCodecName(info.name)
                    c.setCallback(callback, handler)
                    c.configure(format(info, c, sps, pps, w, h, fps, tuned), surface, null, 0)
                    freeInputs.clear()
                    rendered.set(0)
                    firstUnansweredInputNs = 0
                    unansweredInputs = 0
                    lastOutputNs = 0
                    codec = c      // before start(): its first callbacks must count
                    c.start()
                    candidateIndex = index
                    configuredSps = sps
                    Log.i(TAG, "decoder ${info.name} configured for ${w}x$h" +
                        (if (fps > 0) "@$fps" else "") + (if (tuned) "" else " (plain settings)"))
                    return
                } catch (e: Exception) {
                    // IllegalArgumentException / IllegalStateException / CodecException / IOException
                    lastError = e
                    codec = null
                    Log.w(TAG, "decoder ${info.name} (${if (tuned) "tuned" else "plain"}) failed", e)
                    try { c?.release() } catch (_: Exception) {}
                }
            }
        }
        throw IOException("no video decoder on this phone accepted ${w}x$h", lastError)
    }

    /** The next configure starts with the next candidate decoder. */
    fun preferNextDecoder() {
        if (candidates.size > 1) candidateIndex = (candidateIndex + 1) % candidates.size
    }

    /** Queue one access unit, waiting up to [waitMs] for a free input buffer. */
    fun queue(au: AccessUnit, waitMs: Long): QueueResult {
        val c = codec ?: return QueueResult.QUEUED
        val index = freeInputs.poll(waitMs, TimeUnit.MILLISECONDS) ?: return QueueResult.NO_INPUT_BUFFER
        val buf = c.getInputBuffer(index)
        if (buf == null || au.size > buf.capacity()) {
            // Give the index back (empty), or the decoder loses a slot for good.
            c.queueInputBuffer(index, 0, 0, nextPtsUs(), 0)
            if (buf == null) return QueueResult.NO_INPUT_BUFFER
            Log.w(TAG, "access unit ${au.size}B > input buffer ${buf.capacity()}B")
            minInputSize = maxOf(minInputSize, au.size + au.size / 4)
            return QueueResult.TOO_BIG
        }
        buf.clear()
        for (nal in au.nals) buf.put(nal)
        // Note the time before queueing, so a quick output can't predate it.
        val now = System.nanoTime()
        if (lastInputNs <= lastOutputNs || firstUnansweredInputNs == 0L) {
            firstUnansweredInputNs = now
            unansweredInputs = 0
        }
        unansweredInputs++
        lastInputNs = now
        c.queueInputBuffer(index, 0, au.size, nextPtsUs(), 0)
        return QueueResult.QUEUED
    }

    /**
     * How long input has kept flowing without any frame coming out (0 = fine).
     * Some decoders legitimately hold a few frames (reorder buffer) until more
     * input arrives, so a handful of unanswered inputs on a static desktop is
     * normal; more than any reorder buffer holds is not.
     */
    fun stalledMs(): Long {
        val since = firstUnansweredInputNs
        if (since == 0L || lastOutputNs >= since || unansweredInputs < STALL_MIN_INPUTS) return 0
        return (System.nanoTime() - since) / 1_000_000
    }

    /** Monotonic, strictly increasing: the platform reads it as the render time. */
    private fun nextPtsUs(): Long {
        lastPtsUs = maxOf(System.nanoTime() / 1000, lastPtsUs + 1)
        return lastPtsUs
    }

    fun release() {
        val c = codec ?: return
        codec = null           // first: late callbacks from it are now ignored
        configuredSps = null
        freeInputs.clear()
        try { c.stop() } catch (_: Exception) {}
        try { c.release() } catch (_: Exception) {}
    }

    fun shutdown() {
        release()
        callbackThread.quitSafely()
    }

    private fun format(info: MediaCodecInfo, c: MediaCodec, sps: ByteArray, pps: ByteArray,
                       w: Int, h: Int, fps: Int, tuned: Boolean): MediaFormat {
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h)
        fmt.setByteBuffer("csd-0", ByteBuffer.wrap(sps))
        fmt.setByteBuffer("csd-1", ByteBuffer.wrap(pps))
        val pixels = w.toLong() * h
        // A keyframe can approach a raw frame (w*h*3/2) at high quality; ask
        // for that, bounded. The plain settings keep what always worked.
        val inputSize = if (tuned) maxOf(pixels * 3 / 2, minInputSize.toLong(), 1L shl 20)
                        else maxOf(pixels, minInputSize.toLong(), 1L shl 20)
        fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, inputSize.coerceAtMost(MAX_INPUT_BYTES).toInt())
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        fmt.setInteger(MediaFormat.KEY_PRIORITY, 0)          // realtime
        if (!tuned) return fmt

        if (fps > 0) fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, fps)
        for ((name, value) in vendorLowLatencyKeys(info, c)) fmt.setInteger(name, value)
        return fmt
    }

    /**
     * Vendor low-latency switches (Android 8-10 phones, and decoders that
     * ignore KEY_LOW_LATENCY). On Android 12+ only the ones the codec lists
     * as supported; before that by vendor, as the codec name identifies it.
     */
    private fun vendorLowLatencyKeys(info: MediaCodecInfo, c: MediaCodec): Map<String, Int> {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val supported = try { c.supportedVendorParameters.toSet() } catch (_: Exception) { emptySet() }
            return VENDOR_KEYS.values.fold(emptyMap<String, Int>()) { acc, keys -> acc + keys }
                .filterKeys { it in supported || it.removePrefix("vendor.") in supported }
        }
        val name = info.name.lowercase()
        return VENDOR_KEYS.entries.firstOrNull { (prefixes, _) ->
            prefixes.split(',').any { name.startsWith(it) }
        }?.value ?: emptyMap()
    }

    /** AVC decoders, best first; a stable sort keeps the platform's own preference otherwise. */
    private fun chooseDecoders(w: Int, h: Int, fps: Int): List<MediaCodecInfo> {
        val all = try { MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos.toList() }
                  catch (e: Exception) { Log.w(TAG, "codec list unavailable", e); emptyList() }
        val avc = all.filter { info ->
            !info.isEncoder &&
                info.supportedTypes.any { it.equals(MediaFormat.MIMETYPE_VIDEO_AVC, ignoreCase = true) } &&
                !info.name.endsWith(".secure") &&
                !(Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && info.isAlias)
        }
        fun caps(info: MediaCodecInfo) =
            try { info.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC) } catch (_: Exception) { null }
        // Size is a hard limit; the advertised rate is not (hardware decoders
        // often under-report it, while the software one claims nearly any
        // rate). So a hardware decoder that fits the size beats a software one
        // that "fits" the rate too.
        fun fits(info: MediaCodecInfo, withRate: Boolean): Boolean {
            val v = caps(info)?.videoCapabilities ?: return false
            fun ok(a: Int, b: Int) = try {
                if (withRate && fps > 0) v.areSizeAndRateSupported(a, b, fps.toDouble())
                else v.isSizeSupported(a, b)
            } catch (_: Exception) { false }
            return ok(w, h) || ok(h, w)      // portrait monitors too
        }
        fun hardware(info: MediaCodecInfo) =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) info.isHardwareAccelerated && !info.isSoftwareOnly
            else SOFTWARE_PREFIXES.none { info.name.lowercase().startsWith(it) } &&
                ".sw." !in info.name.lowercase()
        fun lowLatency(info: MediaCodecInfo) = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R &&
            caps(info)?.isFeatureSupported(MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency) == true

        val ranked = avc.sortedWith(compareByDescending<MediaCodecInfo> { fits(it, withRate = false) }
            .thenByDescending { hardware(it) }
            .thenByDescending { fits(it, withRate = true) }
            .thenByDescending { lowLatency(it) })
        Log.i(TAG, "decoders for ${w}x$h@$fps: ${ranked.joinToString { it.name }}")
        return ranked
    }

    companion object {
        private const val TAG = "VideoDecoder"
        private const val MAX_INPUT_BYTES = 24L * 1024 * 1024
        /** More than the largest H.264 reorder buffer (16 frames). */
        private const val STALL_MIN_INPUTS = 20
        private val SOFTWARE_PREFIXES = listOf("omx.google.", "c2.android.", "omx.ffmpeg.")

        /** Codec-name prefixes -> that vendor's low-latency format keys. */
        private val VENDOR_KEYS = mapOf(
            "omx.qcom.,c2.qti." to mapOf("vendor.qti-ext-dec-low-latency.enable" to 1),
            "omx.exynos.,c2.exynos." to mapOf("vendor.rtc-ext-dec-low-latency.enable" to 1),
            "omx.amlogic.,c2.amlogic." to mapOf("vendor.low-latency.enable" to 1),
            "omx.hisi." to mapOf(
                "vendor.hisi-ext-low-latency-video-dec.video-scene-for-low-latency-req" to 1,
                "vendor.hisi-ext-low-latency-video-dec.video-scene-for-low-latency-rdy" to -1,
            ),
        )
    }
}
