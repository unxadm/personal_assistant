package assistant.client.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

@Serializable
data class AuthStatus(
    @SerialName("setup_required") val setupRequired: Boolean = false,
    val authenticated: Boolean = false,
)

@Serializable
data class ConversationInfo(
    val id: Long,
    val title: String = "",
    val channel: String = "",
    @SerialName("updated_at") val updatedAt: String = "",
)

@Serializable
data class AttachmentInfo(val id: Long, val mime: String = "", val filename: String = "", val size: Long = 0)

@Serializable
data class ToolEvent(
    val tool: String = "",
    val ok: Boolean = true,
    @SerialName("pending_action_id") val pendingActionId: Long? = null,
)

@Serializable
data class MessageMeta(
    val model: String? = null,
    @SerialName("used_fallback") val usedFallback: Boolean = false,
    @SerialName("tool_events") val toolEvents: List<ToolEvent> = emptyList(),
    val attachments: List<AttachmentInfo> = emptyList(),
)

@Serializable
data class ChatMessage(
    val id: Long,
    val role: String,
    val content: String = "",
    val meta: MessageMeta = MessageMeta(),
)

@Serializable
data class ChatReply(@SerialName("conversation_id") val conversationId: Long, val message: ChatMessage)

@Serializable
data class PendingAction(
    val id: Long,
    @SerialName("conversation_id") val conversationId: Long? = null,
    val tool: String = "",
    val arguments: JsonElement? = null,
    val status: String = "",
    val result: String? = null,
)

@Serializable
data class Suggestion(
    val id: Long,
    val category: String = "",
    val title: String = "",
    val detail: String = "",
    val reason: String = "",
    val confidence: Double = 0.0,
    val source: String = "",
    val proposed: JsonObject? = null,
    @SerialName("action_label") val actionLabel: String? = null,
    val url: String? = null,
    val status: String = "",
    val feedback: String? = null,
    val result: String? = null,
    @SerialName("created_at") val createdAt: String = "",
)

@Serializable
data class SuggestionList(
    val suggestions: List<Suggestion> = emptyList(),
    val pending: Int = 0,
    val enabled: Boolean = true,
)

@Serializable
data class PendingCount(val pending: Int = 0)

@Serializable
data class CheckResult(val created: Int = 0)

@Serializable
data class Reminder(
    val id: Long,
    val text: String = "",
    @SerialName("due_at") val dueAt: String = "",
    @SerialName("due_local") val dueLocal: String = "",
    @SerialName("snoozed_until") val snoozedUntil: String? = null,
    val repeat: String = "",
    val status: String = "",
    val urgency: String = "normal",
)

@Serializable
data class ServerNotification(
    val id: Long,
    val category: String = "",
    val title: String = "",
    val body: String = "",
    val ref: String? = null,
    val urgency: String = "normal",
    val status: String = "",
    @SerialName("sent_at") val sentAt: String? = null,
)

/** Part of a briefing under one heading; the opening paragraph has an empty title. */
@Serializable
data class BriefingSection(val title: String = "", val body: String = "")

@Serializable
data class BriefingText(
    val date: String = "",
    val text: String = "",
    @SerialName("generated_at") val generatedAt: String = "",
    val sections: List<BriefingSection> = emptyList(),
)

@Serializable
data class Briefing(val latest: BriefingText? = null, val history: List<BriefingText> = emptyList())

@Serializable
data class AuditEntry(
    val id: Long,
    val at: String = "",
    val actor: String = "",
    val action: String = "",
    val summary: String = "",
    val change: Boolean = false,
    @SerialName("undo_label") val undoLabel: String? = null,
    @SerialName("undone_at") val undoneAt: String? = null,
)

@Serializable
data class Capabilities(val stt: Boolean = false, val tts: Boolean = false)

/** The few settings the app needs; everything else is edited in the web UI. */
@Serializable
data class ServerSettings(val timezone: String = "UTC", val capabilities: Capabilities = Capabilities())

@Serializable
data class Transcript(val text: String = "")

@Serializable
data class ActionMessage(val message: String = "")
