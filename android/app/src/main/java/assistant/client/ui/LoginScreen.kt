package assistant.client.ui

import android.app.Application
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import assistant.client.data.SsoCallback
import assistant.client.data.SsoRequest
import assistant.client.data.UrlResult
import assistant.client.data.normalizeBaseUrl
import assistant.client.data.parseSsoCallback
import assistant.client.data.ssoStartUrl

class LoginModel(application: Application) : ScreenModel(application) {
    var url by mutableStateOf(app.session.baseUrl.orEmpty())
    var allowHttp by mutableStateOf(app.session.allowHttp)
    var username by mutableStateOf("")
    var password by mutableStateOf("")
    var code by mutableStateOf("")
    var codeNeeded by mutableStateOf(false)
        private set

    val plainHttp get() = url.trim().startsWith("http://", ignoreCase = true)

    private fun serverBase(): String? = when (val result = normalizeBaseUrl(url, allowHttp)) {
        is UrlResult.Error -> {
            error = result.message
            null
        }
        is UrlResult.Ok -> result.base
    }

    /** Opens [open] with the address of the server's single sign-on page. */
    fun signInWithSso(open: (String) -> Unit) {
        if (busy) return
        val base = serverBase() ?: return
        run {
            val status = api.authStatus(base)
            if (status.setupRequired) {
                error = "This server has no account yet. Open $base in a browser to finish the first-time setup, then sign in here."
                return@run
            }
            if (!status.oidc.enabled) {
                error = "This server does not offer single sign-on."
                return@run
            }
            val request = SsoRequest(base, allowHttp && base.startsWith("http://"))
            app.pendingSso = request
            open(ssoStartUrl(base, request.challenge))
        }
    }

    /** The browser is back: [address] carries a one-time code (or why it did not work). */
    fun finishSso(address: String) {
        val callback = parseSsoCallback(address) ?: return
        val request = app.pendingSso
        app.pendingSso = null
        if (request == null || request.expired()) {
            error = "That sign-in expired. Start it again."
            return
        }
        when (callback) {
            is SsoCallback.Failed -> error = callback.message
            is SsoCallback.Code -> run {
                val token = api.ssoExchange(request.base, callback.code, request.verifier)
                app.session.save(request.base, token, request.allowHttp)
            }
        }
    }

    fun signIn() {
        if (busy) return
        val base = serverBase() ?: return
        if (password.isEmpty()) {
            error = "Enter your password"
            return
        }
        run {
            if (api.authStatus(base).setupRequired) {
                error = "This server has no account yet. Open $base in a browser to finish the first-time setup, then sign in here."
                return@run
            }
            try {
                val token = api.login(base, password, code.trim(), username)
                app.session.save(base, token, allowHttp && base.startsWith("http://"))
                password = ""
                code = ""
                codeNeeded = false
            } catch (e: assistant.client.data.ApiException) {
                if (e.status == 401 && e.message == "code_required") codeNeeded = true else throw e
            }
        }
    }
}

@Composable
fun LoginScreen(
    model: LoginModel = viewModel(),
    ssoCallback: String? = null,
    onSsoHandled: () -> Unit = {},
) {
    val context = LocalContext.current
    LaunchedEffect(ssoCallback) {
        if (ssoCallback != null) {
            model.finishSso(ssoCallback)
            onSsoHandled()
        }
    }
    Surface(Modifier.fillMaxSize()) {
        Column(
            Modifier
                .fillMaxSize()
                .systemBarsPadding()
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Assistant", style = MaterialTheme.typography.headlineMedium)
            Text(
                "Sign in to your own server. This app talks to that server and nothing else.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            OutlinedTextField(
                value = model.url,
                onValueChange = { model.url = it },
                label = { Text("Server address") },
                placeholder = { Text("https://assistant.example.com") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next),
                modifier = Modifier.fillMaxWidth(),
            )
            if (model.plainHttp) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = model.allowHttp, onCheckedChange = { model.allowHttp = it })
                    Text(
                        "Allow unencrypted HTTP. Anyone on the network path can read your password and messages; " +
                            "only use this over a VPN or a network you trust.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            OutlinedTextField(
                value = model.username,
                onValueChange = { model.username = it },
                label = { Text("Username") },
                supportingText = { Text("Leave empty if you are the only user of this server") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii, imeAction = ImeAction.Next),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = model.password,
                onValueChange = { model.password = it },
                label = { Text("Password") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(
                    keyboardType = KeyboardType.Password,
                    imeAction = if (model.codeNeeded) ImeAction.Next else ImeAction.Done,
                ),
                keyboardActions = KeyboardActions(onDone = { model.signIn() }),
                modifier = Modifier.fillMaxWidth(),
            )
            if (model.codeNeeded) {
                OutlinedTextField(
                    value = model.code,
                    onValueChange = { model.code = it },
                    label = { Text("Code from your authenticator app") },
                    supportingText = { Text("Or one of your recovery codes") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii, imeAction = ImeAction.Done),
                    keyboardActions = KeyboardActions(onDone = { model.signIn() }),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            ErrorLine(model.error, Modifier.padding(horizontal = 0.dp))
            Button(onClick = model::signIn, enabled = !model.busy, modifier = Modifier.fillMaxWidth()) {
                Text(if (model.busy) "Signing in…" else "Sign in")
            }
            OutlinedButton(
                onClick = {
                    model.signInWithSso { address ->
                        try {
                            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(address)))
                        } catch (e: ActivityNotFoundException) {
                            model.error = "No web browser is available to sign in with."
                        }
                    }
                },
                enabled = !model.busy,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("Sign in with single sign-on")
            }
            Text(
                "Opens your browser to sign in with your organisation's login, if this server offers it.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
