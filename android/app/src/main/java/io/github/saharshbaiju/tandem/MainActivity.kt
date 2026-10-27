package io.github.saharshbaiju.tandem

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.MotionEvent
import android.view.SurfaceHolder
import android.view.View
import android.view.WindowManager
import android.view.animation.PathInterpolator
import android.util.Log
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import io.github.saharshbaiju.tandem.databinding.ActivityMainBinding
import com.google.android.material.color.DynamicColors
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning

/**
 * Connect screen (Material 3) -> water-ripple transition -> full-bleed desktop.
 *
 *  IDLE        connect card: scan the TUI's QR (or type an address), Connect.
 *  CONNECTING  ripples spread from where you tapped while we connect.
 *  LIVE        first frame decoded -> the connect screen dissolves like water
 *              into the video. Chrome is just a fading status pill and a slim
 *              edge handle that opens a side sheet (details + Disconnect).
 *  RECONNECTING  the link dropped after working; retries with backoff.
 *
 * Touch: while LIVE, and when the computer has touch input on, every touch
 * on the video goes to the computer (see [TouchForwarder]). Without touch, a
 * tap on the video just peeks the status pill.
 *
 * Security: a computer is only ever connected to with its pairing key (and,
 * from a QR, its certificate fingerprint); see [SecureChannel]. A link from
 * outside the app (camera, browser, another app) only proposes a computer:
 * the user confirms it before it replaces the saved one.
 */
class MainActivity : AppCompatActivity(), SurfaceHolder.Callback, StreamSession.Listener {

    private lateinit var b: ActivityMainBinding
    private val ui = Handler(Looper.getMainLooper())
    private val prefs by lazy { getSharedPreferences("tandem", Context.MODE_PRIVATE) }

    private enum class State { IDLE, CONNECTING, LIVE, RECONNECTING }

    private var state = State.IDLE
    private var target: ConnectTarget? = null
    private var session: StreamSession? = null
    private var surfaceReady = false
    private var wantConnected = false
    private var everStreamed = false       // reconnect only after a stream worked
    private var reconnectAttempt = 0
    private var rippleStartedAt = 0L
    private var lastTouchX = -1f
    private var lastTouchY = -1f
    private var sheetOpen = false

    private var streamSize = ""
    private var serverConfig: ServerConfig? = null
    private var touchAllowed = false       // the computer accepted our pairing key
    private var touchRefusedReason = ""    // why not, shown in the side sheet
    private val touchForwarder = TouchForwarder { action, finger, x, y ->
        session?.sendTouch(action, finger, x, y)
    }
    private var linkHost = ""
    private var wifiLock: WifiManager.WifiLock? = null

