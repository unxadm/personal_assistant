package assistant.client.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** One notification as sent to the server. Nothing else about it (icons, buttons) leaves the phone. */
@Serializable
data class PhoneNotification(
    val key: String,
    @SerialName("package") val pkg: String,
    val app: String,
    val title: String,
    val text: String,
    @SerialName("posted_at") val postedAt: Long,
    val category: String,
)

/** What the system handed to the listener, before any decision about it. */
data class RawNotification(
    val pkg: String,
    val key: String,
    val app: String,
    val title: String,
    val text: String,
    val postedAt: Long,
    val category: String,
    val ongoing: Boolean,
    val groupSummary: Boolean,
    /** The app marked it as too private to show on a lock screen. */
    val secret: Boolean,
)

private const val TITLE_CHARS = 500
private const val TEXT_CHARS = 4000

/** Notifications that say nothing a person wrote: music players, downloads, system chatter. */
private val QUIET_CATEGORIES = setOf("transport", "progress", "service", "sys", "status", "call", "stopwatch", "alarm")

private val CODE_WORD = Regex(
    "\\b(one[- ]?time|verification|verify|security code|passcode|password|otp|2fa|" +
        "two[- ]factor|login code|sign[- ]?in code|auth(?:entication)? code|pin|code)\\b",
    RegexOption.IGNORE_CASE,
)
private val DIGITS = Regex("(?<![\\w.])\\d{4,8}(?!\\w)|\\b\\d{3}[- ]\\d{3}\\b")
private val CONTROL = Regex("[\\u0000-\\u0008\\u000b-\\u001f\\u007f]")

/**
 * A verification code or PIN. These are dropped on the phone, so they never leave it; the
 * server refuses them as well. Same rule as the server's (assistant/phone/service.py).
 */
fun looksLikeCode(title: String, text: String): Boolean {
    val joined = "$title\n$text"
    return CODE_WORD.containsMatchIn(joined) && DIGITS.containsMatchIn(joined)
}

/**
 * The notification to forward, or null to ignore it.
 *
 * Only apps the person chose in [allowed] are ever considered: with nothing chosen, nothing
 * is sent. Ongoing ones (a playing song, a download), summaries of a group, the app's own and
 * ones an app marked secret are left out too.
 */
fun capture(raw: RawNotification, allowed: Set<String>, ownPackage: String): PhoneNotification? {
    if (raw.pkg !in allowed || raw.pkg == ownPackage) return null
    if (raw.ongoing || raw.groupSummary || raw.secret || raw.category in QUIET_CATEGORIES) return null
    val title = CONTROL.replace(raw.title, "").trim().take(TITLE_CHARS)
    val text = CONTROL.replace(raw.text, "").trim().take(TEXT_CHARS)
    if (title.isEmpty() && text.isEmpty()) return null
    if (looksLikeCode(title, text)) return null
    return PhoneNotification(raw.key, raw.pkg, raw.app.ifBlank { raw.pkg }.take(100), title, text, raw.postedAt, raw.category)
}

/** Apps refresh the same notification again and again; only a change is worth sending. */
class RecentNotifications(private val capacity: Int = 100) {
    private val seen = object : LinkedHashMap<String, Int>(capacity, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Int>?) = size > capacity
    }

    @Synchronized
    fun isNew(n: PhoneNotification): Boolean {
        val fingerprint = (n.title + "\u0000" + n.text).hashCode()
        val id = n.pkg + "\u0000" + n.key
        if (seen[id] == fingerprint) return false
        seen[id] = fingerprint
        return true
    }
}
