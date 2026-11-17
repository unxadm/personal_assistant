package assistant.client.ui

import android.Manifest
import android.app.Application
import android.content.Intent
import android.net.Uri
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
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
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import assistant.client.data.AuditEntry
import assistant.client.data.formatWhen
import assistant.client.notify.Notifications
import java.time.ZoneId

class MoreModel(application: Application) : ScreenModel(application) {
    var changes by mutableStateOf<List<AuditEntry>>(emptyList())
        private set
    val server get() = app.session.baseUrl.orEmpty()

    fun load() = run { changes = api.audit(changesOnly = true) }

    fun undo(entry: AuditEntry) = run {
        api.undo(entry.id)
        changes = api.audit(changesOnly = true)
    }

    fun signOut() = run { api.logout() }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MoreScreen(model: MoreModel, zone: ZoneId) {
    val context = LocalContext.current
    var notificationsAllowed by remember { mutableStateOf(Notifications.allowed(context)) }
    val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
        notificationsAllowed = it
    }
    LaunchedEffect(Unit) { model.load() }

    Scaffold(
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            TopAppBar(
                title = { Text("More") },
                actions = { IconButton(onClick = { model.load() }) { Icon(Icons.Filled.Refresh, "Reload") } },
            )
        },
    ) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding),
            contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                if (model.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                ErrorLine(model.error)
            }
            item {
                Section("Notifications on this phone") {
                    Text(
                        "The app asks your server for new notifications about every 15 minutes in the background, " +
                            "and every minute while it is open. Nothing goes through Google's push service. " +
                            "Android may delay background checks to save battery.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (!notificationsAllowed && Build.VERSION.SDK_INT >= 33) {
                        OutlinedButton(onClick = { askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS) }) {
                            Text("Allow notifications")
                        }
                    }
                }
            }
            item {
                Section("What the assistant changed") {
                    if (model.changes.isEmpty()) {
                        Text("Nothing yet.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
            items(model.changes, key = { it.id }) { entry ->
                Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp), horizontalArrangement = Arrangement.SpaceBetween) {
                    Column(Modifier.weight(1f)) {
                        Text(entry.summary, style = MaterialTheme.typography.bodyMedium)
                        Text(
                            "${formatWhen(entry.at, zone)} · ${entry.actor}" + if (entry.undoneAt != null) " · undone" else "",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    if (entry.undoLabel != null && entry.undoneAt == null) {
                        TextButton(onClick = { model.undo(entry) }, enabled = !model.busy) { Text(entry.undoLabel) }
                    }
                }
                HorizontalDivider(Modifier.padding(top = 8.dp))
            }
            item {
                Section("Server") {
                    Text(model.server, style = MaterialTheme.typography.bodyMedium)
                    Text(
                        "Settings, plugins, memory and account security are in the web interface.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = {
                            val open = Intent(Intent.ACTION_VIEW, Uri.parse(model.server))
                            try {
                                context.startActivity(open)
                            } catch (e: Exception) {
                                model.error = "No browser to open ${model.server}"
                            }
                        }) { Text("Open web interface") }
                        TextButton(onClick = { model.signOut() }) { Text("Sign out") }
                    }
                }
            }
        }
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}
