package assistant.client.data

/** Just enough Markdown for chat replies: bold, inline code, code fences, headings, bullets. */
sealed interface Block {
    data class Text(val spans: List<Span>) : Block
    data class Code(val text: String) : Block
}

data class Span(val text: String, val bold: Boolean = false, val code: Boolean = false)

fun parseMarkdown(source: String): List<Block> {
    val blocks = mutableListOf<Block>()
    // Odd-numbered pieces sit between ``` fences.
    source.split("```").forEachIndexed { index, piece ->
        if (index % 2 == 1) {
            // Drop the language tag on the opening fence ("```python").
            val body = if ('\n' in piece) piece.substringAfter('\n') else piece
            if (body.isNotBlank()) blocks += Block.Code(body.trimEnd())
        } else if (piece.isNotBlank()) {
            blocks += Block.Text(parseLines(piece.trim('\n')))
        }
    }
    return blocks
}

private val HEADING = Regex("^#{1,6}\\s+")
private val BULLET = Regex("^(\\s*)[-*]\\s+")

private fun parseLines(text: String): List<Span> {
    val spans = mutableListOf<Span>()
    val lines = text.split('\n')
    lines.forEachIndexed { index, raw ->
        val heading = HEADING.find(raw)
        val line = when {
            heading != null -> raw.substring(heading.range.last + 1)
            else -> BULLET.replace(raw) { "${it.groupValues[1]}• " }
        }
        spans += if (heading != null) listOf(Span(stripMarks(line), bold = true)) else parseInline(line)
        if (index < lines.lastIndex) spans += Span("\n")
    }
    return spans
}

private fun stripMarks(line: String) = line.replace("**", "").replace("`", "")

fun parseInline(line: String): List<Span> {
    val spans = mutableListOf<Span>()
    val plain = StringBuilder()
    fun flush() {
        if (plain.isNotEmpty()) {
            spans += Span(plain.toString())
            plain.clear()
        }
    }
    var i = 0
    while (i < line.length) {
        if (line.startsWith("**", i)) {
            val end = line.indexOf("**", i + 2)
            if (end > i + 2) {
                flush()
                spans += Span(line.substring(i + 2, end), bold = true)
                i = end + 2
                continue
            }
        } else if (line[i] == '`') {
            val end = line.indexOf('`', i + 1)
            if (end > i + 1) {
                flush()
                spans += Span(line.substring(i + 1, end), code = true)
                i = end + 1
                continue
            }
        }
        plain.append(line[i])
        i++
    }
    flush()
    return spans
}
