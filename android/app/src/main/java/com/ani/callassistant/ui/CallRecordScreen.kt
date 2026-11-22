package com.ani.callassistant.ui

import android.app.Application
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import com.ani.callassistant.CallAssistantApp
import com.ani.callassistant.data.CallSummary
import com.ani.callassistant.data.TranscriptTurn
import com.ani.callassistant.data.userMessage
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

@androidx.annotation.OptIn(UnstableApi::class)
class CallRecordViewModel(app: Application) : AndroidViewModel(app) {
    private val ca = app as CallAssistantApp
    private val repo = ca.repository
    private var callId: String? = null
    private var loadedAudio: String? = null

    val error = MutableStateFlow<String?>(null)
    val playing = MutableStateFlow(false)
    val positionMs = MutableStateFlow(0L)
    val durationMs = MutableStateFlow(0L)
    lateinit var call: StateFlow<CallSummary?>
        private set

    /** Streams the recording with the app token, caching it after the first play. */
    private val playerLazy = lazy {
        val http = DefaultHttpDataSource.Factory().setDefaultRequestProperties(repo.authHeader)
        val cached = CacheDataSource.Factory().setCache(ca.audioCache).setUpstreamDataSourceFactory(http)
        ExoPlayer.Builder(app).setMediaSourceFactory(DefaultMediaSourceFactory(cached)).build().apply {
            addListener(object : Player.Listener {
                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    playing.value = isPlaying
                }
                override fun onPlaybackStateChanged(state: Int) {
                    if (state == Player.STATE_READY) durationMs.value = duration.coerceAtLeast(0)
                }
            })
        }
    }
    val player: ExoPlayer by playerLazy

    fun open(id: String) {
        if (callId == id) return
        callId = id
        call = repo.call(id).stateIn(viewModelScope, SharingStarted.Eagerly, null)
        viewModelScope.launch {
            try {
                val c = repo.loadDetail(id)
                if (!c.seen) repo.markSeen(id)
                error.value = null
            } catch (e: Exception) {
                error.value = e.userMessage()
            }
        }
        viewModelScope.launch {
            while (true) {
                if (playing.value) positionMs.value = player.currentPosition
                delay(200)
            }
        }
    }

    private fun prepare(c: CallSummary): Boolean {
        val url = repo.audioUrl(c) ?: return false
        if (loadedAudio != c.id) {
            // Cache by call ID, so home and WireGuard addresses share one copy.
            player.setMediaItem(MediaItem.Builder().setUri(url).setCustomCacheKey(c.id).build())
            player.prepare()
            loadedAudio = c.id
        }
        return true
    }

    fun toggle(c: CallSummary) {
        if (!prepare(c)) {
            error.value = "Connect to the PC to play the recording the first time."
            return
        }
        if (player.isPlaying) player.pause() else player.play()
    }

    fun seekTo(c: CallSummary, ms: Long) {
        if (!prepare(c)) return
        player.seekTo(ms)
        positionMs.value = ms
        player.play()
    }

    fun correct(severity: String) = viewModelScope.launch {
        callId?.let { id -> runCatching { repo.correctSeverity(id, severity) }.onFailure { error.value = it.userMessage() } }
    }

    fun delete(done: () -> Unit) = viewModelScope.launch {
        callId?.let { id ->
            try {
                repo.delete(id)
                done()
            } catch (e: Exception) {
                error.value = e.userMessage()
            }
        }
    }

    override fun onCleared() {
        if (playerLazy.isInitialized()) player.release()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CallRecordScreen(callId: String, onBack: () -> Unit, vm: CallRecordViewModel = viewModel()) {
    remember(callId) { vm.open(callId); true }
    val call by vm.call.collectAsStateWithLifecycle()
    val error by vm.error.collectAsStateWithLifecycle()
    var confirmDelete by remember { mutableStateOf(false) }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text(call?.displayName ?: "Call") },
            navigationIcon = {
                IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
            },
            actions = {
                IconButton(onClick = { confirmDelete = true }) { Icon(Icons.Filled.Delete, "Delete") }
            }
        )
    }) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            error?.let { MessageBanner(it) }
            call?.let { RecordBody(it, vm) }
        }
    }

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Delete this call?") },
            text = { Text("The record and its recording are removed from the PC.") },
            confirmButton = { TextButton(onClick = { confirmDelete = false; vm.delete(onBack) }) { Text("Delete") } },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Cancel") } }
        )
    }
}

