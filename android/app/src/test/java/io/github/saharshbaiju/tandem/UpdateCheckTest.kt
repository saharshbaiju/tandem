package io.github.saharshbaiju.tandem

import io.github.saharshbaiju.tandem.UpdateCheck.Release
import io.github.saharshbaiju.tandem.UpdateCheck.Version
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The version ordering and the choice of what the connect screen offers. */
class UpdateCheckTest {

    @Test fun parsesReleasesBetasAndDebugBuilds() {
        assertEquals(Version(0, 8, 0, null), Version.parse("0.8.0"))
        assertEquals(Version(0, 8, 0, null), Version.parse("v0.8.0"))
        assertEquals(Version(0, 8, 0, null), Version.parse("0.8.0-debug"))
        assertEquals(Version(0, 9, 0, 2), Version.parse("v0.9.0-beta.2"))
        assertEquals(Version(0, 9, 0, 1), Version.parse("0.9.0-beta.1-debug"))
        assertEquals(Version(1, 0, 0, 0), Version.parse("1.0.0-rc"))
        assertNull(Version.parse("latest"))
        assertNull(Version.parse("0.8"))
        assertNull(Version.parse(null))
    }

    @Test fun ordersByNumberAndPutsBetasBeforeTheirRelease() {
        assertTrue(UpdateCheck.isNewer("0.10.0", "0.9.9"))
        assertTrue(UpdateCheck.isNewer("0.9.0", "0.9.0-beta.3"))
        assertTrue(UpdateCheck.isNewer("0.9.0-beta.2", "0.9.0-beta.1"))
        assertTrue(UpdateCheck.isNewer("0.9.0-beta.1", "0.8.0"))
        assertFalse(UpdateCheck.isNewer("0.9.0-beta.1", "0.9.0"))
        assertFalse(UpdateCheck.isNewer("0.8.0", "0.8.0-debug"))
        assertFalse(UpdateCheck.isNewer("junk", "0.8.0"))
    }

    private fun stable(v: String) = Release(v, "https://github.com/x/$v", beta = false)
    private fun beta(v: String) = Release(v, "https://github.com/x/$v", beta = true)

    @Test fun offersANewerStableReleaseFirst() {
        assertEquals(stable("0.9.0"),
            UpdateCheck.choose("0.8.0", stable("0.9.0"), beta("0.10.0-beta.1"), null, null))
    }

    @Test fun offersABetaNewerThanTheAppAndTheLatestRelease() {
        assertEquals(beta("0.9.0-beta.1"),
            UpdateCheck.choose("0.8.0", stable("0.8.0"), beta("0.9.0-beta.1"), null, null))
    }

    @Test fun doesNotOfferAnOldBeta() {
        // The beta's release is out already: the beta is older than it.
        assertNull(UpdateCheck.choose("0.9.0", stable("0.9.0"), beta("0.9.0-beta.2"), null, null))
        // The app is newer than the beta.
        assertNull(UpdateCheck.choose("0.9.1", null, beta("0.9.0-beta.2"), null, null))
    }

    @Test fun aBetaUserIsOfferedTheNextBetaAndTheRelease() {
        assertEquals(beta("0.9.0-beta.2"),
            UpdateCheck.choose("0.9.0-beta.1", stable("0.8.0"), beta("0.9.0-beta.2"), null, null))
        assertEquals(stable("0.9.0"),
            UpdateCheck.choose("0.9.0-beta.2", stable("0.9.0"), beta("0.9.0-beta.2"), null, null))
    }

    @Test fun saidNoUntilANewerOneComes() {
        assertNull(UpdateCheck.choose("0.8.0", stable("0.9.0"), null, "0.9.0", null))
        assertEquals(stable("0.9.1"), UpdateCheck.choose("0.8.0", stable("0.9.1"), null, "0.9.0", null))
        assertNull(UpdateCheck.choose("0.8.0", stable("0.8.0"), beta("0.9.0-beta.1"), null, "0.9.0-beta.1"))
        assertEquals(beta("0.9.0-beta.2"),
            UpdateCheck.choose("0.8.0", stable("0.8.0"), beta("0.9.0-beta.2"), null, "0.9.0-beta.1"))
    }

    @Test fun upToDateOffersNothing() {
        assertNull(UpdateCheck.choose("0.8.0", stable("0.8.0"), null, null, null))
        assertNull(UpdateCheck.choose("0.8.0-debug", stable("0.8.0"), null, null, null))
        assertNull(UpdateCheck.choose("0.8.0", null, null, null, null))
    }
}
