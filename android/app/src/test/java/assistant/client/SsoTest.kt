package assistant.client

import assistant.client.data.SsoCallback
import assistant.client.data.SsoRequest
import assistant.client.data.challengeOf
import assistant.client.data.newVerifier
import assistant.client.data.parseSsoCallback
import assistant.client.data.ssoStartUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SsoTest {
    @Test
    fun theChallengeIsTheS256HashOfTheVerifier() {
        // The example from RFC 7636, appendix B.
        assertEquals(
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            challengeOf("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
        )
    }

    @Test
    fun verifiersAreLongEnoughAndDifferEveryTime() {
        val a = newVerifier()
        assertTrue(a.length in 43..128)
        assertTrue(a.all { it.isLetterOrDigit() || it == '-' || it == '_' })
        assertNotEquals(a, newVerifier())
    }

    @Test
    fun theServerGetsTheChallengeNeverTheVerifier() {
        val request = SsoRequest("https://assistant.example", allowHttp = false)
        val url = ssoStartUrl(request.base, request.challenge)
        assertTrue(url.startsWith("https://assistant.example/api/auth/oidc/login?challenge="))
        assertFalse(request.verifier in url)
        assertTrue(request.challenge in url)
    }

    @Test
    fun aPendingSignInExpires() {
        val request = SsoRequest("https://assistant.example", false, startedAt = 1_000)
        assertFalse(request.expired(now = 1_000 + 9 * 60_000))
        assertTrue(request.expired(now = 1_000 + 11 * 60_000))
    }

    @Test
    fun theCodeIsReadFromTheRedirect() {
        assertEquals(
            SsoCallback.Code("abc-DEF_123"),
            parseSsoCallback("assistant.client://sso?code=abc-DEF_123"),
        )
    }

    @Test
    fun anErrorIsReadAndKeptShort() {
        assertEquals(
            SsoCallback.Failed("No account is linked to this sign-in."),
            parseSsoCallback("assistant.client://sso?error=No+account+is+linked+to+this+sign-in."),
        )
        val long = (parseSsoCallback("assistant.client://sso?error=${"x".repeat(500)}") as SsoCallback.Failed).message
        assertEquals(200, long.length)
    }

    @Test
    fun anEmptyOrOddRedirectIsAFailureNotACode() {
        assertTrue(parseSsoCallback("assistant.client://sso") is SsoCallback.Failed)
        assertTrue(parseSsoCallback("assistant.client://sso?code=") is SsoCallback.Failed)
    }

    @Test
    fun otherAddressesAreIgnored() {
        assertNull(parseSsoCallback("https://assistant.example/?code=abc"))
        assertNull(parseSsoCallback("assistant.client://other?code=abc"))
        assertNull(parseSsoCallback("not a uri at all"))
    }
}
