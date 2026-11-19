package assistant.client.data

import java.net.URI
import java.net.URLDecoder
import java.net.URLEncoder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

/** Where the server's browser flow sends the app when it is done. Must match the manifest. */
const val SSO_SCHEME = "assistant.client"
const val SSO_HOST = "sso"

private const val PENDING_MS = 10 * 60 * 1000L
private val random = SecureRandom()
private val urlBase64 = Base64.getUrlEncoder().withoutPadding()

/**
 * A single sign-on the app started and is waiting to finish.
 *
 * The phone's browser cannot hand the app a session, and any app can register the same URL
 * scheme and read what is sent to it. So the app keeps [verifier] to itself and gives the
 * server only its hash; the one-time code that comes back is worthless without the verifier.
 * This lives in memory only: if the app is killed while the browser is open, sign in again.
 */
class SsoRequest(
    val base: String,
    val allowHttp: Boolean,
    val verifier: String = newVerifier(),
    private val startedAt: Long = System.currentTimeMillis(),
) {
    val challenge: String get() = challengeOf(verifier)

    fun expired(now: Long = System.currentTimeMillis()): Boolean = now - startedAt > PENDING_MS
}

fun newVerifier(): String = ByteArray(32).also(random::nextBytes).let(urlBase64::encodeToString)

fun challengeOf(verifier: String): String =
    urlBase64.encodeToString(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()))

/** The page that sends the browser to the provider and, afterwards, back to the app. */
fun ssoStartUrl(base: String, challenge: String): String =
    "$base/api/auth/oidc/login?challenge=${URLEncoder.encode(challenge, "UTF-8")}"

sealed interface SsoCallback {
    data class Code(val code: String) : SsoCallback
    data class Failed(val message: String) : SsoCallback
}

/** What the server's redirect to the app carried, or null when [uri] is not meant for the app. */
fun parseSsoCallback(uri: String): SsoCallback? {
    val parsed = try {
        URI(uri)
    } catch (e: Exception) {
        return null
    }
    if (parsed.scheme != SSO_SCHEME || parsed.host != SSO_HOST) return null
    val query = parsed.rawQuery.orEmpty().split('&').filter { it.isNotEmpty() }.associate {
        val key = it.substringBefore('=')
        key to URLDecoder.decode(it.substringAfter('=', ""), "UTF-8")
    }
    query["code"]?.takeIf { it.isNotBlank() }?.let { return SsoCallback.Code(it) }
    // The server's own wording, but anything can send this URL: keep it short and plain.
    val message = query["error"]?.take(200)?.filter { !it.isISOControl() }?.takeIf { it.isNotBlank() }
    return SsoCallback.Failed(message ?: "The sign-in did not finish. Try again.")
}
