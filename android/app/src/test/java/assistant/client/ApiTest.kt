package assistant.client

import assistant.client.data.Api
import assistant.client.data.ApiException
import assistant.client.data.MemorySessionStore
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class ApiTest {
    private val server = MockWebServer()
    private lateinit var base: String

    @Before
    fun start() {
        server.start()
        base = server.url("/").toString().trimEnd('/')
    }

    @After
    fun stop() = server.shutdown()

    private fun json(body: String, code: Int = 200) =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    @Test
    fun loginReturnsTheSessionAndNeverSendsAnOldOne() = runBlocking {
        val session = MemorySessionStore(baseUrl = "https://old.example", cookie = "stale")
        server.enqueue(json("""{"ok":true}""").addHeader("Set-Cookie", "pa_session=fresh; HttpOnly; Path=/"))

        assertEquals("fresh", Api(session).login(base, "hunter22", ""))

        val request = server.takeRequest()
        assertEquals("/api/auth/login", request.path)
        assertNull(request.getHeader("Cookie"))
        assertNull(request.getHeader("Origin"))
        assertEquals("""{"password":"hunter22","code":""}""", request.body.readUtf8())
    }

    @Test
    fun codeRequiredIsReportedAsIs() = runBlocking {
        server.enqueue(json("""{"detail":"code_required"}""", 401))
        try {
            Api(MemorySessionStore()).login(base, "pw", "")
            fail("expected a failure")
        } catch (e: ApiException) {
            assertEquals(401, e.status)
            assertEquals("code_required", e.message)
        }
    }

    @Test
    fun signedInRequestsCarryTheCookie() = runBlocking {
        val session = MemorySessionStore(baseUrl = base, cookie = "tok")
        server.enqueue(json("""{"conversation_id":7,"message":{"id":3,"role":"assistant","content":"Hi","meta":{"tool_events":[{"tool":"create_reminder","arguments":{"text":"x"},"ok":true,"pending_action_id":4}],"unknown":1}}}"""))

        val reply = Api(session).chat("hello", null, listOf(5))

        assertEquals(7, reply.conversationId)
        assertEquals(4L, reply.message.meta.toolEvents.single().pendingActionId)
        val request = server.takeRequest()
        assertEquals("pa_session=tok", request.getHeader("Cookie"))
        assertEquals("""{"text":"hello","conversation_id":null,"attachment_ids":[5]}""", request.body.readUtf8())
    }

    @Test
    fun briefingComesInSectionsWithEarlierDays() = runBlocking {
        val session = MemorySessionStore(baseUrl = base, cookie = "tok")
        server.enqueue(json("""{"latest":{"date":"2026-11-16","text":"Busy.\n\nMail\n- One","generated_at":"2026-11-16T06:30:00+00:00","sections":[{"title":"","body":"Busy."},{"title":"Mail","body":"- One"}]},"history":[{"date":"2026-11-15","text":"Quiet.","generated_at":"2026-11-15T06:30:00+00:00"}]}"""))

        val briefing = Api(session).briefing()

        assertEquals(listOf("", "Mail"), briefing.latest!!.sections.map { it.title })
        assertEquals("Quiet.", briefing.history.single().text)
        assertTrue(briefing.history.single().sections.isEmpty())
    }

    @Test
    fun anExpiredSessionSignsOut() = runBlocking {
        val session = MemorySessionStore(baseUrl = base, cookie = "tok")
        server.enqueue(json("""{"detail":"Not authenticated"}""", 401))
        try {
            Api(session).reminders()
            fail("expected a failure")
        } catch (e: ApiException) {
            assertEquals(401, e.status)
        }
        assertFalse(session.signedIn.value)
        assertNull(session.cookie)
    }

    @Test
    fun redirectsAreNotFollowed() = runBlocking {
        val session = MemorySessionStore(baseUrl = base, cookie = "tok")
        server.enqueue(MockResponse().setResponseCode(302).setHeader("Location", "http://elsewhere.example/api/reminders"))
        try {
            Api(session).reminders()
            fail("expected a failure")
        } catch (e: ApiException) {
            assertEquals(302, e.status)
        }
        assertEquals(1, server.requestCount)
        assertTrue(session.signedIn.value)
    }

    @Test
    fun serverErrorsShowTheServersOwnWording() = runBlocking {
        val session = MemorySessionStore(baseUrl = base, cookie = "tok")
        server.enqueue(json("""{"detail":"I could not find a time in that"}""", 422))
        try {
            Api(session).quickReminder("something")
            fail("expected a failure")
        } catch (e: ApiException) {
            assertEquals("I could not find a time in that", e.message)
        }
    }

    @Test
    fun aPageThatIsNotTheAssistantIsExplained() = runBlocking {
        server.enqueue(MockResponse().setBody("<html>router login</html>"))
        try {
            Api(MemorySessionStore()).authStatus(base)
            fail("expected a failure")
        } catch (e: ApiException) {
            assertTrue(e.message!!.contains("does not understand"))
        }
    }
}
