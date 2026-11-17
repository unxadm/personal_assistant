package assistant.client

import android.app.Application
import assistant.client.data.Api
import assistant.client.data.KeystoreSessionStore
import assistant.client.data.SessionStore
import assistant.client.notify.Notifications

class App : Application() {
    lateinit var session: SessionStore
        private set
    lateinit var api: Api
        private set

    override fun onCreate() {
        super.onCreate()
        session = KeystoreSessionStore(this)
        api = Api(session)
        Notifications.createChannels(this)
        Notifications.schedule(this)
    }
}
