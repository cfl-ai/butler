plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.aibutler.aibutler_mobile"
    compileSdk = flutter.compileSdkVersion
    // **不设 ndkVersion**：模板默认写 ndkVersion = flutter.ndkVersion，
    // 那会强制 AGP 去安装一份约 1GB 的 NDK —— 而本项目**没有自己的原生代码**
    // （Flutter 引擎与各插件的 .so 都是预编译产物，走 AAR 分发）。
    // 去掉后构建不再拉 NDK；将来真引入自写 C/C++ 时再加回来。
    // ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.aibutler.aibutler_mobile"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

dependencies {
    // 常驻唤醒服务用的是 NotificationCompat / ServiceCompat（向后兼容的写法）。
    implementation("androidx.core:core-ktx:1.13.1")
}

flutter {
    source = "../.."
}
