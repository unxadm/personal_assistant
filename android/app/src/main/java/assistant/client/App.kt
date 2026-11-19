package assistant.client

import android.app.Application
import assistant.client.data.Api
import assistant.client.data.KeystoreSessionStore
import assistant.client.data.SessionStore
import assistant.client.data.SsoRequest
import assistant.client.notify.Notifications

class App : Application() {
    lateinit var session: SessionStore
        private set
    lateinit var api: Api
        private set

    /** The single sign-on waiting for the browser to come back (memory only, see [SsoRequest]). */
    @Volatile
    var pendingSso: SsoRequest? = null

    override fun onCreate() {
        super.onCreate()
        session = KeystoreSessionStore(this)
        api = Api(session)
        Notifications.createChannels(this)
        Notifications.schedule(this)
    }
}
