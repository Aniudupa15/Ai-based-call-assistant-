package com.ani.callassistant.service

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.ani.callassistant.MainActivity
import com.ani.callassistant.R
import com.ani.callassistant.data.Event

object Notifications {
    const val CHANNEL_CALLS = "calls"
    const val CHANNEL_URGENT = "urgent"
    const val CHANNEL_SERVICE = "service"
    const val EXTRA_CALL_ID = "call_id"

    fun createChannels(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_CALLS, context.getString(R.string.channel_calls), NotificationManager.IMPORTANCE_DEFAULT))
        // Urgent records use a separate high-priority channel with sound.
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_URGENT, context.getString(R.string.channel_urgent), NotificationManager.IMPORTANCE_HIGH))
        nm.createNotificationChannel(NotificationChannel(
            CHANNEL_SERVICE, context.getString(R.string.channel_service), NotificationManager.IMPORTANCE_MIN))
    }

    private fun canPost(context: Context) =
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    private fun openIntent(context: Context, callId: String?): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        if (callId != null) intent.putExtra(EXTRA_CALL_ID, callId)
        return PendingIntent.getActivity(context, callId?.hashCode() ?: 0, intent,
                                         PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    /** One notification per new record. Low severity is listed in history only. */
    fun show(context: Context, event: Event) {
        if (!canPost(context)) return
        val (channel, title) = when (event.kind) {
            "call_ready" -> when (event.severity) {
                "low" -> return
                "urgent" -> CHANNEL_URGENT to "URGENT: ${event.caller ?: "call"}"
                "high" -> CHANNEL_CALLS to "High: ${event.caller ?: "call"}"
                else -> CHANNEL_CALLS to (event.caller ?: "New call")
            }
            "voicemail_received" -> CHANNEL_CALLS to "New voicemail"
            "health_alert" -> CHANNEL_URGENT to "Call assistant needs attention"
            else -> return
        }
        val text = when (event.kind) {
            "health_alert" -> "Check the module's signal, SIM, card and power on the Status screen."
            "voicemail_received" -> "Processing the recording..."
            else -> event.summary ?: "Open to see the record."
        }
        val n = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_call_assistant)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setPriority(if (channel == CHANNEL_URGENT) NotificationCompat.PRIORITY_HIGH
                         else NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(openIntent(context, event.callId))
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify((event.callId ?: event.kind).hashCode(), n)
    }

    fun serviceNotification(context: Context) =
        NotificationCompat.Builder(context, CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_call_assistant)
            .setContentTitle(context.getString(R.string.service_running))
            .setContentIntent(openIntent(context, null))
            .setOngoing(true)
            .build()
}
