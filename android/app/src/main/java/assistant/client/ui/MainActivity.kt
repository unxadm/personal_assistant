package assistant.client.ui

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.mutableStateOf
import assistant.client.data.SSO_SCHEME
import assistant.client.notify.Notifications

class MainActivity : ComponentActivity() {
    /** Which tab a tapped notification asked for; cleared once the UI has switched to it. */
    private val requestedTab = mutableStateOf<String?>(null)

    /** The address the browser sent us back to after single sign-on, until the sign-in used it. */
    private val ssoCallback = mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (savedInstanceState == null) {
            requestedTab.value = intent?.getStringExtra(Notifications.EXTRA_TAB)
            ssoCallback.value = ssoAddress(intent)
        }
        setContent {
            AssistantTheme {
                AppRoot(
                    requestedTab = requestedTab.value,
                    onTabShown = { requestedTab.value = null },
                    ssoCallback = ssoCallback.value,
                    onSsoHandled = { ssoCallback.value = null },
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        requestedTab.value = intent.getStringExtra(Notifications.EXTRA_TAB)
        ssoAddress(intent)?.let { ssoCallback.value = it }
    }

    private fun ssoAddress(intent: Intent?): String? =
        intent?.data?.takeIf { it.scheme == SSO_SCHEME }?.toString()
}
