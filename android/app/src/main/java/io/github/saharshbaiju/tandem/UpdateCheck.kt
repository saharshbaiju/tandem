package io.github.saharshbaiju.tandem

import android.content.SharedPreferences
import android.util.Log
import org.json.JSONArray
import java.net.HttpURLConnection
import java.net.URL

/**
 * "A newer Tandem is out": asks GitHub for the releases every time the app
 * is opened with internet, and tells the connect screen what to offer:
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
 * answer and its ETag are kept in SharedPreferences: the next ask sends the
 * ETag (If-None-Match), and while nothing changed GitHub answers "304 Not
 * Modified", which doesn't count against its limit of 60 anonymous requests
 * an hour. Offline, rate limited or unreachable: the kept answer is used.
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
    private const val TIMEOUT_MS = 5000
    private const val USER_AGENT = "Tandem-update-check"

    private const val KEY_ETAG = "update.etag"
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
     * What to offer to an app at version [current], or null. With [online],
     * asks GitHub first (blocking: call it off the main thread); without, or
     * when GitHub can't answer, goes by the last answer kept.
     */
    fun check(prefs: SharedPreferences, current: String, online: Boolean): Release? {
        if (online) {
            val fetched = fetchReleases(prefs.getString(KEY_ETAG, null))
            Log.i(TAG, "update check: " + when (fetched) {
                is Fetched.Fresh -> "latest ${fetched.stable?.version}, beta ${fetched.beta?.version}"
                Fetched.Unchanged -> "unchanged since last time"
                Fetched.Failed -> "GitHub didn't answer, using the last answer"
            })
            if (fetched is Fetched.Fresh) {
                prefs.edit()
                    .putString(KEY_STABLE, fetched.stable?.version)
                    .putString(KEY_STABLE_URL, fetched.stable?.pageUrl)
                    .putString(KEY_BETA, fetched.beta?.version)
                    .putString(KEY_BETA_URL, fetched.beta?.pageUrl)
                    .putString(KEY_ETAG, fetched.etag)
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

    /** GitHub's answer: the newest releases, "same as last time", or nothing. */
    private sealed interface Fetched {
        /** The newest stable release and the newest pre-release, and the answer's ETag. */
        data class Fresh(val stable: Release?, val beta: Release?, val etag: String?) : Fetched
        data object Unchanged : Fetched
        data object Failed : Fetched
    }

    private fun fetchReleases(etag: String?): Fetched {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(RELEASES_URL).openConnection() as HttpURLConnection).apply {
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                useCaches = false              // the ETag below does the caching
                setRequestProperty("User-Agent", USER_AGENT)
                setRequestProperty("Accept", "application/vnd.github+json")
                if (etag != null) setRequestProperty("If-None-Match", etag)
            }
            when (connection.responseCode) {
                200 -> {}
                HttpURLConnection.HTTP_NOT_MODIFIED -> return Fetched.Unchanged
                else -> return Fetched.Failed
            }
            val newEtag = connection.getHeaderField("ETag")
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
            Fetched.Fresh(stable?.second, beta?.second, newEtag)
        } catch (e: Exception) {       // offline, rate limited, bad JSON: say nothing
            Log.i(TAG, "update check skipped: ${e.javaClass.simpleName}")
            Fetched.Failed
        } finally {
            connection?.disconnect()
        }
    }
}
