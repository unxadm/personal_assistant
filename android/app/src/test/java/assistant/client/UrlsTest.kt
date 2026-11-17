package assistant.client

import assistant.client.data.UrlResult
import assistant.client.data.normalizeBaseUrl
import assistant.client.data.sessionCookie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class UrlsTest {
    private fun ok(input: String, allowHttp: Boolean = false) =
        (normalizeBaseUrl(input, allowHttp) as UrlResult.Ok).base

    @Test
    fun bareHostGetsHttps() {
        assertEquals("https://assistant.example.com", ok(" assistant.example.com "))
    }

    @Test
    fun pathQueryAndTrailingSlashAreDropped() {
        assertEquals("https://host.example:8443", ok("https://host.example:8443/chat/?tab=inbox#x"))
    }

    @Test
    fun plainHttpNeedsTheExplicitSwitch() {
        assertTrue(normalizeBaseUrl("http://192.168.1.10:8000", allowHttp = false) is UrlResult.Error)
        assertEquals("http://192.168.1.10:8000", ok("http://192.168.1.10:8000", allowHttp = true))
    }

    @Test
    fun credentialsInTheAddressAreRefused() {
        assertTrue(normalizeBaseUrl("https://me:secret@host.example", false) is UrlResult.Error)
    }

    @Test
    fun nonsenseIsRefused() {
        assertTrue(normalizeBaseUrl("", false) is UrlResult.Error)
        assertTrue(normalizeBaseUrl("ftp://host.example", false) is UrlResult.Error)
        assertTrue(normalizeBaseUrl("not a url", false) is UrlResult.Error)
    }

    @Test
    fun sessionCookieIsReadFromSetCookie() {
        val headers = listOf("other=1; Path=/", "pa_session=abc123; HttpOnly; Max-Age=1209600; Path=/; SameSite=strict")
        assertEquals("abc123", sessionCookie(headers))
    }

    @Test
    fun clearedOrMissingCookieIsNull() {
        assertNull(sessionCookie(listOf("pa_session=\"\"; Max-Age=0; Path=/")))
        assertNull(sessionCookie(listOf("xpa_session=abc; Path=/")))
        assertNull(sessionCookie(emptyList()))
    }
}
