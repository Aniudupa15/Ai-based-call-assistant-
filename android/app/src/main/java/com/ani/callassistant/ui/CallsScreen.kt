package com.ani.callassistant.ui

import android.app.Application
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.SmartToy
import androidx.compose.material.icons.filled.Voicemail
import androidx.compose.material.icons.filled.Block
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.ani.callassistant.CallAssistantApp
import com.ani.callassistant.data.CallSummary
import com.ani.callassistant.data.userMessage
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

class CallsViewModel(app: Application) : AndroidViewModel(app) {
    private val repo = (app as CallAssistantApp).repository
    val filter = MutableStateFlow<String?>(null)
    val refreshing = MutableStateFlow(false)
    val error = MutableStateFlow<String?>(null)

    val calls: StateFlow<List<CallSummary>> = combine(repo.calls, filter) { list, f ->
        if (f == null) list else list.filter { it.severity == f }
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    fun refresh() {
        viewModelScope.launch {
            refreshing.value = true
            error.value = try {
                repo.refresh()
                null
            } catch (e: Exception) {
                e.userMessage()
            }
            refreshing.value = false
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CallsScreen(onOpen: (String) -> Unit, vm: CallsViewModel = viewModel()) {
    val calls by vm.calls.collectAsStateWithLifecycle()
    val filter by vm.filter.collectAsStateWithLifecycle()
    val refreshing by vm.refreshing.collectAsStateWithLifecycle()
    val error by vm.error.collectAsStateWithLifecycle()

    LaunchedEffect(Unit) { vm.refresh() }

    Column(Modifier.fillMaxSize()) {
        error?.let { MessageBanner(it) }
        LazyRow(
            Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            item { FilterChip(selected = filter == null, onClick = { vm.filter.value = null }, label = { Text("All") }) }
            items(SEVERITIES) { s ->
                FilterChip(selected = filter == s, onClick = { vm.filter.value = if (filter == s) null else s },
                           label = { Text(severityLabel(s)) })
            }
        }
        PullToRefreshBox(isRefreshing = refreshing, onRefresh = vm::refresh, modifier = Modifier.fillMaxSize()) {
            if (calls.isEmpty() && !refreshing) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("No calls yet", style = MaterialTheme.typography.bodyLarge)
                }
            }
            LazyColumn(Modifier.fillMaxSize()) {
                items(calls, key = { it.id }) { call ->
                    CallRow(call) { onOpen(call.id) }
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun CallRow(call: CallSummary, onClick: () -> Unit) {
    val weight = if (call.seen) FontWeight.Normal else FontWeight.Bold
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Surface(shape = CircleShape, color = severityColor(call.severity), modifier = Modifier.size(12.dp)) {}
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(call.displayName, fontWeight = weight, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(
                when (call.status) {
                    "processing", "live" -> "Processing…"
                    else -> call.reason ?: call.summary ?: ""
                },
                style = MaterialTheme.typography.bodyMedium, fontWeight = weight,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
        Spacer(Modifier.width(8.dp))
        Column(horizontalAlignment = Alignment.End) {
            Text(formatTime(call.startedAt), style = MaterialTheme.typography.labelMedium)
            Icon(
                when (call.handledBy) {
                    "voicemail" -> Icons.Filled.Voicemail
                    "rejected", "ignored" -> Icons.Filled.Block
                    else -> Icons.Filled.SmartToy
                },
                contentDescription = call.handledBy,
                modifier = Modifier.size(18.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}
