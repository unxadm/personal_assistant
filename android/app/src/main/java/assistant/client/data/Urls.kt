package assistant.client.data

import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

const val SESSION_COOKIE = "pa_session"

sealed interface UrlResult {
    data class Ok(val base: String) : UrlResult
    data class Error(val message: String) : UrlResult
}

/**
 * Turn what was typed into the server's origin ("https://host[:port]", no trailing slash).
 *
 * A bare host gets https://. Plain HTTP is refused unless [allowHttp] is set: over HTTP the
 * password and everything the assistant says can be read by anyone on the network path.
 */
fun normalizeBaseUrl(input: String, allowHttp: Boolean): UrlResult {
    val typed = input.trim()
    if (typed.isEmpty()) return UrlResult.Error("Enter the address of your server")
    val withScheme = if ("://" in typed) typed else "https://$typed"
    val url = withScheme.toHttpUrlOrNull()
        ?: return UrlResult.Error("That is not a valid address (expected something like https://assistant.example.com)")
    if (url.username.isNotEmpty() || url.password.isNotEmpty()) {
        return UrlResult.Error("Leave the user name and password out of the address")
    }
    if (!url.isHttps && !allowHttp) {
        return UrlResult.Error("This address is not encrypted (http://). Use https://, or allow unencrypted HTTP below.")
    }
    val origin = url.newBuilder().encodedPath("/").query(null).fragment(null).build().toString()
    return UrlResult.Ok(origin.trimEnd('/'))
}

/** The session token from a login response's Set-Cookie headers, or null. */
fun sessionCookie(setCookie: List<String>): String? {
    for (header in setCookie) {
        val pair = header.substringBefore(';').trim()
        if (pair.substringBefore('=') != SESSION_COOKIE) continue
        val value = pair.substringAfter('=', "").trim().trim('"')
        if (value.isNotEmpty()) return value
    }
    return null
}
