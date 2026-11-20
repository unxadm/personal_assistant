package assistant.client

import assistant.client.data.PhoneNotification
import assistant.client.data.PhoneQueue
import assistant.client.data.RawNotification
import assistant.client.data.RecentNotifications
import assistant.client.data.capture
import assistant.client.data.looksLikeCode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class PhoneCaptureTest {
    @get:Rule
    val folder = TemporaryFolder()

    private val signal = "org.signal"
    private val own = "assistant.client"

    private fun raw(
        pkg: String = signal,
        title: String = "Alex",
        text: String = "Lunch?",
        category: String = "msg",
        ongoing: Boolean = false,
        groupSummary: Boolean = false,
        secret: Boolean = false,
    ) = RawNotification(pkg, "0|$pkg|1", "Signal", title, text, 1_000L, category, ongoing, groupSummary, secret)

    private fun n(text: String = "x") = PhoneNotification("k", signal, "Signal", "t", text, 1L, "msg")

    @Test
    fun nothingIsSentUntilAppsAreChosen() {
        assertNull(capture(raw(), emptySet(), own))
        assertNull(capture(raw(), setOf("com.other"), own))
        assertNotNull(capture(raw(), setOf(signal), own))
    }

    @Test
    fun theAppNeverForwardsItsOwnNotifications() {
        assertNull(capture(raw(pkg = own), setOf(own), own))
    }

    @Test
    fun ongoingSummarySecretAndQuietKindsAreLeftOut() {
        val allowed = setOf(signal)
        assertNull(capture(raw(ongoing = true), allowed, own))
        assertNull(capture(raw(groupSummary = true), allowed, own))
        assertNull(capture(raw(secret = true), allowed, own))
        for (category in listOf("transport", "progress", "service", "sys", "status", "call")) {
            assertNull(category, capture(raw(category = category), allowed, own))
        }
        assertNull(capture(raw(title = " ", text = ""), allowed, own))
    }

    @Test
    fun whatIsSentIsTheTextAndNothingElse() {
        val sent = capture(raw(title = "Al\u0000ex ", text = "Lunch?\u0007"), setOf(signal), own)!!
        assertEquals(PhoneNotification("0|org.signal|1", signal, "Signal", "Alex", "Lunch?", 1_000L, "msg"), sent)
    }

    @Test
    fun longTextIsCutBeforeItLeavesThePhone() {
        val sent = capture(raw(title = "T".repeat(900), text = "x".repeat(9000)), setOf(signal), own)!!
        assertEquals(500, sent.title.length)
        assertEquals(4000, sent.text.length)
    }

    @Test
    fun verificationCodesAreRecognisedLikeOnTheServer() {
        // The same cases as tests/test_phone.py: keep the two rules in step.
        assertTrue(looksLikeCode("Bank", "123456 is your verification code. Do not share it."))
        assertTrue(looksLikeCode("Google", "G-482913 is your Google verification code"))
        assertTrue(looksLikeCode("Shop", "Your one-time password: 7731"))
        assertTrue(looksLikeCode("App", "Use code 482 913 to sign in"))
        assertTrue(looksLikeCode("Login", "Your PIN is 4821"))
        assertFalse(looksLikeCode("Shop", "Order 12345 has shipped. Arrives Thursday."))
        assertFalse(looksLikeCode("Alex", "Lunch at 12?"))
        assertNull(capture(raw(text = "Your verification code is 552201"), setOf(signal), own))
    }

    @Test
    fun aRefreshedNotificationIsSentOnce() {
        val recent = RecentNotifications()
        assertTrue(recent.isNew(n("hello")))
        assertFalse(recent.isNew(n("hello")))
        assertTrue(recent.isNew(n("hello again")))
        assertTrue(recent.isNew(n("hello").copy(key = "other")))
    }

    @Test
    fun theQueueKeepsOrderSurvivesARestartAndIsSealedOnDisk() {
        val file = folder.newFile("queue")
        val seal = { s: String -> s.reversed() }
        val open = { s: String -> s.reversed() }
        val queue = PhoneQueue(file, seal, open)
        queue.add(n("one"))
        queue.add(n("two"))
        queue.add(n("three"))
        assertFalse("sealed, not plain", file.readText().contains("one"))

        val reopened = PhoneQueue(file, seal, open)
        assertEquals(listOf("one", "two"), reopened.peek(2).map { it.text })
        reopened.add(n("four"))
        reopened.drop(2) // two were delivered; "three" and "four" were not
        assertEquals(listOf("three", "four"), reopened.peek(10).map { it.text })
        assertEquals(2, PhoneQueue(file, seal, open).size())
    }

    @Test
    fun theQueueIsBoundedAndClears() {
        val file = folder.newFile("queue2")
        val queue = PhoneQueue(file, { it }, { it }, capacity = 3)
        (1..5).forEach { queue.add(n("m$it")) }
        assertEquals(listOf("m3", "m4", "m5"), queue.peek(10).map { it.text })
        queue.clear()
        assertEquals(0, queue.size())
        assertFalse(file.exists())
    }

    @Test
    fun anUnreadableQueueFileStartsEmptyInsteadOfCrashing() {
        val file = folder.newFile("queue3")
        file.writeText("not sealed by us")
        assertEquals(0, PhoneQueue(file, { it }, { null }).size())
        file.writeText("{ broken json")
        assertEquals(0, PhoneQueue(file, { it }, { it }).size())
    }
}
