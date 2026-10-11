package com.ani.callassistant.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

val SEVERITIES = listOf("urgent", "high", "normal", "low")

fun severityColor(severity: String?): Color = when (severity) {
    "urgent" -> Color(0xFFC62828)
    "high" -> Color(0xFFEF6C00)
    "normal" -> Color(0xFF1F5FA8)
    "low" -> Color(0xFF78909C)
    else -> Color(0xFF9E9E9E)
}

fun severityLabel(severity: String?): String =
    severity?.replaceFirstChar { it.uppercase() } ?: "Processing"

val LANGUAGES = mapOf("en" to "English", "hi" to "Hindi", "kn" to "Kannada", "mixed" to "Mixed")

fun formatTime(epochSeconds: Long, pattern: String = "d MMM, HH:mm"): String =
    SimpleDateFormat(pattern, Locale.getDefault()).format(Date(epochSeconds * 1000))

fun formatDuration(seconds: Long): String = "%d:%02d".format(seconds / 60, seconds % 60)

fun actionLabel(action: String?): String = when (action) {
    "call_back" -> "Call back"
    "send_info" -> "Send information"
    "none", null -> "No action needed"
    else -> action
}

/** Opens the dialler with the number filled in; no call permission is needed. */
fun dial(context: Context, number: String) {
    context.startActivity(Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number))))
}

@Composable
fun SeverityBadge(severity: String?, modifier: Modifier = Modifier) {
    Box(
        modifier
            .background(severityColor(severity), RoundedCornerShape(6.dp))
            .padding(horizontal = 8.dp, vertical = 2.dp)
    ) {
        Text(severityLabel(severity), color = Color.White, style = MaterialTheme.typography.labelMedium,
             fontWeight = FontWeight.Bold)
    }
}

@Composable
fun MessageBanner(text: String, error: Boolean = true) {
    Surface(
        color = if (error) MaterialTheme.colorScheme.errorContainer else MaterialTheme.colorScheme.secondaryContainer,
        modifier = Modifier.fillMaxWidth()
    ) {
        Text(text, Modifier.padding(12.dp), style = MaterialTheme.typography.bodyMedium)
    }
}

/** A labelled button that opens a list of choices. */
@Composable
fun OptionPicker(
    label: String,
    value: String?,
    options: List<Pair<String?, String>>,
    onSelect: (String?) -> Unit,
    modifier: Modifier = Modifier,
) {
    var open by remember { mutableStateOf(false) }
    Row(modifier, verticalAlignment = Alignment.CenterVertically) {
        Text("$label: ", style = MaterialTheme.typography.bodyMedium)
        Box {
            OutlinedButton(onClick = { open = true }) {
                Text(options.firstOrNull { it.first == value }?.second ?: "—")
            }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                options.forEach { (key, text) ->
                    DropdownMenuItem(text = { Text(text) }, onClick = { open = false; onSelect(key) })
                }
            }
        }
    }
}
