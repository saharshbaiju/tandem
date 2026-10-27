package io.github.saharshbaiju.tandem

import android.util.Base64
import java.net.Socket
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager
import org.json.JSONObject

/**
 * The phone's half of the connection security in linux/secure_link.py (read
 * that for the threat model). In short:
 *
 *  1. TLS on the TCP connection. The computer's certificate is self-signed;
 *     it is trusted because its SHA-256 fingerprint came in the QR code
 *     ([ConnectTarget.certFingerprint]). Another certificate = not your
 *     computer, and nothing is sent.
 *  2. Mutual proof of the pairing key, bound to the certificate actually seen:
 *
 *        HELO {v, nonce}  ->   <- CHAL {v, nonce}
 *        AUTH {proof}     ->   <- OKAY {proof}  /  DENY {reason}
 *
 *     proof = HMAC-SHA256(key, "tandem-v2 client|server" 0 fingerprint nc ns).
 *     The key itself never crosses the network. We check the computer's proof
 *     before reading a single video frame or sending a touch, so a look-alike
 *     computer can neither show us a fake desktop nor receive our touches.
 *     This also covers an address typed by hand (no fingerprint to pin).
 */
object SecureChannel {

    const val PROTOCOL_VERSION = 2
    private const val NONCE_BYTES = 32

    const val TYPE_HELLO = "HELO"
    const val TYPE_CHALLENGE = "CHAL"
    const val TYPE_AUTH = "AUTH"
    const val TYPE_OK = "OKAY"
    const val TYPE_DENY = "DENY"

    /** Crockford base32, as linux/secure_link.py generates the key. */
    private const val KEY_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
    const val KEY_LENGTH = 20

    /**
     * The handshake failed in a way retrying can't fix (wrong pairing code,
     * different computer, outdated version): stop and tell the user.
     */
    class PairingException(message: String) : Exception(message)

    private val random = SecureRandom()

    /** What the user typed -> canonical key: case, dashes and i/l/o don't matter. */
    fun normalizeKey(text: String): String =
        text.lowercase().filter { it.isLetterOrDigit() }
            .map { when (it) { 'i', 'l' -> '1'; 'o' -> '0'; else -> it } }
            .joinToString("")

    fun isValidKey(key: String) = key.length == KEY_LENGTH && key.all { it in KEY_ALPHABET }

    /** "abcde-fghjk-mnpqr-stvwx", as the computer shows it. */
    fun displayKey(key: String) = key.chunked(5).joinToString("-")

    fun b64url(data: ByteArray): String =
        Base64.encodeToString(data, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)

    fun b64urlDecode(text: String): ByteArray? = try {
        Base64.decode(text, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    } catch (_: IllegalArgumentException) {
        null
    }

    /**
     * TLS over an already-connected [socket]. With a [pinnedFingerprint] (from
     * the QR) any other certificate is refused during the handshake. Returns
     * the TLS socket and the SHA-256 of the certificate the computer presented.
     */
    fun wrap(socket: Socket, host: String, port: Int, pinnedFingerprint: ByteArray?): Pair<SSLSocket, ByteArray> {
        val trust = PinningTrustManager(pinnedFingerprint)
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, arrayOf(trust), random)
        val tls = ctx.socketFactory.createSocket(socket, host, port, true) as SSLSocket
        tls.enabledProtocols = tls.supportedProtocols
            .filter { it == "TLSv1.3" || it == "TLSv1.2" }.toTypedArray()
        tls.startHandshake()
        val cert = tls.session.peerCertificates.firstOrNull()
            ?: throw PairingException("the computer sent no certificate")
        return tls to sha256(cert.encoded)
    }

    /**
     * Prove we know [key] and check the computer does too. Returns normally
     * when both sides are verified; throws [PairingException] otherwise.
     */
    fun authenticate(reader: FrameReader, writer: FrameWriter, key: String,
                     certFingerprint: ByteArray, appVersion: String) {
        val clientNonce = ByteArray(NONCE_BYTES).also { random.nextBytes(it) }
        writer.send(TYPE_HELLO, JSONObject()
            .put("v", PROTOCOL_VERSION)
            .put("nonce", b64url(clientNonce))
            .put("app", appVersion)
            .toString().toByteArray(Charsets.UTF_8))

        val challenge = readHandshakeFrame(reader)
        val serverNonce = b64urlDecode(challenge.optString("nonce"))
        if (serverNonce == null || serverNonce.size != NONCE_BYTES) {
            throw PairingException("unexpected answer from the computer")
        }
        writer.send(TYPE_AUTH, JSONObject()
            .put("proof", b64url(proof("client", key, certFingerprint, clientNonce, serverNonce)))
            .toString().toByteArray(Charsets.UTF_8))

        val ok = readHandshakeFrame(reader, expectOk = true)
        val serverProof = b64urlDecode(ok.optString("proof")) ?: ByteArray(0)
        val expected = proof("server", key, certFingerprint, clientNonce, serverNonce)
        if (!MessageDigest.isEqual(serverProof, expected)) {
            throw PairingException("this computer doesn't know your pairing code - " +
                "it may not be the one you paired with")
        }
    }

    /** CHAL (or OKAY when [expectOk]) as JSON; DENY becomes a [PairingException]. */
    private fun readHandshakeFrame(reader: FrameReader, expectOk: Boolean = false): JSONObject {
        val f = reader.next() ?: throw PairingException("the computer closed the connection")
        val body = try {
            JSONObject(String(f.body, Charsets.UTF_8))
        } catch (_: Exception) {
            JSONObject()
        }
        val wanted = if (expectOk) TYPE_OK else TYPE_CHALLENGE
        return when (f.type) {
            wanted -> body
            TYPE_DENY -> throw PairingException(body.optString("reason", "the computer refused the connection"))
            else -> throw PairingException("unexpected answer from the computer (${f.type})")
        }
    }

    private fun proof(role: String, key: String, fingerprint: ByteArray,
                      clientNonce: ByteArray, serverNonce: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        mac.update("tandem-v2 $role".toByteArray(Charsets.US_ASCII))
        mac.update(0.toByte())
        mac.update(fingerprint)
        mac.update(clientNonce)
        mac.update(serverNonce)
        return mac.doFinal()
    }

    private fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

    /**
     * Trusts exactly the certificate whose fingerprint was in the QR code.
     * Without a fingerprint (address typed by hand) it accepts the certificate
     * and leaves the decision to the pairing proof, which is bound to it.
     */
    private class PinningTrustManager(private val pinned: ByteArray?) : X509TrustManager {
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            val leaf = chain.firstOrNull() ?: throw CertificateException("no certificate")
            if (pinned != null && !MessageDigest.isEqual(sha256(leaf.encoded), pinned)) {
                throw CertificateException(CERT_MISMATCH)
            }
        }

        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) =
            throw CertificateException("not a server")

        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }

    const val CERT_MISMATCH = "certificate fingerprint mismatch"
}
