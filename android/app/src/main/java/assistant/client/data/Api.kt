package assistant.client.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.DeserializationStrategy
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.net.URLEncoder
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException

/** [status] is the HTTP status, or 0 when the server could not be reached at all. */
class ApiException(val status: Int, message: String) : Exception(message)

/**
 * The assistant's REST API.
 *
 * The server authenticates with a session cookie. This client keeps the token itself (see
 * [SessionStore]) and sends it only to the configured server: redirects are not followed, so
 * it can never be forwarded to another host or downgraded from HTTPS to HTTP.
 */
class Api(private val session: SessionStore, client: OkHttpClient? = null) {
    private val json = Json {
        ignoreUnknownKeys = true
        coerceInputValues = true
        explicitNulls = false
    }
    private val http = (client ?: OkHttpClient()).newBuilder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        // A reply can take a while when the model calls several tools.
        .readTimeout(5, TimeUnit.MINUTES)
        .followRedirects(false)
        .followSslRedirects(false)
        .build()

    // -- sign-in ----------------------------------------------------------------------

    suspend fun authStatus(base: String): AuthStatus =
        send("GET", "/auth/status", base = base) { decode(AuthStatus.serializer(), it) }

    /** Returns the session token. Throws ApiException(401, "code_required") when a code is needed. */
    suspend fun login(base: String, password: String, code: String): String {
        val body = buildJsonObject {
            put("password", password)
            put("code", code)
        }
        return send("POST", "/auth/login", jsonBody(body), base = base) { res ->
            sessionCookie(res.headers("Set-Cookie"))
                ?: throw ApiException(0, "The server accepted the password but did not start a session")
        }
    }

    suspend fun logout() {
        try {
            send("POST", "/auth/logout") { }
        } catch (e: ApiException) {
            // Signing out locally must work even when the server cannot be reached.
        }
        session.signOut()
    }

    // -- chat -------------------------------------------------------------------------

    suspend fun settings(): ServerSettings = get("/settings", ServerSettings.serializer())

    suspend fun conversations(): List<ConversationInfo> = list("/conversations", ConversationInfo.serializer())

    suspend fun messages(conversationId: Long): List<ChatMessage> =
        list("/conversations/$conversationId/messages", ChatMessage.serializer())

    suspend fun deleteConversation(conversationId: Long) = send("DELETE", "/conversations/$conversationId") { }

    suspend fun chat(text: String, conversationId: Long?, attachmentIds: List<Long>): ChatReply {
        val body = buildJsonObject {
            put("text", text)
            put("conversation_id", conversationId?.let { JsonPrimitive(it) } ?: JsonNull)
            put("attachment_ids", JsonArray(attachmentIds.map { JsonPrimitive(it) }))
        }
        return send("POST", "/chat", jsonBody(body)) { decode(ChatReply.serializer(), it) }
    }

    suspend fun uploadImage(jpeg: ByteArray, name: String): AttachmentInfo {
        val body = jpeg.toRequestBody("image/jpeg".toMediaType())
        val headers = mapOf("X-Filename" to URLEncoder.encode(name, "UTF-8"))
        return send("POST", "/attachments", body, headers = headers) { decode(AttachmentInfo.serializer(), it) }
    }

    suspend fun attachment(id: Long): ByteArray =
        send("GET", "/attachments/$id") { it.body?.bytes() ?: ByteArray(0) }

    suspend fun transcribe(audio: ByteArray, mime: String): String =
        send("POST", "/voice/transcribe", audio.toRequestBody(mime.toMediaType())) {
            decode(Transcript.serializer(), it).text
        }

    suspend fun actions(conversationId: Long): List<PendingAction> =
        list("/actions?conversation_id=$conversationId", PendingAction.serializer())

    suspend fun resolveAction(id: Long, approve: Boolean): PendingAction =
        post("/actions/$id/${if (approve) "approve" else "reject"}", PendingAction.serializer())

    // -- suggestions ------------------------------------------------------------------

    suspend fun suggestions(): SuggestionList = get("/suggestions", SuggestionList.serializer())

    suspend fun suggestionCount(): Int = get("/suggestions/count", PendingCount.serializer()).pending

    suspend fun checkNow(): Int = post("/suggestions/check", CheckResult.serializer()).created

    suspend fun acceptSuggestion(id: Long, asReminder: Boolean = false): Suggestion {
        val body = buildJsonObject { if (asReminder) put("mode", "reminder") }
        return post("/suggestions/$id/accept", Suggestion.serializer(), body)
    }

    suspend fun dismissSuggestion(id: Long): Suggestion = post("/suggestions/$id/dismiss", Suggestion.serializer())

    suspend fun suggestionFeedback(id: Long, useful: Boolean?): Suggestion {
        val body = buildJsonObject { put("useful", useful?.let { JsonPrimitive(it) } ?: JsonNull) }
        return post("/suggestions/$id/feedback", Suggestion.serializer(), body)
    }

    // -- reminders, briefing, notifications ---------------------------------------------

    suspend fun reminders(): List<Reminder> = list("/reminders", Reminder.serializer())

    /** Plain language, e.g. "call the dentist tomorrow at 9". */
    suspend fun quickReminder(text: String): Reminder =
        post("/reminders/quick", Reminder.serializer(), buildJsonObject { put("text", text) })

