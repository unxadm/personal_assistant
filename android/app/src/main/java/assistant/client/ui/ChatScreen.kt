package assistant.client.ui

import android.Manifest
import android.app.Application
import android.content.pm.PackageManager
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Image
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewModelScope
import assistant.client.data.ApiException
import assistant.client.data.AttachmentInfo
import assistant.client.data.ChatMessage
import assistant.client.data.ConversationInfo
import assistant.client.data.MessageMeta
import assistant.client.data.PendingAction
import assistant.client.data.formatWhen
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.time.ZoneId

class ChatModel(application: Application) : ScreenModel(application) {
    var conversations by mutableStateOf<List<ConversationInfo>>(emptyList())
        private set
    var conversationId by mutableStateOf<Long?>(null)
        private set
    var messages by mutableStateOf<List<ChatMessage>>(emptyList())
        private set

    /** Things the assistant wants to do that wait for a yes or no. */
    var pending by mutableStateOf<List<PendingAction>>(emptyList())
        private set
    var draft by mutableStateOf("")
    var attachments by mutableStateOf<List<AttachmentInfo>>(emptyList())
        private set
    var sending by mutableStateOf(false)
        private set
    var recording by mutableStateOf(false)
        private set

    private val recorder = VoiceRecorder(application)
    private val thumbnails = HashMap<Long, ImageBitmap>()

    init {
        // Nothing from one session may still be on screen after signing out.
        viewModelScope.launch {
            app.session.signedIn.collect { signedIn -> if (!signedIn) clear() }
        }
    }

    private fun clear() {
        recorder.cancel()
        recording = false
        conversations = emptyList()
        conversationId = null
        messages = emptyList()
        pending = emptyList()
        attachments = emptyList()
        draft = ""
        thumbnails.clear()
    }

    fun loadConversations() = run { conversations = api.conversations() }

    fun open(id: Long) = run {
        conversationId = id
        messages = emptyList()
        pending = emptyList()
        refresh()
    }

    fun startNew(text: String = "") {
        conversationId = null
        messages = emptyList()
        pending = emptyList()
        attachments = emptyList()
        draft = text
        error = null
    }

    private suspend fun refresh() {
        val id = conversationId ?: return
        messages = api.messages(id)
        pending = api.actions(id).filter { it.status == "pending" }
    }

    fun delete(id: Long) = run {
        api.deleteConversation(id)
        if (conversationId == id) startNew()
        conversations = api.conversations()
    }

    fun send() {
        val text = draft.trim()
        val images = attachments
        if (sending || (text.isEmpty() && images.isEmpty())) return
        val before = messages
        messages = before + ChatMessage(id = -1, role = "user", content = text, meta = MessageMeta(attachments = images))
        draft = ""
        attachments = emptyList()
        sending = true
        run {
            try {
                val reply = api.chat(text, conversationId, images.map { it.id })
                conversationId = reply.conversationId
                messages = messages + reply.message
                refresh()
                conversations = api.conversations()
            } catch (e: ApiException) {
                // Put everything back so nothing typed is lost.
                messages = before
                if (draft.isEmpty()) draft = text
                attachments = images + attachments
                throw e
            } finally {
                sending = false
            }
        }
    }

    fun attach(uri: Uri) = run {
        val jpeg = try {
            withContext(Dispatchers.IO) { readAsJpeg(getApplication(), uri) }
        } catch (e: Exception) {
            throw ApiException(0, "Could not read that picture")
        }
        attachments = attachments + api.uploadImage(jpeg, "photo.jpg")
    }

    fun removeAttachment(id: Long) {
        attachments = attachments.filterNot { it.id == id }
    }

    fun resolve(action: PendingAction, approve: Boolean) = run {
        api.resolveAction(action.id, approve)
        refresh()
    }

    fun toggleRecording() {
        if (!recording) {
            try {
                recorder.start()
                recording = true
            } catch (e: Exception) {
                error = "Could not start recording"
            }
            return
        }
        recording = false
        val audio = recorder.stop()
        if (audio == null) {
            error = "Nothing was recorded"
            return
        }
        run {
            val text = api.transcribe(audio, recorder.mime).trim()
            if (text.isNotEmpty()) draft = if (draft.isBlank()) text else "${draft.trimEnd()} $text"
        }
    }