    private val autoHide = Runnable { fadeChrome() }
    private val reconnect = Runnable { launchSession() }
    private val noVideoWatchdog = Runnable {
        if (state == State.CONNECTING && session != null) {
            failConnect("Connected to ${target?.title}, but no video arrived. " +
                "Check the terminal on your computer for errors.")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        if (!DynamicColors.isDynamicColorAvailable()) {
            theme.applyStyle(R.style.ThemeOverlay_Tandem_Fallback, true)
        }
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        b.surface.holder.addCallback(this)
        b.primaryBtn.setOnClickListener { onPrimary() }
        b.scanBtn.setOnClickListener { scan() }
        b.manualBtn.setOnClickListener { toggleManual() }
        b.editBtn.setOnClickListener { toggleManual(true) }
        b.codeInput.setOnEditorActionListener { _, id, _ ->
            if (id == EditorInfo.IME_ACTION_GO) { onPrimary(); true } else false
        }
        b.disconnectBtn.setOnClickListener { closeSheet(); userDisconnect("disconnect button") }
        b.edgeHandle.setOnClickListener { openSheet() }
        b.scrim.setOnClickListener { closeSheet() }
        b.surface.setOnClickListener {
            if (sheetOpen) closeSheet() else if (state != State.IDLE) peekChrome()
        }
        b.surface.setOnTouchListener { view, event -> forwardTouch(view, event) }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                // An edge swipe in immersive mode is easy to trigger by
                // accident (dragging a window near the phone's edge), so it
                // never drops a live stream: it opens the sheet, where
                // Disconnect is one deliberate tap away.
                when {
                    sheetOpen -> closeSheet()
                    state == State.LIVE || state == State.RECONNECTING -> openSheet()
                    state == State.CONNECTING -> userDisconnect("back while connecting")
                    else -> { isEnabled = false; onBackPressedDispatcher.onBackPressed() }
                }
            }
        })

        // park the side sheet off-screen until summoned
        b.controlSheet.visibility = View.INVISIBLE
        b.controlSheet.post {
            b.controlSheet.translationX = b.controlSheet.width + dp(24f)
            b.controlSheet.visibility = View.VISIBLE
        }

        target = ConnectTarget.load(prefs)
        handleIntent(intent)
        render()
        showSystemBars()

        // Testing over adb: am start -n io.github.saharshbaiju.tandem.debug/.MainActivity
        //   --ez autoconnect true
        // Debug builds only, and never together with a link: otherwise any app
        // could make us connect to (and send touches to) a computer it chose.
        val debuggable = (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0
        if (debuggable && intent?.data == null && target != null &&
            intent?.getBooleanExtra("autoconnect", false) == true) {
            b.root.post { startConnect(b.root.width * 0.7f, b.root.height * 0.6f) }
        }
    }

    // uiMode is in configChanges so a light/dark switch can't kill a live
    // stream; re-theme only while nothing is streaming.
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        if (state == State.IDLE && !b.connectScreen.isRippling) recreate()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (state == State.IDLE) handleIntent(intent)
    }

    /**
     * tandem://… opened from the camera app, a browser or another app. Anything
     * on the phone can send such a link, so it never replaces the saved
     * computer silently: the user sees which computer it is and confirms.
     */
    private fun handleIntent(intent: Intent?) {
        val raw = intent?.dataString ?: return
        when (val parsed = ConnectTarget.parse(raw)) {
            is ConnectTarget.Companion.Parsed.Ok -> {
                val t = parsed.target
                if (t.sameComputerAs(target)) { setTarget(t); return }   // new addresses only
                MaterialAlertDialogBuilder(this)
                    .setTitle(getString(R.string.link_confirm_title, t.title))
                    .setMessage(getString(R.string.link_confirm_body, t.addressLine))
                    .setPositiveButton(R.string.link_confirm_yes) { _, _ -> setTarget(t) }
                    .setNegativeButton(android.R.string.cancel, null)
                    .show()
            }
            ConnectTarget.Companion.Parsed.Outdated -> showError(getString(R.string.qr_outdated))
            ConnectTarget.Companion.Parsed.Invalid -> {}
        }
    }

    // remember where the user touched, so the ripple starts under their finger
    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (ev.actionMasked == MotionEvent.ACTION_DOWN) { lastTouchX = ev.x; lastTouchY = ev.y }
        return super.dispatchTouchEvent(ev)
    }

    /**
     * Live with touch allowed: the touch belongs to the desktop, so send it to
     * the computer and consume it. Otherwise return false so a tap falls
     * through to the click listener.
     */
    @SuppressLint("ClickableViewAccessibility")   // taps act on the remote desktop
    private fun forwardTouch(view: View, event: MotionEvent): Boolean {
        if (state != State.LIVE || !touchAllowed || sheetOpen) return false
        touchForwarder.forward(view, event)
        return true
    }

    // ------------------------------------------------------------ connect card

    private fun onPrimary() {
        if (b.manualGroup.visibility == View.VISIBLE) {
            val t = ConnectTarget.manual(b.hostInput.text.toString(), b.portInput.text.toString(),
                b.codeInput.text.toString(), target)
            if (t == null) {
                showError(getString(R.string.manual_invalid))
                return
            }
            setTarget(t)
            hideKeyboard()
            b.manualGroup.visibility = View.GONE
        }
        val t = target
        if (t == null) { scan(); return }
        if (!t.isPaired) {
            // saved by a version before 0.5: no pairing code / certificate yet
            showError(getString(R.string.repair_needed))
            return
        }
        val (x, y) = if (lastTouchX >= 0) lastTouchX to lastTouchY else centerOf(b.primaryBtn)
        startConnect(x, y)
    }

    private fun scan() {
        hideError()
        val options = GmsBarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .enableAutoZoom()
            .build()
        GmsBarcodeScanning.getClient(this, options).startScan()
            .addOnSuccessListener { code ->
                when (val parsed = ConnectTarget.parse(code.rawValue)) {
                    is ConnectTarget.Companion.Parsed.Ok -> setTarget(parsed.target)
                    ConnectTarget.Companion.Parsed.Outdated -> showError(getString(R.string.qr_outdated))
                    ConnectTarget.Companion.Parsed.Invalid ->
                        showError("That QR code isn't from Tandem.")
                }
            }
            .addOnFailureListener {
                showError("The QR scanner isn't available on this phone " +
                    "(needs Google Play services). Enter the address manually.")
                toggleManual(true)
            }
    }

    private fun toggleManual(show: Boolean = b.manualGroup.visibility != View.VISIBLE) {
        b.manualGroup.visibility = if (show) View.VISIBLE else View.GONE
        if (show) {
            target?.let {
                b.hostInput.setText(it.hosts.first())
                b.portInput.setText(it.port.toString())
                if (it.isPaired) b.codeInput.setText(SecureChannel.displayKey(it.pairingKey))
            }
            b.hostInput.requestFocus()
        } else {
            hideKeyboard()
        }
        render()
    }

    private fun setTarget(t: ConnectTarget) {
        target = t
        t.save(prefs)
        hideError()
        render()
    }

    /** Sync the connect card with [target] / manual mode. */
    private fun render() {
        val t = target
        val manual = b.manualGroup.visibility == View.VISIBLE
        b.targetRow.visibility = if (t != null && !manual) View.VISIBLE else View.GONE
        if (t != null) {
            b.targetName.text = t.title
            b.targetAddr.text = "${t.addressLine}  ·  port ${t.port}"
        }
        b.cardTitle.setText(if (t != null || manual) R.string.card_title_ready else R.string.card_title_new)
        b.cardBody.setText(if (t != null || manual) R.string.card_body_ready else R.string.card_body_new)
        val ready = t != null || manual
        b.primaryBtn.setText(if (ready) R.string.connect else R.string.scan_qr)
        b.primaryBtn.setIconResource(if (ready) R.drawable.ic_desktop else R.drawable.ic_qr_scan)
        b.scanBtn.visibility = if (ready) View.VISIBLE else View.GONE
        b.manualBtn.visibility = if (manual) View.GONE else View.VISIBLE
        b.statusChip.setText(when (state) {
            State.IDLE -> R.string.status_idle
            else -> R.string.status_connecting
        })
    }

    private fun showError(msg: String) {
        b.errorText.text = msg
        b.errorText.visibility = View.VISIBLE
    }

    private fun hideError() { b.errorText.visibility = View.GONE }

    // ---------------------------------------------------------------- sessions

    private fun startConnect(x: Float, y: Float) {
        if (target == null || state != State.IDLE) return
        hideError()
        wantConnected = true
        everStreamed = false
        reconnectAttempt = 0
        b.primaryBtn.isEnabled = false
        rippleStartedAt = SystemClock.uptimeMillis()
        b.connectScreen.startRipple(x, y)
        setState(State.CONNECTING)
        acquireWifiLock()
        launchSession()
    }

    private fun launchSession() {
        val t = target ?: return
        if (!surfaceReady || !wantConnected || session != null) return
        setTouchState(false, "")       // until the new session's INPT arrives
        session = StreamSession(t, appVersion(), b.surface.holder.surface, this)
    }

    private fun userDisconnect(why: String) {
        Log.i(TAG, "user disconnect: $why")
        wantConnected = false
        ui.removeCallbacks(reconnect)
        ui.removeCallbacks(noVideoWatchdog)
        session?.stop()          // its onStopped is ignored: session is nulled
        session = null
        releaseWifiLock()
        setState(State.IDLE)
        showConnectScreen()
    }

    /** First connection never produced video: settle the ripple, explain. */
    private fun failConnect(reason: String) {
        wantConnected = false
        ui.removeCallbacks(noVideoWatchdog)
        session?.stop()
        session = null
        releaseWifiLock()
        b.primaryBtn.isEnabled = true
        b.connectScreen.settle()
        setState(State.IDLE)
        showError(reason)
    }

    // Every callback checks it comes from the *current* session. A stale one
    // (stopped when the surface was recreated, say) must not touch state —
    // that race used to start a second decoder and loop connect/disconnect.

    override fun onConnected(session: StreamSession, host: String) = runOnUiThread {
        if (session !== this.session) return@runOnUiThread
        linkHost = host
        if (state == State.CONNECTING) {
            ui.removeCallbacks(noVideoWatchdog)
            ui.postDelayed(noVideoWatchdog, 10_000)
        }
    }

    override fun onFirstFrame(session: StreamSession) = runOnUiThread {
        if (session !== this.session) return@runOnUiThread
        ui.removeCallbacks(noVideoWatchdog)
        everStreamed = true
        reconnectAttempt = 0
        val wasConnecting = state == State.CONNECTING
        setState(State.LIVE)
        if (wasConnecting) {
            // let the drop ripple breathe a moment before the water opens up
            val wait = (MIN_RIPPLE_MS - (SystemClock.uptimeMillis() - rippleStartedAt))
                .coerceAtLeast(0)
            ui.postDelayed({
                if (state == State.LIVE) b.connectScreen.reveal { onRevealed() }
            }, wait)
        } else {
            showPill()
        }
    }

    private fun onRevealed() {
        b.primaryBtn.isEnabled = true
        enterImmersive()
        showPill()
    }

    override fun onStreamInfo(session: StreamSession, width: Int, height: Int) = runOnUiThread {
        if (session !== this.session) return@runOnUiThread
        streamSize = "$width × $height"
        // reshape the surface to the real stream aspect (letterbox, no stretch)
        b.surface.setAspectRatio(width, height)
        fillSheet()
    }

    override fun onServerConfig(session: StreamSession, config: ServerConfig) = runOnUiThread {
        if (session !== this.session) return@runOnUiThread
        serverConfig = config
        fillSheet()
    }

    override fun onInputState(session: StreamSession, touchAllowed: Boolean, reason: String) =
        runOnUiThread {
            if (session !== this.session) return@runOnUiThread
            setTouchState(touchAllowed, reason)
        }

    private fun setTouchState(allowed: Boolean, refusedReason: String) {
        touchAllowed = allowed
        touchRefusedReason = refusedReason
        fillSheet()
    }

    override fun onStopped(session: StreamSession, reason: String?, final: Boolean) = runOnUiThread {
        if (session !== this.session) return@runOnUiThread
        this.session = null
        setTouchState(false, "")
        if (!wantConnected) return@runOnUiThread
        if (final) {
            // Wrong pairing code, a different computer, an outdated version:
            // retrying can't fix it. Back to the connect screen, say why.
            failConnect(friendlyError(reason))
            if (everStreamed) showConnectScreen()
            return@runOnUiThread
        }
        if (!everStreamed) {
            failConnect(friendlyError(reason))
            return@runOnUiThread
        }
        // It worked before: keep the last frame up and retry with backoff
        // (cable re-plugged, Wi-Fi roam, daemon restarted for a config change).
        setState(State.RECONNECTING)
        val delay = BACKOFF_MS[reconnectAttempt.coerceAtMost(BACKOFF_MS.size - 1)]
        reconnectAttempt++
        ui.removeCallbacks(reconnect)
        ui.postDelayed(reconnect, delay)
    }

    private fun friendlyError(reason: String?): String {
        val name = target?.title ?: "your computer"
        return when {
            reason == null -> "Couldn't connect to $name."
            reason == StreamSession.CERT_CHANGED || reason == StreamSession.OUTDATED_COMPUTER -> reason
            "pairing code" in reason || "paired with" in reason || "refused the connection" in reason ||
                "phones connected" in reason || "update the tandem app" in reason.lowercase() ->
                "$name: ${reason.replaceFirstChar(Char::uppercase)}."
            "refused" in reason.lowercase() ->
                "$name is reachable, but the extender isn't running. " +
                    "Start it from the terminal (tandem → Start extender)."
            "couldn't reach" in reason || "timed out" in reason || "unreachable" in reason ->
                "Couldn't reach $name. Turn on USB tethering, or join the same Wi-Fi."
            else -> "Couldn't connect to $name: $reason"
        }
    }

    // ------------------------------------------------------------------ state

    private fun setState(s: State) {
        state = s
        render()
        fillSheet()
        if (s == State.RECONNECTING) showPill()
        if (s == State.IDLE) {
            // no live chrome on the connect screen (a tap while connecting
            // could leave a stale "Connecting…" pill over the error)
            ui.removeCallbacks(autoHide)
            b.statusPill.animate().cancel()
            b.statusPill.visibility = View.GONE
            b.edgeHandle.visibility = View.GONE
        }
    }

    private fun showConnectScreen() {
        b.statusPill.visibility = View.GONE
        b.edgeHandle.visibility = View.GONE
        b.primaryBtn.isEnabled = true
        b.connectScreen.reset()
        b.connectScreen.alpha = 0f
        b.connectScreen.animate().alpha(1f).setDuration(250).start()
        showSystemBars()
    }

    // ------------------------------------------------------------- live chrome

    private fun showPill() {
        val (text, color) = when (state) {
            State.LIVE -> "Live" + (if (streamSize.isNotEmpty()) "  ·  $streamSize" else "") to R.color.live
            State.RECONNECTING -> "Reconnecting…" to R.color.pending
            State.CONNECTING -> "Connecting…" to R.color.pending
            State.IDLE -> return
        }
        b.statusText.text = text
        b.dot.backgroundTintList = ColorStateList.valueOf(getColor(color))
        appear(b.statusPill)
        appear(b.edgeHandle, 0.9f)
        ui.removeCallbacks(autoHide)
        if (state == State.LIVE && !sheetOpen) ui.postDelayed(autoHide, 2800)
    }

    private fun peekChrome() = showPill()

    private fun appear(v: View, target: Float = 1f) {
        if (v.visibility != View.VISIBLE) {
            v.visibility = View.VISIBLE
            v.alpha = 0f
            v.translationY = if (v === b.statusPill) -dp(12f) else 0f
        }
        v.animate().alpha(target).translationY(0f).setDuration(220)
            .setInterpolator(EMPHASIZED).start()
    }

    /** Fade the pill out and dim the handle so the desktop is unobstructed. */
    private fun fadeChrome() {
        b.statusPill.animate().alpha(0f).translationY(-dp(12f)).setDuration(400)
            .withEndAction { b.statusPill.visibility = View.GONE }.start()
        b.edgeHandle.animate().alpha(0.25f).setDuration(400).start()
    }

    // -------------------------------------------------------------- side sheet

    private fun openSheet() {
        if (sheetOpen) return
        sheetOpen = true
        ui.removeCallbacks(autoHide)
        fillSheet()
        b.scrim.alpha = 0f
        b.scrim.visibility = View.VISIBLE
        b.scrim.animate().alpha(1f).setDuration(220).start()
        b.controlSheet.animate().translationX(0f).setDuration(380)
            .setInterpolator(EMPHASIZED).start()
        b.edgeHandle.animate().alpha(0f).setDuration(150).start()
    }

    private fun closeSheet() {
        if (!sheetOpen) return
        sheetOpen = false
        b.scrim.animate().alpha(0f).setDuration(200)
            .withEndAction { b.scrim.visibility = View.GONE }.start()
        b.controlSheet.animate().translationX(b.controlSheet.width + dp(24f))
            .setDuration(300).setInterpolator(EMPHASIZED).start()
        if (state != State.IDLE) showPill()
    }

    private fun fillSheet() {
        val sc = serverConfig
        b.sheetTitle.text = sc?.host?.ifBlank { null } ?: target?.title ?: "—"
        val link = when {
            linkHost.startsWith("192.168.42.") -> "USB tethering"
            linkHost == "127.0.0.1" -> "USB (adb)"
            linkHost.isNotEmpty() -> "Wi-Fi"
            else -> ""
        }
        b.sheetSubtitle.text = when (state) {
            State.LIVE -> "Connected via $link  ·  $linkHost"
            State.RECONNECTING -> "Reconnecting…"
            State.CONNECTING -> "Connecting…"
            State.IDLE -> "Not connected"
        }
        b.sheetRes.text = streamSize.ifEmpty { sc?.takeIf { it.width > 0 }?.let { "${it.width} × ${it.height}" } ?: "—" }
        b.sheetFps.text = sc?.takeIf { it.fps > 0 }?.let { "${it.fps} Hz" } ?: "—"
        b.sheetScale.text = sc?.takeIf { it.scale > 0 }?.let { fmtScale(it.scale) } ?: "—"
        b.sheetSide.text = sc?.side?.takeIf { it.isNotEmpty() }
            ?.let { "${it.replaceFirstChar(Char::uppercase)} of main" } ?: "—"
        b.sheetQuality.text = sc?.takeIf { it.quality.isNotEmpty() }?.let {
            it.quality.replaceFirstChar(Char::uppercase) +
                if (it.bitrate.isNotEmpty()) " · ${it.bitrate}bps" else ""
        } ?: "—"
        b.sheetTouch.text = when {
            touchAllowed -> getString(R.string.touch_on)
            state == State.LIVE && session != null -> getString(R.string.touch_off)
            else -> "—"
        }
        val touchNote = if (touchAllowed) getString(R.string.touch_on_hint) else touchRefusedReason
        b.sheetTouchNote.text = touchNote
        b.sheetTouchNote.visibility = if (touchNote.isEmpty() || state == State.IDLE) View.GONE else View.VISIBLE
        b.sheetBackend.text = when (sc?.backend) {
            "hyprland" -> "Hyprland"
            "gnome" -> "GNOME"
            null, "" -> "—"
            else -> sc.backend
        }
    }

    /** 1.0 -> "1×", 1.25 -> "1.25×". */
    private fun fmtScale(scale: Double): String {
        val s = if (scale == scale.toLong().toDouble()) scale.toLong().toString() else scale.toString()
        return "$s×"
    }

    // ------------------------------------------------------------------ misc

    private fun dp(v: Float) = v * resources.displayMetrics.density

    private fun appVersion(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: ""
    } catch (_: Exception) {
        ""
    }

    private fun centerOf(v: View): Pair<Float, Float> {
        val loc = IntArray(2); v.getLocationInWindow(loc)
        val root = IntArray(2); b.root.getLocationInWindow(root)
        return (loc[0] - root[0] + v.width / 2f) to (loc[1] - root[1] + v.height / 2f)
    }

    private fun hideKeyboard() {
        val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
        imm.hideSoftInputFromWindow(b.root.windowToken, 0)
    }

    private fun enterImmersive() {
        WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }
    }

    private fun showSystemBars() {
        WindowInsetsControllerCompat(window, window.decorView).apply {
            show(WindowInsetsCompat.Type.systemBars())
            // dark icons on the light theme, light icons on the dark one
            val night = (resources.configuration.uiMode and
                Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
            isAppearanceLightStatusBars = !night
            isAppearanceLightNavigationBars = !night
        }
    }

    @SuppressLint("WifiManagerLeak")
    private fun acquireWifiLock() {
        if (wifiLock?.isHeld == true) return
        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        @Suppress("DEPRECATION")
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
        wifiLock = wm.createWifiLock(mode, "tandem:stream").apply {
            setReferenceCounted(false)
            try { acquire() } catch (_: Exception) {}
        }
    }

    private fun releaseWifiLock() {
        try { wifiLock?.takeIf { it.isHeld }?.release() } catch (_: Exception) {}
    }

    // --- SurfaceHolder.Callback ---

    override fun surfaceCreated(holder: SurfaceHolder) {
        surfaceReady = true
        if (wantConnected) launchSession()
    }

    override fun surfaceChanged(holder: SurfaceHolder, f: Int, w: Int, h: Int) {}

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        surfaceReady = false
        // The decoder must let go of the Surface before this returns. Null the
        // field first so the session's own onStopped is recognised as stale.
        val s = session
        session = null
        s?.stopAndJoin()
        if (wantConnected && everStreamed) setState(State.RECONNECTING)
    }

    override fun onDestroy() {
        super.onDestroy()
        wantConnected = false
        ui.removeCallbacksAndMessages(null)
        session?.stop()
        releaseWifiLock()
    }

    companion object {
        private const val TAG = "MainActivity"
        private const val MIN_RIPPLE_MS = 550L
        private val BACKOFF_MS = longArrayOf(300, 800, 1500, 2500, 3000)
        private val EMPHASIZED = PathInterpolator(0.2f, 0f, 0f, 1f)
    }
}