    suspend fun snoozeReminder(id: Long, minutes: Int? = null): Reminder {
        val body = buildJsonObject { if (minutes != null) put("minutes", minutes) }
        return post("/reminders/$id/snooze", Reminder.serializer(), body)
    }

    suspend fun completeReminder(id: Long): Reminder = post("/reminders/$id/complete", Reminder.serializer())

    suspend fun deleteReminder(id: Long) = send("DELETE", "/reminders/$id") { }

    suspend fun briefing(): Briefing = get("/briefing", Briefing.serializer())

    suspend fun runBriefing(): Briefing = post("/briefing/run", Briefing.serializer())

    suspend fun notifications(limit: Int = 30): List<ServerNotification> =
        list("/notifications?limit=$limit", ServerNotification.serializer())

    /** A button on a notification ("done", "snooze", "accept", "dismiss", "undo"). */
    suspend fun notificationAction(ref: String, action: String): String {
        val body = buildJsonObject {
            put("ref", ref)
            put("action", action)
        }
        return post("/notifications/action", ActionMessage.serializer(), body).message
    }

    // -- activity log -----------------------------------------------------------------

    suspend fun audit(changesOnly: Boolean): List<AuditEntry> =
        list("/audit?changes=$changesOnly", AuditEntry.serializer())

    suspend fun undo(entryId: Long): AuditEntry = post("/audit/$entryId/undo", AuditEntry.serializer())

    // -- plumbing ---------------------------------------------------------------------

    private suspend fun <T> get(path: String, type: DeserializationStrategy<T>): T =
        send("GET", path) { decode(type, it) }

    private suspend fun <T> list(path: String, type: KSerializer<T>): List<T> =
        send("GET", path) { decode(ListSerializer(type), it) }

    private suspend fun <T> post(path: String, type: DeserializationStrategy<T>, body: JsonObject? = null): T =
        send("POST", path, body?.let(::jsonBody)) { decode(type, it) }

    private fun jsonBody(value: JsonObject): RequestBody = value.toString().toRequestBody(JSON)

    private fun <T> decode(type: DeserializationStrategy<T>, response: Response): T {
        val text = response.body?.string().orEmpty()
        return try {
            json.decodeFromString(type, text)
        } catch (e: SerializationException) {
            throw ApiException(0, "The server sent a reply this app does not understand. Is this the assistant's address?")
        }
    }

    /**
     * [base] is given only while signing in, before a server is saved; the stored session is
     * never sent on those requests.
     */
    private suspend fun <T> send(
        method: String,
        path: String,
        body: RequestBody? = null,
        base: String? = null,
        headers: Map<String, String> = emptyMap(),
        read: (Response) -> T,
    ): T = withContext(Dispatchers.IO) {
        val root = base ?: session.baseUrl ?: throw ApiException(0, "Not connected to a server")
        val request = Request.Builder().url("$root/api$path").header("Accept", "application/json")
        if (base == null) {
            val token = session.cookie ?: throw ApiException(401, "Not signed in")
            request.header("Cookie", "$SESSION_COOKIE=$token")
        }
        headers.forEach { (name, value) -> request.header(name, value) }
        request.method(method, body ?: if (method == "GET" || method == "DELETE") null else EMPTY)
        try {
            http.newCall(request.build()).execute().use { response ->
                if (!response.isSuccessful) throw failure(response, signedInCall = base == null)
                read(response)
            }
        } catch (e: IOException) {
            throw ApiException(0, describe(e))
        }
    }

    private fun failure(response: Response, signedInCall: Boolean): ApiException {
        val text = try {
            response.body?.string().orEmpty()
        } catch (e: IOException) {
            ""
        }
        val detail = try {
            when (val value = json.parseToJsonElement(text).jsonObject["detail"]) {
                null -> null
                is JsonPrimitive -> value.jsonPrimitive.content
                else -> value.toString()
            }
        } catch (e: Exception) {
            null
        }
        if (response.code == 401 && signedInCall) {
            // The session expired or was revoked ("Sign out other sessions" in the web UI).
            session.signOut()
            return ApiException(401, "Your session ended. Sign in again.")
        }
        val fallback = when (response.code) {
            in 300..399 -> "The server redirected the request. Enter the address it redirects to (https://?)."
            404 -> "Not found. Is this the assistant's address?"
            429 -> "Too many attempts. Wait a moment and try again."
            else -> "The server answered with an error (${response.code})"
        }
        return ApiException(response.code, detail?.takeIf { it.isNotBlank() } ?: fallback)
    }

    private fun describe(e: IOException): String = when (e) {
        is UnknownHostException -> "Cannot find the server. Check the address and your connection."
        is SSLException -> "Could not make a secure connection: ${e.message ?: "certificate problem"}"
        is java.net.SocketTimeoutException -> "The server took too long to answer."
        is java.net.ConnectException -> "Cannot connect to the server. Is it running and reachable from this phone?"
        else -> e.message ?: "Network error"
    }

    private companion object {
        val JSON = "application/json".toMediaType()
        val EMPTY = ByteArray(0).toRequestBody(null)
    }
}
