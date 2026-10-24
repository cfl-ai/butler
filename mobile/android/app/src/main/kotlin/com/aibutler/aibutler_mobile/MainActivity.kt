package com.aibutler.aibutler_mobile

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.app.ActivityCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

/**
 * Flutter 与 Android 原生之间的桥。
 *
 * 目前只桥一件事：**后台常驻唤醒的前台服务**。
 * 其余能力（录音、定位、安全存储）都由各自的 Flutter 插件负责，不走这里。
 *
 * 为什么这条必须自己写
 * --------------------
 * 「揣在兜里也能喊醒」需要一个 foregroundServiceType="microphone" 的前台服务
 * （Android 14+ 强制）。pub 上没有正好做这件事的插件，
 * 而它本身只有几十行，自己写比引一个不透明的依赖更可控。
 */
class MainActivity : FlutterActivity() {

    private val channelName = "com.aibutler.aibutler_mobile/wake_service"

    companion object {
        private const val NOTIFICATION_REQUEST_CODE = 1001
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "start" -> {
                        try {
                            WakeWordForegroundService.start(this)
                            result.success(true)
                        } catch (error: Exception) {
                            // 例如通知被禁用、或没给麦克风权限 ——
                            // 如实把失败原因交回 Dart，不要吞掉。
                            result.error("WAKE_SERVICE_START_FAILED", error.message, null)
                        }
                    }
                    "stop" -> {
                        WakeWordForegroundService.stop(this)
                        result.success(true)
                    }
                    "requestNotification" -> {
                        // Android 13+ 通知必须运行时授权，否则常驻通知不可见 ——
                        // 用户看不到「正在待命」，也没法一键停掉麦克风，属于隐蔽占用。
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                            ActivityCompat.checkSelfPermission(
                                this, Manifest.permission.POST_NOTIFICATIONS
                            ) != PackageManager.PERMISSION_GRANTED
                        ) {
                            ActivityCompat.requestPermissions(
                                this,
                                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                                NOTIFICATION_REQUEST_CODE,
                            )
                            // 系统弹窗是异步的：此刻如实回 false（尚未授予），
                            // 不假装已经拿到，等用户点了之后下次再查为准。
                            result.success(false)
                        } else {
                            result.success(true)
                        }
                    }
                    "openNotificationSettings" -> {
                        // 常驻通知被关掉时前台服务会被系统降级，引导用户去开
                        val intent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).apply {
                            putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
                            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        }
                        startActivity(intent)
                        result.success(true)
                    }
                    "openAppSettings" -> {
                        val intent = Intent(
                            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            Uri.fromParts("package", packageName, null)
                        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        startActivity(intent)
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
    }
}
