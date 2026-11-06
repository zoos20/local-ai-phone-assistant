/**
 * Foreground-уведомления сервиса (перенесено из AssistantService).
 */
package ru.gemma.assistant

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context.NOTIFICATION_SERVICE
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat

private const val CHANNEL_ID = "gemma_service"
private const val NOTIFICATION_ID = 1

internal fun Service.createChannel() {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val channel = NotificationChannel(
            CHANNEL_ID, "Ассистент Гемма",
            NotificationManager.IMPORTANCE_LOW
        ).apply { setShowBadge(false) }
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
            .createNotificationChannel(channel)
    }
}

internal fun Service.buildNotification(text: String): Notification {
    val openApp = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE
    )
    return NotificationCompat.Builder(this, CHANNEL_ID)
        .setSmallIcon(android.R.drawable.ic_btn_speak_now)
        .setContentTitle("Гемма")
        .setContentText(text)
        .setContentIntent(openApp)
        .setOngoing(true)
        .setSilent(true)
        .build()
}

internal fun Service.startForegroundCompat() {
    val notif = buildNotification("Запуск…")
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
        startForeground(
            NOTIFICATION_ID, notif,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        )
    } else {
        startForeground(NOTIFICATION_ID, notif)
    }
}

internal fun Service.updateNotification(text: String) {
    try {
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
            .notify(NOTIFICATION_ID, buildNotification(text))
    } catch (_: Exception) {
    }
}
