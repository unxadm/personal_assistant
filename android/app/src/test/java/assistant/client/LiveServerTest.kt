package assistant.client

import assistant.client.data.Api
import assistant.client.data.ApiException
import assistant.client.data.MemorySessionStore
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * Runs the client against a real assistant server, to catch the two drifting apart.
 * Skipped unless PA_TEST_URL and PA_TEST_PASSWORD are set. Point it at a throwaway
 * instance (it creates and deletes a conversation and a reminder), never at your own:
 *
 *     PA_TEST_URL=http://127.0.0.1:8012 PA_TEST_PASSWORD=... ./gradlew testDebugUnitTest --rerun-tasks
 */
class LiveServerTest {
    @Test
    fun signInAndUseEveryScreen() = runBlocking {
        val base = System.getenv("PA_TEST_URL").orEmpty()
        val password = System.getenv("PA_TEST_PASSWORD").orEmpty()
        assumeTrue("PA_TEST_URL / PA_TEST_PASSWORD not set", base.isNotEmpty() && password.isNotEmpty())

        val session = MemorySessionStore()
        val api = Api(session)
        assertFalse(api.authStatus(base).setupRequired)
        try {
            api.login(base, "$password-wrong", "")
            fail("a wrong password was accepted")
        } catch (e: ApiException) {
            assertEquals(401, e.status)
        }
        session.save(base, api.login(base, password, ""), allowHttp = true)

        // Shell
        api.settings().timezone.let { assertTrue(it.isNotEmpty()) }
        api.suggestionCount()

        // Chat
        val reply = api.chat("Hello from the Android client test", null, emptyList())
        assertEquals("assistant", reply.message.role)
        val messages = api.messages(reply.conversationId)
        assertEquals(listOf("user", "assistant"), messages.map { it.role })
        assertTrue(api.conversations().any { it.id == reply.conversationId })
        api.actions(reply.conversationId)
        api.deleteConversation(reply.conversationId)
        assertFalse(api.conversations().any { it.id == reply.conversationId })

        // Reminders (the wording is turned into a time by the server's chat model)
        val reminder = api.quickReminder("water the plants tomorrow at 9")
        assertTrue(reminder.dueAt.isNotEmpty())
        assertTrue(api.reminders().any { it.id == reminder.id })
        api.completeReminder(reminder.id)
        assertFalse(api.reminders().any { it.id == reminder.id })
        api.deleteReminder(reminder.id)

        // Inbox, More, notifications
        api.suggestions()
        api.checkNow()
        api.briefing()
        api.notifications()
        val changes = api.audit(changesOnly = true)
        assertTrue(changes.any { it.summary.isNotEmpty() })

        api.logout()
        assertFalse(session.signedIn.value)
    }
}
