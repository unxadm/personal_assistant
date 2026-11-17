# Android app

A native client (Kotlin, Jetpack Compose) for your own assistant server. It talks to that
server and to nothing else: no analytics, no crash reporting, no Google push service.

## What it does

| Tab | |
|---|---|
| **Chat** | Conversations with the assistant, earlier conversations, picture attachments, dictation (when a speech-to-text model is configured), and approve/reject for actions that wait for you |
| **Inbox** | Proactive suggestions: accept, "just remind me instead", dismiss, useful / not useful, Check now |
| **Reminders** | Add in plain words, mark done, snooze, delete |
| **Briefing** | Today's briefing under its headings, earlier days, "Write a new one" |
| **More** | What the assistant changed (with undo), sign out |

Settings, plugins, the memory editor and account security (password, two-step sign-in)
stay in the web interface; **More → Open web interface** takes you there. First-time setup
of a new server is also done in the browser.

## Build and install

You need JDK 17 or newer and the Android SDK (platform 35). Android Studio provides both:
open the `android/` folder and press Run. From a terminal:

```bash
cd android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
./gradlew assembleRelease
adb install -r app/build/outputs/apk/release/app-release.apk
```

The release build is signed with your machine's debug key, which is fine for your own
phone. Set a `signingConfig` of your own in [app/build.gradle.kts](app/build.gradle.kts)
before giving the APK to anyone else.

## Signing in

Enter the server's address, your password and, if two-step sign-in is on, the code from
your authenticator app (or a recovery code).

- **HTTPS** is expected. Certificate authorities you installed on the phone are trusted, so
  a private CA works.
- **Plain `http://`** is refused unless you tick *Allow unencrypted HTTP* on the sign-in
  screen. Over HTTP anyone on the network path can read your password and messages; use it
  only on a network you trust or through a VPN such as WireGuard or Tailscale.
- The phone must be able to reach the server. A server bound to `127.0.0.1` (the default)
  is not reachable from a phone; see "Before exposing it beyond your machine" in the main
  README.

The app stores the server address and the session token, never the password. The token is
encrypted with a key in the Android Keystore and excluded from backups and device
transfer. A session lasts `auth.session_ttl_hours` (14 days by default); **Sign out other
sessions** in the web interface signs the phone out too.

## Notifications

The server's own push channel is Web Push, which only a browser can receive, and Google's
FCM would route every notification through Google. So the app polls instead: it asks the
server what it has sent since the last check

- about every 15 minutes in the background (the shortest interval Android allows), and
- every minute while the app is open.

Reminder notifications have **Done** and **Snooze** buttons. Limits to know about:

- A reminder can show up to 15 minutes late on the phone, and later still when Android is
  saving battery. For reminders that must be on time, also enable Telegram or email.
- Every notification the server sends is shown, whatever channels its category is routed
  to. Notifications held for quiet hours appear when the server releases them.
- Lock-screen notifications hide their text.

## Tests

```bash
./gradlew testDebugUnitTest lintDebug
```

Unit tests cover address handling, the API client (against a local mock server) and
Markdown rendering. `LiveServerTest` runs the client against a real server and is skipped
unless `PA_TEST_URL` and `PA_TEST_PASSWORD` are set; point it only at a throwaway instance.

## Layout

```
app/src/main/java/assistant/client/
  data/     Api (REST client), SessionStore (Keystore-encrypted session), models, helpers
  notify/   polling worker, notification buttons
  ui/       sign-in, chat, inbox, reminders, more
```
