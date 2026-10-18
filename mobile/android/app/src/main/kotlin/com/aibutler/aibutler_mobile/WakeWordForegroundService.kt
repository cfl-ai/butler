package com.aibutler.aibutler_mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * 后台常驻唤醒的前台服务（三、P7「离线唤醒、随时打断」）。
 *
 * 为什么必须有它
 * --------------
 * Android 从 8.0 起限制后台应用访问麦克风；从 **14（API 34）** 起，
 * 前台服务还**必须声明 `foregroundServiceType` 并持有对应权限**，
 * 否则 `startForeground` 直接抛 `SecurityException`。
 * 也就是说「揣在兜里也能喊醒」这件事，在 Android 14+ 上
 * **只有靠一个类型为 microphone 的前台服务才成立**。
 *
 * 它自己不采集音频 —— 采集由 Dart 侧的 record 插件完成。
 * 这个服务的职责只有一个：**让进程在后台存活并持有麦克风使用权**，
 * 从而让唤醒词引擎可以持续拿到采样。
 *
 * 说明：能否在真机上稳定后台唤醒，**只有真机可以回答**（见裁定 U23）。
 * 本类提供的是必要条件，不是充分条件的证明。
 */
class WakeWordForegroundService : Service() {

    companion object {
        const val CHANNEL_ID = "aibutler_wake_word"
        const val NOTIFICATION_ID = 1001
        const val ACTION_START = "com.aibutler.aibutler_mobile.START_WAKE"
        const val ACTION_STOP = "com.aibutler.aibutler_mobile.STOP_WAKE"

        fun start(context: Context) {
            val intent = Intent(context, WakeWordForegroundService::class.java).apply {
                action = ACTION_START
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, WakeWordForegroundService::class.java).apply {
                    action = ACTION_STOP
                }
            )
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            else -> startAsForeground()
        }
        // 被系统回收后重建，保证「随时能唤醒」不因进程回收而中断
        return START_STICKY
    }

    private fun startAsForeground() {
        createChannelIfNeeded()
        val notification: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("AI 管家正在待命")
            .setContentText("说出唤醒词即可唤起管家")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // Android 14+ 必须给出类型，且要与清单里声明的、以及已授予的权限一致
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun createChannelIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "常驻唤醒",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "保持离线唤醒在后台可用"
            setShowBadge(false)
        }
        manager.createNotificationChannel(channel)
    }
}
