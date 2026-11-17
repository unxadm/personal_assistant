package assistant.client.ui

import android.app.Application
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import assistant.client.App
import assistant.client.data.ApiException
import assistant.client.data.Block
import assistant.client.data.parseMarkdown
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/** Shared by the screens' view models: the API, one error line, a busy flag. */
abstract class ScreenModel(application: Application) : AndroidViewModel(application) {
    protected val app = application as App
    protected val api get() = app.api

    var error by mutableStateOf<String?>(null)
    var busy by mutableStateOf(false)
        protected set

    protected fun run(block: suspend () -> Unit): Job = viewModelScope.launch {
        busy = true
        error = null
        try {
            block()
        } catch (e: ApiException) {
            error = e.message
        } finally {
            busy = false
        }
    }
}

@Composable
fun ErrorLine(message: String?, modifier: Modifier = Modifier) {
    if (message.isNullOrBlank()) return
    Text(
        message,
        color = MaterialTheme.colorScheme.error,
        style = MaterialTheme.typography.bodyMedium,
        modifier = modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
    )
}

@Composable
fun MarkdownText(source: String, color: Color, modifier: Modifier = Modifier) {
    val blocks = parseMarkdown(source)
    val codeBackground = MaterialTheme.colorScheme.surfaceVariant
    val codeColor = MaterialTheme.colorScheme.onSurfaceVariant
    Column(modifier, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (block in blocks) {
            when (block) {
                is Block.Code -> Text(
                    block.text,
                    color = codeColor,
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodySmall,
                    softWrap = false,
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(codeBackground, RoundedCornerShape(6.dp))
                        .horizontalScroll(rememberScrollState())
                        .padding(8.dp),
                )
                is Block.Text -> Text(
                    buildAnnotatedString {
                        for (span in block.spans) {
                            val style = when {
                                span.code -> SpanStyle(fontFamily = FontFamily.Monospace, background = codeBackground, color = codeColor)
                                span.bold -> SpanStyle(fontWeight = FontWeight.SemiBold)
                                else -> SpanStyle()
                            }
                            withStyle(style) { append(span.text) }
                        }
                    },
                    color = color,
                    style = MaterialTheme.typography.bodyLarge,
                )
            }
        }
    }
}
