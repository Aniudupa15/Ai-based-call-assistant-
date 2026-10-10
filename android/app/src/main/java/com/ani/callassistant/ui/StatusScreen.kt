package com.ani.callassistant.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.ani.callassistant.CallAssistantApp
import com.ani.callassistant.data.Status
import com.ani.callassistant.data.userMessage
import kotlinx.coroutines.launch

private fun signalText(rssi: Int): String = when {
    rssi == 99 || rssi < 0 -> "unknown"
    rssi >= 20 -> "excellent ($rssi)"
    rssi >= 15 -> "good ($rssi)"
    rssi >= 10 -> "fair ($rssi)"
    else -> "weak ($rssi)"
}

@Composable
fun StatusScreen() {
    val app = LocalContext.current.applicationContext as CallAssistantApp
    val scope = rememberCoroutineScope()
    var status by remember { mutableStateOf<Status?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var note by remember { mutableStateOf<String?>(null) }
    var reload by remember { mutableIntStateOf(0) }

    LaunchedEffect(reload) {
        try {
            status = app.repository.status()
            error = null
        } catch (e: Exception) {
            status = null
            error = e.userMessage()
        }
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
           verticalArrangement = Arrangement.spacedBy(12.dp)) {
        error?.let { MessageBanner(it) }
        note?.let { MessageBanner(it, error = false) }
        val s = status
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                StatusLine("PC", if (s != null) "reachable at ${app.client.baseUrl}" else "not reachable", s != null)
                if (s != null) {
                    StatusLine("ESP32 link", if (s.espConnected) "connected" + (s.device?.fwVersion?.let { ", firmware $it" } ?: "") else "down", s.espConnected)
                    val h = s.health
                    if (h != null) {
                        StatusLine("Module", if (h.registered) "registered on the network" else "NOT registered", h.registered)
                        StatusLine("Signal", signalText(h.rssi), h.rssi in 10..31)
                        StatusLine("microSD", "${h.sdFreeMb} MB free", h.sdFreeMb >= 20)
                        StatusLine("Power", if (h.onBattery) "on battery" else "mains", !h.onBattery)
                        Text("Last report ${formatTime(h.ts, "d MMM, HH:mm")}", style = MaterialTheme.typography.labelSmall)
                    } else {
                        StatusLine("Module", "no health report yet", false)
                    }
                    if (s.callLive) Text("A call is in progress", style = MaterialTheme.typography.bodyMedium)
                    if (s.pendingSms > 0) StatusLine("SMS", "${s.pendingSms} waiting to send", false)
                    s.device?.vmPending?.takeIf { it > 0 }?.let { StatusLine("Voicemail", "$it waiting on the card", true) }
                    Text("Models: " + s.modelsLoaded.joinToString(), style = MaterialTheme.typography.labelSmall)
                }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            OutlinedButton(onClick = { reload++ }) { Text("Refresh") }
            Button(enabled = s?.espConnected == true, onClick = {
                scope.launch {
                    note = try {
                        app.repository.testSms()
                        "Test SMS queued; it should arrive within a minute."
                    } catch (e: Exception) {
                        e.userMessage()
                    }
                }
            }) { Text("Send a test SMS") }
        }
    }
}

@Composable
private fun StatusLine(label: String, value: String, ok: Boolean) {
    Row {
        Text("$label: ", style = MaterialTheme.typography.bodyMedium)
        Text(value, style = MaterialTheme.typography.bodyMedium,
             color = if (ok) MaterialTheme.colorScheme.onSurface else Color(0xFFC62828))
    }
}
