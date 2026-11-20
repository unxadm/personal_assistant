package assistant.client.data

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * What the person chose about forwarding notifications. Off, and no apps, until they say so.
 * Not secret (no notification text), so plain preferences.
 */
class PhoneSettings(context: Context) {
    private val prefs = context.getSharedPreferences("phone", Context.MODE_PRIVATE)
    private val on = MutableStateFlow(prefs.getBoolean(KEY_ON, false))
    private val apps = MutableStateFlow(prefs.getStringSet(KEY_APPS, emptySet()).orEmpty().toSet())
    private val why = MutableStateFlow(prefs.getString(KEY_REFUSED, null))

    val enabled: StateFlow<Boolean> = on
    val allowed: StateFlow<Set<String>> = apps

    /** Why forwarding was switched off by the server, until the person looks at it. */
    val refused: StateFlow<String?> = why

    fun setEnabled(value: Boolean) {
        prefs.edit().putBoolean(KEY_ON, value).apply()
        on.value = value
        if (value) setRefused(null)
    }

    fun setAllowed(packages: Set<String>) {
        prefs.edit().putStringSet(KEY_APPS, packages).apply()
        apps.value = packages
    }

    fun setRefused(message: String?) {
        prefs.edit().putString(KEY_REFUSED, message).apply()
        why.value = message
    }

    private companion object {
        const val KEY_ON = "enabled"
        const val KEY_APPS = "apps"
        const val KEY_REFUSED = "refused"
    }
}
