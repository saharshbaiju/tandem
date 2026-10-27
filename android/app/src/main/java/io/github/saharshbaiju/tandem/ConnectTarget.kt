package io.github.saharshbaiju.tandem

import android.content.SharedPreferences
import android.net.Uri

/**
 * Where to connect and how to recognise the computer: every address it
 * advertised, its port and name, the pairing key and the fingerprint of its
 * TLS certificate. Comes from the pairing QR shown by the Linux TUI:
 *
 *     tandem://c?h=192.168.42.129,10.0.0.5&p=5000&n=archbox&k=<key>&f=<fp>&v=2
 *
 * [StreamSession] tries all [hosts] in parallel, so the same QR works over USB
 * tethering and Wi-Fi. Scanning it with the stock camera also opens the app
 * (the manifest registers the `tandem` scheme).
 *
 * [pairingKey] (`k`) is required: the computer streams nothing to a phone
 * that can't prove it knows it (see [SecureChannel]). [certFingerprint] (`f`,
 * base64url SHA-256) pins the computer's certificate; it is empty for an
 * address typed by hand, where the pairing proof alone recognises the computer.
 */
data class ConnectTarget(
    val hosts: List<String>,
    val port: Int,
    val name: String,
    val pairingKey: String,
    val certFingerprint: String = "",
) {

    /** "192.168.42.129 · 10.0.0.5" for display. */
    val addressLine: String get() = hosts.joinToString("  ·  ")

    val title: String get() = name.ifBlank { hosts.firstOrNull() ?: "computer" }

    /** False for a computer saved by a version before 0.5: it must be paired again. */
    val isPaired: Boolean get() = SecureChannel.isValidKey(pairingKey)

    /** The pinned certificate fingerprint, or null when there isn't one. */
    val pinnedFingerprint: ByteArray?
        get() = certFingerprint.takeIf { it.isNotEmpty() }?.let { SecureChannel.b64urlDecode(it) }

    fun save(p: SharedPreferences) {
        p.edit()
            .putString(KEY_HOSTS, hosts.joinToString(","))
            .putInt(KEY_PORT, port)
            .putString(KEY_NAME, name)
            .putString(KEY_PAIRING, pairingKey)
            .putString(KEY_CERT, certFingerprint)
            .apply()
    }

    /** Same computer, same credentials (addresses may differ). */
    fun sameComputerAs(other: ConnectTarget?) =
        other != null && other.pairingKey == pairingKey && other.certFingerprint == certFingerprint

    companion object {
        const val SCHEME = "tandem"
        /** The QR scheme before the rename to Tandem (always a version before 0.5). */
        private const val OLD_SCHEME = "asext"
        const val DEFAULT_PORT = 5000
        private const val KEY_HOSTS = "target_hosts"
        private const val KEY_PORT = "target_port"
        private const val KEY_NAME = "target_name"
        private const val KEY_PAIRING = "target_pairing_key"
        private const val KEY_CERT = "target_cert_fingerprint"

        fun load(p: SharedPreferences): ConnectTarget? {
            val hosts = p.getString(KEY_HOSTS, null)?.split(',')
                ?.map { it.trim() }?.filter { it.isNotEmpty() }.orEmpty()
            if (hosts.isEmpty()) return null
            return ConnectTarget(hosts, p.getInt(KEY_PORT, DEFAULT_PORT),
                p.getString(KEY_NAME, "") ?: "", p.getString(KEY_PAIRING, "") ?: "",
                p.getString(KEY_CERT, "") ?: "")
        }

        /** What a scanned QR / opened link turned out to be. */
        sealed class Parsed {
            data class Ok(val target: ConnectTarget) : Parsed()
            /** A link from a version before 0.5 (no encryption, no pairing proof). */
            object Outdated : Parsed()
            /** Not one of ours, or malformed. */
            object Invalid : Parsed()
        }

        /** Parse a scanned QR / deep link. */
        fun parse(raw: String?): Parsed {
            if (raw.isNullOrBlank()) return Parsed.Invalid
            val uri = try { Uri.parse(raw.trim()) } catch (_: Exception) { return Parsed.Invalid }
            if (uri.scheme == OLD_SCHEME) return Parsed.Outdated
            if (uri.scheme != SCHEME) return Parsed.Invalid
            val hosts = uri.getQueryParameter("h")?.split(',')
                ?.map { it.trim() }?.filter { isHost(it) }.orEmpty()
            if (hosts.isEmpty()) return Parsed.Invalid
            val version = uri.getQueryParameter("v")?.toIntOrNull() ?: 1
            val key = SecureChannel.normalizeKey(uri.getQueryParameter("k").orEmpty())
            val fingerprint = uri.getQueryParameter("f").orEmpty()
            if (version < SecureChannel.PROTOCOL_VERSION || !SecureChannel.isValidKey(key)) {
                return Parsed.Outdated
            }
            if (SecureChannel.b64urlDecode(fingerprint)?.size != 32) return Parsed.Invalid
            val port = uri.getQueryParameter("p")?.toIntOrNull()?.takeIf { it in 1..65535 }
                ?: DEFAULT_PORT
            return Parsed.Ok(ConnectTarget(hosts, port,
                uri.getQueryParameter("n").orEmpty().take(64), key, fingerprint))
        }

        /**
         * Manual entry: "10.0.0.5", "10.0.0.5:5000" or a hostname, plus the
         * pairing code shown in tandem. Null if either is unusable. Keeps the
         * name and pinned certificate of [previous] when it's the same
         * computer (same code: the user only corrected the address).
         */
        fun manual(address: String, portText: String, code: String,
                   previous: ConnectTarget? = null): ConnectTarget? {
            var host = address.trim()
            var port = portText.trim().toIntOrNull()
            val colon = host.lastIndexOf(':')
            if (colon > 0 && host.indexOf(':') == colon) {      // not IPv6
                port = host.substring(colon + 1).toIntOrNull() ?: port
                host = host.substring(0, colon)
            }
            val key = SecureChannel.normalizeKey(code)
            if (!isHost(host) || !SecureChannel.isValidKey(key)) return null
            val validPort = port?.takeIf { it in 1..65535 } ?: DEFAULT_PORT
            val same = previous?.takeIf { it.pairingKey == key }
            return ConnectTarget(listOf(host), validPort, same?.name.orEmpty(), key,
                same?.certFingerprint.orEmpty())
        }

        private fun isHost(s: String) =
            s.isNotEmpty() && s.length < 254 && s.all { it.isLetterOrDigit() || it in ".-:" }
    }
}
