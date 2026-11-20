package assistant.client.ui

import android.content.Intent
import android.content.pm.PackageManager
import android.provider.Settings
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import assistant.client.App
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private data class LaunchableApp(val label: String, val pkg: String)

/**
 * "Phone notifications" on the More tab: off until the person grants Android's notification
 * access, picks apps and switches it on. Says plainly what is sent and where.
 */
@Composable
fun PhoneSection(onChoose: () -> Unit) {
    val context = LocalContext.current
    val app = context.applicationContext as App
    val enabled by app.phone.enabled.collectAsStateWithLifecycle()
    val chosen by app.phone.allowed.collectAsStateWithLifecycle()
    val refused by app.phone.refused.collectAsStateWithLifecycle()
    var access by remember { mutableStateOf(false) }
    var waiting by remember { mutableIntStateOf(0) }

    LifecycleResumeEffect(Unit) {
        access = NotificationManagerCompat.getEnabledListenerPackages(context).contains(context.packageName)
        waiting = app.phoneQueue.size()
        onPauseOrDispose { }
    }

    Text(
        "Forward notifications from the apps you choose (messages, alerts) to your server, so the assistant " +
            "can answer questions about them. They go to your server only, are kept for a limited time and " +
            "can be reviewed and deleted in the web interface. Verification codes and PINs are never sent. " +
            "Choose carefully: anything an app shows is sent, and the assistant's model may read it.",
        style = MaterialTheme.typography.bodySmall,
    )
    if (!access) {
        Text(
            "Step 1: let this app see notifications. Android shows a separate switch for it.",
            style = MaterialTheme.typography.bodySmall,
        )
        OutlinedButton(onClick = {
            try {
                context.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            } catch (e: Exception) {
                // No such settings page on this device.
            }
        }) { Text("Allow notification access") }
    }
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = onChoose) {
            Text(if (chosen.isEmpty()) "Choose apps" else "Choose apps (${chosen.size})")
        }
    }
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text("Send to the assistant", style = MaterialTheme.typography.bodyMedium)
        Switch(
            checked = enabled,
            enabled = access && chosen.isNotEmpty(),
            onCheckedChange = { app.phone.setEnabled(it) },
        )
    }
    if (access && chosen.isEmpty()) {
        Text("Choose at least one app first.", style = MaterialTheme.typography.bodySmall)
    }
    if (enabled && !access) {
        Text(
            "Notification access was withdrawn, so nothing is being sent.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.error,
        )
    }
    refused?.let {
        Text("The server turned this off: $it", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
    }
    if (enabled && waiting > 0) {
        Text("$waiting waiting to be sent.", style = MaterialTheme.typography.bodySmall)
    }
}

/**
 * Full-screen list of apps to forward notifications from. Every tick is saved at once, so
 * there is nothing to forget to confirm; Done (or Back) just returns.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppPickerScreen(onClose: () -> Unit) {
    val context = LocalContext.current
    val app = context.applicationContext as App
    val picked by app.phone.allowed.collectAsStateWithLifecycle()
    var apps by remember { mutableStateOf<List<LaunchableApp>?>(null) }
    var query by remember { mutableStateOf("") }
    LaunchedEffect(Unit) { apps = withContext(Dispatchers.IO) { launchableApps(context) } }
    BackHandler(onBack = onClose)

    fun toggle(pkg: String) {
        val next = if (pkg in picked) picked - pkg else picked + pkg
        app.phone.setAllowed(next)
        // With no app left there is nothing to send.
        if (next.isEmpty()) app.phone.setEnabled(false)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (picked.isEmpty()) "Choose apps" else "Choose apps (${picked.size})") },
                navigationIcon = {
                    IconButton(onClick = onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                },
            )
        },
        bottomBar = {
            Surface(tonalElevation = 3.dp) {
                Button(
                    onClick = onClose,
                    modifier = Modifier.fillMaxWidth().navigationBarsPadding().padding(12.dp),
                ) { Text("Done") }
            }
        },
    ) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).imePadding().padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                "Notifications from the apps you tick are sent to your server. Your choice is saved as you tick.",
                style = MaterialTheme.typography.bodySmall,
            )
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                label = { Text("Search apps") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            val list = apps
            val shown = list.orEmpty().filter { it.label.contains(query, ignoreCase = true) }
            when {
                list == null -> Text("Loading…")
                list.isEmpty() -> Text(
                    "Android did not list any apps. Reinstall this app and try again.",
                    color = MaterialTheme.colorScheme.error,
                )
                shown.isEmpty() -> Text("No app matches \"$query\".")
            }
            LazyColumn(Modifier.weight(1f)) {
                items(shown, key = { it.pkg }) { entry ->
                    Row(
                        Modifier.fillMaxWidth().clickable { toggle(entry.pkg) },
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Checkbox(checked = entry.pkg in picked, onCheckedChange = { toggle(entry.pkg) })
                        Text(entry.label, Modifier.padding(start = 8.dp, top = 12.dp, bottom = 12.dp))
                    }
                }
            }
        }
    }
}

private fun launchableApps(context: android.content.Context): List<LaunchableApp> {
    val pm = context.packageManager
    val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
    return pm.queryIntentActivities(intent, PackageManager.MATCH_ALL)
        .map { LaunchableApp(it.loadLabel(pm).toString(), it.activityInfo.packageName) }
        .filter { it.pkg != context.packageName }
        .distinctBy { it.pkg }
        .sortedBy { it.label.lowercase() }
}
