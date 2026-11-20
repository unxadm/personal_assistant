package assistant.client.notify

import android.app.Notification
import android.content.Context
import android.content.pm.PackageManager
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import assistant.client.App
import assistant.client.data.ApiException
import assistant.client.data.RawNotification
import assistant.client.data.RecentNotifications
import assistant.client.data.capture
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import java.util.concurrent.TimeUnit

/**
 * Sees the notifications other apps post, once the person has granted notification access
 * in Android's settings. It passes on only what [capture] allows: apps they picked, nothing
 * ongoing, secret or looking like a verification code. Nothing is written to the log.
 */
class PhoneListener : NotificationListenerService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val recent = RecentNotifications()

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val app = application as App
        if (!app.session.signedIn.value || !app.phone.enabled.value) return
        val allowed = app.phone.allowed.value
        if (sbn.packageName !in allowed) return
        val raw = raw(sbn) ?: return
        val n = capture(raw, allowed, packageName) ?: return
        if (!recent.isNew(n)) return
        scope.launch {
            app.phoneQueue.add(n)
            PhoneUpload.schedule(applicationContext)
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun raw(sbn: StatusBarNotification): RawNotification? {
        val notification = sbn.notification ?: return null
        val extras = notification.extras ?: return null
        val lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)?.joinToString("\n")
        val text = lines?.takeIf { it.isNotBlank() }
            ?: extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
            ?: extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
            ?: ""
        return RawNotification(
            pkg = sbn.packageName,
            key = sbn.key.orEmpty(),
            app = label(sbn.packageName),
            title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty(),
            text = text,
            postedAt = sbn.postTime,
            category = notification.category.orEmpty(),
            ongoing = sbn.isOngoing,
            groupSummary = notification.flags and Notification.FLAG_GROUP_SUMMARY != 0,
            secret = notification.visibility == Notification.VISIBILITY_SECRET,
        )
    }

    private fun label(pkg: String): String = try {
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    } catch (e: PackageManager.NameNotFoundException) {
        pkg
    }
}

/** Sends what the listener queued: in batches, shortly after the last one arrived, when online. */
object PhoneUpload {
    private const val WORK = "phone-upload"
    private const val BATCH = 50

    fun schedule(context: Context) {
        // A short delay lets a burst of messages go out as one request.
        val request = OneTimeWorkRequestBuilder<PhoneUploadWorker>()
            .setInitialDelay(20, TimeUnit.SECONDS)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(WORK, ExistingWorkPolicy.KEEP, request)
    }

    /** True when everything was delivered (or nothing needs sending any more). */
    suspend fun drain(context: Context): Boolean {
        val app = context.applicationContext as App
        // Without a session or consent nothing belongs in the queue.
        if (!app.session.signedIn.value || !app.phone.enabled.value) {
            app.phoneQueue.clear()
            return true
        }
        while (true) {
            val batch = app.phoneQueue.peek(BATCH)
            if (batch.isEmpty()) return true
            try {
                app.api.sendPhoneNotifications(batch)
                app.phoneQueue.drop(batch.size)
            } catch (e: ApiException) {
                when {
                    // Signed out meanwhile: the queue is cleared when the session ends.
                    e.status == 401 -> return true
                    // The server does not want them (turned off for this account): stop, and say so.
                    e.status == 403 -> {
                        app.phone.setRefused(e.message)
                        app.phone.setEnabled(false)
                        app.phoneQueue.clear()
                        return true
                    }
                    e.status == 0 || e.status == 429 || e.status >= 500 -> return false
                    // A batch the server cannot accept will not get better by repeating it.
                    else -> app.phoneQueue.drop(batch.size)
                }
            }
        }
    }
}

class PhoneUploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result =
        if (PhoneUpload.drain(applicationContext)) Result.success() else Result.retry()
}