@Composable
private fun RecordBody(c: CallSummary, vm: CallRecordViewModel) {
    val context = LocalContext.current
    val playing by vm.playing.collectAsStateWithLifecycle()
    val position by vm.positionMs.collectAsStateWithLifecycle()
    val duration by vm.durationMs.collectAsStateWithLifecycle()

    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        // 1. Caller, time and duration
        item {
            Column {
                Spacer(Modifier.height(4.dp))
                Text(c.number.ifEmpty { "Withheld number" }, style = MaterialTheme.typography.bodyMedium)
                if (c.callerName != null && c.callerName != c.contact) {
                    Text("Said on the call: ${c.callerName}", style = MaterialTheme.typography.bodyMedium)
                }
                Text("${formatTime(c.startedAt, "EEE d MMM yyyy, HH:mm")} · ${formatDuration(c.durationS.toLong())} · " +
                     when (c.handledBy) { "bot" -> "Answered by the assistant"; "voicemail" -> "Voicemail"; else -> c.handledBy.replaceFirstChar { it.uppercase() } },
                     style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        if (c.status == "processing" || c.status == "live") {
            item { MessageBanner("Processing: reason and severity appear shortly.", error = false) }
        }
        // 2. Severity and why, with correction
        item {
            Column {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    SeverityBadge(c.severity)
                    Spacer(Modifier.width(8.dp))
                    Text(c.severityWhy ?: "", style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
                }
                OptionPicker("Correct severity", c.severity, SEVERITIES.map { it to severityLabel(it) },
                             onSelect = { it?.let(vm::correct) }, modifier = Modifier.padding(top = 4.dp))
            }
        }
        // 3. Reason
        item {
            Column {
                Text("Reason", style = MaterialTheme.typography.titleSmall)
                Text(c.reason ?: c.summary ?: "—", style = MaterialTheme.typography.bodyLarge)
                if (c.summary != null && c.summary != c.reason) {
                    Text(c.summary, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
        // 4. Action needed, with Call back
        item {
            Column {
                Text("Action", style = MaterialTheme.typography.titleSmall)
                Text(actionLabel(c.actionNeeded) + (c.deadline?.let { " · by $it" } ?: ""))
                val number = c.callbackNumber ?: c.number
                if (number.isNotEmpty()) {
                    Button(onClick = { dial(context, number) }, modifier = Modifier.padding(top = 4.dp)) {
                        Icon(Icons.Filled.Call, null)
                        Spacer(Modifier.width(8.dp))
                        Text(if (c.callbackNumber != null) "Call back on $number" else "Call back")
                    }
                }
            }
        }
        // 5. What the bot answered, and flags
        if (c.botCommitments.isNotEmpty()) {
            item {
                Column {
                    Text("What the assistant said", style = MaterialTheme.typography.titleSmall)
                    c.botCommitments.forEach { Text("• $it") }
                }
            }
        }
        if (c.flags.isNotEmpty()) {
            item {
                Column {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        c.flags.forEach { AssistChip(onClick = {}, label = { Text(it.replace('_', ' ')) }) }
                    }
                }
            }
        }
        // 6. Audio player
        if (c.audioUrl != null) {
            item {
                Column {
                    Card(Modifier.fillMaxWidth()) {
                        Row(Modifier.padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
                            IconButton(onClick = { vm.toggle(c) }) {
                                Icon(if (playing) Icons.Filled.Pause else Icons.Filled.PlayArrow, if (playing) "Pause" else "Play")
                            }
                            val total = if (duration > 0) duration else c.durationS * 1000L
                            Slider(
                                value = if (total > 0) (position.toFloat() / total).coerceIn(0f, 1f) else 0f,
                                onValueChange = { vm.seekTo(c, (it * total).toLong()) },
                                modifier = Modifier.weight(1f)
                            )
                            Text("${formatDuration(position / 1000)} / ${formatDuration(total / 1000)}",
                                 style = MaterialTheme.typography.labelSmall)
                        }
                    }
                }
            }
        }
        // 7. Transcript; tap a line to jump the audio there
        if (c.turns.isNotEmpty()) {
            item { Text("Transcript", style = MaterialTheme.typography.titleSmall) }
            items(c.turns, key = { it.seq }) { t -> TurnLine(t, active = position in t.startMs..t.endMs) { vm.seekTo(c, t.startMs) } }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

@Composable
private fun TurnLine(t: TranscriptTurn, active: Boolean, onClick: () -> Unit) {
    val bot = t.speaker == "bot"
    val color = if (bot) MaterialTheme.colorScheme.primary else Color(0xFF2E7D32)
    Column(
        Modifier.fillMaxWidth().clickable(onClick = onClick)
            .padding(vertical = 4.dp)
    ) {
        Text(
            (if (bot) "Assistant" else "Caller") + " · " + formatDuration(t.startMs / 1000),
            style = MaterialTheme.typography.labelSmall, color = color,
            fontWeight = if (active) FontWeight.Bold else FontWeight.Normal
        )
        Text(t.text.ifEmpty { "(inaudible)" }, fontWeight = if (active) FontWeight.SemiBold else FontWeight.Normal)
        // English beneath any Hindi or Kannada line.
        t.textEn?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
