package assistant.client.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Where the app keeps which server it talks to and the session it holds there. */
interface SessionStore {
    val baseUrl: String?
    val cookie: String?
    val allowHttp: Boolean
    val signedIn: StateFlow<Boolean>

    /** Time (epoch ms) of the newest server notification already shown on this device. */
    var notifiedUntil: Long

    fun save(baseUrl: String, cookie: String, allowHttp: Boolean)

    /** Forget the session (the server address is kept for the next sign-in). */
    fun signOut()
}

/** For tests. */
class MemorySessionStore(
    override var baseUrl: String? = null,
    override var cookie: String? = null,
    override var allowHttp: Boolean = false,
) : SessionStore {
    private val state = MutableStateFlow(cookie != null)
    override val signedIn: StateFlow<Boolean> = state
    override var notifiedUntil: Long = -1

    override fun save(baseUrl: String, cookie: String, allowHttp: Boolean) {
        this.baseUrl = baseUrl
        this.cookie = cookie
        this.allowHttp = allowHttp
        state.value = true
    }

    override fun signOut() {
        cookie = null
        notifiedUntil = -1
        state.value = false
    }
}

/**
 * SharedPreferences, with the session token encrypted by an AES-GCM key that lives in the
 * Android Keystore and cannot be exported. The password is never stored.
 */
class KeystoreSessionStore(context: Context) : SessionStore {
    private val prefs = context.getSharedPreferences("session", Context.MODE_PRIVATE)
    private val state = MutableStateFlow(false)

    @Volatile
    private var token: String? = null

    init {
        token = prefs.getString(KEY_COOKIE, null)?.let(::decrypt)
        state.value = token != null && baseUrl != null
    }

    override val baseUrl: String? get() = prefs.getString(KEY_URL, null)
    override val cookie: String? get() = token
    override val allowHttp: Boolean get() = prefs.getBoolean(KEY_HTTP, false)
    override val signedIn: StateFlow<Boolean> = state

    override var notifiedUntil: Long
        get() = prefs.getLong(KEY_NOTIFIED, -1)
        set(value) = prefs.edit().putLong(KEY_NOTIFIED, value).apply()

    override fun save(baseUrl: String, cookie: String, allowHttp: Boolean) {
        prefs.edit()
            .putString(KEY_URL, baseUrl)
            .putString(KEY_COOKIE, encrypt(cookie))
            .putBoolean(KEY_HTTP, allowHttp)
            .remove(KEY_NOTIFIED)
            .apply()
        token = cookie
        state.value = true
    }

    override fun signOut() {
        prefs.edit().remove(KEY_COOKIE).remove(KEY_NOTIFIED).apply()
        token = null
        state.value = false
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORM)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val sealed = cipher.iv + cipher.doFinal(plain.toByteArray())
        return Base64.encodeToString(sealed, Base64.NO_WRAP)
    }

    /** Null when the stored value cannot be read (key lost, data damaged): sign in again. */
    private fun decrypt(stored: String): String? = try {
        val sealed = Base64.decode(stored, Base64.NO_WRAP)
        val cipher = Cipher.getInstance(TRANSFORM)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, sealed, 0, IV_BYTES))
        String(cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES))
    } catch (e: Exception) {
        null
    }

    private companion object {
        const val KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "assistant_session"
        const val TRANSFORM = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
        const val KEY_URL = "base_url"
        const val KEY_COOKIE = "cookie"
        const val KEY_HTTP = "allow_http"
        const val KEY_NOTIFIED = "notified_until"
    }
}
