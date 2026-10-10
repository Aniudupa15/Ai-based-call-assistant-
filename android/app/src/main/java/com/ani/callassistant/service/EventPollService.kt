package com.ani.callassistant.service

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.ani.callassistant.CallAssistantApp
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Long-polls GET /api/events while the PC is reachable and raises a local
 * notification for each new record. No push service is used: when the PC
 * cannot be reached, the summary SMS from the module is the notification.
 */
class EventPollService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // The specialUse type exists from Android 14; older versions take no type here.
        val type = if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0
        ServiceCompat.startForeground(this, 1, Notifications.serviceNotification(this), type)
        if (job?.isActive != true) job = scope.launch { pollLoop() }
        return START_STICKY
    }

    private suspend fun pollLoop() {
        val app = application as CallAssistantApp
        var backoffMs = 5_000L
        while (scope.isActive) {
            if (!app.prefs.configured) {
                delay(30_000)
                continue
            }
            try {
                val api = app.client.current() ?: app.client.connect()
                if (api == null) {
                    delay(backoffMs)
                    backoffMs = (backoffMs * 2).coerceAtMost(5 * 60_000L)
                    continue
                }
                backoffMs = 5_000L
                val after = app.prefs.lastEventSeq
                val result = api.events(after)
                if (after == 0L) {
                    // First run: start from now instead of notifying about old calls.
                    app.prefs.lastEventSeq = result.lastSeq
                    continue
                }
                result.events.forEach { Notifications.show(this, it) }
                if (result.events.isNotEmpty()) {
                    app.prefs.lastEventSeq = result.lastSeq
                    runCatching { app.repository.refresh() }
                }
            } catch (e: Exception) {
                app.client.forget()
                delay(backoffMs)
                backoffMs = (backoffMs * 2).coerceAtMost(5 * 60_000L)
            }
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    companion object {
        fun start(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, EventPollService::class.java))
        }
    }
}
