package assistant.client.data

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

// Built per call so a language change on the phone takes effect without a restart.
private fun pattern(pattern: String) = DateTimeFormatter.ofPattern(pattern, Locale.getDefault())

fun zoneOrUtc(name: String): ZoneId = try {
    ZoneId.of(name)
} catch (e: Exception) {
    ZoneId.of("UTC")
}

/** A server timestamp in the assistant's configured timezone: "Today 14:30", "Mon 16 Nov 09:00". */
fun formatWhen(iso: String, zone: ZoneId, now: ZonedDateTime = ZonedDateTime.now(zone)): String {
    val at = try {
        OffsetDateTime.parse(iso).atZoneSameInstant(zone)
    } catch (e: Exception) {
        return iso
    }
    val days = at.toLocalDate().toEpochDay() - now.toLocalDate().toEpochDay()
    val day = when {
        days == 0L -> "Today"
        days == 1L -> "Tomorrow"
        days == -1L -> "Yesterday"
        at.year == now.year -> pattern("EEE d MMM").format(at)
        else -> pattern("d MMM yyyy").format(at)
    }
    return "$day ${pattern("HH:mm").format(at)}"
}

/** A calendar date from the server ("2026-11-16") as "Monday 16 November". */
fun formatDay(date: String): String = try {
    pattern("EEEE d MMMM").format(java.time.LocalDate.parse(date))
} catch (e: Exception) {
    date
}
