package assistant.client

import assistant.client.data.Block
import assistant.client.data.Span
import assistant.client.data.formatWhen
import assistant.client.data.parseInline
import assistant.client.data.parseMarkdown
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.ZoneId
import java.time.ZonedDateTime

class MarkdownTest {
    @Test
    fun boldAndCode() {
        assertEquals(
            listOf(Span("Call "), Span("Anna", bold = true), Span(" via "), Span("zoom", code = true), Span(".")),
            parseInline("Call **Anna** via `zoom`."),
        )
    }

    @Test
    fun unclosedMarksStayAsTyped() {
        assertEquals(listOf(Span("2 ** 3 and a ` tick")), parseInline("2 ** 3 and a ` tick"))
    }

    @Test
    fun fencesBecomeCodeBlocksWithoutTheLanguageTag() {
        val blocks = parseMarkdown("Run:\n```bash\nls -la\n```\nDone.")
        assertEquals(3, blocks.size)
        assertEquals(Block.Code("ls -la"), blocks[1])
        assertEquals(Block.Text(listOf(Span("Done."))), blocks[2])
    }

    @Test
    fun headingsAndBullets() {
        val text = parseMarkdown("## Today\n- **9:00** stand-up\n* lunch").single() as Block.Text
        assertEquals(
            listOf(
                Span("Today", bold = true), Span("\n"),
                Span("• "), Span("9:00", bold = true), Span(" stand-up"), Span("\n"),
                Span("• lunch"),
            ),
            text.spans,
        )
    }

    @Test
    fun timesUseTheServerTimezone() {
        val zone = ZoneId.of("Europe/Berlin")
        val now = ZonedDateTime.of(2026, 11, 16, 12, 0, 0, 0, zone)
        assertEquals("Today 15:30", formatWhen("2026-11-16T14:30:00+00:00", zone, now))
        assertEquals("Tomorrow 00:15", formatWhen("2026-11-16T23:15:00+00:00", zone, now))
        assertEquals("not a date", formatWhen("not a date", zone, now))
    }
}
