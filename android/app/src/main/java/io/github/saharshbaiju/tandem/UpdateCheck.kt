package io.github.saharshbaiju.tandem

import android.content.SharedPreferences
import android.util.Log
import org.json.JSONArray
import java.net.HttpURLConnection
import java.net.URL

/**
 * "A newer Tandem is out": asks GitHub for the releases at most once a day
 * and tells the connect screen what to offer:
 *
 *  - an **update**, when the latest stable release is newer than this app;
 *  - otherwise a **beta**, when a pre-release is newer than this app and than
 *    the latest stable release. The card asks whether the user wants to try
 *    it; saying no hides it until a newer beta comes out.
 *
 * Nothing is downloaded or installed here. The card's button opens the
 * release page in the browser, and the user installs the APK from there. All
 * releases, betas included, are signed with the same key, so they install
 * over this app and keep the pairing.
 *
 * What is sent: one HTTPS GET to api.github.com for the repository's recent
 * releases, with a generic User-Agent. No identifiers, no settings. The
 * answer is kept in SharedPreferences, so the other launches that day stay
 * offline.
 *
 * The PC side checks stable releases only (linux/update_check.py).
 */
object UpdateCheck {

    data class Release(val version: String, val pageUrl: String, val beta: Boolean)

    /**
     * A release number: 0.8.0, or 0.9.0-beta.2 (a pre-release, [pre] = 2).
     * A pre-release comes before its release: 0.9.0-beta.2 < 0.9.0. A
     * trailing "-debug" marks a debug build of the same version and is
     * ignored.
     */
    data class Version(val major: Int, val minor: Int, val patch: Int, val pre: Int?) :
        Comparable<Version> {
        val isPreRelease get() = pre != null

        override fun compareTo(other: Version): Int = compareValuesBy(
            this, other,
            { it.major }, { it.minor }, { it.patch },
            { if (it.pre == null) 1 else 0 },      // the release after its betas
            { it.pre ?: 0 },
        )

        companion object {
            private val PATTERN = Regex("""^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$""")

            fun parse(text: String?): Version? {
                val clean = text?.trim()?.removeSuffix("-debug") ?: return null
                val m = PATTERN.find(clean) ?: return null
                val (major, minor, patch, suffix) = m.destructured
                // Any other suffix (beta.2, rc1, alpha) is a pre-release; its
                // trailing number orders it among the others.
                val pre = if (suffix.isEmpty()) null
                    else Regex("""(\d+)$""").find(suffix)?.value?.toIntOrNull() ?: 0
                return Version(major.toInt(), minor.toInt(), patch.toInt(), pre)
            }
        }
    }

    private const val TAG = "UpdateCheck"
    private const val RELEASES_URL =
        "https://api.github.com/repos/saharshbaiju/tandem/releases?per_page=20"
    const val RELEASES_PAGE = "https://github.com/saharshbaiju/tandem/releases"
    private const val CHECK_EVERY_MS = 24L * 60 * 60 * 1000
    private const val TIMEOUT_MS = 5000
    private const val USER_AGENT = "Tandem-update-check"

    private const val KEY_CHECKED = "update.checked_at"
    private const val KEY_STABLE = "update.latest_version"
    private const val KEY_STABLE_URL = "update.latest_url"
    private const val KEY_BETA = "update.beta_version"
    private const val KEY_BETA_URL = "update.beta_url"
    private const val KEY_DISMISSED = "update.dismissed_version"
    private const val KEY_DISMISSED_BETA = "update.dismissed_beta"

    /** True if version text [a] is newer than [b]; false if either isn't a version. */
    fun isNewer(a: String?, b: String?): Boolean {
        val x = Version.parse(a) ?: return false
        val y = Version.parse(b) ?: return false
        return x > y
    }

    /**
     * What to offer to an app at version [current], or null. Blocks on the
     * network when the cached answer is older than a day, so call it off the
     * main thread.
     */
    fun check(prefs: SharedPreferences, current: String, now: Long = System.currentTimeMillis()): Release? {
        if (now - prefs.getLong(KEY_CHECKED, 0L) !in 0L until CHECK_EVERY_MS) {
            fetchReleases()?.let { (stable, beta) ->
                prefs.edit()
                    .putString(KEY_STABLE, stable?.version)
                    .putString(KEY_STABLE_URL, stable?.pageUrl)
                    .putString(KEY_BETA, beta?.version)
                    .putString(KEY_BETA_URL, beta?.pageUrl)
                    .putLong(KEY_CHECKED, now)
                    .apply()
            }
        }
        return choose(
            current,
            stable = prefs.getString(KEY_STABLE, null)?.let {
                Release(it, prefs.getString(KEY_STABLE_URL, null) ?: RELEASES_PAGE, beta = false)
            },
            beta = prefs.getString(KEY_BETA, null)?.let {
                Release(it, prefs.getString(KEY_BETA_URL, null) ?: RELEASES_PAGE, beta = true)
            },
            dismissed = prefs.getString(KEY_DISMISSED, null),
            dismissedBeta = prefs.getString(KEY_DISMISSED_BETA, null),
        )
    }

    /** The offer, from the latest [stable] and [beta] releases known. */
    fun choose(current: String, stable: Release?, beta: Release?,
               dismissed: String?, dismissedBeta: String?): Release? {
        if (stable != null && isNewer(stable.version, current) && stable.version != dismissed) {
            return stable
        }
        if (beta != null && isNewer(beta.version, current) &&
            (stable == null || isNewer(beta.version, stable.version)) &&
            beta.version != dismissedBeta) {
            return beta
        }
        return null
    }

    /** "Later" / "No thanks": don't offer this version again (a newer one is offered). */
    fun dismiss(prefs: SharedPreferences, release: Release) {
        prefs.edit().putString(if (release.beta) KEY_DISMISSED_BETA else KEY_DISMISSED,
                               release.version).apply()
    }

    /** (newest stable release, newest pre-release), or null if GitHub can't say. */
    private fun fetchReleases(): Pair<Release?, Release?>? {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(RELEASES_URL).openConnection() as HttpURLConnection).apply {
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                setRequestProperty("User-Agent", USER_AGENT)
                setRequestProperty("Accept", "application/vnd.github+json")
            }
            if (connection.responseCode != 200) return null
            val list = JSONArray(connection.inputStream.bufferedReader().use { it.readText() })
            var stable: Pair<Version, Release>? = null
            var beta: Pair<Version, Release>? = null
            for (i in 0 until list.length()) {
                val item = list.optJSONObject(i) ?: continue
                if (item.optBoolean("draft")) continue
                val version = Version.parse(item.optString("tag_name")) ?: continue
                val page = item.optString("html_url")
                    .takeIf { it.startsWith("https://github.com/") } ?: RELEASES_PAGE
                // GitHub's flag decides; a "-beta" tag published as a normal
                // release counts as stable, as the user would see it.
                val isBeta = item.optBoolean("prerelease")
                val release = Release(item.optString("tag_name").removePrefix("v"), page, isBeta)
                if (isBeta) {
                    if (beta == null || version > beta.first) beta = version to release
                } else {
                    if (stable == null || version > stable.first) stable = version to release
                }
            }
            stable?.second to beta?.second
        } catch (e: Exception) {       // offline, rate limited, bad JSON: say nothing
            Log.i(TAG, "update check skipped: ${e.javaClass.simpleName}")
            null
        } finally {
            connection?.disconnect()
        }
    }
}
