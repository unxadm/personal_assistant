package assistant.client.data

import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Notifications waiting to be sent, kept on disk encrypted so a phone without a connection
 * does not lose them, and so their text is never readable from the app's files.
 *
 * Oldest first. Bounded: when the phone stays offline long enough, the oldest are dropped.
 */
class PhoneQueue(
    private val file: File,
    private val seal: (String) -> String,
    private val open: (String) -> String?,
    private val capacity: Int = 200,
) {
    private val json = Json { ignoreUnknownKeys = true }
    private val serializer = ListSerializer(PhoneNotification.serializer())
    private var items: MutableList<PhoneNotification>? = null

    @Synchronized
    private fun list(): MutableList<PhoneNotification> = items ?: load().also { items = it }

    private fun load(): MutableList<PhoneNotification> {
        val stored = try {
            if (file.exists()) file.readText() else return mutableListOf()
        } catch (e: Exception) {
            return mutableListOf()
        }
        val plain = open(stored) ?: return mutableListOf()
        return try {
            json.decodeFromString(serializer, plain).toMutableList()
        } catch (e: Exception) {
            mutableListOf()
        }
    }

    private fun save(list: List<PhoneNotification>) {
        try {
            if (list.isEmpty()) file.delete() else file.writeText(seal(json.encodeToString(serializer, list)))
        } catch (e: Exception) {
            // Keep going in memory; the next change tries again.
        }
    }

    @Synchronized
    fun add(n: PhoneNotification) {
        val list = list()
        list.add(n)
        while (list.size > capacity) list.removeAt(0)
        save(list)
    }

    @Synchronized
    fun size(): Int = list().size

    /** The oldest [count] (they stay queued until [drop]). */
    @Synchronized
    fun peek(count: Int): List<PhoneNotification> = list().take(count)

    /** Remove the oldest [count], after they were delivered. Newer ones are untouched. */
    @Synchronized
    fun drop(count: Int) {
        val list = list()
        repeat(minOf(count, list.size)) { list.removeAt(0) }
        save(list)
    }

    @Synchronized
    fun clear() {
        items = mutableListOf()
        try {
            file.delete()
        } catch (e: Exception) {
            // Nothing more to do.
        }
    }
}
