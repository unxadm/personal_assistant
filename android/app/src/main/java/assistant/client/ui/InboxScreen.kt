package assistant.client.ui

import android.app.Application
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import assistant.client.data.Suggestion
import assistant.client.data.formatWhen
import kotlinx.serialization.json.JsonPrimitive
import java.time.ZoneId

class InboxModel(application: Application) : ScreenModel(application) {
    var suggestions by mutableStateOf<List<Suggestion>>(emptyList())
        private set
    var enabled by mutableStateOf(true)
        private set
    var loaded by mutableStateOf(false)
        private set

    /** What the last action did, e.g. "Reminder set for tomorrow 09:00". */
    var notice by mutableStateOf<String?>(null)
        private set

    fun load() = run { fetch() }

    private suspend fun fetch() {
        val list = api.suggestions()
        suggestions = list.suggestions.filter { it.status == "pending" }
        enabled = list.enabled
        loaded = true
    }

    fun checkNow() = run {
        notice = null
        val created = api.checkNow()
        fetch()
        notice = if (created == 0) "Nothing new." else "$created new."
    }

    /** [onChat] gets the prompt when the suggestion is "ask the assistant about this". */
    fun accept(suggestion: Suggestion, asReminder: Boolean = false, onChat: (String) -> Unit) = run {
        val done = api.acceptSuggestion(suggestion.id, asReminder)
        notice = done.result
        fetch()
        val prompt = (suggestion.proposed?.get("prompt") as? JsonPrimitive)?.content
        if (suggestion.kind == "chat" && !prompt.isNullOrBlank()) onChat(prompt)
    }

    fun dismiss(suggestion: Suggestion) = run {
        api.dismissSuggestion(suggestion.id)
        notice = null
        fetch()
    }

    fun feedback(suggestion: Suggestion, useful: Boolean) = run {
        // Tapping the selected answer again clears it.
        val next = if (suggestion.feedback == (if (useful) "useful" else "not_useful")) null else useful
        val updated = api.suggestionFeedback(suggestion.id, next)
        suggestions = suggestions.map { if (it.id == updated.id) updated else it }
    }
}

private val Suggestion.kind: String?
    get() = (proposed?.get("type") as? JsonPrimitive)?.content

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InboxScreen(model: InboxModel, zone: ZoneId, onChanged: () -> Unit, onChat: (String) -> Unit) {
    LaunchedEffect(Unit) { model.load() }
    LaunchedEffect(model.suggestions.size) { onChanged() }

    Scaffold(
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            TopAppBar(
                title = { Text("Inbox") },
                actions = {
                    TextButton(onClick = { model.checkNow() }, enabled = !model.busy) { Text("Check now") }
                    IconButton(onClick = { model.load() }) { Icon(Icons.Filled.Refresh, "Reload") }
                },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            ErrorLine(model.error)
            model.notice?.takeIf { it.isNotBlank() }?.let {
                Text(it, Modifier.padding(horizontal = 16.dp, vertical = 8.dp), style = MaterialTheme.typography.bodyMedium)
            }
            LazyColumn(
                contentPadding = PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                if (model.loaded && model.suggestions.isEmpty()) {
                    item {
                        Text(
                            if (model.enabled) {
                                "Nothing needs your attention. Suggestions from your mail and calendar appear here."
                            } else {
                                "Proactive suggestions are switched off. Turn them on under Settings in the web interface."
                            },
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(24.dp),
                        )
                    }
                }
                items(model.suggestions, key = { it.id }) { suggestion ->
                    SuggestionCard(suggestion, zone, enabled = !model.busy, model = model, onChat = onChat)
                }
            }
        }
    }
}

@Composable
private fun SuggestionCard(s: Suggestion, zone: ZoneId, enabled: Boolean, model: InboxModel, onChat: (String) -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(
                "${s.category.replaceFirstChar { it.uppercase() }} · ${formatWhen(s.createdAt, zone)}",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.primary,
            )
            Text(s.title, fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.titleMedium)
            if (s.detail.isNotBlank()) Text(s.detail, style = MaterialTheme.typography.bodyMedium)
            if (s.reason.isNotBlank()) {
                Text(
                    "Why: ${s.reason}" + if (s.source == "llm") " (found by the model reading the email)" else "",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (s.proposed != null) {
                    Button(onClick = { model.accept(s, onChat = onChat) }, enabled = enabled) {
                        Text(s.actionLabel ?: "Do it")
                    }
                }
                OutlinedButton(onClick = { model.dismiss(s) }, enabled = enabled) { Text("Dismiss") }
            }
            if (s.kind == "event") {
                TextButton(onClick = { model.accept(s, asReminder = true, onChat = onChat) }, enabled = enabled) {
                    Text("Just remind me instead")
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(
                    selected = s.feedback == "useful",
                    onClick = { model.feedback(s, true) },
                    label = { Text("Useful") },
                )
                FilterChip(
                    selected = s.feedback == "not_useful",
                    onClick = { model.feedback(s, false) },
                    label = { Text("Not useful") },
                )
            }
        }
    }
}