    suspend fun thumbnail(id: Long): ImageBitmap? {
        thumbnails[id]?.let { return it }
        return try {
            val bytes = api.attachment(id)
            val bitmap = withContext(Dispatchers.Default) { decodeSampled(bytes, 512) } ?: return null
            bitmap.asImageBitmap().also { thumbnails[id] = it }
        } catch (e: ApiException) {
            null
        }
    }

    override fun onCleared() {
        recorder.cancel()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(model: ChatModel, voiceInput: Boolean, zone: ZoneId) {
    var showHistory by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()
    val title = model.conversations.firstOrNull { it.id == model.conversationId }?.title ?: "New conversation"

    LaunchedEffect(Unit) { model.loadConversations() }
    LaunchedEffect(model.messages.size, model.pending.size, model.sending) {
        val last = model.messages.size + model.pending.size + (if (model.sending) 1 else 0) - 1
        if (last >= 0) listState.animateScrollToItem(last)
    }

    Scaffold(
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            TopAppBar(
                title = { Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                actions = {
                    IconButton(onClick = { model.startNew() }) { Icon(Icons.Filled.Add, "New conversation") }
                    IconButton(onClick = {
                        model.loadConversations()
                        showHistory = true
                    }) { Icon(Icons.Filled.History, "Earlier conversations") }
                },
            )
        },
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth(),
                contentPadding = PaddingValues(12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (model.messages.isEmpty() && !model.busy) {
                    item {
                        Text(
                            "Ask about your mail and calendar, set a reminder, or attach a picture.",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(24.dp),
                        )
                    }
                }
                items(model.messages, key = { if (it.id < 0) "local" else "m${it.id}" }) { message ->
                    MessageBubble(message, model)
                }
                items(model.pending, key = { "a${it.id}" }) { action ->
                    ActionCard(action, enabled = !model.busy, onResolve = { approve -> model.resolve(action, approve) })
                }
                if (model.sending) {
                    item(key = "thinking") {
                        Text("Thinking…", color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(8.dp))
                    }
                }
            }
            if (model.busy && !model.sending) LinearProgressIndicator(Modifier.fillMaxWidth())
            ErrorLine(model.error)
            Composer(model, voiceInput)
        }
    }

    if (showHistory) {
        ModalBottomSheet(onDismissRequest = { showHistory = false }) {
            LazyColumn(Modifier.fillMaxWidth()) {
                if (model.conversations.isEmpty()) {
                    item { Text("No conversations yet.", Modifier.padding(24.dp)) }
                }
                items(model.conversations, key = { it.id }) { conversation ->
                    var confirm by remember { mutableStateOf(false) }
                    ListItem(
                        headlineContent = { Text(conversation.title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                        supportingContent = { Text(formatWhen(conversation.updatedAt, zone)) },
                        trailingContent = {
                            IconButton(onClick = { confirm = true }) { Icon(Icons.Filled.Delete, "Delete conversation") }
                        },
                        modifier = Modifier.clickable {
                            model.open(conversation.id)
                            showHistory = false
                        },
                    )
                    if (confirm) {
                        AlertDialog(
                            onDismissRequest = { confirm = false },
                            title = { Text("Delete this conversation?") },
                            text = { Text("\"${conversation.title}\" and its pictures are removed from the server. This cannot be undone.") },
                            confirmButton = {
                                TextButton(onClick = {
                                    confirm = false
                                    model.delete(conversation.id)
                                }) { Text("Delete") }
                            },
                            dismissButton = { TextButton(onClick = { confirm = false }) { Text("Keep") } },
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun MessageBubble(message: ChatMessage, model: ChatModel) {
    val mine = message.role == "user"
    val colors = MaterialTheme.colorScheme
    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (mine) Arrangement.End else Arrangement.Start) {
        Column(
            Modifier
                .widthIn(max = 520.dp)
                .fillMaxWidth(0.88f)
                .clip(RoundedCornerShape(14.dp))
                .background(if (mine) colors.primaryContainer else colors.surfaceVariant)
                .padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            for (attachment in message.meta.attachments) AttachmentImage(attachment.id, model, Modifier.fillMaxWidth().height(180.dp))
            if (message.content.isNotBlank()) {
                SelectionContainer {
                    MarkdownText(message.content, if (mine) colors.onPrimaryContainer else colors.onSurfaceVariant)
                }
            }
            val tools = message.meta.toolEvents.map { it.tool.replace('_', ' ') }.distinct()
            if (tools.isNotEmpty()) {
                Text(
                    "Used: ${tools.joinToString(", ")}",
                    style = MaterialTheme.typography.labelSmall,
                    color = colors.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun AttachmentImage(id: Long, model: ChatModel, modifier: Modifier) {
    var image by remember(id) { mutableStateOf<ImageBitmap?>(null) }
    LaunchedEffect(id) { image = model.thumbnail(id) }
    val loaded = image
    if (loaded != null) {
        Image(loaded, "Attached picture", modifier.clip(RoundedCornerShape(8.dp)), contentScale = ContentScale.Crop)
    } else {
        Box(modifier.clip(RoundedCornerShape(8.dp)).background(MaterialTheme.colorScheme.surface))
    }
}

@Composable
private fun ActionCard(action: PendingAction, enabled: Boolean, onResolve: (Boolean) -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("Needs your approval", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
            Text(action.tool.replace('_', ' ').replaceFirstChar { it.uppercase() }, fontWeight = FontWeight.SemiBold)
            for ((name, value) in describeArguments(action)) {
                Text("$name: $value", style = MaterialTheme.typography.bodySmall, maxLines = 6, overflow = TextOverflow.Ellipsis)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { onResolve(true) }, enabled = enabled) { Text("Approve") }
                OutlinedButton(onClick = { onResolve(false) }, enabled = enabled) { Text("Reject") }
            }
        }
    }
}

/** The tool's arguments as "name: value" lines, exactly as they will be used. */
private fun describeArguments(action: PendingAction): List<Pair<String, String>> {
    val arguments = action.arguments as? JsonObject ?: return emptyList()
    return arguments.map { (name, value) ->
        name.replace('_', ' ') to if (value is JsonPrimitive) value.content else value.toString()
    }
}

@Composable
private fun Composer(model: ChatModel, voiceInput: Boolean) {
    val context = LocalContext.current
    val pickImage = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) model.attach(uri)
    }
    val askMicrophone = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) model.toggleRecording() else model.error = "Voice input needs the microphone permission"
    }
    Column(Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp)) {
        if (model.attachments.isNotEmpty()) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(bottom = 6.dp)) {
                for (attachment in model.attachments) {
                    Box {
                        AttachmentImage(attachment.id, model, Modifier.size(64.dp))
                        IconButton(
                            onClick = { model.removeAttachment(attachment.id) },
                            modifier = Modifier.align(Alignment.TopEnd).size(24.dp),
                        ) { Icon(Icons.Filled.Close, "Remove picture") }
                    }
                }
            }
        }
        Row(verticalAlignment = Alignment.Bottom) {
            IconButton(onClick = {
                pickImage.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
            }) { Icon(Icons.Filled.Image, "Attach a picture") }
            if (voiceInput) {
                IconButton(onClick = {
                    val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                        PackageManager.PERMISSION_GRANTED
                    if (granted || model.recording) model.toggleRecording() else askMicrophone.launch(Manifest.permission.RECORD_AUDIO)
                }) {
                    if (model.recording) {
                        Icon(Icons.Filled.Stop, "Stop recording", tint = MaterialTheme.colorScheme.error)
                    } else {
                        Icon(Icons.Filled.Mic, "Dictate a message")
                    }
                }
            }
            OutlinedTextField(
                value = model.draft,
                onValueChange = { model.draft = it },
                placeholder = { Text(if (model.recording) "Recording… tap stop when done" else "Message") },
                maxLines = 5,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.size(4.dp))
            IconButton(
                onClick = model::send,
                enabled = !model.sending && (model.draft.isNotBlank() || model.attachments.isNotEmpty()),
            ) { Icon(Icons.AutoMirrored.Filled.Send, "Send") }
        }
    }
}
