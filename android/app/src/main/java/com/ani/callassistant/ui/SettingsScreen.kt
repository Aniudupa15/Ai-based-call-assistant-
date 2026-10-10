package com.ani.callassistant.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.ani.callassistant.CallAssistantApp
import com.ani.callassistant.data.OwnerSettings
import com.ani.callassistant.data.userMessage
import com.ani.callassistant.service.EventPollService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@Composable
fun SettingsScreen() {
    val context = LocalContext.current
    val app = context.applicationContext as CallAssistantApp
    val prefs = app.prefs
    val scope = rememberCoroutineScope()

    var localUrl by remember { mutableStateOf(prefs.localUrl) }
    var wgUrl by remember { mutableStateOf(prefs.wireguardUrl) }
    var token by remember { mutableStateOf(prefs.token) }
    var moduleNumber by remember { mutableStateOf(prefs.moduleNumber) }
    var owner by remember { mutableStateOf<OwnerSettings?>(null) }
    var retention by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<Pair<String, Boolean>?>(null) }

    LaunchedEffect(Unit) {
        runCatching { app.repository.settings() }.onSuccess { owner = it; retention = it.retentionDays.toString() }
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
           verticalArrangement = Arrangement.spacedBy(10.dp)) {
        message?.let { (text, error) -> MessageBanner(text, error) }

        Text("Connection", style = MaterialTheme.typography.titleMedium)
        OutlinedTextField(localUrl, { localUrl = it }, label = { Text("Home address, e.g. http://192.168.1.50:8765") },
                          singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(wgUrl, { wgUrl = it }, label = { Text("WireGuard address, e.g. http://10.8.0.1:8765") },
                          singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(token, { token = it }, label = { Text("App token") }, singleLine = true,
                          visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        Button(onClick = {
            prefs.localUrl = localUrl
            prefs.wireguardUrl = wgUrl
            prefs.token = token
            app.client.forget()
            scope.launch {
                val api = withContext(Dispatchers.IO) { app.client.connect() }
                message = if (api != null) {
                    EventPollService.start(context)
                    runCatching { app.repository.settings() }.onSuccess { owner = it; retention = it.retentionDays.toString() }
                    "Connected to ${app.client.baseUrl}" to false
                } else {
                    "Saved, but the PC did not answer at either address." to true
                }
            }
        }) { Text("Save and connect") }

        HorizontalDivider()
        Text("Owner", style = MaterialTheme.typography.titleMedium)
        owner?.let { o ->
            OutlinedTextField(o.ownerName, { owner = o.copy(ownerName = it) }, label = { Text("Your name (used in the greeting)") },
                              singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(o.ownerNumber, { owner = o.copy(ownerNumber = it) }, label = { Text("Your number (for SMS summaries)") },
                              singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
                              modifier = Modifier.fillMaxWidth())
            OutlinedTextField(retention, { retention = it.filter(Char::isDigit) }, label = { Text("Keep recordings for (days)") },
                              singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number))
            Button(onClick = {
                scope.launch {
                    message = try {
                        owner = app.repository.saveSettings(o.copy(retentionDays = retention.toIntOrNull() ?: o.retentionDays))
                        "Owner settings saved" to false
                    } catch (e: Exception) {
                        e.userMessage() to true
                    }
                }
            }) { Text("Save owner settings") }
        } ?: Text("Connect to the PC to edit these.", style = MaterialTheme.typography.bodyMedium)

        HorizontalDivider()
        Text("Call forwarding", style = MaterialTheme.typography.titleMedium)
        Text("Each button opens the dialler with a forwarding code for the module's number. " +
             "Press call for each one. Never set forwarding on the module's SIM back to your main number.",
             style = MaterialTheme.typography.bodySmall)
        OutlinedTextField(moduleNumber, { moduleNumber = it; prefs.moduleNumber = it },
                          label = { Text("Module SIM number") }, singleLine = true,
                          keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
                          modifier = Modifier.fillMaxWidth())
        val n = moduleNumber.trim()
        OutlinedButton(enabled = n.isNotEmpty(), onClick = { dial(context, "**61*$n#") }) { Text("Forward when not answered") }
        OutlinedButton(enabled = n.isNotEmpty(), onClick = { dial(context, "**67*$n#") }) { Text("Forward when busy or declined") }
        OutlinedButton(enabled = n.isNotEmpty(), onClick = { dial(context, "**62*$n#") }) { Text("Forward when unreachable") }
        Button(onClick = { dial(context, "##002#") }) { Text("Cancel all forwarding") }
    }
}
