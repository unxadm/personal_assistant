package assistant.client.ui

import android.app.Application
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import assistant.client.data.BriefingSection
import assistant.client.data.BriefingText
import assistant.client.data.formatDay
import assistant.client.data.formatWhen
import java.time.ZoneId

class BriefingModel(application: Application) : ScreenModel(application) {
    var latest by mutableStateOf<BriefingText?>(null)
        private set
    var history by mutableStateOf<List<BriefingText>>(emptyList())
        private set
    var loaded by mutableStateOf(false)
        private set

    fun load() = run { fetch() }

    private suspend fun fetch() {
        val briefing = api.briefing()
        latest = briefing.latest
        history = briefing.history
        loaded = true
    }

    /** Writing one takes as long as the model needs for a few hundred words. */
    fun write() = run {
        api.runBriefing()
        fetch()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BriefingScreen(model: BriefingModel, zone: ZoneId) {
    LaunchedEffect(Unit) { model.load() }
    val latest = model.latest

    Scaffold(
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            TopAppBar(
                title = { Text(latest?.let { formatDay(it.date) } ?: "Daily briefing") },
                actions = {
                    TextButton(onClick = { model.write() }, enabled = !model.busy) {
                        Text(if (latest == null) "Write one" else "Write a new one")
                    }
                    IconButton(onClick = { model.load() }) { Icon(Icons.Filled.Refresh, "Reload") }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            item {
                if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                ErrorLine(model.error, Modifier.padding(horizontal = 0.dp))
            }
            if (latest == null) {
                if (model.loaded) {
                    item {
                        Text(
                            "No briefing yet. It is written every morning when the daily briefing is " +
                                "turned on under Settings in the web interface.",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            } else {
                item {
                    Text(
                        "Written ${formatWhen(latest.generatedAt, zone)}",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                items(sectionsOf(latest)) { section -> SectionText(section) }
            }
            if (model.history.isNotEmpty()) {
                item { Text("Earlier", style = MaterialTheme.typography.titleMedium) }
                items(model.history, key = { it.date }) { earlier -> EarlierBriefing(earlier) }
            }
        }
    }
}

/** Servers from before briefings had headings send only the text. */
private fun sectionsOf(briefing: BriefingText): List<BriefingSection> =
    briefing.sections.ifEmpty { listOf(BriefingSection(body = briefing.text)) }

@Composable
private fun SectionText(section: BriefingSection) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        if (section.title.isNotBlank()) {
            Text(section.title, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        }
        SelectionContainer { MarkdownText(section.body, MaterialTheme.colorScheme.onSurface) }
    }
}

@Composable
private fun EarlierBriefing(briefing: BriefingText) {
    var open by rememberSaveable(briefing.date) { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(
                formatDay(briefing.date),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.fillMaxWidth().clickable { open = !open },
            )
            if (open) sectionsOf(briefing).forEach { SectionText(it) }
        }
    }
}
