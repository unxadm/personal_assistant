package assistant.client.ui

import android.Manifest
import android.app.Application
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Chat
import androidx.compose.material.icons.filled.Alarm
import androidx.compose.material.icons.filled.Inbox
import androidx.compose.material.icons.filled.MoreHoriz
import androidx.compose.material.icons.filled.Today
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import assistant.client.App
import assistant.client.data.ApiException
import assistant.client.data.zoneOrUtc
import assistant.client.notify.Notifications
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.time.ZoneId

/** What every tab needs from the server: its timezone, whether dictation works, the inbox badge. */
class ShellModel(application: Application) : ScreenModel(application) {
    var zone by mutableStateOf<ZoneId>(ZoneId.systemDefault())
        private set
    var voiceInput by mutableStateOf(false)
        private set
    var inboxCount by mutableIntStateOf(0)
        private set

    suspend fun loadSettings() {
        try {
            val settings = api.settings()
            zone = zoneOrUtc(settings.timezone)
            voiceInput = settings.capabilities.stt
        } catch (e: ApiException) {
            // Times fall back to the phone's timezone and dictation stays hidden.
        }
    }

    suspend fun refreshCount() {
        try {
            inboxCount = api.suggestionCount()
        } catch (e: ApiException) {
            // Keep the last known number.
        }
    }

    fun updateBadge() {
        viewModelScope.launch { refreshCount() }
    }
}

private val TABS = listOf("chat", "briefing", "inbox", "reminders", "more")

@Composable
fun AppRoot(
    requestedTab: String?,
    onTabShown: () -> Unit,
    ssoCallback: String? = null,
    onSsoHandled: () -> Unit = {},
) {
    val context = LocalContext.current
    val session = (context.applicationContext as App).session
    val signedIn by session.signedIn.collectAsStateWithLifecycle()
    if (!signedIn) {
        LoginScreen(ssoCallback = ssoCallback, onSsoHandled = onSsoHandled)
        return
    }
    // Signed in already: a stray sign-in address (an old link, another app) is not acted on.
    LaunchedEffect(ssoCallback) { if (ssoCallback != null) onSsoHandled() }

    val shell: ShellModel = viewModel()
    val chat: ChatModel = viewModel()
    val briefing: BriefingModel = viewModel()
    val inbox: InboxModel = viewModel()
    val reminders: RemindersModel = viewModel()
    val more: MoreModel = viewModel()
    var tab by rememberSaveable { mutableStateOf("chat") }

    LaunchedEffect(requestedTab) {
        if (requestedTab != null && requestedTab in TABS) {
            tab = requestedTab
            onTabShown()
        }
    }

    // Ask once, on first use, so reminders can reach the notification shade.
    var asked by rememberSaveable { mutableStateOf(false) }
    val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(Unit) {
        if (!asked && Build.VERSION.SDK_INT >= 33 && !Notifications.allowed(context)) {
            asked = true
            askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        shell.loadSettings()
    }

    // While the app is on screen, check for notifications and the inbox badge once a minute.
    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(lifecycleOwner) {
        lifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                Notifications.sync(context)
                shell.refreshCount()
                delay(60_000)
            }
        }
    }

    Scaffold(
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        bottomBar = {
            NavigationBar {
                NavigationBarItem(
                    selected = tab == "chat",
                    onClick = { tab = "chat" },
                    icon = { Icon(Icons.AutoMirrored.Filled.Chat, null) },
                    label = { Text("Chat") },
                )
                NavigationBarItem(
                    selected = tab == "briefing",
                    onClick = { tab = "briefing" },
                    icon = { Icon(Icons.Filled.Today, null) },
                    label = { Text("Briefing") },
                )
                NavigationBarItem(
                    selected = tab == "inbox",
                    onClick = { tab = "inbox" },
                    icon = {
                        BadgedBox(badge = { if (shell.inboxCount > 0) Badge { Text("${shell.inboxCount}") } }) {
                            Icon(Icons.Filled.Inbox, null)
                        }
                    },
                    label = { Text("Inbox") },
                )
                NavigationBarItem(
                    selected = tab == "reminders",
                    onClick = { tab = "reminders" },
                    icon = { Icon(Icons.Filled.Alarm, null) },
                    label = { Text("Reminders") },
                )
                NavigationBarItem(
                    selected = tab == "more",
                    onClick = { tab = "more" },
                    icon = { Icon(Icons.Filled.MoreHoriz, null) },
                    label = { Text("More") },
                )
            }
        },
    ) { padding ->
        // The keyboard pushes the content up by its height minus the navigation bar it covers.
        Box(Modifier.fillMaxSize().padding(padding).consumeWindowInsets(padding).imePadding()) {
            when (tab) {
                "inbox" -> InboxScreen(
                    inbox, shell.zone,
                    onChanged = shell::updateBadge,
                    onChat = { prompt ->
                        chat.startNew(prompt)
                        tab = "chat"
                    },
                )
                "briefing" -> BriefingScreen(briefing, shell.zone)
                "reminders" -> RemindersScreen(reminders, shell.zone)
                "more" -> MoreScreen(more, shell.zone)
                else -> ChatScreen(chat, shell.voiceInput, shell.zone)
            }
        }
    }
}
