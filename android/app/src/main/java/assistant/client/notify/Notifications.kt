package assistant.client.notify

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import assistant.client.App
import assistant.client.R
import assistant.client.data.ApiException
import assistant.client.data.ServerNotification
import assistant.client.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.time.OffsetDateTime
import java.util.concurrent.TimeUnit

/**
 * Phone notifications without a push service.
 *
 * Google's push service (FCM) would route every notification through Google, which this
 * project rules out. Instead the app asks the server what it has sent since the last check:
 * every 15 minutes in the background (the shortest interval Android allows for periodic
 * work) and once a minute while the app is open.
 */
object Notifications {
    const val EXTRA_TAB = "tab"
    const val EXTRA_REF = "ref"
    const val EXTRA_ACTION = "action"
    const val EXTRA_ID = "notification_id"

    private const val WORK = "notifications"
    private const val REMINDERS = "reminders"
    private const val SUGGESTIONS = "suggestions"
    private const val GENERAL = "general"
    private val lock = Mutex()

    fun createChannels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannels(
            listOf(
                NotificationChannel(REMINDERS, context.getString(R.string.channel_reminders), NotificationManager.IMPORTANCE_HIGH),
                NotificationChannel(SUGGESTIONS, context.getString(R.string.channel_suggestions), NotificationManager.IMPORTANCE_DEFAULT),
                NotificationChannel(GENERAL, context.getString(R.string.channel_general), NotificationManager.IMPORTANCE_DEFAULT),
            ),
        )
    }

    fun schedule(context: Context) {
        val request = PeriodicWorkRequestBuilder<PollWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    fun allowed(context: Context): Boolean =
        Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    /** Show what the server sent since the last check. Returns false if it could not ask. */
    suspend fun sync(context: Context): Boolean = lock.withLock {
        val app = context.applicationContext as App
        if (!app.session.signedIn.value) return true
        val recent = try {
            app.api.notifications()
        } catch (e: ApiException) {
            // Signed out meanwhile: nothing to retry. Anything else: try again later.
            return e.status == 401
        }
        val sent = recent.mapNotNull { n -> sentAt(n)?.let { n to it } }
        val since = app.session.notifiedUntil
        val newest = sent.maxOfOrNull { it.second } ?: return true
        // First check after signing in: start from now instead of replaying old ones.
        if (since >= 0 && allowed(context)) {
            sent.filter { it.second > since }.sortedBy { it.second }.forEach { show(context, it.first) }
        }
        if (newest > since) app.session.notifiedUntil = newest
        true
    }

    /** When it was delivered (epoch ms); null while it is held back, e.g. for quiet hours. */
    private fun sentAt(n: ServerNotification): Long? {
        if (n.status != "sent" || n.sentAt == null) return null
        return try {
            OffsetDateTime.parse(n.sentAt).toInstant().toEpochMilli()
        } catch (e: Exception) {
            null
        }
    }

    private fun show(context: Context, n: ServerNotification) {
        val id = (n.id % Int.MAX_VALUE).toInt()
        val kind = n.ref?.substringBefore(':').orEmpty()
        val (channel, tab) = when {
            kind == "reminder" -> REMINDERS to "reminders"
            kind == "suggestion" -> SUGGESTIONS to "inbox"
            n.category == "briefing" -> GENERAL to "briefing"
            else -> GENERAL to "more"
        }
        val open = Intent(context, MainActivity::class.java)
            .putExtra(EXTRA_TAB, tab)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val builder = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(n.title)
            .setContentText(n.body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(n.body))
            .setAutoCancel(true)
            // The lock screen shows that something arrived, not what it says.
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setContentIntent(PendingIntent.getActivity(context, id, open, IMMUTABLE))
        if (kind == "reminder" && n.ref != null) {
            builder.addAction(0, "Done", actionIntent(context, id, n.ref, "done"))
            builder.addAction(0, "Snooze", actionIntent(context, id, n.ref, "snooze"))
        }
        try {
            NotificationManagerCompat.from(context).notify(id, builder.build())
        } catch (e: SecurityException) {
            // Permission was withdrawn between the check and now.
        }
    }

    private fun actionIntent(context: Context, id: Int, ref: String, action: String): PendingIntent {
        val intent = Intent(context, ActionReceiver::class.java)
            .putExtra(EXTRA_REF, ref)
            .putExtra(EXTRA_ACTION, action)
            .putExtra(EXTRA_ID, id)
        // A distinct request code per button, or the two would share one PendingIntent.
        return PendingIntent.getBroadcast(context, id * 2 + if (action == "done") 0 else 1, intent, IMMUTABLE)
    }

    private const val IMMUTABLE = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
}

class PollWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result =
        if (Notifications.sync(applicationContext)) Result.success() else Result.retry()
}

/** "Done" / "Snooze" on a reminder notification. */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val ref = intent.getStringExtra(Notifications.EXTRA_REF) ?: return
        val action = intent.getStringExtra(Notifications.EXTRA_ACTION) ?: return
        val id = intent.getIntExtra(Notifications.EXTRA_ID, 0)
        val app = context.applicationContext as App
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                app.api.notificationAction(ref, action)
                NotificationManagerCompat.from(context).cancel(id)
            } catch (e: ApiException) {
                // Leave the notification in place so the button can be tried again.
            } finally {
                pending.finish()
            }
        }
    }
}
