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
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import assistant.client.data.Reminder
import assistant.client.data.formatWhen
import java.time.ZoneId

class RemindersModel(application: Application) : ScreenModel(application) {
    var reminders by mutableStateOf<List<Reminder>>(emptyList())
        private set
    var loaded by mutableStateOf(false)
        private set
    var text by mutableStateOf("")

    fun load() = run { fetch() }

    private suspend fun fetch() {
        reminders = api.reminders()
        loaded = true
    }

    fun add() {
        val wanted = text.trim()
        if (wanted.isEmpty() || busy) return
        run {
            api.quickReminder(wanted)
            text = ""
            fetch()
        }
    }

    fun done(reminder: Reminder) = run {
        api.completeReminder(reminder.id)
        fetch()
    }

    fun snooze(reminder: Reminder) = run {
        api.snoozeReminder(reminder.id)
        fetch()
    }

    fun delete(reminder: Reminder) = run {
        api.deleteReminder(reminder.id)
        fetch()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RemindersScreen(model: RemindersModel, zone: ZoneId) {
    LaunchedEffect(Unit) { model.load() }

    Scaffold(
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            TopAppBar(
                title = { Text("Reminders") },
                actions = { IconButton(onClick = { model.load() }) { Icon(Icons.Filled.Refresh, "Reload") } },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                OutlinedTextField(
                    value = model.text,
                    onValueChange = { model.text = it },
                    label = { Text("New reminder") },
                    placeholder = { Text("call the dentist tomorrow at 9") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                    keyboardActions = KeyboardActions(onDone = { model.add() }),
                    modifier = Modifier.weight(1f),
                )
                Button(onClick = model::add, enabled = !model.busy && model.text.isNotBlank()) { Text("Add") }
            }
            ErrorLine(model.error)
            LazyColumn(
                contentPadding = PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                if (model.loaded && model.reminders.isEmpty()) {
                    item {
                        Text(
                            "No reminders. Add one above in plain words, or ask in the chat.",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(24.dp),
                        )
                    }
                }
                items(model.reminders, key = { it.id }) { reminder ->
                    ReminderCard(reminder, zone, enabled = !model.busy, model = model)
                }
            }
        }
    }
}

@Composable
private fun ReminderCard(reminder: Reminder, zone: ZoneId, enabled: Boolean, model: RemindersModel) {
    var confirmDelete by remember { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(reminder.text, fontWeight = FontWeight.SemiBold)
            val details = buildList {
                add(formatWhen(reminder.dueAt, zone))
                if (reminder.repeat.isNotBlank()) add(reminder.repeat)
                reminder.snoozedUntil?.let { add("snoozed until ${formatWhen(it, zone)}") }
                if (reminder.status == "fired") add("due now")
            }
            Text(
                details.joinToString(" · "),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                TextButton(onClick = { model.done(reminder) }, enabled = enabled) { Text("Done") }
                TextButton(onClick = { model.snooze(reminder) }, enabled = enabled) { Text("Snooze") }
                TextButton(onClick = { confirmDelete = true }, enabled = enabled) { Text("Delete") }
            }
        }
    }
    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Delete this reminder?") },
            text = { Text(reminder.text) },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = false
                    model.delete(reminder)
                }) { Text("Delete") }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Keep") } },
        )
    }
}
